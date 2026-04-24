"""Tests for cache_utils — atomic write, backup snapshots, LRU.

Run with:
    python -m unittest tests.test_cache_utils
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache_utils


class TestAtomicWriteRetry(unittest.TestCase):
    """Regression: _atomic_write retries os.replace up to 6 times to work
    around OneDrive's briefly-opened-for-upload lock on Windows. If every
    attempt fails it must re-raise the PermissionError, not swallow it."""

    def test_retry_exhaustion_reraises(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "out.json"
            with mock.patch("cache_utils.os.replace",
                            side_effect=PermissionError("simulated OneDrive lock")) as m, \
                 mock.patch("cache_utils.time.sleep"):
                with self.assertRaises(PermissionError):
                    cache_utils._atomic_write(target, "payload")
            # Six attempts: the initial try plus five retries
            self.assertEqual(m.call_count, 6)

    def test_transient_permission_error_recovers(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "out.json"
            call_count = {"n": 0}
            real_replace = os.replace

            def flaky_replace(src, dst):
                call_count["n"] += 1
                if call_count["n"] < 3:
                    raise PermissionError("transient")
                return real_replace(src, dst)

            with mock.patch("cache_utils.os.replace", side_effect=flaky_replace):
                cache_utils._atomic_write(target, "payload")
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "payload")
            self.assertEqual(call_count["n"], 3)

class TestLRUCache(unittest.TestCase):
    def test_get_missing_returns_none(self):
        c = cache_utils.LRUCache(maxsize=3)
        self.assertIsNone(c.get("nope"))

    def test_eviction_order(self):
        c = cache_utils.LRUCache(maxsize=2)
        c.set("a", {"v": 1})
        c.set("b", {"v": 2})
        c.set("c", {"v": 3})
        self.assertIsNone(c.get("a"), "oldest entry should be evicted")
        self.assertIsNotNone(c.get("b"))
        self.assertIsNotNone(c.get("c"))

    def test_get_refreshes_recency(self):
        c = cache_utils.LRUCache(maxsize=2)
        c.set("a", {"v": 1})
        c.set("b", {"v": 2})
        c.get("a")          # touching "a" makes "b" the oldest
        c.set("c", {"v": 3})  # evicts "b"
        self.assertIsNotNone(c.get("a"))
        self.assertIsNone(c.get("b"))


if __name__ == "__main__":
    unittest.main()
