"""sp_executesql with a parameter list (fixtures/column/sp_executesql_params/, invented): each declared
parameter is bound to its argument, so the evaluated statement reads the caller's value."""
from column_helpers import FIXTURES, inputs, trace

ROOT = FIXTURES / "sp_executesql_params"
DB = ("--default-database", "demo")


def ends(t):
    return {s["node"] for s in t.data["stops"]}


def test_named_arguments_reach_the_callers_value(tmp_path):
    t = trace(ROOT, "demo.dbo.tgt.batch_id", tmp_path, *DB)
    assert t.complete and ends(t) == {"constant:42"}
    assert t.only_step_for(t.target_id)["status"] == "inferred"   # evaluated dynamic SQL
    rows = [r for s in t.steps for r in s["row_selection"]]
    assert any(r["node"].endswith("::@region") for r in rows)   # WHERE s.region = @r reads the caller's @region


def test_positional_arguments_and_a_parameter_list_in_a_variable(tmp_path):
    t = trace(ROOT, "demo.dbo.tgt_pos.batch_id", tmp_path, *DB)
    assert t.complete and ends(t) == {"constant:7"}


def test_output_parameters_keep_the_gap(tmp_path):
    t = trace(ROOT, "demo.dbo.tgt_out.batch_id", tmp_path, *DB)
    assert "dynamic_sql" in t.gap_kinds and not t.complete


def test_a_parameter_without_an_argument_keeps_the_gap(tmp_path):
    t = trace(ROOT, "demo.dbo.tgt_missing.batch_id", tmp_path, *DB)
    assert "dynamic_sql" in t.gap_kinds and not t.complete


def test_parameter_names_inside_string_literals_are_text(tmp_path):
    t = trace(ROOT, "demo.dbo.tgt_str.note", tmp_path, *DB)
    assert t.complete
    assert inputs(t.only_step_for(t.target_id), "value") == ["constant:'uses @b'"]
