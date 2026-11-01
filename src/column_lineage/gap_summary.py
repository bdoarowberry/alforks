"""Sanitized summary of gaps and unsupported SQL, safe to hand back without the source.

Identifiers become ID1, ID2, ... (numbered per sample), string literals become '?'
and numbers become 0. No object, column, path or file name is written.
"""
from __future__ import annotations

from collections import Counter
import re

import sqlglot
from sqlglot.tokens import TokenType

KEYWORD = re.compile(r"[A-Za-z_]+")


def anonymize(sql: str, limit: int = 400, names: dict | None = None) -> str:
    try:
        tokens = sqlglot.Dialect.get_or_raise("tsql").tokenizer().tokenize(sql)
    except Exception:  # noqa: BLE001 - the tokenizer may fail on exactly the SQL we want to report
        return _fallback(sql, limit)
    names = {} if names is None else names
    out = []
    merged = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        # '@name' and '#name' arrive as a prefix token plus a name token.
        if token.text in ("@", "@@", "#", "##") and i + 1 < len(tokens) and tokens[i + 1].start == token.end + 1:
            key = (token.text + tokens[i + 1].text).lower()
            names.setdefault(key, f"ID{len(names) + 1}")
            merged.append(None)
            out.append(token.text + names[key])
            i += 2
            continue
        merged.append(token)
        i += 1
    tokens = [t for t in merged if t is not None]
    prefixed = out
    out = []
    pending = iter(prefixed)
    builtins = set(sqlglot.Dialect.get_or_raise("tsql").parser_class.FUNCTIONS) | {
        "TRY_PARSE", "PARSE", "STRING_AGG", "FORMAT", "IIF", "CHOOSE", "OPENJSON", "OPENQUERY", "OPENROWSET"}
    for position, token in enumerate(merged):
        if token is None:
            out.append(next(pending))
            continue
        kind = token.token_type
        following = next((t for t in merged[position + 1:] if t is not None), None)
        if kind == TokenType.VAR and token.text.upper() in builtins and following is not None and following.text == "(":
            out.append(token.text.upper())  # built-in function names are not identifying and explain parse failures
            continue
        if kind in (TokenType.VAR, TokenType.IDENTIFIER) or (kind == TokenType.PARAMETER):
            key = token.text.lower()
            names.setdefault(key, f"ID{len(names) + 1}")
            out.append(names[key])
        elif kind in (TokenType.STRING, TokenType.NATIONAL_STRING):
            out.append("'?'")
        elif kind == TokenType.NUMBER:
            out.append("0")
        elif token.text.startswith("#") or token.text.startswith("@"):
            key = token.text.lower()
            names.setdefault(key, f"ID{len(names) + 1}")
            out.append(token.text[0] + names[key])
        else:
            out.append(token.text.upper() if KEYWORD.fullmatch(token.text) else token.text)
    text = " ".join(out)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fallback(sql: str, limit: int) -> str:
    words = re.findall(r"[A-Za-z_]+|\S", re.sub(r"'(?:''|[^'])*'", "'?'", sql))
    upper = {"SELECT", "INSERT", "INTO", "UPDATE", "DELETE", "MERGE", "FROM", "WHERE", "JOIN", "ON", "SET",
             "EXEC", "EXECUTE", "AS", "AND", "OR", "CASE", "WHEN", "THEN", "ELSE", "END", "WITH", "VALUES"}
    out = [w.upper() if w.upper() in upper else ("ID" if w[0].isalpha() or w[0] == "_" else w) for w in words]
    text = " ".join(out)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _error_class(error: str | None) -> str:
    if not error:
        return "unsupported command"
    error = re.sub(r"\x1b\[[0-9;]*m", "", error)
    # SQLGlot's own argument and class names are not identifying and say what the parser wanted.
    required = re.search(r"Required keyword: '(\w+)' missing for <class 'sqlglot\.expressions\.(?:\w+\.)*(\w+)'", error)
    if required:
        return f"ParseError: Required keyword: {required.group(1)} missing for {required.group(2)}"
    token = re.search(r"(Expected [\w ]+ but got) <Token token_type: TokenType\.(\w+)", error)
    if token:
        return f"ParseError: {token.group(1)} {token.group(2)}"
    first = error.split(".")[0]
    first = re.sub(r"'[^']*'", "'?'", first)
    first = re.sub(r"Line \d+, Col: \d+", "", first)
    return first.strip()[:120]


def _error_window(sql: str, error: str | None, before: int = 160, after: int = 100) -> str | None:
    """Anonymized text around the parser's reported position, marked with <<HERE>>."""
    match = re.search(r"Line (\d+), Col: (\d+)", re.sub(r"\x1b\[[0-9;]*m", "", error or ""))
    if not match:
        return None
    line, col = int(match.group(1)), int(match.group(2))
    lines = sql.split("\n")
    if line < 1 or line > len(lines):
        return None
    offset = sum(len(x) + 1 for x in lines[:line - 1]) + max(col - 1, 0)
    # Cut at whitespace so a token is not split.
    start = sql.rfind(" ", 0, max(0, offset - before)) + 1 if offset > before else 0
    end = sql.find(" ", min(len(sql), offset + after))
    end = len(sql) if end < 0 else end
    names: dict = {}
    left = anonymize(sql[start:offset], limit=before * 2, names=names)
    right = anonymize(sql[offset:end], limit=after * 2, names=names)
    return ("… " if start else "") + left + " <<HERE>> " + right + (" …" if end < len(sql) else "")


