"""Suggest routes the rider has done more than once.

Auto-discover groups of *geometrically similar* rides from the library so
the rider can save a recurring loop as a route with one click. Unlike
`route_attempts` (which matches a ride against an already-saved route by
trail-name sequence), this module compares the raw GPS footprints of
rides directly, so it needs no OSM trail-name canonicalization and is
naturally invariant to ride direction and start point.

The pipeline is two-stage to avoid an O(n^2) blow-up over ~800 rides:

  1. Prefilter cheap signals already in the sidebar/GPX cache — same
     region bucket, overlapping bounding box, total distance within a
     tolerance — to reject the vast majority of pairs before any
     point-level work.
  2. Reduce each surviving ride to the SET of ~60 m grid cells its track
     occupies, then score pairs by cell-set overlap. Cluster the
     resulting similarity graph (union-find); a connected component of
     >= MIN_CLUSTER_SIZE rides not already covered by a saved route is a
     suggestion.

This module is Flask-agnostic (only the stdlib + typing). app.py wires
the loaders, caching, and HTTP layer; the geometry here stays unit-test
friendly with synthetic cell sets.
"""
from __future__ import annotations

import math
from typing import Callable, Iterable

# Bump to invalidate cache/route_suggestions/clusters.json when the
# clustering math or tuning changes.
SUGGESTIONS_VERSION = 1

# ── Tuning knobs (the canary oracle pins these; see tests) ────────────
# Grid cell edge in meters. GPS noise is ~5-15 m, so a cell comfortably
# larger than the noise absorbs jitter while staying fine enough that two
# genuinely different trails land in different cells.
GRID_CELL_M = 60.0

# Reference latitude for the lon->meters scale. It MUST be a fixed
# constant (not each point's own latitude): the grid has to be identical
# for every ride so two rides' cell indices are comparable, and a
# per-point cos(lat) would couple the axes — at this region's ~-114 deg
# longitude, a small latitude change swings the east coordinate by
# hundreds of meters, smearing a constant-longitude trail across many
# east cells. All rides sit at ~50-51 N (Calgary / Kananaskis); cos
# varies <2% across that band, so a single reference keeps cells
# near-square everywhere they're used.
_REF_LAT_DEG = 50.7
_REF_COS = math.cos(math.radians(_REF_LAT_DEG))

# Keep every Nth point before rounding to cells. At ~11 m point spacing a
# stride of 3 leaves ~33 m between kept points (< one cell), so no cell on
# a continuous track is skipped, while cutting rounding work ~3x. Sparser
# "smart recordings" stay safe because cell-set overlap tolerates the odd
# gap along a line.
CELL_STRIDE = 3

# Prefilter gates.
PREFILTER_DIST_FRAC = 0.20   # total ride distance must match within +-20%
BBOX_MIN_OVERLAP = 0.30      # min IoU of the two rides' bounding boxes

# A graph edge is drawn between two rides when their cell-set similarity
# reaches this. Containment (intersection / smaller set) is used rather
# than Jaccard so a trimmed or sparsely-recorded ride of the same loop
# still matches its fuller twin; the distance gate above prevents
# containment from over-merging a short ride that merely sits inside a
# longer ride's footprint.
SIM_THRESHOLD = 0.55
SIM_METRIC = "containment"   # "containment" | "jaccard"

MIN_CLUSTER_SIZE = 2


# ── Geometry primitives ───────────────────────────────────────────────
def _cell_key(lat: float, lon: float, cell_m: float = GRID_CELL_M) -> tuple[int, int]:
    """Round a coordinate onto a global metric grid, returning the integer
    cell index `(north, east)`. Longitude is scaled by a fixed reference
    cos(lat) (see `_REF_COS`) so the grid is identical for every ride and
    cells stay ~square at this latitude band."""
    m_north = lat * 111_320.0
    m_east = lon * 111_320.0 * _REF_COS
    return (int(math.floor(m_north / cell_m)), int(math.floor(m_east / cell_m)))


def ride_cell_set(
    points: Iterable[dict],
    cell_m: float = GRID_CELL_M,
    stride: int = CELL_STRIDE,
) -> frozenset[tuple[int, int]]:
    """Reduce a ride's points to the set of grid cells it occupies.

    `points` are dicts with `lat`/`lon`. Points lacking either are
    skipped. Striding only affects how many points are rounded, never
    correctness of the resulting set for a continuous track."""
    if stride < 1:
        stride = 1
    cells: set[tuple[int, int]] = set()
    pts = list(points)
    for i in range(0, len(pts), stride):
        p = pts[i]
        lat = p.get("lat")
        lon = p.get("lon")
        if lat is None or lon is None:
            continue
        cells.add(_cell_key(lat, lon, cell_m))
    return frozenset(cells)


def bbox_iou(a: list[float], b: list[float]) -> float:
    """Intersection-over-union of two `[min_lat, min_lon, max_lat,
    max_lon]` boxes, treating degrees as planar (fine for the small,
    nearby boxes this compares). 0.0 if either box is degenerate or they
    don't overlap."""
    if not a or not b:
        return 0.0
    inter_lat = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    inter_lon = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = inter_lat * inter_lon
    if inter <= 0.0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def dist_within(d1: float, d2: float, frac: float = PREFILTER_DIST_FRAC) -> bool:
    """True if two total distances agree within `frac` of the larger.
    Missing/zero distances fail closed (return False)."""
    if not d1 or not d2:
        return False
    return abs(d1 - d2) <= frac * max(d1, d2)


