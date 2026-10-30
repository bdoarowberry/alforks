"""Regressions from the adversarial semantics review: each case once produced a false or incomplete proof.

Fixtures are in fixtures/column/review/<case>/.
"""
from column_helpers import FIXTURES, inputs, row_selection_pairs, trace

DB = ("--default-database", "db")


def review(case, column, tmp_path, *extra):
    return trace(FIXTURES / "review" / case, column, tmp_path, *extra)


def reach(t, roles=("value",)) -> set[str]:
    """Every node reachable from the target through value inputs."""
    seen, frontier = set(), [t.target_id]
    while frontier:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        for step in t.steps_for(node):
            for role in roles:
                frontier.extend(inputs(step, role))
    return seen


def test_reused_derived_aliases_in_union_stay_distinct(tmp_path):
    t = review("union_alias", "demo.output.result_value", tmp_path, *DB)
    assert {"db.demo.source.input_a", "db.demo.other.v"} <= reach(t)


def test_nested_derived_tables_with_same_alias_do_not_self_cycle(tmp_path):
    t = review("nested_alias", "demo.output.result_value", tmp_path, *DB)
    assert "db.demo.source.input_a" in reach(t)
    assert not t.data["cycles"]


def test_same_alias_in_scalar_subquery_is_a_different_table(tmp_path):
    t = review("sibling_same_expr", "demo.output.result_value", tmp_path, *DB)
    assert {"db.demo.source.input_a", "db.demo.other.v"} <= reach(t)


def test_writer_without_database_context_unifies_with_the_only_known_database(tmp_path):
    t = review("unify", "db1.demo.output.result_value", tmp_path)
    assert t.steps_for(t.target_id)
    assert "db1.demo.source.input_a" in reach(t)


def test_cursor_fetch_assigns_variables(tmp_path):
    t = review("cursor", "demo.output.result_value", tmp_path, *DB)
    assert "db.demo.source.input_a" in reach(t)


def test_compound_assignment_update_set_variable_and_exec_return_code(tmp_path):
    t = review("var_forms", "demo.output.id", tmp_path, *DB)
    assert "db.demo.source.input_a" in reach(t)          # SET @a += (SELECT ...)
    t = review("var_forms", "demo.output.result_value", tmp_path, *DB)
    assert "db.demo.source.input_b" in reach(t)          # UPDATE ... SET @b = input_b
    t = review("var_forms", "demo.output.other_value", tmp_path, *DB)
    assert "unresolved_variable" in t.gap_kinds and not t.complete   # EXEC @c = proc


def test_parameter_reassigned_in_body(tmp_path):
    t = review("param_reassign", "demo.output.result_value", tmp_path, *DB)
    assert {"db.demo.other.v", "constant:5"} <= reach(t)


def test_parameter_default_when_caller_omits_it(tmp_path):
    t = review("exec_variants", "demo.output.other_value", tmp_path, *DB)
    assert {"constant:3", "constant:9"} <= reach(t)


def test_update_through_cte_writes_the_base_table(tmp_path):
    t = review("cte_update", "demo.output.result_value", tmp_path, *DB)
    assert "db.demo.source.input_b" in reach(t)


def test_update_by_unaliased_exposed_name(tmp_path):
    t = review("update_exposed", "demo.output.result_value", tmp_path, *DB)
    assert "db.demo.source.input_a" in reach(t)


def test_insert_through_synonym_writes_its_target(tmp_path):
    t = review("syn_write", "stage.dbo.landing.amount", tmp_path, *DB)
    assert "db.demo.source.input_a" in reach(t)


def test_unqualified_name_in_subquery_over_unknown_schema_is_ambiguous(tmp_path):
    t = review("corr_writer", "demo.output.result_value", tmp_path, *DB)
    assert "ambiguous_column" in t.gap_kinds and not t.complete
    assert "db.demo.source.input_b" in reach(t)


