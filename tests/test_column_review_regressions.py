"""Regressions from the adversarial semantics review: each case once produced a false or incomplete proof.

Fixtures are in fixtures/column/review/<case>/.
"""
import json

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


# ---- real-source feedback, round 3

def test_round3_parse_repairs(tmp_path):
    t = review("round3_parse", "demo.output.result_value", tmp_path, *DB)
    assert "db.demo.source.input_a" in reach(t)                      # spaced ! = and < = repaired
    pairs = set().union(*(row_selection_pairs(s) for s in t.steps))
    assert ("db.demo.source.flag", "filter") in pairs
    assert t.data["scan"]["fallback"] == 0                             # PK CLUSTERED, same-line DECLAREs, ALTER ADD PK
    assert "parse_error" not in t.gap_kinds


def test_where_proof_ends_says_when_no_path_reaches_the_boundary(tmp_path):
    t = review("round3_parse", "demo.output.result_value", tmp_path, *DB, "--stop-at-database", "elsewhere")
    assert "No value path reaches the requested boundary" in t.markdown
    t = review("round3_parse", "demo.output.result_value", tmp_path / "b", *DB, "--stop-at-database", "db")
    assert "No value path reaches the requested boundary" not in t.markdown


def test_every_value_path_end_is_explained():
    from column_lineage.model import COLUMN, Boundary, Input, Node, ObjectRef, Step, TraceResult, VALUE
    from column_lineage.tracer import Tracer
    target = Node(COLUMN, "c", ObjectRef("t", "s", "db"))
    leaf = Node(COLUMN, "x", ObjectRef("u", "s", "db"))
    result = TraceResult(target, "db.s.t.c", Boundary(), {})
    result.add_node(target)
    result.add_node(leaf)
    result.steps.append(Step(target, "insert_select", "x", "rename", [Input(leaf, VALUE, "x")]))
    Tracer._explain_ends(result)
    assert [g.kind for g in result.gaps.values()] == ["unexplained_end"]
    assert result.completeness()["status"] == "incomplete"


def test_worker_processes_print_no_sql(tmp_path):
    import shutil
    from column_lineage_helpers_subprocess import run_with_workers
    src = tmp_path / "src"
    src.mkdir()
    for i in range(24):
        shutil.copy(FIXTURES / "parse_failure" / "load_output.sql", src / f"f{i}.sql")
    proc = run_with_workers(src, tmp_path / "out")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stderr.strip() == ""


# ---- real-source feedback, round 4

def test_variables_whose_names_start_with_a_digit(tmp_path):
    t = review("digit_variable", "demo.output.result_value", tmp_path, *DB)
    assert t.data["scan"]["fallback"] == 0
    step = next(s for s in t.steps_for(t.target_id))
    assert {"db.demo.source.input_a", "db.demo.source.input_b"} <= set(inputs(step, "value"))
    assert any(i.endswith("::@2_periods_ago") for i in inputs(step, "condition"))
    assert not any("__d" in i for i in t.all_ids())


# ---- assertions for lineage SQL cannot prove

def test_dynamic_sql_without_assertions_is_incomplete(tmp_path):
    t = review("assert_dynamic", "demo.output.result_value", tmp_path, *DB)
    assert "dynamic_sql" in t.gap_kinds and not t.complete


def test_assertions_continue_the_trace_and_resolve_the_gap(tmp_path):
    root = FIXTURES / "review" / "assert_dynamic"
    t = review("assert_dynamic", "demo.output.result_value", tmp_path, *DB, "--assertions", str(root / "assertions.json"))
    found = reach(t)
    assert {"db.demo.source_x.col_a", "db.demo.source_y.col_b"} <= found
    asserted = [s for s in t.steps if s["writer_kind"] == "asserted"]
    assert len(asserted) == 2 and all(s["status"] == "asserted" for s in asserted)
    assert "dynamic_sql" not in t.gap_kinds
    assert [g["resolved_by"] for g in t.data["resolved_gaps"]] and all(g["resolved_by"] == "A1" for g in t.data["resolved_gaps"])
    comp = t.data["completeness"]
    assert t.complete and comp["relies_on_assertions"] and comp["asserted_steps"] == 2
    assert "relies on 2 asserted steps" in t.markdown
    diagnostics = json.loads((tmp_path / "scan-diagnostics.json").read_text(encoding="utf-8"))
    assert diagnostics["assertions_not_used"] == ["A2"]
    assert any(e.startswith("A2:") for e in diagnostics["assertion_errors"])


def test_malformed_assertions_file_is_a_usage_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"assertions": [{"id": "X", "target": "a.b.c"}]}', encoding="utf-8")
    from column_helpers import cli_args, run_cli
    args = cli_args(FIXTURES / "review" / "assert_dynamic", "demo.output.result_value", tmp_path / "o",
                    "--assertions", str(bad))
    assert run_cli(args) == 2


# ---- real-source feedback, round 5

