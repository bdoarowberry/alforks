"""Scalar user functions whose body is scanned and reads no tables are summarised, not opaque."""
from column_helpers import FIXTURES, inputs, trace

ROOT = FIXTURES / "scalar_udf_pure"
DB = ("--default-database", "demo")


def test_pure_function_depends_on_the_arguments_its_body_uses(tmp_path):
    t = trace(ROOT, "demo.demo.output.date_key", tmp_path, *DB)
    step = t.only_step_for(t.target_id)
    assert inputs(step, "value") == ["demo.demo.source.event_date"]       # @unused's argument is not an input
    assert step["status"] == "inferred"
    assert any("summarised from its definition" in n and "functions.sql:1" in n for n in step["notes"])
    assert "opaque_function" not in t.gap_kinds
    assert t.complete


def test_function_calling_a_pure_function_is_pure(tmp_path):
    t = trace(ROOT, "demo.demo.output.wrapped", tmp_path, *DB)
    assert inputs(t.only_step_for(t.target_id), "value") == ["demo.demo.source.event_date"]
    assert "opaque_function" not in t.gap_kinds


def test_function_reading_a_table_stays_opaque(tmp_path):
    t = trace(ROOT, "demo.demo.output.label", tmp_path, *DB)
    [gap] = t.gaps("opaque_function")
    assert "FROM" in gap["message"]
    assert not t.complete
