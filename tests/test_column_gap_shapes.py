"""Gap shapes from the tester's 178-column run (fixtures/column/gap_shapes/, invented).

- A * over a table whose scanned definitions differ still supplies the names every definition has.
- A positional INSERT into a SELECT ... INTO table uses the column order that SELECT defines, unless an
  identity, computed or rowversion column may have been copied.
- An unqualified column with one source binds to it even when its scanned definition lacks the column
  (ALTER TABLE ... WITH CHECK ADD is now read); genuinely ambiguous joins stay ambiguous.
- INSERT ... EXEC of a named procedure maps the callee's one result set by position; a same-named
  #temp table of another procedure is never a writer; several result sets stay a gap.
- A call of an inline table-valued function is read as its RETURN query.
- A table with no definition or writer can be declared a terminal.
"""
import json

from column_helpers import FIXTURES, inputs, trace

ROOT = FIXTURES / "gap_shapes"
DB = ("--default-database", "demo")


def json_text(value):
    return json.dumps(value)


def kinds(t):
    return {g["kind"] for g in t.data["gaps"] if g["affects"] != "row_selection"}


def reaches(t, node):
    seen, frontier = set(), [t.target_id]
    while frontier:
        current = frontier.pop()
        if current not in seen:
            seen.add(current)
            for step in t.steps_for(current):
                frontier.extend(inputs(step, "value"))
    return node in seen


def test_star_over_differing_definitions_supplies_their_common_columns(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_cte.col_a", tmp_path, *DB)
    assert kinds(t) == set() and reaches(t, "demo.dbo.sourcetable.col_a")
    picked = [s for s in t.steps if "by name" in json_text(s)]
    assert picked and all(s["status"] == "inferred" for s in picked)


def test_positional_insert_uses_the_select_into_column_order(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_star.col_a", tmp_path, *DB)
    assert kinds(t) == set() and reaches(t, "demo.dbo.sourcetable.col_a")
    copy = next(i for i in inputs(t.only_step_for(t.target_id), "value") if "#copy" in i)
    step = t.only_step_for(copy)
    assert step["status"] == "inferred" and any(e["role"] == "target_schema" for e in step["evidence"])


def test_positional_insert_into_a_copy_with_identity_keeps_its_gap(tmp_path):
    for column in ("demo.dbo.Out_identity.col_a", "demo.dbo.Out_identity2.col_a"):
        t = trace(ROOT, column, tmp_path / column, *DB)
        assert "positional_without_schema" in kinds(t)


def test_alter_with_check_add_column_is_part_of_the_definition(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_added.col_new", tmp_path, *DB)
    assert kinds(t) == set() and reaches(t, "demo.dbo.added.col_new")


def test_single_source_column_missing_from_its_definition_binds_to_that_source(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_late.col_new", tmp_path, *DB)
    assert "ambiguous_column" not in kinds(t)
    assert inputs(t.only_step_for(t.target_id), "value") == ["demo.dbo.late.col_new"]
    assert "missing_definition" in kinds(t)   # the definition does not list it: said, not hidden


def test_unqualified_column_in_a_join_stays_ambiguous(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_join.col_a", tmp_path, *DB)
    assert "ambiguous_column" in kinds(t)


def test_insert_exec_maps_the_named_procedures_result_set(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_exec.amount", tmp_path, *DB)
    assert kinds(t) == set()
    assert reaches(t, "demo.dbo.sourcetable.col_a")
    mapped = [s for s in t.steps if s["writer_kind"] == "insert_exec"]
    assert len(mapped) == 1 and mapped[0]["status"] == "inferred"
    assert any(e["role"] == "insert_exec_call" for e in mapped[0]["evidence"])
    assert "archive" not in json_text(t.data).lower()   # another procedure's #result is not a writer


def test_insert_exec_with_several_result_sets_stays_a_gap(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_two.amount", tmp_path, *DB)
    assert "insert_exec" in kinds(t)
    assert not [s for s in t.steps if s["writer_kind"] == "insert_exec"]


def test_inline_table_valued_function_is_read_as_its_query(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_tvf.col_a", tmp_path, *DB)
    assert kinds(t) == set() and reaches(t, "demo.dbo.sourcetable.col_a")
    inlined = [s for s in t.steps if any(e["role"] == "function_definition" for e in s["evidence"])]
    assert inlined and all(s["status"] == "inferred" for s in inlined)


def test_table_without_definition_can_be_declared_a_terminal(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_backup.col_a", tmp_path / "plain", *DB)
    assert "missing_definition" in kinds(t)
    terminals = tmp_path / "terminals.txt"
    terminals.write_text("demo.dbo.out_backup_saved  # saved copy, out of scope for this investigation\n",
                         encoding="utf-8")
    t = trace(ROOT, "demo.dbo.Out_backup.col_a", tmp_path / "declared", *DB, "--declared-terminals", str(terminals))
    assert kinds(t) == set()
    assert t.data["completeness"]["result"] == "complete_to_declared_boundary"


def test_gap_summary_gives_anonymous_reasons_per_gap_kind(tmp_path):
    from column_helpers import cli_args, run_cli
    columns = ["demo.dbo.Out_join.col_a", "demo.dbo.Out_two.amount", "demo.dbo.Out_identity.col_a",
               "demo.dbo.Out_late.col_new"]
    assert run_cli(cli_args(ROOT, columns, tmp_path, *DB, "--gap-summary")) == 0
    reasons = json.loads((tmp_path / "gap-summary.json").read_text(encoding="utf-8"))["totals"][
        "value_gap_traces_by_reason"]
    assert reasons["ambiguous_column"] == {"several_joined_candidates": 1}
    assert reasons["insert_exec"] == {"callee_has_several_result_statements": 1}
    assert reasons["positional_without_schema"] == {"select_into_target_order_not_settled": 1}
    assert reasons["missing_definition"] == {"column_not_in_scanned_definition": 1}
    assert "demo" not in json.dumps(reasons).lower()   # categories only, no names


def test_insert_exec_split_over_two_lines_is_mapped(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_split.amount", tmp_path, *DB)
    assert kinds(t) == set() and reaches(t, "demo.dbo.sourcetable.col_a")
    assert [s for s in t.steps if s["writer_kind"] == "insert_exec"]


def test_cross_apply_of_an_inline_function_without_a_subquery(tmp_path):
    t = trace(ROOT, "demo.dbo.Out_apply.col_a", tmp_path, *DB)
    assert kinds(t) == set() and reaches(t, "demo.dbo.sourcetable.col_a")
