"""Adversarial review of the gap-shape rules (fixtures/column/review6/, invented): each case was a
false complete or a wrong binding."""
from column_helpers import FIXTURES, inputs, trace

ROOT = FIXTURES / "review6"
DB = ("--default-database", "demo")


def value_ends(t):
    seen, frontier, ends = set(), [t.target_id], set()
    while frontier:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        steps = t.steps_for(node)
        if not steps and node != t.target_id:
            ends.add(node)
        for step in steps:
            frontier.extend(inputs(step, "value"))
    return ends


def blocking(t):
    return {g["kind"] for g in t.data["gaps"] if g["affects"] != "row_selection"}


def run(column, tmp_path):
    return trace(ROOT, column, tmp_path / column, *DB)


def test_union_branch_after_unexpanded_star_is_not_dropped(tmp_path):
    for column in ("demo.dbo.Out_u.col_a", "demo.dbo.Out_v.col_a"):
        t = run(column, tmp_path)
        assert "wildcard_without_schema" in blocking(t) and not t.complete


def test_function_argument_is_not_captured_by_the_body_alias(tmp_path):
    t = run("demo.dbo.Out_shadow.pid", tmp_path)
    assert value_ends(t) == {"demo.dbo.parent.code"}


def test_function_body_names_do_not_escape_to_the_caller(tmp_path):
    t = run("demo.dbo.Out_escape.code", tmp_path)
    assert "demo.dbo.parent.code" not in value_ends(t)
    assert not t.complete and t.data["cycles"] == []


def test_function_of_another_database_is_not_inlined_here(tmp_path):
    t = run("demo.dbo.Out_other_db.col_a", tmp_path)
    assert "demo.dbo.thing.col_a" not in value_ends(t) and not t.complete


def test_cte_in_function_return_query(tmp_path):
    t = run("demo.dbo.Out_fn_cte.col_a", tmp_path)
    assert value_ends(t) == {"demo.dbo.src.col_a"}


def test_callee_with_other_row_returning_statements_stays_a_gap(tmp_path):
    for column in ("demo.dbo.Out_del.col_a", "demo.dbo.Out_fetch.col_a"):
        t = run(column, tmp_path)
        assert "insert_exec" in blocking(t)


def test_callee_reads_the_calling_procedures_temp_table(tmp_path):
    t = run("demo.dbo.Out_caller_temp.v", tmp_path)
    assert value_ends(t) == {"demo.dbo.src.col_a"}


def test_conditionally_created_temp_table_keeps_the_callers_gap(tmp_path):
    t = run("demo.dbo.Out_conditional.amount", tmp_path)
    assert "insert_exec" in blocking(t) and not t.complete
