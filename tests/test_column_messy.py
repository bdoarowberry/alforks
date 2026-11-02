"""Messy real-world T-SQL patterns, checked at the level of the JSON contract."""
from __future__ import annotations

import json

from column_helpers import (
    branch_of, cli_args, fixture, inputs, load_trace, row_selection_pairs, run_cli,
    run_cli_subprocess, trace,
)

SOURCE_A = "?.demo.source.input_a"
SOURCE_B = "?.demo.source.input_b"
NOT_PROVEN = ("partial", "ambiguous", "unresolved")


# ---------------------------------------------------------------- CTEs / derived

def test_cte_chain_feeds_insert(tmp_path):
    t = trace("cte_chain", "demo.output.result_value", tmp_path)
    step = t.only_step_for(t.target_id)
    final = t.one_id("cte:", "::final_cte.final_value")
    assert inputs(step, "value") == [final]

    doubled = t.one_id("cte:", "::doubled.doubled_value")
    base = t.one_id("cte:", "::base.base_value")
    final_step = t.only_step_for(final)
    assert final_step["writer_kind"] == "cte"
    assert inputs(final_step, "value") == [doubled]

    doubled_step = t.only_step_for(doubled)
    assert doubled_step["writer_kind"] == "cte"
    assert doubled_step["operation"] == "arithmetic"
    assert base in inputs(doubled_step, "value")
    assert "constant:2" in inputs(doubled_step, "value")

    base_step = t.only_step_for(base)
    assert base_step["writer_kind"] == "cte"
    assert inputs(base_step, "value") == [SOURCE_A]
    # The CTE's WHERE belongs to the CTE step, not to the outer INSERT.
    assert ("?.demo.source.is_active", "filter") in row_selection_pairs(base_step)
    assert ("?.demo.source.is_active", "filter") not in row_selection_pairs(step)

    assert SOURCE_A in t.upstream(t.target_id)
    assert t.complete


def test_derived_table_alias_rename(tmp_path):
    t = trace("derived_table", "demo.output.result_value", tmp_path)
    step = t.only_step_for(t.target_id)
    derived = t.one_id("derived:", "::d.renamed_value")
    assert inputs(step, "value") == [derived]
    inner = t.only_step_for(derived)
    assert inner["writer_kind"] == "derived"
    assert inner["operation"] == "rename"
    assert inputs(inner, "value") == [SOURCE_A]
    assert t.complete


# ------------------------------------------------------------------- UPDATE / MERGE

def test_update_with_alias_target(tmp_path):
    t = trace("update_alias", "demo.output.result_value", tmp_path)
    step = t.only_step_for(t.target_id)  # alias t resolved to demo.output
    assert step["writer_kind"] == "update"
    assert step["mapping_method"] == "set_clause"
    assert inputs(step, "value") == [SOURCE_A]
    rs = row_selection_pairs(step)
    assert ("?.demo.source.id", "join_key") in rs
    assert ("?.demo.output.id", "join_key") in rs
    assert not any(i.endswith(".t.result_value") for i in t.all_ids())
    assert "ambiguous_writer_order" not in t.gap_kinds
    assert t.complete


def test_update_coalesce_precedence_branches(tmp_path):
    t = trace("update_alias", "demo.output.fallback_value", tmp_path)
    step = t.only_step_for(t.target_id)
    assert step["operation"] == "coalesce"
    assert set(inputs(step, "value")) == {SOURCE_A, SOURCE_B, "constant:0"}
    assert (branch_of(step, SOURCE_A, "value") or "").lower() == "coalesce argument 1"
    assert (branch_of(step, SOURCE_B, "value") or "").lower() == "coalesce argument 2"
    assert (branch_of(step, "constant:0", "value") or "").lower() == "coalesce argument 3"
    assert ("constant:0", "constant") in t.stop_pairs()


def test_merge_update_and_insert_writers_kept(tmp_path):
    t = trace("merge_writers", "demo.output.result_value", tmp_path)
    steps = t.steps_for(t.target_id)
    by_kind = {s["writer_kind"]: s for s in steps}
    assert len(steps) == 2 and set(by_kind) == {"merge_update", "merge_insert"}
    assert inputs(by_kind["merge_update"], "value") == [SOURCE_A]
    assert inputs(by_kind["merge_insert"], "value") == [SOURCE_B]

    upd_ctx, ins_ctx = by_kind["merge_update"]["branch_context"], by_kind["merge_insert"]["branch_context"]
    assert upd_ctx != ins_ctx
    assert any("when matched" in c.lower() for c in upd_ctx), upd_ctx
    assert any("not matched" in c.lower() for c in ins_ctx), ins_ctx

    for s in steps:
        rs = row_selection_pairs(s)
        assert ("?.demo.source.id", "join_key") in rs
        assert ("?.demo.output.id", "join_key") in rs
    assert (SOURCE_A, "merge_condition") in row_selection_pairs(by_kind["merge_update"])
    assert "ambiguous_writer_order" not in t.gap_kinds  # one MERGE statement
    assert t.complete


