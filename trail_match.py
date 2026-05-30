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

import osm_breaker

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False

# Bump to invalidate per-file trail_match cache entries (e.g. when the
# snap threshold or run-collapsing logic changes). The /api/activity
# response version in app.py should bump alongside this so clients
# pick up the new shape.
TRAIL_MATCH_VERSION = 13

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
# Two thresholds (used to be one, but they have different cost profiles):
#   - COALESCE_MAX_GAP_SEC: time gap between two adjacent same-name runs
#     where the rider was likely just briefly off the snap line (GPS
#     drift, OSM gaps, etc.). A real "re-lap" requires climbing back up
#     via a different route, which takes minutes, so we can afford a
#     generous 2-minute window without false-merging laps.
#   - COALESCE_MAX_CROSSING_SEC: duration of a *different-name* trail B
#     sandwiched between two A runs. A short B is a crossing and should
#     be dropped; a long B is a real attempt of B and should be preserved.
#     Kept tighter so genuine multi-trail rides don't lose B's.
COALESCE_MAX_GAP_SEC      = 120
COALESCE_MAX_CROSSING_SEC = 60
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

# Traversal-splitting + direction parameters.
#
# Topology drives the split rule:
#   linear/messy — split at progress reversals (start→end XOR end→start).
#                  Direction: 'up'/'down' from elevation delta, else
#                  'forward'/'reverse' from progress sign.
#   loop         — split ONLY on full 2π revolutions around the centroid.
#                  Internal climb/descent waves are intrinsic to the trail
#                  shape (Merlin View climbs then descends within a single
#                  loop) and don't constitute separate attempts.
#                  Direction: 'forward'/'reverse' from angular sign.
# 'mixed' is now a last-resort label for runs that can't be classified at
# all (no elevation signal, no clear progress) — rare with this scheme.
TRAVERSAL_MIN_PROGRESS_DELTA = 0.20   # linear: progress reversal must span
                                       # this much of the trail to flip direction
TRAVERSAL_MIN_ELE_DELTA_M    = 10.0   # below this, elevation isn't decisive
                                       # → fall through to progress-direction
TRAVERSAL_PROGRESS_SMOOTH_WINDOW = 9  # median-window points to filter GPS jitter
TRAVERSAL_MIN_POINTS         = 8     # below this, fold into adjacent traversal

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
# Roads are matched alongside trails as of v13 — a road climb (Moose
# Mountain Road) is a thing you rode and deserves the same leaderboard
# treatment. Naming kept as `trail_match` for legacy callers.
_ROAD_HIGHWAY_TYPES = "residential|service|unclassified|tertiary|secondary|primary"

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


def _read_trail_cache_stale(cp: Path) -> list[dict] | None:
    """Read the trail cache ignoring TTL. Trails change slowly; stale cache
    is better than empty when Overpass is unreachable. Returns None only
    when the file is missing or corrupt."""
    if not cp.exists():
        return None
    try:
        entry = json.loads(cp.read_text(encoding="utf-8"))
        return entry.get("ways")
    except Exception:
        return None


def fetch_osm_trails(bbox, cache_dir: Path) -> list[dict]:
    """Return named OSM trail ways for `bbox`, cached for OSM_CACHE_TTL_SEC.

    Each way: {id, name, highway, mtb_scale, coords: [(lat,lon), ...]}.
    Returns [] on Overpass error so the route never 500s on network blip.
    When Overpass is unreachable (timeout, breaker open, network error),
    falls back to the on-disk cache ignoring TTL before returning [].
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

        if osm_breaker.should_skip():
            stale = _read_trail_cache_stale(cp)
            return stale if stale is not None else []

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
            osm_breaker.record_failure()
            stale = _read_trail_cache_stale(cp)
            return stale if stale is not None else []

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
        osm_breaker.record_success()
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


# ─── Road OSM fetch ─────────────────────────────────────────────────────────
# Mirror of fetch_osm_trails, scoped to the road highway types. Lives here
# (not route_builder.py) so trail_match can match against roads without a
# circular import. Caches at cache/osm_roads/ separately from cache/osm_paths/
# — keeping the two queries split makes invalidation per-set straightforward.

_road_locks: dict[str, threading.Lock] = {}
_road_locks_mu = threading.Lock()
_OSM_ROAD_MEM_CACHE: dict[str, list[dict]] = {}


def _road_lock_for(cp: Path) -> threading.Lock:
    key = cp.name
    with _road_locks_mu:
        if key not in _road_locks:
            _road_locks[key] = threading.Lock()
        return _road_locks[key]


def _try_read_road_cache(cp: Path) -> list[dict] | None:
    mem = _OSM_ROAD_MEM_CACHE.get(cp.stem)
    if mem is not None:
        return mem
    if not cp.exists():
        return None
    try:
        entry = json.loads(cp.read_text(encoding="utf-8"))
        if time.time() - entry.get("fetched", 0) < OSM_CACHE_TTL_SEC:
            ways = entry["ways"]
            _OSM_ROAD_MEM_CACHE[cp.stem] = ways
            return ways
    except Exception:
        pass
    return None


def _read_road_cache_stale(cp: Path) -> list[dict] | None:
    """Read the road cache ignoring TTL. See _read_trail_cache_stale."""
    if not cp.exists():
        return None
    try:
        entry = json.loads(cp.read_text(encoding="utf-8"))
        return entry.get("ways")
    except Exception:
        return None


def fetch_osm_roads(bbox, cache_dir: Path) -> list[dict]:
    """Return named OSM road ways for `bbox`, cached for OSM_CACHE_TTL_SEC.

    Each way: {id, name, highway, oneway, coords: [(lat,lon), ...]}.
    Returns [] on Overpass error so the caller never 500s on a network blip.
    When Overpass is unreachable, falls back to the on-disk cache ignoring TTL.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cp = _osm_cache_path(cache_dir, bbox)   # same hash scheme as trails

    cached = _try_read_road_cache(cp)
    if cached is not None:
        return cached

    with _road_lock_for(cp):
        cached = _try_read_road_cache(cp)
        if cached is not None:
            return cached

        if osm_breaker.should_skip():
            stale = _read_road_cache_stale(cp)
            return stale if stale is not None else []

        s, w, n, e = bbox
        pad = OSM_FETCH_PAD_DEG
        query = (
            f"[out:json][timeout:{OVERPASS_TIMEOUT_SEC}];"
            f'(way["highway"~"^({_ROAD_HIGHWAY_TYPES})$"]["name"]'
            f"({s - pad},{w - pad},{n + pad},{e + pad}););"
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
            logger.warning("Overpass road fetch failed for bbox %s: %s", bbox, exc)
            osm_breaker.record_failure()
            stale = _read_road_cache_stale(cp)
            return stale if stale is not None else []

        ways = _parse_overpass_road_ways(result)
        try:
            tmp = cp.with_suffix(cp.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"fetched": time.time(), "ways": ways}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(cp)
        except OSError as exc:
            logger.warning("Failed to persist road cache %s: %s", cp, exc)
        _OSM_ROAD_MEM_CACHE[cp.stem] = ways
        osm_breaker.record_success()
        return ways


def _parse_overpass_road_ways(osm_json) -> list[dict]:
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
            "id":      e["id"],
            "name":    name,
            "highway": tags.get("highway"),
            "oneway":  tags.get("oneway") == "yes",
            "coords":  coords,
        })
    return ways


