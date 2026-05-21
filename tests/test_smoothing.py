"""Regression tests for bugs fixed in the review pass.

Run with:
    python -m unittest tests.test_smoothing
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Importing app triggers Flask route registration + a few on-disk reads. That's
# OK for these tests — the prewarm thread is gated behind ALFORKS_PREWARM and
# won't spin up on import.
import app
from flask import jsonify as _jsonify


# Register synthetic test endpoints at module-load time. Flask 3.x locks the
# route table after the first request, so these have to be added before any
# test class instantiates a test_client and dispatches.
@app.app.route("/__gzip_test_big__")
def _gzip_test_big():
    return _jsonify({"values": list(range(2000))})


@app.app.route("/__gzip_test_small__")
def _gzip_test_small():
    return _jsonify({"ok": True})


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
        # `_stats_from_trimmed` runs ma20 elevation smoothing now, which on a
        # 4-point synthetic series collapses every delta to zero. This test
        # is about boundary-index attribution, not smoothing — so bypass the
        # smoother for the duration of the call.
        with patch.object(app, '_smooth_elevations', side_effect=lambda eles, **kw: list(eles)):
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


class TestWriteEndpointValidation(unittest.TestCase):
    """Regression: write endpoints used to persist any JSON shape, so a bad
    client could corrupt metadata.json (e.g. trim.start_km = "foo") and
    every subsequent read would 500. Validators now reject bad payloads
    with 400."""

    def setUp(self):
        # Need a real GPX file on disk so _safe_gpx_path passes
        self.fname = "_validation_regression.gpx"
        self.fpath = app.GPX_DIR / self.fname
        self.fpath.write_text(
            '<?xml version="1.0"?>\n<gpx version="1.1"><trk><trkseg>'
            '<trkpt lat="50.0" lon="-116.0"><ele>1000</ele><time>2024-01-01T10:00:00Z</time></trkpt>'
            '<trkpt lat="50.001" lon="-116.0"><ele>1001</ele><time>2024-01-01T10:00:10Z</time></trkpt>'
            '</trkseg></trk></gpx>',
            encoding="utf-8",
        )

    def tearDown(self):
        self.fpath.unlink(missing_ok=True)

    def test_trim_must_be_object(self):
        client = app.app.test_client()
        resp = client.patch(f"/api/activity/{self.fname}/metadata",
                            json={"trim": "not-an-object"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("trim", resp.get_json()["error"])

    def test_trim_numeric_fields(self):
        client = app.app.test_client()
        resp = client.patch(f"/api/activity/{self.fname}/metadata",
                            json={"trim": {"start_km": "foo"}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("start_km", resp.get_json()["error"])

    def test_smoothing_window_must_be_int(self):
        client = app.app.test_client()
        resp = client.patch(f"/api/activity/{self.fname}/metadata",
                            json={"smoothing": {"window": "abc"}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("window", resp.get_json()["error"])

    def test_segment_overrides_must_be_list(self):
        client = app.app.test_client()
        resp = client.patch(f"/api/activity/{self.fname}/segments",
                            json={"segment_overrides": {"not": "a list"}})
        self.assertEqual(resp.status_code, 400)

    def test_segment_override_bad_type(self):
        client = app.app.test_client()
        resp = client.patch(f"/api/activity/{self.fname}/segments",
                            json={"segment_overrides": [{"type": "flying", "start": 0, "end": 5}]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("type", resp.get_json()["error"])

    def test_segment_override_negative_start(self):
        client = app.app.test_client()
        resp = client.patch(f"/api/activity/{self.fname}/segments",
                            json={"segment_overrides": [{"type": "riding", "start": -1, "end": 5}]})
        self.assertEqual(resp.status_code, 400)

    def test_valid_trim_passes(self):
        client = app.app.test_client()
        resp = client.patch(f"/api/activity/{self.fname}/metadata",
                            json={"trim": {"start_km": 0.5, "end_km": 1.5}})
        self.assertEqual(resp.status_code, 200)

    def test_valid_segment_overrides_pass(self):
        client = app.app.test_client()
        resp = client.patch(f"/api/activity/{self.fname}/segments",
                            json={"segment_overrides": [
                                {"type": "riding", "start": 0, "end": 5},
                                {"type": "assisted", "start": 6, "end": 10},
                            ]})
        self.assertEqual(resp.status_code, 200)


class TestParseGpxEdgeCases(unittest.TestCase):
    """Coverage for low-signal GPX inputs that used to slip through untested."""

    def _write_gpx(self, body: str) -> "Path":
        from pathlib import Path
        fname = "_edge_case.gpx"
        fpath: Path = app.GPX_DIR / fname
        fpath.write_text(
            '<?xml version="1.0"?>\n<gpx version="1.1"><trk><trkseg>'
            + body +
            '</trkseg></trk></gpx>',
            encoding="utf-8",
        )
        return fpath

    def test_no_time_elements(self):
        # Two points with no <time> → dt=0 throughout, avg_speed None
        body = (
            '<trkpt lat="50.0" lon="-116.0"><ele>1000</ele></trkpt>'
            '<trkpt lat="50.001" lon="-116.0"><ele>1001</ele></trkpt>'
        )
        fpath = self._write_gpx(body)
        try:
            data = app.parse_gpx(fpath)
            self.assertIsNotNone(data)
            self.assertIsNone(data["stats"]["avg_speed_kmh"])
            # Distance still computed from haversine
            self.assertGreater(data["stats"]["distance_km"], 0)
        finally:
            fpath.unlink(missing_ok=True)

    def test_zero_elevation_track(self):
        # All points at the same elevation → zero gain and loss
        body = "".join(
            f'<trkpt lat="{50 + i * 0.0001}" lon="-116.0"><ele>1000</ele>'
            f'<time>2024-01-01T10:00:{i:02d}Z</time></trkpt>'
            for i in range(10)
        )
        fpath = self._write_gpx(body)
        try:
            data = app.parse_gpx(fpath)
            self.assertIsNotNone(data)
            self.assertEqual(data["stats"]["elev_gain_m"], 0)
            self.assertEqual(data["stats"]["elev_loss_m"], 0)
        finally:
            fpath.unlink(missing_ok=True)


class TestApplyTrimBoundaries(unittest.TestCase):
    """Regression: _apply_trim had several edge-case paths that were
    untested — trim beyond track length, start > end, empty result."""

    def _make_data(self, n: int = 10):
        pts = [{
            "lat": 50.0 + i * 0.0001, "lon": -116.0, "ele": 1000 + i,
            "dist_km": round(i * 0.1, 3),
            "time": f"2024-01-01T10:{i:02d}:00",
            "speed": 10,
        } for i in range(n)]
        return {
            "filename": "test.gpx", "name": "t", "date": "2024-01-01",
            "bbox": [50.0, -116.0, 50.01, -115.99],
            "points": pts,
            "segments": [{"type": "riding", "start": 0, "end": n - 1}],
            "stats": {"distance_km": 0.9, "duration_sec": 540,
                      "elev_gain_m": 9, "elev_loss_m": 0,
                      "assisted_gain_m": 0, "avg_speed_kmh": 6,
                      "max_speed_kmh": 10, "lift_count": 0,
                      "peak_ele_m": 1009},
        }

    def test_trim_beyond_full_distance_is_noop(self):
        d = self._make_data()
        out = app._apply_trim(d, {"start_km": 0, "end_km": 999})
        self.assertIs(out, d)

    def test_start_greater_than_end_returns_original(self):
        d = self._make_data()
        out = app._apply_trim(d, {"start_km": 0.8, "end_km": 0.2})
        # start_idx finds first pt where dist >= 0.8 → idx 8
        # end_idx walks back to last pt where dist <= 0.2 → idx 2
        # end_idx (2) <= start_idx (8) → return unchanged
        self.assertIs(out, d)

    def test_empty_trim_dict_noop(self):
        d = self._make_data()
        out = app._apply_trim(d, {})
        self.assertIs(out, d)

    def test_trim_slice_re_bases_distance_to_zero(self):
        d = self._make_data()
        out = app._apply_trim(d, {"start_km": 0.3, "end_km": 0.7})
        self.assertEqual(out["points"][0]["dist_km"], 0.0)
        self.assertGreater(out["points"][-1]["dist_km"], 0)
        self.assertIn("trim_full_distance_km", out)


class TestMergeHrIntoDataEdgeCases(unittest.TestCase):
    """Regression: _merge_hr_into_data has several early-return paths for
    activities that don't have enough time/location data to align HR."""

    def test_empty_points_returns_unchanged(self):
        data = {"points": [], "date": "2024-01-01T10:00:00", "stats": {}}
        self.assertIs(app._merge_hr_into_data(data), data)

    def test_missing_date_returns_unchanged(self):
        data = {"points": [{"lat": 50, "lon": -116}], "stats": {}}
        self.assertIs(app._merge_hr_into_data(data), data)

    def test_no_start_iso_returns_unchanged(self):
        data = {"points": [{"lat": 50, "lon": -116}], "date": "", "stats": {}}
        self.assertIs(app._merge_hr_into_data(data), data)


