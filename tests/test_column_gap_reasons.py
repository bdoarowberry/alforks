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