# ─── Combined trails+roads fetch ────────────────────────────────────────────

def fetch_osm_ways(bbox, paths_cache_dir: Path,
                   roads_cache_dir: Path | None) -> list[dict]:
    """Return named OSM ways (trails AND roads) for `bbox`. Each way carries
    a `kind` field of "trail" or "road" so downstream code can chip the
    leaderboard rows and (later) score routes against road segments.

    `roads_cache_dir=None` falls back to trails-only, preserving the
    pre-v13 behaviour for callers that haven't migrated yet.
    """
    trail_ways = fetch_osm_trails(bbox, paths_cache_dir)
    # Sentinel: the mem-cache returns the same list object on every call,
    # so once it's been tagged we don't need to rewrite the field. Cuts
    # an O(ways) dict-write loop per activity once the cache is warm.
    if trail_ways and "kind" not in trail_ways[0]:
        for w in trail_ways:
            w["kind"] = "trail"
    if roads_cache_dir is None:
        return trail_ways
    road_ways = fetch_osm_roads(bbox, roads_cache_dir)
    if road_ways and "kind" not in road_ways[0]:
        for w in road_ways:
            w["kind"] = "road"
    return trail_ways + road_ways


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


# ─── Trail topology + progress projection ────────────────────────────────────
# Each trail name maps to one of three topologies:
#   ('linear', chain_coords)     — chainable single or multi-fragment trail
#   ('loop',   (clat, clon))     — cyclic; track angular position around centroid
#   ('messy',  longest_coords)   — Y/T junctions, unchainable; fall back to longest
# The progress series for a run gives us a 1-D position-on-trail signal that
# we can scan for direction reversals (= laps).

_ENDPOINT_SHARE_M = 5.0  # endpoints within this radius are considered shared


def _trail_topology(ways: list[dict], name: str):
    """Return (kind, data) for the named trail. See module-level doc above."""
    frags = [w for w in ways if w["name"] == name and len(w.get("coords", [])) >= 2]
    if not frags:
        return ("messy", [])
    if len(frags) == 1:
        return ("linear", list(frags[0]["coords"]))

    coords_list = [list(f["coords"]) for f in frags]
    endpoints   = [(c[0], c[-1]) for c in coords_list]

    def _near(a, b):
        return _haversine_m(a[0], a[1], b[0], b[1]) <= _ENDPOINT_SHARE_M

    # Endpoint "degree": how many *other* fragment endpoints sit on top of it.
    n = len(coords_list)
    start_deg = [0] * n
    end_deg   = [0] * n
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if _near(endpoints[i][0], endpoints[j][0]) or _near(endpoints[i][0], endpoints[j][1]):
                start_deg[i] += 1
            if _near(endpoints[i][1], endpoints[j][0]) or _near(endpoints[i][1], endpoints[j][1]):
                end_deg[i]   += 1

    termini = [(i, side) for i in range(n) for side in (0, 1)
               if (start_deg[i] if side == 0 else end_deg[i]) == 0]

    if len(termini) == 2:
        start_idx, start_side = termini[0]
        chain = _walk_chain(coords_list, endpoints, start_idx, start_side)
        if chain and len(chain) >= 2:
            return ("linear", chain)

    if len(termini) == 0 and n >= 2:
        # No free endpoints — every endpoint shares with another. Treat as loop.
        all_pts = [c for cl in coords_list for c in cl]
        clat = sum(p[0] for p in all_pts) / len(all_pts)
        clon = sum(p[1] for p in all_pts) / len(all_pts)
        return ("loop", (clat, clon))

    # Y-junction, T, or other multi-terminus mess. Use the longest fragment as
    # a best-effort linear backbone — most laps register on the main backbone.
    longest = max(coords_list, key=_way_length_km)
    return ("messy", longest)


