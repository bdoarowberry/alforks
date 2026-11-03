"""T-SQL constructs probed after the sqlleaf comparison (fixtures/column/tsql_constructs/, invented).

Each case was a false complete, a wrong binding or an unexplained end before the fix."""
import pytest

from column_helpers import FIXTURES, inputs, trace

ROOT = FIXTURES / "tsql_constructs"
DB = ("--default-database", "demo")


def run(case, column, tmp_path):
    return trace(ROOT / case, column, tmp_path / case, *DB)


def ends(t):
    return {s["node"] for s in t.data["stops"]}


@pytest.mark.parametrize("case, column", [
    ("c01d_pivot_writer", "demo.dbo.pivot_tgt.a_amt"),          # PIVOT output bound as a source column
    ("c02b_unpivot_writer", "demo.dbo.unpivot_tgt.attr_val"),   # UNPIVOT the same
    ("c06c_output_misc", "demo.dbo.cust_dim.cust_name"),        # MERGE nested inside INSERT ... SELECT FROM (...)
])
def test_untraced_constructs_are_gaps_not_completes(case, column, tmp_path):
    t = run(case, column, tmp_path)
    assert not t.complete and t.gap_kinds


def test_identity_function_is_a_generated_value(tmp_path):
    t = run("c10d_identity_noddl", "demo.dbo.into_tgt2.row_id", tmp_path)
    assert ends(t) == {"constant:IDENTITY"}


def test_unqualified_next_value_for_is_a_sequence(tmp_path):
    t = run("c11d_seq_noddl", "demo.dbo.seq_tgt.id", tmp_path)
    assert ends(t) == {"constant:NEXT VALUE FOR seq_id"}


def test_cursor_over_union_reads_every_branch(tmp_path):
    t = run("c13_cursor", "demo.dbo.cur_tgt.v", tmp_path)
    assert {"demo.dbo.cur_src1.v", "demo.dbo.cur_src2.v"} <= ends(t)


def test_default_keyword_in_values_is_the_declared_default(tmp_path):
    t = run("c10c_default_kw", "demo.dbo.dflt_tgt.status_cd", tmp_path)
    assert "constant:'NEW'" in ends(t)


def test_cross_apply_values_unpivot_idiom(tmp_path):
    t = run("c03_apply_values", "demo.dbo.unpivot_tgt.attr_val", tmp_path)
    assert t.complete and {"demo.dbo.wide_src.col_x", "demo.dbo.wide_src.col_y"} <= ends(t)
    assert not [g for g in t.data["gaps"] if g["kind"] == "unexplained_end"]


def test_choose_keeps_its_choices_and_uses_the_index_as_a_condition(tmp_path):
    t = run("c08b_funcs", "demo.dbo.f_tgt.choose_v", tmp_path)
    step = next(s for s in t.steps_for(t.target_id) if "CHOOSE" in s["expression"])
    roles = {i["node"]: i["role"] for i in step["inputs"]}
    assert roles["demo.dbo.f_src.idx"] == "condition"
    assert {"constant:'low'", "constant:'mid'", "constant:'high'", "constant:null"} <= set(inputs(step, "value"))


def test_lag_default_is_a_value(tmp_path):
    t = run("c07_window", "demo.dbo.win_tgt.prev_amt", tmp_path)
    step = next(s for s in t.steps_for(t.target_id) if "LAG" in s["expression"])
    assert "constant:-1" in inputs(step, "value")


@pytest.mark.parametrize("column", ["d", "n", "ts", "s"])
def test_literal_only_expressions_record_their_end(column, tmp_path):
    t = run("c16_noinput", f"demo.dbo.ni_tgt.{column}", tmp_path / column)
    assert t.complete and ends(t) and all(e.startswith("constant:") for e in ends(t))


def test_concat_ws_puts_the_separator_between_pieces(tmp_path):
    t = run("r7_concat_ws", "demo.dbo.tgt.amount", tmp_path)
    assert "demo.dbo.src.amount" in ends(t) and "demo.dbo.src_eu.amount" not in ends(t)


def test_isnull_takes_the_first_arguments_type(tmp_path):
    t = run("r8_isnull_len", "demo.dbo.tgt.amount", tmp_path)
    assert "demo.dbo.src_eu.amount" not in ends(t)
