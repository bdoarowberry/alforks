"""SQL Agent job scripts (--job-scripts; fixtures/column/jobs/, invented, SSMS "Script Job as" style).

Job "Nightly load" runs load_target, then fix_a, then run_all (which runs fix_b, then fix_a again).
"Afternoon fix" runs fix_c; the disabled "Old fix" runs fix_d.
"""
import json

from column_helpers import FIXTURES, cli_args, run_cli, trace

ROOT = FIXTURES / "jobs"
JOBS = ("--default-database", "demo", "--job-scripts", str(ROOT / "agent"))


def writer_gaps(t):
    return [g for g in t.data["gaps"] if g["kind"] == "ambiguous_writer_order"]


def test_parser_reads_jobs_steps_and_order():
    from column_lineage.jobs import parse_job_scripts
    jobs, counts = parse_job_scripts([str(ROOT / "agent")])
    assert counts["jobs"] == 7 and counts["steps"] == 11
    nightly = next(j for j in jobs if j.name == "Nightly load")
    assert [s.name for s in nightly.sequence()] == ["Load", "Fix a", "Run all"]
    assert nightly.steps[1].procedures() == ["dbo.fix_a"] and nightly.schedules
    assert not next(j for j in jobs if j.name == "Old fix").enabled


def test_without_job_scripts_the_order_is_a_gap_with_a_reason(tmp_path):
    t = trace(ROOT / "sql", "demo.dbo.Target.col_a", tmp_path, "--default-database", "demo")
    assert [g["detail"]["reason"] for g in writer_gaps(t)] == ["no_job_scripts_or_entry_point"]


def test_job_step_order_settles_writers_in_one_job(tmp_path):
    # load_target (step 1), then fix_b inside run_all (step 3): one order.
    t = trace(ROOT / "sql", "demo.dbo.Target.col_b", tmp_path, *JOBS)
    assert writer_gaps(t) == []
    update = next(s for s in t.steps_for(t.target_id) if s["writer_kind"] == "update")
    assert update["status"] == "inferred"
    assert any(e["role"] == "job_step" for e in update["evidence"])
    assert any("SQL Agent job Nightly load" in n and "not confirmed" in n for n in update["notes"])


def test_writer_reached_twice_in_one_job_stays_a_gap(tmp_path):
    # fix_a runs in step 2 and again inside run_all (step 3).
    t = trace(ROOT / "sql", "demo.dbo.Target.col_a", tmp_path, *JOBS)
    assert [g["detail"]["reason"] for g in writer_gaps(t)] == ["writer_reached_by_several_steps_or_paths"]


def test_writers_in_different_jobs_stay_a_gap(tmp_path):
    t = trace(ROOT / "sql", "demo.dbo.Target.col_c", tmp_path, *JOBS)
    assert [g["detail"]["reason"] for g in writer_gaps(t)] == ["writers_in_different_jobs"]


def test_writer_only_in_a_disabled_job_is_not_ordered(tmp_path):
    t = trace(ROOT / "sql", "demo.dbo.Target.col_d", tmp_path, *JOBS)
    assert [g["detail"]["reason"] for g in writer_gaps(t)] == ["writer_only_in_disabled_job"]


def test_gap_summary_counts_writer_order_reasons(tmp_path):
    columns = ["demo.dbo.Target.col_a", "demo.dbo.Target.col_b"]
    run_cli(cli_args(ROOT / "sql", columns, tmp_path, *JOBS, "--gap-summary"))
    summary = json.loads((tmp_path / "gap-summary.json").read_text(encoding="utf-8"))
    assert summary["totals"]["value_gap_traces_by_reason"]["ambiguous_writer_order"] == {
        "writer_reached_by_several_steps_or_paths": 1}
    scan = json.loads((tmp_path / "demo.dbo.target.col_a.lineage.json").read_text(encoding="utf-8"))["scan"]
    assert scan["job_scripts"]["jobs"] == 7 and scan["job_scripts"]["tsql_steps"] == 11



