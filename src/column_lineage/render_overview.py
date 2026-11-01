"""Reader-facing outputs rendered from trace JSON: a short overview page per column, route families and a
run-level route index.

All of them read only the JSON form of traces (``TraceResult.to_dict()``), like the detailed page::

    python -m column_lineage.render_overview trace.lineage.json [...] [--out-dir DIR] [--index-out DIR]
                                             [--business-glossary FILE]

Overview page (``<stem>.overview.md``), for a reader who does not read SQL. The first screen answers "what
feeds this field, where does proof stop, and what is assumed":

1. Field and definition (glossary definition with its approval status; never derived from names)
2. Plain answer (one or two generated sentences: main source families and value rules)
3. Coverage (routes, families, boundary / declared / unresolved ends, step statuses)
4. Main evidence needed (at most three requests, by routes affected)
5. Route families (a small Mermaid diagram and a family table)
6. Sources at a glance (database | schema | table | column per end; files and constants apart)
7. What changes the value (CASE, fallbacks, overrides, merges, aggregations, package transformations)
8. Which rows? (row selection, kept apart from the value)
9. Evidence and uncertainty
10. Routes, open points and links (shortened)

Definitions:

- A *value route* is a path from the target through value, condition or window inputs to an end, summarised
  by its end, the sequence of distinct objects it passes (tables, views, temp tables, package data flows,
  files; CTEs and derived tables are inside one statement and are not listed), its value rule and its
  evidence status.
- A *route family* groups value routes that agree on all four of: source family (database.schema, file
  connection, constants, variables of one procedure, ...), value rule (the set of rule tags met on the way:
  CASE branch or default, COALESCE/ISNULL primary or fallback n, decides between values, override by a later
  UPDATE, MERGE, IF/ELSE or UNION branch, package mapping, conversions and other calculations), stopping
  reason (boundary, declared terminal, no writer, external file, parameter, depth, constant, open point kinds,
  loop) and evidence status (worst step status on the way; partial or ambiguous when an open point lies on
  the route). Family ids (``fam-<hash>``) hash those four, so they are stable across runs and columns.
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

from .render_markdown import (NEXT_EVIDENCE, SECTION_ANCHORS, _anchor, _cell, _code, _link_target, _one_line,
                              _plural, _status_rank, _text, _Trace, _truncate, assertion_provenance_text)

INDEX_SCHEMA = "column-lineage-routes/1.0"
FAMILY_SCHEMA = "column-lineage-route-families/1.0"
VALUE_ROLES = ("value", "condition", "window")

MAX_ROUTES = 2               # object-sequence routes shown on the overview
MAX_ROUTE_OBJECTS = 7        # objects shown per route before the middle is elided
MAX_ROUTE_ENDS = 4           # ends named per route
MAX_LABELS = 3               # branch labels shown per route or end
MAX_WRITERS = 3              # writers named per route
MAX_ENDPOINTS = 8            # rows of the sources table (all are on the detailed page)
MAX_ROW_OBJECTS = 4          # objects in "Which rows?"
MAX_ROW_PREDICATES = 2       # conditions shown per object (the detailed page has all)
MAX_OPEN = 3                 # open-point kinds shown
MAX_PLACES = 3               # places named per open-point kind
MAX_EVIDENCE = 3             # "Main evidence needed" bullets
MAX_DIAGRAM_FAMILIES = 10    # families drawn in the Mermaid diagram
MAX_FAMILY_ROWS = 10         # rows of the family table
MAX_CHANGES = 6              # "What changes the value" bullets
MAX_PER_CHANGE = 2           # bullets per kind of change
MAX_ANSWER_SOURCES = 4       # source families named in the plain answer
FAMILY_ROUTE_SEQS = 50       # object sequences kept per (end, rule, stop, status) while enumerating families
DETAIL_FAMILY_ROUTES = 25    # member routes listed per family on the detailed page
LABEL_LIMIT = 60             # characters of a branch label
CONDITION_LIMIT = 90         # characters of a row-selection condition
CONSTANT_LIMIT = 30          # characters of a constant shown as a value
NAME_LIMIT = 200             # characters of an object or column name (tables keep identifiers whole)
DIAGRAM_NAME_LIMIT = 110     # characters of a name inside a diagram node
DIAGRAM_RULE_LIMIT = 140     # characters of an edge label (the family table has the full rule)
ROUTE_CAP = 400              # distinct object sequences kept per node while enumerating routes
END_LABEL_CAP = 6            # branch labels kept per end while enumerating routes
INDEX_MAX_SHARED = 25        # shared objects / writers listed per group in routes-index.md
INDEX_MAX_ATTENTION = 10     # rows per "Needs attention" list in routes-index.md

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
    "asserted": "asserted (recorded in an assertions file, not proved from code)",
    "partial": "partial (some inputs not proved)",
    "ambiguous": "ambiguous (candidates, none proved)",
    "unresolved": "unresolved",
}
FAMILY_STATUS_TEXT = {
    "verified": "verified (proved from code)",
    "inferred": "inferred (rule-based, not proved by explicit code)",
    "asserted": "asserted (recorded in an assertions file, not proved from code)",
    "partial": "partial (an open point or unproved input on the way)",
    "ambiguous": "ambiguous (candidates or run order not proved)",
    "unresolved": "unresolved (not proved)",
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
    "asserted": "asserted = a mapping recorded in an assertions file (AI-assisted or by a person; see the assertion), not proved from code",
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
GAP_SHORT = {
    "dynamic_sql": "dynamic SQL", "parse_error": "unreadable statement", "unsupported_statement": "unsupported statement",
    "insert_exec": "INSERT EXEC", "missing_definition": "definition missing", "ambiguous_writer_order":
    "writer order unknown", "unsupported_package_step": "unsupported package step", "package_runtime_value":
    "package run-time value", "unresolved_variable": "variable value unknown", "opaque_function": "function body unknown",
    "external_object": "external object", "ambiguous_column": "ambiguous column", "ambiguous_object": "ambiguous object",
}
END_ORDER = ("boundary", "declared", "source_without_writer", "external_file", "parameter_without_caller",
             "max_depth", "constant", "gap", "earlier", "cycle", "open")

# Value-rule tags of a route family, in display order: (short label, phrase).
RULE_TAGS = {
    "assertion": ("asserted mapping", "an asserted mapping (recorded in an assertions file, not code)"),
    "override": ("override (later UPDATE)", "overwritten by an UPDATE that runs after the other writers"),
    "override_unordered": ("UPDATE (run order not proved)",
                           "an UPDATE that may overwrite other writers (run order not proved)"),
    "update": ("UPDATE", "set by an UPDATE"),
    "merge_update": ("MERGE when matched", "a MERGE update of matched rows"),
    "merge_insert": ("MERGE when not matched", "a MERGE insert of new rows"),
    "if": ("IF branch", "written only when an IF condition or package precondition holds"),
    "else": ("ELSE branch", "written in the ELSE branch of an IF"),
    "loop": ("inside a loop", "written inside a WHILE loop"),
    "catch": ("CATCH block", "written in a CATCH block"),
    "union": ("UNION branch", "one branch of a UNION or package union"),
    "split": ("conditional split output", "one output of a package conditional split"),
    "lookup_output": ("lookup output", "one output of a package lookup"),
    "error_output": ("error output", "a package error output"),
    "package": ("package mapping", "mapped by a package data flow"),
    "case_when": ("CASE branch", "a CASE/IIF branch (WHEN ... THEN)"),
    "case_else": ("CASE default", "the CASE/IIF default (ELSE)"),
    "primary": ("primary", "the first COALESCE/ISNULL argument"),
    "fallback": ("fallback", "a COALESCE/ISNULL fallback, used when earlier arguments are null"),
    "condition": ("decides between values", "a condition that decides between values (not a value source)"),
    "window": ("orders or partitions rows", "orders or partitions rows of a window calculation"),
    "aggregate": ("aggregated", "aggregated over rows"),
    "retained": ("rows not updated keep earlier content",
                 "kept from the rows' earlier content (rows an UPDATE does not match)"),
    "conditional": ("conditional expression", "a conditional expression"),
    "conversion": ("type conversion", "a type conversion"),
    "arithmetic": ("arithmetic", "arithmetic"),
    "function": ("function", "a function"),
    "string": ("text manipulation", "text manipulation"),
    "window_calc": ("window calculation", "a window calculation"),
    "lookup": ("subquery lookup", "a subquery lookup"),
    "calculation": ("calculation", "another calculation"),
    "branch": ("other branch", "another conditional branch"),
}
TAG_ORDER = list(RULE_TAGS)
HEADLINE_ORDER = ("assertion", "override", "override_unordered", "merge_update", "merge_insert", "fallback",
                  "primary", "case_when", "case_else", "condition", "window", "package", "union", "split",
                  "lookup_output", "error_output", "if", "else", "loop", "update", "aggregate")
OP_TAG = {"cast": "conversion", "arithmetic": "arithmetic", "function": "function", "aggregate": "aggregate",
          "string": "string", "window": "window_calc", "subquery": "lookup", "expression": "calculation"}
STOP_SHORT = {
    "boundary": "requested boundary, not traced beyond",
    "declared": "declared terminal, upstream not proved",
    "source_without_writer": "upstream writer unknown",
    "external_file": "file producer unknown",
    "parameter_without_caller": "no caller found",
    "max_depth": "depth limit reached",
    "constant": "constant",
    "cycle": "earlier content before this write",
    "open": "not traced further",
}
SHAPES = {   # diagram node kind -> (open, close, label prefix)
    "database": ('[("', '")]', "DB column"),
    "file": ('[/"', '"/]', "File column"),
    "constant": ('(["', '"])', "Constant"),
    "declared": ('[["', '"]]', "Declared terminal"),
    "unresolved": ('{{"', '"}}', "Unresolved origin"),
    "loop": ('(("', '"))', "Loop, no new source"),
    "earlier": ('(("', '"))', "Before this write"),
}


# --------------------------------------------------------------------------- small helpers

def _hash(prefix: str, items) -> str:
    raw = json.dumps(sorted(items), ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:10]}"


def _hash_list(prefix: str, items: list) -> str:
    raw = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:10]}"


def _worst(*statuses: str | None) -> str:
    present = [s for s in statuses if s]
    return max(present, key=_status_rank) if present else "verified"


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


def _and_list(items: list[str], limit: int = 4) -> str:
    if len(items) > limit:
        return ", ".join(items[:limit]) + f" and {len(items) - limit} more"
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _norm_name(value: str) -> str:
    return re.sub(r"[\[\]\"`]", "", str(value)).strip().lower()


def _tag_rank(tag: str) -> tuple[int, int]:
    base, _, number = tag.partition(":")
    return (TAG_ORDER.index(base) if base in TAG_ORDER else len(TAG_ORDER), int(number) if number.isdigit() else 0)


def _tags(items) -> tuple[str, ...]:
    return tuple(sorted({i for i in items if i}, key=_tag_rank))


def tag_short(tag: str) -> str:
    base, _, number = tag.partition(":")
    short = RULE_TAGS.get(base, (base.replace("_", " "),))[0]
    return f"{short} {number}" if number else short


def rule_text(tags) -> str:
    """The value rule of a family in a few words, e.g. ``CASE default, primary``."""
    return ", ".join(tag_short(t) for t in tags) if tags else "direct copy"


def headline(tags) -> str:
    bases = {t.partition(":")[0] for t in tags}
    for tag in HEADLINE_ORDER:
        if tag in bases:
            return RULE_TAGS[tag][0]
    return "direct"


def stop_short(stop: str) -> str:
    if stop.startswith("gap:"):
        kinds = stop[4:].split(",")
        return "open point: " + ", ".join(GAP_SHORT.get(k, k.replace("_", " ")) for k in kinds)
    return STOP_SHORT.get(stop, stop.replace("_", " "))


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


def _context_tag(label: str) -> str | None:
    """Rule tag of one ``branch_context`` label of a step."""
    if label.startswith("ELSE (of IF"):
        return "else"
    if label.startswith("IF "):
        return "if"
    if label.startswith("WHILE "):
        return "loop"
    if label == "CATCH":
        return "catch"   # runs only on error; a TRY block does not change the value
    if label == "TRY":
        return None
    if re.match(r"(UNION ALL|UNION|EXCEPT|INTERSECT) branch \d+ of \d+$", label):
        return "union"
    if label.startswith(("Union All ", "Merge ")) and " input " in label:
        return "union"
    if label.startswith("Conditional Split "):
        return "split"
    if label.startswith("Lookup "):
        return "lookup_output"
    if label.startswith("error output "):
        return "error_output"
    if label.startswith(("WHEN MATCHED", "WHEN NOT MATCHED", "VALUES row ", "writer ")):
        return None   # MERGE clauses are covered by the writer kind; VALUES rows are constants
    return "branch"


def _input_tags(inp: dict) -> list[str]:
    """Rule tags of one input: its role and the CASE / COALESCE branch it sits in."""
    role = inp.get("role") or "value"
    if role == "condition":
        return ["condition"]
    if role == "window":
        return ["window"]
    tags = []
    for part in (inp.get("branch") or "").split(" / "):
        part = part.strip()
        match = re.fullmatch(r"(?:COALESCE|ISNULL) argument (\d+)", part)
        if match:
            tags.append("primary" if match.group(1) == "1" else f"fallback:{match.group(1)}")
        elif part.endswith(" THEN") and part.startswith(("WHEN ", "IIF ")):
            tags.append("case_when")
        elif part == "ELSE" or (part.startswith("IIF ") and part.endswith(" ELSE")):
            tags.append("case_else")
    return tags


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


def _fam_add(result: dict, key: tuple, seqs, steps, gaps, labels, cut: bool = False) -> None:
    entry = result.get(key)
    if entry is None:
        entry = result[key] = {"seqs": set(), "steps": set(), "gaps": set(), "labels": [], "cut": False}
    for seq in sorted(seqs):
        if seq in entry["seqs"]:
            continue
        if len(entry["seqs"]) >= FAMILY_ROUTE_SEQS:
            entry["cut"] = True
            break
        entry["seqs"].add(seq)
    entry["steps"] |= steps
    entry["gaps"] |= {g for g in gaps if g}
    _add_unique(entry["labels"], labels, END_LABEL_CAP)
    entry["cut"] = entry["cut"] or cut


class Analysis:
    """Routes, route families, ends, writers and row selection of one trace, computed once from its JSON."""

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
        # Route ends that are not leaves: the route walk stopped where values feed each other (a loop).
        self.loop_ends = sorted({n for r in self.routes for n, info in r["agg"].ends.items()
                                 if self.route_end_kind(n, info) == "cycle"})
        self.step_tags = {s["id"]: self._step_tags(s) for s in t.steps if s["id"] in self.value_steps}
        self._fmemo: dict[str, dict] = {}
        self.families = self._families()
        self.families_by_end: dict[str, list[dict]] = {}
        for fam in self.families:
            for end in fam["ends"]:
                self.families_by_end.setdefault(end, []).append(fam)

    def route_end_kind(self, node_id: str, info: dict) -> str:
        if info.get("cycle") or (self.t.steps_by_target.get(node_id) and not info.get("no_inputs")):
            return "cycle"
        return self.end_kind(node_id)

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

    def source_group(self, node_id: str) -> dict:
        """The source family of an end: {"kind", "key", "label"}. Databases group by database.schema,
        files by connection; constants, variables and parameters each form one family."""
        node = self.t.nodes.get(node_id) or {}
        kind = node.get("kind") or ""
        obj = node.get("object") or {}
        if kind == "constant":
            return {"kind": "constant", "key": "constant", "label": "constants"}
        if kind == "prior_state" and self.end_kind(node_id) == "cycle":
            return {"kind": "prior_state", "key": "prior_state",
                    "label": "earlier content before a write (candidate earlier writers are shown on the other "
                             "routes)"}
        if obj:
            okind = obj.get("kind") or "table"
            entry = self.object_entry(node_id)
            if okind == "flat_file":
                return {"kind": "file", "key": entry[0], "label": entry[1], "connection": obj.get("name") or "?",
                        "package": obj.get("scope") or "?"}
            if okind == "package_flow":
                return {"kind": "package_flow", "key": entry[0], "label": entry[1]}
            if okind in ("temp_table", "table_variable"):
                return {"kind": "temp", "key": entry[0], "label": entry[1]}
            if okind in ("cte", "derived"):
                return {"kind": "local", "key": "local", "label": "columns computed inside a statement"}
            label = ".".join(p for p in (obj.get("server"), obj.get("database") or "?", obj.get("schema") or "?") if p)
            return {"kind": "database", "key": label.lower(), "label": label}
        if kind in ("parameter", "variable", "package_variable"):
            noun = {"parameter": "procedure parameters", "variable": "variables",
                    "package_variable": "package variables and parameters"}[kind]
            return {"kind": kind, "key": kind, "label": noun}
        return {"kind": "unknown", "key": "unknown", "label": "inputs that could not be identified"}

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

    # route families -----------------------------------------------------------
    def _step_tags(self, step: dict) -> list[str]:
        """Rule tags a step adds to every route through it: writer kind, branch context, package, operation."""
        tags: list[str] = []
        kind = step.get("writer_kind")
        if step.get("mapping_method") == "assertion" or kind == "assertion":
            tags.append("assertion")
        if kind == "update":
            siblings = self.t.steps_by_target.get(step.get("target") or "", [])
            others = [s for s in siblings
                      if s.get("writer_kind") not in ("update", "merge_update", "assertion", "retained")]
            if not others:
                tags.append("update")
            elif all(s.get("procedure") == step.get("procedure") and (s.get("order") or 0) < (step.get("order") or 0)
                     for s in others):
                tags.append("override")
            else:
                tags.append("override_unordered")
        elif kind in ("merge_update", "merge_insert", "retained"):
            tags.append(kind)
        for label in step.get("branch_context") or []:
            tags.append(_context_tag(label))
        if (step.get("procedure") or "").startswith("package "):
            tags.append("package")
        op = step.get("operation")
        if op in OP_TAG:
            tags.append(OP_TAG[op])
        elif op == "case" and not any(t in ("case_when", "case_else", "condition")
                                      for i in step.get("inputs") or [] for t in _input_tags(i)):
            tags.append("conditional")
        return [t for t in tags if t]

    def stop_key(self, node_id: str, cycle: bool = False) -> str:
        if cycle:
            return "cycle"
        kind = self.end_kind(node_id)
        if kind == "gap":
            return "gap:" + ",".join(sorted({g.get("kind") or "gap" for g in self.t.gaps_by_node.get(node_id, [])}))
        return kind

    def _family_walk(self, node_id: str, stack: set[str]) -> dict[tuple, dict]:
        """Routes from a node, keyed by (end, stop, rule tags, status, open point on the way)."""
        if node_id in self._fmemo:
            return self._fmemo[node_id]
        t = self.t
        entry = self.object_entry(node_id)
        own = (entry[0],) if entry else ()
        steps = t.steps_by_target.get(node_id, [])
        node_gaps = {g["id"] for g in t.gaps_by_node.get(node_id, [])}
        result: dict[tuple, dict] = {}
        if node_id in stack:
            _fam_add(result, (node_id, "cycle", (), "verified", False), {own}, set(), set(), [])
            return result
        if not steps:
            _fam_add(result, (node_id, self.stop_key(node_id), (), "verified", bool(node_gaps)), {own}, set(),
                     node_gaps, [])
            self._fmemo[node_id] = result
            return result
        stack.add(node_id)
        for step in steps:
            ctx = self.step_tags.get(step["id"]) or self._step_tags(step)
            labels = [_truncate(b, LABEL_LIMIT) for b in step.get("branch_context") or []]
            step_gaps = set(step.get("gaps") or []) | node_gaps
            status = step.get("status") or "verified"
            inputs = [i for i in step.get("inputs") or [] if (i.get("role") or "value") in VALUE_ROLES]
            if not inputs:
                _fam_add(result, (node_id, self.stop_key(node_id), _tags(ctx), status, bool(step_gaps)), {own},
                         {step["id"]}, step_gaps, labels)
                continue
            for inp in inputs:
                in_tags = ctx + _input_tags(inp)
                in_labels = labels + ([_truncate(inp["branch"], LABEL_LIMIT)] if inp.get("branch") else [])
                in_gaps = step_gaps | ({inp["gap"]} if inp.get("gap") else set())
                in_status = _worst(status, inp.get("status"))
                for (end, stop, tags, sub_status, sub_open), sub in self._family_walk(inp.get("node") or "?",
                                                                                       stack).items():
                    seqs = {own + s[1:] if own and s[:1] == own else own + s for s in sub["seqs"]}
                    key = (end, stop, _tags(in_tags + list(tags)), _worst(in_status, sub_status),
                           sub_open or bool(in_gaps))
                    _fam_add(result, key, seqs, sub["steps"] | {step["id"]}, sub["gaps"] | in_gaps,
                             in_labels + sub["labels"], sub["cut"])
        stack.discard(node_id)
        self._fmemo[node_id] = result
        return result

    def _families(self) -> list[dict]:
        t = self.t
        if not t.steps_by_target.get(t.target_id):
            return []
        target_key = self.target_entry[0] if self.target_entry else None
        families: dict[tuple, dict] = {}
        for (end, stop, tags, status, has_open), value in self._family_walk(t.target_id, set()).items():
            group = self.source_group(end)
            if has_open:
                kinds = {t.gap_kind(g) for g in value["gaps"]}
                status = _worst(status, "ambiguous" if any(k.startswith("ambiguous") for k in kinds) else "partial")
            key = (group["key"], tags, stop, status)
            fam = families.get(key)
            if fam is None:
                fam = families[key] = {
                    "id": _hash_list("fam", [group["key"], list(tags), stop, status]),
                    "source": group, "value_rule": list(tags), "value_rule_text": rule_text(tags),
                    "headline": headline(tags), "stop": stop, "stop_text": stop_short(stop), "status": status,
                    "route_count": 0, "route_count_is_lower_bound": False, "ends": set(), "members": []}
            seqs = sorted(value["seqs"])
            upstream = [list(s[1:] if target_key and s[:1] == (target_key,) else s) for s in seqs]
            fam["members"].append({
                "end": end, "object_sequences": upstream,
                "steps": sorted(value["steps"], key=self._step_order),
                "gaps": sorted(value["gaps"]), "branches": list(value["labels"])})
            fam["ends"].add(end)
            fam["route_count"] += len(seqs)
            fam["route_count_is_lower_bound"] = fam["route_count_is_lower_bound"] or value["cut"]
        out = []
        for fam in families.values():
            fam["ends"] = sorted(fam["ends"], key=lambda n: (t.display(n).lower(), n))
            fam["members"].sort(key=lambda m: (t.display(m["end"]).lower(), m["end"], m["object_sequences"]))
            fam["role"] = "condition" if any(tg in ("condition", "window") for tg in fam["value_rule"]) else "value"
            fam["diagram_kind"] = self._diagram_kind(fam)
            out.append(fam)
        out.sort(key=lambda f: (f["role"] != "value", -f["route_count"],
                                END_ORDER.index(f["stop"]) if f["stop"] in END_ORDER else len(END_ORDER),
                                f["source"]["label"].lower(), f["id"]))
        for index, fam in enumerate(out, 1):
            fam["label"] = f"F{index}"
        return out

    def _step_order(self, step_id: str) -> tuple[int, str]:
        label = self.t.label.get(step_id, "")
        return (int(label[1:]) if label[1:].isdigit() else 10 ** 9, step_id)

    @staticmethod
    def _diagram_kind(fam: dict) -> str:
        stop, kind = fam["stop"], fam["source"]["kind"]
        if stop == "declared":
            return "declared"
        if kind == "constant":
            return "constant"
        if kind == "file" and stop == "external_file":
            return "file"
        if kind == "database" and stop in ("boundary", "source_without_writer"):
            return "database"
        if stop == "cycle":
            return "earlier" if kind == "prior_state" else "loop"
        return "unresolved"

    # ends ---------------------------------------------------------------------
    def end_kind(self, node_id: str) -> str:
        t = self.t
        stops = t.stops_by_node.get(node_id, [])
        if stops:
            stop = stops[0]
            if stop.get("declared_terminal") and stop.get("reason") not in ("boundary", "constant"):
                return "declared"
            if stop.get("reason") == "prior_state":
                return "cycle"   # earlier content read back in a loop; its sources are traced on the page
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
        self.consumers = consumers
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


def family_json(fam: dict) -> dict:
    """A route family as stored in JSON (sets made lists, internal fields left out)."""
    return {k: fam[k] for k in ("id", "label", "source", "role", "value_rule", "value_rule_text", "headline", "stop",
                                "stop_text", "status", "route_count", "route_count_is_lower_bound", "ends",
                                "diagram_kind", "members")}


def route_families_block(data: dict, analysis: Analysis | None = None) -> dict:
    """The additive ``route_families`` block of a trace JSON: every family with all its member routes."""
    a = analysis or Analysis(data)
    return {"schema_version": FAMILY_SCHEMA,
            "note": "Value routes grouped only when source family, value rule, stopping reason and evidence status "
                    "all agree. Derived from the steps, stops and gaps of this trace; it replaces none of them. "
                    "route_count counts distinct (end, object sequence) pairs per family member; a family's ends "
                    "are where the trace stopped, not confirmed origins.",
            "families": [family_json(f) for f in a.families]}


# --------------------------------------------------------------------------- glossary

def load_glossary(path: str | Path) -> list[dict]:
    """Read a business glossary: {"columns": [{"column", "definition", "status", "source"}]}. ``status`` is
    ``approved`` (an owner-approved definition) or ``proposed`` (a proposed interpretation, shown apart)."""
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


# --------------------------------------------------------------------------- shared wording

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
    if kind == "cycle":
        if t.kind(node_id) == "prior_state":
            return ("earlier content before this write: the write reads the value the column (or variable) "
                    "already had. Candidate earlier writers are shown on the other routes; the code does not prove "
                    "which of them populated the rows before this run")
        return ("loop: values here feed each other — the loop is followed once and adds no new source (every "
                "input into it is traced); see the cycles on the detailed page")
    return "not traced further — not proved"


def _short_kind(a: Analysis, node_id: str, kind: str) -> str:
    """An end kind for short lists: earlier content before a write is not called a loop."""
    return "earlier" if kind == "cycle" and a.t.kind(node_id) == "prior_state" else kind


def _end_short(kind: str) -> str:
    return {"boundary": "boundary reached", "declared": "declared terminal", "source_without_writer":
            "no writer in selected sources", "external_file": "external file", "parameter_without_caller":
            "parameter without caller", "max_depth": "depth limit", "constant": "constant",
            "gap": "open point", "earlier": "earlier content", "cycle": "loop",
            "open": "not traced further"}.get(kind, kind)


def _node_short(a: Analysis, node_id: str, limit: int = NAME_LIMIT) -> str:
    t = a.t
    if t.kind(node_id) == "constant":
        return "constant " + _code((t.nodes.get(node_id) or {}).get("name") or node_id.split(":", 1)[-1],
                                   CONSTANT_LIMIT)
    return _code(_truncate(t.display(node_id), limit))


def _fam_link(fam: dict, detailed: str) -> str:
    return f"[{fam['label']}]({detailed}#{_anchor(fam['id'])})"


def _step_ref(a: Analysis, step_id: str, detailed: str) -> str:
    return f"[{a.t.label.get(step_id, step_id)}]({detailed}#{_anchor(step_id)})"


def _step_refs(a: Analysis, step_ids, detailed: str, limit: int = 3) -> str:
    ids = sorted(set(step_ids), key=a._step_order)
    shown = ", ".join(_step_ref(a, s, detailed) for s in ids[:limit])
    return shown + (f" and {len(ids) - limit} more" if len(ids) > limit else "")


def _group_text(group: dict) -> str:
    """A source family in prose: databases as ``db.schema``, files as connection and package."""
    kind = group["kind"]
    if kind == "database":
        return _code(group["label"])
    if kind == "file":
        return (f"file connection {_code(group.get('connection') or '?')} read by package "
                f"{_code(Path(group.get('package') or '?').name)}")
    return _text(group["label"])


def _object_text(a: Analysis, node_id: str) -> str:
    """The object of an end in prose (table, file connection, variable), for evidence requests."""
    node = a.t.nodes.get(node_id) or {}
    obj = node.get("object") or {}
    if obj.get("kind") == "flat_file":
        return (f"file connection {_code(obj.get('name') or '?')} read by package "
                f"{_code(Path(obj.get('scope') or '?').name)}")
    entry = a.object_entry(node_id)
    return _code(_truncate(entry[1] if entry else a.t.display(node_id), 60))


ANSWER_LAST = ("override (later UPDATE)", "UPDATE (run order not proved)", "MERGE when matched",
               "MERGE when not matched", "asserted mapping")


ANSWER_WORDS = {"override (later UPDATE)": "later UPDATE override", "UPDATE (run order not proved)":
                "UPDATE of unproved run order"}


def _headline_rank(text: str) -> tuple[int, int]:
    order = ("primary", "fallback", "CASE branch", "CASE default", "direct")
    if text in ANSWER_LAST:
        return (2, ANSWER_LAST.index(text))
    return (0, order.index(text)) if text in order else (1, 0)


def _fam_ends_text(a: Analysis, fam: dict, limit: int = 2) -> str:
    names = [_node_short(a, n) for n in fam["ends"]]
    return _and_list(names, limit)


def _is_unresolved(kind: str) -> bool:
    # A loop adds no source: every input into it is traced elsewhere on the page.
    return kind not in ("boundary", "declared", "constant", "cycle")


def _end_kinds(a: Analysis) -> Counter:
    """Distinct end nodes by kind, loop ends included; earlier content before a write is counted apart."""
    counts: Counter = Counter()
    seen: set[str] = set()

    def add(node_id: str, kind: str) -> None:
        if node_id not in seen:
            seen.add(node_id)
            counts["earlier" if a.t.kind(node_id) == "prior_state" else kind] += 1

    for e in a.ends:
        add(e["node"], e["kind"])
    for node_id in a.loop_ends:
        add(node_id, "cycle")
    for fam in a.families:   # ends of steps without inputs (not leaves)
        for end in fam["ends"]:
            add(end, "cycle" if fam["stop"] == "cycle" else (fam["stop"] if not fam["stop"].startswith("gap:")
                                                             else "gap"))
    return +counts


# --------------------------------------------------------------------------- 1. field and definition

def _section_field(a: Analysis, entry: dict | None, others: int) -> list[str]:
    t, data = a.t, a.data
    target = t.nodes.get(t.target_id) or {}
    obj = target.get("object") or {}
    field = f"- **Field:** {_code(t.display(t.target_id))}"
    if obj and (obj.get("kind") or "table") in ("table", "view"):
        db = obj.get("database")
        field += (f" — database {_code(db) if db else 'not established'}, schema {_code(obj.get('schema') or '?')}, "
                  f"table {_code(obj.get('name') or '?')}, column {_code(target.get('name') or '?')}")
    if data.get("requested") and _norm_name(data["requested"]) != _norm_name(t.display(t.target_id)):
        field += f" (requested as {_code(data.get('requested'))})"
    out = [field]
    extra = f" ({_plural(others, 'other glossary entry', 'other glossary entries')} also match.)" if others else ""
    source = f" Source: {_text(entry['source'])}." if entry and entry.get("source") else ""
    if entry is None:
        out.append("- **Definition not approved.** No glossary entry; the tool never infers business meaning from "
                   "names — question for the data owner.")
    elif entry["status"] == "approved":
        out.append(f"- **Definition (approved):** {_text(entry['definition'])}.{source}{extra}")
    else:
        out.append("- **Definition not approved.** No owner-approved definition yet.")
        out.append(f"- *Proposed interpretation* (glossary status: proposed, not approved; "
                   f"{('source: ' + _text(entry['source'])) if entry.get('source') else 'no source given'}): "
                   f"{_text(entry['definition'])}.{extra}")
    return out


# --------------------------------------------------------------------------- 2. plain answer

def _result_head(a: Analysis) -> str:
    comp = a.comp
    if comp.get("status") != "complete":
        n_open = sum((comp.get("value_gaps_by_kind") or {}).values()) + \
            sum((comp.get("context_gaps_by_kind") or {}).values())
        return (f"Not fully traced ({_plural(n_open, 'open point')} on the value routes, so sources may be "
                "missing)")
    return {"reached_requested_boundary": "Traced to the requested boundary (not beyond it)",
            "complete_to_declared_boundary": "Traced to terminals declared for this run (their upstream is not "
                                             "proved)",
            "complete_to_requested_depth": "Traced to the depth limit set for this run (some routes were cut)",
            "complete_within_selected_scope": "Complete only within the selected sources"}.get(
        comp.get("result"), "Traced")


def _no_route_sentence(a: Analysis) -> str:
    t = a.t
    if t.stops_by_node.get(t.target_id):
        kind = a.end_kind(t.target_id)
        return (f"No step leads into this field in the selected sources: it is itself an end point "
                f"({_end_text(a, t.target_id, kind)}).")
    return "No writer of this field could be established in the selected sources; its value is not traced."


def _answer_headline(a: Analysis, unproven: int, total: int) -> str:
    """The first words a reader sees: where proof stands, never "complete" alone when origins are unproven."""
    comp = a.comp
    boundary = sum(f["route_count"] for f in a.families if f["role"] == "value" and f["stop"] == "boundary")
    if comp.get("status") != "complete":
        return f"**{_result_head(a)}.**"
    if comp.get("result") == "complete_within_selected_scope" and unproven:
        # Separate counts of the same routes, never one as a subset of the other.
        where = ("none reaches the requested boundary" if not boundary else
                 f"a separate {boundary} {'reaches' if boundary == 1 else 'reach'} the requested boundary")
        return (f"**Trace stopped at unresolved sources:** of {_plural(total, 'route')} carrying the value, "
                f"{unproven} {'ends' if unproven == 1 else 'end'} where no writer, file producer or caller was "
                f"found (origin not proved there) and {where}. "
                "(Technically complete within the selected sources: no open point inside them.)")
    return f"**{_result_head(a)}.**"


def _section_answer(a: Analysis, detailed: str = "") -> list[str]:
    if not a.families:
        return [_no_route_sentence(a)]
    value_fams = [f for f in a.families if f["role"] == "value"]
    groups: dict[str, dict] = {}
    for fam in value_fams:
        group = groups.setdefault(fam["source"]["key"], {"source": fam["source"], "routes": 0, "headlines": [],
                                                         "fams": []})
        group["routes"] += fam["route_count"]
        group["fams"].append(fam)
        if fam["headline"] not in group["headlines"]:
            group["headlines"].append(fam["headline"])
    ordered = sorted(groups.values(), key=lambda g: (-g["routes"], g["source"]["label"].lower()))
    names = []
    for group in ordered[:MAX_ANSWER_SOURCES]:
        source = group["source"]
        if source["kind"] == "constant":
            values = sorted({_code((a.t.nodes.get(n) or {}).get("name") or n, CONSTANT_LIMIT)
                             for f in group["fams"] for n in f["ends"]})
            text = (f"the constant {values[0]}" if len(values) == 1 else f"the constants {_and_list(values)}"
                    if len(values) == 2 else f"{len(values)} constants such as {values[0]} and {values[1]}")
        else:
            text = _group_text(source)
        rules = [ANSWER_WORDS.get(h, h) for h in sorted(group["headlines"], key=_headline_rank)]
        links = ", ".join(_fam_link(f, detailed) for f in group["fams"][:4]) + (", …" if len(group["fams"]) > 4 else "")
        names.append(f"{text} ({', '.join(rules)}; {links})")
    more = len(ordered) - MAX_ANSWER_SOURCES
    if more > 0:
        names.append(f"and {_plural(more, 'more source family', 'more source families')} (see Route families)")
    total = sum(f["route_count"] for f in value_fams)
    unproven = sum(f["route_count"] for f in value_fams
                   if f["stop"] in ("source_without_writer", "external_file", "parameter_without_caller"))
    def routes(test, earlier: bool | None = None) -> int:
        return sum(f["route_count"] for f in value_fams if test(f["stop"])
                   and (earlier is None or (f["source"]["kind"] == "prior_state") == earlier))
    stops = []
    for n, one, many in (
            (routes(lambda s: s.startswith("gap:") or s == "open"), "stops at an open point", "stop at open points"),
            (routes(lambda s: s == "cycle", True), "ends at earlier content before a write (candidate earlier "
             "writers are shown)", "end at earlier content before a write (candidate earlier writers are shown)"),
            (routes(lambda s: s == "cycle", False), "stops at a loop", "stop at loops"),
            (routes(lambda s: s == "max_depth"), "is cut at the depth limit", "are cut at the depth limit"),
            (routes(lambda s: s == "declared"), "ends at a terminal declared for this run (upstream not traced)",
             "end at terminals declared for this run (upstream not traced)")):
        if n:
            stops.append(f"{_plural(n, 'route')} {one if n == 1 else many}")
    if unproven and a.comp.get("result") != "complete_within_selected_scope":
        stops.insert(0, f"{_plural(unproven, 'route')} {'ends' if unproven == 1 else 'end'} where no writer, file "
                        "producer or caller was found, so the origin is not proved there")
    out = [f"- {_answer_headline(a, unproven, total)}" + (f" Also: {_and_list(stops, 5)}." if stops else ""),
           "- **Sources:**" + ("".join(f"\n    - {n}" for n in names) if names else " no value source (conditions only)")]
    rules = _answer_rules(a)
    if rules:
        out.append(f"- **Value rules:** {'; '.join(rules)} — details under What changes the value.")
    return out


def _answer_rules(a: Analysis) -> list[str]:
    """The kinds of rule that change the value, without repeating the columns named under Sources."""
    tags = {tg for f in a.families if f["role"] in ("value", "condition") for tg in f["value_rule"]}
    out = []
    if {"case_when", "case_else"} & tags or any(f["role"] == "condition" for f in a.families):
        consts = sorted({_code((a.t.nodes.get(n) or {}).get("name") or n, CONSTANT_LIMIT) for f in a.families
                         if f["source"]["kind"] == "constant" and {"case_when", "case_else"} & set(f["value_rule"])
                         for n in f["ends"]})
        out.append("a CASE/IIF choice picks between the alternatives"
                   + (f" (it can set {_limited(consts, 2)})" if consts else ""))
    if any(tg.startswith("fallback") for tg in tags):
        out.append("a COALESCE/ISNULL fallback when earlier values are null")
    if "override" in tags:
        out.append("a later UPDATE overwrites the rows it matches")
    if "override_unordered" in tags:
        out.append("an UPDATE may overwrite the value (run order not proved)")
    if {"merge_update", "merge_insert"} & tags:
        out.append("a MERGE writes matched and new rows")
    if "package" in tags:
        out.append("package transformations on file or package routes")
    if {"aggregate", "window_calc"} & tags:
        out.append("aggregation over rows")
    return out[:5]


def _answer_changes(a: Analysis) -> list[str]:
    """Material value rules, in plain words, from the families and the steps on the value routes."""
    t = a.t
    phrases: list[str] = []
    case_steps = [s for s in a.value_steps if any(tg in ("case_when", "case_else")
                                                  for i in (a.steps_by_id[s].get("inputs") or [])
                                                  for tg in _input_tags(i))]
    if case_steps:
        conds = sorted({n for f in a.families if "condition" in f["value_rule"] for n in f["ends"]},
                       key=lambda n: (t.display(n).lower(), n))
        consts = sorted({_code((t.nodes.get(n) or {}).get("name") or n, CONSTANT_LIMIT) for f in a.families
                         if f["source"]["kind"] == "constant" and {"case_when", "case_else"} & set(f["value_rule"])
                         for n in f["ends"]})
        text = "a CASE/IIF choice"
        if conds:
            text += f" on {_and_list([_node_short(a, n, 60) for n in conds], 2)}"
        text += " picks between the alternatives"
        if consts:
            text += f" and can set the constant{'s' if len(consts) > 1 else ''} {_limited(consts, 2)}"
        phrases.append(text)
    fallbacks = [f for f in a.families if f["role"] == "value" and any(tg.startswith("fallback") for tg in f["value_rule"])]
    if fallbacks:
        phrases.append("COALESCE/ISNULL falls back to " + _and_list([_fam_ends_text(a, f, 1) for f in fallbacks], 2)
                       + " when earlier values are null")
    for tag, text in (("override", "an UPDATE that runs after the other writers overwrites the rows it matches with "),
                      ("override_unordered", "an UPDATE may overwrite the value (run order not proved) with ")):
        fams = [f for f in a.families if f["role"] == "value" and tag in f["value_rule"]]
        if fams:
            phrases.append(text + _and_list([_fam_ends_text(a, f, 1) for f in fams], 2))
    merges = [f for f in a.families if {"merge_update", "merge_insert"} & set(f["value_rule"]) and f["role"] == "value"]
    if merges:
        phrases.append("a MERGE writes matched and new rows from " + _and_list([_fam_ends_text(a, f, 1) for f in merges], 2))
    packages = [f for f in a.families if "package" in f["value_rule"] and f["role"] == "value"]
    if packages:
        pkgs = sorted({(_writer_label(a.steps_by_id[s]) or "?").replace("package ", "", 1) for f in packages for m in f["members"]
                       for s in m["steps"] if s in a.steps_by_id and
                       (a.steps_by_id[s].get("procedure") or "").startswith("package ")})
        ops = sorted({tag_short(tg) for f in packages for tg in f["value_rule"]
                      if tg.partition(":")[0] in ("conversion", "arithmetic", "function", "string", "calculation",
                                                  "conditional", "aggregate", "lookup")})
        what = "file values" if all(f["source"]["kind"] == "file" for f in packages) else \
            "values from " + _and_list([_group_text(f["source"]) for f in packages[:2]], 2)
        phrases.append(f"{what} pass through {_and_list([_code(p) for p in pkgs], 2)}"
                       + (f" ({', '.join(ops)})" if ops else ""))
    if any(tg in ("aggregate", "window_calc") for f in a.families for tg in f["value_rule"]):
        phrases.append("values are aggregated or windowed over rows")
    return phrases[:4]


# --------------------------------------------------------------------------- 3. coverage

def _section_coverage(a: Analysis, detailed: str) -> list[str]:
    comp, t = a.comp, a.t
    if not a.families:
        return [f"- **Status:** {_text(comp.get('result') or comp.get('status') or 'unknown')} — {_no_route_sentence(a)}",
                "- A requested boundary or declared terminal is where this run stopped; it is not an originating "
                "system."]
    counts = _end_kinds(a)
    routes = sum(f["route_count"] for f in a.families)
    lower = any(f["route_count_is_lower_bound"] for f in a.families)
    unresolved_parts = [_plural(counts[k], one, many) for k, one, many in (
        ("source_without_writer", "with no writer in the selected sources", "with no writer in the selected sources"),
        ("external_file", "external file", "external files"),
        ("parameter_without_caller", "parameter without caller", "parameters without caller"),
        ("max_depth", "depth cut", "depth cuts"), ("gap", "open point", "open points"),
        ("open", "not traced further", "not traced further")) if counts.get(k)]
    unresolved = sum(counts.get(k, 0) for k in ("source_without_writer", "external_file", "parameter_without_caller",
                                                 "max_depth", "gap", "open"))
    if comp.get("status") != "complete":
        n_open = sum((comp.get("value_gaps_by_kind") or {}).values()) + \
            sum((comp.get("context_gaps_by_kind") or {}).values())
        status = (f"**Incomplete** — {_plural(n_open, 'open point')} on the value routes ([gaps]({detailed}#gaps)); "
                  "proof stops before an origin on the routes they touch")
        if counts.get("boundary"):
            status += (f". {_plural(counts['boundary'], 'end')} {'does' if counts['boundary'] == 1 else 'do'} reach "
                       "the requested boundary, but the trace as a whole is not complete")
    else:
        status = {
            "reached_requested_boundary": "**Reached the requested boundary** on every route (not traced beyond it)",
            "complete_to_declared_boundary": "**Complete to terminals declared for this run** — how they are "
                                             "populated upstream is not proved",
            "complete_to_requested_depth": "**Cut at the requested depth** on some routes — not traced further",
            "complete_within_selected_scope": "**Trace stopped at unresolved sources** — technically complete "
                                              "within the selected sources (every route ends at a stop), but the "
                                              "unresolved origins below are not proved"}.get(
            comp.get("result"), _text(comp.get("result") or "complete"))
    statuses = comp.get("steps_by_status") or {}
    step_text = ", ".join(f"{statuses.get(s, 0)} {s}" for s in ("verified", "inferred", "asserted"))
    other = [f"{n} {s}" for s, n in sorted(statuses.items(), key=lambda kv: _status_rank(kv[0]))
             if s not in ("verified", "inferred", "asserted")]
    if other:
        step_text += ", " + ", ".join(other)
    carrying = sum(f["route_count"] for f in a.families if f["role"] == "value")
    deciding = routes - carrying
    line = (f"{'at least ' if lower else ''}{_plural(routes, 'route')} ({carrying} "
            f"{'carries' if carrying == 1 else 'carry'} the value"
            + (f", {deciding} {'decides' if deciding == 1 else 'decide'} between values" if deciding else "") + ") in "
            f"{_plural(len(a.families), 'route family', 'route families')} · distinct end points (columns where "
            f"routes stop): {counts.get('boundary', 0)} at the requested boundary · {counts.get('declared', 0)} "
            f"declared terminal{'' if counts.get('declared', 0) == 1 else 's'} · "
            f"{_plural(unresolved, 'unresolved origin')}"
            + (f" ({', '.join(unresolved_parts)})" if unresolved_parts else "")
            + (f" · {_plural(counts['constant'], 'constant')}" if counts.get("constant") else "")
            + (f" · {_plural(counts['earlier'], 'earlier-content end')} (the value before a write that reads "
               "it; candidate earlier writers are shown on the other routes)" if counts.get("earlier") else "")
            + (f" · {_plural(counts['cycle'], 'loop')} (values feed each other; every input into "
               f"{'it' if counts['cycle'] == 1 else 'them'} is traced, so no source is missing there)"
               if counts.get("cycle") else "")
            + f" · steps: {step_text}")
    out = [f"- **Status:** {status}.", f"- **Coverage:** {line}.",
           "- A requested boundary or declared terminal is where this run stopped; it is not an originating system. "
           "An end with no writer in the selected sources is an unresolved origin even when the step reaching it "
           "is verified."]
    run = _run_context_line(a)
    if run:
        out.insert(2, run)
    return out


def _run_context_line(a: Analysis) -> str | None:
    """Which SQL Agent jobs (from --job-scripts) run the writers on the value routes."""
    contexts = [s.get("run_context") for s in a.t.steps if s["id"] in a.value_steps and s.get("run_context")]
    if not contexts:
        return None
    counts = Counter(c["status"] for c in contexts)
    jobs = sorted({(j["job"], j.get("schedule") or "") for c in contexts for j in c.get("jobs") or []})
    shown = ", ".join(_code(name) + (f" ({_text(sched)})" if sched else "") for name, sched in jobs[:4]) + \
        (f" and {len(jobs) - 4} more" if len(jobs) > 4 else "")
    parts = [f"{counts['run_by_enabled_job']} run by enabled jobs" if counts["run_by_enabled_job"] else "",
             f"{counts['only_disabled_jobs']} only by disabled jobs" if counts["only_disabled_jobs"] else "",
             f"{counts['not_in_job_scripts']} by no job in the scripts" if counts["not_in_job_scripts"] else ""]
    return (f"- **Run by (job scripts, static copies):** {_plural(len(contexts), 'writing step')}: "
            + ", ".join(p for p in parts if p) + (f" — jobs: {shown}" if jobs else "") + ".")


# --------------------------------------------------------------------------- 4. main evidence needed

def _evidence_candidates(a: Analysis, detailed: str) -> list[tuple[int, int, str]]:
    """(routes affected, priority, bullet) for every evidence request."""
    t = a.t
    cands: list[tuple[int, int, str]] = []
    gaps = [g for g in t.sorted_gaps() if g.get("affects", "value") != "row_selection"]
    by_kind: dict[str, list[dict]] = {}
    for gap in gaps:
        by_kind.setdefault(gap.get("kind") or "gap", []).append(gap)
    for kind, items in by_kind.items():
        ids = {g["id"] for g in items}
        routes = sum(len(m["object_sequences"]) for f in a.families for m in f["members"] if ids & set(m["gaps"]))
        first = items[0]
        cands.append((routes, 0, f"**{_sentence(GAP_TEXT.get(kind, kind))[:-1]}** ({_plural(len(items), 'place')}, "
                                 f"{_plural(routes, 'route')} affected): "
                                 f"{_text(NEXT_EVIDENCE.get(kind, 'manual review of the cited statement'))} — "
                                 f"[details]({detailed}#{_anchor(first['id'])})"))
    groups: dict[tuple[str, str], list[dict]] = {}
    for fam in a.families:
        stop = fam["stop"]
        if stop in ("source_without_writer", "external_file", "parameter_without_caller", "max_depth"):
            groups.setdefault((stop, fam["source"]["key"]), []).append(fam)
    for (stop, _), fams in groups.items():
        routes = sum(f["route_count"] for f in fams)
        objects = sorted({_object_text(a, n) for f in fams for n in f["ends"]}, key=str.lower)
        names = _limited(objects, 2)
        link = ", ".join(_fam_link(f, detailed) for f in fams[:3]) + (", …" if len(fams) > 3 else "")
        if stop == "source_without_writer":
            text = (f"**Who writes {names}** (no writer in the selected sources; {_plural(routes, 'route')}): the "
                    "procedure, package, job or feed that loads it — add it to the sources, or declare it as a "
                    f"terminal — {link}")
        elif stop == "external_file":
            text = (f"**Who produces {names}** (file producer unknown; {_plural(routes, 'route')}): the system or job "
                    f"that writes the file, or declare it as a terminal — {link}")
        elif stop == "parameter_without_caller":
            text = (f"**Callers of {names}** (parameter without caller; {_plural(routes, 'route')}): the code or job "
                    f"that calls the procedure with this parameter — {link}")
        elif stop == "max_depth":
            text = (f"**Trace beyond {names}** (depth limit; {_plural(routes, 'route')}): rerun with a larger "
                    f"--max-depth — {link}")
        else:
            text = f"**The loop at {names}** ({_plural(routes, 'route')}): manual review of the cycle — {link}"
        cands.append((routes, 1, text))
    return sorted(cands, key=lambda c: (-c[0], c[1], c[2]))


def _section_evidence_needed(a: Analysis, detailed: str) -> list[str]:
    cands = _evidence_candidates(a, detailed)
    if not cands:
        return ["Nothing: every route ends at the requested boundary, a declared terminal or a constant."]
    out = [f"- {text}" for _, _, text in cands[:MAX_EVIDENCE]]
    if len(cands) > MAX_EVIDENCE:
        out += ["", f"And {_plural(len(cands) - MAX_EVIDENCE, 'more request')} — see "
                    f"[gaps and next evidence]({detailed}#gaps)."]
    return out


# --------------------------------------------------------------------------- 5. route families diagram

# Inside a quoted Mermaid label, # | [ ] { } & and backticks render as written (checked with Mermaid 10
# in a browser); only a quote ends the label, "#name;" is read as an entity and < > are HTML. Readable
# substitutes keep the diagram source legible for people who read it unrendered.
_MERMAID_SAFE = {'"': "'", ";": ",", "<": "‹", ">": "›", "…": "...", "←": "from", "\\": "/"}


def mermaid_text(value) -> str:
    """Text safe inside a quoted Mermaid label, with no entity codes."""
    return "".join(_MERMAID_SAFE.get(ch, ch) for ch in _one_line(value))


def _diagram_node_label(a: Analysis, fam: dict) -> str:
    kind = fam["diagram_kind"]
    prefix = SHAPES[kind][2]
    ends = fam["ends"]
    if fam["source"]["kind"] == "constant":
        values = [(a.t.nodes.get(n) or {}).get("name") or n for n in ends]
        what = ", ".join(_truncate(v, 20) for v in values[:3]) + (f" and {len(values) - 3} more" if len(values) > 3 else "")
        prefix = "Constant" if len(values) == 1 else "Constants"
    elif fam["source"]["kind"] == "prior_state" and len(ends) > 1:
        later = [a.t.display((a.t.nodes.get(n) or {}).get("earlier_state_of") or n) for n in ends]
        what = _truncate("earlier content of " + ", ".join(later[:3])
                         + (f" and {len(later) - 3} more" if len(later) > 3 else ""), DIAGRAM_NAME_LIMIT)
    elif len(ends) == 1:
        what = _truncate(a.t.display(ends[0]), DIAGRAM_NAME_LIMIT)
    else:
        noun = "columns" if fam["source"]["kind"] in ("database", "file", "temp", "package_flow") else "distinct"
        what = f"{_truncate(fam['source']['label'], DIAGRAM_NAME_LIMIT)}, {len(ends)} {noun}"
        prefix = {"database": "DB columns", "file": "File columns"}.get(kind, prefix)
    if kind == "database" and fam["stop"] == "boundary":
        prefix += " at requested boundary"
    if kind == "declared":
        prefix = "Declared terminal, not an origin"
    routes = f", {fam['route_count']} routes" if fam["route_count"] > 1 else ""
    return mermaid_text(f"{fam['label']} {prefix}: {what}{routes}")


def _diagram_edge_label(fam: dict) -> str:
    text = f"{fam['value_rule_text']} - {fam['stop_text']}"
    if fam["status"] != "verified":
        text += f" - {fam['status']}"
    return mermaid_text(_truncate(text, DIAGRAM_RULE_LIMIT))


def family_mermaid(a: Analysis) -> list[str]:
    """Mermaid flowchart of route families: one node per family, each with its own edge into the target, so
    alternatives are drawn side by side and never as a chain."""
    t = a.t
    fams = a.families[:MAX_DIAGRAM_FAMILIES]
    lines = ["```mermaid", "flowchart LR",
             f'  T["{mermaid_text("Target: " + _truncate(t.display(t.target_id), DIAGRAM_NAME_LIMIT))}"]']
    classes: dict[str, list[str]] = {}
    for fam in fams:
        opener, closer, _ = SHAPES[fam["diagram_kind"]]
        arrow = "-.->" if fam["role"] == "condition" else "-->"
        lines.append(f'  {fam["label"]}{opener}{_diagram_node_label(a, fam)}{closer} {arrow}|"{_diagram_edge_label(fam)}"| T')
        classes.setdefault(fam["diagram_kind"], []).append(fam["label"])
    rest = a.families[MAX_DIAGRAM_FAMILIES:]
    if rest:
        routes = sum(f["route_count"] for f in rest)
        kinds = Counter(_end_short("earlier" if f["stop"] == "cycle" and f["source"]["kind"] == "prior_state"
                                   else f["stop"] if not f["stop"].startswith("gap:") else "gap") for f in rest)
        what = ", ".join(f"{n} {k}" for k, n in kinds.most_common(3))
        label = (f"Summary, not a source: {len(rest)} more route families ({routes} routes; {what}) - "
                 "each listed in the family table below")
        lines.append(f'  MORE["{mermaid_text(label)}"] -.- T')
    lines.append("  classDef target stroke-width:3px")
    lines.append("  class T target")
    if rest:
        lines.append("  classDef summary stroke-dasharray:6 4")
        lines.append("  class MORE summary")
    for kind, style in (("unresolved", "stroke-dasharray:5 3"), ("declared", "stroke-dasharray:2 2"),
                        ("constant", "stroke-dasharray:1 3")):
        if classes.get(kind):
            lines.append(f"  classDef {kind} {style}")
            lines.append(f"  class {','.join(classes[kind])} {kind}")
    lines.append("```")
    return lines


def _section_families(a: Analysis, detailed: str) -> list[str]:
    if not a.families:
        return ["No route families: no value route could be followed from this field."]
    out = ["Each box is a route family (routes with the same source family, value rule, stopping reason and "
           "evidence status); each has its own arrow into the field, so alternatives are side by side, not in "
           "sequence. Dotted arrows decide between values. Box and edge labels say where proof stops — no box is "
           "claimed as the originating system.", ""]
    out += family_mermaid(a)
    out += ["", "| Family | Source | Value rule | Stops because | Evidence | Routes |", "|---|---|---|---|---|---|"]
    for fam in a.families[:MAX_FAMILY_ROWS]:
        source = fam["source"]
        kind = {"database": "DB", "file": "file", "constant": "constant",
                "prior_state": "earlier content"}.get(source["kind"], _text(source["kind"]))
        what = _fam_ends_text(a, fam, 2) if len(fam["ends"]) <= 2 else \
            f"{_code(_truncate(source['label'], NAME_LIMIT))} ({len(fam['ends'])} {'values' if source['kind'] == 'constant' else 'columns'})"
        routes = f"{'≥' if fam['route_count_is_lower_bound'] else ''}{fam['route_count']}"
        out.append("| " + " | ".join(_cell(c) for c in (
            _fam_link(fam, detailed), f"{kind}: {what}", _text(fam["value_rule_text"]), _text(fam["stop_text"]),
            _text(FAMILY_STATUS_TEXT.get(fam["status"], fam["status"])), routes)) + " |")
    if len(a.families) > MAX_FAMILY_ROWS:
        rest = a.families[MAX_FAMILY_ROWS:]
        out += ["", f"And {_plural(len(rest), 'more family', 'more families')} "
                    f"({_plural(sum(f['route_count'] for f in rest), 'route')}) — "
                    f"[full list]({detailed}#route-families)."]
    return out


# --------------------------------------------------------------------------- 6. sources at a glance

def _endpoint_cells(a: Analysis, node_id: str, node: dict | None = None) -> tuple[str, str, str, str, str]:
    t = a.t
    node = node if node is not None else (t.nodes.get(node_id) or {})
    if node.get("kind") == "prior_state":
        later = dict(node, kind=node.get("of_kind"))
        cells = _endpoint_cells(a, node.get("earlier_state_of") or node_id, later)
        return (f"earlier content of a {cells[0]}",) + tuple(cells[1:])
    obj = node.get("object") or {}
    kind = node.get("kind")
    name = node.get("name") or node_id
    if kind == "constant":
        return "constant", "—", "—", "—", _code(name, CONSTANT_LIMIT)
    if obj.get("kind") == "flat_file":
        return "file column", "—", "—", _code(f"file connection {obj.get('name')} (package {obj.get('scope')})",
                                              NAME_LIMIT), _code(name)
    if obj.get("kind") == "package_flow":
        package, _, task = (obj.get("scope") or "").partition("::")
        return "package data flow column", "—", "—", _code(f"{obj.get('name')} ({task}, package {package})",
                                                           NAME_LIMIT), _code(name)
    if obj.get("kind") in ("temp_table", "table_variable"):
        return "temp table column", "—", "—", _code(
            f"{obj.get('name')} (in {(obj.get('scope') or '').rsplit('::', 1)[-1]})", NAME_LIMIT), _code(name)
    if obj:
        db = obj.get("database")
        server = f"{obj['server']}." if obj.get("server") else ""
        return "DB column", (_code(server + db) if db else "unknown"), _code(obj.get("schema") or "?"), \
            _code(obj.get("name") or "?"), _code(name)
    label = {"parameter": "procedure parameter", "variable": "variable",
             "package_variable": "package parameter" if name.startswith("$") or "::$" in node_id
             else "package variable"}.get(kind, _text(kind or "value"))
    return label, "—", "—", _code(_truncate(t.display(node_id), NAME_LIMIT)), "—"


def _section_sources(a: Analysis, detailed: str) -> list[str]:
    t = a.t
    ends: list[str] = []
    for fam in a.families:
        for end in fam["ends"]:
            if end not in ends:
                ends.append(end)
    if not ends:
        if t.stops_by_node.get(t.target_id):
            return [_no_route_sentence(a)]
        return ["No end points were recorded."]
    by_node = {e["node"]: e for e in a.ends}
    out = ["One row per end point. Files, constants and variables are labelled in the first column and never "
           "shown as databases.", "",
           "| Kind | Database | Schema | Table / source | Column | Family | Why it stops | Proof of the step reaching it |",
           "|---|---|---|---|---|---|---|---|"]
    for node_id in ends[:MAX_ENDPOINTS]:
        fams = a.families_by_end.get(node_id, [])
        cells = list(_endpoint_cells(a, node_id))
        stop_kinds = {f["stop"] for f in fams}
        kind = "cycle" if stop_kinds == {"cycle"} else a.end_kind(node_id)
        why = _end_text(a, node_id, kind)
        if kind == "cycle" and t.kind(node_id) != "prior_state":
            why += f" ([cycles]({detailed}#gaps))"
        roles = {"condition" if f["role"] == "condition" else "value" for f in fams}
        if roles == {"condition"}:
            why += " (decides between values)"
        elif len(roles) > 1:
            why += " (value, and decides between values)"
        info = by_node.get(node_id)
        statuses = info["statuses"] if info else sorted({f["status"] for f in fams}, key=_status_rank)
        steps = info["steps"] if info else sorted({s for f in fams for m in f["members"] if m["end"] == node_id
                                                   for s in m["steps"][-1:]}, key=a._step_order)
        proof = ", ".join(STATUS_SHORT.get(s, s) for s in statuses) or "—"
        if steps:
            proof += " (" + _step_refs(a, steps, detailed, 2) + ")"
        family = ", ".join(_fam_link(f, detailed) for f in fams[:3]) + (", …" if len(fams) > 3 else "")
        out.append("| " + " | ".join(_cell(c) for c in cells + [family, why, proof]) + " |")
    if len(ends) > MAX_ENDPOINTS:
        rest = Counter(_short_kind(a, n, a.end_kind(n)) for n in ends[MAX_ENDPOINTS:])
        out += ["", f"And {len(ends) - MAX_ENDPOINTS} more end points ("
                + ", ".join(f"{_end_short(k)}: {v}" for k, v in sorted(rest.items())) + f") — see the "
                f"[route families]({detailed}#route-families) and the JSON."]
    return out


# --------------------------------------------------------------------------- 7. what changes the value

def _step_target_short(a: Analysis, step: dict) -> str:
    return _code(_truncate(a.t.display(step.get("target")), 60))


def _step_writer(step: dict) -> str:
    proc = step.get("procedure") or ""
    if proc.startswith("package "):
        package = proc[len("package "):].split(", task ", 1)[0]
        component = proc.split(", component ", 1)[1] if ", component " in proc else None
        return f"package {_code(package)}" + (f", component {_code(component)}" if component else "")
    return f"{_code(_norm_name(proc))}" if proc else "a script or view"


def _input_names(a: Analysis, inputs: list[dict], limit: int = 3) -> str:
    names = []
    for inp in inputs:
        name = _node_short(a, inp.get("node") or "?", 50)
        if name not in names:
            names.append(name)
    return _limited(names, limit)


def _section_changes(a: Analysis, detailed: str) -> list[str]:
    t = a.t
    kinds: dict[str, list[str]] = {}
    other: dict[str, list[str]] = {}
    for step in t.ordered:
        sid = step["id"]
        if sid not in a.value_steps:
            continue
        inputs = step.get("inputs") or []
        ref = _step_ref(a, sid, detailed)
        writer, target = _step_writer(step), _step_target_short(a, step)
        tagged = [(i, _input_tags(i)) for i in inputs]
        case_values = [i for i, tags in tagged if {"case_when", "case_else"} & set(tags)]
        conds = [i for i, tags in tagged if "condition" in tags]
        n_conds = len({i.get("branch") for i in conds})
        if case_values:
            kinds.setdefault("case", []).append(
                f"**CASE/IIF choice** in {writer} writing {target}: "
                + (f"{_plural(n_conds, 'condition')} on {_input_names(a, conds, 2)} "
                   f"{'decides' if n_conds == 1 else 'decide'} between " if conds else "chooses between ")
                + f"{_input_names(a, case_values, 3)} — {ref}")
        args = sorted(((int(re.search(r"argument (\d+)", i.get("branch") or "").group(1)), i) for i, tags in tagged
                       if any(tg == "primary" or tg.startswith("fallback") for tg in tags)), key=lambda x: x[0])
        if args:
            kinds.setdefault("fallback", []).append(
                f"**Fallback (COALESCE/ISNULL)** in {writer} writing {target}: first non-null of "
                f"{_input_names(a, [i for _, i in args], 4)}, in that order — {ref}")
        tags = a.step_tags.get(sid, [])
        if "override" in tags or "override_unordered" in tags:
            when = ("runs after the other writers in the same procedure" if "override" in tags
                    else "run order relative to the other writers is not proved")
            kinds.setdefault("override", []).append(
                f"**Override** by UPDATE in {writer}: replaces {target} for the rows it matches with "
                f"{_input_names(a, [i for i in inputs if (i.get('role') or 'value') == 'value'], 2)} ({when}) — {ref}")
        if step.get("writer_kind") in ("merge_update", "merge_insert"):
            what = "updates matched rows" if step["writer_kind"] == "merge_update" else "inserts rows not matched"
            kinds.setdefault("merge", []).append(f"**MERGE** in {writer} {what} of {target} — {ref}")
        op = step.get("operation")
        if op in ("aggregate", "window"):
            kinds.setdefault("aggregate", []).append(
                f"**{'Aggregation' if op == 'aggregate' else 'Window calculation'}** in {writer} writing {target} — {ref}")
        elif (step.get("procedure") or "").startswith("package ") and op not in ("direct", "rename", None):
            kinds.setdefault("package", []).append(
                f"**Package transformation** ({_text(OPERATION_TEXT.get(op, op))}) in {writer} — {ref}")
        elif op in ("cast", "arithmetic", "function", "string", "expression", "subquery") and not case_values and not args:
            other.setdefault(OPERATION_TEXT.get(op, op), []).append(sid)
    out: list[str] = []
    for kind in ("case", "fallback", "override", "merge", "aggregate", "package"):
        items = kinds.get(kind, [])
        out += [f"- {item}" for item in items[:MAX_PER_CHANGE]]
        if len(items) > MAX_PER_CHANGE:
            out.append(f"  - and {len(items) - MAX_PER_CHANGE} more of this kind — see the "
                       f"[detailed steps]({detailed}#detailed-steps)")
    if other:
        out.append("- **Other calculations:** " + "; ".join(
            f"{_text(op)} ({_step_refs(a, ids, detailed, 2)})" for op, ids in sorted(other.items())))
    if len(out) > MAX_CHANGES:
        hidden = len(out) - (MAX_CHANGES - 1)
        out = out[:MAX_CHANGES - 1] + [f"- and {hidden} more lines — see the [detailed steps]({detailed}#detailed-steps)"]
    if not out:
        return ["No CASE choice, fallback, override, merge, aggregation or package transformation on the value routes: "
                "values are copied or renamed."]
    return out


# --------------------------------------------------------------------------- 8. which rows?

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
    rows = sum((a.comp.get("row_selection_gaps_by_kind") or {}).values())
    if not by_object:
        return ["No row-selection conditions were recorded on the value routes: every row of the sources shown "
                "above can contribute."]
    out = [f"These conditions decide **which rows** are included; they do not compute the value. "
           f"{_plural(total, 'condition')} on {_plural(len(by_object), 'object')}"
           + (f"; {_plural(rows, 'row-selection open point')} (not value gaps)" if rows else "") + ":", ""]
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


# --------------------------------------------------------------------------- 9. evidence and uncertainty

def _section_uncertainty(a: Analysis, detailed: str) -> list[str]:
    t, comp, data = a.t, a.comp, a.data
    statuses = comp.get("steps_by_status") or {}
    total = sum(statuses.values())
    out = []
    if total:
        main = ", ".join(f"**{statuses.get(s, 0)} {s}**" for s in ("verified", "inferred", "asserted"))
        other = [f"{n} {s}" for s, n in sorted(statuses.items(), key=lambda kv: _status_rank(kv[0]))
                 if s not in ("verified", "inferred", "asserted")]
        meanings = [STATUS_MEANING[s] for s in sorted(statuses, key=_status_rank) if s in STATUS_MEANING]
        out.append(f"- **Steps on the value routes:** {total} — {main}" + (", " + ", ".join(other) if other else "")
                   + ". Verified = proved from explicit code." + (" " + _sentence("; ".join(meanings)) if meanings else ""))
    else:
        out.append("- **Steps on the value routes:** none were proved.")
    weak: dict[str, list[str]] = {}
    for step in t.ordered:
        status = step.get("status") or "verified"
        if step["id"] in a.value_steps and status != "verified":
            weak.setdefault(status, []).append(step["id"])
    if weak:
        out.append("- **Not proved from code:** " + "; ".join(
            f"{s} {_step_refs(a, ids, detailed, 3)}" for s, ids in sorted(weak.items(), key=lambda kv: _status_rank(kv[0])))
            + " — never read these as code-proved.")
    asserted = comp.get("asserted_steps") or 0
    if asserted or data.get("assertions_used"):
        files = sorted({ev.get("path") for s in t.steps if s["id"] in a.value_steps for ev in s.get("evidence") or []
                        if ev.get("role") == "assertion" and ev.get("path")})
        ids = ", ".join(_code(i) for i in sorted(data.get("assertions_used") or [])) or "—"
        out.append(f"- **Assertions:** relies on {_plural(asserted, 'asserted step')} (assertion ids {ids}"
                   + (f"; record: {', '.join(f'[{_text(f)}]({_link_target(f)})' for f in files)}" if files else "")
                   + ") — mappings recorded in an assertions file, not proved from code ("
                   + assertion_provenance_text(data) + ")"
                   + (f"; {_plural(comp['gaps_resolved_by_assertions'], 'gap')} marked explained by them"
                      if comp.get("gaps_resolved_by_assertions") else "") + ".")
    else:
        out.append("- **Assertions:** none used; nothing here rests on an asserted mapping.")
    counts = _end_kinds(a)
    no_writer, files = counts.get("source_without_writer", 0), counts.get("external_file", 0)
    if no_writer or files:
        parts = ([_plural(no_writer, "end point")+" with no writer in the selected sources"] if no_writer else []) + \
                ([_plural(files, "external file")] if files else [])
        out.append(f"- **Unresolved origins:** {' and '.join(parts)}. The step reaching them may be verified, but who "
                   "populates them is not proved — they are stopping points, not originating systems.")
    value = sum((comp.get("value_gaps_by_kind") or {}).values())
    context = sum((comp.get("context_gaps_by_kind") or {}).values())
    rows = sum((comp.get("row_selection_gaps_by_kind") or {}).values())
    out.append(f"- **Open points:** {value} on the value, {context} of context, {rows} on row selection only "
               f"(which rows, not the value) — [gaps and next evidence]({detailed}#gaps).")
    files_seen: dict[str, list[int]] = {}
    for step in t.ordered:
        if step["id"] not in a.value_steps:
            continue
        for ev in (step.get("evidence") or [])[:1]:
            if ev.get("path") and ev.get("role") != "assertion":
                where = "/".join(p for p in ((ev.get("root") if t.show_root else None), ev["path"]) if p)
                files_seen.setdefault(where, []).append(ev.get("start_line") or 0)
    if files_seen:
        ordered = sorted(files_seen.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        shown = [f"{_code(f)} ({_plural(len(lines), 'step')}, "
                 + (f"line {lines[0]})" if min(lines) == max(lines) else f"lines {min(lines)}–{max(lines)})")
                 for f, lines in ordered[:3]]
        out.append("- **Source locations:** " + _limited(shown, 3) + (f", and {len(ordered) - 3} more files" if len(ordered) > 3 else "")
                   + f" — every step's location and excerpt is on the [detailed page]({detailed}#detailed-steps).")
    return out


# --------------------------------------------------------------------------- 10. routes, open points, links

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
    out = [f"{_plural(len(routes), 'object route')}: each is the sequence of objects the value passes, from this "
           "field back to where the trace ended (← reads \"comes from\"); branch labels are listed, not combined. "
           f"Exact expressions are on the [detailed page]({detailed}#value-lineage)."]
    if a.cycle_hit:
        out[0] += " Some values feed each other (a loop); the loop is followed once."
    for index, route in enumerate(routes[:MAX_ROUTES], 1):
        agg = route["agg"]
        seq = [a.label_of(k) for k in route["seq"]] or [t.display(t.target_id)]
        if len(seq) > MAX_ROUTE_OBJECTS:
            hidden = len(seq) - (MAX_ROUTE_OBJECTS - 1)
            seq = seq[:3] + [f"… {hidden} more objects …"] + seq[-(MAX_ROUTE_OBJECTS - 4):]
        path = " ← ".join(_code(_truncate(s, NAME_LIMIT)) for s in seq)
        kinds = Counter(_short_kind(a, n, a.route_end_kind(n, info)) for n, info in agg.ends.items())
        summary = ", ".join(f"{v} {_end_short(k)}" for k, v in sorted(kinds.items(), key=lambda kv: (
            END_ORDER.index(kv[0]) if kv[0] in END_ORDER else 99)))
        out += ["", f"**Route {index}** ({_plural(len(agg.ends), 'end')}: {summary}): {path}"]
        shared, own = _route_labels(route)
        if shared:
            out.append("- Branches on the way: " + "; ".join(_code(label) for label in shared[:MAX_LABELS])
                       + (f"; and {len(shared) - MAX_LABELS} more" if len(shared) > MAX_LABELS else ""))
        steps = [a.steps_by_id[s] for s in agg.steps if s in a.steps_by_id]
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
           f"[route families]({detailed}#route-families), "
           f"[row selection]({detailed}#{rows}), [steps and evidence]({detailed}#{steps}), "
           f"[gaps and next evidence]({detailed}#{gaps})",
           f"- Full trace (source of truth): [{_text(Path(json_name).name)}]({_link_target(json_name)}) — "
           "route families with every member route are in its `route_families` block"]
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
        ("Field and definition", _section_field(a, entry, others)),
        ("Plain answer", _section_answer(a, detailed)),
        ("Coverage", _section_coverage(a, detailed)),
        ("Main evidence needed", _section_evidence_needed(a, detailed)),
        ("Route families", _section_families(a, detailed)),
        ("Sources at a glance", _section_sources(a, detailed)),
        ("What changes the value", _section_changes(a, detailed)),
        ("Which rows?", _section_rows(a, detailed)),
        ("Evidence and uncertainty", _section_uncertainty(a, detailed)),
        ("Routes: how the value arrives", _section_routes(a, detailed)),
        ("Open points", _section_open(a, detailed)),
        ("Links", _section_links(a, json_name, detailed, index)),
    )
    for title, body in sections:
        out += [f"## {title}", ""] + body + [""]
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- detailed page: family list

def detailed_family_lines(data: dict, t: _Trace) -> list[str]:
    """The "Route families" subsection of the detailed page: every family with an anchor and its member
    routes (capped per family; the JSON ``route_families`` block keeps them all)."""
    a = Analysis(data)
    out = ['<a id="route-families"></a>', "", "### Route families", ""]
    if not a.families:
        return out + ["No value route could be followed from this field."]
    out += [f"{_plural(len(a.families), 'family', 'families')}, "
            f"{_plural(sum(f['route_count'] for f in a.families), 'value route')}. A family groups routes with the "
            "same source family, value rule, stopping reason and evidence status; alternatives with different "
            "logic, confidence or stopping reason are separate families. Ends are where the trace stopped, not "
            "confirmed origins.", ""]
    for fam in a.families:
        lower = "at least " if fam["route_count_is_lower_bound"] else ""
        out.append(f'- <a id="{_anchor(fam["id"])}"></a>**{fam["label"]}** `{fam["id"]}` — source '
                   f"{_text(fam['source']['label'])}; value rule: {_text(fam['value_rule_text'])}; stops: "
                   f"{_text(fam['stop_text'])}; evidence: {_text(FAMILY_STATUS_TEXT.get(fam['status'], fam['status']))}; "
                   f"{lower}{_plural(fam['route_count'], 'route')}")
        for member in fam["members"][:DETAIL_FAMILY_ROUTES]:
            steps = member["steps"]
            seqs = len(member["object_sequences"])
            links = ", ".join(t.step_link({"id": s}) for s in steps[:6] if s in t.label) \
                + (f" and {len(steps) - 6} more" if len(steps) > 6 else "")
            line = f"  - {_code(_truncate(t.display(member['end']), NAME_LIMIT))}"
            if seqs > 1:
                line += f" ({seqs} object sequences)"
            line += f" — steps {links or '—'}"
            if member["branches"]:
                line += " — branches " + ", ".join(_code(b) for b in member["branches"][:3]) \
                    + (", …" if len(member["branches"]) > 3 else "")
            if member["gaps"]:
                line += " — open points " + ", ".join(t.gap_link(g) for g in member["gaps"][:3] if g in t.gaps)
            out.append(line)
        if len(fam["members"]) > DETAIL_FAMILY_ROUTES:
            out.append(f"  - and {len(fam['members']) - DETAIL_FAMILY_ROUTES} more ends — all are in the JSON "
                       "`route_families` block")
    return out


# --------------------------------------------------------------------------- route index

def build_index(items: list[dict], glossary: list[dict] | None = None) -> dict:
    """Route index JSON from ``[{"data": trace dict, "overview": link, "trace": link, "analysis": Analysis?}]``."""
    columns, sources, writers, routes, families = [], {}, {}, {}, {}
    labels: dict[str, dict[str, str]] = {"objects": {}, "writers": {}}   # key -> label, stored once
    unresolved: dict[tuple[str, str], dict] = {}
    writer_order: dict[str, dict] = {}
    assertions: dict[str, set] = {}
    definitions: list[dict] = []
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
        for fam in a.families:
            entry = families.setdefault(fam["id"], {
                "id": fam["id"], "source": fam["source"], "role": fam["role"], "value_rule": fam["value_rule"],
                "value_rule_text": fam["value_rule_text"], "stop": fam["stop"], "stop_text": fam["stop_text"],
                "status": fam["status"], "columns": [], "routes": 0})
            if requested not in entry["columns"]:
                entry["columns"].append(requested)
            entry["routes"] += fam["route_count"]
            if _is_unresolved(fam["stop"].split(":")[0]) or fam["stop"] == "declared":
                kind = "gap" if fam["stop"].startswith("gap:") else fam["stop"]
                group = unresolved.setdefault((fam["source"]["key"], kind), {
                    "source": fam["source"], "stop": kind, "stop_text": stop_short(kind) if kind != "gap"
                    else "open point", "columns": set(), "routes": 0})
                group["columns"].add(requested)
                group["routes"] += fam["route_count"]
        for gap in a.t.sorted_gaps():
            if gap.get("kind") != "ambiguous_writer_order" or gap.get("affects") == "row_selection":
                continue
            where = a.t.display(gap.get("node")) if gap.get("node") else "procedure or statement level"
            entry = writer_order.setdefault(where.lower(), {
                "object": where, "locations": sorted({a.t.location(ev) for ev in gap.get("evidence") or []}),
                "columns": set()})
            entry["columns"].add(requested)
        for assertion in data.get("assertions_used") or []:
            assertions.setdefault(assertion, set()).add(requested)
        gentry, _ = glossary_entry(glossary, data)
        if gentry is None or gentry["status"] != "approved":
            definitions.append({"column": requested, "glossary_status": gentry["status"] if gentry else "missing"})
        columns.append({"column": requested, "target": a.t.target_id, "result": result,
                        "status": comp.get("status"), "relies_on_assertions": bool(comp.get("relies_on_assertions")),
                        "overview": item.get("overview"), "trace": item.get("trace"),
                        "source_signature": src_id, "writer_signature": wr_id,
                        "route_signatures": sorted(route_ids), "ends": len(a.ends),
                        "terminal_sources": len(src), "writers": len(wr),
                        "route_families": [f["id"] for f in a.families],
                        "value_routes": sum(f["route_count"] for f in a.families),
                        "definition_status": gentry["status"] if gentry else "missing"})
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
    for fam in families.values():
        fam["columns"].sort(key=str.lower)

    def by_count(rows):
        for row in rows:
            row["columns"] = sorted(row["columns"], key=str.lower)
        return sorted(rows, key=lambda r: (-len(r["columns"]), -r.get("routes", 0), json.dumps(r, sort_keys=True, default=str)))
    attention = {
        "note": "What to look at first, by the number of columns affected. Unresolved origins are ends with no "
                "writer in the selected sources, external files, parameters without callers, depth cuts, loops "
                "and open points (declared terminals are listed too: their upstream is not proved). Assertions "
                "are recorded mappings awaiting independent review (unless the assertions file marks them reviewed), not code. Definitions are awaiting owner approval when "
                "the glossary has no approved entry.",
        "unresolved_origins": by_count(list(unresolved.values())),
        "ambiguous_writer_order": by_count(list(writer_order.values())),
        "assertions_awaiting_review": by_count([{"assertion": k, "columns": v} for k, v in assertions.items()]),
        "definitions_awaiting_approval": {
            "glossary_supplied": glossary is not None, "count": len(definitions),
            "columns": sorted(definitions, key=lambda d: d["column"].lower())},
    }
    return {
        "schema_version": INDEX_SCHEMA,
        "note": "Columns grouped by the set of terminal source objects of their value-route ends (files grouped "
                "by connection; constants left out) and by the set of writer procedures, packages and scripts "
                "on their value routes. A shared signature does not mean identical expressions, branch "
                "selection or business meaning, and no group replaces a column's own trace: each column's "
                "JSON and detailed page list all of its contributors and exceptions. End points are where "
                "the traces stopped, not confirmed source systems. Object and writer keys are explained in "
                "'objects' and 'writers'; a route signature is the sequence of objects upstream of the target's "
                "own object. A route family id hashes source family, value rule, stopping reason and evidence "
                "status; columns sharing a family id share that pattern, not their meaning.",
        "objects": dict(sorted(labels["objects"].items())),
        "writers": dict(sorted(labels["writers"].items())),
        "columns": columns,
        "source_signatures": finish(sources),
        "writer_signatures": finish(writers),
        "route_signatures": sorted(routes.values(), key=lambda r: (-len(r["columns"]), r["id"])),
        "route_families": sorted(families.values(), key=lambda f: (-len(f["columns"]), f["id"])),
        "needs_attention": attention,
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


def _render_attention(index: dict, member) -> list[str]:
    att = index.get("needs_attention") or {}
    out = ["## Needs attention", "", _text(att.get("note") or ""), ""]

    def columns(row) -> str:
        names = row["columns"]
        return f"{_plural(len(names), 'column')}: " + _limited([member(c) for c in names], 5)

    rows = att.get("unresolved_origins") or []
    out.append(f"### Unresolved origins ({_plural(len(rows), 'source family', 'source families')})")
    out.append("")
    if rows:
        for row in rows[:INDEX_MAX_ATTENTION]:
            source = row["source"]
            label = _code(source["label"]) if source["kind"] == "database" else _text(source["label"])
            out.append(f"- {label} — {_text(row['stop_text'])} ({_plural(row['routes'], 'route')}); {columns(row)}")
        if len(rows) > INDEX_MAX_ATTENTION:
            out.append(f"- and {len(rows) - INDEX_MAX_ATTENTION} more — see routes-index.json")
    else:
        out.append("- none")
    rows = att.get("ambiguous_writer_order") or []
    out += ["", f"### Ambiguous writer order ({len(rows)})", ""]
    if rows:
        for row in rows[:INDEX_MAX_ATTENTION]:
            where = ", ".join(_code(loc) for loc in row["locations"][:3])
            out.append(f"- {_code(row['object'])} (writers at {where or '—'}); {columns(row)}")
        if len(rows) > INDEX_MAX_ATTENTION:
            out.append(f"- and {len(rows) - INDEX_MAX_ATTENTION} more — see routes-index.json")
    else:
        out.append("- none")
    rows = att.get("assertions_awaiting_review") or []
    out += ["", f"### Assertions awaiting review ({len(rows)})", ""]
    if rows:
        for row in rows[:INDEX_MAX_ATTENTION]:
            out.append(f"- assertion {_code(row['assertion'])} (recorded mapping, not code); {columns(row)}")
        if len(rows) > INDEX_MAX_ATTENTION:
            out.append(f"- and {len(rows) - INDEX_MAX_ATTENTION} more — see routes-index.json")
    else:
        out.append("- none")
    defs = att.get("definitions_awaiting_approval") or {"count": 0, "columns": []}
    out += ["", f"### Definitions awaiting owner approval ({defs['count']})", ""]
    if defs["columns"]:
        if not defs.get("glossary_supplied"):
            out.append("No business glossary was supplied, so no column has an approved definition.")
            out.append("")
        status = {"missing": "no glossary entry", "proposed": "proposed interpretation only"}
        shown = [f"{member(d['column'])} ({status.get(d['glossary_status'], d['glossary_status'])})"
                 for d in defs["columns"]]
        out.append("- " + _limited(shown, INDEX_MAX_SHARED, "see routes-index.json"))
    else:
        out.append("- none: every column has an approved definition")
    return out + [""]


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
           f"{_plural(len(index['writer_signatures']), 'writer signature')}; "
           f"{_plural(len(index.get('route_families') or []), 'route family', 'route families')}. Machine-readable form: "
           "[routes-index.json](routes-index.json).", "",
           "> A shared signature means the columns end at the same set of objects (or pass the same set of "
           "writers). It does **not** mean identical expressions, branch selection or business meaning — a shared "
           "signature is not a shared transformation or meaning. Each "
           "column's own overview, detailed page and JSON stay complete: they list all of its contributors and "
           "exceptions. End points are where the traces stopped, not confirmed source systems.", ""]
    if "needs_attention" in index:
        out += _render_attention(index, member)
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
    out += ["## Columns", "", "| Column | Result | Definition | Terminal sources | Writers | Routes | Families |",
            "|---|---|---|---|---|---|---|"]
    for row in index["columns"]:
        out.append("| " + " | ".join(_cell(c) for c in (
            member(row["column"]), _text(row["result"]), _text(row.get("definition_status", "missing")),
            f"{_code(row['source_signature'])} ({row['terminal_sources']})",
            f"{_code(row['writer_signature'])} ({row['writers']})", str(len(row["route_signatures"])),
            str(len(row.get("route_families") or [])))) + " |")
    return "\n".join(out) + "\n"


def write_index(index: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md, js = out_dir / "routes-index.md", out_dir / "routes-index.json"
    with open(md, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_index(index))
    with open(js, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(index, indent=2, ensure_ascii=False, default=sorted) + "\n")
    return [md, js]


def index_counts(index: dict) -> dict:
    """Name-free counts for gap-summary.json."""
    sizes = lambda key: sorted((len(g["columns"]) for g in index[key]), reverse=True)  # noqa: E731
    att = index.get("needs_attention") or {}
    return {"columns": len(index["columns"]),
            "source_signatures": len(index["source_signatures"]), "writer_signatures": len(index["writer_signatures"]),
            "route_signatures": len(index["route_signatures"]),
            "route_families": len(index.get("route_families") or []),
            "largest_source_groups": sizes("source_signatures")[:5],
            "largest_writer_groups": sizes("writer_signatures")[:5],
            "needs_attention": {"unresolved_origin_families": len(att.get("unresolved_origins") or []),
                                "ambiguous_writer_order": len(att.get("ambiguous_writer_order") or []),
                                "assertions_awaiting_review": len(att.get("assertions_awaiting_review") or []),
                                "definitions_awaiting_approval":
                                    (att.get("definitions_awaiting_approval") or {}).get("count", 0)}}


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
                             for d, j, p, a in prepared], glossary)
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
