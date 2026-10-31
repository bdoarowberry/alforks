"""Regressions from adversarial review 5 (encoding, function summaries, result labels, repairs,
declared terminals, compare). Fixtures: fixtures/column/review5/<case>/."""
import json

import pytest

from column_helpers import FIXTURES, inputs, run_cli, trace

ROOT = FIXTURES / "review5"
DB = ("--default-database", "demo")


def run(case, column, tmp_path, *extra):
    return trace(ROOT / case, column, tmp_path, *DB, *extra)


def value_sources(t) -> set[str]:
    seen, frontier, out = set(), [t.target_id], set()
    while frontier:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        steps = t.steps_for(node)
        if not steps and node != t.target_id:
            out.add(node)
        for step in steps:
            frontier.extend(inputs(step, "value"))
    return out


def test_f1_stray_cp1252_byte_keeps_a_utf8_file_utf8(tmp_path):
    t = run("encoding_mixed", "demo.dbo.out.Größe", tmp_path)
    assert "demo.stage.src.größe" in value_sources(t)


def test_f2_utf16_without_bom_and_non_latin_text_is_read(tmp_path):
    t = run("encoding_utf16_cyrillic", "demo.dbo.out.code", tmp_path)
    assert t.steps, "the only writer is in the UTF-16 file"
    assert t.data["scan"]["files_without_statements"] == 0


def test_f3_clr_function_is_opaque(tmp_path):
    t = run("fn_clr", "demo.dbo.out.val", tmp_path)
    [gap] = t.gaps("opaque_function")
    assert "CLR" in gap["message"] and not t.complete


def test_f4_function_with_differing_definitions_is_opaque(tmp_path):
    t = run("fn_shadow_calls", "demo.dbo.out.val", tmp_path)
    [gap] = t.gaps("opaque_function")
    assert "different definitions" in gap["message"] and not t.complete


@pytest.mark.parametrize("case,column,extra", [
    ("labels_boundary_param", "demo.dbo.out.code_note", ("--stop-at-object", "demo.stage.*")),
    ("labels_boundary_depth", "demo.dbo.out.x", ("--stop-at-object", "demo.stage.*", "--max-depth", "1")),
])
def test_f5_boundary_label_needs_every_end_at_the_boundary(case, column, extra, tmp_path):
    t = run(case, column, tmp_path, *extra)
    assert t.completeness["result"] != "reached_requested_boundary"


def test_f6_depth_cut_has_its_own_label_and_is_never_declared_away(tmp_path):
    t = run("labels_depth_param", "demo.dbo.out.code", tmp_path / "a", "--max-depth", "1")
    assert t.completeness["result"] == "complete_to_requested_depth"
    t = run("labels_depth_param", "demo.dbo.out.code", tmp_path / "b", "--max-depth", "1",
            "--declared-terminals", str(ROOT / "labels_depth_param" / "terminals.txt"))
    assert t.completeness["result"] == "complete_to_requested_depth"
    t = run("labels_depth_param", "demo.dbo.out.note", tmp_path / "c",
            "--declared-terminals", str(ROOT / "labels_depth_param" / "terminals_hash.txt"))
    assert t.completeness["result"] != "complete_to_declared_boundary"   # a parameter is never a terminal


def test_f7_function_identity_and_roles(tmp_path):
    t = run("fn_variants", "demo.dbo.out.c_threepart", tmp_path / "a")
    assert "opaque_function" in t.gap_kinds                              # otherdb's function is not scanned
    t = run("fn_variants", "demo.dbo.out.c_cond", tmp_path / "b")
    step = t.only_step_for(t.target_id)
    assert "demo.stage.src.flag" in inputs(step, "condition") and "demo.stage.src.flag" not in inputs(step, "value")
    t = run("fn_variants", "demo.dbo.out.c_decimal", tmp_path / "c")
    assert "ambiguous_column" not in t.gap_kinds                         # DEFAULT is not a column


def test_f8_compound_assignment_before_a_comment(tmp_path):
    t = run("compound_comment", "demo.dbo.out.total", tmp_path)
    assert "parse_error" not in t.gap_kinds and t.steps


@pytest.mark.parametrize("column", ["a", "b", "c", "d"])
def test_f9_procedure_options_do_not_swallow_the_first_statement(column, tmp_path):
    t = run("proc_headers", f"demo.dbo.out.{column}", tmp_path)
    assert "parse_error" not in t.gap_kinds and "demo.stage.src.code" in value_sources(t)


def test_f10_declared_terminals_parsing(tmp_path):
    assert run_cli(["--source-root", str(ROOT / "labels_depth_param"), "--column", "demo.dbo.out.code",
                    "--output", str(tmp_path / "x"), "--no-progress", *DB, "--declared-terminals",
                    str(ROOT / "labels_depth_param" / "terminals_bad.json")]) == 2
    from column_lineage.cli import load_terminals
    patterns = [p for p, _ in load_terminals(ROOT / "labels_depth_param" / "terminals_hash.txt")]
    assert "#tmp_feed.*" in patterns


def test_f11_linked_server_with_empty_database_is_not_guessed(tmp_path):
    t = run("dotdot_names", "demo.dbo.out.code2", tmp_path, "--local-server", "remotesrv")
    assert "demo.dbo.src.code" not in value_sources(t)


def test_f12_compare_two_part_sources_match_only_file_columns():
    from column_lineage.compare import _matches
    assert _matches("flat_file:pkg_a.dtsx::feed.code", "feed.code")
    assert _matches("flat_file:pkg_a.dtsx::feed.code", "pkg_a.dtsx::feed.code")
    assert not _matches("flat_file:pkg_b.dtsx::feed.code", "pkg_a.dtsx::feed.code")
    assert not _matches("demo.archive.feed.code", "feed.code")
    assert not _matches("flat_file:pkg_a.dtsx::[old.feed].code", "feed.code")
    assert _matches("demo.stage.src.code", "stage.src.code")
