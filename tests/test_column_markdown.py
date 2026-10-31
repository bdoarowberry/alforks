"""Tests for the column lineage Markdown renderer.

``python tests/test_column_markdown.py --write-sample`` regenerates
``docs/examples/sample-trace.md`` from ``sample_trace()``.
"""
import json
import random
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from column_lineage.model import (  # noqa: E402
    CONDITION, VALUE, Boundary, Evidence, Gap, Input, Node, ObjectRef, RowSelection, Step, Stop, TraceResult,
)
from column_lineage.render_markdown import SECTION_TITLES, main, render_markdown  # noqa: E402

SAMPLE_MD = ROOT / "docs" / "examples" / "sample-trace.md"
SAMPLE_JSON_NAME = "sample-trace.lineage.json"


# --------------------------------------------------------------------------- builders

def col(database, schema, table, column, kind="table"):
    return Node("column", column, ObjectRef(table, schema, database, kind=kind, database_source="explicit"))


def temp_col(scope, table, column):
    return Node("temp_column", column, ObjectRef(table, kind="temp_table", scope=scope))


def ev(path, line, excerpt, role="statement", end=None):
    return Evidence(path, line, end or line, excerpt, role=role, statement_id=f"stmt:{path}:{line}", root="sql")


def sample_trace() -> TraceResult:
    """A realistic synthetic trace: rename through #work, CASE, filter + join key, dynamic SQL, boundary."""
    proc_load = "reporting.demo.load_output"
    proc_fix = "reporting.demo.apply_overrides"
    target = col("reporting", "demo", "output", "result_value")
    work = temp_col(f"procedures/load_output.sql::{proc_load}", "#work", "chosen_value")
    input_a = col("staging", "demo", "source", "input_a")
    input_b = col("staging", "demo", "source", "input_b")
    src_id = col("staging", "demo", "source", "id")
    is_active = col("staging", "demo", "source", "is_active")
    key_id = col("staging", "demo", "lookup_key", "id")
    override = Node("variable", "@override_value", scope=f"procedures/apply_overrides.sql::{proc_fix}")

    trace = TraceResult(
        target=target, requested="reporting.demo.output.result_value",
        boundary=Boundary(databases=("staging",)),
        scan={"roots": ["sql"], "files": 3, "statements": 14, "parsed": 13, "fallback": 1,
              "source_revision": "synthetic-r1"},
    )
    for node in (target, work, input_a, input_b, src_id, is_active, key_id, override):
        trace.add_node(node)

    dyn = Gap("dynamic_sql", "@override_value is assigned by dynamic SQL (EXEC sp_executesql); its source "
                             "columns cannot be established statically.", node=override,
              evidence=[ev("procedures/apply_overrides.sql", 9,
                           "EXEC sp_executesql @sql, N'@v int OUTPUT', @v = @override_value OUTPUT;")],
              detail={"statement_keyword": "EXEC", "variable": "@override_value"})
    dyn_id = trace.add_gap(dyn)

    trace.steps.append(Step(
        target=target, writer_kind="insert_select", expression="chosen_value", operation="rename",
        mapping_method="target_list", procedure=proc_load, order=1,
        inputs=[Input(work, VALUE, "chosen_value")],
        evidence=[ev("procedures/load_output.sql", 14,
                     "INSERT INTO reporting.demo.output (id, result_value) SELECT id, chosen_value FROM #work",
                     end=16)],
    ))
    case_expr = "CASE WHEN s.input_a IS NOT NULL THEN s.input_a ELSE s.input_b END"
    trace.steps.append(Step(
        target=work, writer_kind="select_into", expression=case_expr, operation="case",
        mapping_method="projection_alias", procedure=proc_load, order=1,
        inputs=[
            Input(input_a, CONDITION, "s.input_a", branch="WHEN s.input_a IS NOT NULL"),
            Input(input_a, VALUE, "s.input_a", branch="WHEN s.input_a IS NOT NULL THEN"),
            Input(input_b, VALUE, "s.input_b", branch="ELSE"),
        ],
        row_selection=[
            RowSelection(src_id, "join_key", "s.id = k.id", ev("procedures/load_output.sql", 10, "JOIN staging.demo.lookup_key AS k ON s.id = k.id", "join")),
            RowSelection(key_id, "join_key", "s.id = k.id", ev("procedures/load_output.sql", 10, "JOIN staging.demo.lookup_key AS k ON s.id = k.id", "join")),
            RowSelection(is_active, "filter", "s.is_active = 1", ev("procedures/load_output.sql", 11, "WHERE s.is_active = 1", "filter")),
        ],
        evidence=[ev("procedures/load_output.sql", 5, f"{case_expr} AS chosen_value", "expression"),
                  ev("procedures/load_output.sql", 3, "SELECT s.id, CASE ... END AS chosen_value INTO #work "
                                                      "FROM staging.demo.source AS s JOIN ... WHERE ...", end=11)],
    ))
    trace.steps.append(Step(
        target=target, writer_kind="update", expression="@override_value", operation="direct",
        mapping_method="set_clause", procedure=proc_fix, order=2, status="partial",
        branch_context=["IF @apply_override = 1"],
        inputs=[Input(override, VALUE, "@override_value", status="unresolved", gap=dyn_id)],
        evidence=[ev("procedures/apply_overrides.sql", 12,
                     "UPDATE reporting.demo.output SET result_value = @override_value WHERE id = @id")],
        gaps=[dyn_id],
    ))
    trace.stops += [Stop(input_a, "boundary", "database staging is in the requested boundary"),
                    Stop(input_b, "boundary", "database staging is in the requested boundary")]
    return trace


