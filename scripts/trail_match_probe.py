"""Probe: match GPX points to named OSM trails.

Usage:
    python scripts/trail_match_probe.py [cache_file.json]

Default: cache/gpx/strava_14441276001.gpx.json (May 10 2025 Moose Mountain).

Fetches named OSM ways (highway=path|track|footway|cycleway|bridleway with
name=*) in the activity's bbox, snaps each GPS point to the nearest way
within SNAP_THRESHOLD_M, and prints (a) an ordered timeline of trails
ridden and (b) per-trail coverage.

OSM result is cached to cache/osm_paths/<bbox_hash>.json so re-runs are free.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / "cache" / "gpx" / "strava_14441276001.gpx.json"
OSM_CACHE_DIR = ROOT / "cache" / "osm_paths"
SNAP_THRESHOLD_M = 12.0
MIN_RUN_POINTS = 4  # collapse very short groups (GPS jitter at junctions)
BBOX_PAD_DEG = 0.0002
VISIT_GAP_SEC = 600  # same-name runs within this gap merge into one visit


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def to_local(lat, lon, lat0, lon0):
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * 6371000.0
    y = math.radians(lat - lat0) * 6371000.0
    return x, y


def point_segment_dist(px, py, ax, ay, bx, by):
    dx = bx - ax
    dy = by - ay
    seg_sq = dx * dx + dy * dy
    if seg_sq == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_sq))
    cx = ax + t * dx
    cy = ay + t * dy
    return math.hypot(px - cx, py - cy)


def fetch_osm_paths(bbox):
    south, west, north, east = bbox
    key = hashlib.md5(f"{south:.5f},{west:.5f},{north:.5f},{east:.5f}".encode()).hexdigest()[:12]
    OSM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = OSM_CACHE_DIR / f"{key}.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))

    query = f"""
