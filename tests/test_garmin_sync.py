"""Garmin sync: a run where every date failed (rate-limit / auth / network)
must NOT report false success. It should exit non-zero with a reason the app's
friendly-message mapper can route. Partial success is still success.
"""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sync"))
import garmin_sync as gs


class TestSystemicFailureMessage(unittest.TestCase):
    def test_all_failed_rate_limit(self):
        msg = gs.systemic_failure_message(
            5, 0, "GarminConnectTooManyRequestsError: 429 Client Error")
        self.assertIsNotNone(msg)
        self.assertIn("rate-limiting", msg.lower())

    def test_all_failed_auth(self):
        msg = gs.systemic_failure_message(
            3, 0, "GarminConnectAuthenticationError: 401 Unauthorized")
        self.assertIsNotNone(msg)
        self.assertIn("Setup", msg)

    def test_all_failed_generic_network(self):
        msg = gs.systemic_failure_message(2, 0, "ConnectionError: timed out")
        self.assertIsNotNone(msg)
        self.assertIn("Couldn't reach Garmin", msg)

    def test_partial_success_is_not_systemic(self):
        # Some dates landed HR — don't fail the whole run.
        self.assertIsNone(gs.systemic_failure_message(4, 1, "429"))

    def test_no_failures_is_not_systemic(self):
        self.assertIsNone(gs.systemic_failure_message(0, 10, ""))


class TestFetchErrorSurfacesReason(unittest.TestCase):
    def test_fetch_failure_returns_error_string(self):
        class BoomClient:
            def get_heart_rates(self, date_str):
                raise RuntimeError("429 Too Many Requests")

        ok, n, err = gs.fetch_and_cache_hr(BoomClient(), "2026-06-01")
        self.assertFalse(ok)
        self.assertEqual(n, 0)
        self.assertIn("429", err)


if __name__ == "__main__":
    unittest.main()
