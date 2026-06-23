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
        self.assertIn("Setup", msg)
        self.assertNotIn("python", msg)

    def test_garmin_runtime_error(self):
        raw = ("RuntimeError: Garmin auth not available (Username and password are "
               "required). Run `python garmin_sync.py --login` first.")
        msg = app._friendly_sync_message("garmin", raw)
        self.assertIn("Garmin", msg)
        self.assertIn("Setup", msg)
        self.assertNotIn("RuntimeError", msg)
        self.assertNotIn("--login", msg)

    def test_generic_error_is_cleaned_but_preserved(self):
        msg = app._friendly_sync_message(
            "strava", "Traceback...\nConnectionError: timed out reading from api.strava.com")
        self.assertIn("timed out", msg)
        self.assertNotIn("ConnectionError", msg)
        self.assertNotIn("Setup", msg)   # not an auth failure -> keep the real reason

    def test_empty_falls_back(self):
        self.assertEqual(app._friendly_sync_message("garmin", ""), "Garmin sync failed.")


if __name__ == "__main__":
    unittest.main()
