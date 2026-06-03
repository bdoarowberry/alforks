"""Detect attempts of a saved route in past rides + build a per-route
leaderboard.

A route is an ordered list of edge segments (trail or road sub-segments
between two junctions on a region's named-way graph). An "attempt" of a
route in a given ride is a strict, in-order, no-detour subsequence of
the ride's trail_match timeline whose entries each:

  * match the segment's (trail_name, kind, direction), AND
  * include GPS points within ENDPOINT_TOUCH_RADIUS_M of both the
    segment's edge endpoints.

Strict ordering + no-detour means: after a segment is matched, the next
*timeline* entry must match the next segment. Any intervening timeline
entry breaks the attempt — reset and re-evaluate from segment zero.
Gap *time* (a coffee break, or off-named-way riding that produces no
timeline entry) is fine and is counted in the total duration.

Attempts are scored by `last_match.end_time - first_match.start_time`,
gap-inclusive. Leaderboard is sorted fastest first.

The module is Flask-agnostic. `detect_attempts_for_route` takes an
`activity_loader` callable so the caller (app.py) wires it to
`get_activity`. This avoids a circular import and keeps the matcher
unit-friendly.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

import trail_match

# Bump to invalidate per-route cached attempts (e.g. when the matching
# rule changes). The cache file stores this alongside the data so old
# entries are detected and recomputed transparently.
ROUTE_ATTEMPTS_VERSION = 4   # v4: accumulate fragmented same-trail runs + ignore noise entries

# Re-export from trail_match so callers don't need to dig into private
# names — the endpoint-touch radius is the central tuning knob here.
ENDPOINT_TOUCH_RADIUS_M = trail_match.ENDPOINT_TOUCH_RADIUS_M

logger = logging.getLogger(__name__)


# Two same-trail entries are treated as one fragmented run only if the second
# is BOTH adjacent in GPS-sample index AND within a generous wall-clock ceiling
# of the first. Index adjacency is the primary signal — a brief dropout or
# internal run-split leaves few samples between fragments — and crucially it
# survives a mid-trail pause (real rides pause ~3 min between split fragments
# while index stays tiny). The time ceiling guards the opposite failure mode:
# "smart recording" (1 sample per 10-15 s) could leave a small index gap across
# two genuinely separate visits to the same trail; the ceiling rejects those so
# we don't merge them into one multi-hour "attempt".
_MAX_FRAGMENT_GAP_IDX = 250
_MAX_FRAGMENT_GAP_SEC = 900.0   # 15 min — generous enough for real rest stops


def _fragment_gap_ok(prev: dict, e: dict) -> bool:
    """True if `e` starts close enough after `prev` to be the same fragmented
    run: adjacent in sample index AND (when timestamps are present) within the
    wall-clock ceiling. Timestamps absent → index alone decides."""
    gap_idx = int(e.get("start_idx") or 0) - int(prev.get("end_idx") or 0)
    if not (0 <= gap_idx <= _MAX_FRAGMENT_GAP_IDX):
        return False
    pe, es = prev.get("end_time"), e.get("start_time")
    if pe and es:
        try:
            gap = (datetime.fromisoformat(es) - datetime.fromisoformat(pe)).total_seconds()
            return gap <= _MAX_FRAGMENT_GAP_SEC
        except ValueError:
            pass
    return True


# Coverage-fallback tuning. An edge/segment "matches" if endpoint-touch passes
# OR the ride's GPS follows at least COVERAGE_MIN_FRAC of the edge polyline
# within COVERAGE_RADIUS_M. Coverage is the robust fallback for misplaced OSM
# junctions (where the rider clearly rode the line but never passed within
# ENDPOINT_TOUCH_RADIUS_M of the off-position junction node). It is only ever
# consulted when endpoint-touch already failed, so it can never loosen an
# existing match — only recover one. Both the polyline and the ride span are
# strided for speed; the inner scan early-exits the moment a vertex is covered.
_COVERAGE_RADIUS_M = 40.0
_COVERAGE_MIN_FRAC = 0.75
_COVERAGE_STRIDE   = 3


def _run_covers(points: list, s: int, e: int, polyline: list,
                radius_m: float = _COVERAGE_RADIUS_M,
                min_frac: float = _COVERAGE_MIN_FRAC) -> bool:
    """True if at least `min_frac` of `polyline`'s vertices lie within
    `radius_m` of some GPS point in points[s:e]. Geometry-coverage answer to
    "did the rider follow this edge's shape?", independent of where OSM placed
    the junction nodes."""
    if not polyline:
        return False
    verts = polyline[::_COVERAGE_STRIDE]
    needed = min_frac * len(verts)
    near = 0
    remaining = len(verts)
    for vlat, vlon in verts:
        covered = False
        for i in range(s, e, _COVERAGE_STRIDE):
            try:
                if trail_match._haversine_m(vlat, vlon, points[i]["lat"], points[i]["lon"]) <= radius_m:
                    covered = True
                    break
            except (KeyError, IndexError):
                continue
        if covered:
            near += 1
            if near >= needed:
                return True
        remaining -= 1
        if near + remaining < needed:   # can't reach the threshold anymore
            return False
    return near >= needed


def _coalesce_timeline(timeline: list[dict]) -> list[dict]:
    """Merge consecutive timeline entries that share (name, kind, direction)
    and sit within a short gap (see `_fragment_gap_ok`) into one entry spanning
    their combined index/time range.

    trail_match can split a single continuous traversal of one trail into
    several timeline entries (brief GPS gaps, internal run-segmentation). The
    route model treats that trail as one edge group, so an un-coalesced
    timeline makes BOTH route-building and attempt-matching miss long edges:
    no single fragment's GPS span reaches both junction endpoints of the edge,
    so the endpoint-touch test fails on every fragment even though the rider
    clearly rode the whole thing. Coalescing first restores the full span.

    Direction is part of the merge key on purpose: an out-and-back on one trail
    (up then immediately down) is two legitimate segments in a route, and
    trail_match emits two entries for it — merging across the reversal would
    collapse them and LOSE that attempt. Same-direction fragments are the only
    thing this joins.

    Returns a new list of shallow-copied entries — never mutates the input
    (the caller's `timeline` is the in-memory-cached trail_match result). Note
    the merged entry's per-fragment aggregates (`distance_km`, `duration_sec`,
    `points`) are NOT summed — only the span (`start_idx`/`end_idx`/`end_time`)
    is extended, which is all the endpoint-touch + duration-from-timestamps
    consumers read.
    """
    merged: list[dict] = []
    for e in timeline:
        if merged:
            prev = merged[-1]   # already a copy (see append below)
            if (prev.get("name") == e.get("name")
                    and prev.get("kind") == e.get("kind")
                    and prev.get("direction") == e.get("direction")
                    and _fragment_gap_ok(prev, e)):
                if e.get("end_idx") is not None:
                    prev["end_idx"] = e.get("end_idx")
                if e.get("end_time"):
                    prev["end_time"] = e.get("end_time")
                continue
        merged.append(dict(e))
    return merged


# ─── Endpoint lookup ────────────────────────────────────────────────────────

def _build_node_xy(region_artifact: dict) -> dict[str, tuple[float, float]]:
    """node_id -> (lat, lon) across junctions + per-entry endpoints
    (component pseudo-nodes). A single lookup covers trail and road edges."""
    node_xy: dict[str, tuple[float, float]] = {}
    for j in region_artifact.get("junctions") or []:
        node_xy[j["id"]] = (j["lat"], j["lon"])
    for entry in (region_artifact.get("trails") or []) + (region_artifact.get("roads") or []):
        for ep in entry.get("endpoints") or []:
            node_xy[ep["id"]] = (ep["lat"], ep["lon"])
    return node_xy


def _segment_endpoints(route: dict, region_artifact: dict
                        ) -> list[tuple[str, str, str, tuple, tuple, list]]:
    """For each segment in `route`, find its trail_name, kind, direction, the
    (lat, lon) of its two edge endpoints, and the edge polyline.

    Returns a list of `(trail_name, kind, direction, term_a, term_b, polyline)`
    where consecutive same-(name, kind, direction) segments are folded into one
    macro-segment: span endpoints become the first segment's start and the last
    segment's end, and `polyline` is the concatenation of the folded edges'
    polylines (used by the coverage fallback when endpoint-touch fails). A
    segment whose edge_id is unknown (stale artifact rebuild) is dropped
    silently and logged at DEBUG.
    """
    node_xy = _build_node_xy(region_artifact)

    # edge_id -> (trail_name, kind) and edge_id -> polyline. The artifact is
    # the source of truth for both the canonical name/kind and the geometry.
    edge_owner: dict[str, tuple[str, str]] = {}
    edge_poly: dict[str, list] = {}
    for kind_label, collection in (("trail", region_artifact.get("trails") or []),
                                    ("road", region_artifact.get("roads") or [])):
        for entry in collection:
            for e in entry.get("edges") or []:
                edge_owner[e["id"]] = (entry["name"], kind_label)
                edge_poly[e["id"]] = e.get("polyline") or []

    # First pass: resolve each route segment to (name, kind, direction,
    # start_xy, end_xy, polyline). Drop unresolvable segments silently.
    resolved: list[tuple[str, str, str, tuple, tuple, list]] = []
    for seg in route.get("segments") or []:
        owner = edge_owner.get(seg.get("edge_id"))
        if not owner:
            logger.debug("route %s: edge_id %s not in artifact; segment dropped",
                          route.get("id"), seg.get("edge_id"))
            continue
        trail_name, kind = owner
        # Prefer the segment's recorded trail_name + kind if they disagree
        # with the artifact (e.g. a route saved before a rename). Artifact
        # is authoritative for endpoints; segment is authoritative for
        # what the user *meant* to ride.
        trail_name = seg.get("trail_name") or trail_name
        kind       = seg.get("kind")       or kind
        a = node_xy.get(seg.get("start_junction"))
        b = node_xy.get(seg.get("end_junction"))
        if a is None or b is None:
            logger.debug("route %s: junction id missing in artifact; segment dropped",
                          route.get("id"))
            continue
        direction = seg.get("direction") or "forward"
        resolved.append((trail_name, kind, direction, a, b,
                         edge_poly.get(seg.get("edge_id")) or []))

    # Second pass: fold consecutive segments on the same (trail_name, kind,
    # direction) into one macro-segment. trail_match collapses a continuous
    # ride along a trail into ONE timeline entry; a route built from
    # several consecutive edges on the same trail must match that single
    # entry, not look for N separate entries. Span endpoints become the
    # first segment's start and the last segment's end; polylines concatenate.
    folded: list[tuple[str, str, str, tuple, tuple, list]] = []
    for trail_name, kind, direction, a, b, poly in resolved:
        if folded and folded[-1][0] == trail_name \
                  and folded[-1][1] == kind \
                  and folded[-1][2] == direction:
            ptn, pkd, pdr, pa, _pb, ppoly = folded[-1]
            folded[-1] = (ptn, pkd, pdr, pa, b, ppoly + poly)
        else:
            folded.append((trail_name, kind, direction, a, b, poly))
    return folded


# ─── Reverse: build route segments from a ride ──────────────────────────────

def _edge_order_index(points: list, lo: int, hi: int, edge: dict) -> int:
    """Ride index (within [lo, hi)) of closest approach to the edge's
    midpoint. Used to order a trail's ridden edges along the actual ride,
    correctly for either travel direction (midpoint is passed regardless of
    which end you enter from). Falls back to `lo` if nothing resolves."""
    pl = edge.get("polyline") or []
    if not pl:
        return lo
    mlat, mlon = pl[len(pl) // 2][0], pl[len(pl) // 2][1]
    best_i, best_d = lo, float("inf")
    for i in range(lo, hi):
        try:
            d = trail_match._haversine_m(points[i]["lat"], points[i]["lon"], mlat, mlon)
        except (KeyError, IndexError):
            continue
        if d < best_d:
            best_d, best_i = d, i
    return best_i


def build_segments_from_ride(timeline: list[dict],
                             points: list[dict],
                             region_artifact: dict) -> list[dict]:
    """Reverse of `detect_attempts_for_route`: turn a ride's trail_match
    timeline into an ordered list of route segments.

    For each timeline entry (a trail the ride was matched onto), find the
    artifact trail of the same (name, kind) and keep the edges whose BOTH
    endpoints the ride actually touched during that entry's GPS span — the
    same endpoint-touch test the detector uses — ordered along the ride.

    Because the kept edges are exactly the ones the ride covered and they're
    emitted in ride order, the resulting segment list macro-folds back to the
    ridden spans: a route saved from it re-detects this ride as an attempt.
    Segments carry the timeline's direction (recorded, not enforced in v1).
    """
    # Same coalescing the detector uses — so a trail trail_match split into
    # several runs is rebuilt as one continuous edge group, not dropped.
    timeline = _coalesce_timeline(timeline)
    node_xy = _build_node_xy(region_artifact)

    by_name: dict[tuple[str, str], dict] = {}
    for entry in region_artifact.get("trails") or []:
        by_name[(entry["name"], "trail")] = entry
    for entry in region_artifact.get("roads") or []:
        by_name[(entry["name"], "road")] = entry

    segments: list[dict] = []
    for e in timeline:
        name = e.get("name")
        kind = e.get("kind") or "trail"
        direction = e.get("direction") or "forward"
        lo = int(e.get("start_idx") or 0)
        hi = int(e.get("end_idx") or 0) + 1
        entry = by_name.get((name, kind))
        if entry is None:
            continue
        touched: list[tuple[int, dict]] = []
        for edge in entry.get("edges") or []:
            a = node_xy.get(edge.get("start_node"))
            b = node_xy.get(edge.get("end_node"))
            if a is None or b is None:
                # Junction nodes don't resolve in the artifact — detection would
                # drop a segment built from this edge anyway (it re-resolves the
                # same ids), so don't emit a dead segment. (Misplaced-but-present
                # junctions still resolve here; only genuinely-missing ones skip.)
                continue
            # Include the edge if the ride touched both junctions OR followed
            # the edge's line (coverage) — the latter recovers edges whose OSM
            # junction nodes are misplaced off the actual trail.
            touched_ep = trail_match._run_touches_both(points, lo, hi, a, b, ENDPOINT_TOUCH_RADIUS_M)
            if touched_ep or _run_covers(points, lo, hi, edge.get("polyline") or []):
                touched.append((_edge_order_index(points, lo, hi, edge), edge))
        touched.sort(key=lambda t: t[0])
        for _idx, edge in touched:
            segments.append({
                "edge_id":        edge["id"],
                "trail_name":     name,
                "kind":           kind,
                "direction":      direction,
                "start_junction": edge.get("start_node"),
                "end_junction":   edge.get("end_node"),
            })
    return segments


# ─── Direction policy ────────────────────────────────────────────────────────
# Direction is recorded on segments/timeline entries but NOT enforced when
# matching. The builder writes every segment `forward` (no trail-topology data
# to distinguish up/down), while trail_match emits a mix of up/down/forward/
# reverse — so a polarity rule rejected genuine attempts. Matching now keys on
# (name, kind) plus endpoint-touch/coverage and strict ordering; the
# accumulation step deliberately joins fragments across directions (e.g. a
# down-then-up snap of one climb), and genuine out-and-backs survive because a
# full traversal satisfies its segment on a single entry (minimal consumption).
# Revisit enforcement only when the builder learns each trail's topology.


def _same_trail_as_adjacent(entry: dict, segs: list, i: int) -> bool:
    """True if `entry` is the same (name, kind) as the segment just matched
    (`segs[i-1]`) or the one expected next (`segs[i]`). Used to tell a
    same-trail wiggle / unmapped fragment (skip, stay in the attempt) apart
    from a real detour onto a different trail (reset). The caller only invokes
    this mid-attempt, so 1 <= i < len(segs) holds and both indices are valid."""
    en, ek = entry.get("name"), entry.get("kind")
    return ((en == segs[i][0] and ek == segs[i][1])
            or (en == segs[i - 1][0] and ek == segs[i - 1][1]))


# A timeline entry this small is GPS junk (a momentary mis-snap while stopped
# or wandering a summit), never a real traversal or a real detour — so it
# neither satisfies nor breaks an attempt. Both conditions must hold.
_NOISE_MAX_COV_PCT = 8.0
_NOISE_MAX_DIST_KM = 0.15


def _is_noise_entry(entry: dict) -> bool:
    # Only call it noise when we have positive evidence it's tiny — a missing
    # coverage/distance field is NOT evidence of noise.
    cov = entry.get("coverage_pct")
    dist = entry.get("distance_km")
    return (cov is not None and cov < _NOISE_MAX_COV_PCT
            and dist is not None and dist < _NOISE_MAX_DIST_KM)


def _try_match_segment(points: list, timeline: list, j: int,
                       name: str, kind: str, term_a, term_b, polyline) -> int | None:
    """Try to satisfy one route segment starting at timeline index `j` by
    accumulating CONSECUTIVE entries on the same (name, kind) trail — any
    direction — into a growing GPS span until endpoint-touch or coverage is
    met. trail_match can split one real traversal into several partial
    fragments (GPS noise, a back-and-forth at a summit); none may clear the
    bar alone, but their combined span does.

    Returns the timeline index just past the last entry consumed on success,
    or None if the same-name run is exhausted without satisfying. Consumes the
    MINIMUM run needed: a full single traversal matches on its first entry, so
    a genuine out-and-back (two same-name route segments) keeps its two legs
    distinct rather than being swallowed by one segment."""
    n = len(timeline)
    if j >= n or timeline[j].get("name") != name or timeline[j].get("kind") != kind:
        return None
    s = int(timeline[j].get("start_idx") or 0)
    k = j
    while k < n and timeline[k].get("name") == name and timeline[k].get("kind") == kind:
        # Only accumulate fragments that are actually adjacent — same gap bound
        # as coalescing/skip. Without this, two separate visits to one trail
        # that happen to be timeline-consecutive (nothing between) could merge
        # into one phantom attempt spanning the whole ride.
        if k > j and not _fragment_gap_ok(timeline[k - 1], timeline[k]):
            break
        e = int(timeline[k].get("end_idx") or 0) + 1
        if (trail_match._run_touches_both(points, s, e, term_a, term_b, ENDPOINT_TOUCH_RADIUS_M)
                or _run_covers(points, s, e, polyline)):
            return k + 1
        k += 1
    return None


# ─── Per-route attempt detection ────────────────────────────────────────────

def detect_attempts_for_route(
    route: dict,
    region_artifact: dict,
    trail_match_cache_dir: Path,
    activity_loader: Callable[[str], dict | None],
    activity_meta: dict[str, dict] | None = None,
) -> list[dict]:
    """Scan every cached trail_match result and return route attempts.

    `activity_loader(filename)` should return the parsed activity dict
    (same shape as `get_activity`) with at least `points`. Called once
    per ride that has at least one timeline entry matching the route's
    first segment (the obvious quick prune below skips otherwise).
    """
    segs = _segment_endpoints(route, region_artifact)
    if not segs:
        return []
    activity_meta = activity_meta or {}

    # Quick prune: any ride whose timeline has no entry with the same name
    # AND kind as the route's first segment can't be an attempt, skip the
    # activity_loader call entirely. The loader is the most expensive step
    # (~10 ms warm per ride for the GPX parse).
    first_name, first_kind, *_ = segs[0]

    attempts: list[dict] = []
    for filename, _mtime, result in trail_match.scan_cached_results(trail_match_cache_dir):
        timeline = (result or {}).get("timeline") or []
        if not timeline:
            continue
        # Prune on the RAW timeline first — coalescing only merges same-name
        # entries, so it never adds or drops a name; the prune result is
        # identical either way, and pruning first avoids the coalesce copy for
        # rides that can't match anyway.
        if not any(e.get("name") == first_name and e.get("kind") == first_kind
                    for e in timeline):
            continue
        # Coalesce fragmented same-trail runs so a long edge the rider fully
        # rode isn't split below the endpoint-touch threshold (see
        # _coalesce_timeline). Must match the builder's view of the timeline.
        timeline = _coalesce_timeline(timeline)

        activity = activity_loader(filename)
        if not activity:
            continue
        points = activity.get("points") or []
        if not points:
            continue

        for first_entry, last_entry in _scan_one_ride(segs, timeline, points):
            try:
                t0 = datetime.fromisoformat(first_entry["start_time"])
                t1 = datetime.fromisoformat(last_entry["end_time"])
                dur_sec = int((t1 - t0).total_seconds())
            except (KeyError, ValueError):
                logger.debug("route %s: dropping attempt in %s — bad timestamps",
                              route.get("id"), filename)
                continue   # skip; don't pollute leaderboard with 0-dur phantoms
            attempts.append({
                "filename":     filename,
                "title":        activity_meta.get(filename, {}).get("title") or filename,
                "start_time":   first_entry.get("start_time", ""),
                "end_time":     last_entry.get("end_time", ""),
                "duration_sec": dur_sec,
                "date":         (first_entry.get("start_time") or "")[:10],
                "first_idx":    int(first_entry.get("start_idx") or 0),
                "last_idx":     int(last_entry.get("end_idx") or 0),
            })

    attempts.sort(key=lambda a: (a["duration_sec"], a["filename"]))
    return attempts


def _scan_one_ride(segs: list[tuple[str, str, str, tuple, tuple]],
                    timeline: list[dict],
                    points: list[dict]) -> list[tuple[dict, dict]]:
    """Greedy left-to-right scan of `timeline` looking for strict
    no-detour matches of `segs`. Returns one `(first_entry, last_entry)`
    pair per attempt found — a single ride that does the route twice
    gets two pairs.

    Reset logic: an entry that doesn't match the next expected segment
    while we're mid-attempt collapses the in-flight match (i=0) without
    advancing j — so the bad entry is re-evaluated as a potential
    fresh attempt start. This catches the case "I tried the route, took
    a wrong turn at segment 3, then started over from segment 1."

    After a successful match, the scanner advances past the matched
    span and resumes scanning from the next entry, allowing back-to-back
    laps of a short loop route to register as separate attempts.
    """
    attempts: list[tuple[dict, dict]] = []
    i = 0
    j = 0
    first_match_entry: dict | None = None
    last_match_entry:  dict | None = None
    while j < len(timeline):
        if i >= len(segs):
            # Just closed an attempt; carry on scanning from j.
            attempts.append((first_match_entry, last_match_entry))   # type: ignore[arg-type]
            i = 0
            first_match_entry = None
            last_match_entry  = None
            continue
        entry = timeline[j]
        name, kind, direction, term_a, term_b, polyline = segs[i]
        # Try to satisfy segment i by accumulating consecutive same-trail
        # fragments from j (endpoint-touch or coverage over the combined span).
        consumed = _try_match_segment(points, timeline, j, name, kind,
                                      term_a, term_b, polyline)
        if consumed is not None:
            if first_match_entry is None:
                first_match_entry = timeline[j]
            last_match_entry = timeline[consumed - 1]
            i += 1
            j = consumed
        elif first_match_entry is None:
            j += 1
        elif (_is_noise_entry(entry)
                or (last_match_entry is not None
                    and _same_trail_as_adjacent(entry, segs, i)
                    and _fragment_gap_ok(last_match_entry, entry))):
            # Not a detour: either trivial GPS noise (a momentary mis-snap), or
            # a same-trail wiggle / leftover fragment sitting right against the
            # last match. Skip and stay in the attempt. The gap bound on the
            # same-trail case is essential — without it a same-named trail
            # touched again HOURS later would be skipped too, forming a phantom
            # whole-ride attempt; anchoring to `last_match_entry` keeps the
            # window from accumulating across a chain of skips. Noise entries
            # are too small to be either a real traversal or a real detour, so
            # they skip unconditionally (this is what lets a summit wander not
            # break the sequence even after a long rest).
            j += 1
        else:
            # Genuine detour (a substantial different trail mid-attempt) —
            # reset and re-evaluate this entry as a possible segs[0] match.
            i = 0
            first_match_entry = None
            last_match_entry  = None
    # Catch the case where the timeline ended exactly on a successful match.
    if i == len(segs) and first_match_entry is not None and last_match_entry is not None:
        attempts.append((first_match_entry, last_match_entry))
    return attempts


# ─── Leaderboard wrapper ────────────────────────────────────────────────────

def build_route_leaderboard(
    route: dict,
    region_artifact: dict,
    trail_match_cache_dir: Path,
    activity_loader: Callable[[str], dict | None],
    activity_meta: dict[str, dict] | None = None,
) -> dict:
    """Compute the per-route leaderboard payload — attempts list plus
    quick-access best stats. Stored as-is by the caching layer."""
    attempts = detect_attempts_for_route(
        route, region_artifact, trail_match_cache_dir,
        activity_loader, activity_meta,
    )
    best_duration_sec: int | None = None
    best_filename: str | None = None
    best_date: str | None = None
    if attempts:
        b = attempts[0]   # already sorted fastest first
        best_duration_sec = b["duration_sec"]
        best_filename     = b["filename"]
        best_date         = b["date"]
    return {
        "version":           ROUTE_ATTEMPTS_VERSION,
        "attempts":          attempts,
        "attempt_count":     len(attempts),
        "best_duration_sec": best_duration_sec,
        "best_filename":     best_filename,
        "best_date":         best_date,
    }
