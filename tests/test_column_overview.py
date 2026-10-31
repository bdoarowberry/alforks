"""Tests for the reader-facing overview page, the route index and the business glossary
(src/column_lineage/render_overview.py), plus the detailed page's size on many writers.

``python tests/test_column_overview.py --write-sample`` regenerates
``docs/examples/sample-overview.md`` from ``test_column_markdown.sample_trace()``.
"""
import importlib.util
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from column_helpers import cli_args, fixture, run_cli, stem_for  # noqa: E402
from test_column_markdown import col, ev, sample_trace, temp_col  # noqa: E402

from column_lineage.model import (  # noqa: E402
    ASSERTED, CONDITION, INFERRED, VALUE, Boundary, Evidence, Gap, Input, Node, RowSelection, Step, Stop, TraceResult,
)
from column_lineage.render_markdown import render_markdown  # noqa: E402
from column_lineage.render_overview import (  # noqa: E402
    Analysis, build_index, glossary_entry, load_glossary, main, render_index, render_overview,
)

SAMPLE_OVERVIEW = ROOT / "docs" / "examples" / "sample-overview.md"
MAX_LINES = 150


def section(md: str, title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", md, re.S | re.M)
    assert match, f"missing section {title}"
    return match.group(1)


def run(root, columns, out, *extra):
    assert run_cli(cli_args(root, columns, out, *extra)) == 0


def overview(out: Path, column: str) -> str:
    return (out / f"{stem_for(column)}.overview.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- builders

def big_trace(mids: int = 40, sources: int = 12) -> TraceResult:
    """target <- 40 alternative writers (IF branches, one temp table each) <- CASE over 12 sources each,
    row selection on 40 objects, dynamic SQL gaps on every fifth branch."""
    target = col("db", "s", "out", "value")
    trace = TraceResult(target=target, requested="db.s.out.value", boundary=Boundary(), scan={"roots": ["r"]})
    trace.add_node(target)
    for i in range(mids):
        proc = f"s.load_{i:02d}"
        mid = temp_col(f"p{i}.sql::{proc}", f"#mid_{i:02d}", "v")
        flag = col("db", "s", f"filter_{i:02d}", "flag")
        trace.steps.append(Step(target=target, writer_kind="insert_select", expression="m.v", operation="rename",
                                mapping_method="target_list", procedure=proc, branch_context=[f"IF @mode = {i}"],
                                inputs=[Input(mid, VALUE, "m.v")],
                                row_selection=[RowSelection(flag, "filter", f"f.flag = {i}", ev(f"p{i}.sql", 9, "WHERE"))],
                                evidence=[ev(f"p{i}.sql", 10, "INSERT INTO s.out ...")]))
        inputs = []
        for j in range(sources):
            src = col("db", "land", f"src_{i % 7}_{j // 4}", f"amount_{j}")
            inputs.append(Input(src, CONDITION, f"x.amount_{j}", branch=f"WHEN x.kind = {j}"))
            inputs.append(Input(src, VALUE, f"x.amount_{j}", branch=f"WHEN x.kind = {j} THEN"))
            if not any(s.node.id == src.id for s in trace.stops):
                trace.stops.append(Stop(src, "source_without_writer"))
            trace.add_node(src)
        step = Step(target=mid, writer_kind="select_into", expression="CASE ... END", operation="case",
                    mapping_method="projection_alias", procedure=proc, inputs=inputs,
                    evidence=[ev(f"p{i}.sql", 5, "SELECT CASE ... END AS v INTO #mid")])
        if i % 5 == 0:
            var = Node("variable", "@dyn", scope=f"p{i}.sql::{proc}")
            gid = trace.add_gap(Gap("dynamic_sql", "built at run time", node=var, evidence=[ev(f"p{i}.sql", 3, "EXEC")]))
            step.inputs.append(Input(var, VALUE, "@dyn", status="unresolved", gap=gid))
            step.status, step.gaps = "partial", [gid]
        trace.steps.append(step)
        for node in (mid, flag):
            trace.add_node(node)
    return trace


def proof_trace() -> TraceResult:
    """target <- a.x (inferred, positional) and <- b.y (asserted); both end with no writer."""
    target = col("db", "s", "t", "c")
    a, b = col("db", "s", "a", "x"), col("db", "s", "b", "y")
    trace = TraceResult(target=target, requested="db.s.t.c", boundary=Boundary(), scan={})
    for node in (target, a, b):
        trace.add_node(node)
    trace.steps.append(Step(target=target, writer_kind="insert_select", expression="x", operation="direct",
                            mapping_method="positional_schema", status=INFERRED, procedure="s.p1",
                            inputs=[Input(a, VALUE, "x", status=INFERRED)], evidence=[ev("p1.sql", 3, "INSERT ...")]))
    assertion_ev = Evidence("assertions.json", 1, 1, "asserted: b.y -> t.c", role="assertion")
    trace.steps.append(Step(target=target, writer_kind="assertion", expression="y", operation="direct",
                            mapping_method="assertion", status=ASSERTED, inputs=[Input(b, VALUE, "y", status=ASSERTED)],
                            evidence=[assertion_ev]))
    trace.assertions_used.append("a1")
    trace.stops += [Stop(a, "source_without_writer"), Stop(b, "source_without_writer")]
    return trace


def table_rows(md: str, title: str) -> list[list[str]]:
    """Cells of every body row of the tables in a section."""
    rows = []
    for line in section(md, title).splitlines():
        if line.startswith("| ") and not line.startswith("|---"):
            rows.append([c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]])
    return rows[1:]


def end_rows(md: str) -> dict[str, str]:
    """table.column (or source.column) -> whole row of the Sources at a glance table."""
    rows = {}
    for line in section(md, "Sources at a glance").splitlines():
        if line.startswith("| ") and not line.startswith("| Kind") and not line.startswith("|---"):
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
            rows[f"{cells[3]}.{cells[4]}".replace("`", "")] = line
    return rows


# --------------------------------------------------------------------------- Mermaid validator

MERMAID_SHAPES = {'["': '"]', '[("': '")]', '[/"': '"/]', '(["': '"])', '[["': '"]]', '{{"': '"}}'}
MERMAID_NODE = re.compile(
    r'^  (?P<id>[A-Za-z][A-Za-z0-9_]*)(?P<open>\[\("|\[/"|\(\["|\[\["|\{\{"|\[")(?P<label>[^"]*)'
    r'(?P<close>"\)\]|"/\]|"\]\)|"\]\]|"\}\}|"\])'
    r'(?: (?P<arrow>-->|-\.->|-\.-)(?:\|"(?P<edge>[^"]*)"\|)? (?P<to>[A-Za-z][A-Za-z0-9_]*))?$')
MERMAID_STYLE = re.compile(r"^  (classDef [a-z]+ [a-z0-9:\- ]+|class [A-Za-z0-9,]+ [a-z]+)$")
MERMAID_SAFE = re.compile(r"^[\w .,:'()/?@$*+=%!~\-#|\[\]{}&`‹›]*$")   # characters checked to render raw


def mermaid_block(md: str) -> list[str]:
    lines = md.splitlines()
    start = lines.index("```mermaid")
    return lines[start:lines.index("```", start + 1) + 1]


def check_mermaid(lines: list[str]) -> dict[str, dict]:
    """A small validator for the family diagram: fenced flowchart LR, one statement per line, known shapes,
    balanced quotes, labels made only of characters that render raw (no entity codes), unique ids, every edge into T."""
    assert lines[0] == "```mermaid" and lines[1] == "flowchart LR" and lines[-1] == "```", lines[:2]
    nodes: dict[str, dict] = {}
    for line in lines[2:-1]:
        assert line.count('"') % 2 == 0, line
        if MERMAID_STYLE.match(line):
            continue
        match = MERMAID_NODE.match(line)
        assert match, f"not a valid statement: {line!r}"
        assert MERMAID_SHAPES[match["open"]] == match["close"], line
        for text in (match["label"], match["edge"] or ""):
            assert not re.search(r"#\w+;", text), f"entity code or entity-like text in {text!r}"
            assert MERMAID_SAFE.match(text), f"unsafe label characters in {text!r}"
        assert match["id"] not in nodes, f"duplicate id {match['id']}"
        nodes[match["id"]] = {"shape": match["open"], "label": match["label"], "arrow": match["arrow"],
                              "edge": match["edge"], "to": match["to"]}
    assert "T" in nodes and nodes["T"]["to"] is None
    for node_id, node in nodes.items():
        if node_id != "T":
            assert node["to"] == "T", f"{node_id} must point straight at the target (alternatives are parallel)"
    return nodes


def fixture_columns(root: Path) -> list[str]:
    """Columns written by INSERT column lists in a fixture (persistent tables only)."""
    found = []
    for path in sorted(root.rglob("*.sql")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for table, cols in re.findall(r"INSERT\s+INTO\s+([A-Za-z_][\w.]*)\s*\(([^)]*)\)", text, re.I):
            for column in cols.split(","):
                column = column.strip().strip("[]")
                if re.fullmatch(r"[A-Za-z_]\w*", column) and f"{table}.{column}" not in found:
                    found.append(f"{table}.{column}")
    return found[:6]


OVB = fixture("overview_branches")
OVB_COLUMN = "rpt_db.report.customer_limit.credit_limit"


def run_ovb(out: Path, *extra) -> str:
    run(OVB / "sql", OVB_COLUMN, out, "--package-root", str(OVB / "packages"), *extra)
    return overview(out, OVB_COLUMN)


def first_screen(md: str) -> str:
    return md[:md.index("## Sources at a glance")]


# --------------------------------------------------------------------------- length caps

def test_overview_is_short_on_a_large_trace():
    trace = big_trace()
    data = trace.to_dict()
    from column_lineage.render_markdown import render_markdown
    md = render_overview(data, "big.lineage.json")
    detailed = render_markdown(data, "big.lineage.json")
    assert len(md.splitlines()) < MAX_LINES, len(md.splitlines())
    assert len(md) * 4 < len(detailed)
    routes = section(md, "Routes: how the value arrives")
    # 40 branches x 3 source tables, plus 8 branches ending at a dynamic-SQL variable.
    assert "128 object routes" in routes and "And 126 more routes" in routes and "(big.lineage.md#value-lineage)" in routes
    assert "more end points" in section(md, "Sources at a glance")
    assert "and 36 more objects" in section(md, "Which rows?")
    assert "dynamic SQL" in section(md, "Open points") and "and 5 more" in section(md, "Open points")
    # 968 value routes collapse into a handful of families; the diagram stays small.
    assert "968 routes (488 carry the value, 480 decide between values) in 5 route families" in section(md, "Coverage")
    diagram = mermaid_block(md)
    check_mermaid(diagram)
    assert len([line for line in diagram if line.startswith("  F")]) == 5
    assert len([b for b in section(md, "Main evidence needed").splitlines() if b.startswith("- ")]) <= 3
    # Exact expressions stay on the detailed page.
    assert "CASE ... END" not in md and "INSERT INTO" not in md


def test_corpus_chain_overviews_stay_short(tmp_path):
    spec = importlib.util.spec_from_file_location("generate_corpus", ROOT / "tools" / "generate_corpus.py")
    gen = importlib.util.module_from_spec(spec)
    sys.modules["generate_corpus"] = gen
    spec.loader.exec_module(gen)
    corpus = tmp_path / "corpus"
    manifest = gen.generate(corpus, objects=160, seed=11, chains=12)
    columns = [c["column"] for c in manifest["chains"]]
    extra = [arg for m in manifest["database_map_args"] for arg in ("--database-map", m)]
    out = tmp_path / "out"
    run(corpus, columns, out, "--jobs", "1", *extra)
    sizes = []
    for column in columns:
        page = overview(out, column)
        detailed = (out / f"{stem_for(column)}.lineage.md").read_text(encoding="utf-8")
        assert len(page.splitlines()) < MAX_LINES, column
        sizes.append((len(detailed), len(page)))
    biggest = max(sizes)
    assert biggest[1] < biggest[0]
    index = json.loads((out / "routes-index.json").read_text(encoding="utf-8"))
    assert sorted(c["column"] for c in index["columns"]) == sorted(columns)


# --------------------------------------------------------------------------- never overstate

def test_no_writer_end_never_reads_as_confirmed_source(tmp_path):
    run(fixture("case_choice"), "demo.output.result_value", tmp_path)
    md = overview(tmp_path, "demo.output.result_value")
    answer, coverage = section(md, "Plain answer"), section(md, "Coverage")
    assert answer.strip().startswith("- **Trace stopped at unresolved sources:**")
    assert "where no writer, file producer or caller was found (origin not proved there)" in answer
    assert "it is not an originating system" in coverage
    assert "unresolved origin even when the step reaching it is verified" in coverage
    for row in end_rows(md).values():
        assert "no writer in the selected sources — a stopping point; its origin is not proved" in row
    lowered = md.lower()
    for claim in ("source system:", "originates in", "originates from", "ultimate source", "comes from the source"):
        assert claim not in lowered


def test_external_file_is_a_stopping_point(tmp_path):
    out = tmp_path / "out"
    assert run_cli(cli_args(fixture("ssis/sql"), "demo.stage.amount", out,
                            "--package-root", str(fixture("ssis/basic")))) == 0
    md = overview(out, "demo.stage.amount")
    (row,) = end_rows(md).values()
    assert row.startswith("| file column | — | — | `file connection file_a")
    assert "external file read by a package — a stopping point, not a confirmed source system" in row
    assert "file connection file_a" in section(md, "Routes: how the value arrives")
    assert "file producer unknown" in section(md, "Route families")
    assert "**Who produces file connection `file_a`" in section(md, "Main evidence needed")


def test_inferred_and_asserted_are_never_shown_as_proved():
    md = render_overview(proof_trace().to_dict(), "t.lineage.json")
    evidence = section(md, "Evidence and uncertainty")
    assert "relies on 1 asserted step" in evidence and "not proved from code" in evidence
    assert "**1 inferred**" in evidence and "**1 asserted**" in evidence and "Inferred = bound by a stated rule" in evidence
    assert "assertion ids `a1`" in evidence and "[assertions.json](assertions.json)" in evidence
    assert "never read these as code-proved" in evidence
    assert "steps: 0 verified, 1 inferred, 1 asserted" in section(md, "Coverage")
    # Different evidence status: separate families, and the diagram says so on the edge.
    families = {row[1]: row for row in table_rows(md, "Route families")}
    assert "inferred (rule-based" in families["DB: `db.s.a.x`"][4]
    assert "asserted (recorded in an assertions file" in families["DB: `db.s.b.y`"][4]
    edges = "\n".join(mermaid_block(md))
    assert '- inferred"| T' in edges and 'asserted mapping - upstream writer unknown - asserted"| T' in edges
    rows = end_rows(md)
    assert "inferred (rule-based, not proved by explicit code)" in rows["a.x"] and "verified" not in rows["a.x"]
    assert "asserted (recorded in an assertions file, not proved from code)" in rows["b.y"] and "verified" not in rows["b.y"]
    routes = section(md, "Routes: how the value arrives")
    assert "verified" not in routes
    assert "assertions file assertions.json (asserted, not code)" in routes


def test_row_selection_is_kept_apart_from_values(tmp_path):
    run(fixture("row_selection"), "demo.output.result_value", tmp_path)
    md = overview(tmp_path, "demo.output.result_value")
    rows = section(md, "Which rows?")
    assert "which rows" in rows and "do not compute the value" in rows
    assert "filter `s.is_active = 1`" in rows
    assert "join key" in rows and "join condition" in rows and "?.demo.lookup" in rows
    routes, ends = section(md, "Routes: how the value arrives"), section(md, "Sources at a glance")
    for name in ("is_active", "region_code", "lookup"):
        assert name not in routes and name not in ends
        assert name not in section(md, "Route families") and name not in section(md, "Plain answer")
    assert list(end_rows(md)) == ["source.input_a"]


def test_branch_labels_for_case_and_if_alternatives(tmp_path):
    run(fixture("case_choice"), ["demo.output.result_value", "demo.output.alt_value"], tmp_path)
    routes = section(overview(tmp_path, "demo.output.result_value"), "Routes: how the value arrives")
    ends = next(line for line in routes.splitlines() if line.startswith("- Ends:"))
    assert re.search(r"input_a` \(via `ELSE`\)", ends)
    assert re.search(r"input_b` \(via `WHEN s\.use_b = 1 THEN`\)", ends)
    assert re.search(r"use_b` \(decides between values; via `WHEN s\.use_b = 1`\)", ends)
    alt = section(overview(tmp_path, "demo.output.alt_value"), "Routes: how the value arrives")
    assert "constant `NULL`" in alt and "`ELSE`" in alt

    # IF / ELSE writers through the same objects keep their own branch per end.
    target, a, b = col("db", "s", "t", "c"), col("db", "s", "src", "a"), col("db", "s", "src", "b")
    trace = TraceResult(target=target, requested="db.s.t.c", boundary=Boundary(), scan={})
    for node, branch, line in ((a, "IF @flag = 1", 3), (b, "ELSE (of IF @flag = 1)", 5)):
        trace.add_node(node)
        trace.steps.append(Step(target=target, writer_kind="update", expression=node.name, operation="direct",
                                procedure="s.p", branch_context=[branch], inputs=[Input(node, VALUE, node.name)],
                                evidence=[ev("p.sql", line, "UPDATE ...")]))
        trace.stops.append(Stop(node, "source_without_writer"))
    routes = section(render_overview(trace.to_dict()), "Routes: how the value arrives")
    assert "`db.s.src.a` (via `IF @flag = 1`)" in routes
    assert "`db.s.src.b` (via `ELSE (of IF @flag = 1)`)" in routes


# --------------------------------------------------------------------------- glossary

def test_glossary_approved_proposed_and_missing(tmp_path):
    glossary = tmp_path / "glossary.json"
    glossary.write_text(json.dumps({"columns": [
        {"column": "demo.output.result_value", "definition": "Amount chosen for the report", "status": "approved",
         "source": "Data owner sign-off 2026-01"},
        {"column": "*.demo.output.alt_*", "definition": "Optional alternative amount", "status": "proposed",
         "source": "Analyst draft"},
    ]}), encoding="utf-8")
    run(fixture("case_choice"), ["demo.output.result_value", "demo.output.alt_value"], tmp_path / "out",
        "--business-glossary", str(glossary))
    approved = section(overview(tmp_path / "out", "demo.output.result_value"), "Field and definition")
    assert "- **Definition (approved):** Amount chosen for the report. Source: Data owner sign-off" in approved
    assert "not approved" not in approved.lower() and "proposed" not in approved.lower()
    proposed = section(overview(tmp_path / "out", "demo.output.alt_value"), "Field and definition")
    assert "- **Definition not approved.**" in proposed
    assert ("- *Proposed interpretation* (glossary status: proposed, not approved; source: Analyst draft): Optional "
            "alternative amount.") in proposed

    run(fixture("rename_temp"), "demo.output.result_value", tmp_path / "none")
    missing = section(overview(tmp_path / "none", "demo.output.result_value"), "Field and definition")
    assert "- **Definition not approved.** No glossary entry; the tool never infers business meaning from names" in missing
    assert "Proposed interpretation" not in missing

    # An unknown database is matched only by '*', never by a named database.
    entries = load_glossary(glossary)
    data = {"target": {"name": "result_value", "object": {"database": None, "schema": "demo", "name": "output"}}}
    assert glossary_entry(entries, data)[0]["status"] == "approved"
    named = [dict(entries[0], column="report_db.demo.output.result_value")]
    assert glossary_entry(named, data) == (None, 0)

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"columns": [{"column": "x.y", "definition": "d", "status": "final"}]}), encoding="utf-8")
    assert run_cli(cli_args(fixture("case_choice"), "demo.output.result_value", tmp_path / "bad",
                            "--business-glossary", str(bad))) == 2


