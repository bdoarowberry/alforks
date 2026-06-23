"""Unit tests for the training-load math (training.py)."""
import math
import unittest

import training as T


class TestRideLoad(unittest.TestCase):
    def test_hr_load_zone_weighting(self):
        # 10 min in each bucket; weights (0,1,2,3,5) -> 0+10+20+30+50 = 110.
        self.assertAlmostEqual(T.hr_load([600, 600, 600, 600, 600]), 110.0)

    def test_hr_load_recovery_zone_is_free(self):
        # All time in <60% bucket contributes nothing.
        self.assertEqual(T.hr_load([3600, 0, 0, 0, 0]), 0.0)

    def test_hr_load_empty(self):
        self.assertEqual(T.hr_load([]), 0.0)
        self.assertEqual(T.hr_load(None), 0.0)

    def test_surrogate_no_climb(self):
        # 60 min at normalizer MET, climb rate at the knee -> 60 effort-min.
        self.assertAlmostEqual(
            T.surrogate_load(3600, 300, 10, T._MET_NORMALIZER), 60.0)

    def test_surrogate_climb_boost(self):
        # 60 m/km is 30 over the knee -> 1 + 30/120 = 1.25x.
        self.assertAlmostEqual(
            T.surrogate_load(3600, 600, 10, T._MET_NORMALIZER), 75.0)

    def test_surrogate_zero_duration(self):
        self.assertEqual(T.surrogate_load(0, 100, 10, 8.5), 0.0)

    def test_calibrate_k_median_ratio(self):
        self.assertAlmostEqual(T.calibrate_k([(110, 55), (100, 50), (90, 30)]), 2.0)

    def test_calibrate_k_empty_is_one(self):
        self.assertEqual(T.calibrate_k([]), 1.0)
        self.assertEqual(T.calibrate_k([(0, 0), (5, 0)]), 1.0)

    def test_ride_load_prefers_hr(self):
        self.assertAlmostEqual(
            T.ride_load(True, [0, 600, 0, 0, 0], 3600, 0, 0, 8.5, k=2.0), 10.0)

    def test_ride_load_surrogate_when_no_hr(self):
        # No HR -> k * surrogate. surrogate(60min, knee climb)=60, k=2 -> 120.
        self.assertAlmostEqual(
            T.ride_load(False, None, 3600, 300, 10, T._MET_NORMALIZER, k=2.0), 120.0)

    def test_ride_load_surrogate_when_hr_all_zero(self):
        # has_hr flag but no actual zone time -> fall back to surrogate.
        self.assertAlmostEqual(
            T.ride_load(True, [0, 0, 0, 0, 0], 3600, 300, 10, T._MET_NORMALIZER, k=1.0), 60.0)


class TestSeries(unittest.TestCase):
    def test_ewma_converges_to_constant(self):
        out = T.ewma([10.0] * 400, T.CTL_TAU)
        self.assertLess(out[-1], 10.0)            # seeded at 0, approaches 10
        self.assertGreater(out[-1], 9.9)
        self.assertTrue(all(b >= a for a, b in zip(out, out[1:])))  # monotonic up

    def test_ewma_single_step(self):
        lam = 1 - math.exp(-1 / 7)
        self.assertAlmostEqual(T.ewma([10.0], 7.0)[0], 10.0 * lam)

    def test_fatigue_reacts_faster_than_fitness(self):
        loads = [0.0] * 30 + [100.0] * 7      # a hard week after rest
        fit, fat, form = T.fitness_fatigue_form(loads)
        self.assertGreater(fat[-1], fit[-1])  # 7d fatigue outruns 42d fitness
        self.assertLess(form[-1], 0.0)        # form goes negative under load

    def test_form_is_lagged_fitness_minus_fatigue(self):
        loads = [10.0, 20.0, 30.0, 40.0]
        fit, fat, form = T.fitness_fatigue_form(loads)
        self.assertEqual(form[0], 0.0)
        self.assertAlmostEqual(form[2], fit[1] - fat[1])

    def test_acwr_none_until_full_chronic_window(self):
        loads = [10.0] * 20
        self.assertIsNone(T.acwr(loads, 19))      # only 20 days, need 28
        loads = [10.0] * 30
        self.assertAlmostEqual(T.acwr(loads, 29), 1.0)  # steady -> ratio 1.0

    def test_acwr_spike_pushes_ratio_up(self):
        loads = [10.0] * 28 + [50.0] * 7          # recent week much harder
        self.assertGreater(T.acwr(loads, len(loads) - 1), 1.3)


if __name__ == "__main__":
    unittest.main()