def _walk_chain(coords_list, endpoints, start_idx: int, start_side: int):
    """Greedy walk through connected fragments, building one ordered coord list."""
    n = len(coords_list)
    used = {start_idx}
    chain: list = []

    def _near(a, b):
        return _haversine_m(a[0], a[1], b[0], b[1]) <= _ENDPOINT_SHARE_M

    if start_side == 0:
        chain.extend(coords_list[start_idx])
        cur_exit = endpoints[start_idx][1]
    else:
        chain.extend(reversed(coords_list[start_idx]))
        cur_exit = endpoints[start_idx][0]

    while True:
        nxt_idx = None
        nxt_reverse = False
        for j in range(n):
            if j in used:
                continue
            if _near(cur_exit, endpoints[j][0]):
                nxt_idx = j
                nxt_reverse = False
                break
            if _near(cur_exit, endpoints[j][1]):
                nxt_idx = j
                nxt_reverse = True
                break
        if nxt_idx is None:
            break
        # Skip the shared endpoint when appending (avoids dup).
        if nxt_reverse:
            chain.extend(reversed(coords_list[nxt_idx][:-1]))
            cur_exit = endpoints[nxt_idx][0]
        else:
            chain.extend(coords_list[nxt_idx][1:])
            cur_exit = endpoints[nxt_idx][1]
        used.add(nxt_idx)
    return chain


def _chain_cumlen_m(chain: list) -> list[float]:
    """Cumulative length in metres at each chain coordinate. cum[0]=0."""
    cum = [0.0]
    for i in range(1, len(chain)):
        cum.append(cum[-1] + _haversine_m(chain[i - 1][0], chain[i - 1][1],
                                          chain[i][0], chain[i][1]))
    return cum


def _project_to_chain(plat: float, plon: float, chain: list,
                       cumlen: list[float]) -> float:
    """Return the rider point's fractional progress (0..1) along the chain.

    Scalar fallback used only when numpy is unavailable or chain is tiny
    (< 2 points). The hot path goes through _project_all_to_chain instead.
    """
    if len(chain) < 2 or cumlen[-1] <= 0:
        return 0.0
    best_d = float("inf")
    best_pos_m = 0.0
    for i in range(1, len(chain)):
        ax, ay = chain[i - 1]
        bx, by = chain[i]
        seg_lat = bx - ax
        seg_lon = by - ay
        seg_sq = seg_lat * seg_lat + seg_lon * seg_lon
        if seg_sq <= 0:
            t = 0.0
        else:
            t = max(0.0, min(1.0,
                ((plat - ax) * seg_lat + (plon - ay) * seg_lon) / seg_sq))
        proj_lat = ax + t * seg_lat
        proj_lon = ay + t * seg_lon
        d = _haversine_m(plat, plon, proj_lat, proj_lon)
        if d < best_d:
            best_d = d
            seg_len_m = cumlen[i] - cumlen[i - 1]
            best_pos_m = cumlen[i - 1] + t * seg_len_m
    return best_pos_m / cumlen[-1]


def _build_chain_np(chain: list, cumlen: list[float]):
    """Pre-compute numpy segment arrays for a chain.

    Returns a tuple (ax, ay, dx, dy, seg_sq_m, cum_arr, seg_len, total_m,
    ax0, ay0, lat_scale, lon_scale) where coordinates are in metres relative
    to the first chain node, using a local flat-Earth projection.
    Returns None if chain has fewer than 2 points or total_m <= 0.
    """
    if not _NUMPY_AVAILABLE or len(chain) < 2 or cumlen[-1] <= 0:
        return None
    lats = np.array([c[0] for c in chain], dtype=np.float64)
    lons = np.array([c[1] for c in chain], dtype=np.float64)
    ax0 = lats[0]
    ay0 = lons[0]
    R = 6371000.0
    lat_scale = R * math.pi / 180.0
    lon_scale = R * math.cos(math.radians(ax0)) * math.pi / 180.0
    ax_m = (lats[:-1] - ax0) * lat_scale
    ay_m = (lons[:-1] - ay0) * lon_scale
    dx_m = (lats[1:] - lats[:-1]) * lat_scale
    dy_m = (lons[1:] - lons[:-1]) * lon_scale
    seg_sq_m = dx_m * dx_m + dy_m * dy_m
    cum_arr = np.array(cumlen[:-1], dtype=np.float64)
    seg_len = np.diff(np.array(cumlen, dtype=np.float64))
    return (ax_m, ay_m, dx_m, dy_m, seg_sq_m,
            cum_arr, seg_len, cumlen[-1],
            ax0, ay0, lat_scale, lon_scale)


