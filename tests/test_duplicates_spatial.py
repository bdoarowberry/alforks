"""Spatial gate on duplicate detection.

Two different rides on the same day with coincidentally-similar distance and
duration (e.g. the 2024-10-13 Canmore pair) used to be flagged as duplicates,
because the heuristic only looked at date + distance + duration. Start time is
unreliable across sources (timezone offsets), so the discriminator is spatial:
real duplicates trace the same ground. These tests cover the bounding-box IoU
gate and its effect on grouping.
"""
import unittest

import app


class TestBboxIou(unittest.TestCase):
    def test_disjoint_is_zero(self):
        self.assertEqual(app._bbox_iou((0, 0, 1, 1), (2, 2, 3, 3)), 0.0)

    def test_identical_is_one(self):
        self.assertEqual(app._bbox_iou((0, 0, 1, 1), (0, 0, 1, 1)), 1.0)

    def test_partial_overlap_between_zero_and_one(self):
        # boxes (0,0,2,2) and (1,1,3,3): inter=1, union=7
        self.assertAlmostEqual(app._bbox_iou((0, 0, 2, 2), (1, 1, 3, 3)), 1 / 7)


class TestTracksColocated(unittest.TestCase):
    NEAR = [[51.000, -115.000], [51.002, -115.000], [51.002, -115.003], [51.000, -115.003]]

    def test_identical_tracks(self):
        self.assertTrue(app._tracks_colocated(self.NEAR, self.NEAR))

    def test_slightly_shifted_tracks_overlap(self):
        shifted = [[la + 0.0005, lo + 0.0005] for la, lo in self.NEAR]  # ~50 m
        self.assertTrue(app._tracks_colocated(self.NEAR, shifted))

    def test_far_apart_tracks_do_not(self):
        far = [[52.000, -116.000], [52.002, -116.000], [52.002, -116.003]]
        self.assertFalse(app._tracks_colocated(self.NEAR, far))

    def test_thin_linear_tracks_far_apart_do_not(self):
        a = [[51.0, -115.00], [51.0, -115.01]]
        b = [[51.0, -114.90], [51.0, -114.89]]   # ~7 km east
        self.assertFalse(app._tracks_colocated(a, b))

    def test_missing_geometry_falls_back_to_true(self):
        self.assertTrue(app._tracks_colocated(None, self.NEAR))
        self.assertTrue(app._tracks_colocated([], self.NEAR))


def _act(filename, polyline, dist=5.0, dur=2600, date="2024-10-13"):
    return {
        "filename": filename, "date": f"{date}T12:00:00", "name": filename,
        "stats": {"distance_km": dist, "duration_sec": dur, "elev_gain_m": 50},
        "polyline": polyline, "start_time": None, "end_time": None,
        "effective_type": "mtb", "excluded": False, "regions": [],
    }


class TestComputeGroupsSpatial(unittest.TestCase):
    """End-to-end: same date + matching distance/duration, differing only by where
    the tracks go, must NOT group; co-located tracks still group."""

    NEAR = [[51.000, -115.000], [51.002, -115.000], [51.002, -115.003], [51.000, -115.003]]
    FAR  = [[52.000, -116.000], [52.002, -116.000], [52.002, -116.003], [52.000, -116.003]]

    def setUp(self):
        self._orig = app.all_activities

    def tearDown(self):
        app.all_activities = self._orig

    def test_far_apart_pair_does_not_group(self):
        app.all_activities = lambda: [_act("a.gpx", self.NEAR), _act("b.gpx", self.FAR)]
        groups = app._compute_duplicate_groups(detail=True)
        self.assertEqual([g for g in groups if g["date"] == "2024-10-13"], [])

    def test_colocated_pair_still_groups(self):
        shifted = [[la + 0.0004, lo + 0.0004] for la, lo in self.NEAR]
        app.all_activities = lambda: [_act("a.gpx", self.NEAR), _act("b.gpx", shifted)]
        groups = [g for g in app._compute_duplicate_groups(detail=True) if g["date"] == "2024-10-13"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(t["filename"] for t in groups[0]["tracks"]), ["a.gpx", "b.gpx"])


if __name__ == "__main__":
    unittest.main()
