"""Region-scoped trail + road aggregation for the route builder.

Pulls named OSM ways (trails + roads, separately) that intersect a
region's polygon, chains same-name fragments into ordered polyline
components, and computes a junction graph for partial-trail selection.

Output is cached at cache/region_trails/<region_id>.json and invalidated
when ROUTE_BUILDER_VERSION bumps. Underlying OSM bbox caches
(cache/osm_paths/, cache/osm_roads/) have their own 90-day TTL.

Reuses trail_match.fetch_osm_trails for the trail layer (same Overpass
schema, same cache dir). Roads are fetched + cached separately so
trail_match's "off-trail = roads" semantic stays intact — trail_match
must NEVER read the roads cache.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from pathlib import Path

import trail_match
from geo import point_in_polygon as _point_in_polygon

# Bump to invalidate per-region cached artifacts (e.g. when junction logic
# changes). The /api/regions/<id>/trails-geometry response shape should
# stay in lockstep — bump ROUTES_API_VERSION in app.py alongside.
ROUTE_BUILDER_VERSION = 4

# Two-pass junction detection thresholds.
ENDPOINT_SHARE_M = 5.0   # endpoints within this radius = shared junction
INTERIOR_NEAR_M  = 8.0   # interior vertex of A within this of B's polyline = junction
JUNCTION_DEDUP_M = 25.0  # collapse near-duplicate junctions. Bigger than
                          # the 8 m detection threshold so interior-near-miss
                          # firing at multiple adjacent vertices of a long
                          # trail running parallel to another collapses to
                          # one junction instead of producing ~10 m sliver
                          # edges that are effectively unclickable.
# Edge splitting: when projecting a junction onto a trail's polyline, only
# accept the match if within this radius. Junctions discovered via
# INTERIOR_NEAR_M are guaranteed within 8 m; this allows slack for endpoint
# clusters whose averaged centroid drifts a few metres from any one trail.
JUNCTION_PROJECT_M = 15.0

logger = logging.getLogger(__name__)


# ─── Polygon geometry ───────────────────────────────────────────────────────

def _polygon_ring_latlon(region: dict) -> list[tuple[float, float]]:
    """GeoJSON polygon outer ring → list of (lat, lon) tuples."""
    coords = region.get("geometry", {}).get("coordinates", [[]])[0]
    return [(c[1], c[0]) for c in coords]


def _ring_bbox(ring) -> tuple[float, float, float, float]:
    lats = [p[0] for p in ring]
    lons = [p[1] for p in ring]
    return (min(lats), min(lons), max(lats), max(lons))


def _way_intersects_polygon(coords, ring) -> bool:
    """Cheap test: any vertex inside the polygon. Misses ways that cross
    cleanly without a vertex inside — acceptable given OSM vertex density
    (~10-50 m) vs typical region size (~km)."""
    return any(_point_in_polygon(lat, lon, ring) for lat, lon in coords)


# ─── Chain same-name fragments into connected components ────────────────────

def _chain_named_ways(ways: list[dict]) -> dict[str, list[list[tuple]]]:
    """For each name, walk every connected component of same-name fragments
    using endpoint sharing. Most named trails collapse to one component;
    OSM ways split at intersections re-merge here.

    Different from trail_match._walk_chain (which picks a single canonical
    chain for progress projection) — here every disconnected component is
    returned, so the map shows the full trail even when its OSM ways
    don't all touch (rare but real).
    """
    by_name: dict[str, list[list[tuple]]] = {}
    for w in ways:
        coords = list(w.get("coords", []))
        if len(coords) >= 2:
            by_name.setdefault(w["name"], []).append(coords)

    out: dict[str, list[list[tuple]]] = {}
    for name, frags in by_name.items():
        out[name] = _all_chain_components(frags)
    return out


def _all_chain_components(coords_list: list[list]) -> list[list[tuple]]:
    """Return every connected component as one ordered polyline.

    Greedy two-direction walk: pick an unused fragment, extend forward
    until no neighbour matches, then extend backward. Repeat until all
    fragments are consumed. O(n^2) in fragment count per name, but n is
    tiny (~1-10).
    """
    n = len(coords_list)
    endpoints = [(c[0], c[-1]) for c in coords_list]

    def _near(a, b):
        return trail_match._haversine_m(a[0], a[1], b[0], b[1]) <= ENDPOINT_SHARE_M

    used = [False] * n
    components: list[list[tuple]] = []

    for start in range(n):
        if used[start]:
            continue
        chain: list[tuple] = list(coords_list[start])
        used[start] = True

        # Walk forward
        while True:
            cur = chain[-1]
            nxt = None
            for j in range(n):
                if used[j]:
                    continue
                if _near(cur, endpoints[j][0]):
                    chain.extend(coords_list[j][1:])
                    used[j] = True
                    nxt = j
                    break
                if _near(cur, endpoints[j][1]):
                    chain.extend(list(reversed(coords_list[j]))[1:])
                    used[j] = True
                    nxt = j
                    break
            if nxt is None:
                break

        # Walk backward
        while True:
            cur = chain[0]
            nxt = None
            for j in range(n):
                if used[j]:
                    continue
                if _near(cur, endpoints[j][1]):
                    chain = list(coords_list[j][:-1]) + chain
                    used[j] = True
                    nxt = j
                    break
                if _near(cur, endpoints[j][0]):
                    chain = list(reversed(coords_list[j]))[:-1] + chain
                    used[j] = True
                    nxt = j
                    break
            if nxt is None:
                break

        if len(chain) >= 2:
            components.append(chain)
    return components


def _polyline_length_m(coords) -> float:
    total = 0.0
    for i in range(1, len(coords)):
        total += trail_match._haversine_m(coords[i - 1][0], coords[i - 1][1],
                                          coords[i][0],     coords[i][1])
    return total


# ─── Junction graph ─────────────────────────────────────────────────────────

def _build_junctions(trails: list[dict]) -> list[dict]:
    """Two-pass junction detection on the trail layer (roads excluded —
    junctions are click targets for trail partials only).

    Pass 1 (endpoint sharing): cluster all component endpoints within
    ENDPOINT_SHARE_M. A cluster spanning >=2 distinct trail names is a
    junction. Most Moose Mountain intersections fall here.

    Pass 2 (interior near-miss): for each interior vertex of trail A,
    check whether it lies within INTERIOR_NEAR_M of any segment of any
    other trail B. Catches OSM ways that don't split at every crossing.

    Final dedup collapses junctions within JUNCTION_DEDUP_M.
    """
    raw: list[dict] = []

    # Pass 1: endpoint clusters
    eps: list[tuple[float, float, str]] = []
    for t in trails:
        for comp in t["_components"]:
            eps.append((comp[0][0],  comp[0][1],  t["name"]))
            eps.append((comp[-1][0], comp[-1][1], t["name"]))

    used = [False] * len(eps)
    for i in range(len(eps)):
        if used[i]:
            continue
        clat, clon = eps[i][0], eps[i][1]
        names = {eps[i][2]}
        used[i] = True
        for j in range(i + 1, len(eps)):
            if used[j]:
                continue
            if trail_match._haversine_m(clat, clon, eps[j][0], eps[j][1]) <= ENDPOINT_SHARE_M:
                names.add(eps[j][2])
                used[j] = True
        if len(names) >= 2:
            raw.append({"lat": clat, "lon": clon, "trails": names})

    # Pass 2: interior near-miss. Build per-name segment list once.
    name_segments: dict[str, list[tuple]] = {}
    for t in trails:
        segs: list[tuple] = []
        for comp in t["_components"]:
            for k in range(1, len(comp)):
                segs.append((comp[k - 1], comp[k]))
        name_segments[t["name"]] = segs

    for t in trails:
        a_name = t["name"]
        for comp in t["_components"]:
            for v_idx in range(1, len(comp) - 1):  # interior only
                vlat, vlon = comp[v_idx]
                for b_name, segs in name_segments.items():
                    if b_name == a_name:
                        continue
                    if _vertex_near_any_segment(vlat, vlon, segs, INTERIOR_NEAR_M):
                        raw.append({"lat": vlat, "lon": vlon,
                                    "trails": {a_name, b_name}})
                        break

    return _dedup_junctions(raw)


def _vertex_near_any_segment(plat, plon, segs, thresh_m: float) -> bool:
    """Local-plane distance with a cheap lat/lon bbox prune per segment."""
    lat_thresh_deg = thresh_m / 111000.0
    cos_lat = max(0.1, math.cos(math.radians(plat)))
    lon_thresh_deg = thresh_m / (111000.0 * cos_lat)
    for a, b in segs:
        if max(a[0], b[0]) + lat_thresh_deg < plat: continue
        if min(a[0], b[0]) - lat_thresh_deg > plat: continue
        if max(a[1], b[1]) + lon_thresh_deg < plon: continue
        if min(a[1], b[1]) - lon_thresh_deg > plon: continue
        ax, ay = trail_match._to_local(a[0], a[1], plat, plon)
        bx, by = trail_match._to_local(b[0], b[1], plat, plon)
        if trail_match._point_segment_dist(0.0, 0.0, ax, ay, bx, by) <= thresh_m:
            return True
    return False


def _dedup_junctions(raw: list[dict]) -> list[dict]:
    """Cluster junctions within JUNCTION_DEDUP_M, average their coords,
    union their trail-name sets. Assign a stable id from the rounded coord
    so repeated builds produce the same junction ids (UI bookmarks survive
    a rebuild)."""
    n = len(raw)
    used = [False] * n
    out: list[dict] = []
    for i in range(n):
        if used[i]:
            continue
        cluster = [raw[i]]
        used[i] = True
        for j in range(i + 1, n):
            if used[j]:
                continue
            if trail_match._haversine_m(raw[i]["lat"], raw[i]["lon"],
                                        raw[j]["lat"], raw[j]["lon"]) <= JUNCTION_DEDUP_M:
                cluster.append(raw[j])
                used[j] = True
        avg_lat = sum(c["lat"] for c in cluster) / len(cluster)
        avg_lon = sum(c["lon"] for c in cluster) / len(cluster)
        names: set[str] = set()
        for c in cluster:
            names.update(c["trails"])
        jid = "j_" + hashlib.md5(
            f"{round(avg_lat, 5)},{round(avg_lon, 5)}".encode()
        ).hexdigest()[:8]
        out.append({
            "id":     jid,
            "lat":    avg_lat,
            "lon":    avg_lon,
            "trails": sorted(names),
        })
    return out


# ─── Edge splitting ─────────────────────────────────────────────────────────

def _project_to_polyline(plat: float, plon: float,
                         polyline: list[tuple]) -> tuple[int, float] | None:
    """Return (nearest_vertex_idx, distance_m) of the polyline vertex
    closest to (plat, plon). None if polyline is empty.
    """
    if not polyline:
        return None
    best_idx = 0
    best_d   = float("inf")
    for i, (lat, lon) in enumerate(polyline):
        d = trail_match._haversine_m(plat, plon, lat, lon)
        if d < best_d:
            best_d   = d
            best_idx = i
    return best_idx, best_d


def _edges_for_component(trail_name: str, component_idx: int,
                          polyline: list[tuple],
                          junctions: list[dict],
                          endpoint_ids: tuple[str, str]) -> list[dict]:
    """Split one polyline component into edges between consecutive nodes.

    Nodes along the component, in polyline order:
      - endpoint_start (vertex 0)
      - any junction whose .trails includes trail_name AND whose projection
        onto the polyline is within JUNCTION_PROJECT_M
      - endpoint_end (vertex -1)

    Two consecutive nodes at the same vertex collapse (no zero-length edge).
    """
    if len(polyline) < 2:
        return []

    # Find which junctions live on this component and where.
    pinned: list[tuple[int, str]] = []   # (vertex_idx, node_id)
    for j in junctions:
        if trail_name not in j.get("trails", []):
            continue
        proj = _project_to_polyline(j["lat"], j["lon"], polyline)
        if proj is None:
            continue
        idx, d = proj
        if d > JUNCTION_PROJECT_M:
            continue
        # Skip junctions that landed at the very endpoints — those are
        # already represented by endpoint pseudo-nodes (and the endpoint
        # pseudo-node has the same coord, so identity is preserved by the
        # client when it loads).
        if idx == 0 or idx == len(polyline) - 1:
            continue
        pinned.append((idx, j["id"]))

    # Collapse pinned entries that land on the same polyline vertex —
    # otherwise the edge between them would be zero-length and the node
    # ids on either side would discontinuously jump. Keep the first id
    # seen at each vertex (sort is stable, so this is deterministic).
    pinned.sort(key=lambda x: x[0])
    pinned_dedup: list[tuple[int, str]] = []
    last_idx = -1
    for idx, jid in pinned:
        if idx == last_idx:
            continue
        pinned_dedup.append((idx, jid))
        last_idx = idx

    nodes: list[tuple[int, str]] = [(0, endpoint_ids[0])]
    nodes.extend(pinned_dedup)
    nodes.append((len(polyline) - 1, endpoint_ids[1]))

    edges: list[dict] = []
    for k in range(1, len(nodes)):
        i, start_id = nodes[k - 1]
        j, end_id   = nodes[k]
        if j <= i:                       # degenerate / zero-length
            continue
        slice_pl = polyline[i:j + 1]
        edge_id = "e_" + hashlib.md5(
            f"{trail_name}|{component_idx}|{start_id}|{end_id}".encode()
        ).hexdigest()[:8]
        edges.append({
            "id":         edge_id,
            "start_node": start_id,
            "end_node":   end_id,
            "polyline":   [[lat, lon] for lat, lon in slice_pl],
            "length_m":   _polyline_length_m(slice_pl),
        })
    return edges


# ─── Build artifact ─────────────────────────────────────────────────────────

def build_region_artifact(region: dict, *,
                          osm_paths_dir: Path,
                          osm_roads_dir: Path) -> dict:
    """Build the trails + roads + junctions artifact for `region` from
    scratch. Pure function — no disk write. See get_region_artifact for
    the cached entry point.
    """
    ring = _polygon_ring_latlon(region)
    if not ring:
        return _empty_artifact(region)
    bbox = _ring_bbox(ring)

    trail_ways = trail_match.fetch_osm_trails(bbox, osm_paths_dir)
    road_ways  = trail_match.fetch_osm_roads(bbox,           osm_roads_dir)

    # If either fetch returned empty, it could mean Overpass actually has
    # nothing — or it timed out / 504'd. The fetch helpers log a warning
    # but otherwise swallow errors and return []. Mark the artifact partial
    # so get_region_artifact skips the disk write and a retry rebuilds.
    fetch_failed = (not trail_ways) or (not road_ways)

    trail_ways = [w for w in trail_ways if _way_intersects_polygon(w["coords"], ring)]
    road_ways  = [w for w in road_ways  if _way_intersects_polygon(w["coords"], ring)]

    trail_chains = _chain_named_ways(trail_ways)
    road_chains  = _chain_named_ways(road_ways)

    # Junction graph spans trails AND roads, so a road gets split at every
    # trail that meets it (e.g. Moose Mountain Road broken at the Pneuma
    # and Sulphur Springs trailheads, instead of one 5 km edge).
    # _build_junctions treats every input way uniformly — `.name` and
    # `._components` are all it needs.
    interim_trails: list[dict] = []
    for name, components in trail_chains.items():
        sample = next((w for w in trail_ways if w["name"] == name), {})
        interim_trails.append({
            "name":        name,
            "kind":        "trail",
            "highway":     sample.get("highway"),
            "mtb_scale":   sample.get("mtb_scale"),
            "oneway":      False,
            "_components": components,
        })
    interim_roads: list[dict] = []
    for name, components in road_chains.items():
        sample = next((w for w in road_ways if w["name"] == name), {})
        interim_roads.append({
            "name":        name,
            "kind":        "road",
            "highway":     sample.get("highway"),
            "oneway":      bool(sample.get("oneway")),
            "_components": components,
        })
    junctions = _build_junctions(interim_trails + interim_roads)

    def _emit_with_edges(interim: list[dict]) -> list[dict]:
        """Convert interim ways into the wire shape, generating per-component
        endpoint pseudo-junctions + edges from the shared junction set."""
        out: list[dict] = []
        for w in interim:
            components = w["_components"]
            endpoints: list[dict] = []
            edges:     list[dict] = []
            for ci, comp in enumerate(components):
                ep_ids: list[str] = []
                for side, (lat, lon) in (("start", comp[0]), ("end", comp[-1])):
                    eid = "j_" + hashlib.md5(
                        f"{w['name']}|{ci}|{side}|{round(lat, 5)},{round(lon, 5)}".encode()
                    ).hexdigest()[:8]
                    endpoints.append({"id": eid, "lat": lat, "lon": lon,
                                       "side": side, "component_idx": ci})
                    ep_ids.append(eid)
                edges.extend(_edges_for_component(
                    w["name"], ci, comp, junctions, (ep_ids[0], ep_ids[1]),
                ))
            comps_payload = [{"polyline": [[lat, lon] for lat, lon in comp],
                              "length_m": _polyline_length_m(comp)}
                             for comp in components]
            entry = {
                "name":           w["name"],
                "kind":           w["kind"],
                "highway":        w["highway"],
                "oneway":         w.get("oneway", False),
                "components":     comps_payload,
                "endpoints":      endpoints,
                "edges":          edges,
                "total_length_m": sum(c["length_m"] for c in comps_payload),
            }
            if w["kind"] == "trail":
                entry["mtb_scale"] = w.get("mtb_scale")
            out.append(entry)
        return out

    trails = _emit_with_edges(interim_trails)
    roads  = _emit_with_edges(interim_roads)

    return {
        "region_id":    region["id"],
        "region_name":  region.get("name"),
        "region_color": region.get("color"),
        "version":      ROUTE_BUILDER_VERSION,
        "built_at":     time.time(),
        "bbox":         list(bbox),
        "trails":       sorted(trails, key=lambda x: x["name"]),
        "roads":        sorted(roads,  key=lambda x: x["name"]),
        "junctions":    junctions,
        "_partial":     fetch_failed,
    }


def _empty_artifact(region: dict) -> dict:
    return {
        "region_id":    region["id"],
        "region_name":  region.get("name"),
        "region_color": region.get("color"),
        "version":      ROUTE_BUILDER_VERSION,
        "built_at":     time.time(),
        "bbox":         None,
        "trails":       [],
        "roads":        [],
        "junctions":    [],
    }


# ─── Cached entry point ─────────────────────────────────────────────────────

_artifact_locks: dict[str, threading.Lock] = {}
_artifact_locks_mu = threading.Lock()


def _artifact_lock_for(region_id: str) -> threading.Lock:
    with _artifact_locks_mu:
        if region_id not in _artifact_locks:
            _artifact_locks[region_id] = threading.Lock()
        return _artifact_locks[region_id]


def _artifact_path(cache_dir: Path, region_id: str) -> Path:
    return cache_dir / f"{region_id}.json"


def _read_valid_artifact(ap: Path, force_rebuild: bool) -> dict | None:
    """The cached artifact at `ap` if present and version-current, else None.
    Corrupt / wrong-version / unreadable files read as a miss."""
    if force_rebuild or not ap.exists():
        return None
    try:
        entry = json.loads(ap.read_text(encoding="utf-8"))
        if entry.get("version") == ROUTE_BUILDER_VERSION:
            return entry
    except Exception:
        pass
    return None


def get_region_artifact(region: dict, *,
                        artifacts_dir: Path,
                        osm_paths_dir: Path,
                        osm_roads_dir: Path,
                        force_rebuild: bool = False) -> dict:
    """Return the cached artifact for `region`, building it if missing,
    version-mismatched, or `force_rebuild=True`. The underlying OSM bbox
    caches honour their own 90-day TTL — we don't second-guess them here.
    """
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    ap = _artifact_path(artifacts_dir, region["id"])
    cached = _read_valid_artifact(ap, force_rebuild)
    if cached is not None:
        return cached

    with _artifact_lock_for(region["id"]):
        cached = _read_valid_artifact(ap, force_rebuild)
        if cached is not None:
            return cached
        artifact = build_region_artifact(
            region,
            osm_paths_dir=osm_paths_dir,
            osm_roads_dir=osm_roads_dir,
        )
        if artifact.pop("_partial", False):
            logger.warning("Skipping cache write for region %s (Overpass fetch incomplete)",
                           region.get("id"))
            return artifact
        try:
            tmp = ap.with_suffix(ap.suffix + ".tmp")
            tmp.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
            tmp.replace(ap)
        except OSError as exc:
            logger.warning("Failed to persist region artifact %s: %s", ap, exc)
        return artifact