# --------------------------------------------------------------------------- route index

def test_index_groups_shared_signatures_and_lists_them_per_column(tmp_path):
    columns = ["demo.output.result_value", "demo.output.alt_value"]
    run(fixture("case_choice"), columns, tmp_path)
    run(fixture("cross_database"), "report_db.demo.output.result_value", tmp_path / "other",
        "--database-map", "report=report_db")
    index = json.loads((tmp_path / "routes-index.json").read_text(encoding="utf-8"))
    rows = {c["column"]: c for c in index["columns"]}
    assert set(rows) == set(columns)
    # Constants are left out of terminal sources, so both columns end at the same object set.
    assert rows[columns[0]]["source_signature"] == rows[columns[1]]["source_signature"]
    assert rows[columns[0]]["writer_signature"] == rows[columns[1]]["writer_signature"]
    (group,) = [g for g in index["source_signatures"] if g["id"] == rows[columns[0]]["source_signature"]]
    assert sorted(group["columns"]) == sorted(columns)
    assert [index["objects"][k] for k in group["objects"]] == ["?.demo.source"]
    for row in rows.values():
        assert row["route_signatures"] and row["overview"] == f"{stem_for(row['column'])}.overview.md"
    assert "does not mean identical expressions, branch selection or business meaning" in index["note"]
    md = (tmp_path / "routes-index.md").read_text(encoding="utf-8")
    assert "It does **not** mean identical expressions, branch selection or business meaning" in md
    assert f"### {group['id']} — 2 columns" in md
    assert "[demo.output.alt_value](demo.output.alt_value.overview.md)" in md
    page = overview(tmp_path, columns[0])
    assert f"`{group['id']}` (shared with 1 other column)" in page
    # Different object sets get different signatures; ids are stable across runs.
    other = json.loads((tmp_path / "other" / "routes-index.json").read_text(encoding="utf-8"))
    assert other["columns"][0]["source_signature"] != group["id"]
    assert build_index([{"data": json.loads((tmp_path / f"{stem_for(c)}.lineage.json").read_text())}
                        for c in columns])["source_signatures"][0]["id"] == group["id"]


