"""The value before a write (fixtures/column/earlier_state/, invented).

A write that reads its own column's earlier content, directly or through other objects, used to show
as a cycle; a column written only by UPDATEs looked complete although the rows' earlier values were
never traced. The read now points to an "earlier content of" node that ends with a stated reason:
``prior_state`` when the writes that produce that content are traced on the page, or
``source_without_writer`` when nothing in the selected sources adds the rows.
"""
from column_helpers import FIXTURES, cli_args, inputs, load_trace, run_cli, trace

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
    assert "1 step outside this read-back" in end["detail"]
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
    assert "rows not updated keep earlier content" in page and "Who loads rows into `demo.dbo.flags`" in page


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


def test_batch_and_single_target_runs_agree(tmp_path):
    """Expansions are shared between the targets of one run; the earlier-content pass of one trace must
    not change the steps another trace reuses (real run: 48 unexplained ends in 10 traces)."""
    root = FIXTURES / "earlier_state_batch"
    first, second = "demo.dbo.Doubled.amount", "demo.dbo.Copy.first_amount"
    assert run_cli(cli_args(root, [first, second], tmp_path / "batch", *DB)) == 0
    batch = load_trace(tmp_path / "batch", first, root)
    batch_second = load_trace(tmp_path / "batch", second, root)
    alone = trace(root, second, tmp_path / "alone", *DB)
    for t in (batch_second, alone):
        assert t.data["completeness"]["result"] == "complete_within_selected_scope"
        assert not t.gaps("unexplained_end")
        assert stop(t, "prior_state:demo.dbo.doubled.amount")["reason"] == "prior_state"
    strip = lambda t: (t.data["steps"], t.data["stops"], t.data["gaps"], t.data["completeness"])
    assert strip(batch_second) == strip(alone)
    assert batch.data["completeness"]["result"] == "complete_within_selected_scope"


def test_multi_column_update_reads_every_column_before_the_update(tmp_path):
    """SET a = ..., b = ... with a derived expression over both: every right-hand-side read, including
    the CROSS APPLY one, is the value before this UPDATE, with no cycle between the updated columns."""
    root = FIXTURES / "earlier_state_multi"
    for column in ("demo.dbo.Target.col_a", "demo.dbo.Capped.col_b"):
        t = trace(root, column, tmp_path / column, *DB)
        assert t.data["cycles"] == []
        derived = next(n["id"] for n in t.data["nodes"] if n["id"].startswith("derived:"))
        assert sorted(inputs(t.only_step_for(derived), "value")) == [
            "constant:0", "prior_state:demo.dbo.target.col_a", "prior_state:demo.dbo.target.col_b"]
        for col in ("col_a", "col_b"):
            node = f"demo.dbo.target.{col}"
            if t.steps_for(node):
                update = next(s for s in t.steps_for(node) if s["writer_kind"] == "update")
                reads = {i["node"] for i in update["inputs"]}
                assert f"prior_state:{node}" in reads and node not in reads
        assert t.data["completeness"]["status"] == "complete"


def test_shortened_lists_call_earlier_content_by_its_name(tmp_path):
    """When the end-point table is cut short, earlier-content ends are counted as such, not as loops."""
    cols = [f"c{i}" for i in range(1, 10)]
    sql = tmp_path / "sql"
    sql.mkdir()
    (sql / "load.sql").write_text(
        f"CREATE TABLE dbo.Src ({', '.join(c + ' int' for c in cols)});\n"
        f"CREATE TABLE dbo.Work ({', '.join(c + ' int' for c in cols)});\n"
        "CREATE TABLE dbo.Total (v int);\nGO\nCREATE PROCEDURE dbo.load_total AS\nBEGIN\n"
        f"    INSERT INTO dbo.Work ({', '.join(cols)}) SELECT {', '.join(cols)} FROM dbo.Src;\n"
        f"    UPDATE dbo.Work SET {', '.join(f'{c} = {c} + 1' for c in cols)};\n"
        f"    INSERT INTO dbo.Total (v) SELECT {' + '.join(cols)} FROM dbo.Work;\nEND\n", encoding="utf-8")
    t = trace(sql, "demo.dbo.Total.v", tmp_path / "out", *DB)
    assert t.data["cycles"] == []
    page = (tmp_path / "out" / "demo.dbo.total.v.overview.md").read_text(encoding="utf-8")
    more = next(line for line in page.splitlines() if line.startswith("And ") and "more end points" in line)
    assert "earlier content:" in more
    assert "loop" not in page.lower()