def _project_all_to_chain(plats: list[float], plons: list[float],
                           chain_np) -> list[float]:
    """Vectorised batch projection: all rider points against all chain segments.

    Replaces N sequential calls to _project_to_chain with one numpy broadcast
    of shape (N_points, M_segments). Typically 10-20x faster than the scalar
    loop for run sizes of 100-700 points and chain sizes of 50-500 segments.

    chain_np is the tuple returned by _build_chain_np.
    Distances use the local flat-Earth approximation built into chain_np —
    accurate to < 0.1 % over the < 5 km chains seen in practice. Progress
    rank-ordering is what matters, not absolute distance values.
    """
    (ax_m, ay_m, dx_m, dy_m, seg_sq_m,
     cum_arr, seg_len, total_m,
     ax0, ay0, lat_scale, lon_scale) = chain_np

    px_m = (np.array(plats, dtype=np.float64) - ax0) * lat_scale  # (N,)
    py_m = (np.array(plons, dtype=np.float64) - ay0) * lon_scale  # (N,)

    # t = clamp( dot(p-a, seg) / seg_sq, 0, 1 )  shape: (N, M)
    dot = ((px_m[:, None] - ax_m[None, :]) * dx_m[None, :] +
           (py_m[:, None] - ay_m[None, :]) * dy_m[None, :])
    safe_sq = np.where(seg_sq_m == 0, 1.0, seg_sq_m)
    t = np.clip(dot / safe_sq, 0.0, 1.0)
    t = np.where(seg_sq_m[None, :] == 0, 0.0, t)

    proj_x = ax_m[None, :] + t * dx_m[None, :]
    proj_y = ay_m[None, :] + t * dy_m[None, :]
    dist2 = (px_m[:, None] - proj_x) ** 2 + (py_m[:, None] - proj_y) ** 2

    best_seg = np.argmin(dist2, axis=1)                              # (N,)
    best_t   = t[np.arange(len(plats)), best_seg]                   # (N,)
    pos_m    = cum_arr[best_seg] + best_t * seg_len[best_seg]        # (N,)
    return (pos_m / total_m).tolist()


def _angle_unwrapped(plat: float, plon: float, prev_angle: float | None,
                      clat: float, clon: float) -> float:
    """Angle (rad) from centroid to point, unwrapped against prev so a full loop
    yields a continuously growing/shrinking value rather than wrapping at ±π.

    `prev_angle` is the *accumulated* unwrapped value (can be > 2π after
    multiple revolutions). We compare against its wrapped equivalent in
    (-π, π] — atan2(sin(prev), cos(prev)) — rather than `prev % 2π` which
    lives in [0, 2π) and would mis-compare across the seam.
    """
    a = math.atan2(plat - clat, plon - clon)
    if prev_angle is None:
        return a
    prev_wrapped = math.atan2(math.sin(prev_angle), math.cos(prev_angle))
    delta = a - prev_wrapped
    if delta > math.pi:
        delta -= 2 * math.pi
    elif delta < -math.pi:
        delta += 2 * math.pi
    return prev_angle + delta


def _compute_progress_series(points, s: int, e: int, topology,
                              cumlen: list[float] | None = None) -> list[float] | None:
    """Compute progress (0..1 for linear/messy, unbounded radians for loop)
    for each point in points[s:e]. Returns None when the topology can't
    yield meaningful progress (empty chain, etc.).

    `cumlen`, if supplied, is the precomputed chain cumulative-length
    series. Pass it when calling repeatedly for the same trail to skip
    the per-call rebuild.
    """
    kind, data = topology
    if kind in ("linear", "messy"):
        chain = data
        if not chain or len(chain) < 2:
            return None
        if cumlen is None:
            cumlen = _chain_cumlen_m(chain)
        if cumlen[-1] <= 0:
            return None

        # Vectorised path: collect all valid (lat, lon) in one pass, project
        # all at once, then stitch holes back in. Falls back to the scalar
        # loop when numpy is unavailable.
        if _NUMPY_AVAILABLE:
            chain_np = _build_chain_np(chain, cumlen)
            if chain_np is not None:
                # First pass: identify which indices have valid coords and
                # collect them. Holes carry the previous value forward.
                valid_idx: list[int] = []   # position within [s, e)
                plats: list[float] = []
                plons: list[float] = []
                hole_fill: float = 0.0
                out: list[float] = [0.0] * (e - s)
                for i in range(s, e):
                    rel = i - s
                    try:
                        plats.append(points[i]["lat"])
                        plons.append(points[i]["lon"])
                        valid_idx.append(rel)
                    except (KeyError, IndexError):
                        out[rel] = hole_fill  # will be overwritten below
                if not plats:
                    return [0.0] * (e - s)
                progress = _project_all_to_chain(plats, plons, chain_np)
                for vi, prog in zip(valid_idx, progress):
                    out[vi] = prog
                # Fill holes with nearest valid neighbour (carry-forward then
                # carry-backward for leading holes).
                valid_set = set(valid_idx)
                last = 0.0
                for i in range(e - s):
                    if i in valid_set:
                        last = out[i]
                    else:
                        out[i] = last
                return out

        # Scalar fallback (numpy unavailable)
        out_scalar: list[float] = []
        for i in range(s, e):
            try:
                plat = points[i]["lat"]
                plon = points[i]["lon"]
            except (KeyError, IndexError):
                # Carry the previous value forward so smoothing/diff math
                # doesn't get fooled by a hole.
                out_scalar.append(out_scalar[-1] if out_scalar else 0.0)
                continue
            out_scalar.append(_project_to_chain(plat, plon, chain, cumlen))
        return out_scalar
    if kind == "loop":
        clat, clon = data
        out_a: list[float] = []
        prev: float | None = None
        for i in range(s, e):
            try:
                plat = points[i]["lat"]
                plon = points[i]["lon"]
            except (KeyError, IndexError):
                out_a.append(prev if prev is not None else 0.0)
                continue
            ang = _angle_unwrapped(plat, plon, prev, clat, clon)
            out_a.append(ang)
            prev = ang
        return out_a
    return None