def test_index_keeps_every_column_complete(tmp_path):
    """A shared group never replaces a column's own contributors: each column's JSON is untouched."""
    run(fixture("case_choice"), ["demo.output.result_value", "demo.output.alt_value"], tmp_path)
    alt = json.loads((tmp_path / "demo.output.alt_value.lineage.json").read_text(encoding="utf-8"))
    assert any(s["reason"] == "constant" for s in alt["stops"])
    assert "| constant | — | — | — | `NULL` |" in section(overview(tmp_path, "demo.output.alt_value"), "Sources at a glance")


# --------------------------------------------------------------------------- JSON only, CLI options

def test_renders_from_json_only(tmp_path):
    columns = ["demo.output.result_value", "demo.output.alt_value"]
    run(fixture("case_choice"), columns, tmp_path / "run")
    copy = tmp_path / "copy"
    copy.mkdir()
    for column in columns:
        name = f"{stem_for(column)}.lineage.json"
        (copy / name).write_bytes((tmp_path / "run" / name).read_bytes())
    assert main([str(copy / f"{stem_for(c)}.lineage.json") for c in columns] + ["--index-out", str(copy)]) == 0
    for column in columns:
        assert overview(copy, column) == overview(tmp_path / "run", column)
    for name in ("routes-index.md", "routes-index.json"):
        assert (copy / name).read_text(encoding="utf-8") == (tmp_path / "run" / name).read_text(encoding="utf-8")
    # Pages in another folder link back to the JSON and the detailed page.
    assert main([str(copy / f"{stem_for(columns[0])}.lineage.json"), "--out-dir", str(tmp_path / "pages")]) == 0
    page = (tmp_path / "pages" / f"{stem_for(columns[0])}.overview.md").read_text(encoding="utf-8")
    assert "(../copy/demo.output.result_value.lineage.json)" in page
    assert "(../copy/demo.output.result_value.lineage.md#gaps)" in page
    assert main([str(tmp_path / "missing.json")]) == 2


