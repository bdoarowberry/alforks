"""Regression tests for bugs fixed in the review pass.

Run with:
    python -m unittest tests.test_smoothing
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Importing app triggers Flask route registration + a few on-disk reads. That's
# OK for these tests — the prewarm thread is gated behind ALFORKS_PREWARM and
# won't spin up on import.
import app


class TestApplySmoothing(unittest.TestCase):
    """Regression: _apply_smoothing used to crash with a KeyError because it
    called haversine(dict, dict) instead of haversine((lat, lon), (lat, lon))."""

    def _make_activity(self, n_pts: int = 20) -> dict:
        """Build a minimal activity-shape dict with n_pts points."""
        pts = []
        for i in range(n_pts):
            pts.append({
                "lat":     50.0 + i * 0.00005,
                "lon":     -116.0 + i * 0.00005,
                "ele":     1000 + i,
                "time":    f"2024-01-01T10:{i // 60:02d}:{i % 60:02d}",
                "dist_km": i * 0.005,
                "speed":   20.0,
            })
        return {
            "filename": "test.gpx",
            "name":     "test",
            "date":     "2024-01-01T10:00:00",
            "bbox":     [50.0, -116.0, 50.001, -115.999],
            "points":   pts,
            "segments": [{"type": "riding", "start": 0, "end": n_pts - 1}],
            "stats": {"distance_km": 0.1, "duration_sec": 20,
                      "elev_gain_m": 19, "elev_loss_m": 0,
                      "assisted_gain_m": 0, "avg_speed_kmh": 18,
                      "max_speed_kmh": 20, "lift_count": 0,
                      "peak_ele_m": 1019},
        }

    def test_smoothing_does_not_crash(self):
        data = self._make_activity(20)
        # Previously threw KeyError inside haversine
        out = app._apply_smoothing(data, 3)
        self.assertIn("points", out)
        self.assertEqual(len(out["points"]), 20)

    def test_smoothing_preserves_point_count(self):
        data = self._make_activity(50)
        out = app._apply_smoothing(data, 5)
        self.assertEqual(len(out["points"]), 50)

    def test_smoothing_window_of_1_is_noop(self):
        data = self._make_activity(10)
        out = app._apply_smoothing(data, 1)
        self.assertIs(out, data)  # Returns the same dict — no smoothing

    def test_smoothing_range_leaves_outside_untouched(self):
        """Range-limited smoothing: points with dist_km outside [start_km, end_km]
        keep their raw lat/lon; points inside are replaced with the moving avg."""
        # Activity has 50 points with dist_km at i*0.005 = 0..0.245 km
        data = self._make_activity(50)
        # Inject a spike in the middle — smoothing should kill it only
        # when the point is inside the range
        data["points"][25]["lat"] += 0.01
        raw_before = [p["lat"] for p in data["points"]]

        # Smooth 0.10–0.15 km (indices ~20..30); leave indices 0..19 and 31..49 raw
        out = app._apply_smoothing(data, {"window": 5, "start_km": 0.10, "end_km": 0.15})
        new_lats = [p["lat"] for p in out["points"]]

        # Well outside the zone → unchanged
        self.assertAlmostEqual(new_lats[0], raw_before[0])
        self.assertAlmostEqual(new_lats[49], raw_before[49])
        # Spike inside the zone is attenuated
        self.assertNotAlmostEqual(new_lats[25], raw_before[25], places=4)
        # `smoothing_applied` surfaces the range back to the client
        applied = out["smoothing_applied"]
        self.assertEqual(applied["window"], 5)
        self.assertAlmostEqual(applied["start_km"], 0.10)
        self.assertAlmostEqual(applied["end_km"], 0.15)

    def test_smoothing_accepts_legacy_int_signature(self):
        """Backend still accepts a bare int for the legacy call shape."""
        data = self._make_activity(20)
        out = app._apply_smoothing(data, 3)
        self.assertEqual(len(out["points"]), 20)
        # Whole-track smoothing records just the window (no range)
        self.assertEqual(out["smoothing_applied"], {"window": 3})


class TestDebugHrDateValidation(unittest.TestCase):
    """Regression: /debug/hr/<date_str> used to pass date_str straight into a
    filesystem path. Now validated against YYYY-MM-DD."""

    def test_invalid_date_rejected(self):
        client = app.app.test_client()
        # Reaches the handler but fails the date-format regex
        resp = client.get("/debug/hr/notadate")
        self.assertEqual(resp.status_code, 400)

    def test_wrong_format_rejected(self):
        client = app.app.test_client()
        resp = client.get("/debug/hr/2024-1-1")
        self.assertEqual(resp.status_code, 400)


class TestUnparseableSentinel(unittest.TestCase):
    """Regression: a file that parse_gpx can't handle (e.g. 1-point track)
    used to be re-parsed on every request. get_activity now caches an
    _UNPARSEABLE sentinel so the second call returns None without touching
    the parser."""

    def test_unparseable_is_cached_and_returns_none(self):
        import tempfile
        from pathlib import Path

        # Write a minimal 1-point GPX inside GPX_DIR so _safe_gpx_path accepts it
        fname = "_unparseable_regression.gpx"
        fpath = app.GPX_DIR / fname
        fpath.write_text(
            '<?xml version="1.0"?>\n'
            '<gpx version="1.1"><trk><trkseg>'
            '<trkpt lat="50.0" lon="-116.0"><ele>1000</ele></trkpt>'
            '</trkseg></trk></gpx>',
            encoding="utf-8",
        )
        try:
            # Clear any existing cache entry
            app._mem_cache.cache.pop(fname, None) if hasattr(app._mem_cache, "cache") else None

            call_count = {"n": 0}
            real_parse_gpx = app.parse_gpx
            def counting_parse(path):
                call_count["n"] += 1
                return real_parse_gpx(path)
            app.parse_gpx = counting_parse
            try:
                self.assertIsNone(app.get_activity(fname))
                self.assertIsNone(app.get_activity(fname))
                self.assertEqual(call_count["n"], 1,
                                 "parse_gpx must only be called once; the sentinel should short-circuit")
            finally:
                app.parse_gpx = real_parse_gpx
        finally:
            fpath.unlink(missing_ok=True)


class TestOutOfOrderTimestamps(unittest.TestCase):
    """Regression: a backwards-jumping timestamp (dt < 0) used to pass the
    dt > 0 speed guard but still get its haversine distance accumulated,
    silently inflating total_dist / riding_dist. Now dt < 0 zeroes both."""

    def _write_gpx(self, pts: list[tuple[float, float, str]]) -> "Path":
        from pathlib import Path
        lines = ['<?xml version="1.0"?>',
                 '<gpx version="1.1"><trk><trkseg>']
        for lat, lon, t in pts:
            lines.append(f'<trkpt lat="{lat}" lon="{lon}"><ele>1000</ele><time>{t}</time></trkpt>')
        lines.append('</trkseg></trk></gpx>')
        fname = "_ooo_ts_regression.gpx"
        fpath: Path = app.GPX_DIR / fname
        fpath.write_text("\n".join(lines), encoding="utf-8")
        return fpath

    def test_out_of_order_timestamp_does_not_inflate_distance(self):
        # 3 points. First step moves ~111 m; second step would move another
        # ~111 m but its timestamp is BEFORE the previous — dt < 0. The
        # distance for that step must not be added to total_dist.
        pts = [
            (50.000, -116.000, "2024-01-01T10:00:00Z"),
            (50.001, -116.000, "2024-01-01T10:00:10Z"),
            (50.002, -116.000, "2024-01-01T10:00:05Z"),  # time went backwards
        ]
        fpath = self._write_gpx(pts)
        try:
            data = app.parse_gpx(fpath)
            self.assertIsNotNone(data)
            # First step only: ~111 m → ~0.111 km. If the bug regressed,
            # distance would be ~222 m.
            self.assertLess(data["stats"]["distance_km"], 0.15,
                            "out-of-order step must not contribute to distance")
        finally:
            fpath.unlink(missing_ok=True)


class TestStatsFromTrimmedBoundary(unittest.TestCase):
    """Regression: _build_segments shares the transition index between adjacent
    segments for rendering continuity. _stats_from_trimmed used to iterate
    [start, end+1) inclusive of start, which mis-attributed the boundary
    point's delta to the wrong segment type. Fix: iterate [start+1, end+1)."""

    def test_boundary_delta_is_not_double_counted(self):
        # 4 points: small riding climb, then a sharp lift gain, then a lift
        # tail. If boundary is counted as assisted, assisted_gain picks up
        # the riding 5 m and reports 60; correct answer is 55.
        pts = [
            {"lat": 0, "lon": 0, "ele": 1000, "dist_km": 0.0,  "time": "2024-01-01T10:00:00", "speed": 10},
            {"lat": 0, "lon": 0, "ele": 1005, "dist_km": 0.1,  "time": "2024-01-01T10:00:10", "speed": 10},
            {"lat": 0, "lon": 0, "ele": 1055, "dist_km": 0.15, "time": "2024-01-01T10:01:00", "speed": 2},
            {"lat": 0, "lon": 0, "ele": 1060, "dist_km": 0.2,  "time": "2024-01-01T10:02:00", "speed": 2},
        ]
        # Segments as _build_segments would emit for is_assisted=[F, F, T, T]:
        segments = [
            {"type": "riding",   "start": 0, "end": 1},
            {"type": "assisted", "start": 1, "end": 3},
        ]
        out = app._stats_from_trimmed(pts, segments, base_stats={})
        # Only the 1→2 and 2→3 deltas are assisted: 50 + 5 = 55
        self.assertEqual(out["assisted_gain_m"], 55)
        # Riding gain is only the 0→1 delta: 5
        self.assertEqual(out["elev_gain_m"], 5)