# ------------------------------------------------------------- procedures and temps

def test_nested_procedure_fills_caller_temp_table(tmp_path):
    t = trace("nested_proc", "demo.output.result_value", tmp_path)
    step = t.only_step_for(t.target_id)
    stage = t.temp_column("#stage", "staged_value")
    assert inputs(step, "value") == [stage]
    assert "load_output" in t.nodes[stage]["object"]["scope"].lower()  # scope is the creator

    fills = t.steps_for(stage)
    callee = [s for s in fills if "fill_stage" in (s["procedure"] or "").lower()]
    assert callee, "callee writer of the caller-created #stage was not used"
    assert inputs(callee[0], "value") == [SOURCE_A]
    assert callee[0]["status"] == "inferred"

    assert SOURCE_A in t.upstream(t.target_id)
    assert t.completeness["overall_step_status"] == "inferred"
    assert t.complete


def test_insert_exec_from_a_single_result_set_is_mapped_not_verified(tmp_path):
    """The callee returns one result set, so INSERT ... EXEC (split over two lines) maps by position;
    the step is inferred, never verified, and the target is not a source."""
    t = trace("insert_exec", "demo.output.result_value", tmp_path)
    assert "insert_exec" not in t.gap_kinds
    [step] = t.steps_for(t.target_id)
    assert step["writer_kind"] == "insert_exec" and step["status"] == "inferred"
    assert inputs(step, "value") == ["?.demo.source.input_a"]
    assert (t.target_id, "source_without_writer") not in t.stop_pairs()


# ------------------------------------------------------------------- wildcards

def test_select_star_into_temp_expanded_from_schema(tmp_path):
    t = trace("wildcard_schema", "demo.output.result_value", tmp_path)
    step = t.only_step_for(t.target_id)
    copy_b = t.temp_column("#copy", "input_b")
    assert inputs(step, "value") == [copy_b]
    copy_step = t.only_step_for(copy_b)
    assert copy_step["mapping_method"] == "wildcard_schema"
    assert copy_step["writer_kind"] == "select_into"
    assert inputs(copy_step, "value") == [SOURCE_B]
    assert SOURCE_A not in t.all_input_ids()
    assert "wildcard_without_schema" not in t.gap_kinds
    assert t.complete


def test_select_star_without_schema_is_gap(tmp_path):
    t = trace("wildcard_no_schema", "demo.output.result_value", tmp_path)
    assert "wildcard_without_schema" in t.gap_kinds
    # No same-name mapping is invented from the wildcard.
    assert "?.demo.unknown_source.input_b" not in t.all_input_ids()
    assert not any(s["mapping_method"] == "wildcard_schema" for s in t.steps)
    assert not t.complete


# ---------------------------------------------------------- binding failures

def test_unqualified_column_in_join_is_ambiguous(tmp_path):
    t = trace("ambiguous_column", "demo.output.result_value", tmp_path)
    assert "ambiguous_column" in t.gap_kinds
    assert not t.complete
    candidates = {"?.demo.left_side.shared_value", "?.demo.right_side.shared_value"}
    for s in t.steps_for(t.target_id):
        assert s["status"] in NOT_PROVEN
        for item in s["inputs"]:
            if item["node"].lower() in candidates:
                assert item["status"] != "verified"


def test_unqualified_unique_column_binds_in_same_statement(tmp_path):
    t = trace("ambiguous_column", "demo.output.other_value", tmp_path)
    step = t.only_step_for(t.target_id)
    assert inputs(step, "value") == ["?.demo.right_side.right_only"]
    assert step["status"] in ("verified", "inferred")
    assert "ambiguous_column" not in t.gap_kinds  # the sibling column's gap does not bleed in


def test_unknown_alias_is_unresolved_alias(tmp_path):
    t = trace("ambiguous_column", "demo.alias_out.result_value", tmp_path)
    assert "unresolved_alias" in t.gap_kinds
    assert not t.complete
    # x.input_a must not be bound to a similarly named column of another object.
    assert SOURCE_A not in t.all_input_ids()
    assert all(s["status"] != "verified" for s in t.steps_for(t.target_id))