def test_no_overview_and_gap_summary_counts(tmp_path):
    run(fixture("case_choice"), ["demo.output.result_value", "demo.output.alt_value"], tmp_path, "--no-overview",
        "--gap-summary")
    assert not list(tmp_path.glob("*.overview.md"))
    md = (tmp_path / "routes-index.md").read_text(encoding="utf-8")
    assert "(demo.output.alt_value.lineage.md)" in md
    text = (tmp_path / "gap-summary.json").read_text(encoding="utf-8")
    counts = json.loads(text)["route_index"]
    assert counts["columns"] == 2 and counts["source_signatures"] == 1 and counts["largest_source_groups"] == [2]
    for name in ("demo.", "load_case", "result_value", "use_b", "src-", "wr-"):
        assert name not in text.lower(), name


def test_deterministic_and_order_independent():
    data = big_trace(mids=8, sources=4).to_dict()
    first = render_overview(data, "x.lineage.json")
    shuffled = json.loads(json.dumps(data))
    random.Random(3).shuffle(shuffled["steps"])
    random.Random(4).shuffle(shuffled["nodes"])
    random.Random(5).shuffle(shuffled["stops"])
    assert render_overview(shuffled, "x.lineage.json") == first


def test_empty_and_unresolved_targets_render():
    target = col("db", "s", "t", "raw")
    trace = TraceResult(target=target, requested="db.s.t.raw", boundary=Boundary(), scan={})
    trace.stops.append(Stop(target, "source_without_writer"))
    md = render_overview(trace.to_dict())
    assert "it is itself an end point (no writer in the selected sources" in section(md, "Plain answer")
    assert "Definition not approved" in render_overview({})


