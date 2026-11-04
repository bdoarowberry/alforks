"""Static evaluation of dynamic SQL whose text can be enumerated from the code (fixtures/column/dynamic/).

When every possible value of the executed string is built from literals, the string's statements are
analysed as writers at the EXEC: their steps are `inferred`, carry the alternative's condition in
branch_context and a note saying where the SQL was built. Anything unknown keeps the dynamic_sql gap,
and the gap's detail says which piece was unknown and why.
"""
from __future__ import annotations

import json

from column_helpers import FIXTURES, assert_step_evidence, inputs, trace

SOURCE_A = "?.demo.source.input_a"
SOURCE_B = "?.demo.source.input_b"


def dyn(name: str):
    return FIXTURES / "dynamic" / name


def evaluated(steps: list[dict]) -> list[dict]:
    return [s for s in steps if any(n.startswith("Evaluated from dynamic SQL") for n in s["notes"])]


def diagnostics(out) -> list[dict]:
    return json.loads((out / "scan-diagnostics.json").read_text(encoding="utf-8"))["statement_issues"]


def assert_evaluated_step(step: dict, built_line: int | None = None) -> None:
    assert step["status"] == "inferred"
    notes = [n for n in step["notes"] if n.startswith("Evaluated from dynamic SQL")]
    assert len(notes) == 1
    assert "load.sql:" in notes[0]
    if built_line is not None:
        assert f"load.sql:{built_line}" in notes[0]


def test_plain_literal_fills_temp_table(tmp_path):
    t = trace(dyn("literal"), "demo.output.result_value", tmp_path)
    stage = t.temp_column("#stage", "staged_value")
    assert inputs(t.only_step_for(t.target_id), "value") == [stage]
    step = t.only_step_for(stage)
    assert step["writer_kind"] == "insert_select"
    assert inputs(step, "value") == [SOURCE_A]
    assert_evaluated_step(step, 6)
    # The #temp keeps the scope of the procedure that runs the EXEC.
    assert "demo.load_literal" in stage
    assert "dynamic_sql" not in t.gap_kinds
    assert t.complete
    # Evidence cites the EXEC line; the excerpt is the evaluated SQL.
    assert_step_evidence(t)
    assert all(ev["start_line"] == 6 for ev in step["evidence"])
    issues = diagnostics(tmp_path)
    assert [i["kind"] for i in issues] == ["evaluated_dynamic_sql"]
    assert issues[0]["evaluation"]["alternatives"][0]["sql"].startswith("INSERT INTO #stage")


def test_concatenation_of_literals_and_literal_variables(tmp_path):
    t = trace(dyn("concat"), "demo.output.result_value", tmp_path / "a")
    stage = t.temp_column("#stage", "staged_value")
    step = t.only_step_for(stage)
    assert inputs(step, "value") == [SOURCE_A]
    assert_evaluated_step(step)
    # Every assignment that built the string is cited.
    note = next(n for n in step["notes"] if n.startswith("Evaluated"))
    for line in (5, 10, 11, 12):
        assert f"load.sql:{line}" in note
    assert t.complete

    # CONCAT treats NULL as an empty string.
    t = trace(dyn("concat"), "demo.output_concat.result_value", tmp_path / "b")
    step = t.only_step_for(t.target_id)
    assert inputs(step, "value") == [SOURCE_B]
    assert_evaluated_step(step)
    assert t.complete


def test_if_else_chooses_target_and_source_tables(tmp_path):
    a = trace(dyn("if_else"), "demo.output_a.result_value", tmp_path / "a")
    b = trace(dyn("if_else"), "demo.output_b.result_value", tmp_path / "b")

    from_branch = [s for s in a.steps_for(a.target_id) if "demo.load_branch" in (s["procedure"] or "")]
    assert len(from_branch) == 1
    step = from_branch[0]
    assert inputs(step, "value") == ["?.demo.source_a.input_a"]
    assert "IF @mode = 1" in step["branch_context"]
    assert "dynamic SQL alternative 1 of 2" in step["branch_context"]
    assert_evaluated_step(step)

    step = b.only_step_for(b.target_id)
    assert inputs(step, "value") == ["?.demo.source_b.input_b"]
    assert "ELSE (of IF @mode = 1)" in step["branch_context"]
    # Variables set in the same IF/ELSE are paired: no output_a <- source_b alternative.
    assert "?.demo.source_b.input_b" not in a.all_input_ids()
    assert "?.demo.source_a.input_a" not in b.all_input_ids()
    assert a.complete and b.complete