class TestStatsFromTrimmedAvgSpeed(unittest.TestCase):
    """Regression: avg_speed in _stats_from_trimmed used wall-clock duration
    as the denominator, so a ski day with 50% lift time reported artificially
    low pace. Denominator is now riding time only."""

    def test_avg_speed_uses_riding_time_only(self):
        # 2 riding points (10 km in 1 hr), then 2 assisted points (1 km in 1 hr)
        pts = [
            {"lat": 0, "lon": 0, "ele": 1000, "dist_km": 0.0,  "time": "2024-01-01T10:00:00", "speed": 10},
            {"lat": 0, "lon": 0, "ele": 1000, "dist_km": 10.0, "time": "2024-01-01T11:00:00", "speed": 10},
            {"lat": 0, "lon": 0, "ele": 1100, "dist_km": 11.0, "time": "2024-01-01T12:00:00", "speed": 1},
        ]
        segments = [
            {"type": "riding",   "start": 0, "end": 1},
            {"type": "assisted", "start": 1, "end": 2},
        ]
        out = app._stats_from_trimmed(pts, segments, base_stats={})
        # Riding: 10 km in 1 hr → 10 km/h. Assisted: excluded from both.
        self.assertEqual(out["avg_speed_kmh"], 10.0)
        # Wall-clock duration is preserved for display.
        self.assertEqual(out["duration_sec"], 2 * 3600)


if __name__ == "__main__":
    unittest.main()