def test_scalar_udf_keeps_argument_and_reports_opaque_function(tmp_path):
    t = trace("scalar_udf", "demo.output.result_value", tmp_path)
    step = t.only_step_for(t.target_id)
    assert step["operation"] == "function"
    assert SOURCE_A in inputs(step, "value")
    assert "opaque_function" in t.gap_kinds
    assert not t.complete


# --------------------------------------------------------- variables / parameters

def test_variable_assigned_from_query_is_traced(tmp_path):
    t = trace("variable_lineage", "demo.output.result_value", tmp_path)
    step = t.only_step_for(t.target_id)
    assert step["operation"] == "arithmetic"
    var = t.one_id("variable:", "::@max_a")
    values = inputs(step, "value")
    assert SOURCE_B in values and var in values
    assert t.nodes[var]["kind"] == "variable"

    assign = t.only_step_for(var)
    assert assign["writer_kind"] == "variable_assignment"
    assert assign["mapping_method"] == "assignment"
    assert SOURCE_A in t.upstream(var)
    assert SOURCE_A in t.upstream(t.target_id)
    assert t.complete


def test_parameter_bound_by_callers(tmp_path):
    t = trace("parameter_binding", "demo.output.result_value", tmp_path)
    step = t.only_step_for(t.target_id)
    param = t.one_id("parameter:", "::@batch_value")
    assert inputs(step, "value") == [param]
    assert t.nodes[param]["kind"] == "parameter"
    assert "load_output" in (t.nodes[param]["scope"] or "").lower()

    bindings = t.steps_for(param)
    assert len(bindings) == 2  # named and positional callers are distinct alternatives
    assert {s["writer_kind"] for s in bindings} == {"parameter_binding"}
    assert {s["mapping_method"] for s in bindings} == {"argument"}
    assert sorted(tuple(inputs(s, "value")) for s in bindings) == [("constant:42",), ("constant:7",)]
    assert {("constant:42", "constant"), ("constant:7", "constant")} <= t.stop_pairs()


def test_parameter_without_caller_stops(tmp_path):
    t = trace("parameter_binding", "demo.output.other_value", tmp_path)
    # load_output's INSERT omits other_value, so it is also an alternative (NULL); take the UPDATE.
    assert {s["writer_kind"] for s in t.steps_for(t.target_id)} == {"update", "insert_omitted"}
    step = next(s for s in t.steps_for(t.target_id) if s["writer_kind"] == "update")
    param = t.one_id("parameter:", "::@other_input")
    assert inputs(step, "value") == [param]
    assert t.steps_for(param) == []
    assert (param, "parameter_without_caller") in t.stop_pairs()


# ------------------------------------------------------------------- cycles

