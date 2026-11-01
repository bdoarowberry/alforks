"""The value before a write (fixtures/column/earlier_state/, invented).

A write that reads its own column's earlier content, directly or through other objects, used to show
as a cycle; a column written only by UPDATEs looked complete although the rows' earlier values were
never traced. The read now points to an "earlier content of" node that ends with a stated reason:
``prior_state`` when the writes that produce that content are traced on the page, or
``source_without_writer`` when nothing in the selected sources adds the rows.
"""
from column_helpers import FIXTURES, inputs, trace

ROOT = FIXTURES / "earlier_state"
DB = ("--default-database", "demo")


def stop(t, node):
    return next(s for s in t.data["stops"] if s["node"] == node)


def test_preserve_and_restore_reads_earlier_content_of_the_target(tmp_path):
    t = trace(ROOT, "demo.dbo.Target.col_a", tmp_path, *DB)
    target = "demo.dbo.target.col_a"
    prior = "prior_state:" + target
    assert t.data["cycles"] == []
    saved = next(i for s in t.steps_for(target) for i in inputs(s, "value") if "#saved" in i)
    assert inputs(t.only_step_for(saved), "value") == [prior]      # the dependency is kept
    end = stop(t, prior)
    assert end["reason"] == "prior_state"
    assert end["loop"]["earlier_state_of"] == target
    assert set(end["loop"]["loop_nodes"]) == {target, saved}
    assert "1 step outside the loop" in end["detail"]
    node = next(n for n in t.data["nodes"] if n["id"] == prior)
    assert node["display"] == "earlier content of demo.dbo.target.col_a" and node["earlier_state_of"] == target
    assert t.data["completeness"]["ends_by_meaning"]["earlier_content_traced"] == 1


def test_earlier_content_needs_no_proof_of_its_own(tmp_path):
    t = trace(ROOT, "demo.dbo.Target.col_a", tmp_path, *DB, "--stop-at-object", "demo.dbo.newinput")
    assert t.data["completeness"]["result"] == "reached_requested_boundary"


def test_aggregate_then_update_reads_the_working_table_before_the_update(tmp_path):
    t = trace(ROOT, "demo.dbo.Result.col_a", tmp_path, *DB)
    assert t.data["cycles"] == []
    working = t.only_step_for(t.target_id)["inputs"][0]["node"]
    prior = "prior_state:" + working
    totals = next(i for s in t.steps_for(working) for i in inputs(s, "value") if "#totals" in i)
    assert inputs(t.only_step_for(totals), "value") == [prior]
    assert stop(t, prior)["reason"] == "prior_state"
    # The working table's own load is still traced to its source.
    assert any(inputs(s, "value") == ["demo.dbo.feed.col_a"] for s in t.steps_for(working))


def test_self_update_without_a_row_creator_is_not_explained_by_its_constant(tmp_path):
    """UPDATE t SET a = a * 2 with nothing adding rows: the earlier content is an untraced source."""
    t = trace(ROOT, "demo.dbo.Scaled.amount", tmp_path, *DB)
    prior = "prior_state:demo.dbo.scaled.amount"
    assert sorted(inputs(t.only_step_for(t.target_id), "value")) == ["constant:2", prior]
    end = stop(t, prior)
    assert end["reason"] == "source_without_writer" and "adds rows to demo.dbo.scaled" in end["detail"]
    assert t.data["completeness"]["result"] == "complete_within_selected_scope"
    assert t.data["completeness"]["ends_by_meaning"]["no_writer_in_selected_scope"] == 1


def test_update_only_column_keeps_unmatched_rows_as_a_value_source(tmp_path):
    t = trace(ROOT, "demo.dbo.Flags.flag", tmp_path, *DB)
    prior = "prior_state:demo.dbo.flags.flag"
    kept = [s for s in t.steps_for(t.target_id) if s["writer_kind"] == "retained"]
    assert len(kept) == 1 and inputs(kept[0], "value") == [prior] and kept[0]["status"] == "inferred"
    assert inputs(t.only_step_for(t.target_id), "value") == ["demo.dbo.flagsource.flag"]
    assert stop(t, prior)["reason"] == "source_without_writer"
    page = (tmp_path / "demo.dbo.flags.flag.overview.md").read_text(encoding="utf-8")
    assert "rows not updated keep earlier content" in page and "Who writes `demo.dbo.flags`" in page


def test_self_update_after_an_insert_ends_at_traced_earlier_content(tmp_path):
    t = trace(ROOT, "demo.dbo.Doubled.amount", tmp_path, *DB)
    prior = "prior_state:demo.dbo.doubled.amount"
    assert not [s for s in t.steps_for(t.target_id) if s["writer_kind"] == "retained"]
    assert stop(t, prior)["reason"] == "prior_state"
    assert ("demo.dbo.raw.amount", "source_without_writer") in t.stop_pairs()


def test_variable_accumulation_reads_its_earlier_value(tmp_path):
    t = trace(ROOT, "demo.dbo.Labels.label", tmp_path, *DB)
    var = t.only_step_for(t.target_id)["inputs"][0]["node"]
    prior = "prior_state:" + var
    assert t.data["cycles"] == []
    assert sorted(tuple(sorted(inputs(s, "value"))) for s in t.steps_for(var)) == [
        ("constant:''",), ("demo.dbo.parts.part", prior)]
    assert stop(t, prior)["reason"] == "prior_state"
    page = (tmp_path / "demo.dbo.labels.label.overview.md").read_text(encoding="utf-8")
    assert "earlier value of @s" in page