def _median_smooth(series: list[float], window: int) -> list[float]:
    """Sliding-window median. Edges fall back to centered partial windows.
    Pure Python — series here is at most a few thousand points per run."""
    if window <= 1 or len(series) < 3:
        return list(series)
    half = window // 2
    out = []
    n = len(series)
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        chunk = sorted(series[lo:hi])
        out.append(chunk[len(chunk) // 2])
    return out


def _split_traversals_linear(progress: list[float], min_delta: float) -> list[tuple[int, int, str]]:
    """Walk the smoothed progress series and emit (sub_s, sub_e, direction)
    segments where direction in {'+', '-'}. A direction flip is only
    recognised once a min_delta swing has accumulated against the running
    direction — that filters noise while still catching real laps.
    """
    n = len(progress)
    if n < 4:
        return [(0, n, "+")]

    # Find first non-trivial direction.
    start = 0
    direction = None
    extremum = progress[0]
    for i in range(1, n):
        d = progress[i] - extremum
        if d >= min_delta:
            direction = "+"
            break
        if d <= -min_delta:
            direction = "-"
            break
        if progress[i] < extremum and direction is None:
            extremum = progress[i]
    if direction is None:
        # Whole run is monotone-ish or too small to flip. One traversal.
        return [(0, n, "+" if progress[-1] >= progress[0] else "-")]

    out: list[tuple[int, int, str]] = []
    seg_start = 0
    extremum = progress[0]
    for i in range(1, n):
        v = progress[i]
        if direction == "+":
            if v > extremum:
                extremum = v
                continue
            if extremum - v >= min_delta:
                # Direction flipped. Close current traversal at the local max.
                # Walk back from i with STRICT inequality so a plateau leaves
                # the peak at the first plateau index, not at seg_start.
                peak_idx = i - 1
                while peak_idx > seg_start and progress[peak_idx - 1] > progress[peak_idx]:
                    peak_idx -= 1
                out.append((seg_start, peak_idx + 1, "+"))
                seg_start = peak_idx
                direction = "-"
                extremum = v
        else:  # direction == "-"
            if v < extremum:
                extremum = v
                continue
            if v - extremum >= min_delta:
                trough_idx = i - 1
                while trough_idx > seg_start and progress[trough_idx - 1] < progress[trough_idx]:
                    trough_idx -= 1
                out.append((seg_start, trough_idx + 1, "-"))
                seg_start = trough_idx
                direction = "+"
                extremum = v
    out.append((seg_start, n, direction))
    return out


def _split_traversals_loop(angles: list[float]) -> list[tuple[int, int, str]]:
    """Loop trails: split on full 2π revolutions. Each ±2π span = one lap."""
    import math as _m
    n = len(angles)
    if n < 4:
        return [(0, n, "+")]
    twopi = 2 * _m.pi
    out: list[tuple[int, int, str]] = []
    seg_start = 0
    seg_origin = angles[0]
    for i in range(1, n):
        delta = angles[i] - seg_origin
        if delta >= twopi:
            out.append((seg_start, i + 1, "+"))
            seg_start = i
            seg_origin = angles[i]
        elif delta <= -twopi:
            out.append((seg_start, i + 1, "-"))
            seg_start = i
            seg_origin = angles[i]
    # Trailing partial revolution
    final_delta = angles[-1] - seg_origin
    if abs(final_delta) > _m.pi * 0.25 or not out:
        out.append((seg_start, n, "+" if final_delta >= 0 else "-"))
    return out


def _classify_direction(points, abs_s: int, abs_e: int,
                         progress_dir: str, kind: str) -> str:
    """Direction label for a traversal.

    Loop trails always use 'forward' / 'reverse' regardless of elevation
    delta — net climb per loop is ~0 so up/down would always be 'mixed'.
    Linear and messy trails prefer up/down when elevation is decisive
    (>= TRAVERSAL_MIN_ELE_DELTA_M) and fall through to forward/reverse
    when it isn't. 'mixed' is only emitted when the run is too short to
    classify at all.
    """
    if abs_e - abs_s < 2:
        return "mixed"

    if kind == "loop":
        return "forward" if progress_dir == "+" else "reverse"

    def _ele(i):
        return (points[i].get("ele_sm")
                if points[i].get("ele_sm") is not None
                else points[i].get("ele")) or 0.0
    delta = _ele(abs_e - 1) - _ele(abs_s)
    if delta >= TRAVERSAL_MIN_ELE_DELTA_M:
        return "up"
    if delta <= -TRAVERSAL_MIN_ELE_DELTA_M:
        return "down"
    # Elevation ambiguous — surface as forward/reverse along the OSM way's
    # canonical direction so the leaderboard still separates "same trail,
    # opposite direction" attempts.
    return "forward" if progress_dir == "+" else "reverse"


def _split_run_into_traversals(points, run, topology, cumlen=None):
    """Top-level: take a kept run (name, span_start, span_end, ridden_km)
    and split it into one-or-more (sub_s, sub_e, direction_label) entries.
    Each entry is a traversal of the trail.

    `cumlen`, if supplied, is the pre-built chain cumulative-length series
    for `topology` (linear/messy only). Caller can compute it once per
    trail name and reuse across this run's traversals — saves a chain
    re-walk per call.
    """
    name, s, e, _d_km = run
    kind, _data = topology
    series = _compute_progress_series(points, s, e, topology, cumlen)
    if not series:
        # No topology / too short. One traversal, direction from elevation only.
        return [(s, e, _classify_direction(points, s, e, "+", kind))]

    smooth = _median_smooth(series, TRAVERSAL_PROGRESS_SMOOTH_WINDOW)
    if kind == "loop":
        chunks = _split_traversals_loop(smooth)
    else:
        chunks = _split_traversals_linear(smooth, TRAVERSAL_MIN_PROGRESS_DELTA)

    # Drop very short chunks (snap noise) by folding into the neighbour.
    chunks = _fold_short_chunks(chunks, TRAVERSAL_MIN_POINTS)

    out = []
    for sub_s, sub_e, prog_dir in chunks:
        abs_s = s + sub_s
        abs_e = s + sub_e
        direction = _classify_direction(points, abs_s, abs_e, prog_dir, kind)
        out.append((abs_s, abs_e, direction))
    return out


def _fold_short_chunks(chunks: list[tuple[int, int, str]],
                       min_pts: int) -> list[tuple[int, int, str]]:
    """Merge sub-min-point chunks into an adjacent one to suppress GPS jitter
    that the progress smoothing didn't fully erase."""
    if len(chunks) <= 1:
        return chunks
    changed = True
    while changed and len(chunks) > 1:
        changed = False
        for i, (s, e, d) in enumerate(chunks):
            if e - s >= min_pts:
                continue
            # Fold into longer neighbour
            if i == 0:
                # merge into next
                nxt = chunks[i + 1]
                chunks = [(s, nxt[1], nxt[2])] + chunks[i + 2:]
            elif i == len(chunks) - 1:
                prev = chunks[i - 1]
                chunks = chunks[:i - 1] + [(prev[0], e, prev[2])]
            else:
                prev = chunks[i - 1]
                nxt = chunks[i + 1]
                # Merge with whichever is longer
                if (prev[1] - prev[0]) >= (nxt[1] - nxt[0]):
                    chunks = chunks[:i - 1] + [(prev[0], e, prev[2])] + chunks[i + 1:]
                else:
                    chunks = chunks[:i] + [(s, nxt[1], nxt[2])] + chunks[i + 2:]
            changed = True
            break
    return chunks


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
            "kind": w.get("kind", "trail"),
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
    """Return (best_name, best_dist, best_kind) per point. Assisted points
    get (None, None, None) so they break visit runs naturally.

    Tracking `best_kind` per snap (not by post-hoc longest-fragment lookup)
    is required: a trail and road named "Connector" would otherwise be
    chip-labelled by whichever fragment is geometrically longer instead
    of by the one this GPS point actually snapped to.

    When numpy is available (nearly always), each way's segments are evaluated
    as a vectorised array broadcast — one numpy call replaces the inner Python
    for-loop over segments. The bbox prune per way is unchanged. The scalar
    fallback path is preserved for environments without numpy.
    """
    use_numpy = _NUMPY_AVAILABLE
    matches = []
    for p in points:
        if p.get("assisted"):
            matches.append((None, None, None))
            continue
        plat, plon = p["lat"], p["lon"]
        px, py = _to_local(plat, plon, lat0, lon0)
        best_name, best_dist, best_kind = None, SNAP_THRESHOLD_M, None
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
                best_kind = w.get("kind", "trail")
        matches.append((best_name, best_dist if best_name else None,
                        best_kind if best_name else None))
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
                   max_gap_sec: float = COALESCE_MAX_GAP_SEC,
                   max_crossing_sec: float = COALESCE_MAX_CROSSING_SEC):
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
                and _gap_sec(out[-1], nxt) <= max_gap_sec):
            out[-1] = (out[-1][0], out[-1][1], nxt[2], out[-1][3] + nxt[3])
            i += 1
            continue
        # Pattern 2: A, B, A where B is a different name AND short. Drop
        # B and merge the A's. Distance is A1 + A2 (NOT span) — see the
        # docstring above for why this matters.
        if (i + 1 < len(runs)
                and runs[i + 1][0] == out[-1][0]
                and nxt[0] != out[-1][0]
                and _dur_sec(nxt) <= max_crossing_sec):
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


