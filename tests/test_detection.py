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

    def test_bbox_prefilter_still_finds_points_near_station(self):
        """Regression: adding the lat/lon bbox pre-filter must not cause
        a true near-station point to be rejected. Craft a track where one
        point is within the 100 m station threshold but far from all
        others, and assert the lift is still detected."""
        # 300 points per_pt: 1 s cadence, 1 m gain each → plausible lift
        per_pt = [{'dt': 0, 'ele_delta': 0}] + \
                 [{'dt': 1, 'ele_delta': 1}] * 299
        # Most points far from the lift (at (50.0, -116.0))
        # Index 10 sits *at a bbox corner* — non-zero Δlat AND Δlon at once,
        # exercising the case where both axis checks must pass simultaneously.
        # Offset 0.0006° lat + 0.0006° lon at latitude 50 is:
        #   sqrt((0.0006 * 111_000)^2 + (0.0006 * 111_000 * cos(50°))^2)
        #   = sqrt(66.6^2 + 42.8^2) ≈ 79 m  — well inside the 100 m threshold.
        latlons = [(49.0, -115.0)] * 300
        latlons[10] = (50.0006, -115.9994)     # ~79 m NE of bottom station
        latlons[120] = (50.0010, -116.0000)    # top station exactly
        lifts = [{"a": (50.0000, -116.0000), "b": (50.0010, -116.0000), "name": "Test"}]
        flags = detection._detect_station_lifts(latlons, per_pt, lifts)
        # Indices 10..120 inclusive should be flagged (lift ride)
        self.assertTrue(flags[10])
        self.assertTrue(flags[120])
        self.assertTrue(all(flags[10:121]))
        # Points far from both stations should remain False
        self.assertFalse(flags[0])
        self.assertFalse(flags[200])


class TestMedianFilter(unittest.TestCase):
    """Regression: the k=5 hot path is a separate implementation from the
    generic path. Assert they produce identical output so a future generic
    tweak can't silently diverge."""

    def _generic_k5(self, values):
        # The exact implementation the hot path replaces, inlined for the test.
        import statistics
        half = 2
        n = len(values)
        out = []
        for i, v in enumerate(values):
            window = [values[j] for j in range(max(0, i - half), min(n, i + half + 1))
                      if values[j] is not None]
            out.append(statistics.median(window) if window else v)
        return out

    def test_k5_matches_generic_on_simple(self):
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual(detection._median_filter(values, k=5), self._generic_k5(values))

    def test_k5_matches_generic_with_nones(self):
        values = [None, 1, 2, None, 5, 4, None, 7, None, 10]
        self.assertEqual(detection._median_filter(values, k=5), self._generic_k5(values))

    def test_k5_empty_preserves_original(self):
        # All None → output keeps the None placeholders
        values = [None, None, None, None, None]
        self.assertEqual(detection._median_filter(values, k=5), values)

    def test_k5_single_value(self):
        self.assertEqual(detection._median_filter([42], k=5), [42])

    def test_k5_even_window_averages(self):
        # Window of 4 non-None values should return mean of the two middle ones
        values = [1, 2, 3, 4]
        # At i=0 the window is [1,2,3] (indices 0..2). With None treated as absent,
        # the window is all 3 values, median = 2. Cross-check with the generic.
        out = detection._median_filter(values, k=5)
        self.assertEqual(out, self._generic_k5(values))


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

    def test_merge_stats_avg_speed_excludes_lift_time(self):
        # 1 km riding in 60 s, 0.1 km "assisted" across 600 s of lift time.
        # Wall-clock avg would include the lift (660 s for 1 km = 5.45 km/h).
        # Riding-only avg: 1 km in 60 s = 60 km/h.
        per_pt = [{'dt': 0, 'dist': 0, 'speed': 0, 'ele_delta': 0}]
        # 10 riding transitions, each 6 s / 100 m
        per_pt += [{'dt': 6, 'dist': 100, 'speed': 60, 'ele_delta': 0}] * 10
        # 10 assisted transitions, each 60 s / 10 m (slow lift)
        per_pt += [{'dt': 60, 'dist': 10, 'speed': 0.6, 'ele_delta': 5.0}] * 10
        flags = [False]*11 + [True]*10
        base = {'duration_sec': 660, 'max_speed_kmh': 60, 'peak_ele_m': 1050}
        stats = detection._merge_stats(flags, per_pt, base)
        # Numerator is riding distance only (1.0 km), denominator is riding
        # time only (60 s). 1.0 / (60/3600) = 60.0 km/h.
        self.assertEqual(stats['avg_speed_kmh'], 60.0)
        # duration_sec is preserved from base_stats (wall-clock).
        self.assertEqual(stats['duration_sec'], 660)


if __name__ == "__main__":
    unittest.main()
