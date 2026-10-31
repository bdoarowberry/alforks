"""Regression tests for the adversarial review of the SSIS adapter (fixtures/column/ssis_review).

Each folder is one invented case (a package root); ``ssis_review/sql/tables.sql`` declares
``rev_db.rv.src(a,b,c)``, ``rv.src2``, ``rv.ref``, ``rv.tgt(x,y,z)`` and ``other_db.rv.tgt(x,y,z)``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from column_helpers import FIXTURES, inputs, load_trace, row_selection, run_cli, trace

REVIEW = FIXTURES / "ssis_review"
SQL = REVIEW / "sql"
TARGET = "rev_db.rv.tgt.x"


def review(case: str | Path, out: Path, column: str = TARGET, *extra: str):
    root = case if isinstance(case, Path) else REVIEW / case
    return trace(SQL, column, out, "--package-root", str(root), *extra)


def package_steps(t):
    return [s for s in t.steps if s["writer_kind"].startswith("package_")]


def issues(out: Path) -> list[dict]:
    return json.loads((out / "scan-diagnostics.json").read_text())["package_issues"]


# 1. Error output of an in-place Derived Column: the original value, never the rewrite.
def test_error_output_does_not_take_the_replacement_expression(tmp_path):
    t = review("error_output_replace", tmp_path)
    dest = t.only_step_for(t.target_id)
    [source] = inputs(dest, "value")
    assert source.endswith("[src.out].a")                 # the column as it arrived, not [dbl.in].a
    assert dest["status"] == "inferred"
    assert any(b.startswith("error output Err of Dbl") for b in dest["branch_context"])
    assert not [s for s in t.steps if s["writer_kind"] == "package_derived_column"]
    assert "constant:2" not in t.all_ids()


# 2. Variables are scoped per container; the innermost definition visible from the component wins.
def test_task_variable_shadows_package_variable(tmp_path):
    rev = review("variable_scope", tmp_path / "rev")
    assert package_steps(rev) == []                       # the package-level value [rv].[tgt] is shadowed
    other = review("variable_scope", tmp_path / "other", "other_db.rv.tgt.x")
    dest = other.only_step_for(other.target_id)
    assert dest["writer_kind"] == "package_destination" and dest["status"] == "inferred"
    assert any("defined on Package\\DF" in n for n in dest["notes"])


# 3. Lookup on a cache connection: never the leftover SqlCommand.
@pytest.mark.parametrize("extra", [(), ("--connection-map", "cache_cm=rev_db")])
def test_cache_lookup_is_unsupported(tmp_path, extra):
    t = review("lookup_cache", tmp_path, TARGET, *extra)
    [gap] = t.gaps("unsupported_package_step")
    assert "cache" in gap["message"] and gap["affects"] == "value"
    assert "rev_db.rv.ref.label" not in t.all_ids()
    assert not [s for s in t.steps if s["writer_kind"] == "package_lookup_reference"]
    assert not t.complete


# 4. ADO.NET double-quoted names.
def test_adonet_quoted_destination_and_source_names(tmp_path):
    dest = review("adonet_quoted", tmp_path / "d")
    assert dest.only_step_for(dest.target_id)["writer_kind"] == "package_destination"
    src = review("adonet_source_quoted", tmp_path / "s")
    assert "rev_db.rv.src.a" in src.upstream(src.target_id)
    assert not any('"' in i for i in src.all_ids())
    assert src.complete


def test_object_name_parts():
    from column_lineage.ssis import object_name_parts
    assert object_name_parts('"rv"."tgt"') == ["rv", "tgt"]
    assert object_name_parts('[rv]."tgt"') == ["rv", "tgt"]
    assert object_name_parts("rv.tgt") == ["rv", "tgt"]
    assert object_name_parts('"a""b".[c]]d]') == ['a"b', "c]d"]
    assert object_name_parts("[db] . [rv] . [tgt]") == ["db", "rv", "tgt"]
    for bad in ('"rv".', "[rv", "rv..tgt", "", "rv tgt"):
        assert object_name_parts(bad) is None, bad


def test_unreadable_table_name_is_counted_and_reported(tmp_path):
    t = review("unreadable_table_name", tmp_path)
    assert package_steps(t) == []
    assert t.data["scan"]["packages"]["unreadable_table_names"] == 1
    assert any(i["kind"] == "package_metadata" and "[extra]" in i["message"] for i in issues(tmp_path))


# 5. Package configurations make connections and variables unproven.
def test_configurations_make_literal_database_unproven(tmp_path):
    t = review("configurations_literal", tmp_path)
    dest = t.only_step_for(t.target_id)
    assert dest["status"] == "ambiguous"
    gaps = t.gaps("package_database_unknown")
    assert gaps and all("configuration" in g["message"] and g["detail"]["configurations"] for g in gaps)
    assert not t.complete


def test_configurations_make_table_variable_runtime(tmp_path):
    t = review("configurations", tmp_path)
    assert package_steps(t) == []
    [gap] = t.gaps("package_runtime_value")
    assert gap["detail"]["variable"] == "User::tbl" and gap["detail"]["design_time_value"] == "[rv].[tgt]"
    assert any("configuration" in s for s in gap["detail"]["set_by"])
    assert gap["affects"] == "value" and not t.complete


# 6. Script Component ReadWriteVariables (pipeline property form) sets the variable at run time.
def test_script_component_read_write_variables_are_runtime(tmp_path):
    t = review("script_component_setter", tmp_path)
    assert package_steps(t) == []
    [gap] = t.gaps("package_runtime_value")
    assert gap["detail"]["set_by"] == ["a Script Component with write access"]


# 7. A destination table from an expression variable: a named gap on the object it names, never an edge.
def test_runtime_destination_table_is_a_gap_on_the_named_object(tmp_path):
    t = review("runtime_table", tmp_path)
    assert package_steps(t) == []
    [gap] = t.gaps("package_runtime_value")
    assert gap["node"] == TARGET and gap["affects"] == "value"
    assert gap["detail"]["expression"] == '"[rv].[" + "tgt]"' and gap["detail"]["design_time_value"] == "[rv].[tgt]"
    assert "User::tbl" in gap["message"]
    assert not t.complete
    assert t.completeness["result"] == "incomplete"
    assert any(i["kind"] == "package_runtime_value" for i in issues(tmp_path))


# 8. Run-time unknowns on the value path are gaps, not stops.
@pytest.mark.parametrize("column,needle", [("rev_db.rv.tgt.x", "$Package::rate"),
                                           ("rev_db.rv.tgt.y", "System::StartTime"),
                                           ("rev_db.rv.tgt.z", "GETDATE()")])
def test_runtime_values_are_gaps(tmp_path, column, needle):
    t = review("runtime_values", tmp_path, column)
    gaps = t.gaps("package_runtime_value")
    assert any(needle in g["message"] and g["affects"] == "value" for g in gaps)
    assert not t.complete and t.completeness["result"] == "incomplete"
    assert not {s["reason"] for s in t.stops()} & {"package_parameter", "package_runtime"}
    assert t.completeness["ends_by_meaning"].get("package_runtime_value")
    # An expression variable never gives a verified step without inputs.
    assert not [s for s in t.steps if s["writer_kind"] == "package_variable" and s["status"] == "verified"]


# 9. Disabled tasks and data flows are not writers, and the stop says so.
def test_disabled_data_flow_is_not_a_writer(tmp_path):
    t = review("disabled_flow", tmp_path)
    assert t.steps == []
    [stop] = t.stops("source_without_writer")
    assert "disabled task in p.dtsx" in stop["detail"] and "maps this column" in stop["detail"]
    assert t.data["scan"]["packages"]["disabled_data_flows"] == 1
    assert any(i.get("detail", {}).get("disabled") for i in issues(tmp_path))


def test_disabled_execute_sql_is_not_indexed(tmp_path):
    t = review("disabled_exec_sql", tmp_path)
    assert t.steps == []
    [stop] = t.stops("source_without_writer")
    assert "disabled task Package\\Load" in stop["detail"]
    assert t.data["scan"]["packages"]["disabled_tasks"] == 1


# 10. Parameters bind by name: Param_N for OLE DB Command, ParameterN for an OLE DB source.
def test_oledb_command_binds_by_param_index(tmp_path):
    x = review("oledb_command_params", tmp_path / "x")
    binding = x.only_step_for(inputs(x.only_step_for(x.target_id), "value")[0])
    assert inputs(binding, "value")[0].endswith("[src.out].a")      # Param_0 is a, although listed second
    y = review("oledb_command_params", tmp_path / "y", "rev_db.rv.tgt.y")
    binding = y.only_step_for(inputs(y.only_step_for(y.target_id), "value")[0])
    assert inputs(binding, "value")[0].endswith("[src.out].b")
    # The third marker has no mapped column: it stays a named run-time value.
    update = x.only_step_for(x.target_id)
    assert any(r.endswith("@ssis_param_2") for r in row_selection(update))


def test_source_parameter_mapping_binds_by_parameter_name(tmp_path):
    t = review("source_param_order", tmp_path)
    query = t.only_step_for(inputs(t.only_step_for(t.target_id), "value")[0])
    binding = t.only_step_for(inputs(query, "value")[0])
    assert inputs(binding, "value")[0].endswith("user::va")         # Parameter0 -> va, though listed second
    assert "constant:1" in t.all_ids() and "constant:2" not in t.all_ids()


# 11. One file reachable from several roots is one package copy.
def test_overlapping_roots_are_deduplicated(tmp_path):
    t = review("overlapping_roots", tmp_path, TARGET, "--package-root", str(REVIEW / "overlapping_roots" / "sub"))
    [dest] = t.steps_for(t.target_id)
    assert dest["status"] == "verified" and t.gaps("ambiguous_package_copy") == []
    assert t.data["scan"]["packages"]["in_scope"] == 1 and t.data["scan"]["packages"]["duplicate_paths"] == 1


def test_file_root_inside_directory_root_is_one_copy(tmp_path):
    t = review("copies_scope", tmp_path, TARGET, "--package-root", str(REVIEW / "copies_scope" / "prod" / "p.dtsx"))
    assert t.gaps("ambiguous_package_copy")[0]["detail"]["copies"] == ["copies_scope/dev/p.dtsx",
                                                                      "copies_scope/prod/p.dtsx"]
    assert len(t.steps_for(t.target_id)) == 2


# 12. Row selection: later split outputs exclude earlier conditions; aggregate keys of the output on the path.
def test_second_split_output_excludes_earlier_condition(tmp_path):
    t = review("split_second_output", tmp_path)
    dest = t.only_step_for(t.target_id)
    assert {r.rsplit(".", 1)[-1] for r in row_selection(dest, "filter")} == {"a", "b"}
    assert all("NOT ([a] > 100) AND [b] > 5" in r["expression"] for r in dest["row_selection"])


def test_aggregate_uses_group_by_of_its_own_output(tmp_path):
    t = review("aggregate_two_outputs", tmp_path)
    agg = t.only_step_for(inputs(t.only_step_for(t.target_id), "value")[0])
    assert [r.rsplit(".", 1)[-1] for r in row_selection(agg, "group_by")] == ["a"]


# 13. Deep expressions are a gap, not a failed trace.
def test_deep_condition_is_a_row_selection_gap(tmp_path):
    t = review("deep_condition", tmp_path)
    assert t.gaps("unsupported_statement") == []
    [gap] = t.gaps("package_expression")
    assert gap["affects"] == "row_selection" and "nested too deeply" in gap["message"]
    assert t.only_step_for(t.target_id)["writer_kind"] == "package_destination"


def test_deep_derived_expression_is_a_value_gap(tmp_path):
    root = tmp_path / "pkgs"
    root.mkdir()
    text = (REVIEW / "error_output_replace" / "p.dtsx").read_text()
    deep = "(" * 400 + "#{Package\\DF\\Src.Outputs[Out].Columns[a]}" + ")" * 400
    text = text.replace("#{Package\\DF\\Src.Outputs[Out].Columns[a]} * 2", deep)
    text = text.replace('startId="Package\\DF\\Dbl.Outputs[Err]"', 'startId="Package\\DF\\Dbl.Outputs[Ok]"')
    (root / "p.dtsx").write_text(text)
    t = review(root, tmp_path / "out")
    assert t.gaps("package_expression") and not t.complete
    assert t.gaps("unsupported_statement") == []


def test_merge_failure_is_a_gap_not_a_failed_trace(tmp_path, monkeypatch):
    from column_lineage import ssis

    def boom(self, node, found):
        raise RuntimeError("injected")
    monkeypatch.setattr(ssis.PackageCatalog, "_merge", boom)
    t = review("copies_scope", tmp_path)
    [gap] = t.gaps("unsupported_package_step")
    assert "injected" in gap["message"] and t.target_id == TARGET


# 14. Classification never uses the author-editable description.
def test_description_alone_does_not_classify(tmp_path):
    root = tmp_path / "pkgs"
    root.mkdir()
    text = (REVIEW / "copies_scope" / "prod" / "p.dtsx").read_text()
    text = text.replace('componentClassID="Microsoft.OLEDBDestination" contactInfo="OLE DB Destination;Microsoft '
                        'Corporation;x;0"', 'componentClassID="{0F0F0F0F-0000-4000-8000-000000000001}"')
    assert 'description="OLE DB Destination"' in text
    (root / "p.dtsx").write_text(text)
    t = review(root, tmp_path / "out")
    assert package_steps(t) == []
    assert t.data["scan"]["packages"]["destinations"] == 0


def test_unknown_components_on_the_path_are_unsupported(tmp_path):
    t = review("unsupported_in_place", tmp_path, "rev_db.rv.tgt.y")
    kinds = {g["detail"].get("component") for g in t.gaps("unsupported_package_step")}
    assert kinds == {"Upper", "Thing"}
    assert t.only_step_for(t.target_id)["status"] == "partial"


def test_duplicate_lineage_id_follows_the_path(tmp_path):
    t = review("duplicate_lineage", tmp_path)
    [source] = inputs(t.only_step_for(t.target_id), "value")
    assert source.endswith("[src.out].a")
    assert "rev_db.rv.src.a" in t.upstream(t.target_id)


def test_gap_summary_stays_nameless_with_review_cases(tmp_path):
    args = ["--column", TARGET, "--source-root", str(SQL), "--output", str(tmp_path), "--no-progress", "--gap-summary"]
    for case in ("runtime_table", "disabled_flow", "configurations", "unreadable_table_name"):
        args += ["--package-root", str(REVIEW / case)]
    assert run_cli(args) == 0
    text = (tmp_path / "gap-summary.json").read_text().lower()
    for name in ("rev_db", "rv.", "tgt", "user::", "p.dtsx", "cfg", "dtsconfig", "srv_x"):
        assert name not in text, name
    summary = json.loads(text)["packages"]
    assert summary["disabled_data_flows"] == 1 and summary["unreadable_table_names"] == 1


def test_disabled_task_without_targets_is_not_a_gap_on_traces(tmp_path):
    """Tester round: a disabled audit task in the same package put unsupported_package_step context gaps
    on every trace through the package. A disabled task does not run as saved: it stays a package issue."""
    root = REVIEW.parent / "ssis_review" / "disabled_audit"
    t = trace(REVIEW.parent / "ssis" / "sql", "demo_db.demo.stage.amount", tmp_path, "--package-root", str(root))
    assert "unsupported_package_step" not in t.gap_kinds
    assert t.complete
    assert t.data["scan"]["packages"]["disabled_tasks"] == 1


def test_expression_shape_is_anonymized_and_tags_reference_kinds():
    from column_lineage import ssis_expression as sx
    text = 'LEN(TRIM(#{Package\\Load\\Src.Outputs[Out].Columns[Customer Name]})) == 0 && [Order Ref] != "ABC" && @[User::Cutoff] > 5'
    shape = sx.shape(text)
    assert shape == 'LEN ( TRIM ( #ID1 ) ) == 0 && [ID2] != "?" && @ID3 > 0'
    for name in ("Customer", "Order", "ABC", "Cutoff", "Package"):
        assert name not in shape
