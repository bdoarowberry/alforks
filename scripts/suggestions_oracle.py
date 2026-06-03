"""Throwaway real-cache oracle for route_suggestions (commit 3 de-risk).

Assembles RideMeta from the sidebar + GPX caches, runs cluster_rides over
the real library, and checks the documented canaries:
  * Cox Hill Large Loop: the 3 same-loop rides cluster together.
  * Elbow Loop: 2024-08-03 + 2025-06-15 cluster; 2020-08-15 stays OUT.

Run: python scripts/suggestions_oracle.py
"""
import json
import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import route_suggestions as rs

GPX = os.path.join("cache", "gpx")
SIDEBAR = os.path.join("cache", "sidebar")


def load_points(fn):
    p = os.path.join(GPX, fn + ".json")
    if not os.path.exists(p):
        return None
    return json.load(open(p))["data"]["points"]


def load_bbox(fn):
    p = os.path.join(GPX, fn + ".json")
    if not os.path.exists(p):
        return None
    return json.load(open(p))["data"].get("bbox")


def build_rides():
    rides = []
    for sf in glob.glob(os.path.join(SIDEBAR, "*.json")):
        e = json.load(open(sf)).get("entry") or {}
        if e.get("effective_type") != "mtb" or e.get("excluded"):
            continue
        fn = e.get("filename")
        bbox = load_bbox(fn)
        dist = (e.get("stats") or {}).get("distance_km")
        if not fn or not bbox or not dist:
            continue
        rides.append({
            "filename": fn, "regions": e.get("regions") or [],
            "bbox": bbox, "distance_km": dist,
            "date": (e.get("date") or "")[:10],
        })
    return rides


def main():
    rides = build_rides()
    print(f"MTB rides with bbox+dist: {len(rides)}")

    _cache = {}

    def loader(fn):
        if fn not in _cache:
            pts = load_points(fn)
            _cache[fn] = rs.ride_cell_set(pts) if pts else frozenset()
        return _cache[fn]

    clusters = rs.cluster_rides(rides, loader)
    print(f"cell sets loaded (prefiltered): {len(_cache)} / {len(rides)}")
    print(f"clusters (>=2): {len(clusters)}  sizes: {sorted((c['size'] for c in clusters), reverse=True)}")

    def find_cluster(fn):
        for c in clusters:
            if fn in c["members"]:
                return c
        return None

    cox = ["strava_6215078907.gpx", "6605091066.gpx", "strava_9817819809.gpx"]
    print("\n=== Cox Hill canary (all 3 should share one cluster) ===")
    cc = [find_cluster(f) for f in cox]
    for f, c in zip(cox, cc):
        print(f"  {f:28} -> {'cluster size '+str(c['size'])+' ('+c['representative']+')' if c else 'NOT CLUSTERED'}")
    same = cc[0] is not None and all(c is cc[0] for c in cc)
    print(f"  PASS: all three in one cluster" if same else "  FAIL")

    # Elbow Loop: positives from the route's attempts, negative = the
    # 2020-08-15 ride in the Elbow region (cdddc75c).
    elbow_neg = [r["filename"] for r in rides
                 if r["date"] == "2020-08-15" and "cdddc75c" in (r["regions"] or [])]
    print("\n=== Elbow Loop canary ===")
    print(f"  2020-08-15 Elbow-region rides found: {elbow_neg}")
    try:
        att = json.load(open("cache/route_attempts/313e8cf9577f.json"))
        payload = att.get("payload") or att
        pos = [(a.get("date"), a.get("filename")) for a in (payload.get("attempts") or [])]
        print(f"  saved-route attempts (positives): {pos}")
        for _, f in pos:
            c = find_cluster(f)
            print(f"    {f:28} -> {'cluster size '+str(c['size']) if c else 'not clustered'}")
    except FileNotFoundError:
        print("  (no attempts cache for 313e8cf9577f)")
    for f in elbow_neg:
        c = find_cluster(f)
        print(f"    NEG {f:24} -> {'IN cluster size '+str(c['size'])+' (BAD)' if c else 'not clustered (GOOD)'}")


if __name__ == "__main__":
    main()