def test_cycle_terminates_at_earlier_content(tmp_path):
    """A loop is cut where it reads back: the read keeps its dependency, on an explicit earlier-content node."""
    root = fixture("cycle")
    proc = run_cli_subprocess(cli_args(root, "demo.beta.val", tmp_path), timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    t = load_trace(tmp_path, "demo.beta.val", root)
    alpha, beta = "?.demo.alpha.val", "?.demo.beta.val"
    prior = "prior_state:" + beta
    assert t.data["cycles"] == []
    assert len(t.steps_for(alpha)) == 2  # seed writer and the cyclic writer
    assert sorted(tuple(inputs(s, "value")) for s in t.steps_for(alpha)) == [("?.demo.seed.seed_val",), (prior,)]
    stop = next(s for s in t.data["stops"] if s["node"] == prior)
    assert stop["reason"] == "prior_state"
    assert stop["loop"]["earlier_state_of"] == beta and set(stop["loop"]["loop_nodes"]) == {alpha, beta}
    assert "?.demo.seed.seed_val" in t.upstream(t.target_id)


# ------------------------------------------------------ identifiers and noise

def test_bracketed_and_mixed_case_identifiers(tmp_path):
    t = trace("quoted_identifiers", "[Demo].[Output].[Result_Value]", tmp_path / "q")
    plain = trace("quoted_identifiers", "demo.output.result_value", tmp_path / "p")
    assert t.target_id == plain.target_id == "?.demo.output.result_value"
    assert SOURCE_A in t.upstream(t.target_id)
    assert t.complete and not t.gaps()

    # One identity per column: no ids that differ only by case or brackets.
    raw = t.raw_ids()
    assert len({i.lower() for i in raw}) == len(raw), sorted(raw)
    assert not any("[" in i or "]" in i or '"' in i for i in raw)
    assert len(t.ids_matching("temp_table:", "::#stage.staged_value")) == 1

    def shape(tr):
        return sorted((s["target"].lower(), tuple(inputs(s))) for s in tr.steps)
    assert shape(t) == shape(plain)


def test_comments_and_string_literals_do_not_create_lineage(tmp_path):
    t = trace("comments_strings", "demo.output.result_value", tmp_path)
    step = t.only_step_for(t.target_id)
    assert inputs(step, "value") == [SOURCE_A]
    assert not any("decoy" in i for i in t.all_ids())
    assert "dynamic_sql" not in t.gap_kinds
    assert t.complete


def test_unparseable_insert_writer_is_gap(tmp_path):
    t = trace("parse_failure", "demo.output.parsed_value", tmp_path)
    assert t.gap_kinds & {"parse_error", "unsupported_statement"}
    assert not t.complete
    assert all(s["status"] != "verified" for s in t.steps_for(t.target_id)
               if s["writer_kind"] != "insert_omitted")
    assert (t.target_id, "source_without_writer") not in t.stop_pairs()
    assert t.data["scan"]["fallback"] >= 1
    diagnostics = tmp_path / "scan-diagnostics.json"
    assert diagnostics.exists()
    json.loads(diagnostics.read_text(encoding="utf-8"))


def test_query_hint_is_removed_and_writer_is_traced(tmp_path):
    # OPTION (USE HINT (...)) has no lineage; the INSERT is re-parsed without it.
    t = trace("parse_failure", "demo.output.result_value", tmp_path)
    step = next(s for s in t.steps_for(t.target_id) if s["writer_kind"] == "insert_select")
    assert inputs(step, "value") == ["?.demo.source.input_a"]


def test_update_output_into_is_traced_and_output_target_reported(tmp_path):
    # The OUTPUT clause is blanked so the UPDATE parses; the OUTPUT target is reported, not mapped.
    t = trace("parse_failure", "demo.output.other_value", tmp_path)
    step = next(s for s in t.steps_for(t.target_id) if s["writer_kind"] == "update")
    assert inputs(step, "value") == ["?.demo.source.input_a"]
    diagnostics = json.loads((tmp_path / "scan-diagnostics.json").read_text(encoding="utf-8"))
    assert any(i["kind"] == "unsupported_statement" and "#changed" in i["targets"] for i in diagnostics["statement_issues"])


# ------------------------------------------------------------ CLI contract

def test_output_is_deterministic(tmp_path):
    root = fixture("ambiguous_writers")
    column = "demo.output.result_value"
    assert run_cli(cli_args(root, column, tmp_path)) == 0
    first_json = (tmp_path / "demo.output.result_value.lineage.json").read_bytes()
    first_md = (tmp_path / "demo.output.result_value.lineage.md").read_bytes()
    assert run_cli(cli_args(root, column, tmp_path)) == 0
    assert (tmp_path / "demo.output.result_value.lineage.json").read_bytes() == first_json
    assert (tmp_path / "demo.output.result_value.lineage.md").read_bytes() == first_md


def test_max_depth_stops_traversal(tmp_path):
    t = trace("rename_temp", "demo.output.result_value", tmp_path, "--max-depth", "1")
    assert t.data["boundary"]["max_depth"] == 1
    step = t.only_step_for(t.target_id)
    stage = inputs(step, "value")[0]
    assert t.steps_for(stage) == []
    assert (stage, "max_depth") in t.stop_pairs()
    assert "?.demo.source.input_a" not in t.all_input_ids()


def test_gap_summary_contains_no_names(tmp_path):
    trace("dynamic_sql_sp_executesql", "demo.output.result_value", tmp_path, "--gap-summary")
    summary = tmp_path / "gap-summary.json"
    assert summary.exists()
    text = summary.read_text(encoding="utf-8")
    json.loads(text)
    lowered = text.lower()
    assert "dynamic_sql" in lowered  # counts by kind
    for name in ("demo.", "load_dynamic", "result_value", "input_a", "column_name", ".sql"):
        assert name not in lowered, name


def test_missing_column_option_is_usage_error(tmp_path):
    code = run_cli(["--source-root", str(fixture("rename_temp")), "--output", str(tmp_path), "--no-progress"])
    assert code == 2