def test_job_started_by_the_last_step_runs_after_the_earlier_steps(tmp_path):
    t = trace(ROOT / "sql", "demo.dbo.Target2.col_a", tmp_path, *JOBS)
    assert writer_gaps(t) == []
    fix = next(s for s in t.steps_for(t.target_id) if s["writer_kind"] == "update")
    assert any("after dbo.chain_load" in n for n in fix["notes"])


def test_job_started_before_later_steps_overlaps_them(tmp_path):
    t = trace(ROOT / "sql", "demo.dbo.Target3.col_a", tmp_path, *JOBS)
    assert [g["detail"]["reason"] for g in writer_gaps(t)] == ["writers_overlap_in_chained_jobs"]


def test_run_context_says_which_job_runs_each_writer(tmp_path):
    t = trace(ROOT / "sql", "demo.dbo.Target.col_d", tmp_path, *JOBS)
    contexts = {s["procedure"]: s["run_context"] for s in t.steps_for(t.target_id)}
    assert contexts["dbo.load_target"]["status"] == "run_by_enabled_job"
    assert contexts["dbo.load_target"]["jobs"][0]["schedule"] == "daily from 02:00"
    assert contexts["dbo.fix_d"]["status"] == "only_disabled_jobs"
    t = trace(ROOT / "sql", "demo.dbo.Target4.col_a", tmp_path / "manual", *JOBS)
    assert t.only_step_for(t.target_id)["run_context"] == {"status": "not_in_job_scripts"}
    page = (tmp_path / "manual" / "demo.dbo.target4.col_a.overview.md").read_text(encoding="utf-8")
    assert "Run by (job scripts, static copies):" in page and "1 by no job in the scripts" in page


def test_package_step_and_sql_fix_in_one_job_are_ordered(tmp_path):
    """A job runs an SSIS package that loads a table, then a procedure that updates it."""
    ssis = FIXTURES / "ssis"
    sql = tmp_path / "sql"
    sql.mkdir()
    (sql / "tables.sql").write_text((ssis / "sql" / "tables.sql").read_text(encoding="utf-8"), encoding="utf-8")
    (sql / "fix.sql").write_text("USE demo_db;\nGO\nCREATE PROCEDURE demo.fix_stage AS\n"
                                 "    UPDATE demo.stage SET amount = LTRIM(amount) WHERE amount IS NOT NULL;\nGO\n",
                                 encoding="utf-8")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "load.sql").write_text(
        "DECLARE @jobId BINARY(16)\n"
        "EXEC msdb.dbo.sp_add_job @job_name=N'Stage load', @enabled=1, @job_id = @jobId OUTPUT\n"
        "EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'Package', @step_id=1, @on_success_action=3,\n"
        "    @subsystem=N'SSIS', @command=N'/FILE \"\\\\share\\packages\\load_stage.dtsx\" /REPORTING E'\n"
        "EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'Fix', @step_id=2, @on_success_action=1,\n"
        "    @subsystem=N'TSQL', @command=N'EXEC demo.fix_stage', @database_name=N'demo_db'\n", encoding="utf-8")
    base = ("--package-root", str(ssis / "basic"))
    t = trace(sql, "demo_db.demo.stage.amount", tmp_path / "plain", *base)
    assert [g["detail"]["reason"] for g in writer_gaps(t)] == ["package_and_sql_writers"]
    t = trace(sql, "demo_db.demo.stage.amount", tmp_path / "jobs_out", *base, "--job-scripts", str(jobs))
    assert writer_gaps(t) == []
    package = next(s for s in t.steps_for(t.target_id) if s["writer_kind"] == "package_destination")
    assert package["run_context"]["status"] == "run_by_enabled_job"
    fix = next(s for s in t.steps_for(t.target_id) if s["writer_kind"] == "update")
    assert any("after package load_stage" in n for n in fix["notes"]) and fix["status"] == "inferred"
