"""Tests for duplicate-group dismissal persistence (the "Not duplicates" action).

Regression: dup_dismissals.json had picked up the Windows *Hidden* attribute, and
the old raw `Path.write_text` (open(path,'w')) raised PermissionError [Errno 13]
truncating a hidden file — surfacing as "Failed to dismiss: dismiss failed". The
fix routes the save through `_atomic_write` (temp + os.replace), which overwrites
hidden targets and clears the stray bit.
"""
import ctypes
import json
import sys
import tempfile
import unittest
from pathlib import Path

import app

FILE_ATTRIBUTE_HIDDEN = 0x02
FILE_ATTRIBUTE_NORMAL = 0x80


class TestDupDismissPersistence(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._target = Path(self._dir) / "dup_dismissals.json"
        self._orig = app.DUP_DISMISSALS_FILE
        app.DUP_DISMISSALS_FILE = self._target

    def tearDown(self):
        app.DUP_DISMISSALS_FILE = self._orig
        if sys.platform == "win32" and self._target.exists():
            ctypes.windll.kernel32.SetFileAttributesW(str(self._target), FILE_ATTRIBUTE_NORMAL)
        self._target.unlink(missing_ok=True)

    def test_round_trip(self):
        app._save_dup_dismissals({("a.gpx", "b.gpx"), ("c.gpx", "d.gpx")})
        self.assertEqual(
            app._load_dup_dismissals(), {("a.gpx", "b.gpx"), ("c.gpx", "d.gpx")})

    @unittest.skipUnless(sys.platform == "win32", "Hidden-attribute bug is Windows-only")
    def test_save_overwrites_hidden_file(self):
        # Seed an existing hidden file (the exact state that broke dismissal).
        self._target.write_text("[]", encoding="utf-8")
        ctypes.windll.kernel32.SetFileAttributesW(str(self._target), FILE_ATTRIBUTE_HIDDEN)
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(self._target))
        self.assertTrue(attrs & FILE_ATTRIBUTE_HIDDEN, "precondition: file is hidden")

        app._save_dup_dismissals({("x.gpx", "y.gpx")})  # must not raise PermissionError

        self.assertEqual(app._load_dup_dismissals(), {("x.gpx", "y.gpx")})
        attrs2 = ctypes.windll.kernel32.GetFileAttributesW(str(self._target))
        self.assertFalse(attrs2 & FILE_ATTRIBUTE_HIDDEN, "replacement file is no longer hidden")

    def test_load_missing_file_is_empty(self):
        self.assertEqual(app._load_dup_dismissals(), set())


if __name__ == "__main__":
    unittest.main()
