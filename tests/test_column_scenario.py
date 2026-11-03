"""Scenario-specific lineage (--scenario, fixtures/column/scenario/, invented): assumed parameter values
exclude the branches they make unreachable in that procedure; uncertain branches are kept; the pages state
the values as assumptions and list what was excluded."""
import pytest

from column_helpers import FIXTURES, cli_args, inputs, run_cli, trace

ROOT = FIXTURES / "scenario" / "sql"
DB = ("--default-database", "demo")
TARGET = "demo.dbo.saved_result.value_col"


def run(tmp_path, column=TARGET, *scenario):
    extra = [x for s in scenario for x in ("--scenario", s)]
    return trace(ROOT, column, tmp_path, *DB, *extra)


def reads(t, column=None):
    return sorted(i for s in t.steps_for(column or t.target_id) for i in inputs(s, "value"))


def pages(tmp_path, column=TARGET):
    stem = column.lower()
    return ((tmp_path / f"{stem}.overview.md").read_text(encoding="utf-8"),
            (tmp_path / f"{stem}.lineage.md").read_text(encoding="utf-8"))


def test_unconstrained_run_keeps_both_build_branches(tmp_path):
    t = run(tmp_path)
    assert reads(t) == ["demo.dbo.interim_stage.value_col", "demo.dbo.regular_stage.value_col"]
    overview, detailed = pages(tmp_path)
    assert "Scenario" not in overview and "scenario" not in t.data["scan"]


def test_regular_mode_with_rebuild_builds_from_the_regular_stage(tmp_path):
    t = run(tmp_path, TARGET, "dbo.publish_data @mode=0 @rebuild=1")
    assert reads(t) == ["demo.dbo.regular_stage.value_col"]
    # The staging table keeps its own, separately scheduled writer; no call is invented.
    assert [s["procedure"] for s in t.steps_for("demo.dbo.regular_stage.value_col")] == ["dbo.load_regular_stage"]
    [proc] = t.data["scan"]["scenario"]["procedures"]
    assert proc["values"] == {"@mode": "0", "@rebuild": "1"}
    [excluded] = proc["excluded_statements"]
    assert "interim_stage" in excluded["statement"] and "ELSE arm of 'IF @mode = 0'" in excluded["reason"]
    overview, detailed = pages(tmp_path)
    scenario = overview[overview.index("## Scenario (assumed values)"):overview.index("## Plain answer")]
    assert "assumed values, not evidence of what ran in production" in scenario
    assert "`demo.dbo.publish_data` with `@mode = 0`, `@rebuild = 1`" in scenario
    assert "interim_stage" in scenario and "unconstrained run" in scenario
    assert "**Writers on this trace run by this invocation:** `dbo.publish_data` (the scenario's entry procedure)" in scenario
    assert ("**Writers on this trace not run by this invocation:** `dbo.load_regular_stage`. They may have written the "
            "rows earlier or separately; whether, when and in what order they ran needs job or run evidence") in scenario
    assert "**Scenario-specific result:**" in overview[overview.index("## Coverage"):]
    assert '<a id="scenario"></a>' in detailed and "### Scenario (assumed values)" in detailed


def test_interim_mode_with_rebuild_builds_from_the_interim_stage(tmp_path):
    t = run(tmp_path, TARGET, "dbo.publish_data @mode=1 @rebuild=1")
    assert reads(t) == ["demo.dbo.interim_stage.value_col"]


def test_reuse_reads_the_existing_result_and_does_not_claim_its_origin(tmp_path):
    t = run(tmp_path, TARGET, "dbo.publish_data @rebuild=0")
    assert not t.steps
    [stop] = t.data["stops"]
    assert stop["reason"] == "scenario_not_written"
    assert "reads the existing rows instead: sql/publish.sql:14 (SELECT value_col FROM dbo.saved_result;)" in stop["detail"]
    assert "How those rows were populated is not determined by this scenario" in stop["detail"]
    assert t.data["completeness"]["ends_by_meaning"] == {"existing_content_not_written_in_scenario": 1}
    overview, _ = pages(tmp_path)
    assert "**Not written in this scenario.**" in overview
    excluded = t.data["scan"]["scenario"]["procedures"][0]["excluded_statements"]
    assert len(excluded) == 2 and all("after the RETURN at line 15" in x["reason"] for x in excluded)


