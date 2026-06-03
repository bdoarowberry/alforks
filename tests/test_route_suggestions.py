"""Tests for the route-suggestion geometry primitives: grid cell sets,
bounding-box / distance prefilters, cell-set similarity, and union-find
clustering. These lock in the math the clustering pipeline (commit 2)
will build on, including a synthetic stand-in for each real-data canary.

Run with:
    python -m unittest tests.test_route_suggestions
"""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import route_suggestions as rs

# A reference point in the rides' region (~Kananaskis). Helpers below
# offset from here in meters so tests read in physical units.
_LAT0 = 50.70
_LON0 = -114.65
_M_PER_DEG_LAT = 111_320.0
_M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(rs._REF_LAT_DEG))


def _pt(north_m=0.0, east_m=0.0):
    """A point `north_m` / `east_m` meters from the reference origin."""
    return {
        "lat": _LAT0 + north_m / _M_PER_DEG_LAT,
        "lon": _LON0 + east_m / _M_PER_DEG_LON,
    }


def _line(n, step_m=11.0, axis="east"):
    """A straight run of `n` points spaced `step_m` apart (default ~real
    GPS spacing), starting at the origin."""
    pts = []
    for i in range(n):
        d = i * step_m
        pts.append(_pt(0.0, d) if axis == "east" else _pt(d, 0.0))
    return pts


class TestCellKey(unittest.TestCase):
    def test_same_spot_same_cell(self):
        a = rs._cell_key(_LAT0, _LON0)
        b = rs._cell_key(_LAT0 + 0.1 / _M_PER_DEG_LAT, _LON0)  # 0.1 m away
        self.assertEqual(a, b)

    def test_north_move_changes_north_index_only_axis(self):
        base = rs._cell_key(**_coords(_pt(0, 0)))
        north = rs._cell_key(**_coords(_pt(200, 0)))  # 200 m north
        self.assertGreater(north[0], base[0])  # north index advanced

    def test_east_move_changes_east_index(self):
        base = rs._cell_key(**_coords(_pt(0, 0)))
        east = rs._cell_key(**_coords(_pt(0, 200)))  # 200 m east
        self.assertNotEqual(east[1], base[1])

    def test_north_move_does_not_smear_east_index(self):
        # Regression: a per-point cos(lat) would shift the east index when
        # only latitude changes (large |lon| amplifies it). A fixed
        # reference cos must keep east stable for a pure-north move.
        base = rs._cell_key(**_coords(_pt(0, 0)))
        north = rs._cell_key(**_coords(_pt(300, 0)))  # 300 m north, same lon
        self.assertEqual(north[1], base[1])


def _coords(p):
    return {"lat": p["lat"], "lon": p["lon"]}


class TestRideCellSet(unittest.TestCase):
    def test_back_and_forth_dedups(self):
        # Out-and-back over the same line occupies each cell once (set).
        out = _line(40, axis="east")
        back = list(reversed(out))
        once = rs.ride_cell_set(out, stride=1)
        twice = rs.ride_cell_set(out + back, stride=1)
        self.assertEqual(once, twice)

    def test_stride_preserves_cells_for_dense_line(self):
        # At 11 m spacing and a 60 m cell, striding by 3 (~33 m) skips no
        # cell on a continuous track.
        line = _line(120, step_m=11.0, axis="east")
        full = rs.ride_cell_set(line, stride=1)
        strided = rs.ride_cell_set(line, stride=3)
        self.assertEqual(full, strided)

    def test_skips_points_missing_coords(self):
        pts = [_pt(0, 0), {"lat": None, "lon": 1.0}, {"foo": "bar"}, _pt(0, 200)]
        cells = rs.ride_cell_set(pts, stride=1)
        self.assertEqual(len(cells), 2)


class TestBboxIou(unittest.TestCase):
    def test_disjoint_zero(self):
        a = [50.0, -114.0, 50.1, -113.9]
        b = [51.0, -113.0, 51.1, -112.9]
        self.assertEqual(rs.bbox_iou(a, b), 0.0)

    def test_identical_one(self):
        a = [50.0, -114.0, 50.1, -113.9]
        self.assertAlmostEqual(rs.bbox_iou(a, a), 1.0)

    def test_half_overlap(self):
        a = [0.0, 0.0, 2.0, 2.0]   # area 4
        b = [1.0, 0.0, 3.0, 2.0]   # area 4, intersection 1x2 = 2
        # iou = 2 / (4 + 4 - 2) = 2/6
        self.assertAlmostEqual(rs.bbox_iou(a, b), 2.0 / 6.0)

    def test_empty_zero(self):
        self.assertEqual(rs.bbox_iou([], [1, 2, 3, 4]), 0.0)


