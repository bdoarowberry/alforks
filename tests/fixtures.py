"""Synthetic per-point data for detection tests.

Each `per_pt` is a list of transitions (one per GPX point) with keys:
    dt, dist, speed, ele_delta
and `latlons` is the parallel list of (lat, lon) tuples.

Keeping fixtures programmatic rather than using real GPX files avoids
parsing/gpxpy and makes edge cases (perfect lift profile, pure descent)
easy to reason about.
"""

from __future__ import annotations


def _make(segments: list[dict]) -> tuple[list, list]:
    """Build (per_pt, latlons) from a list of segment dicts.

    Each segment: {'n': point_count, 'dt': seconds_per_point,
                   'dist': metres_per_point, 'speed': kmh,
                   'ele_delta': metres_per_point, 'lat': starting_lat,
                   'lon': starting_lon, 'dlat': per_point_lat_step,
                   'dlon': per_point_lon_step}
    """
    per_pt = [{'dt': 0, 'dist': 0.0, 'speed': None, 'ele_delta': 0.0}]
    latlons = []
    cur_lat = cur_lon = 0.0
    if segments:
        cur_lat = segments[0].get('lat', 0.0)
        cur_lon = segments[0].get('lon', 0.0)
    latlons.append((cur_lat, cur_lon))
    for seg in segments:
        cur_lat = seg.get('lat', cur_lat)
        cur_lon = seg.get('lon', cur_lon)
        for _ in range(seg['n']):
            cur_lat += seg.get('dlat', 0.0)
            cur_lon += seg.get('dlon', 0.0)
            latlons.append((cur_lat, cur_lon))
            per_pt.append({
                'dt':        seg['dt'],
                'dist':      seg['dist'],
                'speed':     seg['speed'],
                'ele_delta': seg['ele_delta'],
            })
    return per_pt, latlons


def ski_day_with_lift():
    """A ski day: one clear lift up + one descent. Elev-rate should flag the lift."""
    return _make([
        # 120 pts, 1 s each, ~15 km/h straight-line, gaining 8 m per point (480 m/min? — no, 8m/s*60/10=...)
        # 8 m per 1 s = 480 m/min, way above threshold. total gain = 120*8 = 960 m (plenty).
        {'n': 120, 'dt': 1.0, 'dist': 4.2, 'speed': 15.0, 'ele_delta': 8.0,
         'lat': 50.0, 'lon': -116.0, 'dlat': 0.00005, 'dlon': 0.00005},
        # 100 pts descent (20 km/h), losing 5 m each
        {'n': 100, 'dt': 1.0, 'dist': 5.5, 'speed': 20.0, 'ele_delta': -5.0,
         'dlat': -0.00004, 'dlon': 0.00006},
    ])


def pure_descent():
    """Pure downhill ski run — nothing should flag as assisted."""
    return _make([
        {'n': 200, 'dt': 1.0, 'dist': 5.5, 'speed': 20.0, 'ele_delta': -4.0,
         'lat': 50.0, 'lon': -116.0, 'dlat': -0.00004, 'dlon': 0.00006},
    ])


def time_gap_shuttle():
    """GPS-off shuttle: a single long-dt point with big elev gain, bordered
    by normal recording. Time-gap algo should flag index 201."""
    before, _ = _make([
        {'n': 200, 'dt': 1.0, 'dist': 2.0, 'speed': 8.0, 'ele_delta': 0.0,
         'lat': 50.0, 'lon': -116.0},
    ])
    # Inject one transition with dt=300s, ele_delta=150m (shuttle)
    before.append({'dt': 300, 'dist': 0.0, 'speed': None, 'ele_delta': 150.0})
    after, _ = _make([
        {'n': 200, 'dt': 1.0, 'dist': 5.0, 'speed': 18.0, 'ele_delta': -3.0},
    ])
    # Drop the leading zero-transition from `after`
    per_pt = before + after[1:]
    latlons = [(50.0, -116.0)] * len(per_pt)
    return per_pt, latlons