[out:json][timeout:90];
(
  way["highway"~"^(path|track|footway|cycleway|bridleway)$"]["name"]({south},{west},{north},{east});
);
(._;>;);
out body;
"""
    print(f"Fetching OSM (one-time, ~30s)...", file=sys.stderr)
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(
        "https://overpass-api.de/api/interpreter",
        data=data,
        headers={"User-Agent": "AlForks/trail_match_probe (personal use)"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        result = json.load(r)
    cached.write_text(json.dumps(result), encoding="utf-8")
    return result


def parse_ways(osm_json):
    nodes = {e["id"]: (e["lat"], e["lon"]) for e in osm_json["elements"] if e["type"] == "node"}
    ways = []
    for e in osm_json["elements"]:
        if e["type"] != "way":
            continue
        tags = e.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        coords = [nodes[nid] for nid in e["nodes"] if nid in nodes]
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


def way_length_km(coords):
    total = 0.0
    for i in range(1, len(coords)):
        total += haversine_m(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1])
    return total / 1000.0


def main():
    cache_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CACHE
    data = json.loads(cache_path.read_text(encoding="utf-8"))["data"]
    bbox = data["bbox"]
    stats = data["stats"]
    print(f"Activity: {data['name']}  ({data['date'][:10]})")
    print(f"  {stats['distance_km']} km, {stats['duration_sec'] // 60} min, peak {stats['peak_ele_m']} m")
    print(f"  Bbox: {bbox}")
    print()

    osm = fetch_osm_paths(bbox)
    ways = parse_ways(osm)
    print(f"OSM: {len(ways)} named ways in bbox")

    lat0 = (bbox[0] + bbox[2]) / 2
    lon0 = (bbox[1] + bbox[3]) / 2
    for w in ways:
        w["local"] = [to_local(c[0], c[1], lat0, lon0) for c in w["coords"]]
        lats = [c[0] for c in w["coords"]]
        lons = [c[1] for c in w["coords"]]
        w["bb"] = (min(lats), min(lons), max(lats), max(lons))
        w["length_km"] = way_length_km(w["coords"])

    points = data["points"]
    print(f"Snapping {len(points)} points (threshold {SNAP_THRESHOLD_M} m)...")
    t0 = time.time()

    matches = []
    for p in points:
        plat, plon = p["lat"], p["lon"]
        px, py = to_local(plat, plon, lat0, lon0)
        best_id, best_name, best_dist = None, None, SNAP_THRESHOLD_M
        for w in ways:
            wb = w["bb"]
            if (plat < wb[0] - BBOX_PAD_DEG or plat > wb[2] + BBOX_PAD_DEG
                    or plon < wb[1] - BBOX_PAD_DEG or plon > wb[3] + BBOX_PAD_DEG):
                continue
            local = w["local"]
            for j in range(1, len(local)):
                ax, ay = local[j - 1]
                bx, by = local[j]
                d = point_segment_dist(px, py, ax, ay, bx, by)
                if d < best_dist:
                    best_dist = d
                    best_id = w["id"]
                    best_name = w["name"]
        matches.append((best_id, best_name, best_dist))

    elapsed = time.time() - t0
    matched = sum(1 for m in matches if m[0])
    pct = 100 * matched / len(matches) if matches else 0
    print(f"  done in {elapsed:.1f}s  ({matched}/{len(matches)} matched, {pct:.0f}%)")
    print()

    # Group consecutive matches by NAME (collapses OSM way fragmentation
    # where one trail is split across multiple ways at intersections).
    runs = []
    cur_name, cur_start = matches[0][1], 0
    for i in range(1, len(matches)):
        wname = matches[i][1]
        if wname != cur_name:
            runs.append((cur_name, cur_start, i))
            cur_name, cur_start = wname, i
    runs.append((cur_name, cur_start, len(matches)))

    # Drop short runs (GPS jitter / brief mis-snaps at junctions)
    runs = [r for r in runs if r[2] - r[1] >= MIN_RUN_POINTS]

    # Parse timestamps for visit-gap merging
    def t_of(idx):
        return points[idx]["time"]

    def secs_between(end_iso, start_iso):
        # crude: both are local ISO with offset; compare HH:MM:SS within same day
        from datetime import datetime
        return (datetime.fromisoformat(start_iso) - datetime.fromisoformat(end_iso)).total_seconds()

    print("Timeline (runs >= {} pts):".format(MIN_RUN_POINTS))
    print(f"{'Time':>6}  {'Pts':>5}  {'Dist':>7}  Trail")
    print("-" * 70)
    for name, s, e in runs:
        d_km = points[e - 1]["dist_km"] - points[s]["dist_km"]
        t_start = points[s]["time"][11:16]
        label = name if name else "-- (unmatched)"
        print(f"{t_start:>6}  {e - s:>5}  {d_km:>6.2f}k  {label}")
    print()

    # Per-trail summary, grouped by name, with visit-gap merging
    # Sum OSM length across all ways sharing the name (so coverage means
    # "fraction of the full trail ridden", regardless of how OSM split it).
    name_length_km = {}
    for w in ways:
        name_length_km[w["name"]] = name_length_km.get(w["name"], 0.0) + w["length_km"]

    by_name = {}
    for name, s, e in runs:
        if not name:
            continue
        d_km = points[e - 1]["dist_km"] - points[s]["dist_km"]
        rec = by_name.setdefault(name, {"ridden_km": 0.0, "runs": []})
        rec["ridden_km"] += d_km
        rec["runs"].append((s, e))

    # Merge runs with small time gaps into visits
    for name, rec in by_name.items():
        rec["runs"].sort()
        visits = 1
        for i in range(1, len(rec["runs"])):
            prev_end_idx = rec["runs"][i - 1][1] - 1
            cur_start_idx = rec["runs"][i][0]
            gap = secs_between(points[prev_end_idx]["time"], points[cur_start_idx]["time"])
            if gap > VISIT_GAP_SEC:
                visits += 1
        rec["visits"] = visits

    print("Per-trail coverage (aggregated by name):")
    print(f"{'Trail':<32} {'Visits':>7} {'Ridden':>10} {'OSM len':>10} {'Cov':>5}")
    print("-" * 70)
    for name, info in sorted(by_name.items(), key=lambda x: -x[1]["ridden_km"]):
        wl = name_length_km.get(name, 0)
        cov = 100 * info["ridden_km"] / wl if wl > 0 else 0
        print(f"{name[:32]:<32} {info['visits']:>7} {info['ridden_km']:>8.2f}km {wl:>8.2f}km {cov:>4.0f}%")


if __name__ == "__main__":
    main()
