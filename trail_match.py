"""OSM trail-matching for MTB activities.

Mirrors the lifts pipeline in app.py (`_fetch_osm_lifts`, `cache/lifts/`):
fetches named highway=path|track|footway|cycleway|bridleway ways from
Overpass for an activity's bbox, snaps each (non-assisted) GPS point to
the nearest way within SNAP_THRESHOLD_M, groups consecutive matches by
trail name, and reports a chronological timeline plus a per-trail
coverage summary.

Validated end-to-end against the May 10 2025 Moose Mountain ride in
scripts/trail_match_probe.py. This module is the productionised version
of that probe.

Coverage > 100% is possible and expected when the rider rides a trail in
both directions (e.g. climbs Cutoff then comes back down it) or when
OSM's mapped trail length undershoots the true on-the-ground length.

Why MTB + Moose Mountain only (for now): ski runs have no defined OSM
boundaries (tree-skiing), and the OSM bbox fetch is expensive. Gating to
one region keeps Overpass traffic + per-activity processing bounded.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False

# Bump to invalidate per-file trail_match cache entries (e.g. when the
# snap threshold or run-collapsing logic changes). The /api/activity
# response version in app.py should bump alongside this so clients
# pick up the new shape.
TRAIL_MATCH_VERSION = 9

SNAP_THRESHOLD_M = 8.0   # tightened from 12 m: roads parallel to mapped
                         # trails (e.g. Moose Mountain Road alongside
                         # Pneuma) were keeping rider points snapped to
                         # the trail after the actual route diverged.
                         # 8 m is still comfortably above typical GPS
                         # noise (~3-5 m) but below the trail-vs-road
                         # spacing in the validated cases.
MIN_RUN_POINTS = 4          # drop GPS-jitter blips at junctions
VISIT_GAP_SEC = 600         # merge same-name runs separated by < this gap
# Coalesce parameters — see _coalesce_runs. The grouping splits runs at
# every name change, including the 1-3-point misfires that happen at
# every junction; the coalesce reunites them. Two distinct rules:
#
#   (1) Adjacent same-name [A, A]: merge when the gap is < max_split_sec.
#       A genuinely separate attempt of the same OSM way requires
#       leaving that way (usually onto a road), which is excluded from
#       the OSM query — so a real "next lap" leaves a minutes-long gap,
#       not a 1-2 s seam.
#
#   (2) [A, B, A] crossings: merge when B is a different name AND its
#       duration is < max_split_sec. Drops B because crossing a trail
#       briefly isn't an attempt of it. The summary's by-name aggregation
#       independently filters tiny crossings, so the dropped B's
#       contribution doesn't leak into ridden_km anyway.
COALESCE_MAX_SPLIT_SEC = 60
BBOX_PAD_DEG = 0.0002       # cheap point-in-bbox prune slop
OSM_FETCH_PAD_DEG = 0.002   # pad bbox before Overpass query
OSM_CACHE_TTL_SEC = 90 * 24 * 3600   # 90 days — trails change slowly
OVERPASS_TIMEOUT_SEC = 60

# Coverage filter for the summary table. Below this, matches are almost
# always GPS jitter brushing a parallel trail rather than a real attempt
# — validated against the probe data.
SUMMARY_MIN_COVERAGE_PCT = 5.0

# Treat a trail as "fully ridden" at this coverage. The buffer below 100
# absorbs the usual reasons real-world coverage falls short of OSM length
# even on a full traversal: GPS undersample, OSM gaps near termini, snap
# threshold dropouts at the end of the trail. Tuned against the May 10
# Moose Mountain ride — all full descents/climbs landed >= 90%.
COMPLETE_COVERAGE_PCT = 85.0

# Endpoint-touch override. If the rider's GPS path comes within this many
# metres of BOTH of the trail's overall termini during a run, the run is
# marked completed even if the snap coverage is below COMPLETE_COVERAGE_PCT.
# Targets the case where the GPS wandered just over SNAP_THRESHOLD_M for
# part of an otherwise-full descent — e.g. Feb 16 2026 on 7-27, where the
# rider rode the whole trail but the snap only credited 73 % of it.
ENDPOINT_TOUCH_RADIUS_M = 30.0

# Floor on snap coverage for the endpoint-touch override to fire. Without
# this, a rider who briefly brushed both ends of a trail at separate
# points in a ride (e.g. via parallel trails at the junctions) gets
# falsely promoted to "completed" with a tiny ridden_km — wrecking the
# leaderboard with a 6-minute 49 %-coverage entry ranked above genuine
# full descents. 60 % is well below typical full-descent snap (90 %+)
# while comfortably above the false-positive zone (40-50 %) we saw on
# the 7-27 leaderboard.
ENDPOINT_MIN_COVERAGE_PCT = 60.0

# Timeline filters — only show entries (a) whose trail name passes the
# summary coverage gate (so we don't show attempts of "trails" we don't
# trust we visited) and (b) with at least this much ridden distance
# (drops mid-ride junction brushes where the snap caught 4-8 points on a
# crossing trail for a couple of seconds). Both are tuned against the
# Moose Mountain probe — entries below 50 m were universally noise.
TIMELINE_MIN_DISTANCE_KM = 0.05

# Bump TRAIL_MATCH_VERSION above when changing any of these thresholds —
# it invalidates the per-file cache so reloads pick up the new shape.

_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_HIGHWAY_TYPES = "path|track|footway|cycleway|bridleway"

logger = logging.getLogger(__name__)


# ─── OSM fetch + cache (mirrors _fetch_osm_lifts) ────────────────────────────

_osm_locks: dict[str, threading.Lock] = {}
_osm_locks_mu = threading.Lock()

# In-memory mirror of disk cache. Key: cache file stem (md5 of bbox).
_OSM_TRAIL_MEM_CACHE: dict[str, list[dict]] = {}


def _osm_lock_for(cp: Path) -> threading.Lock:
    key = cp.name
    with _osm_locks_mu:
        if key not in _osm_locks:
            _osm_locks[key] = threading.Lock()
        return _osm_locks[key]


def _osm_cache_path(cache_dir: Path, bbox) -> Path:
    # Round to 2 dp — same as lifts. Matches "trips through the same
    # mountain" to the same cache file.
    s, w, n, e = bbox
    key = f"{round(s,2)},{round(w,2)},{round(n,2)},{round(e,2)}"
    h = hashlib.md5(key.encode()).hexdigest()[:12]
    return cache_dir / f"{h}.json"


def _try_read_osm_cache(cp: Path) -> list[dict] | None:
    mem = _OSM_TRAIL_MEM_CACHE.get(cp.stem)
    if mem is not None:
        return mem
    if not cp.exists():
        return None
    try:
        entry = json.loads(cp.read_text(encoding="utf-8"))
        if time.time() - entry.get("fetched", 0) < OSM_CACHE_TTL_SEC:
            ways = entry["ways"]
            _OSM_TRAIL_MEM_CACHE[cp.stem] = ways
            return ways
    except Exception:
        pass
    return None


def fetch_osm_trails(bbox, cache_dir: Path) -> list[dict]:
    """Return named OSM trail ways for `bbox`, cached for OSM_CACHE_TTL_SEC.

    Each way: {id, name, highway, mtb_scale, coords: [(lat,lon), ...]}.
    Returns [] on Overpass error so the route never 500s on network blip.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cp = _osm_cache_path(cache_dir, bbox)

    cached = _try_read_osm_cache(cp)
    if cached is not None:
        return cached

    with _osm_lock_for(cp):
        cached = _try_read_osm_cache(cp)
        if cached is not None:
            return cached

        s, w, n, e = bbox
        pad = OSM_FETCH_PAD_DEG
        query = (
            f"[out:json][timeout:{OVERPASS_TIMEOUT_SEC}];"
            f'(way["highway"~"^({_HIGHWAY_TYPES})$"]["name"]'
            f"({s-pad},{w-pad},{n+pad},{e+pad}););"
            f"(._;>;);out body;"
        )
        try:
            data = urllib.parse.urlencode({"data": query}).encode()
            req = urllib.request.Request(
                _OVERPASS_URL, data=data,
                headers={"User-Agent": "AlanForks-GPX-Viewer/1.0"},
            )
            with urllib.request.urlopen(req, timeout=OVERPASS_TIMEOUT_SEC + 20) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.warning("Overpass fetch failed for trail bbox %s: %s", bbox, exc)
            return []

        ways = _parse_overpass_ways(result)
        try:
            tmp = cp.with_suffix(cp.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"fetched": time.time(), "ways": ways}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(cp)
        except OSError as exc:
            logger.warning("Failed to persist trail cache %s: %s", cp, exc)
        _OSM_TRAIL_MEM_CACHE[cp.stem] = ways
        return ways