def test_exec_without_database_context_matching_two_procedures_is_ambiguous(tmp_path):
    t = review("multidb2", "db1.dbo.out1.val", tmp_path, "--database-map", "db1=db1", "db2=db2")
    assert "ambiguous_object" in t.gap_kinds and not t.complete


def test_same_procedure_name_twice_in_one_file_has_separate_scopes(tmp_path):
    t = review("multidb", "db1.dbo.out1.val", tmp_path)
    assert len(t.ids_matching("parameter:", "::@p")) == 2


def test_computed_column_traces_its_definition(tmp_path):
    t = review("computed", "db.demo.output.total", tmp_path, *DB)
    assert {"db.demo.source.input_a", "db.demo.source.input_b"} <= reach(t)


def test_repeated_source_root_flags_are_all_scanned(tmp_path):
    root = FIXTURES / "review" / "roots"
    t = trace(root / "a", "demo.output.result_value", tmp_path, "--source-root", str(root / "b"))
    assert len(t.data["scan"]["roots"]) == 2
    assert t.steps_for(t.target_id)


def test_dropped_and_recreated_temp_table_forgets_earlier_rows(tmp_path):
    t = review("temp_recreate", "demo.output.result_value", tmp_path, *DB)
    found = reach(t)
    assert "db.demo.other.v" in found and "db.demo.source.input_a" not in found


def test_callee_that_runs_after_the_read_does_not_contribute(tmp_path):
    t = review("callee_after", "demo.output.result_value", tmp_path, *DB)
    found = reach(t)
    assert "db.demo.source.input_a" in found and "db.demo.other.v" not in found


def test_insert_that_omits_the_column_contributes_null(tmp_path):
    t = review("omitted_col", "demo.output.result_value", tmp_path, *DB)
    assert "insert_omitted" in {s["writer_kind"] for s in t.steps_for(t.target_id)}
    assert "constant:null" in reach(t)


def test_top_order_by_is_row_selection(tmp_path):
    t = review("top_order", "demo.output.result_value", tmp_path, *DB)
    pairs = set().union(*(row_selection_pairs(s) for s in t.steps))
    assert ("db.demo.other.dt", "top_order") in pairs


def test_scalar_subquery_predicates_in_where_are_row_selection(tmp_path):
    t = review("where_subq", "demo.output.result_value", tmp_path, *DB)
    pairs = set().union(*(row_selection_pairs(s) for s in t.steps))
    assert ("db.demo.other.k", "filter") in pairs


def test_alter_table_add_on_select_into_temp(tmp_path):
    t = review("alter_add", "demo.output.result_value", tmp_path, *DB)
    assert "missing_definition" not in t.gap_kinds
    assert "db.demo.other.v" in reach(t)


def test_temp_table_persists_across_go_batches_of_a_script(tmp_path):
    t = review("script_batches", "demo.output.result_value", tmp_path, *DB)
    assert "db.demo.source.input_a" in reach(t)


def test_single_merge_is_not_described_as_two_ordered_writers(tmp_path):
    t = review("merge_derived", "demo.output.result_value", tmp_path, *DB)
    for step in t.steps_for(t.target_id):
        assert not any("Runs after earlier writers" in n for n in step["notes"])


# ---- real-source feedback, round 2

def test_wildcard_step_cites_the_star_and_its_source_schema(tmp_path):
    t = review("wildcard_cte", "schema1.stage2.target_col", tmp_path, *DB)
    step = t.only_step_for(t.target_id)
    assert step["mapping_method"] == "wildcard_schema"
    assert step["status"] == "inferred"
    star = next(e for e in step["evidence"] if e["role"] == "wildcard")
    assert star["start_line"] == 11 and "s.*" in star["excerpt"]
    assert any(e["role"] == "source_schema" and e["start_line"] == 1 for e in step["evidence"])
    assert "db.schema1.stage1.target_col" in reach(t)


def test_qualified_names_in_insert_column_list_are_repaired(tmp_path):
    t = review("insert_qualified_cols", "demo.output.result_value", tmp_path, *DB)
    assert "db.demo.source.input_a" in reach(t)