def test_sample_overview_is_current():
    expected = render_overview(sample_trace().to_dict(), "sample-trace.lineage.json", detailed_page="sample-trace.md")
    assert SAMPLE_OVERVIEW.read_text(encoding="utf-8") == expected, \
        "regenerate with: .venv/bin/python tests/test_column_overview.py --write-sample"


def test_overview_short_with_thousands_of_writers(tmp_path):
    n = 1500
    sql = tmp_path / "sql"
    sql.mkdir()
    rows = "\n".join(f"INSERT INTO demo.target (id, val) VALUES ({i}, 'v{i}');" for i in range(n))
    (sql / "load.sql").write_text("CREATE TABLE demo.target (id int, val varchar(10));\nGO\n"
                                  f"CREATE PROCEDURE demo.load_rows AS\nBEGIN\n{rows}\nEND\nGO\n", encoding="utf-8")
    run(sql, "demo.demo.target.val", tmp_path / "out", "--default-database", "demo", "--jobs", "1")
    page = overview(tmp_path / "out", "demo.demo.target.val")
    assert len(page.splitlines()) < MAX_LINES and f"{n} constants such as `'v0'` and `'v1'` (direct; [F1]" in section(page, "Plain answer")
    assert f"{n} routes ({n} carry the value) in 1 route family" in section(page, "Coverage")
    assert "And 1492 more end points (constant: 1492)" in page


if __name__ == "__main__" and "--write-sample" in sys.argv:
    SAMPLE_OVERVIEW.write_text(render_overview(sample_trace().to_dict(), "sample-trace.lineage.json",
                                               detailed_page="sample-trace.md"), encoding="utf-8")
    print(SAMPLE_OVERVIEW)


def test_loop_ends_are_unresolved_origins(tmp_path):
    """Loop ends are counted as unresolved origins, labelled in the sources table and linked to the cycles."""
    run(fixture("cycle"), "demo.beta.val", tmp_path)
    md = overview(tmp_path, "demo.beta.val")
    assert "2 unresolved origins (1 with no writer in the selected sources, 1 loop)" in section(md, "Coverage")
    assert "1 route stops at a loop" in section(md, "Plain answer")
    assert md.index("## Sources at a glance") < md.index("## Routes")
    endpoints = section(md, "Sources at a glance")
    assert "loop: values here feed each other" in endpoints and "#gaps" in endpoints
    assert '{{"F3 Unresolved origin' in "\n".join(mermaid_block(md))


# --------------------------------------------------------------------------- first screen (multi-branch fixture)

def test_first_screen_answers_what_feeds_where_proof_stops_and_what_is_assumed(tmp_path):
    md = run_ovb(tmp_path)
    titles = re.findall(r"^## (.+)$", md, re.M)
    assert titles[:9] == ["Field and definition", "Plain answer", "Coverage", "Main evidence needed", "Route families",
                          "Sources at a glance", "What changes the value", "Which rows?", "Evidence and uncertainty"]
    field = section(md, "Field and definition")
    assert ("`rpt_db.report.customer_limit.credit_limit` — database `rpt_db`, schema `report`, table "
            "`customer_limit`, column `credit_limit`") in field
    assert "**Definition not approved.**" in field
    # What feeds it: every source family, with the rules that change the value.
    answer = section(md, "Plain answer")
    lines = [l for l in answer.strip().splitlines() if l.strip() and not l.startswith("    ")]
    assert len(lines) <= 3 and all(l.startswith("- **") for l in lines)   # three short bullets
    assert lines[0].startswith("- **Trace stopped at unresolved sources:** of 6 routes carrying the value, 5 end "
                               "where no writer")
    assert "and none reaches the requested boundary" in lines[0]
    assert lines[1] == "- **Sources:**"
    assert "\n    - `core_db.crm` (primary, fallback, later UPDATE override; [F" in answer   # one line per group
    for family in ("the constant `0` (CASE branch; [F", "file connection `limit_feed` read by package "
                   "`load_file_limit.dtsx` (package mapping; [F", "`partner_db.stage` (direct; [F"):
        assert family in answer
    assert "a COALESCE/ISNULL fallback" in lines[2] and "a later UPDATE overwrites the rows it matches" in lines[2]
    assert "it can set `0`" in lines[2]
    assert "core_db.crm.account" not in lines[2]                        # columns are named once, under Sources
    coverage = section(md, "Coverage")
    assert ("7 routes (6 carry the value, 1 decides between values) in 7 route families · distinct end points "
            "(columns where routes stop): 0 at the requested boundary · 0 declared terminals · 6 unresolved "
            "origins (5 with no writer in the selected sources, 1 external file) · 1 constant · steps: 7 verified, "
            "0 inferred, 0 asserted") in coverage
    assert "**Trace stopped at unresolved sources**" in coverage
    # What is assumed: nothing is asserted or inferred here, and the page says so.
    evidence = section(md, "Evidence and uncertainty")
    assert "**7 verified**, **0 inferred**, **0 asserted**" in evidence and "none used" in evidence
    # Main evidence: at most three, the most routes first.
    needed = section(md, "Main evidence needed").strip().splitlines()
    assert len(needed) == 3 and all(line.startswith("- **") for line in needed)
    assert needed[0].startswith("- **Who writes `core_db.crm.account`, `core_db.crm.customer`, and 1 more** (no writer "
                                "in the selected sources; 4 routes)")
    assert "file producer unknown; 1 route" in needed[1] or "file producer unknown; 1 route" in needed[2]
    # The first screen never presents a stop as an origin, and never infers meaning.
    lowered = first_screen(md).lower()
    for claim in ("source system:", "originates in", "originates from", "ultimate source", "comes from the source",
                  "credit limit of", "means "):
        assert claim not in lowered
    # Value rules are kept apart from row selection.
    assert "is_active" not in first_screen(md) and "batch_ok" not in first_screen(md)
    rows = section(md, "Which rows?")
    assert "filter `c.is_active = 1`" in rows and "filter `p.batch_ok = 1`" in rows and "filter `adj.approved = 1`" in rows
    changes = section(md, "What changes the value")
    assert "**CASE/IIF choice**" in changes and "**Fallback (COALESCE/ISNULL)**" in changes
    assert "**Override** by UPDATE in `report.load_customer_limit`" in changes and "runs after the other writers" in changes
    assert "**Package transformation** (type conversion) in package `load_file_limit.dtsx`" in changes
    assert "is_active" not in changes
    assert len(md.splitlines()) < MAX_LINES


