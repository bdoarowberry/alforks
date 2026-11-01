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


WRITER = ("USE demo;\r\nGO\r\nALTER PROCEDURE dbo.load_destination AS\r\nBEGIN\r\n"
          "  INSERT INTO dbo.destination (field_01)\r\n  SELECT s.value_01 FROM dbo.stage AS s; -- café\r\nEND;\r\n")


@pytest.mark.parametrize("encoding,bom", [("utf-16-le", b"\xff\xfe"), ("utf-16-le", b""), ("utf-16-be", b"\xfe\xff"),
                                          ("cp1252", b""), ("utf-8", b"\xef\xbb\xbf")])
def test_writer_in_non_utf8_file_is_read(encoding, bom, tmp_path):
    """SSMS saves "Unicode" scripts as UTF-16; read as UTF-8 their statements silently vanished."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "tables.sql").write_text("CREATE TABLE dbo.stage (value_01 int);\nCREATE TABLE dbo.destination (field_01 int);\n")
    (src / "load.sql").write_bytes(bom + WRITER.encode(encoding))
    t = trace(src, "demo.dbo.destination.field_01", tmp_path / "out", *DB)
    assert "demo.stage.value_01" not in reached(t)
    assert "demo.dbo.stage.value_01" in reached(t)
    assert t.data["scan"]["files_without_statements"] == 0


def test_keyword_named_variables_do_not_split_statements(tmp_path):
    """`@End`, `@Update`, `@Set` used to start a new statement at the keyword, cutting the statement
    after '@' (a writer among them was lost to a parse failure)."""
    t = trace(ROOT / "keyword_variables", "demo.dbo.t.d", tmp_path, *DB)
    assert "demo.dbo.s.d" in reached(t)
    assert t.data["scan"]["fallback"] == 0


def test_filegroup_clause_on_its_own_is_non_lineage():
    from column_lineage.catalog import non_lineage
    assert non_lineage("ON [PRIMARY]\n-- ==========\n")
    assert non_lineage("ON [PRIMARY] TEXTIMAGE_ON [PRIMARY]")
    assert not non_lineage("ON x.a = y.a")


def test_declared_terminals_label_ends_without_hiding_their_reason(tmp_path):
    """A reviewer may accept no-writer sources as terminals for a run. The result says so
    (complete_to_declared_boundary); the original stop reason, statuses and evidence are kept."""
    terminals = tmp_path / "terminals.txt"
    terminals.write_text("demo.stage.*   # staging loaded by an external feed (reviewer decision)\n", encoding="utf-8")
    plain = trace(ROOT / "writer_shapes", "demo.dbo.dest1.ref_no", tmp_path / "a", *DB)
    t = trace(ROOT / "writer_shapes", "demo.dbo.dest1.ref_no", tmp_path / "b", *DB,
              "--declared-terminals", str(terminals))
    assert plain.completeness["result"] == "complete_within_selected_scope"
    assert t.completeness["result"] == "complete_to_declared_boundary"
    [stop] = [s for s in t.stops() if s["node"] == "demo.stage.src.source_ref"]
    assert stop["reason"] == "source_without_writer"
    assert stop["declared_terminal"] == "staging loaded by an external feed (reviewer decision)"
    assert t.completeness["declared_terminal_ends"] >= 1
    assert [s["status"] for s in t.steps] == [s["status"] for s in plain.steps]
    page = (tmp_path / "b" / "demo.dbo.dest1.ref_no.lineage.md").read_text(encoding="utf-8")
    assert "declared terminal for this run" in page and "not proved" in page


def test_declared_terminal_matching_only_some_ends_is_not_declared_complete(tmp_path):
    terminals = tmp_path / "terminals.txt"
    terminals.write_text("demo.other.*\n", encoding="utf-8")
    t = trace(ROOT / "writer_shapes", "demo.dbo.dest1.ref_no", tmp_path / "c", *DB,
              "--declared-terminals", str(terminals))
    assert t.completeness["result"] == "complete_within_selected_scope"


def test_boundary_only_trace_keeps_reached_boundary_when_terminals_match_it(tmp_path):
    terminals = tmp_path / "terminals.txt"
    terminals.write_text("demo.*\n", encoding="utf-8")
    t = trace(ROOT / "writer_shapes", "demo.dbo.dest3.ref_no", tmp_path / "d", *DB, "--stop-at-object", "demo.stage.*",
              "--declared-terminals", str(terminals))
    assert t.completeness["result"] == "reached_requested_boundary"
    assert t.completeness["declared_terminal_ends"] == 0


@pytest.mark.parametrize("column", ["col_a", "col_b"])
def test_empty_layout_copy_is_not_a_value_source(column, tmp_path):
    """`SELECT * INTO #r FROM target WHERE 1 = 2` (or TOP (0)) copies the layout, zero rows: no value edge
    from the target into #r, so no false loop (real data: 153 of 178 traces had one)."""
    t = trace(ROOT / "empty_copy", f"demo.dbo.targettable.{column}", tmp_path, *DB)
    assert not t.data["cycles"]
    assert f"demo.dbo.sourcetable.{column}" in reached(t)
    assert f"demo.dbo.targettable.{column}" not in {i for s in t.steps for i in inputs(s, "value")}
    # A constant-true filter (1 = 1, NOT (1 = 0)) keeps its rows: the real writers stay traced.
    assert t.complete


def test_yields_no_rows_only_for_row_independent_predicates():
    import sqlglot
    from column_lineage.analyzer import Analyzer
    def no_rows(sql):
        return Analyzer.yields_no_rows(sqlglot.parse_one(sql, read="tsql"))
    for sql in ("SELECT * FROM t WHERE 1 = 2", "SELECT a FROM t WHERE 1 = 0 AND a > 1", "SELECT TOP (0) a FROM t",
                "SELECT TOP 0 a FROM t", "SELECT a FROM t WHERE 'x' = 'y'", "SELECT a FROM t WHERE NOT (1 = 1)",
                "SELECT a FROM t WHERE 1 = 2 OR 0 > 1"):
        assert no_rows(sql), sql
    for sql in ("SELECT a FROM t WHERE 1 = 1", "SELECT a FROM t WHERE a = 2", "SELECT a FROM t WHERE 1 = 2 OR a = 1",
                "SELECT TOP 1 a FROM t", "SELECT a FROM t WHERE @p = 0", "SELECT a FROM t WHERE '1' = 1"):
        assert not no_rows(sql), sql
