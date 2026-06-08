"""Tests for route-attempt matching: timeline coalescing + the scan's
same-trail-skip. These lock in the fixes for trail_match fragmentation
(a long edge split into several runs) without regressing out-and-backs.

Run with:
    python -m unittest tests.test_route_attempts
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import route_attempts

_BASE = datetime(2024, 8, 3, 10, 0, 0)


def _entry(name, direction, start_idx, end_idx, start_min, end_min, kind="trail"):
    """Minimal timeline entry. Times are minutes-past a fixed base so gaps
    are easy to reason about."""
    return {
        "name": name, "kind": kind, "direction": direction,
        "start_idx": start_idx, "end_idx": end_idx,
        "start_time": (_BASE + timedelta(minutes=start_min)).isoformat(),
        "end_time": (_BASE + timedelta(minutes=end_min)).isoformat(),
    }


class TestCoalesceTimeline(unittest.TestCase):
    def test_same_direction_adjacent_fragments_merge(self):
        # The Big Elbow case: one descent trail_match split into two runs,
        # a few samples apart, a couple of minutes of wall-clock between.
        tl = [_entry("Big Elbow", "down", 0, 100, 0, 5),
              _entry("Big Elbow", "down", 110, 300, 8, 14)]
        merged = route_attempts._coalesce_timeline(tl)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["start_idx"], 0)
        self.assertEqual(merged[0]["end_idx"], 300)
        self.assertEqual(merged[0]["end_time"], "2024-08-03T10:14:00")

    def test_out_and_back_not_merged(self):
        # up then immediately down on the same trail = two legitimate route
        # segments. Merging would lose the attempt — must stay separate.
        tl = [_entry("Ridge", "up", 0, 100, 0, 5),
              _entry("Ridge", "down", 100, 200, 5, 10)]
        merged = route_attempts._coalesce_timeline(tl)
        self.assertEqual(len(merged), 2)
        self.assertEqual([e["direction"] for e in merged], ["up", "down"])

    def test_far_apart_same_direction_not_merged(self):
        # Two genuinely separate visits to the same trail (small sample gap
        # but >15 min apart — the sparse-recording trap) must NOT merge.
        tl = [_entry("Loop", "down", 0, 100, 0, 5),
              _entry("Loop", "down", 150, 250, 90, 95)]
        merged = route_attempts._coalesce_timeline(tl)
        self.assertEqual(len(merged), 2)

    def test_large_index_gap_not_merged(self):
        # No timestamps -> index gap decides; a big index gap stays separate.
        a = {"name": "T", "kind": "trail", "direction": "down", "start_idx": 0, "end_idx": 100}
        b = {"name": "T", "kind": "trail", "direction": "down", "start_idx": 9000, "end_idx": 9100}
        merged = route_attempts._coalesce_timeline([a, b])
        self.assertEqual(len(merged), 2)

    def test_does_not_mutate_input(self):
        tl = [_entry("X", "down", 0, 100, 0, 5),
              _entry("X", "down", 110, 200, 8, 12)]
        before = (tl[0]["end_idx"], tl[1]["start_idx"], len(tl))
        route_attempts._coalesce_timeline(tl)
        self.assertEqual((tl[0]["end_idx"], tl[1]["start_idx"], len(tl)), before)


class TestSameTrailAdjacent(unittest.TestCase):
    def test_matches_next_or_prev_segment(self):
        # 6-tuples to match the production _segment_endpoints shape.
        segs = [("A", "trail", "f", (0, 0), (0, 0), []),
                ("B", "trail", "f", (0, 0), (0, 0), []),
                ("C", "trail", "f", (0, 0), (0, 0), [])]
        # i=1 means just matched A (segs[0]), expecting B (segs[1]).
        self.assertTrue(route_attempts._same_trail_as_adjacent(
            {"name": "B", "kind": "trail"}, segs, 1))   # == next
        self.assertTrue(route_attempts._same_trail_as_adjacent(
            {"name": "A", "kind": "trail"}, segs, 1))   # == prev
        self.assertFalse(route_attempts._same_trail_as_adjacent(
            {"name": "C", "kind": "trail"}, segs, 1))   # neither


class TestScanWiggleSkip(unittest.TestCase):
    """_scan_one_ride with synthetic geometry: two segments A then B, each a
    short edge whose endpoints we place points exactly on."""

    # Two ~111m-apart endpoint pairs (0.001 deg latitude ~= 111 m).
    A0, A1 = (51.000, -114.0), (51.001, -114.0)
    B0, B1 = (51.002, -114.0), (51.003, -114.0)

    def _segs(self):
        # 6-tuples: (name, kind, direction, term_a, term_b, polyline). Empty
        # polylines keep these cases endpoint-touch-driven (coverage on an
        # empty polyline returns False), which is what they exercise.
        return [("A", "trail", "forward", self.A0, self.A1, []),
                ("B", "trail", "forward", self.B0, self.B1, [])]

    def _points(self, n=400):
        # Filler points far away; specific indices overwritten by callers.
        return [{"lat": 60.0, "lon": -100.0} for _ in range(n)]

    def _place(self, pts, idx, latlon):
        pts[idx] = {"lat": latlon[0], "lon": latlon[1]}

    def test_clean_match(self):
        segs = self._segs()
        pts = self._points()
        # A over [0,100): touch both A endpoints; B over [100,200): both B.
        self._place(pts, 10, self.A0); self._place(pts, 90, self.A1)
        self._place(pts, 110, self.B0); self._place(pts, 190, self.B1)
        tl = [_entry("A", "forward", 0, 99, 0, 5),
              _entry("B", "forward", 100, 199, 5, 10)]
        self.assertEqual(len(route_attempts._scan_one_ride(segs, tl, pts)), 1)

    def test_adjacent_same_trail_wiggle_is_skipped(self):
        segs = self._segs()
        pts = self._points()
        self._place(pts, 10, self.A0); self._place(pts, 90, self.A1)
        self._place(pts, 210, self.B0); self._place(pts, 290, self.B1)
        # An "A" fragment sits between A and B, adjacent, touching nothing
        # relevant — must be skipped, not treated as a detour.
        tl = [_entry("A", "forward", 0, 99, 0, 5),
              _entry("A", "forward", 100, 199, 5, 9),     # wiggle, adjacent
              _entry("B", "forward", 200, 299, 9, 14)]
        self.assertEqual(len(route_attempts._scan_one_ride(segs, tl, pts)), 1)

    def test_far_apart_same_trail_does_not_bridge(self):
        segs = self._segs()
        pts = self._points(n=9000)
        self._place(pts, 10, self.A0); self._place(pts, 90, self.A1)
        self._place(pts, 8800, self.B0); self._place(pts, 8900, self.B1)
        # The "A"-named entry between is hours/8000 samples later: must reset,
        # so no phantom whole-ride-spanning attempt forms.
        tl = [_entry("A", "forward", 0, 99, 0, 5),
              _entry("A", "forward", 8000, 8099, 200, 205),   # far away
              _entry("B", "forward", 8800, 8999, 210, 215)]
        self.assertEqual(len(route_attempts._scan_one_ride(segs, tl, pts)), 0)


class TestCoverageFallback(unittest.TestCase):
    """Coverage recovers edges whose OSM junctions are misplaced: the rider
    followed the line but never passed within 30 m of the off-position node."""

    # ~22 m between vertices (0.0002 deg lat), 12 vertices ~= 244 m of trail.
    POLY = [[51.0 + 0.0002 * k, -114.0] for k in range(12)]
    FAR = (52.0, -115.0)   # nowhere near the ride — endpoint-touch must fail

    def test_run_covers_true_when_ride_follows_line(self):
        pts = [{"lat": p[0], "lon": p[1]} for p in self.POLY]
        self.assertTrue(route_attempts._run_covers(pts, 0, len(pts), self.POLY))

    def test_run_covers_false_when_ride_elsewhere(self):
        pts = [{"lat": 60.0, "lon": -100.0} for _ in range(20)]
        self.assertFalse(route_attempts._run_covers(pts, 0, 20, self.POLY))

    def test_scan_matches_via_coverage_with_far_endpoints(self):
        # Endpoints are far (touch fails) but the ride covers the polyline —
        # this is the misplaced-junction case the fallback exists for.
        segs = [("A", "trail", "forward", self.FAR, self.FAR, self.POLY)]
        pts = [{"lat": p[0], "lon": p[1]} for p in self.POLY]
        tl = [_entry("A", "forward", 0, len(self.POLY) - 1, 0, 10)]
        self.assertEqual(len(route_attempts._scan_one_ride(segs, tl, pts)), 1)

    def test_scan_no_match_when_neither_touch_nor_cover(self):
        segs = [("A", "trail", "forward", self.FAR, self.FAR, self.POLY)]
        pts = [{"lat": 60.0, "lon": -100.0} for _ in range(20)]  # ride far away
        tl = [_entry("A", "forward", 0, 19, 0, 10)]
        self.assertEqual(len(route_attempts._scan_one_ride(segs, tl, pts)), 0)


class TestAccumulationAndNoise(unittest.TestCase):
    """Fragmented-but-complete traversals should match (accumulate across
    same-trail fragments); trivial noise entries shouldn't break a sequence."""

    POLY = [[51.0 + 0.0002 * k, -114.0] for k in range(12)]
    FAR = (52.0, -115.0)
    A0, A1 = (51.0, -114.0), (51.001, -114.0)
    B0, B1 = (51.002, -114.0), (51.003, -114.0)

    def _pts_on_poly(self):
        return [{"lat": p[0], "lon": p[1]} for p in self.POLY]

    def test_two_partial_fragments_accumulate_to_match(self):
        # Endpoints far (touch fails); neither half covers >=75% alone, but the
        # two consecutive same-trail fragments combined cover the whole line.
        segs = [("A", "trail", "forward", self.FAR, self.FAR, self.POLY)]
        pts = self._pts_on_poly()
        tl = [_entry("A", "forward", 0, 5, 0, 3),     # first half
              _entry("A", "forward", 6, 11, 3, 6)]    # second half
        self.assertEqual(len(route_attempts._scan_one_ride(segs, tl, pts)), 1)

    def test_noise_entry_skipped_even_after_long_gap(self):
        # A 4%/0.1km blip between two segments, with a >15min time gap that the
        # normal same-trail skip would reject — but noise skips unconditionally.
        segs = [("A", "trail", "forward", self.A0, self.A1, []),
                ("B", "trail", "forward", self.B0, self.B1, [])]
        pts = [{"lat": 60.0, "lon": -100.0} for _ in range(400)]
        for idx, ll in ((10, self.A0), (90, self.A1), (310, self.B0), (390, self.B1)):
            pts[idx] = {"lat": ll[0], "lon": ll[1]}
        tl = [_entry("A", "forward", 0, 99, 0, 5),
              {"name": "A", "kind": "trail", "direction": "down",
               "start_idx": 100, "end_idx": 110, "coverage_pct": 4, "distance_km": 0.1,
               "start_time": _entry("x", "f", 0, 0, 30, 30)["start_time"],   # +30 min
               "end_time": _entry("x", "f", 0, 0, 31, 31)["end_time"]},
              _entry("B", "forward", 300, 399, 35, 40)]
        self.assertEqual(len(route_attempts._scan_one_ride(segs, tl, pts)), 1)

    def test_far_apart_fragments_do_not_accumulate(self):
        # Two same-trail fragments each covering ~half the line, but far apart
        # in index/time — must NOT accumulate into a phantom match.
        segs = [("A", "trail", "forward", self.FAR, self.FAR, self.POLY)]
        pts = self._pts_on_poly() + [{"lat": 60.0, "lon": -100.0} for _ in range(9000)]
        # place the second half of the line far out in the index space
        for k in range(6, 12):
            pts[8000 + k] = {"lat": self.POLY[k][0], "lon": self.POLY[k][1]}
        tl = [_entry("A", "forward", 0, 5, 0, 3),            # first half
              _entry("A", "forward", 8006, 8011, 200, 205)]  # second half, hours later
        self.assertEqual(len(route_attempts._scan_one_ride(segs, tl, pts)), 0)

    def test_out_and_back_two_segments_need_two_entries(self):
        # Two same-name route segments (up + down). A full traversal satisfies
        # each on its own entry — the first segment must NOT swallow both.
        segs = [("A", "trail", "up", self.A0, self.A1, []),
                ("A", "trail", "down", self.B0, self.B1, [])]
        pts = [{"lat": 60.0, "lon": -100.0} for _ in range(400)]
        for idx, ll in ((10, self.A0), (90, self.A1), (210, self.B0), (290, self.B1)):
            pts[idx] = {"lat": ll[0], "lon": ll[1]}
        tl = [_entry("A", "up", 0, 99, 0, 5),
              _entry("A", "down", 100, 299, 5, 12)]
        self.assertEqual(len(route_attempts._scan_one_ride(segs, tl, pts)), 1)
        # And with only ONE entry it must NOT complete the two-segment route.
        self.assertEqual(len(route_attempts._scan_one_ride(segs, tl[:1], pts)), 0)