def test_assignment_and_exec_in_the_same_branch(tmp_path):
    # load_inside_branch overwrites @sql inside IF @mode = 2 and executes it there: the initial value
    # (which names output_b) never reaches the EXEC.
    a = trace(dyn("if_else"), "demo.output_a.result_value", tmp_path / "a")
    inside = [s for s in a.steps_for(a.target_id) if "load_inside_branch" in (s["procedure"] or "")]
    assert len(inside) == 1
    assert inside[0]["branch_context"] == ["IF @mode = 2"]
    b = trace(dyn("if_else"), "demo.output_b.result_value", tmp_path / "b")
    assert not [s for s in b.steps if "load_inside_branch" in (s["procedure"] or "")]
    assert "dynamic_sql" not in b.gap_kinds


def test_quotename_replace_char_and_cast_of_literals(tmp_path):
    t = trace(dyn("quotename"), "demo.output.result_value", tmp_path)
    step = t.only_step_for(t.target_id)
    assert inputs(step, "value") == [SOURCE_A]
    assert_evaluated_step(step)
    assert t.complete
    sql = diagnostics(tmp_path)[0]["evaluation"]["alternatives"][0]["sql"]
    assert sql == "INSERT INTO [demo].[output] (id, result_value)\r\nSELECT id, input_a FROM [demo].[source]"


def test_parameter_default_and_callers_passing_literals(tmp_path):
    t = trace(dyn("parameter"), "demo.output.result_value", tmp_path / "a")
    steps = t.steps_for(t.target_id)
    assert sorted(tuple(inputs(s, "value")) for s in steps) == [(SOURCE_A,), (SOURCE_B,)]
    for step in steps:
        assert_evaluated_step(step)
    by_source = {inputs(s, "value")[0]: s for s in steps}
    assert any("without @source_column (its default N'input_a')" in c for c in by_source[SOURCE_A]["branch_context"])
    assert any("with @source_column = @which" in c for c in by_source[SOURCE_B]["branch_context"])
    assert "dynamic_sql" not in t.gap_kinds
    assert t.complete


def test_parameter_without_caller_keeps_the_gap(tmp_path):
    # Only the default is known; callers outside the scanned files may pass anything.
    t = trace(dyn("parameter"), "demo.output2.result_value", tmp_path)
    gaps = t.gaps("dynamic_sql")
    assert len(gaps) == 1
    detail = gaps[0]["detail"]
    assert detail["evaluation"] == "open"
    assert any("@source_column" in p and "no caller" in p for p in detail["open_pieces"])
    # The known alternative is still analysed, labelled with its condition.
    step = t.only_step_for(t.target_id)
    assert inputs(step, "value") == [SOURCE_A]
    assert any("left to its default" in c for c in step["branch_context"])
    assert not t.complete


def test_value_from_a_query_keeps_the_gap(tmp_path):
    t = trace(dyn("unknown_select"), "demo.output.result_value", tmp_path)
    gaps = t.gaps("dynamic_sql")
    assert len(gaps) == 1
    detail = gaps[0]["detail"]
    assert detail["evaluation"] == "unknown"
    assert any(p.startswith("@table:") and "query" in p and "load.sql:11" in p for p in detail["unknown_pieces"])
    assert not evaluated(t.steps)
    assert SOURCE_A not in t.all_input_ids()
    assert not t.complete
    assert diagnostics(tmp_path)[0]["kind"] == "dynamic_sql"


def test_loop_built_sql_keeps_the_gap(tmp_path):
    t = trace(dyn("loop"), "demo.output.result_value", tmp_path)
    detail = t.gaps("dynamic_sql")[0]["detail"]
    assert any(p.startswith("@sql:") and "loop" in p for p in detail["unknown_pieces"])
    assert not evaluated(t.steps)
    assert not t.complete


def test_more_than_sixteen_alternatives_keeps_the_gap(tmp_path):
    t = trace(dyn("too_many"), "demo.output.result_value", tmp_path)
    detail = t.gaps("dynamic_sql")[0]["detail"]
    assert any("more than 16 alternatives" in p for p in detail["unknown_pieces"])
    assert not evaluated(t.steps)
    assert not t.complete


def test_string_with_several_statements(tmp_path):
    t = trace(dyn("multi_statement"), "demo.output.result_value", tmp_path)
    step = t.only_step_for(t.target_id)
    work = t.temp_column("#work", "step_value")
    assert inputs(step, "value") == [work, "constant:2"]
    inner = t.only_step_for(work)
    assert inner["writer_kind"] == "select_into"
    assert inputs(inner, "value") == [SOURCE_A]
    for s in (step, inner):
        assert_evaluated_step(s, 5)
    assert t.complete
    assert diagnostics(tmp_path)[0]["evaluation"]["alternatives"][0]["statements"] == 2


