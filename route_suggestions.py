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
     resulting similarity graph by complete (mutual) linkage — a cluster
     is a set whose every pair clears the threshold; a cluster of
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
# v2: added MIN_RIDE_DISTANCE_KM floor (drops sub-1km tracks before pairing).
SUGGESTIONS_VERSION = 2

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

# Default metric for the low-level `cell_similarity` primitive.
SIM_METRIC = "containment"   # "containment" | "jaccard"

# Clustering rides into "same loop" groups uses SYMMETRIC Jaccard, not
# containment: two rides are the same route only if they cover the *same*
# ground, so a short ride that merely sits inside a longer ride's
# footprint (containment ~1.0) must NOT merge. Combined with mutual
# (complete) linkage below — a cluster is a set whose every pair clears
# the threshold — this prevents the single-linkage chaining that blobbed
# distinct overlapping loops together on real data.
CLUSTER_SIM_METRIC = "jaccard"
CLUSTER_SIM_THRESHOLD = 0.50

# The "is this cluster already a saved route?" exclusion DOES use
# containment: a route's footprint should suppress a ride it covers, even
# if the ride is a shorter slice of a longer saved route.
COVERAGE_SIM_METRIC = "containment"
COVERAGE_SIM_THRESHOLD = 0.55

MIN_CLUSTER_SIZE = 2

# Floor on a ride's total distance before it can seed a suggestion. Very
# short tracks (loops around a parking lot, a GPS still warming up, a
# dropped-then-resumed recording) cluster trivially — their footprints are
# tiny so a handful of shared cells clears Jaccard — but they're never a
# route worth saving. Drop anything under this before pairing.
MIN_RIDE_DISTANCE_KM = 1.0


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


# ── Pipeline ──────────────────────────────────────────────────────────
def _pk(a: str, b: str) -> tuple[str, str]:
    """Order-independent pair key."""
    return (a, b) if a < b else (b, a)


def _complete_linkage(items: list[str], pair_sim: dict, threshold: float) -> list[list[str]]:
    """Agglomerative complete-linkage clustering. Two clusters merge only
    when EVERY cross-pair similarity clears `threshold` (the complete-
    linkage score = the minimum cross-pair similarity), greedily strongest
    first. Result: every returned cluster is a clique at `threshold`, so a
    weak chain A-B-C with A,C dissimilar never collapses into one group.

    `pair_sim` maps `_pk(a,b)` -> similarity; absent pairs are 0 (they
    failed the prefilter or scored zero), which keeps cross-region and
    non-overlapping rides apart."""
    clusters = [[x] for x in items]

    def link(c1: list[str], c2: list[str]) -> float:
        worst = 1.0
        for a in c1:
            for b in c2:
                s = pair_sim.get(_pk(a, b), 0.0)
                if s < worst:
                    worst = s
                    if worst < threshold:
                        return worst  # can never merge — stop early
        return worst

    while len(clusters) > 1:
        best = None
        best_val = threshold
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                v = link(clusters[i], clusters[j])
                if v >= best_val:
                    best_val, best = v, (i, j)
        if best is None:
            break
        i, j = best
        clusters[i].extend(clusters[j])
        del clusters[j]
    return clusters


def cluster_rides(
    rides: list[dict],
    cell_set_loader: Callable[[str], "frozenset | None"],
    *,
    cell_m: float = GRID_CELL_M,
    bbox_min_overlap: float = BBOX_MIN_OVERLAP,
    dist_frac: float = PREFILTER_DIST_FRAC,
    sim_threshold: float = CLUSTER_SIM_THRESHOLD,
    sim_metric: str = CLUSTER_SIM_METRIC,
    min_cluster_size: int = MIN_CLUSTER_SIZE,
    min_distance_km: float = MIN_RIDE_DISTANCE_KM,
) -> list[dict]:
    """Group rides that are the same loop into clusters.

    `rides` is per-ride metadata dicts: `{filename, regions:[id,...],
    bbox, distance_km, date}`. `cell_set_loader(filename)` returns that
    ride's cell set (a frozenset) and is called ONLY for rides that
    survive the cheap prefilter — so the expensive point load happens for
    a small fraction of the library.

    Pairing is prefiltered within region buckets (bbox overlap + distance)
    to keep it ~O(rides_per_region^2); the surviving pairs are scored with
    symmetric Jaccard and grouped by complete linkage so each cluster is a
    mutually-similar set, not a chain. Returns cluster dicts:
        {region_id, members:[fn,...], size, representative,
         representative_cells}
    sorted largest-first. Saved-route exclusion is applied separately by
    the caller via `cluster_covered_by_route`.

    Rides shorter than `min_distance_km` are dropped up front so trivially
    short tracks can't seed a suggestion (see `MIN_RIDE_DISTANCE_KM`)."""
    rides = [r for r in rides if (r.get("distance_km") or 0) >= min_distance_km]
    by_fn = {r["filename"]: r for r in rides}

    # Bucket by region so pairing is O(rides_per_region^2), not O(all^2).
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
                candidate_pairs.add(_pk(a, b))

    if not candidate_pairs:
        return []

    # Stage 2: load cell sets only for rides that appear in a candidate pair.
    involved: set = set()
    for a, b in candidate_pairs:
        involved.add(a)
        involved.add(b)
    cells: dict = {fn: (cell_set_loader(fn) or frozenset()) for fn in involved}

    # Stage 3: score each candidate pair (symmetric Jaccard).
    pair_sim: dict = {}
    for a, b in candidate_pairs:
        s = cell_similarity(cells[a], cells[b], sim_metric)
        if s > 0:
            pair_sim[_pk(a, b)] = s

    # Stage 4: complete-linkage clustering over the rides that scored a
    # non-zero pair. Cross-region pairs are absent from pair_sim (sim 0),
    # so this stays effectively per-region while naturally treating a
    # multi-region ride as a single item (no cross-bucket duplication).
    items = sorted({x for pair in pair_sim for x in pair})
    clusters: list = []
    for group in _complete_linkage(items, pair_sim, sim_threshold):
        if len(group) < min_cluster_size:
            continue
        members = sorted(group)
        rep = _medoid(members, pair_sim, by_fn)
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


def _medoid(members: list[str], pair_sim: dict, by_fn: dict) -> str:
    """The cluster's most central ride: highest average similarity to the
    other members. This avoids both truncated rides (low overlap) and
    detour-heavy rides (extra ground the others don't share) as the route
    source. Tie-broken by newest date, then filename for determinism."""
    if len(members) == 1:
        return members[0]

    def avg_sim(fn: str) -> float:
        return sum(pair_sim.get(_pk(fn, o), 0.0) for o in members if o != fn) / (len(members) - 1)

    return max(members, key=lambda fn: (avg_sim(fn), by_fn[fn].get("date") or "", fn))


def cluster_covered_by_route(
    representative_cells: "frozenset",
    route_cellsets: Iterable["frozenset"],
    *,
    sim_threshold: float = COVERAGE_SIM_THRESHOLD,
    sim_metric: str = COVERAGE_SIM_METRIC,
) -> bool:
    """True if any already-saved route's footprint already captures this
    cluster (the representative ride is contained in a route within
    `sim_threshold`). Such clusters are dropped from suggestions."""
    for rc in route_cellsets:
        if cell_similarity(representative_cells, rc, sim_metric) >= sim_threshold:
            return True
    return False