class TestRouteAttemptsDiskCache(unittest.TestCase):
    """Regression: the route_attempts disk-cache key embeds a NESTED tuple
    (the trail_match dir fingerprint). It must survive the JSON round-trip so
    the disk cache HITS across restarts.

    The old read compared `tuple(stored_key) == key`, which only un-tupled the
    OUTER level — the nested fingerprint stayed a list, so the compare missed
    every time and every restart recomputed all leaderboards from scratch.
    """

    def setUp(self):
        import app  # heavyweight; import lazily so the pure-logic tests above stay light
        self.app = app
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = app.ROUTE_ATTEMPTS_CACHE_DIR
        app.ROUTE_ATTEMPTS_CACHE_DIR = Path(self._tmp.name)
        app._invalidate_route_attempts()

    def tearDown(self):
        self.app.ROUTE_ATTEMPTS_CACHE_DIR = self._orig_dir
        self._tmp.cleanup()
        self.app._invalidate_route_attempts()

    def test_disk_cache_hits_after_json_round_trip(self):
        app = self.app
        route = {"id": "deadbeef0001", "region_id": "no-such-region-xyz",
                 "modified": "2026-06-08T00:00:00", "segments": []}
        fp = (3, 1234567.5)   # nested tuple — the crux of the bug
        key = app._route_attempts_cache_key(route, trail_match_fp=fp)
        payload = {"version": route_attempts.ROUTE_ATTEMPTS_VERSION,
                   "attempts": [], "attempt_count": 7,
                   "best_duration_sec": 3661, "best_filename": "x.gpx",
                   "best_date": "2026-06-01"}
        disk = app.ROUTE_ATTEMPTS_CACHE_DIR / f"{route['id']}.json"
        disk.write_text(json.dumps({"key": list(key), "payload": payload}),
                        encoding="utf-8")

        # Mem cache is empty (= fresh process / restart). A correct read returns
        # the stored payload via a disk HIT. Under the old bug it would miss,
        # recompute against the (unknown) region, and yield an empty leaderboard
        # — so asserting on the stored count distinguishes hit from recompute.
        result = app._get_route_attempts(route["id"], route=route, trail_match_fp=fp)
        self.assertEqual(result, payload)
        self.assertEqual(result["attempt_count"], 7)

    def test_changed_fingerprint_misses(self):
        # Sanity the other way: a stored key with a STALE fingerprint must NOT
        # hit — otherwise the fix would mask genuine invalidation.
        app = self.app
        route = {"id": "deadbeef0002", "region_id": "no-such-region-xyz",
                 "modified": "2026-06-08T00:00:00", "segments": []}
        stale_key = app._route_attempts_cache_key(route, trail_match_fp=(1, 1.0))
        payload = {"attempt_count": 99}
        disk = app.ROUTE_ATTEMPTS_CACHE_DIR / f"{route['id']}.json"
        disk.write_text(json.dumps({"key": list(stale_key), "payload": payload}),
                        encoding="utf-8")
        # Ask with a DIFFERENT fingerprint → stored key no longer matches → the
        # disk entry must be ignored (recompute yields an empty leaderboard).
        result = app._get_route_attempts(route["id"], route=route, trail_match_fp=(2, 2.0))
        self.assertNotEqual(result.get("attempt_count"), 99)


if __name__ == "__main__":
    unittest.main()
