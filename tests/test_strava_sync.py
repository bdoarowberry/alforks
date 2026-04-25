"""Tests for sync/strava_sync.py — sport-type auto-mapping behaviour.

Run with:
    python -m unittest tests.test_strava_sync
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sync"))

import strava_sync


class TestSportTypeMapping(unittest.TestCase):
    def test_known_sport_types_map(self):
        cases = [
            ("AlpineSki",         "ski"),
            ("BackcountrySki",    "ski"),
            ("Snowboard",         "snowboard"),
            ("MountainBikeRide",  "mtb"),
            ("EMountainBikeRide", "mtb"),
            ("GravelRide",        "mtb"),
            ("Hike",              "hike"),
            ("Walk",              "hike"),
            ("TrailRun",          "hike"),
        ]
        for sport, expected in cases:
            self.assertEqual(strava_sync._STRAVA_SPORT_TO_TYPE[sport], expected,
                             f"{sport} should map to {expected}")

    def test_unmapped_sport_types_stay_untagged(self):
        # These shouldn't be auto-tagged — Ride is ambiguous (road vs trail),
        # Run could be road, etc.
        for sport in ("Ride", "EBikeRide", "Run", "Yoga", "WeightTraining"):
            self.assertNotIn(sport, strava_sync._STRAVA_SPORT_TO_TYPE)


class TestApplySportTypeTag(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_meta = Path(self._tmpdir.name) / "metadata.json"
        self._patcher = mock.patch.object(strava_sync, "METADATA_FILE", self.tmp_meta)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_creates_metadata_file_when_missing(self):
        applied = strava_sync._apply_sport_type_tag("strava_1.gpx", "MountainBikeRide")
        self.assertEqual(applied, "mtb")
        meta = json.loads(self.tmp_meta.read_text(encoding="utf-8"))
        self.assertEqual(meta["strava_1.gpx"]["type"], "mtb")

    def test_does_not_overwrite_existing_type(self):
        self.tmp_meta.write_text(json.dumps({
            "strava_1.gpx": {"type": "snowboard", "title": "kept"},
        }), encoding="utf-8")
        applied = strava_sync._apply_sport_type_tag("strava_1.gpx", "MountainBikeRide")
        self.assertIsNone(applied)
        meta = json.loads(self.tmp_meta.read_text(encoding="utf-8"))
        # Manual tag respected, title preserved
        self.assertEqual(meta["strava_1.gpx"]["type"], "snowboard")
        self.assertEqual(meta["strava_1.gpx"]["title"], "kept")

    def test_unknown_sport_type_is_noop(self):
        applied = strava_sync._apply_sport_type_tag("strava_1.gpx", "Yoga")
        self.assertIsNone(applied)
        self.assertFalse(self.tmp_meta.exists())

    def test_none_sport_type_is_noop(self):
        applied = strava_sync._apply_sport_type_tag("strava_1.gpx", None)
        self.assertIsNone(applied)


if __name__ == "__main__":
    unittest.main()
