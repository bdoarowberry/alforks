"""Synthetic acceptance cases from BUILD_BRIEF.md, against docs/column-lineage-design.md."""
from __future__ import annotations

import json
import sys

from column_helpers import (
    FIXTURES, REPO, assert_step_evidence, branch_of, cli_args, fixture, inputs, load_trace,
    row_selection_pairs, run_cli, trace,
)

SOURCE_A = "?.demo.source.input_a"
SOURCE_B = "?.demo.source.input_b"


# ----------------------------------------------------------------------------
# 1. Rename and temporary table.

def test_1_trace_through_rename_and_temp_table(tmp_path):
    t = trace("rename_temp", "demo.output.result_value", tmp_path)

    assert t.data["schema_version"] == "column-lineage/1.0"
    assert t.data["requested"].lower() == "demo.output.result_value"
    assert t.target_id == "?.demo.output.result_value"
    assert t.data["target"]["kind"] == "column"
    assert t.data["scan"]["files"] == 2

    step = t.only_step_for(t.target_id)
    assert step["writer_kind"] == "insert_select"
    assert step["mapping_method"] == "target_list"
    assert "load_output" in (step["procedure"] or "").lower()
    stage = t.temp_column("#stage", "staged_value")
    assert inputs(step, "value") == [stage]

    stage_step = t.only_step_for(stage)
    assert stage_step["writer_kind"] == "select_into"
    assert stage_step["mapping_method"] == "projection_alias"
    assert stage_step["operation"] == "rename"
    assert inputs(stage_step, "value") == [SOURCE_A]

    assert SOURCE_A in t.upstream(t.target_id)
    assert SOURCE_B not in t.all_ids()
    assert (SOURCE_A, "source_without_writer") in t.stop_pairs()
    assert all(s["status"] == "verified" for s in t.steps)
    assert t.gaps() == []
    assert t.complete

    # Evidence: file and line of the INSERT (lines 12-14 of load_output.sql).
    assert_step_evidence(t)
    assert any(ev["path"] == "load_output.sql" and 12 <= ev["start_line"] <= 14 for ev in step["evidence"])

    md = t.markdown.lower()
    assert "demo.output.result_value" in md
    assert "demo.source.input_a" in md
    assert "demo.output.result_value.lineage.json" in md  # companion JSON is referenced


def test_1_spec_case_and_brackets_normalize_stem_and_identity(tmp_path):
    first = trace("rename_temp", "Demo.Output.Result_Value", tmp_path / "a")
    second = trace("rename_temp", "[demo].[output].[result_value]", tmp_path / "b")
    # Both specs write demo.output.result_value.lineage.json (checked by load_trace) and
    # resolve to the same target identity and lineage.
    assert first.target_id == second.target_id == "?.demo.output.result_value"
    assert SOURCE_A in first.upstream(first.target_id)
    assert SOURCE_A in second.upstream(second.target_id)


# ----------------------------------------------------------------------------
# 2. CASE chooses between two value contributors.

def test_2_case_keeps_both_alternatives_and_condition(tmp_path):
    t = trace("case_choice", "demo.output.result_value", tmp_path)
    step = t.only_step_for(t.target_id)
    assert step["operation"] == "case"

    values = inputs(step, "value")
    assert SOURCE_A in values and SOURCE_B in values
    branch_b = (branch_of(step, SOURCE_B, "value") or "").strip()
    branch_a = (branch_of(step, SOURCE_A, "value") or "").strip()
    assert branch_b.upper().startswith("WHEN") and branch_b.upper().endswith("THEN")
    assert "use_b" in branch_b.lower()
    assert branch_a.upper() == "ELSE"

    use_b = "?.demo.source.use_b"
    conditions = inputs(step, "condition")
    assert use_b in conditions
    assert use_b not in values
    assert SOURCE_A not in conditions and SOURCE_B not in conditions
    assert (branch_of(step, use_b, "condition") or "").strip().upper().startswith("WHEN")

    assert t.complete
    md = t.markdown.lower()
    assert "input_a" in md and "input_b" in md and "use_b" in md


