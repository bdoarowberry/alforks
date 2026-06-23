"""Unit tests for the Garmin wellness extractor (sync/garmin_wellness.py).
Fixtures mirror the real Forerunner 245 payload shapes."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sync"))
import garmin_wellness as W


TS = {
    "mostRecentVO2Max": {"generic": {
        "calendarDate": "2026-06-13", "vo2MaxValue": 36.0, "fitnessAge": 58}},
    "latestTrainingStatusData": {
        "3408056794": {"calendarDate": "2026-06-15", "trainingStatus": 3,
                       "weeklyTrainingLoad": 250, "timestamp": 1781503367000,
                       "loadTunnelMin": 67, "loadTunnelMax": 218}},
}
BB = [{"date": "2026-06-15", "charged": 63, "drained": 63,
       "bodyBatteryValuesArray": [[1781503200000, 5], [1781529660000, 80], [1781560000000, 42]]}]
RHR = {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 60.0}]}}}
SLEEP = {"dailySleepDTO": {"sleepTimeSeconds": 27600, "deepSleepSeconds": 7080,
                           "lightSleepSeconds": 17520, "remSleepSeconds": 3000,
                           "awakeSleepSeconds": 0, "sleepScores": None}}
STRESS = {"avgStressLevel": 29, "maxStressLevel": 100}
IM = {"weeklyModerate": 85, "weeklyVigorous": 61}


class TestExtract(unittest.TestCase):
    def test_full_extract(self):
        w = W.extract_wellness("2026-06-15", ts=TS, bb=BB, rhr=RHR,
                               sleep=SLEEP, stress=STRESS, im=IM)
        self.assertEqual(w["vo2max"], 36.0)
        self.assertEqual(w["vo2max_date"], "2026-06-13")
        self.assertEqual(w["fitness_age"], 58)
        self.assertEqual(w["training_status"], 3)
        self.assertEqual(w["weekly_training_load"], 250)
        self.assertEqual(w["bb_charged"], 63)
        self.assertEqual(w["bb_high"], 80)
        self.assertEqual(w["bb_low"], 5)
        self.assertEqual(w["bb_end"], 42)
        self.assertEqual(w["resting_hr"], 60)
        self.assertEqual(w["sleep_sec"], 27600)
        self.assertEqual(w["sleep_deep"], 7080)
        self.assertNotIn("sleep_score", w)          # 245 has no sleep score
        self.assertEqual(w["stress_avg"], 29)
        self.assertEqual(w["im_moderate_wk"], 85)

    def test_empty_payloads_leave_fields_absent(self):
        w = W.extract_wellness("2026-06-15")
        self.assertEqual(w, {"date": "2026-06-15"})

    def test_body_battery_three_element_array(self):
        # Some payloads are [ts, status, level]; level is the LAST element.
        bb = [{"charged": 10, "drained": 5,
               "bodyBatteryValuesArray": [[1, "ACTIVE", 30], [2, "ACTIVE", 70]]}]
        w = W.extract_wellness("d", bb=bb)
        self.assertEqual(w["bb_high"], 70)
        self.assertEqual(w["bb_low"], 30)

    def test_latest_device_status_picks_newest(self):
        ts = {"latestTrainingStatusData": {
            "a": {"trainingStatus": 1, "timestamp": 100},
            "b": {"trainingStatus": 4, "timestamp": 200}}}
        w = W.extract_wellness("d", ts=ts)
        self.assertEqual(w["training_status"], 4)

    def test_missing_vo2max_is_absent(self):
        ts = {"latestTrainingStatusData": {"a": {"trainingStatus": 2, "timestamp": 1}}}
        w = W.extract_wellness("d", ts=ts)
        self.assertNotIn("vo2max", w)
        self.assertEqual(w["training_status"], 2)


if __name__ == "__main__":
    unittest.main()