def match_trails(data: dict, cache_dir: Path,
                  roads_cache_dir: Path | None = None) -> dict:
    """Match a parsed GPX activity to named OSM trails + roads.

    `data` must look like the dict returned by `get_activity`: has `points`
    (list of {lat, lon, time, dist_km, assisted, ...}) and `bbox`
    ([south, west, north, east]).

    `cache_dir`: directory for the bbox -> trail OSM ways disk cache
    (`cache/osm_paths/` in production).
    `roads_cache_dir`: directory for the bbox -> road OSM ways cache
    (`cache/osm_roads/`). When None, trails-only mode (pre-v13 behaviour).

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

    ways = fetch_osm_ways(bbox, cache_dir, roads_cache_dir)
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
    name_kind: dict[str, str] = {}
    for nm, w in longest_frag.items():
        if w.get("highway"):
            name_highway[nm] = w["highway"]
        if w.get("mtb_scale"):
            name_mtb_scale[nm] = w["mtb_scale"]
        name_kind[nm] = w.get("kind", "trail")
    # Override name_kind with snap-derived majority when a name has
    # contradictory kinds (an OSM "Connector" exists as both a trail and a
    # road). The longest-fragment lookup would pick whichever has more
    # mapped length, which isn't necessarily what was ridden. Counting
    # actual snap hits picks the kind the rider was on.
    snap_kind_votes: dict[str, dict[str, int]] = {}
    for nm, dist, kd in matches:
        if not nm or not kd:
            continue
        snap_kind_votes.setdefault(nm, {})
        snap_kind_votes[nm][kd] = snap_kind_votes[nm].get(kd, 0) + 1
    for nm, votes in snap_kind_votes.items():
        name_kind[nm] = max(votes.items(), key=lambda kv: kv[1])[0]

    # Compute outer termini for each trail name from the RAW (unprojected)
    # ways. Used by the endpoint-touch completion override below — a run
    # whose snap coverage falls short of COMPLETE_COVERAGE_PCT can still
    # be marked completed if the rider's path visits within
    # ENDPOINT_TOUCH_RADIUS_M of both termini during that run.
    # Computed once per name and reused across runs / summary aggregation.
    termini_by_name: dict[str, tuple[tuple, tuple] | None] = {}
    topology_by_name: dict[str, tuple] = {}
    # Cumulative chain lengths per trail name. Built once here and reused
    # for every traversal of that trail in this ride — laps on the same
    # trail (e.g. Merlin View) would otherwise rebuild this each call.
    cumlen_by_name: dict[str, list[float]] = {}
    for nm in name_length_km:
        termini_by_name[nm] = _trail_termini(ways, nm)
        topo = _trail_topology(ways, nm)
        topology_by_name[nm] = topo
        if topo[0] in ("linear", "messy"):
            chain = topo[1]
            if chain and len(chain) >= 2:
                cumlen_by_name[nm] = _chain_cumlen_m(chain)

    timeline_raw: list = []
    by_name: dict[str, dict] = {}
    for run in runs:
        name, s, e, d_km = run
        topology = topology_by_name.get(name) or ("messy", [])
        # Split the run into one or more traversals based on progress-along-
        # trail reversals. Single-direction climbs/descents produce one
        # entry; lapping a trail (most acutely on loops like Merlin View)
        # produces N entries, one per traversal.
        traversals = _split_run_into_traversals(points, run, topology,
                                                cumlen_by_name.get(name))

        for sub_s, sub_e, direction in traversals:
            if sub_e <= sub_s:
                continue
            ss_pt = points[sub_s]
            ee_pt = points[sub_e - 1]
            try:
                sub_dur = (_parse_iso(ee_pt["time"]) - _parse_iso(ss_pt["time"])).total_seconds()
            except (KeyError, ValueError):
                sub_dur = 0.0
            sub_d_km = (ee_pt.get("dist_km") or 0.0) - (ss_pt.get("dist_km") or 0.0)
            dists = [m[1] for m in matches[sub_s:sub_e] if m[1] is not None]
            avg_dist_m = sum(dists) / len(dists) if dists else None
            entry = {
                "name": name,
                "kind": name_kind.get(name, "trail"),
                "direction": direction,
                "start_idx": sub_s, "end_idx": sub_e - 1,
                "start_time": ss_pt.get("time", ""),
                "end_time": ee_pt.get("time", ""),
                "duration_sec": round(sub_dur),
                "distance_km": round(sub_d_km, 3),
                "points": sub_e - sub_s,
                "avg_dist_m": round(avg_dist_m, 1) if avg_dist_m is not None else None,
            }
            timeline_raw.append(entry)
            # Aggregate into the by-name+direction bucket so the summary
            # rollup and leaderboard see direction-split totals.
            if sub_d_km < TIMELINE_MIN_DISTANCE_KM:
                continue
            key = (name, direction)
            rec = by_name.setdefault(key, {
                "ridden_km": 0.0, "runs": [],
                "first_time": ss_pt.get("time", ""),
            })
            rec["ridden_km"] += max(sub_d_km, 0.0)
            rec["runs"].append((sub_s, sub_e))

    # Merge runs of the same name+direction into "visits" when they're close
    # in time. Same-name, opposite-direction runs are by definition different
    # traversals so they each count as a separate visit; the splitter has
    # already separated them.
    summary = []
    for key, rec in by_name.items():
        name, direction = key
        rec["runs"].sort()
        visits = len(rec["runs"])  # each traversal = a visit now
        # Optional: merge consecutive runs that the visit-gap rule would have
        # collapsed (e.g. same-direction same-trail back-to-back, no real
        # break). Keeping the raw count is more honest since the splitter
        # already operates on continuous progress signals.
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
            "kind": name_kind.get(name, "trail"),
            "direction": direction,
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
    # `qualifying` keys on (name, direction) — but for name-only fallback
    # (e.g. a timeline traversal that didn't independently qualify because
    # its sub-distance was small) we let any direction of the same name
    # pass through via name-only check too.
    qualifying = {(t["name"], t["direction"]): t["osm_length_km"] for t in summary}
    qualifying_names = {t["name"] for t in summary}
    timeline = []
    for e in timeline_raw:
        if e["name"] not in qualifying_names or e["distance_km"] < TIMELINE_MIN_DISTANCE_KM:
            continue
        key = (e["name"], e.get("direction", "mixed"))
        osm_len = qualifying.get(key) or name_length_km.get(e["name"], 0.0)
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
                 meta_fp: str = "",
                 cache_dir_roads: Path | None = None) -> dict:
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

    result = match_trails(data, cache_dir_osm, roads_cache_dir=cache_dir_roads)
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
                       ) -> dict[tuple[str, str], list[dict]]:
    """Aggregate cached results into per-(trail, direction) leaderboards.

    Keys are `(trail_name, direction)` where direction ∈ {'up', 'down',
    'mixed'}. A trail ridden in both directions therefore has two
    leaderboards (and a mixed one if the rider did out-and-back loops).
    Each leaderboard is sorted fastest first.
    """
    cached = scan_cached_results(cache_dir_results)
    boards: dict[tuple[str, str], list[dict]] = {}
    for filename, _mtime, result in cached:
        timeline = (result or {}).get("timeline") or []
        for entry in timeline:
            if not entry.get("completed"):
                continue
            name = entry.get("name")
            if not name:
                continue
            direction = entry.get("direction") or "mixed"
            row = {
                "filename": filename,
                "title":     (activity_meta or {}).get(filename, {}).get("title") or filename,
                "start_time": entry.get("start_time", ""),
                "date":       (entry.get("start_time") or "")[:10],
                "duration_sec": int(entry.get("duration_sec") or 0),
                "distance_km":  float(entry.get("distance_km") or 0.0),
                "coverage_pct": float(entry.get("coverage_pct") or 0.0),
                "direction":    direction,
                "kind":         entry.get("kind", "trail"),
                "start_idx":    int(entry.get("start_idx") or 0),
                "end_idx":      int(entry.get("end_idx")   or 0),
            }
            boards.setdefault((name, direction), []).append(row)

    for key, rows in boards.items():
        rows.sort(key=lambda r: (r["duration_sec"], r["filename"]))
    return boards


def build_region_trail_index(cache_dir_results: Path,
                             activity_meta: dict[str, dict] | None,
                             activity_regions: dict[str, list[str]],
                             ) -> dict[str, dict[str, dict]]:
    """Group completed trail attempts by the region(s) the source ride is in.

    Returns `{region_id: {trail_name: {attempts, best_duration_sec,
    best_filename, best_date, best_start_idx, best_end_idx}}}`.

    A trail that appears in rides across multiple regions is recorded
    under each region — e.g. a generic "Connector" trail that exists in
    both Moose Mountain rides and Bragg Creek rides shows up in both.
    The "attempts" + "best" stats are then scoped to that region (only
    rides matching that region count toward its leaderboard for the
    trail). This matches how a rider thinks: "what's my best Pneuma at
    Moose Mountain?" is a different question from "best Pneuma anywhere".

    Args:
        cache_dir_results: per-file trail_match cache directory.
        activity_meta: optional `{filename: {title, ...}}` from metadata.json.
        activity_regions: `{filename: [region_id, ...]}` from the
            sidebar / activity index — needed because the trail_match
            cache itself doesn't know about regions.

    Completed runs only — partials are not meaningful for "best time".
    """
    cached = scan_cached_results(cache_dir_results)
    out: dict[str, dict[str, dict]] = {}
    activity_meta = activity_meta or {}
    for filename, _mtime, result in cached:
        regions = activity_regions.get(filename) or []
        if not regions:
            continue
        timeline = (result or {}).get("timeline") or []
        for entry in timeline:
            if not entry.get("completed"):
                continue
            name = entry.get("name")
            if not name:
                continue
            direction = entry.get("direction") or "mixed"
            # Bucket key is name + direction so "Cutoff up" and "Cutoff
            # down" rank against each other separately. Display name in
            # the UI is the same; the direction field carries the badge.
            bucket_name = name
            dur = int(entry.get("duration_sec") or 0)
            date = (entry.get("start_time") or "")[:10]
            start_idx = int(entry.get("start_idx") or 0)
            end_idx   = int(entry.get("end_idx")   or 0)
            for region_id in regions:
                region_dict = out.setdefault(region_id, {})
                trail_key = (bucket_name, direction)
                trail = region_dict.get(trail_key)
                if trail is None:
                    region_dict[trail_key] = {
                        "name":      bucket_name,
                        "direction": direction,
                        "kind":      entry.get("kind", "trail"),
                        "attempts": 1,
                        "best_duration_sec": dur,
                        "best_filename": filename,
                        "best_title": activity_meta.get(filename, {}).get("title") or filename,
                        "best_date": date,
                        "best_start_idx": start_idx,
                        "best_end_idx":   end_idx,
                    }
                else:
                    trail["attempts"] += 1
                    if dur < trail["best_duration_sec"]:
                        trail["best_duration_sec"] = dur
                        trail["best_filename"]    = filename
                        trail["best_title"]       = activity_meta.get(filename, {}).get("title") or filename
                        trail["best_date"]        = date
                        trail["best_start_idx"]   = start_idx
                        trail["best_end_idx"]     = end_idx
    return out


def rank_for_attempt(boards: dict[tuple[str, str], list[dict]],
                     name: str, direction: str,
                     filename: str, start_idx: int) -> tuple[int, int] | None:
    """Find a specific attempt's 1-based rank within its (trail, direction)
    leaderboard. Identity is `(filename, start_idx)`."""
    rows = boards.get((name, direction))
    if not rows:
        return None
    for i, row in enumerate(rows, start=1):
        if row["filename"] == filename and row["start_idx"] == start_idx:
            return (i, len(rows))
    return None
