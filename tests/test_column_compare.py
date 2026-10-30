"""The columns-file option and the known-lineage comparison."""
import json

from column_helpers import FIXTURES, run_cli


def run(root, columns, out, *extra):
    columns_file = out / "targets.txt"
    out.mkdir(parents=True, exist_ok=True)
    columns_file.write_text("# targets\n" + "\n".join(columns) + "\n\n", encoding="utf-8")
    assert run_cli(["--columns-file", str(columns_file), "--source-root", str(root), "--output", str(out),
                    "--no-progress", "--gap-summary", *extra]) == 0


def compare(out, expected):
    from column_lineage.compare import main
    path = out / "expected.json"
    path.write_text(json.dumps({"schema_version": "column-lineage-expected/1.0", "columns": expected}), encoding="utf-8")
    assert main(["--expected", str(path), "--traces", str(out)]) == 0
    return (json.loads((out / "comparison-summary.json").read_text(encoding="utf-8")),
            json.loads((out / "comparison-details.json").read_text(encoding="utf-8")))


def test_columns_file_runs_every_target_and_totals_are_summarised(tmp_path):
    out = tmp_path / "out"
    run(FIXTURES / "cross_database", ["report_db.demo.output.result_value", "stage_db.demo.staged.staged_amount"], out,
        "--database-map", "report=report_db", "stage=stage_db", "land=land_db")
    assert len(list(out.glob("*.lineage.json"))) == 2
    totals = json.loads((out / "gap-summary.json").read_text(encoding="utf-8"))["totals"]
    assert totals["traces"] == 2 and totals["outcomes"] == {"complete": 2}


def test_comparison_classifies_found_missing_and_extra(tmp_path):
    out = tmp_path / "out"
    run(FIXTURES / "cross_database", ["report_db.demo.output.result_value"], out,
        "--database-map", "report=report_db", "stage=stage_db", "land=land_db")
    summary, details = compare(out, [
        {"target": "report_db.demo.output.result_value", "label": "C01", "kind": "pass-through",
         "sources": ["land_db.demo.raw_feed.raw_amount"]},
        {"target": "report_db.demo.output.result_value", "label": "C02", "kind": "documented wrongly",
         "sources": ["demo.raw_feed.other_amount"]},
        {"target": "report_db.demo.output.missing_column", "label": "C03", "sources": ["x.y.z"]},
    ])
    by_label = {c["label"]: c for c in summary["per_column"]}
    assert by_label["C01"]["result"] == "exact" and by_label["C01"]["sources"] == {"found": 1}
    assert by_label["C02"]["sources"] == {"missing_unexplained": 1} and by_label["C02"]["extra_sources"] == 1
    assert by_label["C03"]["result"] == "no_trace_file"
    text = json.dumps(summary)
    assert "raw_feed" not in text and "land_db" not in text      # the summary carries no names
    assert details[1]["extra"] == ["land_db.demo.raw_feed.raw_amount"]


def test_comparison_attributes_misses_to_open_gaps(tmp_path):
    out = tmp_path / "out"
    run(FIXTURES / "review" / "assert_dynamic", ["demo.output.result_value"], out, "--default-database", "db")
    summary, _ = compare(out, [{"target": "demo.output.result_value", "sources": ["demo.source_x.col_a"]}])
    column = summary["per_column"][0]
    assert column["sources"] == {"missing_behind_gaps": 1}
    assert column["open_value_and_context_gaps_by_kind"] == {"dynamic_sql": 1}