def test_a_parameter_set_under_a_settled_condition_is_followed(tmp_path):
    t = run(tmp_path, "demo.dbo.out_changed.v", "dbo.p_changed @mode=1")
    assert reads(t) == ["demo.dbo.src_a.v"]          # @mode = 1 is reset to 0 before the second IF


def test_a_parameter_assigned_from_a_query_keeps_both_branches(tmp_path):
    t = run(tmp_path, "demo.dbo.out_selected.v", "dbo.p_selected @mode=0")
    assert reads(t) == ["demo.dbo.src_a.v", "demo.dbo.src_b.v"]
    proc = t.data["scan"]["scenario"]["procedures"][0]
    assert "@mode" in proc["parameters_changed_in_procedure"] and not proc["excluded_statements"]


def test_a_condition_reading_a_table_is_kept_unless_the_values_settle_it(tmp_path):
    t = run(tmp_path / "a", "demo.dbo.out_exists.v", "dbo.p_exists @mode=1")
    assert reads(t) == ["demo.dbo.src_a.v", "demo.dbo.src_b.v"]
    assert t.data["scan"]["scenario"]["procedures"][0]["uncertain_conditions"]
    t = run(tmp_path / "b", "demo.dbo.out_exists.v", "dbo.p_exists @mode=0")   # 0 = 1 AND ... is false
    assert reads(t) == ["demo.dbo.src_b.v"]


def test_values_are_not_carried_into_called_procedures(tmp_path):
    t = run(tmp_path, "demo.dbo.out_child.v", "dbo.p_parent @mode=0")
    assert reads(t) == ["demo.dbo.src_a.v", "demo.dbo.src_b.v"]
    notes = t.data["scan"]["scenario"]["procedures"][0]["notes"]
    assert any("not carried into called procedures" in n and "dbo.p_child" in n for n in notes)


def test_an_output_argument_makes_the_parameter_unknown(tmp_path):
    t = run(tmp_path, "demo.dbo.out_output.v", "dbo.p_output @mode=0")
    assert reads(t) == ["demo.dbo.src_a.v", "demo.dbo.src_b.v"]


def test_goto_turns_exclusion_off(tmp_path):
    t = run(tmp_path, "demo.dbo.out_goto.v", "dbo.p_goto @mode=1")
    assert reads(t) == ["demo.dbo.src_a.v", "demo.dbo.src_b.v"]


def test_a_call_in_an_excluded_branch_no_longer_binds_the_callee_parameter(tmp_path):
    t = run(tmp_path / "s", "demo.dbo.out_param.v", "dbo.p_caller @mode=0")
    ends = {s["node"] for s in t.data["stops"]}
    assert "constant:5" in ends and "constant:7" not in ends
    t = run(tmp_path / "u", "demo.dbo.out_param.v")
    assert {"constant:5", "constant:7"} <= {s["node"] for s in t.data["stops"]}


@pytest.mark.parametrize("scenario, message", [
    ("dbo.no_such_proc @mode=0", "is not defined"),
    ("dbo.publish_data @nope=1", "has no parameter @nope"),
    ("dbo.publish_data mode=1", "is not @param=value"),
])
def test_bad_scenarios_are_refused(tmp_path, capsys, scenario, message):
    assert run_cli(cli_args(ROOT, TARGET, tmp_path, *DB, "--scenario", scenario)) == 2
    assert message in capsys.readouterr().err


