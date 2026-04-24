"""Profile parse_gpx and detection hot paths against a real GPX file.

Usage:
    python scripts/profile_parse.py [filename.gpx]

Picks the largest file in tracks/ by default. Reports per-phase timings
averaged over --runs iterations (default 3).

Timings cover:
  - gpxpy.parse (XML → object tree)
  - per-point loop (haversine, speed, ele_delta)
  - _median_filter over speeds
  - bbox passes (3 list comprehensions over points)
  - _algo_lift (detection, includes _detect_station_lifts if OSM lifts loaded)
  - stats loop (the final per-point accumulation)
  - total parse_gpx wall-clock
  - _write_disk_cache serialize (json.dumps of the result)
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

# Make project root importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gpxpy                                        # noqa: E402
from detection import (                             # noqa: E402
    ALGO_SIG,
    _algo_lift,
    _median_filter,
    haversine,
)


def _largest_gpx() -> Path:
    tracks = ROOT / "tracks"
    files = sorted(tracks.glob("*.gpx"), key=lambda f: f.stat().st_size, reverse=True)
    if not files:
        sys.exit(f"No GPX files found in {tracks}")
    return files[0]


def _profile_once(path: Path) -> dict:
    splits: dict = {}

    t = time.perf_counter()
    with open(path, encoding="utf-8") as f:
        gpx = gpxpy.parse(f)
    splits["gpxpy_parse"] = time.perf_counter() - t

    t = time.perf_counter()
    raw = [pt for track in gpx.tracks for seg in track.segments for pt in seg.points]
    splits["flatten_points"] = time.perf_counter() - t

    n_points = len(raw)

    # Per-point loop — haversine, dt, speed
    t = time.perf_counter()
    per_pt = [{"dt": 0, "dist": 0.0, "speed": None, "ele_delta": 0.0}]
    for i in range(1, len(raw)):
        prev, curr = raw[i - 1], raw[i]
        d   = haversine((prev.latitude, prev.longitude), (curr.latitude, curr.longitude))
        dt  = (curr.time - prev.time).total_seconds() if prev.time and curr.time else 0
        if dt < 0:
            dt, d = 0, 0.0
        spd = None
        if dt > 0 and d > 0:
            spd = (d / dt) * 3.6
            if spd > 150:
                spd = None
        ele_d = ((curr.elevation - prev.elevation)
                 if curr.elevation is not None and prev.elevation is not None else 0.0)
        per_pt.append({"dt": dt, "dist": d, "speed": spd, "ele_delta": ele_d})
    splits["per_point_loop"] = time.perf_counter() - t

    # Median filter
    t = time.perf_counter()
    smooth = _median_filter([p["speed"] for p in per_pt], k=5)
    for i, s in enumerate(smooth):
        per_pt[i]["speed"] = s
    splits["median_filter"] = time.perf_counter() - t

    # bbox passes
    t = time.perf_counter()
    latlons = [(pt.latitude, pt.longitude) for pt in raw]
    lats_raw = [pt.latitude for pt in raw]
    lons_raw = [pt.longitude for pt in raw]
    raw_bbox = (min(lats_raw), min(lons_raw), max(lats_raw), max(lons_raw))
    splits["bbox_comprehensions"] = time.perf_counter() - t

    # Detection — pass empty osm_lifts so we measure the pure-Python detection
    # cost without a variable OSM fetch in the middle.
    t = time.perf_counter()
    is_assisted = _algo_lift(per_pt, latlons, [])
    splits["algo_lift_no_osm"] = time.perf_counter() - t

    # Stats accumulation loop
    t = time.perf_counter()
    total_dist = riding_dist = 0.0
    elev_gain = elev_loss = assisted_gain = max_speed = 0.0
    riding_dur_sec = 0.0
    points = []
    for i, pt in enumerate(raw):
        if i > 0:
            p = per_pt[i]
            total_dist += p["dist"]
            if is_assisted[i]:
                if p["ele_delta"] > 0:
                    assisted_gain += p["ele_delta"]
            else:
                riding_dist += p["dist"]
                riding_dur_sec += p["dt"]
                if p["ele_delta"] > 0:
                    elev_gain += p["ele_delta"]
                elif p["ele_delta"] < 0:
                    elev_loss += abs(p["ele_delta"])
            if p["speed"] is not None and p["speed"] > max_speed:
                max_speed = p["speed"]
        points.append({
            "lat":     pt.latitude,
            "lon":     pt.longitude,
            "ele":     round(pt.elevation, 1) if pt.elevation is not None else None,
            "time":    pt.time.isoformat() if pt.time else None,
            "dist_km": round(total_dist / 1000, 3),
            "speed":   round(per_pt[i]["speed"], 1) if per_pt[i]["speed"] is not None else None,
        })
    splits["stats_loop"] = time.perf_counter() - t

    # Simulate the full result dict then serialise to mimic _write_disk_cache
    result = {
        "filename": path.name,
        "name":     path.stem,
        "bbox":     raw_bbox,
        "points":   points,
        "is_assisted_count": sum(is_assisted),
    }

    t = time.perf_counter()
    payload = json.dumps({"version": ALGO_SIG, "data": result}, ensure_ascii=False)
    splits["json_dumps"] = time.perf_counter() - t
    splits["json_size_bytes"] = len(payload)

    splits["n_points"] = n_points
    splits["total"] = sum(v for k, v in splits.items()
                          if k not in ("n_points", "json_size_bytes"))
    return splits


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("file", nargs="?", help="GPX file (default: largest in tracks/)")
    p.add_argument("--runs", type=int, default=3)
    args = p.parse_args()

    path = Path(args.file) if args.file else _largest_gpx()
    if not path.is_absolute():
        path = (ROOT / path).resolve() if not path.exists() else path.resolve()
    if not path.exists():
        sys.exit(f"Not found: {path}")

    print(f"Profiling: {path.name} ({path.stat().st_size / 1024:.0f} KB), "
          f"{args.runs} runs")

    runs = [_profile_once(path) for _ in range(args.runs)]
    keys = [k for k in runs[0] if k not in ("n_points", "json_size_bytes")]

    print(f"\nPoints: {runs[0]['n_points']:,}  "
          f"JSON size: {runs[0]['json_size_bytes'] / 1024:.0f} KB\n")

    print(f"{'phase':<25} {'mean (ms)':>10} {'min (ms)':>10} {'max (ms)':>10} {'% of total':>12}")
    print("-" * 70)
    total_means = [r["total"] for r in runs]
    grand_total = statistics.mean(total_means) * 1000
    for k in keys:
        vals = [r[k] * 1000 for r in runs]
        mean = statistics.mean(vals)
        pct = (mean / grand_total * 100) if grand_total > 0 else 0
        marker = "  <-- total" if k == "total" else ""
        print(f"{k:<25} {mean:>10.2f} {min(vals):>10.2f} {max(vals):>10.2f} {pct:>11.1f}%{marker}")


if __name__ == "__main__":
    main()