def chain_trace(cycle=False, shared=False) -> TraceResult:
    """t <- a <- b (<- a when cycle). With shared, t <- a and t <- b, a <- c, b <- c, c <- d."""
    def n(name):
        return col("db", "s", "t", name)
    target = n("target")
    trace = TraceResult(target=target, requested="db.s.t.target", boundary=Boundary(), scan={"roots": ["r"]})

    def step(tgt, *srcs, line=1):
        trace.steps.append(Step(target=tgt, writer_kind="update", expression=" + ".join(s.name for s in srcs),
                                operation="arithmetic" if len(srcs) > 1 else "direct", mapping_method="set_clause",
                                inputs=[Input(s, VALUE, s.name) for s in srcs],
                                evidence=[ev("x.sql", line, "UPDATE ...")]))
        for node in (tgt, *srcs):
            trace.add_node(node)
    a, b, c, d = n("a"), n("b"), n("c"), n("d")
    if shared:
        step(target, a, b, line=1)
        step(a, c, line=2)
        step(b, c, line=3)
        step(c, d, line=4)
        trace.stops.append(Stop(d, "source_without_writer"))
    else:
        step(target, a, line=1)
        step(a, b, line=2)
        if cycle:
            step(b, a, line=3)
            trace.cycles.append([a.id, b.id, a.id])
            trace.add_gap(Gap("cycle", "a and b feed each other", node=a))
    return trace


def section(md: str, title: str) -> str:
    """Body of one '## n. Title' section."""
    match = re.search(rf"^## \d+\. {re.escape(title)}\n(.*?)(?=^## \d+\. |\Z)", md, re.S | re.M)
    assert match, title
    return match.group(1)


def tree(md: str) -> str:
    body = section(md, "Value lineage overview")
    match = re.search(r"^(`{3,})text\n(.*?)^\1$", body, re.S | re.M)
    assert match
    return match.group(2)


# --------------------------------------------------------------------------- tests

def test_six_sections_in_order():
    md = render_markdown(sample_trace().to_dict(), SAMPLE_JSON_NAME)
    positions = [md.index(f"## {i}. {title}") for i, title in enumerate(SECTION_TITLES, 1)]
    assert positions == sorted(positions)
    assert md.startswith("# Column lineage: `reporting.demo.output.result_value`")


def test_identity_and_result():
    md = render_markdown(sample_trace().to_dict(), SAMPLE_JSON_NAME)
    ident = section(md, "Identity and result")
    assert "**Result: Incomplete — 1 value-path gap**" in ident
    assert "`reporting.demo.output.result_value`" in ident
    assert "databases `staging`" in ident
    assert "`synthetic-r1`" in ident
    assert "3 files, 14 statements, 13 parsed, 1 parse fallbacks" in ident
    assert "not a claim" in ident
    assert SAMPLE_JSON_NAME in ident


def test_complete_and_no_boundary():
    trace = chain_trace(shared=True)
    md = render_markdown(trace.to_dict())
    ident = section(md, "Identity and result")
    assert "**Result: Complete within the selected sources" in ident
    assert "| Requested boundary | none |" in ident
    assert "Uncertainty:** none recorded" in section(md, "Short answer")