def test_family_diagram_is_valid_small_and_parallel(tmp_path):
    md = run_ovb(tmp_path / "plain")
    nodes = check_mermaid(mermaid_block(md))
    assert len(nodes) == 8   # target + 7 families
    shapes = {node_id: n["shape"] for node_id, n in nodes.items()}
    labels = {n["label"].split(" ", 1)[1].split(":")[0]: n for node_id, n in nodes.items() if node_id != "T"}
    assert labels["DB column"]["shape"] == '[("' and labels["File column"]["shape"] == '[/"'
    assert labels["Constant"]["shape"] == '(["'
    by_end = {n["label"].split(": ", 1)[1]: n for node_id, n in nodes.items() if node_id != "T"}
    assert by_end["core_db.crm.customer.credit_limit"]["edge"] == "CASE default, primary - upstream writer unknown"
    assert by_end["core_db.crm.account.default_limit"]["edge"] == "CASE default, fallback 2 - upstream writer unknown"
    assert by_end["core_db.crm.limit_adjustment.new_limit"]["edge"] == "override (later UPDATE) - upstream writer unknown"
    assert by_end["partner_db.stage.partner_extract.credit_limit"]["edge"] == "direct copy - upstream writer unknown"
    assert by_end["limit_feed.limit_amount (in packages/load_file_limit.dtsx)"]["edge"] == \
        "package mapping, type conversion - file producer unknown"
    assert by_end["0"]["edge"] == "CASE branch - constant"
    condition = by_end["core_db.crm.customer.is_closed"]
    assert condition["arrow"] == "-.->" and condition["edge"].startswith("decides between values")
    assert all(n["arrow"] == "-->" for k, n in by_end.items() if k != "core_db.crm.customer.is_closed")
    assert shapes["T"] == '["'

    # A declared terminal gets its own shape and is labelled as not an origin.
    md = run_ovb(tmp_path / "declared", "--declared-terminals", str(OVB / "terminals.txt"))
    nodes = check_mermaid(mermaid_block(md))
    (declared,) = [n for n in nodes.values() if n["shape"] == '[["']
    assert declared["label"].endswith("Declared terminal, not an origin: partner_db.stage.partner_extract.credit_limit")
    assert declared["edge"] == "direct copy - declared terminal, upstream not proved"
    assert "1 declared terminal · 5 unresolved origins" in section(md, "Coverage")
    assert "1 route ends at a terminal declared for this run (upstream not traced)" in section(md, "Plain answer")
    assert "partner_extract" not in section(md, "Main evidence needed")
    (row,) = [r for r in end_rows(md).values() if "partner_extract" in r]
    assert "declared terminal for this run (loaded by an external partner feed)" in row
    assert "how it is populated upstream is not proved" in row

    # Unresolved origins (open points) get the hexagon; too many families collapse into a "more" node.
    md = render_overview(sample_trace().to_dict(), "s.lineage.json")
    nodes = check_mermaid(mermaid_block(md))
    (hexagon,) = [n for n in nodes.values() if n["shape"] == '{{"']
    assert "Unresolved origin: @override_value" in hexagon["label"] and "open point: dynamic SQL" in hexagon["edge"]
    many = big_trace(mids=30, sources=2)
    for i in range(30):   # every source schema its own family
        other = many.add_node(col("db", f"other_{i}", "x", "y"))
        many.stops.append(Stop(many.nodes[other], "boundary"))
        many.steps.append(Step(target=many.target, writer_kind="insert_select", expression="y", operation="direct",
                               procedure=f"s.extra_{i}", inputs=[Input(many.nodes[other], VALUE, "y")],
                               evidence=[ev(f"x{i}.sql", 1, "INSERT ...")]))
    md = render_overview(many.to_dict(), "m.lineage.json")
    nodes = check_mermaid(mermaid_block(md))
    assert len(nodes) == 12 and "MORE" in nodes and nodes["MORE"]["arrow"] == "-.-"
    assert len(md.splitlines()) < MAX_LINES
    assert nodes["MORE"]["label"].startswith("Summary, not a source:") and "(m.lineage.md#route-families)" in section(md, "Route families")


def test_sources_table_separates_databases_files_and_constants(tmp_path):
    md = run_ovb(tmp_path)
    rows = {(r[3], r[4]): r for r in table_rows(md, "Sources at a glance")}
    assert rows[("`customer`", "`credit_limit`")][:3] == ["DB column", "`core_db`", "`crm`"]
    assert rows[("`partner_extract`", "`credit_limit`")][:3] == ["DB column", "`partner_db`", "`stage`"]
    file_row = rows[("`file connection limit_feed (package packages/load_file_limit.dtsx)`", "`limit_amount`")]
    assert file_row[:3] == ["file column", "—", "—"]
    assert rows[("—", "`0`")][:3] == ["constant", "—", "—"]
    for row in rows.values():
        assert re.fullmatch(r"(\[F\d+\]\([^)]*#fam-[0-9a-f]{10}\)(, )?)+", row[5]), row[5]
        assert "#step-" in row[7]
    # The database column never holds a file connection or a constant.
    assert all("file" not in r[1] and "constant" not in r[1] for r in rows.values())