def cell_similarity(
    a: frozenset[tuple[int, int]],
    b: frozenset[tuple[int, int]],
    metric: str = SIM_METRIC,
) -> float:
    """Overlap of two cell sets. `containment` = |A&B| / min(|A|,|B|);
    `jaccard` = |A&B| / |A|B|. Empty input -> 0.0."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    if metric == "jaccard":
        denom = len(a | b)
    else:  # containment
        denom = min(len(a), len(b))
    return inter / denom if denom else 0.0


# ── Connected-components clustering ───────────────────────────────────
class _UF:
    """Minimal union-find over hashable items, with `groups()` returning
    the members of each component with >1 element first established by
    insertion order."""

    def __init__(self) -> None:
        self._parent: dict = {}

    def add(self, x) -> None:
        self._parent.setdefault(x, x)

    def find(self, x):
        self.add(x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression.
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def groups(self) -> list[list]:
        comps: dict = {}
        for x in self._parent:
            comps.setdefault(self.find(x), []).append(x)
        return list(comps.values())


# ── Pipeline ──────────────────────────────────────────────────────────
def cluster_rides(
    rides: list[dict],
    cell_set_loader: Callable[[str], "frozenset | None"],
    *,
    cell_m: float = GRID_CELL_M,
    bbox_min_overlap: float = BBOX_MIN_OVERLAP,
    dist_frac: float = PREFILTER_DIST_FRAC,
    sim_threshold: float = SIM_THRESHOLD,
    sim_metric: str = SIM_METRIC,
    min_cluster_size: int = MIN_CLUSTER_SIZE,
) -> list[dict]:
    """Group geometrically-similar rides.

    `rides` is per-ride metadata dicts: `{filename, regions:[id,...],
    bbox, distance_km, date}`. `cell_set_loader(filename)` returns that
    ride's cell set (a frozenset) and is called ONLY for rides that
    survive the cheap prefilter — so the expensive point load happens for
    a small fraction of the library.

    Returns a list of cluster dicts:
        {region_id, members:[fn,...], size, representative,
         representative_cells}
    sorted largest-first. Saved-route exclusion is applied separately by
    the caller via `cluster_covered_by_route` (it needs route geometry
    this layer doesn't have)."""
    by_fn = {r["filename"]: r for r in rides}

    # Bucket by region so pairing is O(rides_per_region^2), not O(all^2).
    # A multi-region ride joins every one of its buckets; the union-find
    # below is global, so a cross-bucket match still merges correctly.
    buckets: dict = {}
    for r in rides:
        for rid in (r.get("regions") or [None]):
            buckets.setdefault(rid, []).append(r["filename"])

    # Stage 1: candidate pairs that clear the bbox + distance gates.
    candidate_pairs: set = set()
    for fns in buckets.values():
        for i in range(len(fns)):
            for j in range(i + 1, len(fns)):
                a, b = fns[i], fns[j]
                ra, rb = by_fn[a], by_fn[b]
                if not dist_within(ra.get("distance_km"), rb.get("distance_km"), dist_frac):
                    continue
                if bbox_iou(ra.get("bbox") or [], rb.get("bbox") or []) < bbox_min_overlap:
                    continue
                candidate_pairs.add((a, b) if a < b else (b, a))

    if not candidate_pairs:
        return []

    # Stage 2: load cell sets only for rides that appear in a candidate pair.
    involved: set = set()
    for a, b in candidate_pairs:
        involved.add(a)
        involved.add(b)
    cells: dict = {}
    for fn in involved:
        cs = cell_set_loader(fn)
        cells[fn] = cs if cs else frozenset()

    # Stage 3: score each candidate pair; similar pairs draw a graph edge.
    uf = _UF()
    for fn in involved:
        uf.add(fn)
    for a, b in candidate_pairs:
        if cell_similarity(cells[a], cells[b], sim_metric) >= sim_threshold:
            uf.union(a, b)

    # Stage 4: connected components of >= min_cluster_size become clusters.
    clusters: list = []
    for group in uf.groups():
        if len(group) < min_cluster_size:
            continue
        members = sorted(group)
        # Representative = most-fully-recorded footprint (largest cell
        # set), tie-broken by newest ride.
        rep = max(members, key=lambda fn: (len(cells[fn]), by_fn[fn].get("date") or ""))
        rep_regions = by_fn[rep].get("regions") or []
        clusters.append({
            "region_id": rep_regions[0] if rep_regions else None,
            "members": members,
            "size": len(members),
            "representative": rep,
            "representative_cells": cells[rep],
        })

    clusters.sort(key=lambda c: (-c["size"], c["representative"]))
    return clusters


def cluster_covered_by_route(
    representative_cells: "frozenset",
    route_cellsets: Iterable["frozenset"],
    *,
    sim_threshold: float = SIM_THRESHOLD,
    sim_metric: str = SIM_METRIC,
) -> bool:
    """True if any already-saved route's footprint already captures this
    cluster (the representative ride is contained in a route within
    `sim_threshold`). Such clusters are dropped from suggestions."""
    for rc in route_cellsets:
        if cell_similarity(representative_cells, rc, sim_metric) >= sim_threshold:
            return True
    return False