def _parse_overpass_ways(osm_json) -> list[dict]:
    nodes = {
        e["id"]: (e["lat"], e["lon"])
        for e in osm_json.get("elements", []) if e.get("type") == "node"
    }
    ways: list[dict] = []
    for e in osm_json.get("elements", []):
        if e.get("type") != "way":
            continue
        tags = e.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        coords = [nodes[nid] for nid in e.get("nodes", []) if nid in nodes]
        if len(coords) < 2:
            continue
        ways.append({
            "id": e["id"],
            "name": name,
            "highway": tags.get("highway"),
            "mtb_scale": tags.get("mtb:scale"),
            "coords": coords,
        })
    return ways


# ─── Geometry helpers ────────────────────────────────────────────────────────

def _haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _to_local(lat, lon, lat0, lon0):
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * 6371000.0
    y = math.radians(lat - lat0) * 6371000.0
    return x, y


def _point_segment_dist(px, py, ax, ay, bx, by):
    dx = bx - ax
    dy = by - ay
    seg_sq = dx * dx + dy * dy
    if seg_sq == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_sq))
    cx = ax + t * dx
    cy = ay + t * dy
    return math.hypot(px - cx, py - cy)


def _way_length_km(coords):
    total = 0.0
    for i in range(1, len(coords)):
        total += _haversine_m(coords[i - 1][0], coords[i - 1][1],
                              coords[i][0], coords[i][1])
    return total / 1000.0


