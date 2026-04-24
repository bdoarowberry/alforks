"""Tests for detection algorithms.

Run with:
    python -m unittest tests.test_detection
"""

from __future__ import annotations

import os
import sys
import unittest

# Add project root so `import detection` works when run from the tests dir
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import detection
from tests.fixtures import pure_descent, ski_day_with_lift, time_gap_shuttle


class TestBuildSegments(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(detection._build_segments([]), [])

    def test_all_riding(self):
        segs = detection._build_segments([False] * 5)
        self.assertEqual(segs, [{"type": "riding", "start": 0, "end": 4}])

    def test_transition(self):
        flags = [False, False, True, True, False]
        segs = detection._build_segments(flags)
        self.assertEqual(segs[0]["type"], "riding")
        self.assertEqual(segs[1]["type"], "assisted")
        # Transitions happen between indices — segment boundaries are inclusive
        # so the riding/assisted switch point is shared.


class TestHaversine(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(detection.haversine((0, 0), (0, 0)), 0)

    def test_roughly_1km(self):
        # Roughly 1 degree of lat = 111 km at the equator
        d = detection.haversine((0, 0), (0.009, 0))
        self.assertAlmostEqual(d, 1000, delta=5)


class TestAlgorithms(unittest.TestCase):
    def test_pure_descent_flags_nothing(self):
        per_pt, latlons = pure_descent()
        flags = detection._algo_lift(per_pt, latlons, [])
        self.assertFalse(any(flags), "descent should have no assisted points")

    def test_ski_lift_flagged(self):
        per_pt, latlons = ski_day_with_lift()
        flags = detection._algo_lift(per_pt, latlons, [])
        assisted_count = sum(flags)
        self.assertGreater(assisted_count, 50,
                           f"expected the 120-point lift to be flagged; got {assisted_count}")
        # Descent (last 100 points) should be mostly un-flagged
        descent_flagged = sum(flags[-100:])
        self.assertLess(descent_flagged, 10,
                        f"descent should be largely un-flagged; got {descent_flagged}")

    def test_time_gap_shuttle_flagged(self):
        per_pt, latlons = time_gap_shuttle()
        flags = detection._algo_time_gap(per_pt, latlons, [])
        self.assertTrue(flags[201], "the dt=300/gain=150 transition should flag")

    def test_heuristic_equals_time_gap_plus_speed(self):
        # Regression test for the dedup: _detect_assisted should equal the
        # union of _algo_time_gap and _algo_speed_sinuosity.
        per_pt, latlons = ski_day_with_lift()
        tg  = detection._algo_time_gap(per_pt, latlons, [])
        spd = detection._algo_speed_sinuosity(per_pt, latlons, [])
        heur = detection._algo_heuristic(per_pt, latlons, [])
        expected = [t or s for t, s in zip(tg, spd)]
        self.assertEqual(heur, expected)

    def test_elev_rate_equals_param_form(self):
        # Regression for the dedup: _algo_elevation_rate must equal the
        # parametric form with the default thresholds.
        per_pt, latlons = ski_day_with_lift()
        a = detection._algo_elevation_rate(per_pt, latlons, [])
        b = detection._detect_elev_rate_param(
            per_pt,
            detection._ELEV_RATE_THRESHOLD,
            detection._ELEV_RATE_MIN_GAIN,
            detection._ELEV_RATE_MIN_DUR,
        )
        self.assertEqual(a, b)


class TestStationLifts(unittest.TestCase):
    def test_empty_inputs_return_empty(self):
        self.assertEqual(detection._detect_station_lifts([], [], []), [])
        self.assertEqual(detection._detect_station_lifts([(0, 0)], [{'dt': 0, 'ele_delta': 0}], []), [False])

    def test_no_lifts_returns_all_false(self):
        per_pt = [{'dt': 1, 'ele_delta': 1}] * 5
        latlons = [(0, 0)] * 5
        self.assertEqual(detection._detect_station_lifts(latlons, per_pt, []), [False]*5)


class TestAlgoSig(unittest.TestCase):
    def test_sig_changes_when_mtb_threshold_changes(self):
        # Tuning any MTB constant must bump the cache signature so stale
        # MTB segmentation is invalidated.
        tokens = detection.ALGO_SIG.split(",")
        mtb_constants = (
            "_SHUTTLE_SPEED_MIN",
            "_SHUTTLE_WIN_SEC",
            "_SHUTTLE_MIN_GAIN",
            "_MTB_ELEV_THRESHOLD",
            "_MTB_ELEV_MIN_GAIN",
        )
        for name in mtb_constants:
            self.assertIn(str(getattr(detection, name)), tokens,
                          f"{name} must contribute to ALGO_SIG")


class TestStatsComputation(unittest.TestCase):
    def test_compute_algo_stats_all_riding(self):
        per_pt = [{'dt': 0, 'dist': 0, 'speed': 0, 'ele_delta': 0}]
        per_pt += [{'dt': 1, 'dist': 10.0, 'speed': 36, 'ele_delta': 1.0}] * 100
        is_assisted = [False] * len(per_pt)
        s = detection._compute_algo_stats(is_assisted, per_pt)
        self.assertEqual(s["distance_km"], 1.0)
        self.assertEqual(s["elev_gain_m"], 100)
        self.assertEqual(s["assisted_gain_m"], 0)
        self.assertEqual(s["lift_count"], 0)

    def test_compute_algo_stats_lift_count(self):
        # Build: ride 3 pts, lift 3 pts, ride 3 pts, lift 3 pts → 2 lifts
        per_pt = [{'dt': 1, 'dist': 5, 'speed': 10, 'ele_delta': 1.0}] * 12
        flags = [False]*3 + [True]*3 + [False]*3 + [True]*3
        s = detection._compute_algo_stats(flags, per_pt)
        self.assertEqual(s["lift_count"], 2)


if __name__ == "__main__":
    unittest.main()
