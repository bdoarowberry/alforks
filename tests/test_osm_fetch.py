"""Characterization tests for the trail/road Overpass fetch stacks.

These pin the *observable* surface of `fetch_osm_trails` / `fetch_osm_roads`
so the two stacks can be unified onto a shared template without drift:
  * the exact Overpass query string sent (per highway-type set),
  * the urlopen timeout,
  * the parsed way dicts AND their key order (cache bytes must stay stable so
    existing on-disk caches keep reading and no refetch storm is triggered),
  * the on-disk cache blob shape,
  * the breaker-open / network-failure fallbacks,
  * the mem-cache short-circuit, and crucially that trail and road caches for
    the same bbox do NOT cross-contaminate (they hash to the same stem).

The network is faked — nothing here hits Overpass.
"""
import io
import json
import urllib.parse
import urllib.request

import osm_breaker
import trail_match

BBOX = (10.0, 20.0, 11.0, 21.0)

# Goldens captured from the pre-unification code (see the fetch stacks).
TRAIL_QUERY = ('[out:json][timeout:60];(way["highway"~"^(path|track|footway|'
               'cycleway|bridleway)$"]["name"](9.998,19.998,11.002,21.002);'
               ');(._;>;);out body;')
ROAD_QUERY = ('[out:json][timeout:60];(way["highway"~"^(residential|service|'
              'unclassified|tertiary|secondary|primary)$"]["name"]'
              '(9.998,19.998,11.002,21.002););(._;>;);out body;')

# Two nodes + one named way (kept) + one unnamed way (dropped) + one
# named-but-too-short way (dropped). The tags carry both set-specific fields
# so each parser picks out only its own.
CANNED = {"elements": [
    {"type": "node", "id": 1, "lat": 10.0, "lon": 20.0},
    {"type": "node", "id": 2, "lat": 10.1, "lon": 20.1},
    {"type": "way", "id": 100, "nodes": [1, 2],
     "tags": {"name": "Foo", "highway": "path", "mtb:scale": "2", "oneway": "yes"}},
    {"type": "way", "id": 101, "nodes": [1, 2], "tags": {"highway": "track"}},
    {"type": "way", "id": 102, "nodes": [1], "tags": {"name": "Stub", "highway": "path"}},
]}


class _FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _fake_overpass(captured, response=None, raise_exc=None):
    """Build a fake urlopen recording the posted query into `captured`."""
    def fake_urlopen(req, timeout=None):
        captured["query"] = urllib.parse.parse_qs(req.data.decode())["data"][0]
        captured["timeout"] = timeout
        captured["calls"] = captured.get("calls", 0) + 1
        if raise_exc is not None:
            raise raise_exc
        return _FakeResp(json.dumps(response if response is not None else CANNED).encode())
    return fake_urlopen


def setup_function(_):
    osm_breaker.reset()
    trail_match._OSM_TRAIL_MEM_CACHE.clear()
    trail_match._OSM_ROAD_MEM_CACHE.clear()


# ── Query strings + timeout ─────────────────────────────────────────────────

def test_trail_query_string(monkeypatch, tmp_path):
    cap = {}
    monkeypatch.setattr(urllib.request, "urlopen", _fake_overpass(cap))
    trail_match.fetch_osm_trails(BBOX, tmp_path)
    assert cap["query"] == TRAIL_QUERY
    assert cap["timeout"] == trail_match.OVERPASS_TIMEOUT_SEC + 20


def test_road_query_string(monkeypatch, tmp_path):
    cap = {}
    monkeypatch.setattr(urllib.request, "urlopen", _fake_overpass(cap))
    trail_match.fetch_osm_roads(BBOX, tmp_path)
    assert cap["query"] == ROAD_QUERY
    assert cap["timeout"] == trail_match.OVERPASS_TIMEOUT_SEC + 20


# ── Parsed output + key order ───────────────────────────────────────────────