def test_short_answer_states_sources_conditions_and_uncertainty():
    md = render_markdown(sample_trace().to_dict())
    short = section(md, "Short answer")
    assert "`staging.demo.source.input_a`" in short and "`staging.demo.source.input_b`" in short
    assert "requested boundary" in short
    assert "`WHEN s.input_a IS NOT NULL THEN` → `staging.demo.source.input_a`" in short
    assert "`ELSE` → `staging.demo.source.input_b`" in short
    assert "chosen by `WHEN s.input_a IS NOT NULL` (reads `staging.demo.source.input_a`)" in short
    assert "2 alternative writers" in short
    assert "dynamic_sql: 1" in short
    assert "partial" in short
    assert "not value sources" in short
    assert "@override_value" in short  # the unproven path is named in the summary


def test_case_tree_shows_both_alternatives_and_condition():
    t = tree(render_markdown(sample_trace().to_dict()))
    assert "[value · WHEN s.input_a IS NOT NULL THEN] staging.demo.source.input_a  ■ stop: boundary" in t
    assert "[value · ELSE] staging.demo.source.input_b  ■ stop: boundary" in t
    assert "[condition · WHEN s.input_a IS NOT NULL] staging.demo.source.input_a" in t
    assert "#work.chosen_value (in reporting.demo.load_output)" in t
    assert "writer 1 of 2:" in t and "writer 2 of 2:" in t
    assert "branch: IF @apply_override = 1" in t
    assert "⚠ gap: dynamic_sql" in t
    assert t.splitlines()[0].startswith("reporting.demo.output.result_value")
    assert "└─" in t and "├─" in t and "│" in t


def test_row_selection_separate_from_value_inputs():
    md = render_markdown(sample_trace().to_dict())
    rows = section(md, "Row-selection context")
    for name in ("staging.demo.source.is_active", "staging.demo.source.id", "staging.demo.lookup_key.id"):
        assert f"`{name}`" in rows
        assert name not in tree(md)
        assert name not in section(md, "Short answer")
    assert "| filter (WHERE) | `staging.demo.source.is_active` | `s.is_active = 1` |" in rows
    assert "| join key |" in rows
    assert "`procedures/load_output.sql:11`" in rows
    # Row-selection columns never appear in the input tables of the detailed steps.
    steps = section(md, "Detailed steps")
    assert "is_active" not in steps
    assert "is_active" not in md.split("```mermaid")[1].split("```")[0]


def test_detailed_steps_order_and_evidence():
    md = render_markdown(sample_trace().to_dict())
    steps = section(md, "Detailed steps")
    headings = re.findall(r"^### (S\d+)\. `([^`]+)`", steps, re.M)
    assert [h[0] for h in headings] == ["S1", "S2", "S3"]
    # Target writers first (by order), then the upstream #work step.
    assert headings[0][1] == headings[1][1] == "reporting.demo.output.result_value"
    assert headings[2][1].startswith("#work.chosen_value")
    assert "`procedures/load_output.sql:14-16`" in steps
    assert "- **Mapping method:** projection_alias" in steps
    assert "| 3 | `staging.demo.source.input_b` | value | `ELSE` | `s.input_b` | verified ■ boundary |" in steps
    # Anchors used by links exist.
    for anchor in re.findall(r"\]\(#([^)]+)\)", md):
        assert f'id="{anchor}"' in md, anchor


def test_gaps_section_lists_gaps_and_json():
    md = render_markdown(sample_trace().to_dict(), SAMPLE_JSON_NAME)
    gaps = section(md, "Gaps and next evidence")
    assert "### dynamic_sql (1)" in gaps
    assert "EXEC sp_executesql" in gaps
    assert "`procedures/apply_overrides.sql:9`" in gaps
    assert "Where proof ends" in gaps
    assert "- Proof is missing at:\n  - `@override_value (in reporting.demo.apply_overrides)` — dynamic_sql (value path)" in gaps
    # Every stop and missing-proof item is its own bullet, never a run-on list.
    proof = gaps.split("### Where proof ends", 1)[1].split("\n\n", 2)[1]
    assert all(line.startswith(("- ", "  - ", "    - ")) for line in proof.splitlines())
    assert "    - Next evidence (dynamic_sql):" in proof and "    - Where: " in proof
    assert "  - `staging.demo.source.input_a`" in proof and "  - `staging.demo.source.input_b`" in proof
    assert f"[{SAMPLE_JSON_NAME}]({SAMPLE_JSON_NAME})" in gaps
    assert "### Steps that are not verified" in gaps