# ─── Matching ────────────────────────────────────────────────────────────────

def _trail_termini(ways: list[dict], name: str) -> tuple[tuple, tuple] | None:
    """Return the two coordinates farthest apart across all OSM ways with
    the given name, treated as the trail's overall start and end.

    For a single-fragment trail (most descents) this is just that way's
    first and last point. For a multi-fragment trail (longer ways split
    at intersections) the farthest-apart pair across all fragments'
    endpoints picks the true outer termini.

    O(n^2) over endpoint count, but n is tiny (typically 2-8 per trail).
    Returns None if the trail has fewer than two endpoint candidates —
    a "completion" check can't run without two termini to touch.
    """
    pts: list[tuple[float, float]] = []
    for w in ways:
        if w["name"] != name:
            continue
        coords = w["coords"]
        if not coords:
            continue
        pts.append(coords[0])
        pts.append(coords[-1])
    if len(pts) < 2:
        return None
    best: tuple[tuple, tuple] | None = None
    best_d = -1.0
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = _haversine_m(pts[i][0], pts[i][1], pts[j][0], pts[j][1])
            if d > best_d:
                best_d = d
                best = (pts[i], pts[j])
    return best


def _run_touches_both(points, s: int, e: int,
                      term_a: tuple, term_b: tuple,
                      radius_m: float = ENDPOINT_TOUCH_RADIUS_M) -> bool:
    """True if some point in points[s:e] comes within `radius_m` of term_a
    AND some (possibly different) point comes within `radius_m` of term_b.

    Doesn't require ordering — a rider may enter from either end. Stops
    early once both are hit, so cheap for typical runs.
    """
    touched_a = False
    touched_b = False
    a_lat, a_lon = term_a
    b_lat, b_lon = term_b
    for i in range(s, e):
        try:
            plat = points[i]["lat"]
            plon = points[i]["lon"]
        except (KeyError, IndexError):
            continue
        if not touched_a and _haversine_m(plat, plon, a_lat, a_lon) <= radius_m:
            touched_a = True
        if not touched_b and _haversine_m(plat, plon, b_lat, b_lon) <= radius_m:
            touched_b = True
        if touched_a and touched_b:
            return True
    return False


def _project_ways(ways: list[dict], lat0: float, lon0: float) -> list[dict]:
    out = []
    for w in ways:
        local = [_to_local(c[0], c[1], lat0, lon0) for c in w["coords"]]
        lats = [c[0] for c in w["coords"]]
        lons = [c[1] for c in w["coords"]]
        entry: dict = {
            "id": w["id"], "name": w["name"], "highway": w.get("highway"),
            "mtb_scale": w.get("mtb_scale"),
            "local": local,
            "bb": (min(lats), min(lons), max(lats), max(lons)),
            "length_km": _way_length_km(w["coords"]),
        }
        # Pre-build numpy segment arrays for vectorised snap. Each array holds
        # one element per segment (len(local)-1 segments). Computed once here
        # so _snap_points doesn't repeat the work per GPS point.
        if _NUMPY_AVAILABLE and len(local) >= 2:
            ax = np.array([local[j - 1][0] for j in range(1, len(local))], dtype=np.float64)
            ay = np.array([local[j - 1][1] for j in range(1, len(local))], dtype=np.float64)
            bx = np.array([local[j][0]     for j in range(1, len(local))], dtype=np.float64)
            by = np.array([local[j][1]     for j in range(1, len(local))], dtype=np.float64)
            dx = bx - ax
            dy = by - ay
            seg_sq = dx * dx + dy * dy
            entry["np_segs"] = (ax, ay, dx, dy, seg_sq)
        out.append(entry)
    return out


