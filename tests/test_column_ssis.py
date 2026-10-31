"""SSIS package adapter (src/column_lineage/ssis.py) against hand-written fixtures in fixtures/column/ssis.

Every scenario uses the shared SQL root ``fixtures/column/ssis/sql`` (``USE demo_db``) plus one
package root per scenario.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from column_helpers import FIXTURES, inputs, load_trace, row_selection, row_selection_pairs, run_cli, trace

SSIS = FIXTURES / "ssis"
SQL = SSIS / "sql"
PACKAGE_STEP = "package_"


def ssis_trace(scenario: str | Path, column: str, out: Path, *extra: str):
    root = scenario if isinstance(scenario, Path) else SSIS / scenario
    return trace(SQL, column, out, "--package-root", str(root), *extra)


def package_steps(t) -> list[dict]:
    return [s for s in t.steps if s["writer_kind"].startswith(PACKAGE_STEP)]


def assert_package_evidence(t, root: Path) -> None:
    """Every package step cites a package file under its root, a line, a role and its package copy."""
    steps = package_steps(t)
    assert steps
    for step in steps:
        assert step["evidence"], step
        for ev in step["evidence"]:
            assert ev["path"].endswith(".dtsx") and (root / ev["path"]).is_file(), ev
            assert ev["start_line"] >= 1 and ev["role"]
        assert step["procedure"].startswith("package "), step["procedure"]
        assert any(n.startswith("From package copy ") for n in step["notes"]), step["notes"]


# ----------------------------------------------------------------------------- acceptance

def test_acceptance_flat_file_conversion_destination(tmp_path):
    t = ssis_trace("basic", "demo.stage.amount", tmp_path)
    assert t.target_id == "demo_db.demo.stage.amount"

    dest = t.only_step_for(t.target_id)
    assert dest["writer_kind"] == "package_destination"
    assert dest["mapping_method"] == "destination_column_mapping"
    [converted] = inputs(dest, "value")
    assert converted.startswith("package_flow:basic/load_stage.dtsx::") and converted.endswith(".converted_amount")
    assert t.nodes[converted]["kind"] == "package_column"
    assert any("@[User::run_load] == 1" in b for b in dest["branch_context"])   # precedence constraint expression

    conversion = t.only_step_for(converted)
    assert conversion["writer_kind"] == "package_data_conversion"
    assert conversion["operation"] == "cast"
    [source_col] = inputs(conversion, "value")
    assert source_col.endswith(".source_amount") and t.nodes[source_col]["kind"] == "package_column"

    source = t.only_step_for(source_col)
    assert source["writer_kind"] == "package_source"
    [file_col] = inputs(source, "value")
    assert t.nodes[file_col]["kind"] == "file_column"
    assert file_col == "flat_file:basic/load_stage.dtsx::file_a.source_amount"
    [stop] = [s for s in t.stops("external_file") if s["node"].lower() == file_col]
    assert "file_a.csv" in stop["detail"] and "file_a" in stop["detail"]

    assert "source_code" not in json.dumps([s["inputs"] for s in t.steps])   # read but not mapped
    assert t.gaps() == []
    assert t.complete
    assert_package_evidence(t, SSIS / "basic")
    assert all(ev["path"] == "load_stage.dtsx" for s in t.steps for ev in s["evidence"])
    md = t.markdown
    assert "external file read by an SSIS package" in md and "load_stage.dtsx" in md
    assert "SSIS packages" in md


def test_acceptance_unmapped_column_is_not_explained_by_the_load(tmp_path):
    t = ssis_trace("basic", "demo_db.demo.stage.unmapped_column", tmp_path)
    assert t.steps == []
    assert not t.complete
    [gap] = t.gaps("package_column_not_mapped")
    assert gap["node"] == "demo_db.demo.stage.unmapped_column" and gap["affects"] == "value"
    assert "Write stage" in gap["message"] and "load_stage.dtsx" in gap["message"]
    [stop] = t.stops("source_without_writer")
    assert "load_stage.dtsx" in stop["detail"] and "map no input to this column" in stop["detail"]


def test_identity_column_left_out_by_destination_gets_no_gap(tmp_path):
    t = ssis_trace("basic", "demo_db.demo.stage.stage_id", tmp_path)
    assert t.steps == [] and t.gaps("package_column_not_mapped") == []


def test_without_package_root_nothing_changes(tmp_path):
    t = trace(SQL, "demo_db.demo.stage.amount", tmp_path)
    assert t.steps == []
    assert "packages" not in t.data["scan"]
    assert {s["reason"] for s in t.stops()} == {"source_without_writer"}
    diagnostics = json.loads((tmp_path / "scan-diagnostics.json").read_text())
    assert "package_issues" not in diagnostics


def test_scan_reports_package_counts(tmp_path):
    t = ssis_trace("basic", "demo.stage.amount", tmp_path)
    packages = t.data["scan"]["packages"]
    assert packages["scanned"] == 1 and packages["in_scope"] == 1 and packages["excluded"] == 0
    assert packages["data_flows"] == 1 and packages["destinations"] == 1
    assert packages["execute_sql_tasks"] == 1 and packages["execute_sql_indexed"] == 1
    assert t.data["scan"]["files"] == 1          # Execute SQL text is not counted as a SQL file


# ----------------------------------------------------------------------------- transformations

def test_conditional_split_filter_is_row_selection(tmp_path):
    t = ssis_trace("split", "demo_db.demo.split_target.amount", tmp_path)
    dest = t.only_step_for(t.target_id)
    [amount] = inputs(dest, "value")
    assert amount.endswith("[read orders.ole db source output].amount")
    status = amount.rsplit(".", 1)[0] + ".status_code"
    assert (status, "filter") in row_selection_pairs(dest)
    assert status not in inputs(dest)                     # the filter column is not a value input
    [row] = dest["row_selection"]
    assert '[status_code] == "A"' in row["expression"] and "Split by status" in row["expression"]
    assert any(b.startswith("Conditional Split Split by status output Active") for b in dest["branch_context"])
    assert "demo_db.demo.orders.amount" in t.upstream(t.target_id)
    assert t.complete


def test_conditional_split_default_output_negates_the_other_conditions(tmp_path):
    root = tmp_path / "pkgs"
    root.mkdir()
    text = (SSIS / "split" / "load_split.dtsx").read_text()
    text = text.replace('startId="Package\\Split orders\\Split by status.Outputs[Active]"',
                        'startId="Package\\Split orders\\Split by status.Outputs[Other]"')
    (root / "load_other.dtsx").write_text(text)
    t = ssis_trace(root, "demo_db.demo.split_target.amount", tmp_path / "out")
    dest = t.only_step_for(t.target_id)
    [row] = dest["row_selection"]
    assert row["role"] == "filter" and 'NOT ([status_code] == "A")' in row["expression"]
    assert any("default output Other" in b for b in dest["branch_context"])


def test_derived_column_conditional_expression(tmp_path):
    t = ssis_trace("derived", "demo_db.demo.derived_target.chosen_amount", tmp_path)
    dest = t.only_step_for(t.target_id)
    [derived] = inputs(dest, "value")
    step = t.only_step_for(derived)
    assert step["writer_kind"] == "package_derived_column" and step["operation"] == "case"
    by_node = {i["node"].lower().rsplit(".", 1)[-1]: i for i in step["inputs"]}
    assert by_node["flag"]["role"] == "condition" and by_node["flag"]["branch"] == "WHEN [flag] == 1"
    assert by_node["amount"]["role"] == "value" and by_node["amount"]["branch"] == "WHEN [flag] == 1 THEN"
    assert any(i["node"] == "constant:0" and i["branch"] == "ELSE" for i in step["inputs"])
    assert step["expression"] == "flag == 1 ? amount : (DT_NUMERIC,18,2)0"
    assert {"demo_db.demo.orders.amount", "demo_db.demo.orders.flag"} <= t.upstream(t.target_id, ("value", "condition"))
    assert t.complete


def test_derived_column_replacing_a_column_in_place(tmp_path):
    t = ssis_trace("derived", "demo_db.demo.derived_target.order_id", tmp_path)
    dest = t.only_step_for(t.target_id)
    [replaced] = inputs(dest, "value")
    assert "[choose amount.derived column input]" in replaced     # the value as rewritten by the component
    step = t.only_step_for(replaced)
    assert step["operation"] == "arithmetic" and step["expression"] == "order_id * 10"
    assert any("in place" in n for n in step["notes"])
    assert "demo_db.demo.orders.order_id" in t.upstream(t.target_id)


def test_unparseable_derived_expression_is_partial_with_gap(tmp_path):
    root = tmp_path / "pkgs"
    root.mkdir()
    text = (SSIS / "derived" / "load_derived.dtsx").read_text()
    text = text.replace("== 1 ? #{", "== 1 ?? #{")
    (root / "load_bad.dtsx").write_text(text)
    t = ssis_trace(root, "demo_db.demo.derived_target.chosen_amount", tmp_path / "out")
    dest = t.only_step_for(t.target_id)
    [derived] = inputs(dest, "value")
    step = t.only_step_for(derived)
    assert step["status"] == "partial"
    assert t.gaps("package_expression") and not t.complete
    # The lineage ids written in the expression are still shown.
    assert len(step["inputs"]) == 2


def test_lookup_join_keys_and_reference_column(tmp_path):
    t = ssis_trace("lookup", "demo_db.demo.lookup_target.region_label", tmp_path)
    dest = t.only_step_for(t.target_id)
    [label] = inputs(dest, "value")
    lookup = t.only_step_for(label)
    assert lookup["writer_kind"] == "package_lookup"
    keys = row_selection(lookup, "join_key")
    assert any(k.endswith("[read orders.ole db source output].region_code") for k in keys)
    assert any(k.endswith("[find region.reference].region_code") for k in keys)
    assert row_selection(dest, "join_key")                      # rows reaching the destination passed the lookup
    [reference] = inputs(lookup, "value")
    ref_step = t.only_step_for(reference)
    assert ref_step["writer_kind"] == "package_lookup_reference"
    assert inputs(ref_step, "value") == ["demo_db.demo.region.region_label"]
    assert not any(k in inputs(lookup) for k in keys)
    assert t.complete


def test_sql_command_source_is_mapped_by_the_sql_analyzer(tmp_path):
    t = ssis_trace("query", "demo_db.demo.query_target.doubled_amount", tmp_path,
                   "--connection-map", "source_conn=demo_db")
    dest = t.only_step_for(t.target_id)
    [col] = inputs(dest, "value")
    query = t.only_step_for(col)
    assert query["writer_kind"] == "package_source_query" and query["operation"] == "arithmetic"
    assert "demo_db.demo.orders.amount" in inputs(query, "value")
    assert ("demo_db.demo.orders.status_code", "filter") in row_selection_pairs(query)
    assert query["evidence"][0]["path"] == "load_query.dtsx"
    assert query["status"] == "verified"
    assert t.complete


def test_sql_command_source_without_database_follows_unknown_database_rules(tmp_path):
    t = ssis_trace("query", "demo.query_target.doubled_amount", tmp_path)
    dest = t.only_step_for(t.target_id)
    assert dest["status"] == "inferred"            # matched through the only known database for the name
    query = t.only_step_for(inputs(dest, "value")[0])
    assert query["status"] == "inferred"
    assert any("--connection-map" in n for n in query["notes"])


def test_sql_command_from_expression_variable_is_a_named_gap(tmp_path):
    t = ssis_trace("variable", "demo_db.demo.variable_target.amount", tmp_path)
    dest = t.only_step_for(t.target_id)
    [col] = inputs(dest, "value")
    assert t.steps_for(col) == []                  # no guessed mapping from the design-time value
    [gap] = t.gaps("package_runtime_value")
    assert gap["node"].lower() == col and gap["affects"] == "value"
    assert gap["detail"]["variable"] == "User::source_query"
    assert "@[User::region]" in gap["detail"]["expression"]
    assert "User::source_query" in gap["message"]
    assert "demo_db.demo.orders.amount" not in t.all_ids()
    assert not t.complete


# ----------------------------------------------------------------------------- copies and scope

def test_two_package_copies_are_ambiguous(tmp_path):
    t = ssis_trace("copies", "demo_db.demo.copy_target.copy_value", tmp_path)
    dests = t.steps_for(t.target_id)
    assert len(dests) == 2
    assert {s["status"] for s in dests} == {"ambiguous"}
    [gap] = t.gaps("ambiguous_package_copy")
    assert gap["detail"]["copies"] == ["copies/dev/load_copy.dtsx", "copies/prod/load_copy.dtsx"]
    assert all(gap["id"] in s["gaps"] for s in dests)
    assert {s["evidence"][0]["path"] for s in dests} == {"dev/load_copy.dtsx", "prod/load_copy.dtsx"}
    assert not t.complete
    assert t.completeness["overall_step_status"] == "ambiguous"


def test_package_exclude_narrows_copies(tmp_path):
    t = ssis_trace("copies", "demo_db.demo.copy_target.copy_value", tmp_path, "--package-exclude", "dev/*")
    [dest] = t.steps_for(t.target_id)
    assert dest["status"] == "verified" and dest["evidence"][0]["path"] == "prod/load_copy.dtsx"
    assert t.gaps("ambiguous_package_copy") == []
    assert t.complete
    packages = t.data["scan"]["packages"]
    assert (packages["scanned"], packages["in_scope"], packages["excluded"]) == (2, 1, 1)


def test_package_include_narrows_copies(tmp_path):
    t = ssis_trace("copies", "demo_db.demo.copy_target.copy_value", tmp_path, "--package-include", "dev/*.dtsx")
    [dest] = t.steps_for(t.target_id)
    assert dest["evidence"][0]["path"] == "dev/load_copy.dtsx" and dest["status"] == "verified"


# ----------------------------------------------------------------------------- gaps

def test_unsupported_component_on_the_path(tmp_path):
    t = ssis_trace("unsupported", "demo_db.demo.script_target.amount", tmp_path)
    dest = t.only_step_for(t.target_id)
    assert dest["status"] == "partial"
    [gap] = t.gaps("unsupported_package_step")
    assert gap["id"] in dest["gaps"] and gap["affects"] == "value"
    assert gap["detail"]["component"] == "Adjust rows"
    assert gap["detail"]["package"] == "unsupported/load_script.dtsx"
    assert gap["detail"]["task"] == "Package\\Scripted load"
    assert gap["evidence"][0]["path"] == "load_script.dtsx"
    assert not t.complete


def test_matching_names_alone_never_make_an_edge(tmp_path):
    # Package, task and source component are named after the column, the destination loads the table and its
    # input column is called amount - but the metadata maps it to other_value.
    t = ssis_trace("name_only", "demo_db.demo.name_target.amount", tmp_path)
    assert package_steps(t) == [] and t.steps == []
    assert t.gaps("package_column_not_mapped")
    assert not t.complete
    other = ssis_trace("name_only", "demo_db.demo.name_target.other_value", tmp_path / "other")
    dest = other.only_step_for(other.target_id)
    assert dest["operation"] == "rename" and dest["expression"] == "amount"
    assert "demo_db.demo.orders.amount" in other.upstream(other.target_id)


def test_ssis_2008_format_with_integer_ids(tmp_path):
    t = ssis_trace("legacy", "demo_db.demo.legacy_target.amount_text", tmp_path)
    dest = t.only_step_for(t.target_id)
    [derived] = inputs(dest, "value")
    step = t.only_step_for(derived)
    assert step["operation"] == "cast" and step["expression"] == "(DT_STR,30,1252)amount"
    assert "demo_db.demo.orders.amount" in t.upstream(t.target_id)
    # 2008 precedence constraints refer to executables by DTSID.
    assert any(b.startswith("IF @[User::enabled]") and "Clear legacy" in b for b in dest["branch_context"])
    assert_package_evidence(t, SSIS / "legacy")
    assert t.complete


def test_union_all_branches_and_aggregate_group_by(tmp_path):
    t = ssis_trace("union", "demo_db.demo.union_target.total_amount", tmp_path)
    [total] = inputs(t.only_step_for(t.target_id), "value")
    agg = t.only_step_for(total)
    assert agg["writer_kind"] == "package_aggregate" and agg["operation"] == "aggregate"
    assert agg["expression"] == "SUM(amount)"
    [group] = row_selection(agg, "group_by")
    assert group.endswith("[union all.union all output 1].region_code")
    [union_amount] = inputs(agg, "value")
    branches = t.steps_for(union_amount)
    assert sorted(b["branch_context"][-1] for b in branches) == [
        "Union All Union All input 1 of 2 (Union All Input 1)", "Union All Union All input 2 of 2 (Union All Input 2)"]
    assert {"demo_db.demo.orders.amount", "demo_db.demo.orders_archive.amount"} <= t.upstream(t.target_id)
    assert t.complete
    # An input that maps nothing to an output column contributes NULL rows.
    r = ssis_trace("union", "demo_db.demo.union_target.region_code", tmp_path / "region")
    assert "constant:null" in r.all_input_ids()


def test_execute_sql_from_variable_is_a_context_gap_for_traces_through_the_package(tmp_path):
    root = tmp_path / "pkgs"
    root.mkdir()
    text = (SSIS / "basic" / "load_stage.dtsx").read_text()
    text = text.replace('SQLTask:SqlStatementSource="TRUNCATE TABLE demo.stage;"',
                        'SQLTask:SqlStmtSourceType="Variable" SQLTask:SqlStatementSource="User::run_load"')
    (root / "load_var_sql.dtsx").write_text(text)
    t = ssis_trace(root, "demo.stage.amount", tmp_path / "out")
    [gap] = t.gaps("package_runtime_value")
    assert gap["affects"] == "context" and gap["detail"]["variable"] == "User::run_load"
    assert "Truncate stage" in gap["message"]
    assert not t.complete


def test_execute_sql_task_statements_feed_the_sql_catalog(tmp_path):
    t = ssis_trace("exec_sql", "demo_db.demo.audit.row_total", tmp_path)
    insert = t.only_step_for(t.target_id)
    assert insert["writer_kind"] == "insert_values"
    assert insert["evidence"][0]["path"] == "write_audit.dtsx"
    assert "Write audit" in insert["procedure"]
    assert insert["branch_context"] == ["IF @[User::loaded_rows] > 0 AND Count rows succeeds (precedence constraint)"]
    [param] = inputs(insert, "value")
    binding = t.only_step_for(param)
    assert binding["writer_kind"] == "package_parameter_binding"
    [variable] = inputs(binding, "value")
    assert t.nodes[variable]["kind"] == "package_variable"
    [gap] = t.gaps("package_runtime_value")
    assert gap["detail"]["variable"] == "User::loaded_rows" and "Count rows" in gap["message"]
    assert not t.complete


# ----------------------------------------------------------------------------- summaries

def test_gap_summary_counts_without_names(tmp_path):
    columns = ["demo.stage.amount", "demo_db.demo.copy_target.copy_value", "demo_db.demo.script_target.amount",
               "demo_db.demo.variable_target.amount", "demo_db.demo.name_target.amount"]
    args = []
    for c in columns:
        args += ["--column", c]
    for scenario in ("basic", "copies", "unsupported", "variable", "name_only"):
        args += ["--package-root", str(SSIS / scenario)]
    code = run_cli([*args, "--source-root", str(SQL), "--output", str(tmp_path), "--no-progress", "--gap-summary"])
    assert code == 0
    text = (tmp_path / "gap-summary.json").read_text()
    summary = json.loads(text)
    packages = summary["packages"]
    assert packages["packages_in_scope"] == 6 and packages["packages_excluded"] == 0
    assert packages["package_formats"] == {"2012+": 6}
    assert packages["components_by_class"]["unsupported"] == 1
    kinds = {}
    for entry in summary["traces"]:
        for k, v in entry["package_gaps_by_kind"].items():
            kinds[k] = kinds.get(k, 0) + v
    assert {"ambiguous_package_copy", "unsupported_package_step", "package_runtime_value",
            "package_column_not_mapped"} <= set(kinds)
    assert any(entry["package_steps"] for entry in summary["traces"])
    lowered = text.lower()
    for name in ("demo", "stage", "file_a", "file_b", "load_", ".dtsx", "orders", "adjust rows", "script_target",
                 "user::", "package\\\\", "source_query", "copy_value", "server_", "inbound"):
        assert name not in lowered, name


def test_scan_diagnostics_list_package_issues(tmp_path):
    root = tmp_path / "pkgs"
    root.mkdir()
    shutil.copy(SSIS / "basic" / "load_stage.dtsx", root / "good.dtsx")
    (root / "broken.dtsx").write_text("<DTS:Executable xmlns:DTS='www.microsoft.com/SqlServer/Dts'>")
    t = ssis_trace(root, "demo.stage.amount", tmp_path / "out")
    assert t.data["scan"]["packages"]["unreadable"] == 1
    diagnostics = json.loads((tmp_path / "out" / "scan-diagnostics.json").read_text())
    assert any(e["kind"] == "unreadable_package" and e["path"] == "broken.dtsx" for e in diagnostics["package_issues"])
    assert t.complete


def test_package_root_may_be_a_file(tmp_path):
    t = ssis_trace(SSIS / "basic" / "load_stage.dtsx", "demo.stage.amount", tmp_path)
    assert t.complete and t.data["scan"]["packages"]["in_scope"] == 1
    assert {ev["path"] for s in t.steps for ev in s["evidence"]} == {"load_stage.dtsx"}


def test_connection_map_overrides_initial_catalog(tmp_path):
    t = ssis_trace("basic", "other_db.demo.stage.amount", tmp_path, "--connection-map", "demo_db=other_db")
    dest = t.only_step_for(t.target_id)
    assert dest["writer_kind"] == "package_destination"
    assert any("--connection-map" in n for n in dest["notes"])


def test_connection_map_rejects_bad_value(tmp_path):
    code = run_cli(["--column", "demo.stage.amount", "--source-root", str(SQL), "--package-root",
                    str(SSIS / "basic"), "--connection-map", "no_equals", "--output", str(tmp_path), "--no-progress"])
    assert code == 2


def test_all_package_roots_together(tmp_path):
    t = ssis_trace(SSIS, "demo.stage.amount", tmp_path)
    assert t.complete           # other packages in the tree do not write demo.stage
    assert t.data["scan"]["packages"]["in_scope"] == 13