def test_deterministic_and_order_independent():
    data = sample_trace().to_dict()
    first = render_markdown(data, SAMPLE_JSON_NAME)
    assert first == render_markdown(json.loads(json.dumps(data)), SAMPLE_JSON_NAME)
    shuffled = json.loads(json.dumps(data))
    rng = random.Random(7)
    for key in ("nodes", "steps", "stops", "gaps"):
        rng.shuffle(shuffled[key])
    for step in shuffled["steps"]:
        rng.shuffle(step["row_selection"])
        rng.shuffle(step["evidence"])
    assert render_markdown(shuffled, SAMPLE_JSON_NAME) == first


def test_row_selection_traced_upstream_stays_out_of_tree():
    trace = sample_trace()
    is_active = col("staging", "demo", "source", "is_active")
    raw_flag = col("staging", "demo", "raw", "flag")
    trace.add_node(raw_flag)
    trace.steps.append(Step(target=is_active, writer_kind="update", expression="flag", operation="rename",
                            inputs=[Input(raw_flag, VALUE, "flag")], evidence=[ev("x.sql", 1, "UPDATE ...")]))
    md = render_markdown(trace.to_dict())
    assert "raw.flag" not in tree(md)
    assert "reached only through row-selection columns" in section(md, "Value lineage overview")
    assert "(traced: [S4]" in section(md, "Row-selection context")
    assert "### S4. `staging.demo.source.is_active`" in section(md, "Detailed steps")
    assert "staging.demo.raw.flag" not in md.split("```mermaid")[1].split("```")[0]


def test_shared_subtree_printed_once():
    md = render_markdown(chain_trace(shared=True).to_dict())
    t = tree(md)
    assert t.count("db.s.t.c") == 2  # expanded once, referenced once
    assert "db.s.t.c  (see above: " in t
    assert t.count("db.s.t.d") == 1
    assert "■ stop: source_without_writer" in t


def test_cycle_terminates():
    md = render_markdown(chain_trace(cycle=True).to_dict())
    t = tree(md)
    assert "↻ (cycle)" in t
    assert len(t.splitlines()) < 10
    gaps = section(md, "Gaps and next evidence")
    assert "### Cycles" in gaps and "### cycle (1)" in gaps


def test_depth_and_width_caps():
    target = col("db", "s", "t", "c0")
    trace = TraceResult(target=target, requested="db.s.t.c0", boundary=Boundary(), scan={})
    nodes = [col("db", "s", "t", f"c{i}") for i in range(30)]
    for i in range(29):
        trace.steps.append(Step(target=nodes[i], writer_kind="update", expression=f"c{i + 1}",
                                operation="direct", inputs=[Input(nodes[i + 1], VALUE, f"c{i + 1}")],
                                evidence=[ev("x.sql", i + 1, "x")]))
    trace.steps.append(Step(target=nodes[29], writer_kind="update", expression="many", operation="function",
                            inputs=[Input(col("db", "s", "w", f"x{i:02d}"), VALUE, f"x{i}") for i in range(20)],
                            evidence=[ev("x.sql", 99, "x")]))
    md = render_markdown(trace.to_dict())
    t = tree(md)
    assert "more step" in t and "upstream (see Detailed steps)" in t
    # The Mermaid diagram is still drawn for 30 steps, but not beyond 40.
    assert "flowchart RL" in md
    assert len(re.findall(r"^### S\d+\.", section(md, "Detailed steps"), re.M)) == 30


def test_width_cap():
    target = col("db", "s", "t", "c0")
    trace = TraceResult(target=target, requested="db.s.t.c0", boundary=Boundary(), scan={})
    trace.steps.append(Step(target=target, writer_kind="update", expression="f(...)", operation="function",
                            inputs=[Input(col("db", "s", "w", f"x{i:02d}"), VALUE, f"x{i}") for i in range(20)],
                            evidence=[ev("x.sql", 1, "x")]))
    t = tree(render_markdown(trace.to_dict()))
    assert "… 8 more inputs (see S1 in Detailed steps)" in t


def test_no_mermaid_over_limit():
    target = col("db", "s", "t", "c0")
    trace = TraceResult(target=target, requested="db.s.t.c0", boundary=Boundary(), scan={})
    nodes = [col("db", "s", "t", f"c{i}") for i in range(42)]
    for i in range(41):
        trace.steps.append(Step(target=nodes[i], writer_kind="update", expression="x", operation="direct",
                                inputs=[Input(nodes[i + 1], VALUE, "x")], evidence=[ev("x.sql", i + 1, "x")]))
    assert "flowchart RL" not in render_markdown(trace.to_dict())