class TestPointInPolygon(unittest.TestCase):
    """Ray-casting point-in-polygon. Interior and exterior are well-defined;
    points exactly on an edge are implementation-dependent (ray-casting).
    Pin down current behaviour with tests so future changes are deliberate."""

    # Unit square with corners at (0,0), (0,1), (1,1), (1,0)
    # Stored as [[lat, lon], ...] per the _point_in_polygon contract
    SQUARE = [[0, 0], [0, 1], [1, 1], [1, 0]]

    def test_interior_point(self):
        self.assertTrue(app._point_in_polygon(0.5, 0.5, self.SQUARE))

    def test_exterior_point(self):
        self.assertFalse(app._point_in_polygon(2.0, 2.0, self.SQUARE))

    def test_empty_ring_is_outside(self):
        self.assertFalse(app._point_in_polygon(0.5, 0.5, []))

    def test_concave_polygon_hole_is_outside(self):
        # L-shape: outside the carved-out quadrant
        l_shape = [[0, 0], [0, 2], [1, 2], [1, 1], [2, 1], [2, 0]]
        self.assertFalse(app._point_in_polygon(1.5, 1.5, l_shape))
        self.assertTrue(app._point_in_polygon(0.5, 1.5, l_shape))


class TestGzipMiddleware(unittest.TestCase):
    """Regression: the after_request gzip hook should compress JSON above
    the threshold, skip below, and respect Accept-Encoding."""

    def test_small_response_is_not_compressed(self):
        client = app.app.test_client()
        resp = client.get("/__gzip_test_small__", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotEqual(resp.headers.get("Content-Encoding"), "gzip")
        # Vary is still set so caches partition the response — required even
        # when this individual response wasn't compressed.
        self.assertEqual(resp.headers.get("Vary"), "Accept-Encoding")

    def test_no_compression_when_client_doesnt_accept(self):
        client = app.app.test_client()
        resp = client.get("/__gzip_test_big__", headers={"Accept-Encoding": "identity"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotEqual(resp.headers.get("Content-Encoding"), "gzip")

    def test_large_response_is_compressed(self):
        import gzip
        client = app.app.test_client()
        resp = client.get("/__gzip_test_big__", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("Content-Encoding"), "gzip")
        self.assertEqual(resp.headers.get("Vary"), "Accept-Encoding")
        # Werkzeug's test client doesn't auto-decompress; do it ourselves
        decoded = gzip.decompress(resp.data)
        import json as _json
        self.assertEqual(len(_json.loads(decoded)["values"]), 2000)


class TestSafeGpxPath(unittest.TestCase):
    """`_safe_gpx_path` is the path-traversal guard between user-supplied
    filenames (in API routes) and the real filesystem. These tests assert
    that escapes outside `tracks/` and non-`.gpx` extensions are rejected
    so the various `/api/activity/<filename>/...` endpoints can't be
    coerced into reading or modifying arbitrary files."""

    def test_simple_filename_inside_tracks_is_accepted(self):
        # Make a real file so the resolved-path check can succeed
        f = app.GPX_DIR / "_test_safe_path.gpx"
        try:
            f.write_text("<gpx></gpx>", encoding="utf-8")
            result = app._safe_gpx_path("_test_safe_path.gpx")
            self.assertIsNotNone(result)
            self.assertEqual(result.name, "_test_safe_path.gpx")
        finally:
            try: f.unlink()
            except FileNotFoundError: pass

    def test_relative_traversal_rejected(self):
        # `..` segments must not escape GPX_DIR even with a .gpx extension.
        # Forward slashes only — pathlib treats backslash as a separator on
        # Windows but a literal character on POSIX, so a `..\\file.gpx`
        # assertion is platform-dependent and not a useful security check.
        self.assertIsNone(app._safe_gpx_path("../metadata.json"))
        self.assertIsNone(app._safe_gpx_path("../../etc/passwd"))
        self.assertIsNone(app._safe_gpx_path("../malicious.gpx"))

    def test_absolute_path_rejected(self):
        self.assertIsNone(app._safe_gpx_path("/etc/passwd"))
        self.assertIsNone(app._safe_gpx_path("C:\\Windows\\System32\\config\\SAM"))

    def test_non_gpx_extension_rejected(self):
        self.assertIsNone(app._safe_gpx_path("metadata.json"))
        self.assertIsNone(app._safe_gpx_path("config.json"))
        self.assertIsNone(app._safe_gpx_path("activity.txt"))
        self.assertIsNone(app._safe_gpx_path("activity"))  # no extension

    def test_garbage_input_rejected(self):
        # Empty filename resolves to GPX_DIR itself, which has no .gpx
        # suffix and so falls out of the gate.
        self.assertIsNone(app._safe_gpx_path(""))


class TestSpikeDetectionAssistedAware(unittest.TestCase):
    """_find_speed_spikes must skip legs touching an assisted (lift/shuttle)
    point. Sustained real vehicle speeds otherwise either false-positive
    individual high samples or contaminate the local-median window so real
    riding spikes go undetected."""

    def _pts(self, n: int, assisted_range: tuple | None = None,
             phantom_at: int | None = None) -> list:
        """Build a synthetic track of n points. dt=1s between samples,
        positions advance at ~10 m/s riding pace. assisted_range marks a
        [start, end) window as assisted=True (also at riding pace, so the
        median is uniform — the assisted flag is what should suppress, not
        the speed magnitude). phantom_at injects a 300 m teleport at that
        index to simulate a phantom warp."""
        pts = []
        lat = 50.0
        for i in range(n):
            # 10 m/s ≈ 36 km/h advance per sample (~0.00009° lat per 10 m)
            lat_step = 0.00009
            if phantom_at is not None and i == phantom_at:
                lat_step = 0.0027  # ~300 m jump → ~1080 km/h implied
            lat += lat_step
            is_assisted = (assisted_range is not None
                           and assisted_range[0] <= i < assisted_range[1])
            pts.append({
                "lat":      lat,
                "lon":      -116.0,
                "ele":      1000.0,
                "time":     f"2024-01-01T10:{i // 60:02d}:{i % 60:02d}",
                "dist_km":  i * 0.010,
                "speed":    20.0,
                "assisted": is_assisted,
            })
        return pts

    def test_phantom_in_riding_section_is_flagged(self):
        """Baseline: a clear teleport on a non-assisted leg must trip rule 5."""
        pts = self._pts(n=60, phantom_at=30)
        mask, max_implied = app._find_speed_spikes(pts)
        self.assertTrue(mask[30], "phantom leg in riding section should flag")
        self.assertGreater(max_implied, 100)

    def test_phantom_in_assisted_section_is_skipped(self):
        """A phantom inside an assisted span must not flag — lift/shuttle
        samples are real, not warps."""
        pts = self._pts(n=60, assisted_range=(20, 40), phantom_at=30)
        mask, max_implied = app._find_speed_spikes(pts)
        self.assertFalse(mask[30],
                         "phantom leg inside assisted span must not flag")
        # No other legs should be flagged either (clean riding pace elsewhere)
        self.assertEqual(sum(mask), 0)
        self.assertEqual(max_implied, 0.0)

    def test_leg_touching_assisted_boundary_is_skipped(self):
        """The leg from the last riding sample into the first assisted
        sample (and vice-versa) is also dropped — boundary legs straddle
        the regime change and aren't comparable to either side's median."""
        # Phantom at index 20 — same index as the start of an assisted span,
        # so the leg pts[19]→pts[20] touches an assisted point.
        pts = self._pts(n=60, assisted_range=(20, 40), phantom_at=20)
        mask, _ = app._find_speed_spikes(pts)
        self.assertFalse(mask[20], "leg touching assisted boundary must skip")

    def test_short_tracks_return_empty(self):
        """Sanity: the existing guard for tracks shorter than the window
        still applies regardless of assisted flags."""
        pts = self._pts(n=5, assisted_range=(0, 5))
        mask, max_implied = app._find_speed_spikes(pts)
        self.assertEqual(sum(mask), 0)
        self.assertEqual(max_implied, 0.0)


class TestStatsFromTrimmedRidingMaxSpeed(unittest.TestCase):
    """_stats_from_trimmed must report max_speed_kmh_riding that excludes
    samples falling inside an assisted segment. The Top Speed PR ranks on
    this field so lift/shuttle telemetry doesn't appear as a riding PR."""

    def _make_trimmed_args(self, speeds: list, assisted_span: tuple | None):
        """Build (pts, segments, base_stats) for _stats_from_trimmed where
        each point gets the given speed and assisted_span (start_idx, end_idx)
        is wrapped as an 'assisted' segment."""
        pts = []
        for i, sp in enumerate(speeds):
            pts.append({
                "lat":     50.0 + i * 0.00009,
                "lon":     -116.0,
                "ele":     1000.0,
                "time":    f"2024-01-01T10:{i // 60:02d}:{i % 60:02d}",
                "dist_km": i * 0.010,
                "speed":   sp,
            })
        segments = []
        if assisted_span:
            s, e = assisted_span
            # A riding seg before, an assisted seg in the middle, riding after
            if s > 0:
                segments.append({"type": "riding",   "start": 0,     "end": s})
            segments.append({"type":     "assisted", "start": s,     "end": e})
            if e < len(speeds) - 1:
                segments.append({"type": "riding",   "start": e,     "end": len(speeds) - 1})
        else:
            segments.append({"type": "riding", "start": 0, "end": len(speeds) - 1})
        base_stats = {"distance_km": 1.0, "max_speed_kmh": max(speeds)}
        return pts, segments, base_stats

    def test_riding_max_excludes_assisted_samples(self):
        """Riding speeds: 20, 25. Assisted (middle): 100. Riding max should
        be 25, overall max should be 100."""
        speeds = [20.0, 22.0, 100.0, 105.0, 102.0, 25.0, 24.0]
        pts, segments, base = self._make_trimmed_args(speeds, assisted_span=(2, 4))
        out = app._stats_from_trimmed(pts, segments, base)
        self.assertEqual(out["max_speed_kmh"], 105.0)
        self.assertEqual(out["max_speed_kmh_riding"], 25.0)

    def test_all_riding_riding_max_matches_overall(self):
        """No assisted segment: riding max == overall max."""
        speeds = [10.0, 20.0, 30.0, 25.0, 15.0]
        pts, segments, base = self._make_trimmed_args(speeds, assisted_span=None)
        out = app._stats_from_trimmed(pts, segments, base)
        self.assertEqual(out["max_speed_kmh"], 30.0)
        self.assertEqual(out["max_speed_kmh_riding"], 30.0)


if __name__ == "__main__":
    unittest.main()
