"""Reader-facing outputs rendered from trace JSON: a short overview page per column and a run-level
route index.

Both read only the JSON form of traces (``TraceResult.to_dict()``), like the detailed page::

    python -m column_lineage.render_overview trace.lineage.json [...] [--out-dir DIR] [--index-out DIR]
                                             [--business-glossary FILE]

Overview page (``<stem>.overview.md``), for a reader who does not read SQL:

1. Field and result
2. Business definition (from an optional glossary; never derived from names)
3. Routes: how the value arrives, branch by branch
4. Rows included (row selection, kept apart from the value)
5. End points
6. Open points
7. Links

Definitions:

- A *route* is a path from the target through value, condition or window inputs to an end. It is
  summarised as the sequence of distinct objects it passes (tables, views, temp tables, package data
  flows, files; CTEs and derived tables are inside one statement and are not listed). Routes with the
  same object sequence are shown as one route; their ends keep their own branch labels.
- An *end* is a node on a route with no step into it: a stop (boundary, no writer in the selected
  sources, external file, constant, parameter without caller, depth limit) or a node with a gap.
- Route index signatures: the set of terminal source objects of a column's ends (files grouped by
  connection, constants left out), and the set of writer procedures, packages and scripts on its value
  routes. A shared signature does not mean identical expressions, branch selection or business meaning.

Output is deterministic: everything is sorted, ids are content hashes, nothing time-dependent is added.
"""
from __future__ import annotations

import argparse
from collections import Counter
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from .render_markdown import (NEXT_EVIDENCE, SECTION_ANCHORS, _anchor, _cell, _code, _link_target, _plural,
                              _status_rank, _text, _Trace, _truncate)

INDEX_SCHEMA = "column-lineage-routes/1.0"
VALUE_ROLES = ("value", "condition", "window")

MAX_ROUTES = 6               # routes shown on the overview
MAX_ROUTE_OBJECTS = 7        # objects shown per route before the middle is elided
MAX_ROUTE_ENDS = 4           # ends named per route
MAX_LABELS = 3               # branch labels shown per route or end
MAX_WRITERS = 3              # writers named per route
MAX_ENDPOINTS = 20           # rows of the end-point table
MAX_ROW_OBJECTS = 6          # objects in "Rows included"
MAX_ROW_PREDICATES = 3       # conditions shown per object
MAX_OPEN = 6                 # open-point kinds shown
MAX_PLACES = 3               # places named per open-point kind
LABEL_LIMIT = 60             # characters of a branch label
CONDITION_LIMIT = 90         # characters of a row-selection condition
CONSTANT_LIMIT = 30          # characters of a constant shown as a value
NAME_LIMIT = 90              # characters of an object or column name
ROUTE_CAP = 400              # distinct object sequences kept per node while enumerating routes
END_LABEL_CAP = 6            # branch labels kept per end while enumerating routes
INDEX_MAX_SHARED = 25        # shared objects / writers listed per group in routes-index.md

OPERATION_TEXT = {
    "cast": "type conversion",
    "case": "conditional choice (CASE/IIF)",
    "coalesce": "first non-empty of several values (COALESCE/ISNULL)",
    "arithmetic": "arithmetic",
    "function": "function applied",
    "aggregate": "aggregated over rows",
    "constant": "set to a constant",
    "string": "text manipulation",
    "window": "window calculation over rows",
    "subquery": "looked up by a subquery",
    "expression": "other calculation",
    "variable": "passed through a variable or parameter",
}
STATUS_TEXT = {
    "verified": "proved from code (verified)",
    "inferred": "inferred",
    "asserted": "asserted",
    "partial": "partial",
    "ambiguous": "ambiguous",
    "unresolved": "unresolved",
}
STATUS_SHORT = {
    "verified": "verified (proved from code)",
    "inferred": "inferred (rule-based, not proved by explicit code)",
    "asserted": "asserted (a person's statement, not code)",
    "partial": "partial (some inputs not proved)",
    "ambiguous": "ambiguous (candidates, none proved)",
    "unresolved": "unresolved",
}
ROW_TEXT = {
    "filter": "filter",
    "join_key": "join key",
    "join_predicate": "join condition",
    "group_by": "grouped by",
    "having": "group filter (HAVING)",
    "qualify": "window filter (QUALIFY)",
    "top_order": "top-N order",
    "merge_condition": "merge condition",
}
STATUS_MEANING = {
    "inferred": "inferred = bound by a stated rule (column position, default schema or database, a caller's "
                "temp table, evaluated dynamic SQL), not by explicit code",
    "asserted": "asserted = stated by a person in an assertions file, not proved from code",
    "partial": "partial = some inputs of the step are not proved",
    "ambiguous": "ambiguous = several candidates, none proved",
    "unresolved": "unresolved = no binding could be established",
}
GAP_TEXT = {
    "dynamic_sql": "SQL built and run at run time (dynamic SQL)",
    "parse_error": "a statement the tool could not read",
    "unsupported_statement": "a statement the tool does not analyse",
    "insert_exec": "rows loaded from a procedure's result set (INSERT ... EXEC)",
    "missing_definition": "an object or column whose definition is not in the selected sources",
    "unresolved_alias": "a column reference that names nothing in scope",
    "ambiguous_column": "a column that could come from more than one table",
    "ambiguous_object": "an object name that matches more than one database",
    "ambiguous_writer_order": "several writers whose run order is not known",
    "positional_without_schema": "an insert by column position into a table with no known column list",
    "wildcard_without_schema": "a SELECT * from a source with no known column list",
    "column_count_mismatch": "an insert whose column and value counts differ",
    "opaque_function": "a user function whose body was not analysed",
    "table_valued_function": "a table-valued function whose output was not analysed",
    "external_object": "an object on another server or outside the selected sources",
    "unresolved_variable": "a variable whose value is not known from the code",
    "runtime_branch": "a branch chosen only at run time",
    "cycle": "values that feed each other (a loop)",
    "depth_limit": "an expression too deeply nested to analyse",
    "column_not_written": "no writer of this column was found",
    "unexplained_end": "a path end the tool could not explain (a tool defect)",
    "unsupported_package_step": "a package step the tool does not analyse",
    "ambiguous_package_copy": "several copies of a package write this column",
    "package_runtime_value": "a package value known only at run time",
    "package_expression": "a package expression that could not be read",
    "package_metadata": "incomplete package metadata",
    "package_column_not_mapped": "a package load that does not map this column",
    "package_database_unknown": "a package connection whose database is not proved",
}
END_ORDER = ("boundary", "declared", "source_without_writer", "external_file", "parameter_without_caller",
             "max_depth", "constant", "gap", "cycle", "open")


# --------------------------------------------------------------------------- small helpers