def _snap_points(points, ways_proj, lat0, lon0):
    """Return (best_name, best_dist) per point. Assisted points get (None, None)
    so they break visit runs naturally.

    When numpy is available (nearly always), each way's segments are evaluated
    as a vectorised array broadcast — one numpy call replaces the inner Python
    for-loop over segments. The bbox prune per way is unchanged. The scalar
    fallback path is preserved for environments without numpy.
    """
    use_numpy = _NUMPY_AVAILABLE
    matches = []
    for p in points:
        if p.get("assisted"):
            matches.append((None, None))
            continue
        plat, plon = p["lat"], p["lon"]
        px, py = _to_local(plat, plon, lat0, lon0)
        best_name, best_dist = None, SNAP_THRESHOLD_M
        for w in ways_proj:
            wb = w["bb"]
            if (plat < wb[0] - BBOX_PAD_DEG or plat > wb[2] + BBOX_PAD_DEG
                    or plon < wb[1] - BBOX_PAD_DEG or plon > wb[3] + BBOX_PAD_DEG):
                continue
            if use_numpy and "np_segs" in w:
                ax, ay, dx, dy, seg_sq = w["np_segs"]
                # t = clamp(dot(p-a, seg) / |seg|^2, 0, 1); degenerate (zero-
                # length) segments collapse to distance from point a (t=0).
                t = np.where(
                    seg_sq == 0,
                    0.0,
                    np.clip(((px - ax) * dx + (py - ay) * dy) / seg_sq, 0.0, 1.0),
                )
                d = float(np.hypot(px - (ax + t * dx), py - (ay + t * dy)).min())
            else:
                local = w["local"]
                d = SNAP_THRESHOLD_M + 1.0  # ensure we enter the inner loop
                for j in range(1, len(local)):
                    ax_s, ay_s = local[j - 1]
                    bx_s, by_s = local[j]
                    d = min(d, _point_segment_dist(px, py, ax_s, ay_s, bx_s, by_s))
            if d < best_dist:
                best_dist = d
                best_name = w["name"]
        matches.append((best_name, best_dist if best_name else None))
    return matches


# Runs are tuples (name, span_start, span_end, ridden_km). `span_*` are
# point indices into `data["points"]` and define the visible span of the
# run (used for map highlight and time bounds). `ridden_km` is the sum
# of dist_km contributions from the *original* sub-runs that compose
# this run — when Pattern 2 merges A-B-A into one A, B's distance is
# NOT included even though [span_start, span_end] covers B's points.

def _runs_by_name(matches, points):
    """Collapse consecutive same-name matches into runs of >= MIN_RUN_POINTS.

    Grouped by NAME (not way id) so OSM's splitting of trails at every
    intersection doesn't shred a single ride into a dozen mini-runs.
    """
    if not matches:
        return []

    def _dist_km(s: int, e: int) -> float:
        try:
            return (points[e - 1].get("dist_km") or 0.0) - (points[s].get("dist_km") or 0.0)
        except IndexError:
            return 0.0

    runs = []
    cur_name = matches[0][0]
    cur_start = 0
    for i in range(1, len(matches)):
        name = matches[i][0]
        if name != cur_name:
            if cur_name is not None and (i - cur_start) >= MIN_RUN_POINTS:
                runs.append((cur_name, cur_start, i, _dist_km(cur_start, i)))
            cur_name, cur_start = name, i
    if cur_name is not None and (len(matches) - cur_start) >= MIN_RUN_POINTS:
        runs.append((cur_name, cur_start, len(matches), _dist_km(cur_start, len(matches))))
    return runs


