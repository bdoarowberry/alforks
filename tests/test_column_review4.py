"""Regressions from adversarial review 4 (repair layer, assertions, gap classes, compare).

Each case once produced a false or hidden proof. Fixtures: fixtures/column/review4/<case>/.
"""
import json

from column_helpers import FIXTURES, cli_args, inputs, run_cli, trace

ROOT = FIXTURES / "review4"
DB = ("--default-database", "demo")


def run(case, column, tmp_path, *extra):
    return trace(ROOT / case, column, tmp_path, *DB, *extra)


def reach(t) -> set[str]:
    seen, frontier = set(), [t.target_id]
    while frontier:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        for step in t.steps_for(node):
            frontier.extend(inputs(step, "value"))
    return seen


def test_f13_line_start_hint_does_not_split_one_statement(tmp_path):
    t = run("split_hint", "demo.dbo.target.c", tmp_path)
    assert "parse_error" in t.gap_kinds and not t.complete


def test_f1_output_into_native_targets_are_reported(tmp_path):
    t = run("output_into", "demo.dbo.amount_history.old_amount", tmp_path)
    assert "unsupported_statement" in t.gap_kinds and not t.complete
    t = run("output_temp", "demo.dbo.change_log.old_amount", tmp_path / "temp")
    assert "unsupported_statement" in t.gap_kinds and not t.complete


def test_f7_unparsed_writes_through_synonym_and_view_reach_the_base_table(tmp_path):
    t = run("syn_unparsed", "demo.dbo.target.c", tmp_path)
    assert len(t.gaps("parse_error")) == 2 and not t.complete


def test_f6_alias_reused_in_a_subquery_still_names_the_target(tmp_path):
    t = run("alias_shadow", "demo.dbo.target.c", tmp_path)
    assert "parse_error" in t.gap_kinds and not t.complete


def test_f16_write_through_view_with_two_definitions_is_a_gap(tmp_path):
    t = run("view_twice", "demo.dbo.target.c", tmp_path)
    assert "ambiguous_object" in t.gap_kinds and not t.complete


def test_f4_partition_switch_is_a_writer(tmp_path):
    t = run("switch", "demo.dbo.target.c", tmp_path)
    assert "unsupported_statement" in t.gap_kinds and not t.complete


def test_f12_placeholder_name_does_not_capture_real_variables(tmp_path):
    found = reach(run("digit_collision", "demo.dbo.target.c", tmp_path))
    assert "demo.dbo.feed.v" in found and "demo.dbo.feed.w" not in found


def test_f15_alter_table_default_constraint_is_the_omitted_value(tmp_path):
    assert "constant:42" in reach(run("alter_default", "demo.dbo.target.c", tmp_path))


def test_f8_write_method_sets_the_column(tmp_path):
    t = run("write_method", "demo.dbo.doc.body", tmp_path)
    assert "demo.dbo.feed.chunk" in reach(t)


def test_f5_bogus_literal_targets_do_not_hide_runtime_dynamic_sql(tmp_path):
    for case in ("dyn_partial_target", "dyn_unrelated_literal"):
        t = run(case, "demo.dbo.target.c", tmp_path / case)
        assert "dynamic_sql" in t.gap_kinds and not t.complete, case


def test_f14_generic_loader_called_with_literal_names_is_followed(tmp_path):
    t = run("dyn_offpath", "demo.dbo.target.c", tmp_path)
    evaluated = [s for s in t.steps_for(t.target_id) if any("dynamic SQL" in n for n in s["notes"])]
    assert evaluated or "dynamic_sql" in t.gap_kinds
    assert all(s["status"] != "verified" for s in evaluated)


def test_f3_execute_as_header_does_not_hide_the_first_statement(tmp_path):
    assert "demo.dbo.feed.v" in reach(run("exec_as", "demo.dbo.target.c", tmp_path))


def test_f19_function_in_where_is_a_row_selection_gap(tmp_path):
    t = run("udf_where", "demo.dbo.target.c", tmp_path)
    assert all(g["affects"] == "row_selection" for g in t.gaps("opaque_function"))
    assert t.complete


def test_f18_compound_assignment_keeps_precedence(tmp_path):
    t = run("compound_prec", "demo.dbo.target.c", tmp_path)
    assert any("(f.v - f.w)" in s["expression"] for s in t.steps_for(t.target_id))


def test_f11_top_limited_writes_are_noted(tmp_path):
    t = run("update_top", "demo.dbo.target.c", tmp_path)
    assert any("TOP (n)" in n for s in t.steps for n in s["notes"])


def test_f9_assertion_path_must_match_exactly(tmp_path):
    t = run("assert_path", "demo.dbo.target.c", tmp_path, "--assertions", str(ROOT / "assert_path_assertions.json"))
    diagnostics = json.loads((tmp_path / "scan-diagnostics.json").read_text(encoding="utf-8"))
    assert [e["location"].split(":")[0] for e in diagnostics["assertion_effects"]["A1"]] == ["sales/load.sql"]
    assert not t.complete      # the other loader's dynamic SQL stays open


def test_f17_malformed_assertion_writer_is_a_usage_error(tmp_path):
    for bad in ({"writer": "load.sql"}, {"writer": {"line": 5}}, {"writer": {"path": "x.sql"}, "resolves": "dynamic_sql"}):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"assertions": [{"id": "A1", "target": "demo.dbo.target.c",
                                                    "alternatives": [{"inputs": ["demo.dbo.feed_a.v"]}], **bad}]}),
                        encoding="utf-8")
        assert run_cli(cli_args(ROOT / "assert_path", "demo.dbo.target.c", tmp_path / "o", *DB,
                                "--assertions", str(path))) == 2, bad


def test_f10_ambiguous_candidate_is_not_reported_as_found(tmp_path):
    from column_lineage.compare import main
    run("cmp_ambig", "demo.dbo.target.c", tmp_path)
    assert main(["--expected", str(ROOT / "cmp_ambig_expected.json"), "--traces", str(tmp_path)]) == 0
    summary = json.loads((tmp_path / "comparison-summary.json").read_text(encoding="utf-8"))
    assert summary["per_column"][0]["sources"] == {"found_as_ambiguous_candidate": 1}
