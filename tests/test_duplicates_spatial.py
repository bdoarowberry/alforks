"""Map-based duplicate detection.

Duplicates are decided by how much the two tracks overlap on the map (grid-cell
Jaccard), NOT by distance/duration/time — those are unreliable between two
recordings of one ride (gaps, pauses, stopped time, timezone offsets). Two
real-world cases anchor these tests:
  * 2024-10-13 — different rides, similar length, disjoint paths -> NOT duplicates.
  * 2021-08-29 — same ride, paths identical but distance/duration 19%/39% apart
    -> ARE duplicates (this is what plain ±5% matching missed).
"""
import unittest

import app


class TestTrackCells(unittest.TestCase):
    def test_same_point_collapses_to_one_cell(self):
        self.assertEqual(len(app._track_cells([[51.0, -115.0], [51.0, -115.0]])), 1)

    def test_distant_points_are_separate_cells(self):
        # ~1.1 km apart in latitude -> different ~120 m cells
        self.assertEqual(len(app._track_cells([[51.0, -115.0], [51.01, -115.0]])), 2)

    def test_empty_polyline_is_empty_set(self):
        self.assertEqual(app._track_cells(None), set())
        self.assertEqual(app._track_cells([]), set())


class TestTracksOverlap(unittest.TestCase):
    def test_identical_is_one(self):
        self.assertEqual(app._tracks_overlap({1, 2, 3}, {1, 2, 3}), 1.0)

    def test_disjoint_is_zero(self):
        self.assertEqual(app._tracks_overlap({1, 2}, {3, 4}), 0.0)

    def test_subset_scores_small_over_big(self):
        # a short ride that's a sub-path of a longer one: 2/6 -> stays under 0.6
        self.assertAlmostEqual(app._tracks_overlap({1, 2}, {1, 2, 3, 4, 5, 6}), 2 / 6)
        self.assertLess(app._tracks_overlap({1, 2}, {1, 2, 3, 4, 5, 6}),
                        app._DUPLICATE_GEO_MIN_OVERLAP)

    def test_missing_geometry_is_none(self):
        self.assertIsNone(app._tracks_overlap(set(), {1, 2}))
        self.assertIsNone(app._tracks_overlap({1, 2}, set()))


def _act(filename, polyline, dist=5.0, dur=2600, date="2024-10-13"):
    return {
        "filename": filename, "date": f"{date}T12:00:00", "name": filename,
        "stats": {"distance_km": dist, "duration_sec": dur, "elev_gain_m": 50},
        "polyline": polyline, "start_time": None, "end_time": None,
        "effective_type": "mtb", "excluded": False, "regions": [],
    }


class TestComputeGroups(unittest.TestCase):
    # A small loop, and a far-away loop (~110 km north / east).
    NEAR = [[51.000, -115.000], [51.002, -115.000], [51.002, -115.003], [51.000, -115.003]]
    FAR  = [[52.000, -116.000], [52.002, -116.000], [52.002, -116.003], [52.000, -116.003]]

    def setUp(self):
        self._orig = app.all_activities

    def tearDown(self):
        app.all_activities = self._orig

    def _groups_on(self, acts, date="2024-10-13"):
        app.all_activities = lambda: acts
        return [g for g in app._compute_duplicate_groups(detail=True) if g["date"] == date]

    def test_same_path_different_stats_groups(self):
        # 2021-08-29 shape: identical path, very different distance + duration.
        groups = self._groups_on(
            [_act("a.gpx", self.NEAR, dist=20.3, dur=14349, date="2021-08-29"),
             _act("b.gpx", self.NEAR, dist=25.1, dur=23449, date="2021-08-29")],
            date="2021-08-29")
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(t["filename"] for t in groups[0]["tracks"]), ["a.gpx", "b.gpx"])

    def test_similar_stats_disjoint_paths_do_not_group(self):
        # 2024-10-13 shape: same length, different place.
        groups = self._groups_on(
            [_act("a.gpx", self.NEAR, dist=5.2, dur=2700),
             _act("b.gpx", self.FAR,  dist=5.2, dur=2700)])
        self.assertEqual(groups, [])

    def test_no_geometry_falls_back_to_stats(self):
        # Both lack a polyline -> fall back to ±5% distance/duration matching.
        groups = self._groups_on(
            [_act("a.gpx", None, dist=5.0, dur=2600),
             _act("b.gpx", [],   dist=5.05, dur=2620)])
        self.assertEqual(len(groups), 1)


if __name__ == "__main__":
    unittest.main()
