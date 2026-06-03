"""Tests for the cached-result leaderboard builders in trail_match.

These exercise build_leaderboards / build_region_trail_index (and the shared
_iter_completed_attempts generator) against a synthetic trail_match cache:
completed/named filtering, (name, direction) bucketing, fastest-first sort,
per-region scoping, and best-attempt selection.
"""
import json

import trail_match


def _write_result(cache_dir, filename, timeline):
    (cache_dir / f"{filename}.json").write_text(json.dumps({
        "version": trail_match.TRAIL_MATCH_VERSION,
        "result": {"timeline": timeline},
    }), encoding="utf-8")


def _entry(name, direction, duration_sec, **over):
    e = {
        "name": name, "direction": direction, "completed": True,
        "start_time": "2026-05-01T10:00:00", "duration_sec": duration_sec,
        "distance_km": 1.5, "coverage_pct": 95.0, "kind": "trail",
        "start_idx": 0, "end_idx": 100,
    }
    e.update(over)
    return e


def test_build_leaderboards_buckets_and_sorts(tmp_path):
    _write_result(tmp_path, "rideA", [_entry("Pneuma", "down", 300, start_idx=5)])
    _write_result(tmp_path, "rideB", [_entry("Pneuma", "down", 250, start_idx=9)])
    boards = trail_match.build_leaderboards(tmp_path, {"rideA": {"title": "Morning"}})
    rows = boards[("Pneuma", "down")]
    assert [r["filename"] for r in rows] == ["rideB", "rideA"]  # fastest first
    assert rows[1]["title"] == "Morning"   # activity_meta applied
    assert rows[0]["title"] == "rideB"     # falls back to filename
    assert set(rows[0].keys()) == {
        "filename", "title", "start_time", "date", "duration_sec",
        "distance_km", "coverage_pct", "direction", "kind", "start_idx", "end_idx"}


def test_build_leaderboards_skips_incomplete_and_unnamed(tmp_path):
    _write_result(tmp_path, "ride", [
        _entry("Pneuma", "down", 300),
        _entry("Pneuma", "down", 100, completed=False),  # dropped
        _entry("", "down", 100),                          # dropped (no name)
    ])
    boards = trail_match.build_leaderboards(tmp_path)
    assert list(boards.keys()) == [("Pneuma", "down")]
    assert len(boards[("Pneuma", "down")]) == 1


def test_directions_bucket_separately(tmp_path):
    _write_result(tmp_path, "ride", [
        _entry("Cutoff", "up", 400),
        _entry("Cutoff", "down", 200),
    ])
    boards = trail_match.build_leaderboards(tmp_path)
    assert set(boards.keys()) == {("Cutoff", "up"), ("Cutoff", "down")}


def test_region_index_scopes_and_picks_best(tmp_path):
    _write_result(tmp_path, "rideA", [_entry("Pneuma", "down", 300)])
    _write_result(tmp_path, "rideB", [_entry("Pneuma", "down", 250)])
    _write_result(tmp_path, "rideC", [_entry("Pneuma", "down", 100)])  # no region → excluded
    regions = {"rideA": ["moose"], "rideB": ["moose"], "rideC": []}
    idx = trail_match.build_region_trail_index(tmp_path, {}, regions)
    trail = idx["moose"][("Pneuma", "down")]
    assert trail["attempts"] == 2                  # rideC excluded
    assert trail["best_duration_sec"] == 250       # rideB, not the faster region-less rideC
    assert trail["best_filename"] == "rideB"


def test_region_index_multi_region_ride_counts_in_each(tmp_path):
    _write_result(tmp_path, "ride", [_entry("Connector", "mixed", 200)])
    idx = trail_match.build_region_trail_index(tmp_path, {}, {"ride": ["moose", "bragg"]})
    assert idx["moose"][("Connector", "mixed")]["attempts"] == 1
    assert idx["bragg"][("Connector", "mixed")]["attempts"] == 1
