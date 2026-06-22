"""Tests for route_builder.rebuild_route_segments — re-snapping a saved route's
segments to a current region artifact (the "Rebuild stale routes" feature)."""
import unittest

import route_builder as rb


def _seg(trail, eid, a, b, direction="down", kind="trail"):
    return {"trail_name": trail, "kind": kind, "direction": direction,
            "edge_id": eid, "start_junction": a, "end_junction": b}


# Synthetic artifact: trail "A" = jA-jB-jC (2 edges), trail "B" = jC-jD (1 edge).
ARTIFACT = {
    "trails": [
        {"name": "A", "edges": [
            {"id": "e_a1", "start_node": "jA", "end_node": "jB", "polyline": [[0, 0], [0, 1]], "length_m": 100},
            {"id": "e_a2", "start_node": "jB", "end_node": "jC", "polyline": [[0, 1], [0, 2]], "length_m": 100},
        ], "endpoints": [{"id": "jA", "lat": 0, "lon": 0}, {"id": "jC", "lat": 0, "lon": 2}]},
        {"name": "B", "edges": [
            {"id": "e_b1", "start_node": "jC", "end_node": "jD", "polyline": [[0, 2], [0, 3]], "length_m": 100},
        ]},
    ],
    "roads": [],
    "junctions": [
        {"id": "jA", "lat": 0, "lon": 0}, {"id": "jB", "lat": 0, "lon": 1},
        {"id": "jC", "lat": 0, "lon": 2}, {"id": "jD", "lat": 0, "lon": 3},
    ],
}


class TestRebuildRouteSegments(unittest.TestCase):
    def test_fresh_route_unchanged(self):
        route = {"id": "r", "region_id": "x", "segments": [
            _seg("A", "e_a1", "jA", "jB"), _seg("A", "e_a2", "jB", "jC"), _seg("B", "e_b1", "jC", "jD")]}
        out, status = rb.rebuild_route_segments(route, ARTIFACT)
        self.assertEqual(status, "fresh")
        self.assertEqual([s["edge_id"] for s in out], ["e_a1", "e_a2", "e_b1"])

    def test_exact_remap_when_edge_ids_stale_but_junctions_intact(self):
        route = {"id": "r", "region_id": "x", "segments": [
            _seg("A", "e_OLD1", "jA", "jB"), _seg("A", "e_OLD2", "jB", "jC")]}
        out, status = rb.rebuild_route_segments(route, ARTIFACT)
        self.assertEqual(status, "exact")
        self.assertEqual([s["edge_id"] for s in out], ["e_a1", "e_a2"])
        self.assertEqual(out[0]["start_junction"], "jA")
        self.assertEqual(out[-1]["end_junction"], "jC")

    def test_mixed_fresh_and_stale_in_one_run_is_exact(self):
        route = {"id": "r", "region_id": "x", "segments": [
            _seg("A", "e_a1", "jA", "jB"), _seg("A", "e_OLD2", "jB", "jC")]}
        out, status = rb.rebuild_route_segments(route, ARTIFACT)
        self.assertEqual(status, "exact")
        self.assertEqual([s["edge_id"] for s in out], ["e_a1", "e_a2"])

    def test_approximate_full_trail_when_junctions_vanished(self):
        # stale edge AND junctions not present in the artifact -> full trail A
        route = {"id": "r", "region_id": "x", "segments": [_seg("A", "e_OLD", "jX", "jY")]}
        out, status = rb.rebuild_route_segments(route, ARTIFACT)
        self.assertEqual(status, "approximate")
        self.assertEqual([s["edge_id"] for s in out], ["e_a1", "e_a2"])
        # connected: each segment chains into the next
        self.assertEqual(out[0]["end_junction"], out[1]["start_junction"])

    def test_failed_when_trail_name_missing_leaves_route_unchanged(self):
        segs = [_seg("Z", "e_z", "j1", "j2")]
        route = {"id": "r", "region_id": "x", "segments": segs}
        out, status = rb.rebuild_route_segments(route, ARTIFACT)
        self.assertEqual(status, "failed")
        self.assertEqual(out, segs)  # untouched

    def test_empty_route_is_fresh(self):
        out, status = rb.rebuild_route_segments({"id": "r", "segments": []}, ARTIFACT)
        self.assertEqual(status, "fresh")
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