def _coalesce_runs(runs, points,
                   max_split_sec: float = COALESCE_MAX_SPLIT_SEC):
    """Merge same-name runs that the grouping incorrectly split.

    See COALESCE_* docs above for rule rationale.

      Pattern 1 — adjacent [A, A] with gap <= max_split_sec. Merge keeps
                  span [A1.start, A2.end] and ridden_km = A1.ridden +
                  A2.ridden (the gap between is the tiny seam from the
                  filtered B, contributing negligible distance).
      Pattern 2 — [A, B, A] with B different-name and dur <= max_split_sec.
                  Drop B. Merge keeps span [A1.start, A2.end] but
                  ridden_km = A1.ridden + A2.ridden — explicitly does NOT
                  include B's contribution, even though B's points fall
                  inside the merged span. Without this, riding 10 m onto
                  a crossing trail and back would inflate A's coverage
                  by 10 m.

    Iterates greedily: each merged run is rechecked against the next
    candidate so a chain collapses fully.
    """
    if len(runs) < 2:
        return list(runs)

    def _t(i):
        return _parse_iso(points[i]["time"])

    def _gap_sec(end_run, start_run) -> float:
        try:
            return (_t(start_run[1]) - _t(end_run[2] - 1)).total_seconds()
        except (KeyError, ValueError):
            return float("inf")

    def _dur_sec(r) -> float:
        try:
            return (_t(r[2] - 1) - _t(r[1])).total_seconds()
        except (KeyError, ValueError):
            return 0.0

    out = [runs[0]]
    i = 1
    while i < len(runs):
        nxt = runs[i]
        # Pattern 1: adjacent same-name with short gap. Sum ridden_km
        # rather than recomputing across the span — the seam between
        # them holds the dropped tiny B, which we don't want to credit.
        if (nxt[0] == out[-1][0]
                and _gap_sec(out[-1], nxt) <= max_split_sec):
            out[-1] = (out[-1][0], out[-1][1], nxt[2], out[-1][3] + nxt[3])
            i += 1
            continue
        # Pattern 2: A, B, A where B is a different name AND short. Drop
        # B and merge the A's. Distance is A1 + A2 (NOT span) — see the
        # docstring above for why this matters.
        if (i + 1 < len(runs)
                and runs[i + 1][0] == out[-1][0]
                and nxt[0] != out[-1][0]
                and _dur_sec(nxt) <= max_split_sec):
            a2 = runs[i + 1]
            out[-1] = (out[-1][0], out[-1][1], a2[2], out[-1][3] + a2[3])
            i += 2
            continue
        # No merge candidate — start a new anchor.
        out.append(nxt)
        i += 1
    return out


def _parse_iso(ts: str):
    # Both ends of any gap come from the same activity, so identical tz
    # offset — datetime.fromisoformat handles the +HH:MM suffix natively.
    from datetime import datetime
    return datetime.fromisoformat(ts)


