"""Compare traces with known lineage, locally, and write a summary with no names in it.

    python -m column_lineage.compare --expected expected.json --traces <output folder>
                                     [--summary comparison-summary.json] [--details comparison-details.json]

expected.json (kept local; it names real objects):

    {
      "schema_version": "column-lineage-expected/1.0",
      "columns": [
        {"target": "db.schema.table.column", "label": "C01", "kind": "pass-through",
         "sources": ["db.schema.source.column", "..."]}
      ]
    }

``target`` is the spec passed to --column (the trace file is found by the same file-name rule).
``sources`` are the terminal sources the known lineage gives for the column. They need not all be at
the requested boundary: a column may draw from several branches ending in different databases or
files. A source may omit the database (schema.table.column) to match any database; a flat-file column
read by an SSIS package is written ``<file connection>.<column>``.
``label`` and ``kind`` are free text that goes into the summary, so keep them generic.

For each expected source the comparison reports one of:

- ``found``: on the trace's value path (as a value input);
- ``found_as_condition``: reached only as a CASE/IIF condition or window input;
- ``found_as_ambiguous_candidate``: reached only through an ambiguous or unresolved binding (one of
  several candidates the trace could not decide between);
- ``found_as_row_selection``: appears only as a filter/join column;
- ``missing_behind_gaps``: not reached, and the trace has open value or context gaps (the likely
  reason; their kinds are counted);
- ``missing_at_boundary``: not reached, the trace stops at the requested boundary and has no open
  gaps (expected list and boundary are at different levels);
- ``missing_unexplained``: not reached and nothing in the trace explains why. This is the
  strongest sign of a tool error.

Reached sources the known lineage does not list are counted as ``extra``; the details file
lists them so someone can judge whether the tool or the documentation is right.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

from .cli import stem


def _norm(value: str) -> str:
    return value.strip().lower().replace("[", "").replace("]", "").replace('"', "")


def _matches(node_id: str, expected: str) -> bool:
    node = _norm(node_id)
    want = _norm(expected)
    # Package file columns are flat_file:<package>::<connection>.<column>: match on the part after "::".
    return node == want or node.endswith("." + want) or node.endswith("::" + want)


def _walk(trace: dict) -> tuple[dict[str, set[str]], set[str], set[str]]:
    """Node id -> roles by which it is reached from the target; leaves; row-selection nodes."""
    by_target: dict[str, list[dict]] = {}
    for step in trace.get("steps", []):
        by_target.setdefault(step["target"], []).append(step)
    roles: dict[str, set[str]] = {}
    rows: set[str] = set()
    stack = [(trace["target"]["id"], "value")]
    seen = set()
    while stack:
        node, role = stack.pop()
        roles.setdefault(node, set()).add(role)
        if (node, role) in seen:
            continue
        seen.add((node, role))
        for step in by_target.get(node, []):
            for item in step.get("inputs", []):
                # A condition anywhere upstream makes everything behind it condition-only; an ambiguous
                # or unresolved binding makes everything behind it only a candidate.
                if role == "condition" or item["role"] != "value":
                    child = "condition"
                elif role == "candidate" or item.get("status") in ("ambiguous", "unresolved"):
                    child = "candidate"
                else:
                    child = "value"
                stack.append((item["node"], child))
            rows.update(r["node"] for r in step.get("row_selection", []))
    leaves = {n for n in roles if n not in by_target and n != trace["target"]["id"]}
    return roles, leaves, rows


def compare_one(expected: dict, trace: dict | None) -> tuple[dict, dict]:
    label = expected.get("label") or "?"
    summary = {"label": label, "kind": expected.get("kind"), "expected_sources": len(expected.get("sources", []))}
    details = {"label": label, "target": expected["target"], "sources": {}, "extra": []}
    if trace is None:
        summary["result"] = "no_trace_file"
        return summary, details
    completeness = trace["completeness"]
    roles, leaves, rows = _walk(trace)
    blocking = {**completeness.get("value_gaps_by_kind", {}), **completeness.get("context_gaps_by_kind", {})}
    stops = {s["node"]: s["reason"] for s in trace.get("stops", [])}
    boundary_stops = any(r == "boundary" for r in stops.values())
    outcome: Counter = Counter()
    for source in expected.get("sources", []):
        reached = [n for n in roles if _matches(n, source)]
        if any("value" in roles[n] for n in reached):
            result = "found"
        elif any("candidate" in roles[n] for n in reached):
            result = "found_as_ambiguous_candidate"
        elif reached:
            result = "found_as_condition"
        elif any(_matches(n, source) for n in rows):
            result = "found_as_row_selection"
        elif blocking:
            result = "missing_behind_gaps"
        elif boundary_stops:
            result = "missing_at_boundary"
        else:
            result = "missing_unexplained"
        outcome[result] += 1
        details["sources"][source] = {"result": result, "matched_nodes": sorted(reached)}
    expected_sources = expected.get("sources", [])
    value_leaves = [n for n in leaves if roles[n] & {"value", "candidate"} and not n.startswith(("constant:", "unknown:"))
                    and stops.get(n) in ("boundary", "source_without_writer", "external_file")]
    extra = sorted(n for n in value_leaves if not any(_matches(n, s) for s in expected_sources))
    details["extra"] = extra
    summary.update({
        "status": completeness["status"],
        "relies_on_assertions": completeness.get("relies_on_assertions", False),
        "steps": len(trace.get("steps", [])),
        "sources": dict(sorted(outcome.items())),
        "extra_sources": len(extra),
        "open_value_and_context_gaps_by_kind": dict(sorted(blocking.items())),
        "row_selection_gaps": sum(completeness.get("row_selection_gaps_by_kind", {}).values()),
    })
    if not expected_sources:
        summary["result"] = "no_baseline"   # nothing to compare against; extras are listed in the details
    else:
        summary["result"] = ("exact" if outcome.get("found", 0) == len(expected_sources) and not extra else
                             "partial" if outcome.get("found") else "none_found")
    return summary, details


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m column_lineage.compare",
                                     description="Compare column lineage traces with known lineage.")
    parser.add_argument("--expected", required=True, help="Local JSON file of known lineage (see module docs)")
    parser.add_argument("--traces", required=True, help="Output folder of a column_lineage run")
    parser.add_argument("--summary", default=None, help="Summary path (default: <traces>/comparison-summary.json)")
    parser.add_argument("--details", default=None, help="Local details path (default: <traces>/comparison-details.json)")
    args = parser.parse_args(argv)
    try:
        data = json.loads(Path(args.expected).read_text(encoding="utf-8-sig"))
        columns = data["columns"]
        assert isinstance(columns, list)
    except (OSError, ValueError, KeyError, AssertionError) as exc:
        print(f"error: cannot read expected lineage {args.expected}: {exc}", file=sys.stderr)
        return 2
    folder = Path(args.traces)
    summaries, details = [], []
    for index, column in enumerate(columns, 1):
        column = dict(column)
        column.setdefault("label", f"C{index:02d}")
        path = folder / f"{stem(column['target'])}.lineage.json"
        trace = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        one, detail = compare_one(column, trace)
        summaries.append(one)
        details.append(detail)
    totals: Counter = Counter()
    for one in summaries:
        totals.update(one.get("sources", {}))
    blocking: Counter = Counter()
    for one in summaries:
        blocking.update(one.get("open_value_and_context_gaps_by_kind", {}).keys())
    summary = {
        "schema_version": "column-lineage-comparison/1.0",
        "note": "Counts and generic labels only. Details with names are in the local details file.",
        "columns": len(summaries),
        "results": dict(sorted(Counter(s["result"] for s in summaries).items())),
        "expected_sources": sum(s["expected_sources"] for s in summaries),
        "sources_by_result": dict(sorted(totals.items())),
        "extra_sources": sum(s.get("extra_sources", 0) for s in summaries),
        "columns_blocked_by_gap_kind": dict(sorted(blocking.items())),
        "per_column": summaries,
    }
    summary_path = Path(args.summary) if args.summary else folder / "comparison-summary.json"
    details_path = Path(args.details) if args.details else folder / "comparison-details.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    details_path.write_text(json.dumps(details, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{summary_path}\n{details_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
