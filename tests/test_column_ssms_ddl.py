"""SSMS-scripted table definitions (fixtures/column/ssms_ddl/, invented).

TEXTIMAGE_ON, SPARSE, MASKED WITH, ENCRYPTED WITH, table WITH (...) options made the CREATE TABLE fail
to parse, so the table had no column list: every * over it and every positional INSERT into it was a
gap (real run: the empty-copy-then-CTE wildcard shape in 28 traces). An inline INDEX clause was read
as a column named index, shifting positions. Temporal period columns take no explicit value.
"""
from column_helpers import FIXTURES, inputs, trace

ROOT = FIXTURES / "ssms_ddl"
DB = ("--default-database", "demo")


def blocking(t):
    return {g["kind"] for g in t.data["gaps"] if g["affects"] != "row_selection"}


def test_star_over_a_textimage_on_table_expands(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_cte.col_a", tmp_path, *DB)
    assert blocking(t) == set()
    assert t.data["completeness"]["status"] == "complete"


def test_positional_insert_into_a_table_with_column_options(tmp_path):
    for column, source in (("col_a", "col_a"), ("col_b", "col_b"), ("secret", "secret")):
        t = trace(ROOT, f"demo.dbo.TargetTable.{column}", tmp_path / column, *DB)
        assert blocking(t) == set()
        assert inputs(t.only_step_for(t.target_id), "value") == [f"demo.dbo.feed.{source}"]


def test_inline_index_is_not_a_column(tmp_path):
    t = trace(ROOT, "demo.dbo.Indexed.col_b", tmp_path, *DB)
    assert inputs(t.only_step_for(t.target_id), "value") == ["demo.dbo.feed.id"]


def test_temporal_period_columns_take_no_value(tmp_path):
    t = trace(ROOT, "demo.dbo.Timed.col_a", tmp_path, *DB)
    assert inputs(t.only_step_for(t.target_id), "value") == ["demo.dbo.feed.col_b"]


def test_gap_summary_says_why_a_star_was_not_expanded(tmp_path):
    import json
    from column_helpers import run_cli, cli_args
    root = FIXTURES / "review6"
    assert run_cli(cli_args(root, "demo.dbo.Out_u.col_a", tmp_path, *DB, "--gap-summary")) == 0
    summary = json.loads((tmp_path / "gap-summary.json").read_text(encoding="utf-8"))
    assert summary["totals"]["wildcard_traces_by_unexpanded_star_reason"] == {"differing_create_table_definitions": 1}
    assert summary["scan"]["create_table_unread"] == 0


def test_create_table_repairs_are_counted(tmp_path):
    import json
    from column_helpers import run_cli, cli_args
    assert run_cli(cli_args(ROOT, "demo.dbo.Out_cte.col_a", tmp_path, *DB, "--gap-summary")) == 0
    scan = json.loads((tmp_path / "gap-summary.json").read_text(encoding="utf-8"))["scan"]
    assert scan["create_table_options_blanked"] >= 2 and scan["create_table_unread"] == 0