def test_linked_server_objects_are_external_unless_declared_local(tmp_path):
    base = FIXTURES / "review" / "linked_server"
    t = trace(base / "main", "demo.output.result_value", tmp_path / "a", *DB, "--source-root", str(base / "remote"))
    assert "external_object" in t.gap_kinds and not t.complete
    t = trace(base / "main", "demo.output.result_value", tmp_path / "b", *DB, "--source-root", str(base / "remote"),
              "--local-server", "lnk")
    assert "external_object" not in t.gap_kinds
    assert "remote_db.dbo.staging.raw_amount" in reach(t)


def test_assertion_reliance_is_stated_at_the_top_and_effects_are_listed(tmp_path):
    root = FIXTURES / "review" / "assert_dynamic"
    t = review("assert_dynamic", "demo.output.result_value", tmp_path, *DB, "--assertions", str(root / "assertions.json"))
    top = t.markdown.split("## 2.", 1)[0]
    assert "This trace relies on 2 asserted steps" in top
    asserted = next(s for s in t.steps if s["writer_kind"] == "asserted")
    ev = asserted["evidence"][0]
    assert ev["root"] is None and ev["path"].endswith("assert_dynamic/assertions.json")
    assert (tmp_path / ev["path"]).resolve() == (root / "assertions.json").resolve()   # relative to the page
    diagnostics = json.loads((tmp_path / "scan-diagnostics.json").read_text(encoding="utf-8"))
    effects = diagnostics["assertion_effects"]["A1"]
    assert [e["kind"] for e in effects] == ["dynamic_sql"] and effects[0]["location"] == "load.sql:13"


def test_statements_behind_repeated_gaps_are_grouped(tmp_path):
    t = review("dynamic_two_columns", "demo.output.result_value", tmp_path, *DB)
    dynamic = t.gaps("dynamic_sql")
    assert len(dynamic) == 2 and len({g["evidence"][0]["statement_id"] for g in dynamic}) == 1
    assert "2 gaps that cite a statement come from 1 statement" in t.markdown
    assert "  - `load.sql:6` — 2 gaps (dynamic_sql) on 2 nodes" in t.markdown
    summary = t.data["completeness"]
    assert summary["value_gaps_by_kind"] == {"dynamic_sql": 2}


# ---- real-source feedback, round 6

def test_row_selection_ctes_are_not_traced_by_default(tmp_path):
    t = review("row_selection_cte", "demo.output.result_value", tmp_path, *DB)
    on_path = set(reach(t, roles=("value", "condition", "window")))
    assert all(s["target"] in on_path for s in t.steps), "steps off the value path"
    assert "wildcard_without_schema" not in t.gap_kinds
    assert t.complete
    # With --trace-row-selection the CTE is expanded; its SELECT f.* has no known schema, so the result
    # is a wildcard gap that affects row selection only.
    t = review("row_selection_cte", "demo.output.result_value", tmp_path / "rs", *DB, "--trace-row-selection")
    wildcard = t.gaps("wildcard_without_schema")
    assert wildcard and all(g["affects"] == "row_selection" for g in wildcard)
    assert t.complete


def test_dynamic_sql_that_can_only_be_empty_is_not_a_gap(tmp_path):
    t = review("dead_dynamic", "demo.output.result_value", tmp_path, *DB)
    assert "dynamic_sql" not in t.gap_kinds and t.complete
    diagnostics = json.loads((tmp_path / "scan-diagnostics.json").read_text(encoding="utf-8"))
    assert any(i["kind"] == "dead_dynamic_sql" for i in diagnostics["statement_issues"])


def test_dynamic_sql_assigned_real_text_or_from_a_parameter_stays_a_gap(tmp_path):
    t = review("dead_dynamic", "demo.output2.result_value", tmp_path / "live", *DB)
    assert "dynamic_sql" in t.gap_kinds and not t.complete
    t = review("dead_dynamic", "demo.output3.result_value", tmp_path / "param", *DB)
    assert "dynamic_sql" in t.gap_kinds and not t.complete


# ---- real-source feedback, round 7: a writer must never be dropped silently

def test_writer_after_an_if_condition_on_one_line_is_traced(tmp_path):
    t = review("condition_then_writer", "db1.schema1.destination.reference_no", tmp_path)
    assert "db1.schema1.stage.source_ref" in reach(t)
    step = t.only_step_for(t.target_id)
    assert any(c.startswith("IF NOT EXISTS") for c in step["branch_context"])
    t = review("condition_then_writer", "db1.schema1.destination.amount", tmp_path / "update")
    assert "db1.schema1.stage.amount" in reach(t)


def test_writer_glued_after_non_lineage_statements_is_not_dropped(tmp_path):
    t = review("condition_then_writer", "db1.schema1.destination.note", tmp_path)
    assert t.steps_for(t.target_id) or t.gap_kinds & {"parse_error", "unsupported_statement"}
    assert (t.target_id, "source_without_writer") not in t.stop_pairs()