class TestDistWithin(unittest.TestCase):
    def test_within_tolerance(self):
        self.assertTrue(rs.dist_within(20.0, 22.0, 0.20))

    def test_outside_tolerance(self):
        self.assertFalse(rs.dist_within(20.0, 26.0, 0.20))  # 30% longer

    def test_zero_fails_closed(self):
        self.assertFalse(rs.dist_within(0.0, 10.0, 0.20))
        self.assertFalse(rs.dist_within(10.0, None, 0.20))


class TestCellSimilarity(unittest.TestCase):
    def setUp(self):
        self.big = frozenset((0, j) for j in range(100))
        self.small = frozenset((0, j) for j in range(50))  # subset of big

    def test_containment_subset_is_one(self):
        self.assertAlmostEqual(
            rs.cell_similarity(self.small, self.big, "containment"), 1.0)

    def test_jaccard_subset_below_one(self):
        # 50 / 100 — documents why containment is the default: Jaccard
        # would reject a trimmed recording of the same loop.
        self.assertAlmostEqual(
            rs.cell_similarity(self.small, self.big, "jaccard"), 0.5)

    def test_disjoint_zero(self):
        other = frozenset((9, j) for j in range(50))
        self.assertEqual(rs.cell_similarity(self.small, other, "containment"), 0.0)

    def test_empty_zero(self):
        self.assertEqual(rs.cell_similarity(frozenset(), self.big), 0.0)


class TestUnionFind(unittest.TestCase):
    def test_transitive_chain_one_component(self):
        uf = rs._UF()
        uf.union("a", "b")
        uf.union("b", "c")  # a~b, b~c => {a,b,c} even though a,c never joined
        groups = uf.groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]), {"a", "b", "c"})

    def test_separate_components(self):
        uf = rs._UF()
        uf.union("a", "b")
        uf.union("x", "y")
        uf.add("loner")
        groups = {frozenset(g) for g in uf.groups()}
        self.assertIn(frozenset({"a", "b"}), groups)
        self.assertIn(frozenset({"x", "y"}), groups)
        self.assertIn(frozenset({"loner"}), groups)


class TestCanaryGeometry(unittest.TestCase):
    """Synthetic stand-ins for the documented real-data canaries, proving
    the primitives compose into the intended cluster/exclude behavior
    before the full pipeline (commit 2) wires real rides."""

    def _loop(self, jitter_m=0.0, extra_east_m=0.0):
        """A closed-ish loop footprint; `jitter_m` nudges it (a different
        day's GPS), `extra_east_m` appends a detour leg (a longer ride)."""
        pts = []
        for d in range(0, 1200, 11):           # ~1.2 km east leg
            pts.append(_pt(jitter_m, d + jitter_m))
        for d in range(0, 1200, 11):           # ~1.2 km return, offset north
            pts.append(_pt(120 + jitter_m, 1200 - d + jitter_m))
        for d in range(0, int(extra_east_m), 11):  # optional detour appendage
            pts.append(_pt(120, 1200 + d))
        return pts

    def test_cox_hill_three_days_cluster(self):
        # Three jittered recordings of one loop -> one component of 3.
        rides = {
            "2019": rs.ride_cell_set(self._loop(jitter_m=0)),
            "2020": rs.ride_cell_set(self._loop(jitter_m=8)),
            "2023": rs.ride_cell_set(self._loop(jitter_m=15)),
        }
        uf = rs._UF()
        names = list(rides)
        for a in names:
            uf.add(a)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                if rs.cell_similarity(rides[names[i]], rides[names[j]]) >= rs.SIM_THRESHOLD:
                    uf.union(names[i], names[j])
        groups = uf.groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]), {"2019", "2020", "2023"})

    def test_elbow_long_detour_excluded(self):
        # Two near-identical rides cluster; a third with a long detour
        # (+~50% distance) must be rejected by the distance gate before it
        # can join, mirroring the 2020-08-15 Elbow ride.
        short_pts = self._loop(jitter_m=0)
        short_pts2 = self._loop(jitter_m=10)
        long_pts = self._loop(jitter_m=5, extra_east_m=1500)

        # Distances proxied by point count * spacing.
        d_short = len(short_pts) * 0.011
        d_long = len(long_pts) * 0.011

        # Prefilter: the long ride fails the distance gate vs the short.
        self.assertTrue(rs.dist_within(d_short, len(short_pts2) * 0.011))
        self.assertFalse(rs.dist_within(d_short, d_long))

        a = rs.ride_cell_set(short_pts)
        b = rs.ride_cell_set(short_pts2)
        self.assertGreaterEqual(rs.cell_similarity(a, b), rs.SIM_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