def build(catalog, traces: list[dict], samples: int = 25, packages=None) -> dict:
    statements = Counter()
    errors = Counter()
    sample_list = []
    seen = set()
    for statement, unit in catalog.fallback_samples:
        keyword = (statement.text.strip().split(None, 1) or ["?"])[0].upper()
        keyword = keyword if KEYWORD.fullmatch(keyword) else "?"
        statements[keyword] += 1
        error_class = _error_class(statement.parse_error)
        errors[error_class] += 1
        shape = anonymize(statement.text)
        if len(sample_list) < samples and (keyword, error_class) not in seen:
            seen.add((keyword, error_class))
            sample = {"keyword": keyword, "error_class": error_class, "anonymized_sql": shape}
            near = _error_window(statement.text, statement.parse_error)
            if near:
                sample["anonymized_near_error"] = near
            sample_list.append(sample)
    issue_kinds = Counter(issue.kind for issue in catalog.issues)
    evaluations = Counter()
    for issue in catalog.issues:
        evaluation = catalog.dynamic_evaluation(issue) if hasattr(catalog, "dynamic_evaluation") else None
        if evaluation is not None:
            evaluations[evaluation.status] += 1
    trace_summary = []
    for data in traces:
        completeness = data["completeness"]
        trace_summary.append({
            "status": completeness["status"],
            "overall_step_status": completeness["overall_step_status"],
            "steps": len(data["steps"]),
            "steps_by_status": completeness["steps_by_status"],
            "gaps_by_kind": completeness["gaps_by_kind"],
            "stops_by_reason": completeness["stops_by_reason"],
            "result": completeness.get("result"),
            "ends_by_meaning": completeness.get("ends_by_meaning", {}),
            "declared_terminal_ends": completeness.get("declared_terminal_ends", 0),
            "asserted_steps": completeness.get("asserted_steps", 0),
            "gaps_resolved_by_assertions": completeness.get("gaps_resolved_by_assertions", 0),
            "gap_statements_by_kind": _gap_statements(data),
            "context_gaps_by_kind": completeness.get("context_gaps_by_kind", {}),
            "row_selection_gaps_by_kind": completeness.get("row_selection_gaps_by_kind", {}),
            "operations": dict(sorted(Counter(s["operation"] for s in data["steps"]).items())),
            "writer_kinds": dict(sorted(Counter(s["writer_kind"] for s in data["steps"]).items())),
            "max_depth_reached": _depth(data),
        })
        if packages is not None:
            trace_summary[-1].update(_package_counts(data))
    result = {
        "schema_version": "column-lineage-gap-summary/1.0",
        "note": "Contains no object, column, path or file names. Anonymized SQL replaces identifiers with IDn.",
        "scan": {**catalog.stats, "file_encodings": getattr(catalog, "file_encodings", {})},
        "statement_issues_by_kind": dict(sorted(issue_kinds.items())),
        "dynamic_sql_evaluation": dict(sorted(evaluations.items())),
        "unparsed_statements_by_keyword": dict(sorted(statements.items())),
        "parse_error_classes": dict(errors.most_common(40)),
        "unparsed_samples": sample_list,
        "totals": _totals(traces),
        "traces": trace_summary,
    }
    if packages is not None:
        result["packages"] = packages.summary()   # counts only (see ssis.PackageCatalog.summary)
    return result


PACKAGE_GAP_KINDS = ("unsupported_package_step", "ambiguous_package_copy", "package_runtime_value",
                     "package_expression", "package_metadata", "package_column_not_mapped",
                     "package_database_unknown")


def _package_counts(data: dict) -> dict:
    """Package steps by writer kind and package gaps by kind for one trace (counts only)."""
    steps = Counter(s["writer_kind"] for s in data["steps"] if str(s.get("writer_kind", "")).startswith("package_"))
    gaps = Counter(g["kind"] for g in data.get("gaps", []) if g["kind"] in PACKAGE_GAP_KINDS)
    copies = {ev.get("path") for s in data["steps"] if str(s.get("writer_kind", "")).startswith("package_")
              for ev in s.get("evidence", [])[:1]}
    return {"package_steps": sum(steps.values()), "package_steps_by_writer_kind": dict(sorted(steps.items())),
            "package_gaps_by_kind": dict(sorted(gaps.items())), "package_files_on_path": len(copies)}


