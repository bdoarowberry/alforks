"""Validate the region trails artifact for a given region.

Usage:
    python scripts/dump_region_trails.py "Moose Mountain"
    python scripts/dump_region_trails.py d5662439           # by id
    python scripts/dump_region_trails.py "Moose Mountain" --force

Prints summary stats + writes the artifact to cache/region_trails/<id>.json.
The companion scratch HTML at scripts/region_preview.html will render it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import route_builder

REGIONS_FILE      = ROOT / "regions.json"
ARTIFACTS_DIR     = ROOT / "cache" / "region_trails"
OSM_PATHS_DIR     = ROOT / "cache" / "osm_paths"
OSM_ROADS_DIR     = ROOT / "cache" / "osm_roads"


def find_region(needle: str) -> dict | None:
    regions = json.loads(REGIONS_FILE.read_text(encoding="utf-8"))
    for r in regions:
        if r.get("id") == needle:
            return r
    needle_l = needle.lower()
    for r in regions:
        if r.get("name", "").lower() == needle_l:
            return r
    for r in regions:
        if needle_l in r.get("name", "").lower():
            return r
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("region", help="region id or name")
    ap.add_argument("--force", action="store_true", help="ignore cached artifact")
    args = ap.parse_args()

    region = find_region(args.region)
    if not region:
        print(f"No region matched {args.region!r}", file=sys.stderr)
        sys.exit(1)

    print(f"Region: {region['name']} ({region['id']})")
    art = route_builder.get_region_artifact(
        region,
        artifacts_dir=ARTIFACTS_DIR,
        osm_paths_dir=OSM_PATHS_DIR,
        osm_roads_dir=OSM_ROADS_DIR,
        force_rebuild=args.force,
    )

    bbox = art.get("bbox")
    print(f"  bbox: {bbox}")
    print(f"  trails: {len(art['trails'])}")
    print(f"  roads:  {len(art['roads'])}")
    print(f"  junctions: {len(art['junctions'])}")

    if art["trails"]:
        print("\nTop 10 trails by length:")
        top = sorted(art["trails"], key=lambda t: -t["total_length_m"])[:10]
        for t in top:
            ncomp = len(t["components"])
            comp_note = f" ({ncomp} components)" if ncomp > 1 else ""
            print(f"  {t['total_length_m']/1000:6.2f} km  {t['name']:<30}"
                  f"  highway={t['highway']}{comp_note}")

    if art["junctions"]:
        print(f"\nFirst 10 junctions:")
        for j in art["junctions"][:10]:
            print(f"  {j['id']}  ({j['lat']:.5f}, {j['lon']:.5f})  "
                  f"trails: {', '.join(j['trails'])}")

    out = ARTIFACTS_DIR / f"{region['id']}.json"
    print(f"\nArtifact written: {out}")


if __name__ == "__main__":
    main()