# Adversarial review cases (fixtures/column/scenario/sql/review.sql): each excluded a statement that runs.
@pytest.mark.parametrize("column, scenario, expected", [
    ("r_print", "dbo.r_p_print @dbg=0", ["demo.dbo.r_src_a.v"]),               # PRINT ... END on one line
    ("r_else", "dbo.r_p_else @dbg=0", ["demo.dbo.r_src_a.v"]),                 # PRINT ... ELSE on one line
    ("r_case", "dbo.r_p_case @p=0", ["demo.dbo.r_src_a.v"]),                   # CASE ... END over two lines
    ("r_loop", "dbo.r_p_loop @p=0", ["demo.dbo.r_src_a.v"]),                   # WHILE after RETURN; @p set in the loop
    ("r_dialog", "dbo.r_p_dialog @p=0", ["demo.dbo.r_src_a.v"]),               # BEGIN DIALOG is no block
    ("r_trunc", "dbo.r_p_trunc @code=ABCD", ["demo.dbo.r_src_b.v"]),           # varchar(3): 'ABC' <> 'ABCD'
    ("r_int", "dbo.r_p_int @n=1.7", ["demo.dbo.r_src_a.v"]),                   # int: 1.7 is 1
    ("r_setbit", "dbo.r_p_setbit @b=0", ["demo.dbo.r_src_a.v"]),               # SET @b = 5 makes it 1
    ("r_accent", "dbo.r_p_accent @w=é", ["demo.dbo.r_src_a.v", "demo.dbo.r_src_b.v"]),   # collation unknown
    ("r_null", "dbo.r_p_null @p=NULL", ["demo.dbo.r_src_a.v", "demo.dbo.r_src_b.v"]),   # ANSI_NULLS not seen
    ("r_word", "dbo.r_p_word @w=inf", ["demo.dbo.r_src_a.v"]),                 # a word, not a number
    ("r_dyn", "dbo.r_p_dyn @p=0", ["demo.dbo.r_src_b.v"]),                     # evaluated dynamic SQL filtered too
])
def test_review_cases_keep_what_runs(tmp_path, column, scenario, expected):
    t = run(tmp_path, f"demo.dbo.{column}.v", scenario)
    assert reads(t) == expected


def test_a_column_whose_only_writer_is_excluded_is_not_written(tmp_path):
    t = run(tmp_path, "demo.dbo.r_cols.v", "dbo.r_p_cols @p=0")       # another statement still writes column w
    assert [s["reason"] for s in t.data["stops"]] == ["scenario_not_written"]


@pytest.mark.parametrize("scenarios, message", [
    (["dbo.r_p_int @n=nan"], "cannot convert"),
    (["dbo.r_p_int @n=0", "dbo.r_p_int @n=1"], "is given twice"),
])
def test_review_inputs_are_refused(tmp_path, capsys, scenarios, message):
    extra = [x for s in scenarios for x in ("--scenario", s)]
    assert run_cli(cli_args(ROOT, "demo.dbo.r_int.v", tmp_path, *DB, *extra)) == 2
    assert message in capsys.readouterr().err


def test_indented_calls_with_trailing_comments_line_up(tmp_path):
    """Tester trial shape (try_calls.sql): the index starts each call after the first at its line start, the
    walk at its first word; the scenario must still apply."""
    t = run(tmp_path, "demo.dbo.t_result.v", "dbo.t_publish @mode=0 @rebuild=1")
    assert reads(t) == ["demo.dbo.t_regular.v"]
    [proc] = t.data["scan"]["scenario"]["procedures"]
    assert proc["applied"] and not any("could not be lined up" in n for n in proc["notes"])
    assert not proc["parameters_changed_in_procedure"]          # passing @mode to a call does not change it
    assert len(proc["excluded_statements"]) == 1 and "t_interim" in proc["excluded_statements"][0]["statement"]


def test_a_scenario_that_cannot_be_applied_says_so(tmp_path):
    t = run(tmp_path, "demo.dbo.t_goto_out.v", "dbo.t_goto @mode=1")
    [proc] = t.data["scan"]["scenario"]["procedures"]
    assert proc["applied"] is False and reads(t) == ["demo.dbo.t_regular.v"]
    overview, detailed = pages(tmp_path, "demo.dbo.t_goto_out.v")
    for page in (overview, detailed):
        assert "**Scenario requested but not applied — this is the unconstrained all-path result.**" in page
        assert "Only what these values allow is shown" not in page
    assert "- **Scenario not applied:** the counts below are the unconstrained all-path result" in overview
    assert "**Scenario-specific result:**" not in overview


def test_bracketed_keywords_and_indexed_while_line_up(tmp_path):
    t = run(tmp_path, "demo.dbo.t_shapes.v", "dbo.t_align @p=1")
    [proc] = t.data["scan"]["scenario"]["procedures"]
    assert proc["applied"], proc["notes"]
    assert "demo.dbo.t_interim.v" not in reads(t) and "demo.dbo.t_regular.v" in reads(t)