def _family_trace() -> TraceResult:
    """Five inputs from one schema: two plain copies (one family), one inferred, one via COALESCE argument 2,
    one at the requested boundary."""
    target = col("db", "s", "t", "c")
    trace = TraceResult(target=target, requested="db.s.t.c", boundary=Boundary(databases=("db",)), scan={})
    trace.add_node(target)
    cases = [("a", "x", VALUE, None, "verified", "source_without_writer"),
             ("b", "x", VALUE, None, "verified", "source_without_writer"),
             ("c", "x", VALUE, None, INFERRED, "source_without_writer"),
             ("d", "x", VALUE, "COALESCE argument 2", "verified", "source_without_writer"),
             ("e", "x", VALUE, None, "verified", "boundary")]
    for i, (table, column, role, branch, status, reason) in enumerate(cases):
        node = col("db", "src", table, column)
        trace.add_node(node)
        trace.steps.append(Step(target=target, writer_kind="insert_select", expression=column, operation="direct",
                                procedure=f"s.p{i}", status=status, inputs=[Input(node, role, column, branch, status)],
                                evidence=[ev(f"p{i}.sql", 3, "INSERT ...")]))
        trace.stops.append(Stop(node, reason))
    return trace


def test_families_are_never_merged_across_rule_stop_or_status():
    a = Analysis(_family_trace().to_dict())
    fams = {(f["value_rule_text"], f["stop"], f["status"]): f for f in a.families}
    assert len(fams) == 4
    plain = fams[("direct copy", "source_without_writer", "verified")]
    assert plain["route_count"] == 2 and plain["ends"] == ["db.src.a.x", "db.src.b.x"]
    assert fams[("direct copy", "source_without_writer", "inferred")]["ends"] == ["db.src.c.x"]
    assert fams[("fallback 2", "source_without_writer", "verified")]["ends"] == ["db.src.d.x"]
    assert fams[("direct copy", "boundary", "verified")]["ends"] == ["db.src.e.x"]
    assert len({f["id"] for f in a.families}) == 4 and all(re.fullmatch(r"fam-[0-9a-f]{10}", f["id"]) for f in a.families)
    # Ids are stable: the same pattern in another column of another run keeps its id.
    other = _family_trace()
    other.target = col("db", "s", "t2", "c2")
    for step in other.steps:
        step.target = other.target
    assert {f["id"] for f in Analysis(other.to_dict()).families} == {f["id"] for f in a.families}
    # An open point on the way makes the family partial, never verified.
    gapped = _family_trace()
    gid = gapped.add_gap(Gap("unresolved_variable", "unknown", node=None, evidence=[ev("p0.sql", 3, "x")]))
    gapped.steps[0].gaps = [gid]
    fams = {f["status"] for f in Analysis(gapped.to_dict()).families if "db.src.a.x" in f["ends"]}
    assert fams == {"partial"}


def test_incomplete_trace_stays_visibly_incomplete():
    """The sample reaches the requested boundary on two branches, but a dynamic-SQL override is unresolved."""
    md = render_overview(sample_trace().to_dict(), "sample-trace.lineage.json", detailed_page="sample-trace.md")
    answer, coverage = section(md, "Plain answer"), section(md, "Coverage")
    assert answer.strip().startswith("- **Not fully traced (1 open point on the value routes, so sources may be missing).**")
    assert "**Incomplete**" in coverage and "2 ends do reach the requested boundary, but the trace as a whole is not " \
        "complete" in coverage
    assert "Complete" not in coverage.split("**Incomplete**")[0]
    assert "UPDATE (run order not proved)" in section(md, "Route families")
    assert "**SQL built and run at run time (dynamic SQL)**" in section(md, "Main evidence needed")
    assert "unresolved (not proved)" in section(md, "Route families")


def test_every_overview_link_resolves_to_the_detailed_page(tmp_path):
    for md_path, data_path in ((None, None), ("ovb", None)):
        if md_path is None:
            data = sample_trace().to_dict()
        else:
            run_ovb(tmp_path)
            data = json.loads((tmp_path / f"{stem_for(OVB_COLUMN)}.lineage.json").read_text(encoding="utf-8"))
        page = render_overview(data, "x.lineage.json")
        detailed = render_markdown(data, "x.lineage.json")
        anchors = set(re.findall(r'<a id="([^"]+)"></a>', detailed))
        links = re.findall(r"\]\(x\.lineage\.md#([^)]+)\)", page)
        assert links and {"route-families", "gaps", "detailed-steps"} <= set(links) | anchors
        missing = sorted({link for link in links if link not in anchors})
        assert not missing, missing
        # Every family in the diagram has a row linking to it; every source row links to its family and step.
        fams = re.findall(r"^\| \[(F\d+)\]\(x\.lineage\.md#(fam-[0-9a-f]+)\)", section(page, "Route families"), re.M)
        diagram = check_mermaid(mermaid_block(page))
        assert sorted(f for f, _ in fams) == sorted(k for k in diagram if k not in ("T", "MORE"))
        # The detailed page keeps every step and lists every family member.
        for fam in Analysis(data).families:
            assert f'<a id="{fam["id"]}"></a>' in detailed
            for member in fam["members"]:
                for step in member["steps"]:
                    assert f'<a id="{step}"></a>' in detailed


def test_trace_json_gets_an_additive_route_families_block(tmp_path):
    run_ovb(tmp_path)
    data = json.loads((tmp_path / f"{stem_for(OVB_COLUMN)}.lineage.json").read_text(encoding="utf-8"))
    assert set(data) == {"schema_version", "requested", "target", "target_status", "target_gaps", "boundary", "scan",
                         "completeness", "nodes", "steps", "stops", "gaps", "cycles", "resolved_gaps",
                         "assertions_used", "assertion_provenance", "route_families"}
    block = data["route_families"]
    assert block["schema_version"] == "column-lineage-route-families/1.0"
    steps = {s["id"] for s in data["steps"]}
    nodes = {n["id"] for n in data["nodes"]}
    ends = set()
    for fam in block["families"]:
        assert fam["route_count"] == sum(len(m["object_sequences"]) for m in fam["members"])
        for member in fam["members"]:
            assert set(member["steps"]) <= steps and member["end"] in nodes
            ends.add(member["end"])
    # Every stop of the value routes is the end of some family: nothing is dropped.
    assert {s["node"] for s in data["stops"]} == ends
    assert len(data["steps"]) == 7 and len(data["stops"]) == 7
    # Re-rendering from the JSON gives the same families.
    assert [f["id"] for f in Analysis(data).families] == [f["id"] for f in block["families"]]