def test_trail_ways_parsed_with_stable_key_order(monkeypatch, tmp_path):
    cap = {}
    monkeypatch.setattr(urllib.request, "urlopen", _fake_overpass(cap))
    ways = trail_match.fetch_osm_trails(BBOX, tmp_path)
    assert ways == [{"id": 100, "name": "Foo", "highway": "path",
                     "mtb_scale": "2", "coords": [(10.0, 20.0), (10.1, 20.1)]}]
    # Key order is part of the cache-file bytes; pin it.
    assert list(ways[0].keys()) == ["id", "name", "highway", "mtb_scale", "coords"]


def test_road_ways_parsed_with_stable_key_order(monkeypatch, tmp_path):
    cap = {}
    monkeypatch.setattr(urllib.request, "urlopen", _fake_overpass(cap))
    ways = trail_match.fetch_osm_roads(BBOX, tmp_path)
    assert ways == [{"id": 100, "name": "Foo", "highway": "path",
                     "oneway": True, "coords": [(10.0, 20.0), (10.1, 20.1)]}]
    assert list(ways[0].keys()) == ["id", "name", "highway", "oneway", "coords"]


def test_cache_blob_shape(monkeypatch, tmp_path):
    cap = {}
    monkeypatch.setattr(urllib.request, "urlopen", _fake_overpass(cap))
    trail_match.fetch_osm_trails(BBOX, tmp_path)
    blob = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert set(blob.keys()) == {"fetched", "ways"}
    assert list(blob["ways"][0].keys()) == ["id", "name", "highway", "mtb_scale", "coords"]


# ── Caching / fallback behavior ─────────────────────────────────────────────

def test_mem_cache_short_circuits_second_fetch(monkeypatch, tmp_path):
    cap = {}
    monkeypatch.setattr(urllib.request, "urlopen", _fake_overpass(cap))
    trail_match.fetch_osm_trails(BBOX, tmp_path)
    trail_match.fetch_osm_trails(BBOX, tmp_path)
    assert cap["calls"] == 1  # second call served from mem cache


def test_trail_and_road_mem_caches_do_not_cross_contaminate(monkeypatch, tmp_path):
    """trail and road for the same bbox hash to the same cache stem; their
    mem caches must stay independent or one set's ways leak into the other."""
    cap = {}
    monkeypatch.setattr(urllib.request, "urlopen", _fake_overpass(cap))
    trails = trail_match.fetch_osm_trails(BBOX, tmp_path / "paths")
    roads = trail_match.fetch_osm_roads(BBOX, tmp_path / "roads")
    assert "mtb_scale" in trails[0] and "oneway" not in trails[0]
    assert "oneway" in roads[0] and "mtb_scale" not in roads[0]


def test_breaker_open_skips_network(monkeypatch, tmp_path):
    cap = {}
    monkeypatch.setattr(urllib.request, "urlopen", _fake_overpass(cap))
    for _ in range(osm_breaker.FAILURE_THRESHOLD):
        osm_breaker.record_failure()
    assert trail_match.fetch_osm_trails(BBOX, tmp_path) == []
    assert "calls" not in cap  # urlopen never invoked


def test_network_failure_records_failure_and_returns_empty(monkeypatch, tmp_path):
    cap = {}
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_overpass(cap, raise_exc=OSError("boom")))
    assert trail_match.fetch_osm_trails(BBOX, tmp_path) == []
    assert cap["calls"] == 1


def test_network_failure_falls_back_to_stale_cache(monkeypatch, tmp_path):
    # Seed a fresh cache, expire it by clearing mem + rewriting fetched=0,
    # then fail the network: stale-ignoring-TTL read should serve the old ways.
    cap = {}
    monkeypatch.setattr(urllib.request, "urlopen", _fake_overpass(cap))
    trail_match.fetch_osm_trails(BBOX, tmp_path)
    cf = next(tmp_path.glob("*.json"))
    blob = json.loads(cf.read_text(encoding="utf-8"))
    blob["fetched"] = 0  # force TTL miss
    cf.write_text(json.dumps(blob), encoding="utf-8")
    trail_match._OSM_TRAIL_MEM_CACHE.clear()
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_overpass(cap, raise_exc=OSError("boom")))
    ways = trail_match.fetch_osm_trails(BBOX, tmp_path)
    assert ways and ways[0]["id"] == 100  # served stale
