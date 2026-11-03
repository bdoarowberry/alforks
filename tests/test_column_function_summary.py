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


def test_function_returning_one_query_is_read_from_its_body(tmp_path):
    """RETURN (SELECT ...) is inlined (fixtures/column/scalar_udf_inline/ covers the limits)."""
    t = trace(ROOT, "demo.demo.output.label", tmp_path, *DB)
    step = t.only_step_for(t.target_id)
    assert inputs(step, "value") == ["demo.demo.labels.label"] and step["status"] == "inferred"
    assert "opaque_function" not in t.gap_kinds