def test_a_merged_return_and_insert_still_lines_up(tmp_path):
    """IF @p = 1 RETURN INSERT ... on one line: the index merges the two (and cannot parse them; a parse_error
    gap without a scenario too). The scenario still lines up and excludes nothing it cannot place."""
    t = run(tmp_path, "demo.dbo.t_merged.v", "dbo.t_merge @p=0")
    [proc] = t.data["scan"]["scenario"]["procedures"]
    assert proc["applied"] and not proc["excluded_statements"]
    assert "parse_error" in t.gap_kinds


# Tester trial shape (invocation.sql): the entry procedure only calls loaders, inside TRY/CATCH.
INV = "demo.dbo.i_report.value_col"


def scenario_block(t):
    [proc] = t.data["scan"]["scenario"]["procedures"]
    return proc


def test_try_body_runs_in_order_so_an_early_test_is_decided(tmp_path):
    t = run(tmp_path, INV, "dbo.i_publish @mode=0 @refresh=1")
    proc = scenario_block(t)
    assert not proc["uncertain_conditions"] and not proc["parameters_changed_in_procedure"]
    assert [c["procedure"] for c in proc["calls"]] == ["demo.dbo.i_load_regular"]
    assert proc["called_only_from_excluded_branches"] == ["demo.dbo.i_load_interim"]
    overview, _ = pages(tmp_path, INV)
    assert "`dbo.i_load_regular` (called at line 39)" in overview
    assert "`dbo.i_load_interim` (called only from an excluded branch)" in overview
    assert "**Scenario applied, nothing excluded on this trace:**" in overview


def test_a_later_reassignment_keeps_only_the_later_test_uncertain(tmp_path):
    proc = scenario_block(run(tmp_path, INV, "dbo.i_publish @mode=1 @refresh=1"))
    assert [u.split("'")[1] for u in proc["uncertain_conditions"]] == ["IF @as_of = @period_end", "IF @mode = 1"]
    assert "line 36" in proc["uncertain_conditions"][1]


def test_reuse_excludes_every_later_call_and_says_the_rows_origin_is_open(tmp_path):
    t = run(tmp_path, INV, "dbo.i_publish @mode=0 @refresh=0")      # also excludes an assignment (@msg)
    proc = scenario_block(t)
    assert proc["calls"] == [] and len(proc["excluded_statements"]) == 5
    assert any(x["what"] == "assigns @msg" for x in proc["excluded_statements"])
    overview, _ = pages(tmp_path, INV)
    assert ("**This invocation writes no object on this trace** under these values. It reads the existing rows of "
            "`demo.dbo.i_saved` (line 32); where those rows came from is not determined for this invocation") in overview
    assert "may have written the rows earlier or separately" in overview


def test_reuse_lists_only_what_the_walk_reached(tmp_path):
    """Tester trial shape (reuse.sql): calls and reads after a certain RETURN are not this invocation's."""
    t = run(tmp_path, "demo.dbo.u_report.value_col", "dbo.u_publish @reuse=1")
    proc = scenario_block(t)
    assert proc["calls"] == [] and [i["procedure"] for i in proc["invocation"]] == ["demo.dbo.u_publish"]
    assert [(r["display"], r["line"]) for r in proc["reads"]] == [("demo.dbo.u_saved", 16)]
    assert proc["statements"] == {"reached": 2, "excluded": 3, "neither_lines": []}
    assert not any("called procedures" in n for n in proc["notes"])
    overview, _ = pages(tmp_path, "demo.dbo.u_report.value_col")
    assert "`dbo.u_build_result` (called only from an excluded branch)" in overview
    assert "u_stage` (line" not in overview


def test_several_definitions_are_named_and_each_is_walked(tmp_path):
    root = FIXTURES / "scenario_copies"
    t = trace(root, "demo.dbo.c_saved.value_col", tmp_path, *DB, "--scenario", "dbo.c_publish @reuse=1")
    procs = t.data["scan"]["scenario"]["procedures"]
    assert len(procs) == 2
    calls = {p["definition"].split(":")[0]: [c["procedure"] for c in p["calls"]] for p in procs}
    assert calls == {"scenario_copies/a/publish.sql": [], "scenario_copies/b/publish_old.sql": ["demo.dbo.c_build"]}
    overview = (tmp_path / "demo.dbo.c_saved.value_col.overview.md").read_text(encoding="utf-8")
    assert "**`demo.dbo.c_publish` has 2 definitions**" in overview
