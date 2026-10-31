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
from column_lineage.render_overview import (  # noqa: E402
    build_index, glossary_entry, load_glossary, main, render_overview,
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


def end_rows(md: str) -> dict[str, str]:
    """Column cell -> whole row of the End points table."""
    rows = {}
    for line in section(md, "End points").splitlines():
        if line.startswith("| ") and not line.startswith("| Database") and not line.startswith("|---"):
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
            rows[f"{cells[2]}.{cells[3]}".replace("`", "")] = line
    return rows


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
    assert "128 routes" in routes and "And 122 more routes" in routes and "(big.lineage.md#value-lineage)" in routes
    assert "more end points" in section(md, "End points")
    assert "and 34 more objects" in section(md, "Rows included")
    assert "dynamic SQL" in section(md, "Open points") and "and 5 more" in section(md, "Open points")
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
    result = section(md, "Field and result")
    assert "Traced within the selected sources" in result
    assert "no writer in those sources — their origin is not proved" in result
    assert "None of them is claimed as the system where the value originates" in result
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
    assert "external file read by a package (a stopping point, not a confirmed source system)" in md
    (row,) = end_rows(md).values()
    assert "(file connection)" in row and "not a confirmed source system" in row
    assert "file connection file_a" in section(md, "Routes: how the value arrives")


def test_inferred_and_asserted_are_never_shown_as_proved():
    md = render_overview(proof_trace().to_dict(), "t.lineage.json")
    field = section(md, "Field and result")
    assert "relies on 1 asserted step" in field and "not proved from code" in field
    assert "1 inferred" in field and "1 asserted" in field and "Inferred = bound by a stated rule" in field
    rows = end_rows(md)
    assert "inferred (rule-based, not proved by explicit code)" in rows["a.x"] and "verified" not in rows["a.x"]
    assert "asserted (a person's statement, not code)" in rows["b.y"] and "verified" not in rows["b.y"]
    routes = section(md, "Routes: how the value arrives")
    assert "verified" not in routes
    assert "assertions file assertions.json (asserted, not code)" in routes


def test_row_selection_is_kept_apart_from_values(tmp_path):
    run(fixture("row_selection"), "demo.output.result_value", tmp_path)
    md = overview(tmp_path, "demo.output.result_value")
    rows = section(md, "Rows included")
    assert "which rows" in rows and "do not compute the value" in rows
    assert "filter `s.is_active = 1`" in rows
    assert "join key" in rows and "join condition" in rows and "?.demo.lookup" in rows
    routes, ends = section(md, "Routes: how the value arrives"), section(md, "End points")
    for name in ("is_active", "region_code", "lookup"):
        assert name not in routes and name not in ends
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
    approved = section(overview(tmp_path / "out", "demo.output.result_value"), "Business definition")
    assert "Amount chosen for the report *(approved)*" in approved and "Data owner sign-off" in approved
    proposed = section(overview(tmp_path / "out", "demo.output.alt_value"), "Business definition")
    assert proposed.strip().startswith("Proposed (not approved): Optional alternative amount")

    run(fixture("rename_temp"), "demo.output.result_value", tmp_path / "none")
    missing = section(overview(tmp_path / "none", "demo.output.result_value"), "Business definition")
    assert missing.startswith("\nNot confirmed — question for the source owner.")

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
    assert "| (constant) | — | — | `NULL` |" in section(overview(tmp_path, "demo.output.alt_value"), "End points")


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
    assert "it is itself an end point (no writer in the selected sources" in md
    assert "Not confirmed" in render_overview({})


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
    assert len(page.splitlines()) < MAX_LINES and f"{n} constant values" in page
    assert "And 1480 more end points (1480 constant)" in page


if __name__ == "__main__" and "--write-sample" in sys.argv:
    SAMPLE_OVERVIEW.write_text(render_overview(sample_trace().to_dict(), "sample-trace.lineage.json",
                                               detailed_page="sample-trace.md"), encoding="utf-8")
    print(SAMPLE_OVERVIEW)


def test_proof_line_sources_at_a_glance_and_loop_ends(tmp_path):
    """Tester review: a 'proven to / origin unproven' line beside the result; source families before the
    routes; loop ends labelled in the End points table and linked to the cycle evidence."""
    run(fixture("cycle"), "demo.beta.val", tmp_path)
    md = overview(tmp_path, "demo.beta.val")
    result = section(md, "Field and result")
    assert "**Proven to / origin unproven:**" in result and "origin unproven at" in result and "1 loop" in result
    assert md.index("## Sources at a glance") < md.index("## Routes")
    endpoints = section(md, "End points")
    assert "loop: values here feed each other" in endpoints and "#gaps" in endpoints