def test_2_case_without_else_records_implicit_null(tmp_path):
    t = trace("case_choice", "demo.output.alt_value", tmp_path)
    step = t.only_step_for(t.target_id)
    assert step["operation"] == "case"
    values = inputs(step, "value")
    assert SOURCE_B in values
    assert "constant:null" in values
    assert (branch_of(step, "constant:NULL", "value") or "").strip().upper() == "ELSE"
    assert SOURCE_A not in t.all_ids()


# ----------------------------------------------------------------------------
# 3. Row selection is separate from value inputs.

def test_3_filter_and_join_keys_are_row_selection_not_values(tmp_path):
    t = trace("row_selection", "demo.output.result_value", tmp_path)
    step = t.only_step_for(t.target_id)
    assert inputs(step, "value") == [SOURCE_A]

    rs = row_selection_pairs(step)
    assert ("?.demo.source.is_active", "filter") in rs
    assert ("?.demo.source.id", "join_key") in rs
    assert ("?.demo.lookup.id", "join_key") in rs
    assert ("?.demo.lookup.region_code", "join_predicate") in rs

    selection_nodes = {node for node, _ in rs}
    assert not selection_nodes & set(inputs(step))  # not value/condition/window inputs

    # Row-selection columns are not traced upstream by default.
    assert t.steps_for("?.demo.lookup.id") == []
    assert "?.demo.lookup_raw.id" not in t.all_ids()
    assert t.complete
    assert "is_active" in t.markdown.lower()


def test_3_trace_row_selection_option_follows_join_key_upstream(tmp_path):
    t = trace("row_selection", "demo.output.result_value", tmp_path, "--trace-row-selection")
    lookup_steps = t.steps_for("?.demo.lookup.id")
    assert lookup_steps, "join key column was not traced with --trace-row-selection"
    assert any("?.demo.lookup_raw.id" in inputs(s, "value") for s in lookup_steps)
    # Still not promoted to a value input of the target step.
    assert inputs(t.only_step_for(t.target_id), "value") == [SOURCE_A]


# ----------------------------------------------------------------------------
# 4. Positional INSERT needs a declared ordered schema.

def test_4_positional_insert_uses_declared_schema(tmp_path):
    result = trace("positional_schema", "demo.output.result_value", tmp_path / "r")
    step = result.only_step_for(result.target_id)
    assert step["mapping_method"] == "positional_schema"
    assert inputs(step, "value") == [SOURCE_A]
    assert step["status"] == "inferred"
    assert "positional_without_schema" not in result.gap_kinds
    assert result.complete

    # The IDENTITY column is skipped: first_value takes the first select item.
    first = trace("positional_schema", "demo.output.first_value", tmp_path / "f")
    first_step = first.only_step_for(first.target_id)
    assert first_step["mapping_method"] == "positional_schema"
    assert inputs(first_step, "value") == [SOURCE_B]


def test_4_positional_insert_without_schema_is_gap(tmp_path):
    t = trace("positional_no_schema", "demo.output.result_value", tmp_path)
    assert t.steps_for(t.target_id) == []
    assert "positional_without_schema" in t.gap_kinds
    assert SOURCE_A not in t.all_input_ids()
    assert SOURCE_B not in t.all_input_ids()
    assert not t.complete


# ----------------------------------------------------------------------------
# 5. Separation of columns, branches and identities.

def test_5_unrelated_columns_do_not_bleed(tmp_path):
    root = fixture("separation")
    assert run_cli(cli_args(root, ["demo.output.result_value", "demo.output.other_value"], tmp_path)) == 0
    result = load_trace(tmp_path, "demo.output.result_value", root)
    other = load_trace(tmp_path, "demo.output.other_value", root)
    one, two = "?.demo.source_one.amount", "?.demo.source_two.amount"

    assert inputs(result.only_step_for(result.target_id), "value") == [one]
    assert inputs(other.only_step_for(other.target_id), "value") == [two]
    assert two not in result.all_input_ids()
    assert one not in other.all_input_ids()
    assert result.steps_for(other.target_id) == []
    assert other.steps_for(result.target_id) == []
    assert result.complete and other.complete


