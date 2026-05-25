"""Tests for sidebar_cache — per-file sidebar entry persistence.

Run with:
    python -m unittest tests.test_sidebar_cache
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sidebar_cache


def _fp(**overrides) -> str:
    """Fingerprint with sensible defaults; pass overrides to mutate one input."""
    base = dict(
        gpx_mtime=1_700_000_000.0,
        file_meta={"type": "mtb", "title": "Old Goat"},
        regions_mtime=1_600_000_000.0,
        types_mtime=1_500_000_000.0,
        algo_sig="ALGO-v7",
        region_match_version=2,
    )
    base.update(overrides)
    return sidebar_cache.sidebar_fingerprint(**base)


class TestFingerprint(unittest.TestCase):
    def test_stable_across_calls(self):
        self.assertEqual(_fp(), _fp())

    def test_gpx_mtime_changes_fingerprint(self):
        self.assertNotEqual(_fp(), _fp(gpx_mtime=1_700_000_001.0))

    def test_meta_changes_fingerprint(self):
        self.assertNotEqual(_fp(), _fp(file_meta={"type": "snowboard"}))

    def test_regions_mtime_changes_fingerprint(self):
        self.assertNotEqual(_fp(), _fp(regions_mtime=1_600_000_001.0))

    def test_types_mtime_changes_fingerprint(self):
        self.assertNotEqual(_fp(), _fp(types_mtime=1_500_000_001.0))

    def test_algo_sig_changes_fingerprint(self):
        # Bumping the algorithm signature must invalidate every entry —
        # the whole point of including it in the key.
        self.assertNotEqual(_fp(), _fp(algo_sig="ALGO-v8"))

    def test_region_match_version_changes_fingerprint(self):
        self.assertNotEqual(_fp(), _fp(region_match_version=3))

    def test_meta_key_order_does_not_matter(self):
        # json.dumps with sort_keys collapses dict ordering — same content
        # in different insertion order should still produce the same FP.
        a = _fp(file_meta={"type": "mtb", "title": "A"})
        b = _fp(file_meta={"title": "A", "type": "mtb"})
        self.assertEqual(a, b)


class TestReadWriteRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.sidebar_dir = root / "sidebar"
        self.hr_dir      = root / "hr"
        self.hr_dir.mkdir()
        self.entry = {
            "filename": "ride_2026-05-20.gpx",
            "name":     "Sunday rip",
            "date":     "2026-05-20T10:00:00",
            "stats":    {"distance_km": 12.3, "duration_sec": 3600},
            "start_latlon": [50.1, -115.2],
            "has_hr":   False,
        }
        self.fp = "deadbeefcafebabe"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self):
        sidebar_cache.write_sidebar_entry(
            sidebar_cache_dir=self.sidebar_dir, hr_cache_dir=self.hr_dir,
            filename=self.entry["filename"], entry=self.entry, fp=self.fp,
        )

    def _read(self, fp: str | None = None):
        return sidebar_cache.read_sidebar_entry(
            sidebar_cache_dir=self.sidebar_dir, hr_cache_dir=self.hr_dir,
            filename=self.entry["filename"], expected_fp=fp or self.fp,
        )

    def test_round_trip(self):
        self._write()
        result = self._read()
        self.assertIsNotNone(result)
        entry, aux = result
        self.assertEqual(entry, self.entry)
        # Aux's _start_latlon comes back as a tuple so callers can match
        # the original `_build_activity_entry` aux shape.
        self.assertEqual(aux, {"_start_latlon": (50.1, -115.2)})

    def test_missing_file_returns_none(self):
        self.assertIsNone(self._read())

    def test_stale_fingerprint_returns_none(self):
        self._write()
        self.assertIsNone(self._read(fp="not-the-same"))

    def test_corrupt_json_returns_none(self):
        self._write()
        path = self.sidebar_dir / f"{self.entry['filename']}.json"
        path.write_text("{not valid json", encoding="utf-8")
        self.assertIsNone(self._read())

    def test_hr_mtime_change_invalidates(self):
        # Persist the entry while no HR file exists for its date.
        self._write()
        self.assertIsNotNone(self._read())
        # Now an HR file appears for that date — `has_hr` could legitimately
        # flip, so the cached row must be considered stale even though the
        # input fingerprint is unchanged.
        (self.hr_dir / "2026-05-20.json").write_text("[]", encoding="utf-8")
        self.assertIsNone(self._read())

    def test_entry_without_start_latlon(self):
        entry = dict(self.entry)
        entry.pop("start_latlon")
        sidebar_cache.write_sidebar_entry(
            sidebar_cache_dir=self.sidebar_dir, hr_cache_dir=self.hr_dir,
            filename=entry["filename"], entry=entry, fp=self.fp,
        )
        result = sidebar_cache.read_sidebar_entry(
            sidebar_cache_dir=self.sidebar_dir, hr_cache_dir=self.hr_dir,
            filename=entry["filename"], expected_fp=self.fp,
        )
        self.assertIsNotNone(result)
        _, aux = result
        self.assertEqual(aux, {})

    def test_delete_removes_file(self):
        self._write()
        path = self.sidebar_dir / f"{self.entry['filename']}.json"
        self.assertTrue(path.exists())
        sidebar_cache.delete_sidebar_entry(self.sidebar_dir, self.entry["filename"])
        self.assertFalse(path.exists())

    def test_delete_missing_is_silent(self):
        # No write first — deleting a never-persisted entry must not raise.
        sidebar_cache.delete_sidebar_entry(self.sidebar_dir, "nope.gpx")


class TestHrFileMtime(unittest.TestCase):
    def test_missing_date_returns_negative_one(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sidebar_cache.hr_file_mtime(Path(d), ""), -1.0)

    def test_missing_file_returns_negative_one(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                sidebar_cache.hr_file_mtime(Path(d), "2026-05-20"), -1.0)

    def test_present_file_returns_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "2026-05-20.json"
            p.write_text("[]", encoding="utf-8")
            self.assertAlmostEqual(
                sidebar_cache.hr_file_mtime(Path(d), "2026-05-20"),
                p.stat().st_mtime,
                places=3,
            )


if __name__ == "__main__":
    unittest.main()
