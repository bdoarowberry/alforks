"""Anonymous reason categories in gap-summary.json for the gap kinds that need outside evidence."""
import json

from column_helpers import FIXTURES, cli_args, run_cli

DB = ("--default-database", "demo")


def summary(tmp_path, root, column, *extra):
    assert run_cli(cli_args(root, column, tmp_path, *DB, "--gap-summary", *extra)) in (0, 3)
    return json.loads((tmp_path / "gap-summary.json").read_text(encoding="utf-8"))["totals"]["value_gap_traces_by_reason"]


def test_dynamic_sql_reasons_say_where_and_why(tmp_path):
    reasons = summary(tmp_path / "a", FIXTURES / "dynamic_sql_sp_executesql", "demo.output.result_value")
    [(reason, count)] = reasons["dynamic_sql"].items()
    site, why = reason.split("/")
    assert site == "dynamic_sql_names_this_object_as_target" and why == "parameter_without_caller" and count == 1
    reasons = summary(tmp_path / "b", FIXTURES / "review4" / "dyn_partial_target", "demo.dbo.target.k")
    assert list(reasons["dynamic_sql"]) == ["procedure_on_path_builds_target_at_runtime/parameter_without_caller"]
    assert "output" not in json.dumps(reasons) and "target" not in json.dumps(reasons).replace("_target", "")


def test_linked_server_reason_says_whether_the_database_is_scanned(tmp_path):
    main, remote = FIXTURES / "review" / "linked_server" / "main", FIXTURES / "review" / "linked_server" / "remote"
    reasons = summary(tmp_path / "a", main, "demo.output.result_value")
    assert reasons["external_object"] == {"linked_server_to_a_database_not_in_scan": 1}
    reasons = summary(tmp_path / "b", main, "demo.output.result_value", "--source-root", str(remote),
                      "--database-map", f"{remote}=remote_db")
    assert reasons["external_object"] == {"linked_server_to_a_scanned_database_name": 1}
    page = (tmp_path / "b" / "demo.output.result_value.lineage.md").read_text(encoding="utf-8")
    assert "--local-server" in page


def test_assertion_use_is_summarised_as_counts(tmp_path):
    root = FIXTURES / "review" / "assert_dynamic"
    assert run_cli(cli_args(root, "demo.output.result_value", tmp_path, *DB, "--gap-summary",
                            "--assertions", str(root / "assertions.json"))) in (0, 3)
    totals = json.loads((tmp_path / "gap-summary.json").read_text(encoding="utf-8"))["totals"]
    block = totals["assertions"]
    assert block["used"] >= 1 and sum(block["by_review"].values()) == block["used"]
    assert block["traces_per_assertion"][0] == 1
    assert block["traces_relying_on_unreviewed"] == (1 if block["by_review"].get("reviewed", 0) < block["used"] else 0)
    assert "demo" not in json.dumps(block).lower()


def test_row_selection_only_gaps_have_their_own_reason_table(tmp_path):
    """Dynamic SQL known except a filter value affects which rows are selected: it is counted under
    row_selection_gap_traces_by_reason, not value_gap_traces_by_reason."""
    root = FIXTURES / "dynamic" / "literal_placeholder"
    assert run_cli(cli_args(root, "demo.dbo.out_c.v", tmp_path, *DB, "--gap-summary")) in (0, 3)
    totals = json.loads((tmp_path / "gap-summary.json").read_text(encoding="utf-8"))["totals"]
    assert totals["row_selection_gap_traces_by_reason"]["dynamic_sql"] == {
        "object_name_passed_to_a_dynamic_procedure/known_except_a_filter_value": 1}
    assert "dynamic_sql" not in totals["value_gap_traces_by_reason"]
