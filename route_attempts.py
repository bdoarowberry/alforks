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
ROUTE_ATTEMPTS_VERSION = 1

# Re-export from trail_match so callers don't need to dig into private
# names — the endpoint-touch radius is the central tuning knob here.
ENDPOINT_TOUCH_RADIUS_M = trail_match.ENDPOINT_TOUCH_RADIUS_M

logger = logging.getLogger(__name__)


# ─── Endpoint lookup ────────────────────────────────────────────────────────

def _segment_endpoints(route: dict, region_artifact: dict
                        ) -> list[tuple[str, str, str, tuple, tuple]]:
    """For each segment in `route`, find its trail_name, kind, direction
    and the (lat, lon) of its two edge endpoints (start_node, end_node).

    Returns a list of `(trail_name, kind, direction, term_a, term_b)`. A
    segment whose edge_id is unknown (stale artifact rebuild) is dropped
    silently and logged at DEBUG — callers can detect "no segments left"
    by checking the return length against `len(route["segments"])`.
    """
    # Build a node_id -> (lat, lon) index across junctions + per-entry
    # endpoints (component pseudo-nodes). A single lookup table covers
    # both trail and road edges.
    node_xy: dict[str, tuple[float, float]] = {}
    for j in region_artifact.get("junctions") or []:
        node_xy[j["id"]] = (j["lat"], j["lon"])
    for entry in (region_artifact.get("trails") or []) + (region_artifact.get("roads") or []):
        for ep in entry.get("endpoints") or []:
            node_xy[ep["id"]] = (ep["lat"], ep["lon"])

    # Build edge_id -> (trail_name, kind) so we can resolve a segment's
    # canonical name/kind from its edge_id alone (segment also carries
    # them, but the artifact is the source of truth).
    edge_owner: dict[str, tuple[str, str]] = {}
    for entry in region_artifact.get("trails") or []:
        for e in entry.get("edges") or []:
            edge_owner[e["id"]] = (entry["name"], "trail")
    for entry in region_artifact.get("roads") or []:
        for e in entry.get("edges") or []:
            edge_owner[e["id"]] = (entry["name"], "road")

    # First pass: resolve each route segment to (name, kind, direction,
    # start_xy, end_xy). Drop unresolvable segments silently.
    resolved: list[tuple[str, str, str, tuple, tuple]] = []
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
        resolved.append((trail_name, kind, direction, a, b))

    # Second pass: fold consecutive segments on the same (trail_name, kind,
    # direction) into one macro-segment. trail_match collapses a continuous
    # ride along a trail into ONE timeline entry; a route built from
    # several consecutive edges on the same trail must match that single
    # entry, not look for N separate entries. Span endpoints become the
    # first segment's start and the last segment's end.
    folded: list[tuple[str, str, str, tuple, tuple]] = []
    for trail_name, kind, direction, a, b in resolved:
        if folded and folded[-1][0] == trail_name \
                  and folded[-1][1] == kind \
                  and folded[-1][2] == direction:
            ptn, pkd, pdr, pa, _pb = folded[-1]
            folded[-1] = (ptn, pkd, pdr, pa, b)
        else:
            folded.append((trail_name, kind, direction, a, b))
    return folded


# ─── Direction compatibility ────────────────────────────────────────────────

def _direction_compatible(timeline_direction: str | None,
                          requested_direction: str) -> bool:
    """v1 policy: accept any direction.

    Concrete reason: the builder writes every segment as `forward` (it
    has no trail-topology data to distinguish `up`/`down`). A real ride
    that does the route's loop will produce a mix of `up`, `down`,
    `forward`, `reverse` in trail_match's timeline. A polarity-bucket
    rule was rejecting genuine attempts like Braggin' Wizard, whose
    return leg shows `down` while the route stores `forward`.

    Strict macro-ordering + endpoint-touch already gives the meaningful
    correctness guarantee here — the rider's GPS span must include the
    edge group's start and end coordinates, AND segments must appear in
    the correct timeline order. Direction is recorded but not enforced
    in v1; tighten when the builder learns each trail's topology.
    """
    return True


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
    first_name, first_kind, _fd, _fa, _fb = segs[0]

    attempts: list[dict] = []
    for filename, _mtime, result in trail_match.scan_cached_results(trail_match_cache_dir):
        timeline = (result or {}).get("timeline") or []
        if not timeline:
            continue
        if not any(e.get("name") == first_name and e.get("kind") == first_kind
                    for e in timeline):
            continue

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
        name, kind, direction, term_a, term_b = segs[i]
        same = (entry.get("name") == name
                and entry.get("kind") == kind
                and _direction_compatible(entry.get("direction"), direction))
        if same and trail_match._run_touches_both(
                points, int(entry.get("start_idx") or 0),
                int(entry.get("end_idx") or 0) + 1,
                term_a, term_b, ENDPOINT_TOUCH_RADIUS_M):
            if first_match_entry is None:
                first_match_entry = entry
            last_match_entry = entry
            i += 1
            j += 1
        elif first_match_entry is None:
            j += 1
        else:
            # Mid-attempt detour — reset, re-evaluate this entry as a
            # possible segs[0] match next iteration.
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