def test_nested_exec_inside_the_string(tmp_path):
    t = trace(dyn("nested"), "demo.output.result_value", tmp_path)
    steps = t.steps_for(t.target_id)
    assert sorted((s["writer_kind"], tuple(inputs(s, "value"))) for s in steps) == [
        ("insert_select", (SOURCE_A,)), ("update", (SOURCE_B,))]
    for step in steps:
        assert_evaluated_step(step)
    assert t.complete


def test_sp_executesql_parameter_binds_to_its_argument_and_truncation_applies(tmp_path):
    # @delta inside the string is the sp_executesql parameter bound by @delta = @offset.
    t = trace(dyn("batch_variable"), "demo.output.result_value", tmp_path / "a")
    assert not t.gaps("dynamic_sql")
    reads = inputs(t.only_step_for(t.target_id), "value")
    assert any(r.endswith("::@offset") for r in reads) and "?.demo.source.input_a" in reads
    # DECLARE @table varchar(6) = 'source_b' keeps 'source': the string reads demo.source.
    t = trace(dyn("batch_variable"), "demo.output2.result_value", tmp_path / "b")
    step = t.only_step_for(t.target_id)
    assert inputs(step, "value") == [SOURCE_B]
    assert "?.demo.source_b.input_b" not in t.all_ids()


def test_markdown_shows_the_evaluated_note(tmp_path):
    t = trace(dyn("if_else"), "demo.output_b.result_value", tmp_path)
    md = t.markdown
    assert "Evaluated from dynamic SQL" in md
    assert "ELSE (of IF @mode = 1)" in md


def test_compound_assignment_ending_in_a_literal(tmp_path):
    """`SET @sql += N'...'`: the rewrite to `@sql = @sql + (...)` used to drop a final string literal,
    so the statement failed to parse and the dynamic SQL could not be evaluated."""
    t = trace(dyn("compound"), "demo.output.result_value", tmp_path)
    step = t.only_step_for(t.target_id)
    assert inputs(step, "value") == [SOURCE_A]
    assert_evaluated_step(step)
    assert t.complete


def test_exec_without_semicolon_ends_before_the_next_line_statement(tmp_path):
    """EXEC (@sql) <comment> SET @x = ... (fixtures/column/dynamic/exec_then_set, invented from a tester trial):
    the SET was read as an EXEC suffix ("runs elsewhere or as another user") and nothing was evaluated."""
    t = trace(dyn("exec_then_set"), "demo.dbo.out_stamp.stamp", tmp_path / "a", "--default-database", "demo")
    assert t.complete and not t.gaps("dynamic_sql")
    assert "demo.dbo.audit_log.stamp" in t.all_ids()
    t = trace(dyn("exec_then_set"), "demo.dbo.out_count.n", tmp_path / "b", "--default-database", "demo")
    assert "runs elsewhere" not in json.dumps(t.data)
    [gap] = t.gaps("dynamic_sql")                                  # the real reason: the SQL comes from a function
    assert any("dbo.build_index_script" in p for p in gap["detail"]["unknown_pieces"])
    assert any("(SELECT COUNT(*) FROM dbo.target_table)" in s["expression"] for s in t.steps)   # SET: own statement


def test_unknown_date_inside_a_filter_literal_is_read_with_a_placeholder(tmp_path):
    """fixtures/column/dynamic/literal_placeholder (invented from a tester trial): a long concatenation used to
    stop with "call chain is deeper than 8 procedures" (the expression depth was counted); the remaining unknown,
    a date converted to text inside a quoted WHERE literal, can only change which rows are selected."""
    root, db = dyn("literal_placeholder"), ("--default-database", "demo")
    t = trace(root, "demo.dbo.out_latest.d", tmp_path / "a", *db)
    assert "demo.dbo.events_a.d" in t.all_ids() and "deeper than" not in json.dumps(t.data)
    gaps = t.gaps("dynamic_sql")                        # kept, open: the filter date is not known
    assert gaps and all(g["detail"]["evaluation"] == "open" and g["detail"]["row_selection_only"]
                        and g["affects"] == "row_selection" for g in gaps)
    assert t.complete                                   # the value route itself is traced
    assert any("only in a WHERE, ON or HAVING literal" in p for g in gaps for p in g["detail"]["open_pieces"])
    steps = [s for s in t.steps if any(i["node"] == "demo.dbo.events_a.d" for i in s["inputs"])]
    assert steps and all(s["status"] == "inferred" for s in steps)
    for column in ("out_select.d", "out_name.v", "out_table.v"):      # value, quote-able text, code: refused
        t = trace(root, f"demo.dbo.{column}", tmp_path / column, *db)
        assert t.gaps("dynamic_sql") and not t.complete, column
