"""Regressions from real-source feedback round 8 (fixtures/column/round8/).

- An explicit INSERT ... SELECT inside a procedure body must give the target column a writer
  (several shapes), and a no-writer stop must say what the writer search found.
- Parse repairs: spaced compound assignment, qualified columns in an INSERT into a table variable,
  index options glued to the next statement, a lone WITH EXECUTE AS header, `db..name`.
"""
import json

import pytest

from column_helpers import FIXTURES, inputs, trace

ROOT = FIXTURES / "round8"
DB = ("--default-database", "demo")


def reached(t) -> set[str]:
    seen, frontier = set(), [t.target_id]
    while frontier:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        for step in t.steps_for(node):
            frontier.extend(inputs(step, "value"))
    return seen


@pytest.mark.parametrize("table", ["dest1", "dest2", "dest3", "dest4", "dest5", "dest6", "dest7"])
def test_insert_select_in_procedure_body_reaches_stage(table, tmp_path):
    t = trace(ROOT / "writer_shapes", f"demo.dbo.{table}.ref_no", tmp_path, *DB)
    assert "demo.stage.src.source_ref" in reached(t)
    assert ("demo.dbo." + table + ".ref_no", "source_without_writer") not in t.stop_pairs()


@pytest.mark.parametrize("table", ["dest1", "dest2", "dest3", "dest4", "dest5", "dest6"])
def test_more_insert_shapes_reach_stage(table, tmp_path):
    t = trace(ROOT / "writer_shapes_more", f"demo.dbo.{table}.ref_no", tmp_path, *DB)
    assert "demo.stage.src.source_ref" in reached(t)


def test_no_writer_stop_lists_near_misses(tmp_path):
    t = trace(ROOT / "other_database", "demo.dbo.dest.ref_no", tmp_path, *DB, "--database-map", "db_b=other",
              "--gap-summary")
    [stop] = [s for s in t.stops("source_without_writer") if s["node"] == "demo.dbo.dest.ref_no"]
    categories = sorted(f["category"] for f in stop["writer_search"])
    assert categories == ["comment_or_string", "other_object"]
    assert "other.dbo.dest" in stop["detail"] and "--database-map" in stop["detail"]
    assert t.completeness["result"] == "complete_within_selected_scope"
    summary = json.loads((tmp_path / "gap-summary.json").read_text())
    assert summary["totals"]["no_writer_stops"]["unused_writer_candidates_by_category"] == {
        "comment_or_string": 1, "other_object": 1}
    assert "dest" not in json.dumps(summary)


def test_writer_found_when_database_context_matches(tmp_path):
    t = trace(ROOT / "other_database", "other.dbo.dest.ref_no", tmp_path, *DB, "--database-map", "db_b=other")
    assert "demo.stage.src.source_ref" in reached(t)


def test_reached_boundary_is_distinct_from_no_writer(tmp_path):
    t = trace(ROOT / "writer_shapes", "demo.dbo.dest3.ref_no", tmp_path, *DB, "--stop-at-object", "demo.stage.*")
    assert t.completeness["result"] == "reached_requested_boundary"
    assert t.completeness["ends_by_meaning"] == {"reached_requested_boundary": 1}


def test_parse_repairs(tmp_path):
    t = trace(ROOT / "parse_repairs", "demo.dbo.t.a", tmp_path, *DB)
    assert "demo.dbo.src.a" in reached(t)
    assert t.data["scan"]["fallback"] == 0
    assert t.data["scan"]["ignored_non_lineage"] == 1   # the WITH EXECUTE AS header on its own