def test_escaping_pipes_and_backticks():
    target = col("db", "s", "t", "target")
    src = col("db", "s", "t", "src")
    trace = TraceResult(target=target, requested="db.s.t.target", boundary=Boundary(), scan={})
    trace.add_node(src)
    expr = "a || b + `quoted` | c"
    trace.steps.append(Step(target=target, writer_kind="update", expression=expr, operation="string",
                            inputs=[Input(src, VALUE, "x | y", branch="WHEN p = '|' THEN")],
                            row_selection=[RowSelection(src, "filter", "s.v <> '|'", ev("x.sql", 2, "WHERE s.v <> '|'"))],
                            evidence=[ev("x.sql", 1, "SET target = a || b + `quoted` | c")]))
    md = render_markdown(trace.to_dict())
    assert "`` a || b + `quoted` | c ``" in md
    for line in md.splitlines():
        if line.startswith("| ") and not line.startswith("|---"):
            unescaped = re.findall(r"(?<!\\)\|", line)
            assert len(unescaped) in (3, 6, 7), line  # field/value, row-selection and input tables
    assert "`s.v <> '\\|'`" in section(md, "Row-selection context")


def test_empty_trace_source_target():
    target = col("db", "s", "t", "raw")
    trace = TraceResult(target=target, requested="db.s.t.raw", boundary=Boundary(), scan={"roots": ["r"]})
    trace.stops.append(Stop(target, "source_without_writer"))
    md = render_markdown(trace.to_dict())
    assert "**Result: Complete within the selected sources" in md
    assert "No mapping step leads into `db.s.t.raw`" in section(md, "Short answer")
    assert "No mapping steps" in section(md, "Detailed steps")
    assert "No row-selection dependencies" in section(md, "Row-selection context")
    assert "No gaps were recorded." in section(md, "Gaps and next evidence")


def test_empty_trace_unresolved_target():
    target = col("db", "s", "t", "missing")
    trace = TraceResult(target=target, requested="db.s.t.missing", boundary=Boundary(), scan={},
                        target_status="unresolved")
    gid = trace.add_gap(Gap("column_not_written", "No statement writes db.s.t.missing", node=target))
    trace.target_gaps.append(gid)
    md = render_markdown(trace.to_dict())
    assert "**Result: Incomplete — 1 value-path gap**" in md
    assert "**unresolved**" in section(md, "Short answer")
    assert "### column_not_written (1)" in md
    assert len(re.findall(r"^## \d\. ", render_markdown({}), re.M)) == 6  # even an empty dict renders all sections


def test_cli_regenerates_page(tmp_path):
    data = sample_trace().to_dict()
    json_path = tmp_path / "out" / "x.lineage.json"
    json_path.parent.mkdir()
    json_path.write_text(json.dumps(data), encoding="utf-8")
    assert main([str(json_path)]) == 0
    md = (tmp_path / "out" / "x.lineage.md").read_text(encoding="utf-8")
    assert md == render_markdown(data, "x.lineage.json")
    assert main([str(json_path), "-o", str(tmp_path / "page.md")]) == 0
    assert "(out/x.lineage.json)" in (tmp_path / "page.md").read_text(encoding="utf-8")
    assert main([str(tmp_path / "nope.json")]) == 2
    proc = subprocess.run([sys.executable, "-m", "column_lineage.render_markdown", str(json_path), "-o", "-"],
                          capture_output=True, text=True, encoding="utf-8", cwd=ROOT, env={"PYTHONPATH": str(ROOT / "src")})
    assert proc.returncode == 0 and "## 6. Gaps and next evidence" in proc.stdout


def test_sample_document_is_current():
    expected = render_markdown(sample_trace().to_dict(), SAMPLE_JSON_NAME)
    assert SAMPLE_MD.read_text(encoding="utf-8") == expected, \
        "regenerate with: .venv/bin/python tests/test_column_markdown.py --write-sample"


if __name__ == "__main__" and "--write-sample" in sys.argv:
    data = sample_trace().to_dict()
    SAMPLE_MD.parent.mkdir(parents=True, exist_ok=True)
    SAMPLE_MD.write_text(render_markdown(data, SAMPLE_JSON_NAME), encoding="utf-8")
    print(SAMPLE_MD)
