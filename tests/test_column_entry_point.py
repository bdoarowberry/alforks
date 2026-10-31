"""--entry-point: the call sequence from the run's entry procedure orders writers in several procedures."""
from column_helpers import FIXTURES, inputs, trace

ROOT = FIXTURES / "entry_point"
DB = ("--default-database", "demo")
TARGET = "demo.demo.result.amount"


def notes(t) -> list[str]:
    return [n for s in t.steps for n in s["notes"] if "call sequence" in n]


def test_without_entry_point_the_order_is_a_gap(tmp_path):
    t = trace(ROOT, TARGET, tmp_path, *DB)
    assert "ambiguous_writer_order" in t.gap_kinds and not t.complete


def test_unconditional_call_sequence_orders_the_writers(tmp_path):
    t = trace(ROOT, TARGET, tmp_path, *DB, "--entry-point", "demo.run_all")
    assert "ambiguous_writer_order" not in t.gap_kinds
    sources = {i for s in t.steps_for(t.target_id) for i in inputs(s, "value")}
    assert {"demo.demo.source_a.amount", "demo.demo.source_b.amount"} <= sources   # both writers stay
    assert any("after demo.load_base" in n and "overwrites the rows it matches" in n for n in notes(t))
    assert t.complete


def test_conditional_call_keeps_the_gap(tmp_path):
    t = trace(ROOT, TARGET, tmp_path, *DB, "--entry-point", "demo.run_conditional")
    [gap] = t.gaps("ambiguous_writer_order")
    assert "conditional" in gap["message"]


def test_writer_not_called_from_the_entry_point_keeps_the_gap(tmp_path):
    t = trace(ROOT, TARGET, tmp_path, *DB, "--entry-point", "demo.apply_override")
    [gap] = t.gaps("ambiguous_writer_order")
    assert "demo.load_base is not called from the entry point" in gap["message"]


def test_writers_in_the_two_arms_of_one_if_never_both_run(tmp_path):
    t = trace(FIXTURES / "entry_point_either", TARGET, tmp_path, *DB, "--entry-point", "demo.run_either")
    assert "ambiguous_writer_order" not in t.gap_kinds
    t = trace(FIXTURES / "entry_point_either", TARGET, tmp_path / "plain", *DB)
    assert "ambiguous_writer_order" in t.gap_kinds


def test_unknown_entry_point_is_a_usage_error(tmp_path):
    from column_helpers import run_cli
    assert run_cli(["--source-root", str(ROOT), "--column", TARGET, "--output", str(tmp_path), "--no-progress",
                    *DB, "--entry-point", "demo.nope"]) == 2