def match_trails(data: dict, cache_dir: Path) -> dict:
    """Match a parsed GPX activity to named OSM trails.

    `data` must look like the dict returned by `get_activity`: has `points`
    (list of {lat, lon, time, dist_km, assisted, ...}) and `bbox`
    ([south, west, north, east]).

    `cache_dir`: directory for the bbox -> OSM ways disk cache
    (`cache/osm_paths/` in production).

    Returns:
        {
          "timeline": [
            {name, start_idx, end_idx, start_time, end_time,
             duration_sec, distance_km, points, avg_dist_m},
            ...
          ],
          "summary": [
            {name, visits, ridden_km, osm_length_km, coverage_pct,
             first_time, highway, mtb_scale},
            ...   # filtered to coverage_pct >= SUMMARY_MIN_COVERAGE_PCT
          ],
          "stats": {match_rate_pct, named_ways_in_bbox, snap_threshold_m},
        }
    """
    points = data.get("points") or []
    bbox = data.get("bbox")
    if not points or not bbox or len(bbox) != 4:
        return _empty_result()

    ways = fetch_osm_trails(bbox, cache_dir)
    if not ways:
        return _empty_result(named_ways_in_bbox=0)

    lat0 = (bbox[0] + bbox[2]) / 2
    lon0 = (bbox[1] + bbox[3]) / 2
    ways_proj = _project_ways(ways, lat0, lon0)

    t0 = time.time()
    matches = _snap_points(points, ways_proj, lat0, lon0)
    elapsed = time.time() - t0
    logger.debug("trail_match: snapped %d points in %.1fs", len(points), elapsed)

    runs = _runs_by_name(matches, points)
    runs = _coalesce_runs(runs, points)

    # Sum OSM length per trail NAME (collapse fragmented ways).
    name_length_km: dict[str, float] = {}
    for w in ways_proj:
        name_length_km[w["name"]] = name_length_km.get(w["name"], 0.0) + w["length_km"]
    # Prefer the longest fragment's tags (best chance of being "the" trail).
    # Done as a SECOND pass — the original single-pass version compared each
    # fragment to the running total, which made the FIRST fragment win
    # `setdefault` regardless of length. Now we scan all fragments and pick
    # the one with the largest length per name.
    longest_frag: dict[str, dict] = {}
    for w in ways_proj:
        cur = longest_frag.get(w["name"])
        if cur is None or w["length_km"] > cur["length_km"]:
            longest_frag[w["name"]] = w
    name_highway: dict[str, str] = {}
    name_mtb_scale: dict[str, str] = {}
    for nm, w in longest_frag.items():
        if w.get("highway"):
            name_highway[nm] = w["highway"]
        if w.get("mtb_scale"):
            name_mtb_scale[nm] = w["mtb_scale"]

    # Compute outer termini for each trail name from the RAW (unprojected)
    # ways. Used by the endpoint-touch completion override below — a run
    # whose snap coverage falls short of COMPLETE_COVERAGE_PCT can still
    # be marked completed if the rider's path visits within
    # ENDPOINT_TOUCH_RADIUS_M of both termini during that run.
    # Computed once per name and reused across runs / summary aggregation.
    termini_by_name: dict[str, tuple[tuple, tuple] | None] = {}
    for nm in name_length_km:
        termini_by_name[nm] = _trail_termini(ways, nm)

    timeline_raw: list = []
    by_name: dict[str, dict] = {}
    for name, s, e, d_km in runs:
        s_pt = points[s]
        e_pt = points[e - 1]
        try:
            duration_sec = (_parse_iso(e_pt["time"]) - _parse_iso(s_pt["time"])).total_seconds()
        except (KeyError, ValueError):
            duration_sec = 0.0
        # Average snap distance — useful diagnostic for "how confident is
        # this match" but not surfaced in the UI yet.
        dists = [m[1] for m in matches[s:e] if m[1] is not None]
        avg_dist_m = sum(dists) / len(dists) if dists else None

        entry = {
            "name": name,
            "start_idx": s, "end_idx": e - 1,
            "start_time": s_pt.get("time", ""),
            "end_time": e_pt.get("time", ""),
            "duration_sec": round(duration_sec),
            "distance_km": round(d_km, 3),
            "points": e - s,
            "avg_dist_m": round(avg_dist_m, 1) if avg_dist_m is not None else None,
        }
        timeline_raw.append(entry)

        # Don't credit a name with a crossing — a sub-50 m run is below
        # the floor for "I rode this trail". The user-visible summary AND
        # the timeline filter both depend on `qualifying` (= summary
        # names), so excluding the crossing here propagates everywhere.
        if d_km < TIMELINE_MIN_DISTANCE_KM:
            continue
        rec = by_name.setdefault(name, {"ridden_km": 0.0, "runs": [], "first_time": s_pt.get("time", "")})
        rec["ridden_km"] += max(d_km, 0.0)
        rec["runs"].append((s, e))

    # Merge runs of the same name into "visits" when they're close in time
    # (mostly fixes OSM coverage gaps that split a single descent into two).
    summary = []
    for name, rec in by_name.items():
        rec["runs"].sort()
        visits = 1
        for i in range(1, len(rec["runs"])):
            prev_end_idx = rec["runs"][i - 1][1] - 1
            cur_start_idx = rec["runs"][i][0]
            try:
                gap = (_parse_iso(points[cur_start_idx]["time"])
                       - _parse_iso(points[prev_end_idx]["time"])).total_seconds()
            except (KeyError, ValueError):
                gap = 0.0
            if gap > VISIT_GAP_SEC:
                visits += 1
        osm_len = name_length_km.get(name, 0.0)
        raw_cov = 100.0 * rec["ridden_km"] / osm_len if osm_len > 0 else 0.0
        # Filter on the raw figure so a slightly-noisy 6 % run still
        # makes it through; cap the displayed value at 100 because a
        # rider can't ride more than "the whole trail" in a meaningful
        # sense, and the >100 % readings are GPS-path-length artefacts.
        if raw_cov < SUMMARY_MIN_COVERAGE_PCT:
            continue
        cov_pct = min(raw_cov, 100.0)
        completed = raw_cov >= COMPLETE_COVERAGE_PCT
        endpoint_completed = False
        # If snap coverage didn't reach the threshold but cleared the
        # ENDPOINT_MIN_COVERAGE_PCT floor, check whether any of this
        # trail's runs touched both termini — the user might have ridden
        # the whole thing but the GPS wandered off the OSM way. The
        # coverage floor blocks false positives where a rider brushed
        # both ends without actually riding the trail.
        if not completed and raw_cov >= ENDPOINT_MIN_COVERAGE_PCT:
            term = termini_by_name.get(name)
            if term is not None:
                a, b = term
                for s_idx, e_idx in rec["runs"]:
                    if _run_touches_both(points, s_idx, e_idx, a, b):
                        endpoint_completed = True
                        completed = True
                        break
        summary.append({
            "name": name,
            "visits": visits,
            "ridden_km": round(rec["ridden_km"], 3),
            "osm_length_km": round(osm_len, 3),
            "coverage_pct": round(cov_pct, 1),
            "completed": completed,
            "endpoint_completed": endpoint_completed,
            "first_time": rec["first_time"],
            "highway": name_highway.get(name),
            "mtb_scale": name_mtb_scale.get(name),
        })
    summary.sort(key=lambda r: -r["ridden_km"])

    # Filter the raw timeline to (a) trail names that made the summary cut
    # and (b) entries with at least TIMELINE_MIN_DISTANCE_KM of ride.
    # Decorate each survivor with its per-attempt coverage of the trail
    # (capped at 100 — same as the summary). The timeline is the primary
    # surface in the UI, so the coverage pill lives here per row.
    qualifying = {t["name"]: t["osm_length_km"] for t in summary}
    timeline = []
    for e in timeline_raw:
        if e["name"] not in qualifying or e["distance_km"] < TIMELINE_MIN_DISTANCE_KM:
            continue
        osm_len = qualifying[e["name"]]
        raw_cov = 100.0 * e["distance_km"] / osm_len if osm_len > 0 else 0.0
        e["coverage_pct"] = round(min(raw_cov, 100.0), 1)
        completed = raw_cov >= COMPLETE_COVERAGE_PCT
        endpoint_completed = False
        if not completed and raw_cov >= ENDPOINT_MIN_COVERAGE_PCT:
            term = termini_by_name.get(e["name"])
            if term is not None:
                a, b = term
                if _run_touches_both(points, e["start_idx"], e["end_idx"] + 1, a, b):
                    endpoint_completed = True
                    completed = True
        e["completed"]          = completed
        e["endpoint_completed"] = endpoint_completed
        timeline.append(e)

    matched_points = sum(1 for m in matches if m[0] is not None)
    return {
        "timeline": timeline,
        "summary": summary,
        "stats": {
            "match_rate_pct": round(100.0 * matched_points / len(points), 1) if points else 0.0,
            "named_ways_in_bbox": len(ways),
            "snap_threshold_m": SNAP_THRESHOLD_M,
            "snap_elapsed_sec": round(elapsed, 2),
        },
    }


