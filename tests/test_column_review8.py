"""Adversarial review of --job-scripts ordering (fixtures/column/review8/, invented). Each case (except
c00 and c05) used to claim an order that SQL Agent does not guarantee, or crashed."""
import pytest

from column_helpers import FIXTURES, trace

ROOT = FIXTURES / "review8"


def run(case, tmp_path):
    return trace(ROOT / case / "sql", "demo.dbo.Tgt.v", tmp_path, "--default-database", "demo",
                 "--job-scripts", str(ROOT / case / "agent"))


def reasons(t):
    return [g["detail"].get("reason") for g in t.data["gaps"] if g["kind"] == "ambiguous_writer_order"]


def order_note(t, procedure):
    step = next(s for s in t.steps_for(t.target_id) if s["procedure"] == procedure and s["writer_kind"] == "update")
    return next((n for n in step["notes"] if "step order" in n), "")


def test_baseline_is_ordered(tmp_path):
    t = run("c00", tmp_path)
    assert reasons(t) == [] and "after dbo.w_one" in order_note(t, "dbo.w_two")


def test_updated_job_steps_are_applied(tmp_path):
    t = run("c05", tmp_path)   # sp_update_jobstep swaps the two commands
    assert reasons(t) == [] and "after dbo.w_two" in order_note(t, "dbo.w_one")


@pytest.mark.parametrize("case, reason", [
    ("c01", "job_step_loop"),
    ("c03", "writer_only_in_disabled_job"),          # disabled by sp_update_job in another file
    ("c07", "step_subsystem_not_read"),              # a CmdExec step
    ("c09", "step_command_not_followed"),            # sp_executesql in a step
    ("c13", "step_command_not_followed"),            # RETURN between EXECs
    ("c14", "step_command_not_followed"),            # IF in the step command
    ("c15", "call_on_the_way_is_conditional"),       # IF inside the called procedure
    ("s01", "writers_in_different_jobs"),            # sp_start_job only in a comment
    ("s03", "job_start_cycle"),
    ("x06", "writer_only_in_disabled_job"),          # @owner_login_name = SUSER_SNAME() before @enabled = 0
    ("y02", "job_deleted_in_scripts"),
    ("d01", "writer_reached_by_several_steps_or_paths"),   # USE inside the step command
])
def test_unsettled_job_runs_keep_the_gap(case, reason, tmp_path):
    assert reasons(run(case, tmp_path)) == [reason]


def test_non_numeric_arguments_do_not_crash(tmp_path):
    t = run("x01", tmp_path)
    assert reasons(t)   # the job is not followed; the run completes