def _hash(prefix: str, items) -> str:
    raw = json.dumps(sorted(items), ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:10]}"


def _add_unique(target: list, items, cap: int) -> None:
    for item in items:
        if item and item not in target and len(target) < cap:
            target.append(item)


def _sentence(text: str) -> str:
    return (text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")) if text else ""


def _limited(items: list[str], limit: int, more: str = "") -> str:
    shown = ", ".join(items[:limit])
    if len(items) > limit:
        shown += f", and {len(items) - limit} more" + (f" — {more}" if more else "")
    return shown


def _norm_name(value: str) -> str:
    return re.sub(r"[\[\]\"`]", "", str(value)).strip().lower()


def overview_name(json_name: str) -> str:
    """``x.lineage.json`` -> ``x.overview.md``."""
    base = Path(json_name).name
    for suffix in (".lineage.json", ".json"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base + ".overview.md"


def detailed_name(json_name: str) -> str:
    base = str(json_name)
    return (base[: -len(".json")] if base.endswith(".json") else base) + ".md"


# --------------------------------------------------------------------------- analysis

class _Agg:
    """What one object sequence (a route) collects: its ends and the steps on it."""
    __slots__ = ("ends", "steps", "gaps", "truncated")

    def __init__(self):
        self.ends: dict[str, dict] = {}       # end node id -> {"roles": set, "labels": list}
        self.steps: set[str] = set()
        self.gaps: set[str] = set()
        self.truncated = False

    def merge(self, other: "_Agg", role: str | None = None, labels=(), step: str | None = None, gaps=()):
        for node, info in other.ends.items():
            mine = self.ends.setdefault(node, {"roles": set(), "labels": []})
            roles = info["roles"] if role in (None, "value") else {role}
            mine["roles"] |= roles
            _add_unique(mine["labels"], list(labels) + info["labels"], END_LABEL_CAP)
        self.steps |= other.steps
        self.gaps |= other.gaps
        self.truncated = self.truncated or other.truncated
        if step:
            self.steps.add(step)
        self.gaps |= {g for g in gaps if g}


class Analysis:
    """Routes, ends, writers and row selection of one trace, computed once from its JSON."""

    def __init__(self, data: dict):
        self.data = data or {}
        self.t = t = _Trace(self.data)
        self.comp = self.data.get("completeness") or {}
        self.value_nodes, self.value_steps = t.reach(t.target_id, VALUE_ROLES)
        self.steps_by_id = {s["id"]: s for s in t.steps}
        self.target_entry = self.object_entry(t.target_id)
        self._labels: dict[str, str] = {}     # object or source key -> display label
        for node_id in sorted(t.nodes):
            entry = self.source_entry(node_id)
            if entry:
                self._labels.setdefault(entry[0], entry[1])
        self._memo: dict[str, dict] = {}
        self.cycle_hit = False
        self.routes = self._routes()
        self.ends = self._ends()
        self.writers = self._writers()

    # objects ------------------------------------------------------------------
    def object_entry(self, node_id: str | None) -> tuple[str, str] | None:
        """(key, label) of the object a node belongs to, or None (CTEs, derived tables, no object)."""
        node = self.t.nodes.get(node_id or "") or {}
        obj = node.get("object")
        if not obj:
            return None
        kind = obj.get("kind") or "table"
        name, scope = obj.get("name") or "?", obj.get("scope") or ""
        if kind in ("cte", "derived"):
            return None
        if kind == "package_flow":
            package, _, task = scope.partition("::")
            return f"package_flow:{scope}", f"data flow {task or '?'} (package {package or '?'})"
        if kind == "flat_file":
            return obj.get("key") or f"flat_file:{scope}::{name}", f"file connection {name} (package {scope})"
        if kind in ("temp_table", "table_variable"):
            where = scope.rsplit("::", 1)[-1]
            return obj.get("key") or f"{kind}:{scope}::{name}", f"{name} (temp table in {where})"
        label = ".".join(p for p in (obj.get("server"), obj.get("database") or "?", obj.get("schema") or "?", name) if p)
        return obj.get("key") or label.lower(), label

    def source_entry(self, node_id: str) -> tuple[str, str] | None:
        """The terminal source key of an end for the index: its object, or the parameter/variable itself."""
        entry = self.object_entry(node_id)
        if entry is not None:
            return entry
        kind = self.t.kind(node_id)
        if kind in ("parameter", "variable", "package_variable"):
            label = {"parameter": "procedure parameter", "variable": "variable",
                     "package_variable": "package variable or parameter"}[kind]
            return node_id, f"{label} {self.t.display(node_id)}"
        return None

    # routes -------------------------------------------------------------------
    def _routes_from(self, node_id: str, stack: set[str]) -> dict[tuple, _Agg]:
        if node_id in self._memo:
            return self._memo[node_id]
        t = self.t
        entry = self.object_entry(node_id)
        own = (entry[0],) if entry else ()
        steps = t.steps_by_target.get(node_id, [])
        if node_id in stack:
            self.cycle_hit = True
            agg = _Agg()
            agg.ends[node_id] = {"roles": {"value"}, "labels": [], "cycle": True}
            return {own: agg}
        if not steps:
            agg = _Agg()
            agg.ends[node_id] = {"roles": {"value"}, "labels": []}
            result = {own: agg}
            self._memo[node_id] = result
            return result
        stack.add(node_id)
        node_gaps = [g["id"] for g in t.gaps_by_node.get(node_id, [])]
        procedures = {s.get("procedure") for s in steps}
        result: dict[tuple, _Agg] = {}
        for step in steps:
            labels = [_truncate(b, LABEL_LIMIT) for b in step.get("branch_context") or []]
            if len(steps) > 1 and len(procedures) > 1:
                labels.append(_truncate(f"writer {_writer_label(step) or step.get('writer_kind')}", LABEL_LIMIT))
            inputs = [i for i in step.get("inputs") or [] if (i.get("role") or "value") in VALUE_ROLES]
            if not inputs:
                agg = result.setdefault(own, _Agg())
                agg.merge(_Agg(), labels=labels, step=step["id"], gaps=list(step.get("gaps") or []) + node_gaps)
                agg.ends.setdefault(node_id, {"roles": {"value"}, "labels": [], "no_inputs": True})
                continue
            for inp in inputs:
                child = self._routes_from(inp.get("node") or "?", stack)
                in_labels = labels + ([_truncate(inp["branch"], LABEL_LIMIT)] if inp.get("branch") else [])
                for seq, sub in child.items():
                    key = own + seq[1:] if own and seq[:1] == own else own + seq
                    result.setdefault(key, _Agg()).merge(
                        sub, role=inp.get("role") or "value", labels=in_labels, step=step["id"],
                        gaps=list(step.get("gaps") or []) + [inp.get("gap")] + node_gaps)
        stack.discard(node_id)
        if len(result) > ROUTE_CAP:
            ranked = sorted(result.items(), key=lambda kv: (-len(kv[1].ends), kv[0]))
            result = dict(ranked[:ROUTE_CAP - 1])
            rest = _Agg()
            for _, sub in ranked[ROUTE_CAP - 1:]:
                rest.merge(sub)
            rest.truncated = True
            result[own + ("…",)] = rest
        self._memo[node_id] = result
        return result

    def _routes(self) -> list[dict]:
        raw = self._routes_from(self.t.target_id, set())
        routes = []
        target_key = self.target_entry[0] if self.target_entry else None
        for seq, agg in raw.items():
            upstream = list(seq[1:] if target_key and seq[:1] == (target_key,) else seq)
            routes.append({"seq": list(seq), "upstream": upstream, "agg": agg,
                           "id": _hash("route", upstream) if upstream else "route-local"})
        routes.sort(key=lambda r: (-len(r["agg"].ends), r["seq"]))
        return routes

    def label_of(self, key: str) -> str:
        if key == "…":
            return "… (other routes, merged: see detailed page)"
        return self._labels.get(key, key)

    # ends ---------------------------------------------------------------------
    def end_kind(self, node_id: str) -> str:
        t = self.t
        stops = t.stops_by_node.get(node_id, [])
        if stops:
            stop = stops[0]
            if stop.get("declared_terminal") and stop.get("reason") not in ("boundary", "constant"):
                return "declared"
            return stop.get("reason") or "open"
        if t.gaps_by_node.get(node_id):
            return "gap"
        if t.kind(node_id) == "constant":
            return "constant"
        return "open"

    def _ends(self) -> list[dict]:
        t = self.t
        consumers: dict[str, list[tuple[dict, dict]]] = {}
        for step in t.steps:
            if step["id"] not in self.value_steps:
                continue
            for inp in step.get("inputs") or []:
                if (inp.get("role") or "value") in VALUE_ROLES:
                    consumers.setdefault(inp.get("node"), []).append((step, inp))
        leaves = sorted(n for n in self.value_nodes if not t.steps_by_target.get(n))
        out = []
        for node_id in leaves:
            statuses = set()
            for step, inp in consumers.get(node_id, []):
                statuses.add(max((step.get("status") or "verified", inp.get("status") or "verified"), key=_status_rank))
            if node_id == t.target_id:
                statuses.add(self.data.get("target_status") or "verified")
            steps = [s for s, _ in consumers.get(node_id, [])]
            out.append({"node": node_id, "kind": self.end_kind(node_id), "statuses": sorted(statuses, key=_status_rank),
                        "steps": sorted({s["id"] for s in steps}, key=lambda i: t.label.get(i, i)),
                        "roles": sorted({(i.get("role") or "value") for _, i in consumers.get(node_id, [])})})
        out.sort(key=lambda e: (END_ORDER.index(e["kind"]) if e["kind"] in END_ORDER else len(END_ORDER),
                                t.display(e["node"]).lower(), e["node"]))
        return out

    # writers ------------------------------------------------------------------
    def _writers(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for step in self.t.steps:
            if step["id"] in self.value_steps:
                entry = writer_entry(step, self.t)
                if entry:
                    found.setdefault(entry[0], entry[1])
        return dict(sorted(found.items()))

    def source_keys(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for end in self.ends:
            entry = self.source_entry(end["node"])
            if entry:
                found.setdefault(entry[0], entry[1])
        return dict(sorted(found.items()))


def _writer_label(step: dict) -> str | None:
    proc = step.get("procedure")
    if not proc:
        return None
    if proc.startswith("package "):
        return "package " + proc[len("package "):].split(", task ", 1)[0]
    return _norm_name(proc)


def writer_entry(step: dict, t: _Trace | None = None) -> tuple[str, str] | None:
    """(key, label) of the code unit that writes a step: procedure (by name and file), package, script
    or assertions file. Views, CTEs and derived tables are objects on the route, not writers."""
    ev = (step.get("evidence") or [{}])[0] or {}
    where = "/".join(p for p in (ev.get("root"), ev.get("path")) if p) or "?"
    if step.get("mapping_method") == "assertion" or ev.get("role") == "assertion":
        return f"assertion:{ev.get('path') or '?'}", f"assertions file {ev.get('path') or '?'} (asserted, not code)"
    proc = step.get("procedure")
    if proc and proc.startswith("package "):
        package = proc[len("package "):].split(", task ", 1)[0]
        return f"package:{package.lower()}", f"package {package}"
    if proc:
        return f"procedure:{_norm_name(proc)}@{where.lower()}", f"procedure {_norm_name(proc)}"
    if step.get("writer_kind") in ("view", "cte", "derived", "computed_column"):
        return None
    return f"script:{where.lower()}", f"script {where}"


# --------------------------------------------------------------------------- glossary

def load_glossary(path: str | Path) -> list[dict]:
    """Read a business glossary: {"columns": [{"column", "definition", "status", "source"}]}."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    items = data.get("columns") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError('expected {"columns": [{"column": ..., "definition": ..., "status": ..., "source": ...}]}')
    out = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict) or not item.get("column") or not item.get("definition"):
            raise ValueError(f"entry {index}: needs 'column' and 'definition'")
        status = str(item.get("status") or "proposed").strip().lower()
        if status not in ("approved", "proposed"):
            raise ValueError(f"entry {index}: status must be 'approved' or 'proposed', got {item.get('status')!r}")
        out.append({"column": _norm_name(item["column"]), "definition": str(item["definition"]).strip(),
                    "status": status, "source": str(item.get("source") or "").strip(), "order": index})
    return out


def glossary_entry(glossary: list[dict] | None, data: dict) -> tuple[dict | None, int]:
    """The best glossary entry for a trace's target and how many other entries also match. Exact names
    win over globs, approved over proposed, then file order."""
    if not glossary:
        return None, 0
    target = data.get("target") or {}
    obj = target.get("object") or {}
    if obj:
        parts = [obj.get("database") or "?", obj.get("schema") or "?", obj.get("name") or "?", target.get("name") or "?"]
    else:
        parts = _norm_name(data.get("requested") or "").split(".")
    parts = [_norm_name(p) for p in parts]
    matches = []
    for item in glossary:
        pattern = item["column"]
        n = len(pattern.split("."))
        if n > len(parts):
            continue
        exact = pattern == ".".join(parts[-n:]) and "?" not in parts[-n:]
        if exact or (any(ch in pattern for ch in "*?[") and _glob_match(pattern, parts[-n:])):
            matches.append((0 if exact else 1, 0 if item["status"] == "approved" else 1, item["order"], item))
    if not matches:
        return None, 0
    matches.sort(key=lambda m: m[:3])
    return matches[0][3], len(matches) - 1


def _glob_match(pattern: str, parts: list[str]) -> bool:
    """Part-by-part glob; an unknown part ('?', no database context) is matched only by '*'."""
    pattern_parts = pattern.split(".")
    for part, pat in zip(parts, pattern_parts):
        if part == "?" and pat != "*":
            return False
        if not fnmatch.fnmatchcase(part, pat):
            return False
    return True


# --------------------------------------------------------------------------- overview sections

def _end_text(a: Analysis, node_id: str, kind: str) -> str:
    """Why a route ends here, in plain words; never claims a confirmed source system."""
    t = a.t
    stop = (t.stops_by_node.get(node_id) or [{}])[0]
    if kind == "boundary":
        return "requested boundary reached (the run was asked to stop here; not traced beyond)"
    if kind == "declared":
        original = {"source_without_writer": "no writer in the selected sources",
                    "external_file": "external file", "parameter_without_caller": "parameter with no caller",
                    "max_depth": "depth limit"}.get(stop.get("reason"), _text(stop.get("reason") or "stop"))
        return (f"declared terminal for this run ({_text(stop.get('declared_terminal'))}); original reason: "
                f"{original}; how it is populated upstream is not proved")
    if kind == "source_without_writer":
        return "no writer in the selected sources — a stopping point; its origin is not proved"
    if kind == "external_file":
        return "external file read by a package — a stopping point, not a confirmed source system"
    if kind == "parameter_without_caller":
        return "procedure parameter with no caller in the scanned files — its value is not known"
    if kind == "max_depth":
        return "depth limit of the run reached — not traced further"
    if kind == "constant":
        return "constant value"
    if kind == "gap":
        kinds = sorted({g.get("kind") or "gap" for g in t.gaps_by_node.get(node_id, [])})
        return "open point: " + "; ".join(_text(GAP_TEXT.get(k, k)) for k in kinds) + " — not proved"
    return "not traced further — not proved"


def _end_short(kind: str) -> str:
    return {"boundary": "boundary reached", "declared": "declared terminal", "source_without_writer":
            "no writer in selected sources", "external_file": "external file", "parameter_without_caller":
            "parameter without caller", "max_depth": "depth limit", "constant": "constant",
            "gap": "open point", "cycle": "loop", "open": "not traced further"}.get(kind, kind)


def _node_short(a: Analysis, node_id: str) -> str:
    t = a.t
    if t.kind(node_id) == "constant":
        return "constant " + _code((t.nodes.get(node_id) or {}).get("name") or node_id.split(":", 1)[-1],
                                   CONSTANT_LIMIT)
    return _code(_truncate(t.display(node_id), NAME_LIMIT))


def _result_sentence(a: Analysis) -> str:
    comp, t = a.comp, a.t
    result = comp.get("result")
    counts = Counter(e["kind"] for e in a.ends)
    if not t.steps:
        if t.stops_by_node.get(t.target_id):
            kind = a.end_kind(t.target_id)
            return f"No step leads into this field in the selected sources: it is itself an end point ({_end_text(a, t.target_id, kind)})."
        return "No writer of this field could be established in the selected sources; its value is not traced."
    head = {"reached_requested_boundary": "Traced to the requested boundary",
            "complete_to_declared_boundary": "Traced to the boundary declared for this run",
            "complete_to_requested_depth": "Traced to the depth limit set for this run (some routes were cut and "
                                           "are not traced further)",
            "complete_within_selected_scope": "Traced within the selected sources"}.get(result)
    if comp.get("status") != "complete" or head is None:
        n_open = sum((comp.get("value_gaps_by_kind") or {}).values()) + sum((comp.get("context_gaps_by_kind") or {}).values())
        head = f"Not fully traced: {_plural(n_open, 'open point')} on the value routes (see Open points); " \
               "the routes below show what could be followed"
    parts = []
    if counts.get("boundary"):
        parts.append(f"{_plural(counts['boundary'], 'end point')} at the requested boundary (not traced beyond)")
    if counts.get("declared"):
        parts.append(f"{_plural(counts['declared'], 'end point')} at terminals declared for this run — how they "
                     "are populated upstream is not proved")
    if counts.get("source_without_writer"):
        n = counts["source_without_writer"]
        parts.append(f"{_plural(n, 'end point')} {'is an object' if n == 1 else 'are objects'} with no writer in "
                     f"those sources — {'its' if n == 1 else 'their'} origin is not proved")
    if counts.get("external_file"):
        n = counts["external_file"]
        parts.append(f"{_plural(n, 'end point')} {'is an external file read by a package (a stopping point' if n == 1 else 'are external files read by packages (stopping points'}"
                     f", not {'a confirmed source system' if n == 1 else 'confirmed source systems'})")
    if counts.get("parameter_without_caller"):
        n = counts["parameter_without_caller"]
        parts.append(f"{_plural(n, 'end point')} {'is a procedure parameter' if n == 1 else 'are procedure parameters'}"
                     " with no caller in the scanned files")
    if counts.get("max_depth"):
        parts.append(f"{_plural(counts['max_depth'], 'end point')} at the depth limit")
    if counts.get("constant"):
        parts.append(f"{_plural(counts['constant'], 'constant value')}")
    if counts.get("gap") or counts.get("open"):
        parts.append(f"{_plural(counts.get('gap', 0) + counts.get('open', 0), 'end point')} where proof stops")
    return head + ("; " + "; ".join(parts) if parts else "") + "."


def _section_field(a: Analysis) -> list[str]:
    t, comp, data = a.t, a.comp, a.data
    out = [f"- **Field:** {_code(t.display(t.target_id))}"
           + (f" (requested as {_code(data.get('requested'))})"
              if data.get("requested") and _norm_name(data["requested"]) != _norm_name(t.display(t.target_id)) else "")]
    out.append(f"- **Result:** {_result_sentence(a)}")
    asserted = comp.get("asserted_steps") or 0
    if asserted:
        out.append(f"- **Assertions:** relies on {_plural(asserted, 'asserted step')} — stated by a person in an "
                   "assertions file, not proved from code"
                   + (f" ({_plural(comp['gaps_resolved_by_assertions'], 'gap')} marked explained by them)"
                      if comp.get("gaps_resolved_by_assertions") else "") + ".")
    else:
        out.append("- **Assertions:** none used; nothing here rests on a person's statement.")
    statuses = comp.get("steps_by_status") or {}
    if statuses:
        total = sum(statuses.values())
        ordered = sorted(statuses.items(), key=lambda kv: _status_rank(kv[0]))
        text = ", ".join(f"{n} {STATUS_TEXT.get(s, s)}" for s, n in ordered)
        meanings = [STATUS_MEANING[s] for s, _ in ordered if s in STATUS_MEANING]
        out.append(f"- **Confidence:** {_plural(total, 'step')} on the value routes — {text}."
                   + (" " + _sentence("; ".join(meanings)) if meanings else ""))
    else:
        out.append("- **Confidence:** no mapping steps were proved.")
    rows = sum((comp.get("row_selection_gaps_by_kind") or {}).values())
    if rows:
        out.append(f"- **Row selection:** {_plural(rows, 'open point')} {'affects' if rows == 1 else 'affect'} "
                   "which rows are included, not the value (see Open points).")
    out.append("- End points are where this trace stopped. None of them is claimed as the system where the value "
               "originates.")
    return out


def _section_definition(entry: dict | None, others: int) -> list[str]:
    if entry is None:
        return ["Not confirmed — question for the source owner. (No glossary entry; the tool never derives "
                "business meaning from names.)"]
    source = f" Source: {_text(entry['source'])}." if entry.get("source") else ""
    extra = f" ({_plural(others, 'other glossary entry', 'other glossary entries')} also match.)" if others else ""
    if entry["status"] == "approved":
        return [f"{_text(entry['definition'])} *(approved)*.{source}{extra}"]
    return [f"Proposed (not approved): {_text(entry['definition'])}.{source}{extra}"]


def _route_labels(route: dict) -> tuple[list[str], dict[str, list[str]]]:
    """Labels shared by every end of a route, and each end's own remaining labels."""
    ends = route["agg"].ends
    common = None
    for info in ends.values():
        labels = set(info["labels"])
        common = labels if common is None else common & labels
    common = common or set()
    first = next(iter(ends.values()), {"labels": []})["labels"]
    shared = [label for label in first if label in common]
    own = {node: [label for label in info["labels"] if label not in common] for node, info in ends.items()}
    return shared, own


def _role_text(roles: set[str]) -> str:
    """How an end takes part: empty for a plain value contributor."""
    other = roles - {"value"}
    if not other:
        return ""
    what = "decides between values" if "condition" in other else "orders or partitions rows"
    return ("value, and " + what) if "value" in roles else what


def _section_routes(a: Analysis, detailed: str) -> list[str]:
    t = a.t
    routes = a.routes
    if not routes:
        return ["No route could be followed from this field."]
    out = [f"{_plural(len(routes), 'route')}: each is the sequence of objects the value passes, from this field "
           "back to where the trace ended (← reads \"comes from\"). Branch labels are the alternatives "
           "(IF/ELSE, CASE, MERGE, UNION, package paths) the route passes; they are listed, not combined. Exact "
           f"expressions are on the [detailed page]({detailed}#value-lineage)."]
    if a.cycle_hit:
        out[0] += " Some values feed each other (a loop); the loop is followed once."
    for index, route in enumerate(routes[:MAX_ROUTES], 1):
        agg = route["agg"]
        seq = [a.label_of(k) for k in route["seq"]] or [t.display(t.target_id)]
        if len(seq) > MAX_ROUTE_OBJECTS:
            hidden = len(seq) - (MAX_ROUTE_OBJECTS - 1)
            seq = seq[:3] + [f"… {hidden} more objects …"] + seq[-(MAX_ROUTE_OBJECTS - 4):]
        path = " ← ".join(_code(_truncate(s, NAME_LIMIT)) for s in seq)
        kinds = Counter(a.end_kind(n) if not info.get("cycle") else "cycle" for n, info in agg.ends.items())
        summary = ", ".join(f"{v} {_end_short(k)}" for k, v in sorted(kinds.items(), key=lambda kv: (
            END_ORDER.index(kv[0]) if kv[0] in END_ORDER else 99)))
        out += ["", f"**Route {index}** ({_plural(len(agg.ends), 'end')}: {summary}): {path}"]
        shared, own = _route_labels(route)
        if shared:
            out.append("- Branches on the way: " + "; ".join(_code(label) for label in shared[:MAX_LABELS])
                       + (f"; and {len(shared) - MAX_LABELS} more" if len(shared) > MAX_LABELS else ""))
        steps = [a.steps_by_id[s] for s in agg.steps if s in a.steps_by_id]
        ops = sorted({s.get("operation") for s in steps} - {None, "direct", "rename"},
                     key=lambda o: list(OPERATION_TEXT).index(o) if o in OPERATION_TEXT else 99)
        out.append("- Changes on the way: " + ("; ".join(_text(OPERATION_TEXT.get(o, o)) for o in ops)
                                                if ops else "none (copied or renamed only)"))
        ends = sorted(agg.ends.items(), key=lambda kv: (
            END_ORDER.index(a.end_kind(kv[0])) if a.end_kind(kv[0]) in END_ORDER else 99, t.display(kv[0]).lower()))
        named = []
        for node, info in ends[:MAX_ROUTE_ENDS]:
            text = _node_short(a, node)
            notes = []
            role = _role_text(info["roles"])
            if role:
                notes.append(role)
            labels = own.get(node, [])
            if labels:
                notes.append("via " + ", ".join(_code(label) for label in labels[:MAX_LABELS])
                             + (", …" if len(labels) > MAX_LABELS else ""))
            named.append(text + (f" ({'; '.join(notes)})" if notes else ""))
        more = f"; and {len(ends) - MAX_ROUTE_ENDS} more" if len(ends) > MAX_ROUTE_ENDS else ""
        out.append("- Ends: " + "; ".join(named) + more)
        statuses = Counter(s.get("status") or "verified" for s in steps)
        proof = ", ".join(f"{n} {STATUS_SHORT.get(s, s)}" for s, n in sorted(statuses.items(),
                                                                            key=lambda kv: _status_rank(kv[0])))
        writers = sorted({w[1] for w in (writer_entry(s) for s in steps) if w})
        line = f"- Proof: {proof or 'no steps'}"
        if writers:
            line += f"; written by {_limited([_code(w) for w in writers], MAX_WRITERS)}"
        open_kinds = sorted({t.gap_kind(g) for g in agg.gaps if g in t.gaps})
        if open_kinds:
            line += "; open points: " + ", ".join(_text(GAP_TEXT.get(k, k)) for k in open_kinds)
        if agg.truncated:
            line += "; some routes were merged here to keep the summary short"
        out.append(line)
    if len(routes) > MAX_ROUTES:
        rest = sum(len(r["agg"].ends) for r in routes[MAX_ROUTES:])
        out += ["", f"And {_plural(len(routes) - MAX_ROUTES, 'more route')} ({_plural(rest, 'end')}) — see the "
                    f"[detailed page]({detailed}#value-lineage)."]
    return out


def _section_rows(a: Analysis, detailed: str) -> list[str]:
    t = a.t
    by_object: dict[str, dict] = {}
    total = 0
    for step in t.ordered:
        if step["id"] not in a.value_steps:
            continue
        for rs in step.get("row_selection") or []:
            total += 1
            entry = a.object_entry(rs.get("node"))
            if entry is None and t.kind(rs.get("node")) in ("local_column",):
                entry = ("local", "columns computed inside the statement")
            key, label = entry or ("other", "variables, parameters and other values")
            item = by_object.setdefault(key, {"label": label, "conditions": [], "gaps": 0})
            role = ROW_TEXT.get(rs.get("role"), rs.get("role") or "?")
            text = f"{_text(role)} {_code(rs.get('expression'), CONDITION_LIMIT)}"
            if text not in item["conditions"]:
                item["conditions"].append(text)
            if rs.get("gap"):
                item["gaps"] += 1
    if not by_object:
        return ["No row-selection conditions were recorded on the value routes: every row of the sources shown "
                "above can contribute."]
    out = [f"These conditions decide **which rows** are included; they do not compute the value. "
           f"{_plural(total, 'condition')} on {_plural(len(by_object), 'object')}:", ""]
    ordered = sorted(by_object.items(), key=lambda kv: (-len(kv[1]["conditions"]), kv[1]["label"].lower()))
    for _, item in ordered[:MAX_ROW_OBJECTS]:
        conds = item["conditions"]
        line = f"- {_code(_truncate(item['label'], NAME_LIMIT))}: " + "; ".join(conds[:MAX_ROW_PREDICATES])
        if len(conds) > MAX_ROW_PREDICATES:
            line += f"; and {len(conds) - MAX_ROW_PREDICATES} more"
        if item["gaps"]:
            line += f" — ⚠ {_plural(item['gaps'], 'condition')} not fully resolved"
        out.append(line)
    if len(ordered) > MAX_ROW_OBJECTS:
        out.append(f"- and {_plural(len(ordered) - MAX_ROW_OBJECTS, 'more object')} — see the "
                   f"[detailed page]({detailed}#row-selection)")
    return out


def _endpoint_cells(a: Analysis, node_id: str) -> tuple[str, str, str, str]:
    t = a.t
    node = t.nodes.get(node_id) or {}
    obj = node.get("object") or {}
    kind = node.get("kind")
    name = node.get("name") or node_id
    if kind == "constant":
        return "(constant)", "—", "—", _code(name, CONSTANT_LIMIT)
    if obj.get("kind") == "flat_file":
        return "(file connection)", "—", _code(f"{obj.get('name')} (package {obj.get('scope')})", NAME_LIMIT), _code(name)
    if obj.get("kind") == "package_flow":
        package, _, task = (obj.get("scope") or "").partition("::")
        return "(package data flow)", "—", _code(f"{obj.get('name')} ({task}, package {package})", NAME_LIMIT), _code(name)
    if obj.get("kind") in ("temp_table", "table_variable"):
        return "(temp table)", "—", _code(f"{obj.get('name')} (in {(obj.get('scope') or '').rsplit('::', 1)[-1]})",
                                          NAME_LIMIT), _code(name)
    if obj:
        db = obj.get("database")
        server = f"{obj['server']}." if obj.get("server") else ""
        return (_code(server + db) if db else "unknown"), _code(obj.get("schema") or "?"), \
            _code(obj.get("name") or "?"), _code(name)
    label = {"parameter": "(procedure parameter)", "variable": "(variable)",
             "package_variable": "(package parameter)" if name.startswith("$") or "::$" in node_id
             else "(package variable)"}.get(kind, f"({_text(kind or 'value')})")
    return label, "—", _code(_truncate(t.display(node_id), NAME_LIMIT)), "—"


def _section_endpoints(a: Analysis, detailed: str) -> list[str]:
    t = a.t
    if not a.ends:
        return ["No end points were recorded."]
    out = ["| Database | Schema | Table / source | Column | Why it stops | Proof of the step reaching it |",
           "|---|---|---|---|---|---|"]
    for end in a.ends[:MAX_ENDPOINTS]:
        cells = list(_endpoint_cells(a, end["node"]))
        why = _end_text(a, end["node"], end["kind"])
        role = _role_text(set(end["roles"]))
        if role:
            why += f" ({role})"
        proof = ", ".join(STATUS_SHORT.get(s, s) for s in end["statuses"]) or "—"
        if end["steps"]:
            proof += " (" + ", ".join(f"[{t.label[s]}]({detailed}#{_anchor(s)})" for s in end["steps"][:2]) \
                     + (", …" if len(end["steps"]) > 2 else "") + ")"
        out.append("| " + " | ".join(_cell(c) for c in cells + [why, proof]) + " |")
    if len(a.ends) > MAX_ENDPOINTS:
        kinds = Counter(e["kind"] for e in a.ends[MAX_ENDPOINTS:])
        out += ["", f"And {len(a.ends) - MAX_ENDPOINTS} more end points ("
                + ", ".join(f"{v} {_end_short(k)}" for k, v in sorted(kinds.items())) + f") — see the "
                f"[detailed page]({detailed}#gaps) and the JSON."]
    return out


def _section_open(a: Analysis, detailed: str) -> list[str]:
    t = a.t
    gaps = t.sorted_gaps()
    open_gaps = [g for g in gaps if g.get("affects", "value") != "row_selection"]
    row_gaps = [g for g in gaps if g.get("affects") == "row_selection"]
    out = []
    if not open_gaps:
        out.append("No open points on the value routes.")
    else:
        by_kind: dict[str, list[dict]] = {}
        for gap in open_gaps:
            by_kind.setdefault(gap.get("kind") or "gap", []).append(gap)
        ordered = sorted(by_kind.items(), key=lambda kv: (0 if any(g.get("affects") == "value" for g in kv[1]) else 1,
                                                           -len(kv[1]), kv[0]))
        for kind, items in ordered[:MAX_OPEN]:
            places = []
            for gap in items:
                if gap.get("node"):
                    place = _code(_truncate(t.display(gap["node"]), NAME_LIMIT))
                else:
                    ev = (gap.get("evidence") or [None])[0]
                    place = _code(t.location(ev)) if ev else "procedure or statement level"
                link = f"[{place}]({detailed}#{_anchor(gap['id'])})"
                if link not in places:
                    places.append(link)
            scope = "value" if any(g.get("affects") == "value" for g in items) else "context around the value"
            out.append(f"- **{_text(GAP_TEXT.get(kind, kind))}** ({_plural(len(items), 'place')}, affects {scope}): "
                       + _limited(places, MAX_PLACES))
            out.append(f"  - Next evidence: {_text(NEXT_EVIDENCE.get(kind, 'manual review of the cited statement'))}")
        if len(ordered) > MAX_OPEN:
            rest = sum(len(v) for _, v in ordered[MAX_OPEN:])
            out.append(f"- and {_plural(rest, 'more open point')} of {_plural(len(ordered) - MAX_OPEN, 'other kind')} "
                       f"— see the [detailed page]({detailed}#gaps)")
    if row_gaps:
        kinds = Counter(g.get("kind") or "gap" for g in row_gaps)
        out += ["", f"Row selection: {_plural(len(row_gaps), 'open point')} ("
                + ", ".join(f"{_text(GAP_TEXT.get(k, k))}: {v}" for k, v in sorted(kinds.items()))
                + f") {'affects' if len(row_gaps) == 1 else 'affect'} which rows are included, not the value — see the "
                f"[detailed page]({detailed}#gaps)."]
    return out


def _section_links(a: Analysis, json_name: str, detailed: str, index: dict | None) -> list[str]:
    _, short, tree, rows, steps, gaps = SECTION_ANCHORS  # the detailed page's section anchors
    out = [f"- Detailed page: [{_text(Path(detailed).name)}]({detailed}) — "
           f"[short answer]({detailed}#{short}), [value tree]({detailed}#{tree}), "
           f"[row selection]({detailed}#{rows}), [steps and evidence]({detailed}#{steps}), "
           f"[gaps and next evidence]({detailed}#{gaps})",
           f"- Full trace (source of truth): [{_text(Path(json_name).name)}]({_link_target(json_name)})"]
    if index:
        link = index["link"]
        def peers(n: int) -> str:
            return f"shared with {_plural(n, 'other column')}" if n else "not shared"
        out.append(f"- Route index: [{_text(Path(link).name)}]({link}) — terminal sources "
                   f"{_code(index['source_signature'])} ({peers(index['source_peers'])}), "
                   f"writers {_code(index['writer_signature'])} ({peers(index['writer_peers'])}). "
                   "Shared signatures do not mean identical expressions, branches or meaning.")
    return out


def render_overview(data: dict, json_name: str | None = None, glossary: list[dict] | None = None,
                    index: dict | None = None, analysis: Analysis | None = None,
                    detailed_page: str | None = None) -> str:
    """Render the overview page of one trace (``TraceResult.to_dict()``). ``json_name`` is the trace JSON path
    relative to the page; the detailed page is ``detailed_page``, by default the JSON path with ``.json``
    replaced by ``.md``. ``index`` is this column's entry in the route index with a ``link`` to it."""
    a = analysis or Analysis(data or {})
    json_name = json_name or "trace.lineage.json"
    detailed = _link_target(detailed_page or detailed_name(json_name))
    entry, others = glossary_entry(glossary, a.data)
    out = [f"# Column overview: {_code(a.t.display(a.t.target_id))}", ""]
    sections = (
        ("Field and result", _section_field(a)),
        ("Business definition", _section_definition(entry, others)),
        ("Routes: how the value arrives", _section_routes(a, detailed)),
        ("Rows included", _section_rows(a, detailed)),
        ("End points", _section_endpoints(a, detailed)),
        ("Open points", _section_open(a, detailed)),
        ("Links", _section_links(a, json_name, detailed, index)),
    )
    for title, body in sections:
        out += [f"## {title}", ""] + body + [""]
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- route index

def build_index(items: list[dict]) -> dict:
    """Route index JSON from ``[{"data": trace dict, "overview": link, "trace": link, "analysis": Analysis?}]``."""
    columns, sources, writers, routes = [], {}, {}, {}
    labels: dict[str, dict[str, str]] = {"objects": {}, "writers": {}}   # key -> label, stored once
    for item in items:
        a = item.get("analysis") or Analysis(item["data"])
        data = a.data
        comp = a.comp
        src = a.source_keys()
        wr = a.writers
        src_id, wr_id = _hash("src", list(src)), _hash("wr", list(wr))
        requested = data.get("requested") or a.t.target_id
        result = comp.get("result") or comp.get("status") or "unknown"
        route_ids = []
        for route in a.routes:
            for k in route["upstream"]:
                labels["objects"].setdefault(k, a.label_of(k))
            entry = routes.setdefault(route["id"], {"id": route["id"], "objects": route["upstream"], "columns": []})
            if requested not in entry["columns"]:
                entry["columns"].append(requested)
            if route["id"] not in route_ids:
                route_ids.append(route["id"])
        for registry, key, members, sig in ((sources, "objects", src, src_id), (writers, "writers", wr, wr_id)):
            for k, v in members.items():
                labels[key].setdefault(k, v)
            group = registry.setdefault(sig, {"id": sig, key: list(members), "columns": [], "results": Counter()})
            group["columns"].append(requested)
            group["results"][result] += 1
        columns.append({"column": requested, "target": a.t.target_id, "result": result,
                        "status": comp.get("status"), "relies_on_assertions": bool(comp.get("relies_on_assertions")),
                        "overview": item.get("overview"), "trace": item.get("trace"),
                        "source_signature": src_id, "writer_signature": wr_id,
                        "route_signatures": sorted(route_ids), "ends": len(a.ends),
                        "terminal_sources": len(src), "writers": len(wr)})
    columns.sort(key=lambda c: (c["column"].lower(), c["column"]))

    def finish(registry):
        groups = []
        for group in registry.values():
            group["columns"] = sorted(group["columns"], key=str.lower)
            group["results"] = dict(sorted(group["results"].items()))
            groups.append(group)
        return sorted(groups, key=lambda g: (-len(g["columns"]), g["id"]))
    for route in routes.values():
        route["columns"].sort(key=str.lower)
    return {
        "schema_version": INDEX_SCHEMA,
        "note": "Columns grouped by the set of terminal source objects of their value-route ends (files grouped "
                "by connection; constants left out) and by the set of writer procedures, packages and scripts "
                "on their value routes. A shared signature does not mean identical expressions, branch "
                "selection or business meaning, and no group replaces a column's own trace: each column's "
                "JSON and detailed page list all of its contributors and exceptions. End points are where "
                "the traces stopped, not confirmed source systems. Object and writer keys are explained in "
                "'objects' and 'writers'; a route signature is the sequence of objects upstream of the target's "
                "own object.",
        "objects": dict(sorted(labels["objects"].items())),
        "writers": dict(sorted(labels["writers"].items())),
        "columns": columns,
        "source_signatures": finish(sources),
        "writer_signatures": finish(writers),
        "route_signatures": sorted(routes.values(), key=lambda r: (-len(r["columns"]), r["id"])),
    }


def index_entry_for(index: dict, column: str, link: str) -> dict | None:
    """What an overview page says about its column's place in the index."""
    row = next((c for c in index.get("columns", []) if c["column"] == column), None)
    if row is None:
        return None
    size = {g["id"]: len(g["columns"]) for g in index["source_signatures"] + index["writer_signatures"]}
    return {"link": link, "source_signature": row["source_signature"], "writer_signature": row["writer_signature"],
            "source_peers": size.get(row["source_signature"], 1) - 1,
            "writer_peers": size.get(row["writer_signature"], 1) - 1}


def render_index(index: dict) -> str:
    cols = {c["column"]: c for c in index["columns"]}

    def member(name: str) -> str:
        row = cols.get(name) or {}
        link = row.get("overview") or row.get("trace")
        return f"[{_text(name)}]({_link_target(link)})" if link else _code(name)

    def results(group: dict) -> str:
        return ", ".join(f"{_text(k)}: {v}" for k, v in group["results"].items())

    out = ["# Route index", "",
           f"{_plural(len(index['columns']), 'column')}; {_plural(len(index['source_signatures']), 'terminal-source signature')}; "
           f"{_plural(len(index['writer_signatures']), 'writer signature')}. Machine-readable form: "
           "[routes-index.json](routes-index.json).", "",
           "> A shared signature means the columns end at the same set of objects (or pass the same set of "
           "writers). It does **not** mean identical expressions, branch selection or business meaning. Each "
           "column's own overview, detailed page and JSON stay complete: they list all of its contributors and "
           "exceptions. End points are where the traces stopped, not confirmed source systems.", ""]
    for title, key, field, noun in (("Grouped by terminal source objects", "source_signatures", "objects", "object"),
                                    ("Grouped by writer procedures and packages", "writer_signatures", "writers", "writer")):
        out += [f"## {title}", ""]
        for group in index[key]:
            shared = [_code(_truncate(index[field].get(k, k), NAME_LIMIT)) for k in group[field]]
            out += [f"### {group['id']} — {_plural(len(group['columns']), 'column')}", ""]
            out.append(f"- {noun.capitalize()}s ({len(shared)}): "
                       + (_limited(shared, INDEX_MAX_SHARED, "see routes-index.json") if shared
                          else ("none (constants only)" if noun == "object" else "none (views only, or no steps)")))
            out.append(f"- Results: {results(group)}")
            out.append("- Columns: " + ", ".join(member(c) for c in group["columns"]))
            out.append("")
    out += ["## Columns", "", "| Column | Result | Terminal sources | Writers | Routes |", "|---|---|---|---|---|"]
    for row in index["columns"]:
        out.append("| " + " | ".join(_cell(c) for c in (
            member(row["column"]), _text(row["result"]), f"{_code(row['source_signature'])} ({row['terminal_sources']})",
            f"{_code(row['writer_signature'])} ({row['writers']})", str(len(row["route_signatures"])))) + " |")
    return "\n".join(out) + "\n"


def write_index(index: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md, js = out_dir / "routes-index.md", out_dir / "routes-index.json"
    with open(md, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_index(index))
    with open(js, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(index, indent=2, ensure_ascii=False) + "\n")
    return [md, js]


def index_counts(index: dict) -> dict:
    """Name-free counts for gap-summary.json."""
    sizes = lambda key: sorted((len(g["columns"]) for g in index[key]), reverse=True)  # noqa: E731
    return {"columns": len(index["columns"]),
            "source_signatures": len(index["source_signatures"]), "writer_signatures": len(index["writer_signatures"]),
            "route_signatures": len(index["route_signatures"]),
            "largest_source_groups": sizes("source_signatures")[:5],
            "largest_writer_groups": sizes("writer_signatures")[:5]}


def write_outputs(entries: list[tuple[dict, Path]], out_dir: Path | None = None, index_dir: Path | None = None,
                  glossary: list[dict] | None = None, overviews: bool = True) -> tuple[list[Path], dict | None]:
    """Write overview pages (next to each trace JSON, or in ``out_dir``) and, with ``index_dir``, the route
    index. ``entries`` are (trace dict, trace JSON path)."""
    written: list[Path] = []
    prepared = []
    for data, json_path in entries:
        page_dir = (out_dir or json_path.parent).resolve()
        page = page_dir / overview_name(json_path.name)
        prepared.append((data, json_path.resolve(), page, Analysis(data)))
    index = None
    if index_dir is not None:
        index_dir = index_dir.resolve()

        def rel(path: Path) -> str:
            return os.path.relpath(path, index_dir).replace(os.sep, "/")
        index = build_index([{"data": d, "analysis": a, "trace": rel(j),
                              "overview": rel(p) if overviews else rel(j.with_name(detailed_name(j.name)))}
                             for d, j, p, a in prepared])
        written += write_index(index, index_dir)
    if overviews:
        for data, json_path, page, analysis in prepared:
            page.parent.mkdir(parents=True, exist_ok=True)
            json_name = os.path.relpath(json_path, page.parent).replace(os.sep, "/")
            entry = None
            if index is not None:
                link = os.path.relpath(index_dir / "routes-index.md", page.parent).replace(os.sep, "/")
                entry = index_entry_for(index, data.get("requested") or analysis.t.target_id, _link_target(link))
            with open(page, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(render_overview(data, json_name, glossary, entry, analysis))
            written.append(page)
    return written, index


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m column_lineage.render_overview",
                                     description="Render overview pages and a route index from trace JSON files.")
    parser.add_argument("traces", nargs="+", help="<stem>.lineage.json trace files")
    parser.add_argument("--out-dir", help="directory for the overview pages (default: next to each JSON)")
    parser.add_argument("--index-out", metavar="DIR", help="also write routes-index.md and routes-index.json here")
    parser.add_argument("--business-glossary", metavar="FILE", help="glossary JSON of business definitions")
    parser.add_argument("--no-overview", action="store_true", help="write only the route index")
    args = parser.parse_args(argv)
    glossary = None
    if args.business_glossary:
        try:
            glossary = load_glossary(args.business_glossary)
        except (OSError, ValueError) as exc:
            print(f"error: --business-glossary: {exc}", file=sys.stderr)
            return 2
    entries = []
    for name in args.traces:
        path = Path(name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"error: cannot read trace {path}: {exc}", file=sys.stderr)
            return 2
        if not isinstance(data, dict) or "completeness" not in data:
            print(f"error: {path} does not contain a column lineage trace", file=sys.stderr)
            return 2
        entries.append((data, path))
    if args.no_overview and not args.index_out:
        parser.error("--no-overview needs --index-out")
    written, _ = write_outputs(entries, Path(args.out_dir) if args.out_dir else None,
                               Path(args.index_out) if args.index_out else None, glossary, not args.no_overview)
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
