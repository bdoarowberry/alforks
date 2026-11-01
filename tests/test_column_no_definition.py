"""A * over a table with no CREATE TABLE in the scanned files (fixtures/column/no_definition/, invented).

Real run: 28 traces through an empty #temp copy of such a table and 10 directly over one. A column
that a scanned INSERT column list, UPDATE SET or MERGE names certainly exists, so a lookup by name
through the * can bind it (inferred). Positions stay unknown, and names no writer mentions stay gaps.
"""
import json

from column_helpers import FIXTURES, cli_args, inputs, run_cli, trace

ROOT = FIXTURES / "no_definition"
DB = ("--default-database", "demo")


def blocking(t):
    return {g["kind"] for g in t.data["gaps"] if g["affects"] != "row_selection"}


def reached(t):
    seen, frontier = set(), [t.target_id]
    while frontier:
        node = frontier.pop()
        if node not in seen:
            seen.add(node)
            for step in t.steps_for(node):
                frontier.extend(inputs(step, "value"))
    return seen


def test_empty_copy_of_an_undefined_table_then_cte(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_copy.col_a", tmp_path, *DB)
    assert blocking(t) == set() and "demo.dbo.raw.col_a" in reached(t)


def test_cte_star_over_an_undefined_table_uses_written_names(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_cte.col_b", tmp_path, *DB)
    # (The INSERT and the UPDATE in two procedures still leave a genuine run-order question.)
    assert "wildcard_without_schema" not in blocking(t) and "demo.dbo.feed.col_b" in reached(t)
    assert all(s["status"] == "inferred" for s in t.steps if "by name" in json.dumps(s))


def test_name_no_writer_mentions_stays_a_gap_with_its_reason(tmp_path):
    assert run_cli(cli_args(ROOT, "demo.dbo.Out_missing.col_z", tmp_path, *DB, "--gap-summary")) == 0
    summary = json.loads((tmp_path / "gap-summary.json").read_text(encoding="utf-8"))
    assert summary["totals"]["wildcard_traces_by_unexpanded_star_reason"] == {
        "written_but_no_definition_in_selected_sources": 1}


def test_qualified_read_elsewhere_proves_the_column_exists(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_read.col_r", tmp_path, *DB)
    assert "wildcard_without_schema" not in blocking(t) and "demo.dbo.loaded.col_r" in reached(t)


def test_package_destination_mapping_proves_the_column_exists(tmp_path):
    """demo.stage is loaded by a package and has no CREATE TABLE in this SQL root; the destination's
    column mapping proves it has amount."""
    sql = tmp_path / "sql"
    sql.mkdir()
    (sql / "report.sql").write_text(
        "USE demo_db;\nGO\nCREATE TABLE demo.report (amount nvarchar(50));\nGO\n"
        "CREATE PROCEDURE demo.build_report AS\nBEGIN\n"
        "    WITH c AS (SELECT * FROM demo.stage)\n"
        "    INSERT INTO demo.report (amount) SELECT amount FROM c;\nEND\nGO\n", encoding="utf-8")
    t = trace(sql, "demo_db.demo.report.amount", tmp_path / "out",
              "--package-root", str(FIXTURES / "ssis" / "basic"))
    assert "wildcard_without_schema" not in blocking(t)
    assert "demo_db.demo.stage.amount" in reached(t)


def test_inner_source_that_certainly_has_the_column_wins(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_inner.col_a", tmp_path, *DB)
    assert "ambiguous_column" not in blocking(t)
    assert "demo.dbo.feed.col_a" in reached(t) and "demo.dbo.outside.col_a" not in reached(t)


def test_insert_exec_into_a_select_into_table_uses_its_order(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_exec.col_a", tmp_path, *DB)
    assert "insert_exec" not in blocking(t) and "demo.dbo.raw.col_a" in reached(t)