def test_5_union_all_branches_are_distinct_steps(tmp_path):
    t = trace("separation", "demo.union_out.result_value", tmp_path)
    steps = t.steps_for(t.target_id)
    assert len(steps) == 2
    assert sorted(tuple(inputs(s, "value")) for s in steps) == [
        ("?.demo.source_one.amount",), ("?.demo.source_two.amount",)]
    contexts = [tuple(s["branch_context"]) for s in steps]
    assert contexts[0] != contexts[1]
    for ctx in contexts:
        assert any("union" in c.lower() for c in ctx), ctx
    assert "ambiguous_writer_order" not in t.gap_kinds


def test_5_if_else_writers_are_distinct_steps(tmp_path):
    t = trace("separation", "demo.branch_out.result_value", tmp_path)
    steps = t.steps_for(t.target_id)
    assert len(steps) == 2
    by_input = {inputs(s, "value")[0]: s for s in steps}
    assert set(by_input) == {"?.demo.source_one.amount", "?.demo.source_two.amount"}
    if_ctx = by_input["?.demo.source_one.amount"]["branch_context"]
    else_ctx = by_input["?.demo.source_two.amount"]["branch_context"]
    assert any(c.strip().upper().startswith("IF") for c in if_ctx), if_ctx
    assert any(c.strip().upper().startswith("ELSE") for c in else_ctx), else_ctx
    assert "ambiguous_writer_order" not in t.gap_kinds  # INSERTs are row-additive


def test_5_same_names_in_different_databases_are_distinct(tmp_path):
    left = trace("separation", "demo.cross_out.left_value", tmp_path / "l")
    right = trace("separation", "demo.cross_out.right_value", tmp_path / "r")
    db_one, db_two = "db_one.demo.shared.amount", "db_two.demo.shared.amount"
    assert inputs(left.only_step_for(left.target_id), "value") == [db_one]
    assert inputs(right.only_step_for(right.target_id), "value") == [db_two]
    assert db_two not in left.all_input_ids()
    assert db_one not in right.all_input_ids()
    assert left.nodes[db_one]["object"]["database"].lower() == "db_one"
    assert right.nodes[db_two]["object"]["database"].lower() == "db_two"


# ----------------------------------------------------------------------------
# 6. Dynamic SQL and ambiguous writers are gaps, not invented paths.

def test_6_exec_dynamic_sql_filling_temp_table_is_gap(tmp_path):
    t = trace("dynamic_sql_exec", "demo.output.result_value", tmp_path)
    step = t.only_step_for(t.target_id)
    stage = t.temp_column("#stage", "staged_value")
    assert inputs(step, "value") == [stage]

    assert "dynamic_sql" in t.gap_kinds
    assert not t.complete
    # The temp table is filled only by dynamic SQL: that is not "a source without writer".
    assert (stage, "source_without_writer") not in t.stop_pairs()
    assert SOURCE_A not in t.all_input_ids()


def test_6_sp_executesql_writer_is_gap_and_fail_on_incomplete(tmp_path):
    t = trace("dynamic_sql_sp_executesql", "demo.output.result_value", tmp_path / "a")
    assert "dynamic_sql" in t.gap_kinds
    assert not t.complete
    assert all(s["status"] != "verified" for s in t.steps_for(t.target_id))
    assert (t.target_id, "source_without_writer") not in t.stop_pairs()
    assert "dynamic" in t.markdown.lower()

    # A zero exit never means complete; --fail-on-incomplete makes it visible.
    root = fixture("dynamic_sql_sp_executesql")
    assert run_cli(cli_args(root, "demo.output.result_value", tmp_path / "b", "--fail-on-incomplete")) == 3
    assert run_cli(cli_args(fixture("rename_temp"), "demo.output.result_value", tmp_path / "c",
                            "--fail-on-incomplete")) == 0


