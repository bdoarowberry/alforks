"""Lift / assisted-segment detection algorithms.

All algorithms take the same signature:
    (per_pt, latlons, osm_lifts) -> list[bool]
where:
    per_pt  = [{'dt','dist','speed','ele_delta'}, ...]  transitions
    latlons = [(lat, lon), ...]                         per point
    osm_lifts = [{'a':(lat,lon), 'b':(lat,lon), 'name':str}, ...]

Plus geometry helpers (haversine, sinuosity, median filter), segment
builders, stats computation, and per-point reconstruction from cached
parsed-GPX data.

Dependency-free: no Flask, no filesystem IO. Safe to import from tests.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime


# ─── Detection thresholds ────────────────────────────────────────────────────

# Time-gap shuttle detection (GPS off during transit)
_ASSISTED_MIN_DT_SEC = 200
_ASSISTED_MIN_GAIN_M = 100

# Gondola/chairlift detection (GPS on, steady uphill at cable speed)
_LIFT_SPEED_MIN  =  6.0   # km/h
_LIFT_SPEED_MAX  = 25.0   # km/h
_LIFT_SPEED_STD  = 13.0   # km/h
_LIFT_SINUOSITY  =  1.15
_LIFT_WINDOW     = 60
_LIFT_WIN_GAIN   = 25
_LIFT_MIN_GAIN   = 40
_LIFT_MIN_DUR    = 90

_STATION_THRESH_M   = 100
_LIFT_MIN_RIDE_SEC  = 60
_LIFT_MAX_RIDE_SEC  = 30 * 60
_LIFT_MIN_NET_GAIN  = 50

# Elevation-rate algorithm
_ELEV_RATE_WIN_SEC   = 60
_ELEV_RATE_MIN_GAIN  = 25
_ELEV_RATE_THRESHOLD = 5.0
_ELEV_RATE_MIN_DUR   = 60

# Smart-combined algorithm
_SMART_STATION_THRESH_M = 250
_SMART_SNAP_WINDOW      = 60
_SMART_TRIM_MAX         = 45

# MTB shuttle detector
_SHUTTLE_SPEED_MIN = 20.0
_SHUTTLE_WIN_SEC   = 90
_SHUTTLE_MIN_GAIN  = 50

# MTB-specific elevation-rate thresholds
_MTB_ELEV_THRESHOLD = 15.0
_MTB_ELEV_MIN_GAIN  = 40


# ─── Geometry helpers ────────────────────────────────────────────────────────

def haversine(p1, p2) -> float:
    R = 6371000
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _sinuosity(start: tuple, end: tuple, path_dist: float) -> float:
    # Epsilon of 1 m guards against a zero-division when start and end are
    # effectively the same point (GPS noise or a closed loop) while still
    # letting the real (large) sinuosity surface when the straight-line
    # distance is genuinely small but the path wandered — prevents a noisy
    # short segment from being clamped to 1.0 and misdetected as a lift.
    straight = haversine(start, end)
    return path_dist / straight if straight > 1 else 1.0


def _median_filter(values: list, k: int = 5) -> list:
    """Sliding-window median filter. Hardcoded-fast path for k=5 (the only
    size used in the app) — avoids statistics.median's sort-and-call
    overhead on a million-plus filter calls during prewarm. The generic
    path covers other k values for completeness and tests."""
    n = len(values)
    out: list = []
    if k == 5:
        # Inline median for windows of size 1–5. len-branching is faster
        # than sorted(...)[mid] in a hot loop at this size.
        for i in range(n):
            lo = i - 2 if i - 2 >= 0 else 0
            hi = i + 3 if i + 3 <= n else n
            window = [x for x in values[lo:hi] if x is not None]
            wl = len(window)
            if wl == 0:
                out.append(values[i])
            elif wl == 1:
                out.append(window[0])
            else:
                window.sort()
                if wl % 2:
                    out.append(window[wl // 2])
                else:
                    mid = wl // 2
                    out.append((window[mid - 1] + window[mid]) / 2)
        return out
    # Generic path (kept for completeness; not on the hot code path)
    half = k // 2
    for i, v in enumerate(values):
        window = [values[j] for j in range(max(0, i - half), min(n, i + half + 1))
                  if values[j] is not None]
        out.append(statistics.median(window) if window else v)
    return out


def _prefix_sum(per_pt: list, field: str) -> list[float]:
    """Cumulative sum of per_pt[i][field]. prefix[0] = 0; prefix[i+1] = sum(per_pt[0..i][field]).
    Segment gain from index a to b inclusive = prefix[b + 1] - prefix[a].
    Lets us replace O(seg_len) sum() calls inside lift-candidate loops with O(1) lookups.
    """
    n = len(per_pt)
    prefix = [0.0] * (n + 1)
    cum = 0.0
    for i in range(n):
        cum += per_pt[i][field]
        prefix[i + 1] = cum
    return prefix


# ─── Station-proximity lift detector ─────────────────────────────────────────

def _detect_station_lifts(latlons: list, per_pt: list, lifts: list[dict]) -> list[bool]:
    """OSM aerialway matching: flag points between a bottom→top station pair
    where the ride duration and net gain are plausible.
    """
    n = len(latlons)
    assisted = [False] * n
    if n == 0 or not lifts:
        return assisted

    cum_t = [0.0] * n
    for i in range(1, n):
        cum_t[i] = cum_t[i - 1] + per_pt[i]["dt"]

    cum_gain = [0.0] * n
    for i in range(1, n):
        cum_gain[i] = cum_gain[i - 1] + per_pt[i]["ele_delta"]

    # Cheap lat/lon-degree bbox pre-filter around each station. 0.003 degrees
    # is ~335 m of latitude everywhere, and ~215 m of longitude at latitude 50
    # (where this app is used). That comfortably contains the 100 m STATION
    # threshold with margin. Above roughly latitude 72 the longitude scale
    # shrinks enough that 0.003 could reject valid candidates — not a concern
    # for the mid-latitude resorts this app targets, but worth revisiting if
    # the app ever ingests polar tracks. Unsafe-latitude boundary derivation:
    # arccos(_STATION_THRESH_M / (111_000 * _BBOX_TOL)) ≈ 72.5°.
    _BBOX_TOL = 0.003
    for lift in lifts:
        for bottom, top in [(lift["a"], lift["b"]), (lift["b"], lift["a"])]:
            blat, blon = bottom
            tlat, tlon = top
            near_b = [False] * n
            near_t = [False] * n
            for j in range(n):
                lat, lon = latlons[j]
                if abs(lat - blat) < _BBOX_TOL and abs(lon - blon) < _BBOX_TOL:
                    near_b[j] = haversine(latlons[j], bottom) <= _STATION_THRESH_M
                if abs(lat - tlat) < _BBOX_TOL and abs(lon - tlon) < _BBOX_TOL:
                    near_t[j] = haversine(latlons[j], top) <= _STATION_THRESH_M

            i = 0
            while i < n:
                b_idx = None
                for j in range(i, n):
                    if near_b[j]:
                        b_idx = j
                        break
                if b_idx is None:
                    break
                t_idx = None
                for j in range(b_idx + 1, n):
                    dt = cum_t[j] - cum_t[b_idx]
                    if dt > _LIFT_MAX_RIDE_SEC:
                        break
                    if near_t[j] and dt >= _LIFT_MIN_RIDE_SEC:
                        t_idx = j
                        break
                if t_idx is not None:
                    net_gain = cum_gain[t_idx] - cum_gain[b_idx]
                    if net_gain >= _LIFT_MIN_NET_GAIN:
                        for k in range(b_idx, t_idx + 1):
                            assisted[k] = True
                    i = t_idx + 1
                else:
                    i = b_idx + 1
    return assisted


# ─── Segment / stats helpers ─────────────────────────────────────────────────

def _build_segments(is_assisted: list[bool]) -> list[dict]:
    n = len(is_assisted)
    if n == 0:
        return []
    segments = []
    cur = 'riding'
    seg_start = 0
    for i in range(1, n):
        t = 'assisted' if is_assisted[i] else 'riding'
        if t != cur:
            segments.append({"type": cur, "start": seg_start, "end": i - 1})
            cur = t
            seg_start = i - 1
    segments.append({"type": cur, "start": seg_start, "end": n - 1})
    return segments


def _compute_algo_stats(is_assisted: list[bool], per_pt: list) -> dict:
    """Compute riding stats for a given is_assisted flag array."""
    n = len(per_pt)
    riding_dist = elev_gain = elev_loss = assisted_gain = 0.0
    riding_dur_sec = 0.0
    lift_count = 0
    in_lift = False
    for i in range(1, n):
        p = per_pt[i]
        flag = is_assisted[i]
        if flag and not in_lift:
            lift_count += 1
            in_lift = True
        elif not flag:
            in_lift = False
        if flag:
            if p['ele_delta'] > 0:
                assisted_gain += p['ele_delta']
        else:
            riding_dist += p['dist']
            riding_dur_sec += p['dt']
            if   p['ele_delta'] > 0: elev_gain += p['ele_delta']
            elif p['ele_delta'] < 0: elev_loss -= p['ele_delta']
    return {
        "distance_km":     round(riding_dist / 1000, 2),
        "elev_gain_m":     round(elev_gain),
        "elev_loss_m":     round(elev_loss),
        "assisted_gain_m": round(assisted_gain),
        "lift_count":      lift_count,
        "riding_dur_sec":  riding_dur_sec,
    }


def _merge_stats(is_assisted: list[bool], per_pt: list, base_stats: dict) -> dict:
    """Rebuild a full stats dict from is_assisted flags, preserving the
    fields that depend on the original recording (duration, max_speed, peak)."""
    s = _compute_algo_stats(is_assisted, per_pt)
    duration = base_stats.get('duration_sec')
    # avg_speed uses riding time only (see convention in app.parse_gpx).
    avg_speed = None
    riding_dur = s['riding_dur_sec']
    if riding_dur > 0 and s['distance_km'] > 0:
        avg_speed = round(s['distance_km'] / (riding_dur / 3600), 1)
    return {
        'distance_km':     s['distance_km'],
        'duration_sec':    duration,
        'elev_gain_m':     s['elev_gain_m'],
        'elev_loss_m':     s['elev_loss_m'],
        'assisted_gain_m': s['assisted_gain_m'],
        'avg_speed_kmh':   avg_speed,
        'max_speed_kmh':   base_stats.get('max_speed_kmh'),
        'lift_count':      s['lift_count'],
        'peak_ele_m':      base_stats.get('peak_ele_m'),
    }


def _per_pt_from_points(points: list) -> tuple[list, list]:
    """Reconstruct per_pt transitions and latlons from the cached points array.
    Uses smoothed speed already stored in points, so no re-filtering needed.
    """
    latlons = [(p["lat"], p["lon"]) for p in points]
    per_pt = [{'dt': 0, 'dist': 0.0, 'speed': None, 'ele_delta': 0.0}]
    for i in range(1, len(points)):
        prev, curr = points[i - 1], points[i]
        dt = 0
        if prev.get("time") and curr.get("time"):
            try:
                t0 = datetime.fromisoformat(prev["time"])
                t1 = datetime.fromisoformat(curr["time"])
                dt = (t1 - t0).total_seconds()
            except Exception:
                pass
        ele_delta = 0.0
        if prev.get("ele") is not None and curr.get("ele") is not None:
            ele_delta = curr["ele"] - prev["ele"]
        per_pt.append({
            'dt':        dt,
            'dist':      haversine(latlons[i - 1], latlons[i]),
            'speed':     curr.get("speed"),
            'ele_delta': ele_delta,
        })
    return per_pt, latlons


# ─── Dispatch-form algorithms ────────────────────────────────────────────────
# Signature: (per_pt, latlons, osm_lifts) -> list[bool]

def _algo_time_gap(per_pt: list, latlons: list, _osm: list) -> list[bool]:
    """GPS dropout with elevation gain — shuttles, GPS-off transit."""
    n = len(per_pt)
    assisted = [False] * n
    for i in range(1, n):
        if per_pt[i]['dt'] >= _ASSISTED_MIN_DT_SEC and per_pt[i]['ele_delta'] >= _ASSISTED_MIN_GAIN_M:
            assisted[i] = True
    return assisted


def _algo_speed_sinuosity(per_pt: list, latlons: list, _osm: list) -> list[bool]:
    """Consistent cable speed on a straight-line path — no time-gap, no OSM."""
    n = len(per_pt)
    assisted = [False] * n
    i = 1
    while i < n:
        win = per_pt[i: i + _LIFT_WINDOW]
        spds = [p['speed'] for p in win if p['speed'] is not None and 0 < p['dt'] < 5]
        gain = sum(p['ele_delta'] for p in win)
        if len(spds) >= _LIFT_WINDOW // 2 and gain >= _LIFT_WIN_GAIN:
            avg = sum(spds) / len(spds)
            if _LIFT_SPEED_MIN < avg < _LIFT_SPEED_MAX:
                j = min(i + _LIFT_WINDOW, n)
                while j < n:
                    recent = per_pt[max(i, j - 20): j]
                    rs = [p['speed'] for p in recent if p['speed'] is not None and 0 < p['dt'] < 5]
                    if not rs or not (_LIFT_SPEED_MIN < sum(rs) / len(rs) < _LIFT_SPEED_MAX):
                        break
                    j += 1
                seg_spds = [p['speed'] for p in per_pt[i:j] if p['speed'] is not None and 0 < p['dt'] < 5]
                total_gain = sum(p['ele_delta'] for p in per_pt[i:j])
                total_dur  = sum(p['dt']        for p in per_pt[i:j])
                total_dist = sum(p['dist']      for p in per_pt[i:j])
                std  = statistics.stdev(seg_spds) if len(seg_spds) > 1 else 0.0
                sino = _sinuosity(latlons[i], latlons[j - 1], total_dist)
                if (total_gain >= _LIFT_MIN_GAIN and total_dur >= _LIFT_MIN_DUR
                        and (std < _LIFT_SPEED_STD or sino <= _LIFT_SINUOSITY)):
                    for k in range(i, j):
                        assisted[k] = True
                    i = j
                    continue
        i += 1
    return assisted


def _detect_assisted(per_pt: list, latlons: list) -> list[bool]:
    """Legacy combined detector: time-gap shuttles OR speed+sinuosity lifts."""
    tg  = _algo_time_gap(per_pt, latlons, [])
    spd = _algo_speed_sinuosity(per_pt, latlons, [])
    return [t or s for t, s in zip(tg, spd)]


def _algo_heuristic(per_pt: list, latlons: list, _osm: list) -> list[bool]:
    """Time-gap + speed+sinuosity combined (the original fallback)."""
    return _detect_assisted(per_pt, latlons)


def _detect_elev_rate_param(per_pt: list, threshold_m_per_min: float,
                            min_gain_m: float, min_dur_sec: float) -> list[bool]:
    """Parametrized elevation-rate detector. Finds sustained uphill stretches
    above `threshold_m_per_min`. Used directly for MTB (higher threshold to
    reject pedalling) and as the default ski/snowboard detector.
    """
    n = len(per_pt)
    assisted = [False] * n
    ele_prefix = _prefix_sum(per_pt, 'ele_delta')
    dt_prefix  = _prefix_sum(per_pt, 'dt')
    i = 1
    while i < n:
        j, gain, dur = i, 0.0, 0.0
        while j < n and dur < _ELEV_RATE_WIN_SEC:
            gain += per_pt[j]['ele_delta']
            dur  += per_pt[j]['dt']
            j += 1
        rate = (gain / dur * 60) if dur > 0 else 0
        if gain >= min_gain_m and rate >= threshold_m_per_min:
            while j < n:
                lg, ld, k = 0.0, 0.0, j
                while k < n and ld < 30:
                    lg += per_pt[k]['ele_delta']
                    ld += per_pt[k]['dt']
                    k  += 1
                if ld > 0 and (lg / ld * 60) >= threshold_m_per_min * 0.5:
                    j = k
                else:
                    break
            seg_gain = ele_prefix[j] - ele_prefix[i]
            seg_dur  = dt_prefix[j]  - dt_prefix[i]
            if seg_gain >= min_gain_m and seg_dur >= min_dur_sec:
                for k in range(i, j):
                    assisted[k] = True
                i = j
                continue
        i += 1
    return assisted


def _algo_elevation_rate(per_pt: list, latlons: list, _osm: list) -> list[bool]:
    """Default ski/snowboard uphill detector. Wrapper around the parametric
    form with 5 m/min threshold.
    """
    return _detect_elev_rate_param(
        per_pt, _ELEV_RATE_THRESHOLD, _ELEV_RATE_MIN_GAIN, _ELEV_RATE_MIN_DUR)


def _algo_osm(per_pt: list, latlons: list, osm_lifts: list) -> list[bool]:
    """OSM aerialway station proximity only."""
    if not osm_lifts:
        return [False] * len(per_pt)
    return _detect_station_lifts(latlons, per_pt, osm_lifts)


def _algo_combined(per_pt: list, latlons: list, osm_lifts: list) -> list[bool]:
    """OSM if available, else the heuristic fallback."""
    if osm_lifts:
        return _detect_station_lifts(latlons, per_pt, osm_lifts)
    return _detect_assisted(per_pt, latlons)


def _try_snap_to_osm(start: int, end: int, latlons: list, osm_lifts: list) -> tuple[int, int] | None:
    """Find the OSM lift whose stations best bracket a candidate segment."""
    n = len(latlons)
    b_range = range(max(0, start - _SMART_SNAP_WINDOW), min(n, start + _SMART_SNAP_WINDOW))
    t_range = range(max(0, end   - _SMART_SNAP_WINDOW), min(n, end   + _SMART_SNAP_WINDOW))
    best_match: tuple[int, int] | None = None
    best_score = float('inf')
    for lift in osm_lifts:
        for bottom, top in [(lift['a'], lift['b']), (lift['b'], lift['a'])]:
            db = {k: haversine(latlons[k], bottom) for k in b_range}
            dt = {k: haversine(latlons[k], top)    for k in t_range}
            cb = min(db, key=db.get)
            ct = min(dt, key=dt.get)
            if (db[cb] <= _SMART_STATION_THRESH_M
                    and dt[ct] <= _SMART_STATION_THRESH_M
                    and ct > cb):
                score = db[cb] + dt[ct]
                if score < best_score:
                    best_score = score
                    best_match = (cb, ct)
    return best_match


def _trim_segment_boundaries(start: int, end: int, per_pt: list) -> tuple[int, int]:
    """Advance the start / retreat the end to the FIRST point in each tail that
    is moving at cable speed (>= _LIFT_SPEED_MIN), so the flagged segment
    excludes slow loading / unloading zones at each end. If no fast point is
    found within the trim window, leaves the boundary unchanged."""
    trim = min(_SMART_TRIM_MAX, (end - start) // 4)
    new_start = start
    for k in range(start, start + trim):
        spd = per_pt[k].get('speed')
        if spd is not None and spd >= _LIFT_SPEED_MIN:
            new_start = k
            break
    new_end = end
    for k in range(end, end - trim, -1):
        spd = per_pt[k].get('speed')
        if spd is not None and spd >= _LIFT_SPEED_MIN:
            new_end = k
            break
    return new_start, new_end


def _trim_by_speed_minimum(start: int, end: int, per_pt: list) -> tuple[int, int]:
    """Trim boundaries at the speed minimum within a 30-point window of each end."""
    window = 30
    new_start = start
    best_spd = float('inf')
    for k in range(start, min(start + window, end)):
        spd = per_pt[k].get('speed')
        if spd is not None and spd < best_spd:
            best_spd = spd
            new_start = k
    new_end = end
    best_spd = float('inf')
    for k in range(max(end - window, new_start), end + 1):
        spd = per_pt[k].get('speed')
        if spd is not None and spd < best_spd:
            best_spd = spd
            new_end = k
    return new_start, new_end


def _algo_speed_osm(per_pt: list, latlons: list, osm_lifts: list) -> list[bool]:
    """Speed+Sinuosity detection with OSM boundary snapping."""
    n = len(per_pt)
    candidates = _algo_speed_sinuosity(per_pt, latlons, [])
    tg = _algo_time_gap(per_pt, latlons, [])
    candidates = [c or t for c, t in zip(candidates, tg)]
    lift_cands = [s for s in _build_segments(candidates) if s['type'] == 'assisted']
    final = [False] * n
    ele_prefix = _prefix_sum(per_pt, 'ele_delta')
    for seg in lift_cands:
        start, end = seg['start'], seg['end']
        seg_gain = ele_prefix[end + 1] - ele_prefix[start]
        if seg_gain < _LIFT_MIN_NET_GAIN:
            continue
        snapped = _try_snap_to_osm(start, end, latlons, osm_lifts) if osm_lifts else None
        if snapped:
            start, end = snapped
        else:
            start, end = _trim_segment_boundaries(start, end, per_pt)
        for k in range(start, end + 1):
            final[k] = True
    return final


def _algo_smart_combined(per_pt: list, latlons: list, osm_lifts: list) -> list[bool]:
    """Three-phase: union of elev-rate + speed+sinuosity + time-gap detection,
    then OSM boundary snap (250 m), then speed-inflection trim for unmatched."""
    n = len(per_pt)
    elev = _algo_elevation_rate(per_pt, latlons, [])
    spd  = _algo_speed_sinuosity(per_pt, latlons, [])
    tg   = _algo_time_gap(per_pt, latlons, [])
    candidates = [e or s or t for e, s, t in zip(elev, spd, tg)]
    lift_cands = [s for s in _build_segments(candidates) if s['type'] == 'assisted']
    final = [False] * n
    ele_prefix = _prefix_sum(per_pt, 'ele_delta')
    for seg in lift_cands:
        start, end = seg['start'], seg['end']
        seg_gain = ele_prefix[end + 1] - ele_prefix[start]
        if seg_gain < _LIFT_MIN_NET_GAIN:
            continue
        snapped = _try_snap_to_osm(start, end, latlons, osm_lifts) if osm_lifts else None
        if snapped:
            start, end = snapped
        else:
            start, end = _trim_segment_boundaries(start, end, per_pt)
        for k in range(start, end + 1):
            final[k] = True
    return final


def _detect_high_speed_shuttle(per_pt: list) -> list[bool]:
    """MTB shuttles: sustained high speed + uphill movement."""
    n = len(per_pt)
    assisted = [False] * n
    i = 1
    while i < n:
        j, gain, dur = i, 0.0, 0.0
        while j < n and dur < _SHUTTLE_WIN_SEC:
            gain += per_pt[j]['ele_delta']
            dur  += per_pt[j]['dt']
            j += 1
        spds = [p['speed'] for p in per_pt[i:j] if p['speed'] is not None]
        if dur >= _SHUTTLE_WIN_SEC * 0.5 and spds and gain >= _SHUTTLE_MIN_GAIN:
            avg = sum(spds) / len(spds)
            if avg >= _SHUTTLE_SPEED_MIN:
                while j < n:
                    rs = max(i, j - 30)
                    recent_spds = [p['speed'] for p in per_pt[rs:j] if p['speed'] is not None]
                    recent_gain = sum(p['ele_delta'] for p in per_pt[rs:j])
                    if (recent_spds and sum(recent_spds) / len(recent_spds) >= _SHUTTLE_SPEED_MIN
                            and recent_gain >= 0):
                        j += 1
                    else:
                        break
                for k in range(i, j):
                    assisted[k] = True
                i = j
                continue
        i += 1
    return assisted


def _algo_lift(per_pt: list, latlons: list, osm_lifts: list) -> list[bool]:
    """Default ski/snowboard algorithm: elev-rate ∪ time-gap → OSM snap → speed-min trim."""
    n = len(per_pt)
    elev = _detect_elev_rate_param(
        per_pt, _ELEV_RATE_THRESHOLD, _ELEV_RATE_MIN_GAIN, _ELEV_RATE_MIN_DUR)
    tg = _algo_time_gap(per_pt, latlons, [])
    candidates = [e or t for e, t in zip(elev, tg)]
    lift_cands = [s for s in _build_segments(candidates) if s['type'] == 'assisted']
    final = [False] * n
    ele_prefix = _prefix_sum(per_pt, 'ele_delta')
    for seg in lift_cands:
        start, end = seg['start'], seg['end']
        seg_gain = ele_prefix[end + 1] - ele_prefix[start]
        if seg_gain < _LIFT_MIN_NET_GAIN:
            continue
        snapped = _try_snap_to_osm(start, end, latlons, osm_lifts) if osm_lifts else None
        if snapped:
            start, end = snapped
        else:
            start, end = _trim_by_speed_minimum(start, end, per_pt)
        for k in range(start, end + 1):
            final[k] = True
    return final


def _algo_mtb(per_pt: list, latlons: list, osm_lifts: list) -> list[bool]:
    """MTB algorithm: elev-rate (high threshold) ∪ time-gap ∪ high-speed
    shuttle ∪ OSM, then OSM boundary snap (no trim on unmatched — shuttle
    starts/ends are usually already clean)."""
    n = len(per_pt)
    elev    = _detect_elev_rate_param(per_pt, _MTB_ELEV_THRESHOLD, _MTB_ELEV_MIN_GAIN, _ELEV_RATE_MIN_DUR)
    tg      = _algo_time_gap(per_pt, latlons, [])
    shuttle = _detect_high_speed_shuttle(per_pt)
    osm_f   = _algo_osm(per_pt, latlons, osm_lifts)
    candidates = [e or t or s or o for e, t, s, o in zip(elev, tg, shuttle, osm_f)]
    lift_cands = [s for s in _build_segments(candidates) if s['type'] == 'assisted']
    final = [False] * n
    ele_prefix = _prefix_sum(per_pt, 'ele_delta')
    for seg in lift_cands:
        start, end = seg['start'], seg['end']
        seg_gain = ele_prefix[end + 1] - ele_prefix[start]
        if seg_gain < _LIFT_MIN_NET_GAIN:
            continue
        snapped = _try_snap_to_osm(start, end, latlons, osm_lifts) if osm_lifts else None
        if snapped:
            start, end = snapped
        for k in range(start, end + 1):
            final[k] = True
    return final


DETECTION_ALGORITHMS: list[tuple] = [
    ("lift",       "Lift (default)",        "Elev-rate (5 m/min) + time-gap → OSM snap (250 m) → speed-min trim",  _algo_lift),
    ("mtb",        "MTB (shuttles + lifts)","Elev-rate (15 m/min) + time-gap + speed-shuttle + OSM → OSM snap",     _algo_mtb),
    ("smart",      "Smart Combined",        "Elev-rate + speed detection → OSM snap (250 m) → boundary trim",       _algo_smart_combined),
    ("speed_osm",  "Speed → OSM Snap",      "Speed+sinuosity detection → OSM snap (250 m) → boundary trim",         _algo_speed_osm),
    ("osm",        "OSM Station Proximity", "Match track to mapped aerialway station endpoints (100 m)",            _algo_osm),
    ("elev_rate",  "Elevation Rate",        "Sustained uphill movement (>=5 m/min for >=60 s)",                     _algo_elevation_rate),
    ("heuristic",  "Speed + Sinuosity",     "Consistent cable speed on a straight-line path",                       _algo_heuristic),
    ("time_gap",   "Time Gap Only",         "GPS dropout with elevation gain (GPS-off shuttles)",                   _algo_time_gap),
]


# Signature string for cache invalidation. Consumed by app.py to compute the
# CACHE_VERSION hash — bump when threshold values or algorithm logic changes.
ALGO_SIG = (
    f"v9-riding-avg,{_STATION_THRESH_M},{_LIFT_MIN_RIDE_SEC},{_LIFT_MAX_RIDE_SEC},{_LIFT_MIN_NET_GAIN},"
    f"{_ASSISTED_MIN_DT_SEC},{_ASSISTED_MIN_GAIN_M},"
    f"{_LIFT_SPEED_MIN},{_LIFT_SPEED_MAX},{_LIFT_SPEED_STD},"
    f"{_LIFT_SINUOSITY},{_LIFT_WIN_GAIN},{_LIFT_MIN_GAIN},{_LIFT_MIN_DUR},"
    f"{_ELEV_RATE_THRESHOLD},{_ELEV_RATE_MIN_GAIN},{_ELEV_RATE_MIN_DUR},"
    f"{_SMART_STATION_THRESH_M},"
    f"{_SHUTTLE_SPEED_MIN},{_SHUTTLE_WIN_SEC},{_SHUTTLE_MIN_GAIN},"
    f"{_MTB_ELEV_THRESHOLD},{_MTB_ELEV_MIN_GAIN}"
)