def test_index_needs_attention(tmp_path):
    glossary = tmp_path / "glossary.json"
    glossary.write_text(json.dumps({"columns": [
        {"column": OVB_COLUMN, "definition": "Invented approved text", "status": "approved", "source": "Owner"},
        {"column": "demo.output.result_value", "definition": "Invented draft", "status": "proposed"},
    ]}), encoding="utf-8")
    run_ovb(tmp_path / "ovb", "--business-glossary", str(glossary))
    run(fixture("ambiguous_writers"), "demo.output.result_value", tmp_path / "amb")
    items = []
    for out, column in ((tmp_path / "ovb", OVB_COLUMN), (tmp_path / "amb", "demo.output.result_value")):
        items.append({"data": json.loads((out / f"{stem_for(column)}.lineage.json").read_text(encoding="utf-8")),
                      "overview": f"{stem_for(column)}.overview.md"})
    items.append({"data": proof_trace().to_dict(), "overview": "db.s.t.c.overview.md"})
    index = build_index(items, load_glossary(glossary))
    att = index["needs_attention"]
    origins = {(r["source"]["label"], r["stop"]): r for r in att["unresolved_origins"]}
    assert origins[("core_db.crm", "source_without_writer")]["columns"] == [OVB_COLUMN]
    assert origins[("core_db.crm", "source_without_writer")]["routes"] == 4
    assert ("file connection limit_feed (package packages/load_file_limit.dtsx)", "external_file") in origins
    assert origins[("db.s", "source_without_writer")]["columns"] == ["db.s.t.c"]
    (order,) = att["ambiguous_writer_order"]
    assert order["object"] == "?.demo.output.result_value" and order["columns"] == ["demo.output.result_value"]
    assert len(order["locations"]) == 2
    assert att["assertions_awaiting_review"] == [{"assertion": "a1", "columns": ["db.s.t.c"]}]
    defs = att["definitions_awaiting_approval"]
    assert defs["glossary_supplied"] and defs["count"] == 2
    assert {d["column"]: d["glossary_status"] for d in defs["columns"]} == {"db.s.t.c": "missing",
                                                                         "demo.output.result_value": "proposed"}
    rows = {c["column"]: c for c in index["columns"]}
    assert rows[OVB_COLUMN]["definition_status"] == "approved" and len(rows[OVB_COLUMN]["route_families"]) == 7
    assert {f["id"] for f in index["route_families"]} >= set(rows[OVB_COLUMN]["route_families"])
    md = render_index(index)
    assert md.index("## Needs attention") < md.index("## Grouped by terminal source objects")
    for heading in ("### Unresolved origins (", "### Ambiguous writer order (1)", "### Assertions awaiting review (1)",
                    "### Definitions awaiting owner approval (2)"):
        assert heading in md
    assert "- `core_db.crm` — upstream writer unknown (4 routes); 1 column: " \
           f"[{OVB_COLUMN}]({stem_for(OVB_COLUMN)}.overview.md)" in md
    assert "assertion `a1` (recorded mapping, not code)" in md
    assert "a shared signature is not a shared transformation or meaning" in md
    # Written by the CLI too, and counted (names left out) in the gap summary.
    run_ovb(tmp_path / "cli", "--gap-summary")
    on_disk = json.loads((tmp_path / "cli" / "routes-index.json").read_text(encoding="utf-8"))
    assert on_disk["needs_attention"]["definitions_awaiting_approval"]["glossary_supplied"] is False
    counts = json.loads((tmp_path / "cli" / "gap-summary.json").read_text(encoding="utf-8"))["route_index"]
    assert counts["route_families"] == 7 and counts["needs_attention"]["definitions_awaiting_approval"] == 1
    assert "No business glossary was supplied" in (tmp_path / "cli" / "routes-index.md").read_text(encoding="utf-8")


def test_mermaid_is_valid_for_every_fixture(tmp_path):
    """Generate the family diagram for columns of every fixture directory and check its syntax."""
    checked = 0
    for root in sorted(p for p in (ROOT / "fixtures" / "column").iterdir() if p.is_dir()):
        columns = fixture_columns(root)
        if not columns:
            continue
        out = tmp_path / root.name
        extra = ["--package-root", str(root.parent / "ssis")] if root.name == "ssis" else []
        run_cli(cli_args(root, columns, out, "--jobs", "1", *extra))
        for page in sorted(out.glob("*.overview.md")):
            md = page.read_text(encoding="utf-8")
            if "```mermaid" in md:
                nodes = check_mermaid(mermaid_block(md))
                assert len(nodes) <= 12
                checked += 1
            assert len(md.splitlines()) < MAX_LINES, page
    assert checked >= 40, checked


def test_mermaid_escapes_awkward_names():
    target = col("db", "s", 'odd"name', "c|1")
    src = col("db", "s[x]", "t{1}", "a<b>#;`c")
    trace = TraceResult(target=target, requested="db.s.t.c", boundary=Boundary(), scan={})
    for node in (target, src):
        trace.add_node(node)
    trace.steps.append(Step(target=target, writer_kind="insert_select", expression="x", operation="direct",
                            procedure="s.p", branch_context=['IF @a = "x|y"'], inputs=[Input(src, VALUE, "x")],
                            evidence=[ev("p.sql", 1, "INSERT ...")]))
    trace.stops.append(Stop(src, "source_without_writer"))
    nodes = check_mermaid(mermaid_block(render_overview(trace.to_dict())))
    # Readable, entity-free: a quote becomes an apostrophe, < > become ‹ ›, ; becomes a comma; the rest stays.
    assert "odd'name" in nodes["T"]["label"] and "c|1" in nodes["T"]["label"]
    assert "s[x]" in nodes["F1"]["label"] and "a‹b›#,`c" in nodes["F1"]["label"]


def test_headline_counts_boundary_and_unresolved_routes_separately():
    """Reviewer: 'N of M end unresolved; N of these reach the boundary' read as a subset. Separate counts."""
    from column_lineage.render_overview import Analysis, _answer_headline
    trace = sample_trace()
    a = Analysis(trace.to_dict())
    a.comp = {**a.comp, "status": "complete", "result": "complete_within_selected_scope"}
    text = _answer_headline(a, unproven=9, total=25)
    assert "of 25 routes carrying the value, 9 end where no writer" in text
    assert "of these" not in text and ("a separate" in text or "none reaches" in text)