def test_6_updates_from_two_procedures_are_ambiguous_writer_order(tmp_path):
    t = trace("ambiguous_writers", "demo.output.result_value", tmp_path)
    steps = t.steps_for(t.target_id)
    assert len(steps) == 2
    assert {s["writer_kind"] for s in steps} == {"update"}
    assert len({(s["procedure"] or "").lower() for s in steps}) == 2
    assert sorted(tuple(inputs(s, "value")) for s in steps) == [(SOURCE_A,), (SOURCE_B,)]

    gaps = t.gaps("ambiguous_writer_order")
    assert gaps
    text = json.dumps(gaps).lower()
    assert "update_from_a" in text and "update_from_b" in text
    assert not t.complete

    b_step = next(s for s in steps if inputs(s, "value") == [SOURCE_B])
    assert (SOURCE_B, "filter") in row_selection_pairs(b_step)
    for s in steps:
        assert ("?.demo.output.id", "join_key") in row_selection_pairs(s)


# ----------------------------------------------------------------------------
# 7. Caller-selected database boundary.

REPORT_MAP = "--database-map"
TARGET_DB = "report_db.demo.output.result_value"
STAGED = "stage_db.demo.staged.staged_amount"
RAW = "land_db.demo.raw_feed.raw_amount"
DECOY = "land_db.demo.staged.staged_amount"


def _cross(tmp_path, *extra):
    return trace("cross_database", TARGET_DB, tmp_path, REPORT_MAP, "report=report_db", *extra)


def test_7_cross_database_without_boundary(tmp_path):
    t = _cross(tmp_path)
    assert t.target_id == TARGET_DB
    up = t.upstream(t.target_id)
    assert STAGED in up and RAW in up
    assert DECOY not in t.all_ids()  # same schema.name and column, other database
    assert (RAW, "source_without_writer") in t.stop_pairs()
    assert t.nodes[STAGED]["object"]["database"].lower() == "stage_db"
    assert t.nodes[RAW]["object"]["database"].lower() == "land_db"
    assert t.complete


def test_7_stop_at_stage_database(tmp_path):
    t = _cross(tmp_path, "--stop-at-database", "stage_db")
    assert [d.lower() for d in t.data["boundary"]["databases"]] == ["stage_db"]
    assert (STAGED, "boundary") in t.stop_pairs()
    assert t.steps_for(STAGED) == []
    assert RAW not in t.all_input_ids()
    assert t.complete


def test_7_different_boundary_same_files(tmp_path):
    t = _cross(tmp_path, "--stop-at-database", "land_db")
    assert [d.lower() for d in t.data["boundary"]["databases"]] == ["land_db"]
    assert t.steps_for(STAGED), "trace should continue through stage_db"
    assert (RAW, "boundary") in t.stop_pairs()
    assert (RAW, "source_without_writer") not in t.stop_pairs()
    assert (STAGED, "boundary") not in t.stop_pairs()


def test_7_stop_at_object_pattern(tmp_path):
    t = _cross(tmp_path, "--stop-at-object", "stage_db.demo.staged")
    assert (STAGED, "boundary") in t.stop_pairs()
    assert t.steps_for(STAGED) == []


# ----------------------------------------------------------------------------
# 8. Existing table-level CLI is unchanged.

def test_8_table_level_cli_on_column_fixture(tmp_path):
    sys.path.insert(0, str(REPO / "src"))
    from table_lineage.cli import main as table_main

    assert table_main(["--target", "demo.output", "--source-root", str(FIXTURES / "rename_temp"),
                       "--output", str(tmp_path), "--no-progress"]) == 0
    result = json.loads((tmp_path / "lineage.json").read_text(encoding="utf-8"))
    assert result["target"] == "demo.output"
    assert (tmp_path / "lineage.mmd").exists() and (tmp_path / "lineage.txt").exists()
    assert not list(tmp_path.glob("*.lineage.json"))  # column outputs are separate

    upstream: dict[str, set[str]] = {}
    for edge in result["edges"]:
        upstream.setdefault(edge["downstream"], set()).add(edge["upstream"])
    seen, frontier = set(), ["demo.output"]
    while frontier:
        for nxt in upstream.get(frontier.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    assert "demo.source" in seen