def _totals(traces: list[dict]) -> dict:
    """Across all traces: outcomes, which gap kinds block how many traces, and shared statements."""
    blocked: Counter = Counter()
    gaps: Counter = Counter()
    statements: dict[str, set] = {}
    traces_per_statement: Counter = Counter()
    outcome: Counter = Counter()
    results: Counter = Counter()
    ends: Counter = Counter()
    declared_ends = 0
    no_writer = {"stops": 0, "distinct_objects": set(), "with_unused_writer_candidates": 0}
    near: Counter = Counter()
    earlier = {"traces_with_earlier_content": 0, "loops_cut_traced": 0, "update_only_untraced": 0,
               "rows_not_updated_steps": 0, "traces_with_remaining_cycles": 0, "remaining_cycles": 0}
    for data in traces:
        priors = [s for s in data.get("stops", []) if s.get("loop")]
        earlier["traces_with_earlier_content"] += bool(priors)
        earlier["loops_cut_traced"] += sum(1 for s in priors if s["reason"] == "prior_state")
        earlier["update_only_untraced"] += sum(1 for s in priors if s["reason"] != "prior_state")
        earlier["rows_not_updated_steps"] += sum(1 for s in data.get("steps", []) if s.get("writer_kind") == "retained")
        earlier["traces_with_remaining_cycles"] += bool(data.get("cycles"))
        earlier["remaining_cycles"] += len(data.get("cycles") or [])
        results[data["completeness"].get("result", "?")] += 1
        ends.update(data["completeness"].get("ends_by_meaning", {}))
        declared_ends += data["completeness"].get("declared_terminal_ends", 0)
        for stop in data.get("stops", []):
            if stop["reason"] != "source_without_writer":
                continue
            no_writer["stops"] += 1
            no_writer["distinct_objects"].add(stop["node"].rsplit(".", 1)[0])
            found = stop.get("writer_search") or []
            if found:
                no_writer["with_unused_writer_candidates"] += 1
            near.update(f["category"] for f in found)
    for data in traces:
        completeness = data["completeness"]
        blocking = {**completeness.get("value_gaps_by_kind", {}), **completeness.get("context_gaps_by_kind", {})}
        if completeness["status"] == "complete":
            outcome["complete_relying_on_assertions" if completeness.get("relies_on_assertions") else "complete"] += 1
        elif blocking:
            outcome["incomplete"] += 1
        else:
            outcome["incomplete_without_value_gaps"] += 1
        for kind in blocking:
            blocked[kind] += 1
        seen_here = set()
        for gap in data.get("gaps", []):
            gaps[gap["kind"]] += 1
            for ev in gap.get("evidence", [])[:1]:
                key = ev.get("statement_id") or f"{ev.get('path')}:{ev.get('start_line')}"
                statements.setdefault(gap["kind"], set()).add(key)
                if gap.get("affects") != "row_selection":
                    seen_here.add(key)
        for key in seen_here:
            traces_per_statement[key] += 1
    shared = sorted(traces_per_statement.values(), reverse=True)
    return {
        "traces": len(traces),
        "outcomes": dict(sorted(outcome.items())),
        "results": dict(sorted(results.items())),
        "value_path_ends_by_meaning": dict(sorted(ends.items())),
        "declared_terminal_ends": declared_ends,
        "no_writer_stops": {"stops": no_writer["stops"], "distinct_objects": len(no_writer["distinct_objects"]),
                            "with_unused_writer_candidates": no_writer["with_unused_writer_candidates"],
                            "unused_writer_candidates_by_category": dict(sorted(near.items()))},
        "earlier_content": earlier,
        "traces_blocked_by_gap_kind": dict(sorted(blocked.items())),
        "gaps_by_kind": dict(sorted(gaps.items())),
        "distinct_gap_statements_by_kind": {k: len(v) for k, v in sorted(statements.items())},
        "statements_blocking_most_traces": shared[:10],
        "note": "statements_blocking_most_traces lists, for the ten statements behind value or context gaps "
                "in the most traces, how many traces each blocks (counts only). earlier_content counts reads "
                "of a column's or variable's own earlier value: loops_cut_traced end where that value's writes "
                "are traced (stop prior_state); update_only_untraced end where nothing in the selected sources "
                "adds the rows (stop source_without_writer); remaining_cycles are loops left as cycles "
                "(for example recursive CTEs).",
    }


def _gap_statements(data: dict) -> dict[str, int]:
    """Distinct statements behind the gaps of each kind (counts only)."""
    seen: dict[str, set] = {}
    for gap in data.get("gaps", []):
        for ev in gap.get("evidence", [])[:1]:
            seen.setdefault(gap["kind"], set()).add(ev.get("statement_id") or (ev.get("path"), ev.get("start_line")))
    return {kind: len(ids) for kind, ids in sorted(seen.items())}


def _depth(data: dict) -> int:
    by_target: dict[str, list[dict]] = {}
    for step in data["steps"]:
        by_target.setdefault(step["target"], []).append(step)
    best = 0
    frontier = [(data["target"]["id"], 0)]
    seen = set()
    while frontier:
        node, depth = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        best = max(best, depth)
        for step in by_target.get(node, []):
            for item in step["inputs"]:
                frontier.append((item["node"], depth + 1))
    return best
