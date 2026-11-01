"""Adversarial review of the existence-proof rules (fixtures/column/review7/, invented). Each case was a
false complete or a wrong binding before the fix."""
import time

import pytest

from column_helpers import FIXTURES, trace

ROOT = FIXTURES / "review7"
DB = ("--default-database", "demo")


def blocking(t):
    return {g["kind"] for g in t.data["gaps"] if g["affects"] != "row_selection"}


@pytest.mark.parametrize("case, column, gap", [
    ("repro1", "demo.dbo.Out1.colx", "ambiguous_column"),    # alias of a CTE reused for a real table
    ("repro1b", "demo.dbo.Out1.colx", "ambiguous_column"),   # alias of a table variable reused
    ("p1c", "demo.dbo.Out1.colx", "wildcard_without_schema"),
    ("p11c", "demo.dbo.Out1.colx", "wildcard_without_schema"),  # #t dropped and re-created
    ("p7", "demo.dbo.Out1.colx", "ambiguous_column"),        # ALTER TABLE ... DROP COLUMN cancels a proof
    ("cr2", "demo.dbo.Out1.lat", "wildcard_without_schema"),   # SET udt_col.Prop is not a column
])
def test_false_existence_proofs_leave_the_gap(case, column, gap, tmp_path):
    t = trace(ROOT / case, column, tmp_path, *DB)
    assert gap in blocking(t) and not t.complete


def test_bare_identity_is_skipped_in_positional_insert(tmp_path):
    t = trace(ROOT / "id1", "demo.dbo.T.col_a", tmp_path, *DB)
    assert [i["node"] for i in t.only_step_for(t.target_id)["inputs"]] == ["constant:10"]


def test_chained_star_ctes_are_not_exponential(tmp_path):
    started = time.monotonic()
    t = trace(ROOT / "perf16", "demo.dbo.Out1.colx", tmp_path, *DB)
    assert time.monotonic() - started < 5
    assert t.data["completeness"]["status"] in ("complete", "incomplete")