def _empty_result(named_ways_in_bbox: int = 0) -> dict:
    return {
        "timeline": [],
        "summary": [],
        "stats": {
            "match_rate_pct": 0.0,
            "named_ways_in_bbox": named_ways_in_bbox,
            "snap_threshold_m": SNAP_THRESHOLD_M,
            "snap_elapsed_sec": 0.0,
        },
    }


# ─── Per-activity result cache ───────────────────────────────────────────────
# The OSM bbox fetch is cached; the per-point snap is not. Snapping is the
# expensive part (5-20s for a ~5k-point activity), so we persist the final
# match result per file. Key: (filename, GPX mtime, TRAIL_MATCH_VERSION).

_RESULT_MEM_CACHE: dict[tuple[str, float, int], dict] = {}
_RESULT_MEM_LOCK = threading.Lock()


def cached_match(filename: str, gpx_mtime: float, data: dict,
                 cache_dir_osm: Path, cache_dir_results: Path,
                 meta_fp: str = "") -> dict:
    """Memoised wrapper around `match_trails`. Disk + memory.

    Disk cache file: `{cache_dir_results}/{filename}.json`, payload
    `{mtime, version, meta_fp, result}`. Stale entries (mtime mismatch,
    version bump, or meta_fp change) are recomputed.

    `meta_fp` should encode any per-file metadata that changes `data`
    *without* changing the raw GPX file's mtime: trim, smoothing,
    time_shift, spike_repair. When these change, the snap result needs
    to recompute. Pass "" to skip meta-tracking (e.g. for tests).
    """
    key = (filename, gpx_mtime, TRAIL_MATCH_VERSION, meta_fp)
    with _RESULT_MEM_LOCK:
        hit = _RESULT_MEM_CACHE.get(key)
    if hit is not None:
        return hit

    cache_dir_results.mkdir(parents=True, exist_ok=True)
    disk = cache_dir_results / f"{filename}.json"
    if disk.exists():
        try:
            entry = json.loads(disk.read_text(encoding="utf-8"))
            # meta_fp absent in the on-disk entry is treated as "" — old
            # cache files written before this param was introduced will
            # match the default meta_fp="" and stay valid for the
            # untouched-metadata case (most rides).
            if (entry.get("mtime") == gpx_mtime
                    and entry.get("version") == TRAIL_MATCH_VERSION
                    and (entry.get("meta_fp") or "") == meta_fp):
                result = entry["result"]
                with _RESULT_MEM_LOCK:
                    _RESULT_MEM_CACHE[key] = result
                return result
        except Exception:
            pass

    result = match_trails(data, cache_dir_osm)
    try:
        tmp = disk.with_suffix(disk.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"mtime": gpx_mtime, "version": TRAIL_MATCH_VERSION,
                        "meta_fp": meta_fp, "result": result}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(disk)
    except OSError as exc:
        logger.warning("Failed to persist trail_match result %s: %s", disk, exc)
    with _RESULT_MEM_LOCK:
        _RESULT_MEM_CACHE[key] = result
    return result


