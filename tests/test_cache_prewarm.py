"""Tests for the OSM-lifts + TZ-LRU disk-cache prewarm.

Storm scenario (ALGO_SIG bump, ~554 GPX files re-parsing) used to pay a
disk read per file for the same OSM/TZ cache entries. The prewarm
pre-loads both caches into memory so workers hit a dict instead of disk.

Run with:
    python -m unittest tests.test_cache_prewarm
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

import app


def _reset_state():
    """Clear all in-memory caches and the prewarm flag so each test starts
    from cold state. Module-level globals would otherwise leak between
    tests run in the same process."""
    app._OSM_LIFT_MEM_CACHE.clear()
    app._TZ_LRU.clear()
    app._TZ_LRU_FAIL.clear()
    app._DISK_CACHES_PREWARMED = False


class TestOsmMemCache(unittest.TestCase):
    """`_try_read_osm_cache` must consult the mem dict first, fall back to
    disk on miss, and populate the dict so subsequent reads short-circuit."""

    def setUp(self):
        _reset_state()
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_cache_dir = app.CACHE_DIR
        app.CACHE_DIR = Path(self._tmp.name)
        (app.CACHE_DIR / "lifts").mkdir(parents=True)

    def tearDown(self):
        app.CACHE_DIR = self._orig_cache_dir
        self._tmp.cleanup()
        _reset_state()

    def _write_cache_file(self, bbox, lifts, fetched=None):
        cp = app._lift_cache_path(bbox)
        cp.write_text(json.dumps({
            "fetched": fetched if fetched is not None else time.time(),
            "lifts":   lifts,
        }), encoding="utf-8")
        return cp

    def test_disk_hit_populates_mem(self):
        bbox = (50.0, -116.0, 50.1, -115.9)
        lifts = [{"name": "Alpine", "a": [50.05, -115.95], "b": [50.06, -115.94]}]
        cp = self._write_cache_file(bbox, lifts)
        self.assertNotIn(cp.stem, app._OSM_LIFT_MEM_CACHE)
        result = app._try_read_osm_cache(cp)
        self.assertEqual(result, lifts)
        self.assertEqual(app._OSM_LIFT_MEM_CACHE[cp.stem], lifts)

    def test_mem_hit_short_circuits_disk(self):
        # Populate mem cache, then delete the disk file. Read must still
        # return the mem value — proves the disk read was skipped.
        bbox = (49.0, -120.0, 49.1, -119.9)
        lifts = [{"name": "Phantom"}]
        cp = self._write_cache_file(bbox, lifts)
        app._try_read_osm_cache(cp)  # warm mem
        cp.unlink()
        self.assertFalse(cp.exists())
        result = app._try_read_osm_cache(cp)
        self.assertEqual(result, lifts)

    def test_expired_disk_entry_not_loaded(self):
        bbox = (51.0, -114.0, 51.1, -113.9)
        long_ago = time.time() - app._LIFT_CACHE_TTL_SEC - 60
        cp = self._write_cache_file(bbox, [{"x": 1}], fetched=long_ago)
        self.assertIsNone(app._try_read_osm_cache(cp))
        self.assertNotIn(cp.stem, app._OSM_LIFT_MEM_CACHE)


class TestPrewarm(unittest.TestCase):
    """`_prewarm_disk_caches` loads both lift + weather dirs into the
    in-memory dicts. Idempotent, tolerant of missing dirs / corrupt files."""

    def setUp(self):
        _reset_state()
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_cache_dir = app.CACHE_DIR
        app.CACHE_DIR = Path(self._tmp.name)
        (app.CACHE_DIR / "lifts").mkdir(parents=True)
        (app.CACHE_DIR / "weather").mkdir(parents=True)

    def tearDown(self):
        app.CACHE_DIR = self._orig_cache_dir
        self._tmp.cleanup()
        _reset_state()

    def _write_lift(self, bbox, lifts, fetched=None):
        cp = app._lift_cache_path(bbox)
        cp.write_text(json.dumps({
            "fetched": fetched if fetched is not None else time.time(),
            "lifts":   lifts,
        }), encoding="utf-8")
        return cp

    def _write_weather(self, lat, lon, date_str, *, tz_name=None, key=None):
        cp = app._weather_cache_path(lat, lon, date_str)
        entry = {"fetched": int(time.time())}
        if tz_name is not None:
            entry["timezone_name"] = tz_name
        if key is not None:
            entry["tz_lru_key"] = key
        cp.write_text(json.dumps(entry), encoding="utf-8")
        return cp

    def test_loads_fresh_lifts(self):
        cp1 = self._write_lift((50.0, -116.0, 50.1, -115.9), [{"a": 1}])
        cp2 = self._write_lift((49.0, -120.0, 49.1, -119.9), [{"b": 2}])
        app._prewarm_disk_caches()
        self.assertEqual(app._OSM_LIFT_MEM_CACHE[cp1.stem], [{"a": 1}])
        self.assertEqual(app._OSM_LIFT_MEM_CACHE[cp2.stem], [{"b": 2}])

    def test_skips_expired_lifts(self):
        cp = self._write_lift((52.0, -110.0, 52.1, -109.9), [{"old": 1}],
                              fetched=time.time() - app._LIFT_CACHE_TTL_SEC - 100)
        app._prewarm_disk_caches()
        self.assertNotIn(cp.stem, app._OSM_LIFT_MEM_CACHE)

    def test_loads_tz_with_key(self):
        # `tz_lru_key` is the rounded (lat, lon) pair. Entries with it are
        # back-loadable into `_TZ_LRU` without parsing any GPX.
        self._write_weather(50.12, -115.93, "2024-01-15",
                            tz_name="America/Edmonton",
                            key=[50.12, -115.93])
        app._prewarm_disk_caches()
        self.assertEqual(app._TZ_LRU.get((50.12, -115.93)), "America/Edmonton")

    def test_skips_tz_without_key(self):
        # Old-format entries (pre-`tz_lru_key`) can't be back-loaded; they
        # upgrade lazily on next network fetch. Must not crash the prewarm.
        self._write_weather(48.0, -123.0, "2023-06-01",
                            tz_name="America/Vancouver", key=None)
        app._prewarm_disk_caches()
        self.assertNotIn((48.0, -123.0), app._TZ_LRU)

    def test_corrupt_file_does_not_break_prewarm(self):
        # One broken JSON next to a valid one — valid entries must still
        # load. A single bad cache file should never cascade.
        cp_bad = app.CACHE_DIR / "lifts" / "bad.json"
        cp_bad.write_text("{not json", encoding="utf-8")
        cp_good = self._write_lift((47.0, -121.0, 47.1, -120.9), [{"ok": 1}])
        app._prewarm_disk_caches()
        self.assertEqual(app._OSM_LIFT_MEM_CACHE[cp_good.stem], [{"ok": 1}])

    def test_idempotent(self):
        # Second call must not re-scan. We detect this by mutating the
        # mem dict between calls — a re-scan would overwrite our mutation
        # back to the on-disk value.
        cp = self._write_lift((46.0, -122.0, 46.1, -121.9), [{"orig": 1}])
        app._prewarm_disk_caches()
        app._OSM_LIFT_MEM_CACHE[cp.stem] = [{"mutated": 1}]
        app._prewarm_disk_caches()
        self.assertEqual(app._OSM_LIFT_MEM_CACHE[cp.stem], [{"mutated": 1}])

    def test_missing_dirs_does_not_crash(self):
        # Fresh install case: cache subdirs don't exist yet. Prewarm
        # must succeed silently rather than crashing the sidebar build.
        import shutil
        shutil.rmtree(app.CACHE_DIR / "lifts")
        shutil.rmtree(app.CACHE_DIR / "weather")
        app._prewarm_disk_caches()  # no exception


if __name__ == "__main__":
    unittest.main()
