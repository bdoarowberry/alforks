"""GUI-friendly translation of sync-script failures.

The CLI sync scripts print things like "Not logged in. Run: python
strava_sync.py --login" or a raw "RuntimeError: ...". In the GUI the user
connects on the Setup page, so _friendly_sync_message must surface that instead
of a stack-trace line or a terminal command.
"""
import unittest

import app


class TestFriendlySyncMessage(unittest.TestCase):
    def test_strava_not_logged_in(self):
        msg = app._friendly_sync_message(
            "strava", "Not logged in. Run: python strava_sync.py --login")
        self.assertIn("Strava", msg)
        self.assertIn("Settings", msg)
        self.assertNotIn("python", msg)

    def test_strava_token_refresh_failure_routes_to_reconnect(self):
        # A revoked/expired refresh token must not leak the raw API dict.
        raw = ("Strava login expired — please reconnect on the Setup page. "
               "(token refresh failed: {'message': 'Bad Request', 'errors': [...]})")
        msg = app._friendly_sync_message("strava", raw)
        self.assertIn("Settings", msg)
        self.assertNotIn("Bad Request", msg)
        self.assertNotIn("{", msg)

    def test_garmin_runtime_error(self):
        raw = ("RuntimeError: Garmin auth not available (Username and password are "
               "required). Run `python garmin_sync.py --login` first.")
        msg = app._friendly_sync_message("garmin", raw)
        self.assertIn("Garmin", msg)
        self.assertIn("Settings", msg)
        self.assertNotIn("RuntimeError", msg)
        self.assertNotIn("--login", msg)

    def test_network_error_gets_friendly_connection_message(self):
        # A timeout / connection failure is not the user's fault and not a stale
        # login — surface a plain "couldn't reach" hint instead of leaking the
        # raw "ConnectionError: timed out ..." line.
        msg = app._friendly_sync_message(
            "strava", "Traceback...\nConnectionError: timed out reading from api.strava.com")
        self.assertNotIn("ConnectionError", msg)
        self.assertNotIn("timed out", msg)
        self.assertIn("Strava", msg)
        self.assertIn("try again", msg.lower())
        self.assertNotIn("Settings", msg)   # not an auth failure

    def test_server_5xx_gets_friendly_connection_message(self):
        msg = app._friendly_sync_message(
            "strava", "list activities page 1 failed: HTTP Error 503: Service Unavailable")
        self.assertNotIn("503", msg)
        self.assertIn("Strava", msg)
        self.assertIn("try again", msg.lower())

    def test_generic_error_is_cleaned_but_preserved(self):
        # A non-auth, non-network failure still shows its real reason (cleaned).
        msg = app._friendly_sync_message(
            "strava", "Traceback...\nRuntimeError: could not write GPX file: disk full")
        self.assertIn("disk full", msg)
        self.assertNotIn("RuntimeError", msg)
        self.assertNotIn("Settings", msg)   # not an auth failure -> keep the real reason

    def test_garmin_rate_limit_message(self):
        # The Garmin sync now exits non-zero on an all-429 run; the GUI should
        # tell the user to wait, not leak the 429 or claim success.
        msg = app._friendly_sync_message(
            "garmin", "Garmin is rate-limiting requests (HTTP 429 Too Many "
                      "Requests) — wait a few minutes and sync again.")
        self.assertIn("rate-limiting", msg.lower())
        self.assertNotIn("Settings", msg)   # not an auth failure

    def test_empty_falls_back(self):
        self.assertEqual(app._friendly_sync_message("garmin", ""), "Garmin sync failed.")


if __name__ == "__main__":
    unittest.main()