# ─── Leaderboard aggregation ─────────────────────────────────────────────────
# Pure data layer over the per-file trail_match cache. Given the cache
# directory (which holds one JSON per qualifying activity), build a
# trail-name -> sorted-attempts dict where attempts are ordered fastest
# first. Used by the activity API to attach rank info and by the
# /api/trails/leaderboard endpoint to surface the popup view.
#
# Filtering rules:
#   - Only completed attempts count for the leaderboard (partials don't
#     have a meaningful "time on the trail").
#   - Sort key is duration_sec ascending (faster = earlier in list).
#   - Tie-break: filename ascending (stable, deterministic across runs).

def scan_cached_results(cache_dir_results: Path) -> list[tuple[str, float, dict]]:
    """Return `(filename, mtime, result_dict)` for every cached trail-match
    file in `cache_dir_results` that matches the current TRAIL_MATCH_VERSION.

    Files at older versions are silently skipped — they'll get rebuilt
    on next access. Corrupt JSON is also skipped (logged once at warning).
    """
    if not cache_dir_results.exists():
        return []
    out: list[tuple[str, float, dict]] = []
    for p in cache_dir_results.glob("*.json"):
        if p.name.endswith(".tmp"):
            continue
        try:
            entry = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Skipping unreadable trail_match cache %s: %s", p, exc)
            continue
        if entry.get("version") != TRAIL_MATCH_VERSION:
            continue
        result = entry.get("result") or {}
        filename = p.name[:-len(".json")]
        out.append((filename, p.stat().st_mtime, result))
    return out


def build_leaderboards(cache_dir_results: Path,
                       activity_meta: dict[str, dict] | None = None
                       ) -> dict[str, list[dict]]:
    """Aggregate cached results into per-trail-name leaderboards.

    Each leaderboard entry includes the originating filename, date,
    duration, distance, coverage, and span indices — enough for the UI
    popup to render rows and link back to the source activity.

    `activity_meta`: optional `{filename: {title, type, ...}}` from the
    main metadata.json. Used to enrich each row with a display title.
    Missing entries fall back to the filename.
    """
    cached = scan_cached_results(cache_dir_results)
    boards: dict[str, list[dict]] = {}
    for filename, _mtime, result in cached:
        timeline = (result or {}).get("timeline") or []
        for entry in timeline:
            if not entry.get("completed"):
                continue
            name = entry.get("name")
            if not name:
                continue
            row = {
                "filename": filename,
                "title":     (activity_meta or {}).get(filename, {}).get("title") or filename,
                "start_time": entry.get("start_time", ""),
                "date":       (entry.get("start_time") or "")[:10],
                "duration_sec": int(entry.get("duration_sec") or 0),
                "distance_km":  float(entry.get("distance_km") or 0.0),
                "coverage_pct": float(entry.get("coverage_pct") or 0.0),
                "start_idx":    int(entry.get("start_idx") or 0),
                "end_idx":      int(entry.get("end_idx")   or 0),
            }
            boards.setdefault(name, []).append(row)

    for name, rows in boards.items():
        rows.sort(key=lambda r: (r["duration_sec"], r["filename"]))
    return boards


def rank_for_attempt(boards: dict[str, list[dict]], name: str,
                     filename: str, start_idx: int) -> tuple[int, int] | None:
    """Find a specific attempt's 1-based rank within its trail's leaderboard.

    Identity is `(filename, start_idx)` — same activity can contribute
    multiple completed attempts of the same trail (multi-lap rides), and
    each gets its own rank.

    Returns `(rank, total)` or `None` if the attempt isn't on the board
    (most commonly because the activity's trail_match cache hasn't been
    rebuilt yet to mark the attempt completed, or the attempt is a partial).
    """
    rows = boards.get(name)
    if not rows:
        return None
    for i, row in enumerate(rows, start=1):
        if row["filename"] == filename and row["start_idx"] == start_idx:
            return (i, len(rows))
    return None
