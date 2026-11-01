"""Column lineage trace model and its versioned JSON form.

The model is the contract between the analyzer, the tracer and the renderers.
Everything a report says must be derivable from a ``TraceResult``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

SCHEMA_VERSION = "column-lineage/1.0"

# Step and trace statuses, strongest first.
VERIFIED = "verified"      # every input is bound from explicit SQL evidence
INFERRED = "inferred"      # bound using a stated rule (positional schema, default schema, caller temp table)
ASSERTED = "asserted"      # stated by a person in an assertions file; not proved from SQL
PARTIAL = "partial"        # some inputs are bound, others are gaps
AMBIGUOUS = "ambiguous"    # more than one candidate binding or writer order exists
UNRESOLVED = "unresolved"  # no binding could be established
STATUS_ORDER = (VERIFIED, INFERRED, ASSERTED, PARTIAL, AMBIGUOUS, UNRESOLVED)

# Node kinds.
COLUMN = "column"              # column of a persistent table or view
TEMP_COLUMN = "temp_column"    # column of a #temp table or @table variable, scoped
LOCAL_COLUMN = "local_column"  # column of a CTE or derived table inside one statement
VARIABLE = "variable"          # @variable, scoped to a procedure or batch
PARAMETER = "parameter"        # procedure parameter
CONSTANT = "constant"          # literal value
UNKNOWN = "unknown"            # an input that could not be identified
PACKAGE_COLUMN = "package_column"      # column inside an SSIS data flow (component output), scoped to package and task
FILE_COLUMN = "file_column"            # column of a flat file read by an SSIS package
PACKAGE_VARIABLE = "package_variable"  # SSIS variable or parameter, scoped to a package
PACKAGE_OBJECT_KINDS = ("package_flow", "flat_file")  # ObjectRef kinds of package-internal objects
PRIOR_STATE = "prior_state"    # the earlier content of a column (or earlier value of a variable) that a
                               # write reads back: the value *before* the write, where the node itself is
                               # the value *after* it. ``of`` holds the kind of the node it precedes.

# Input roles. VALUE inputs compute the target value; CONDITION inputs choose
# between value alternatives (CASE/IIF predicates, simple CASE operands).
VALUE = "value"
CONDITION = "condition"
WINDOW = "window"              # PARTITION BY / ORDER BY of a window function

# Row-selection roles: these decide which rows contribute, not the value.
FILTER = "filter"              # WHERE
JOIN_PREDICATE = "join_predicate"  # non-equality parts of ON
JOIN_KEY = "join_key"          # equality columns of ON / MERGE ON / correlated keys
GROUP_BY = "group_by"
HAVING = "having"
QUALIFY = "qualify"
TOP_ORDER = "top_order"        # ORDER BY of a TOP n / OFFSET-FETCH query
MERGE_CONDITION = "merge_condition"  # WHEN MATCHED AND ... predicates

# Gap kinds. A gap is a place where proof stops for a reason other than the
# caller's boundary or a source with no writer.
GAP_KINDS = (
    "dynamic_sql", "parse_error", "unsupported_statement", "insert_exec",
    "missing_definition", "unresolved_alias", "ambiguous_column", "ambiguous_object",
    "ambiguous_writer_order", "positional_without_schema", "wildcard_without_schema",
    "column_count_mismatch", "opaque_function", "table_valued_function", "external_object",
    "unresolved_variable", "runtime_branch", "cycle", "depth_limit", "column_not_written", "unexplained_end",
    # SSIS packages (ssis.py)
    "unsupported_package_step", "ambiguous_package_copy", "package_runtime_value", "package_expression",
    "package_metadata", "package_column_not_mapped", "package_database_unknown",
)


def quote_part(part: str) -> str:
    """Bracket an identifier part that would otherwise be ambiguous in a dotted id."""
    return f"[{part.replace(']', ']]')}]" if ("." in part or "]" in part or "[" in part) else part


def stable_id(prefix: str, *parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def worst(*statuses: str) -> str:
    present = [s for s in statuses if s]
    return max(present, key=STATUS_ORDER.index) if present else VERIFIED


@dataclass(frozen=True)
class ObjectRef:
    """A data object. ``database`` is None when no context establishes it."""
    name: str
    schema: str | None = None
    database: str | None = None
    server: str | None = None
    kind: str = "table"            # table, view, temp_table, table_variable, cte, derived, procedure
    scope: str | None = None       # procedure/batch scope for temp objects, statement id for CTEs
    database_source: str | None = None  # explicit, use_statement, configured, default, None

    @property
    def key(self) -> str:
        if self.kind in ("temp_table", "table_variable", "cte", "derived") + PACKAGE_OBJECT_KINDS:
            return f"{self.kind}:{self.scope}::{quote_part(self.name)}"
        parts = [p for p in (self.server, self.database or "?", self.schema or "?", self.name)]
        return ".".join(quote_part(p) for p in parts if p is not None)

    @property
    def display(self) -> str:
        if self.kind in ("temp_table", "table_variable"):
            return f"{self.name} (in {self.scope_label})"
        if self.kind in ("cte", "derived"):
            return f"{self.kind} {self.name}"
        if self.kind in PACKAGE_OBJECT_KINDS:
            return f"{self.name} (in {self.scope})"
        return ".".join(p for p in (self.server, self.database or "?", self.schema or "?", self.name) if p)

    @property
    def scope_label(self) -> str:
        return (self.scope or "").rsplit("::", 1)[-1]

    def to_dict(self) -> dict:
        return {"key": self.key, "server": self.server, "database": self.database, "schema": self.schema,
                "name": self.name, "kind": self.kind, "scope": self.scope, "database_source": self.database_source}


@dataclass(frozen=True)
class Node:
    kind: str
    name: str                      # column, variable, parameter name, or literal SQL
    object: ObjectRef | None = None
    scope: str | None = None       # for variables and parameters
    of: str | None = None          # PRIOR_STATE: kind of the node whose earlier state this is

    @property
    def later(self) -> "Node":
        """PRIOR_STATE: the node whose earlier state this is."""
        return Node(self.of, self.name, self.object, self.scope) if self.kind == PRIOR_STATE else self

    @property
    def id(self) -> str:
        if self.kind == PRIOR_STATE:
            return f"{PRIOR_STATE}:{self.later.id}"
        if self.object is not None:
            return f"{self.object.key}.{quote_part(self.name)}"
        if self.kind in (VARIABLE, PARAMETER):
            return f"{self.kind}:{self.scope}::{self.name}"
        return f"{self.kind}:{self.name}"

    @property
    def display(self) -> str:
        if self.kind == PRIOR_STATE:
            later = self.later
            noun = "value" if later.kind in (VARIABLE, PARAMETER, PACKAGE_VARIABLE) else "content"
            return f"earlier {noun} of {later.display}"
        if self.object is not None and self.object.kind in PACKAGE_OBJECT_KINDS:
            return f"{self.object.name}.{self.name} (in {self.object.scope})"
        if self.object is not None:
            return f"{self.object.display}.{self.name}" if self.object.kind not in ("temp_table", "table_variable") \
                else f"{self.object.name}.{self.name} (in {self.object.scope_label})"
        if self.kind in (VARIABLE, PARAMETER):
            return f"{self.name} (in {(self.scope or '').rsplit('::', 1)[-1]})"
        return self.name

    def to_dict(self) -> dict:
        out = {"id": self.id, "kind": self.kind, "name": self.name, "display": self.display,
               "object": self.object.to_dict() if self.object else None, "scope": self.scope}
        if self.kind == PRIOR_STATE:
            out["earlier_state_of"] = self.later.id
            out["of_kind"] = self.of
        return out


@dataclass(frozen=True)
class Evidence:
    path: str                      # path relative to its source root, forward slashes
    start_line: int
    end_line: int
    excerpt: str                   # short SQL excerpt (expression or clause), whitespace-collapsed
    role: str = "statement"        # statement, expression, target_column, filter, join, condition, ...
    statement_id: str | None = None
    root: str | None = None        # label of the source root

    def to_dict(self) -> dict:
        return {"root": self.root, "path": self.path, "start_line": self.start_line, "end_line": self.end_line,
                "role": self.role, "excerpt": self.excerpt, "statement_id": self.statement_id}

    @property
    def location(self) -> str:
        lines = f"{self.start_line}" if self.start_line == self.end_line else f"{self.start_line}-{self.end_line}"
        return f"{self.path}:{lines}"


@dataclass
class Input:
    """One contributor to a step's value."""
    node: Node
    role: str                      # VALUE, CONDITION, WINDOW
    expression: str                # the column reference / literal as written
    branch: str | None = None      # e.g. "WHEN r.a IS NOT NULL THEN", "ELSE", "COALESCE argument 2"
    status: str = VERIFIED
    gap: str | None = None         # gap id when this input could not be bound
    note: str | None = None

    def to_dict(self) -> dict:
        return {"node": self.node.id, "role": self.role, "expression": self.expression, "branch": self.branch,
                "status": self.status, "gap": self.gap, "note": self.note}


@dataclass
class RowSelection:
    """A column that decides which rows reach the target, not their value."""
    node: Node
    role: str                      # FILTER, JOIN_KEY, JOIN_PREDICATE, GROUP_BY, HAVING, MERGE_CONDITION
    expression: str                # the whole predicate or clause item
    evidence: Evidence | None = None
    status: str = VERIFIED
    gap: str | None = None

    def to_dict(self) -> dict:
        return {"node": self.node.id, "role": self.role, "expression": self.expression,
                "evidence": self.evidence.to_dict() if self.evidence else None, "status": self.status, "gap": self.gap}


@dataclass
class Step:
    """One mapping: how ``target`` gets its value from ``inputs`` in one writer and branch."""
    target: Node
    writer_kind: str               # insert_select, insert_values, insert_omitted, select_into, update, merge_update,
                                   # merge_insert, view, cte, derived, variable_assignment, parameter_binding,
                                   # parameter_default, computed_column
    expression: str                # the value expression as written, or the source column for a projection
    operation: str                 # direct, rename, cast, case, coalesce, arithmetic, function, aggregate, constant,
                                   # string, window, subquery, expression
    inputs: list[Input] = field(default_factory=list)
    row_selection: list[RowSelection] = field(default_factory=list)
    branch_context: list[str] = field(default_factory=list)  # IF/ELSE/WHILE, UNION branch, MERGE WHEN clause
    mapping_method: str = "projection"  # target_list, positional_schema, projection_alias, set_clause, view_column_list, ...
    status: str = VERIFIED
    procedure: str | None = None
    evidence: list[Evidence] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    order: int = 0                 # source order of the writer within its scope, for overwrite reasoning

    @property
    def id(self) -> str:
        first = self.evidence[0] if self.evidence else None
        return stable_id("step", self.target.id, self.writer_kind, self.expression, self.branch_context,
                         first.path if first else None, first.start_line if first else None, self.mapping_method)

    def to_dict(self) -> dict:
        return {"id": self.id, "target": self.target.id, "writer_kind": self.writer_kind,
                "procedure": self.procedure, "expression": self.expression, "operation": self.operation,
                "mapping_method": self.mapping_method, "status": self.status,
                "branch_context": list(self.branch_context),
                "inputs": [i.to_dict() for i in self.inputs],
                "row_selection": [r.to_dict() for r in self.row_selection],
                "evidence": [e.to_dict() for e in self.evidence],
                "gaps": sorted(self.gaps), "notes": list(self.notes), "order": self.order}


@dataclass
class Gap:
    kind: str
    message: str
    node: Node | None = None
    evidence: list[Evidence] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        first = self.evidence[0] if self.evidence else None
        return stable_id("gap", self.kind, self.node.id if self.node else None, self.message,
                         first.path if first else None, first.start_line if first else None)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "message": self.message,
                "node": self.node.id if self.node else None,
                "evidence": [e.to_dict() for e in self.evidence], "detail": self.detail}


@dataclass
class Stop:
    """Where a value path ends without a gap."""
    node: Node
    reason: str                    # boundary, source_without_writer, constant, parameter_without_caller, max_depth,
                                   # external_file (SSIS), prior_state (earlier content whose sources are traced)
    detail: str | None = None
    writer_search: list[dict] | None = None   # source_without_writer: same-named writes not used, with why
    declared_terminal: str | None = None      # accepted as a terminal by the run's --declared-terminals (reason)
    loop: dict | None = None                  # PRIOR_STATE stops: the dependency kept, {"reads_back": ...}

    def to_dict(self) -> dict:
        out = {"node": self.node.id, "reason": self.reason, "detail": self.detail}
        if self.writer_search is not None:
            out["writer_search"] = self.writer_search
        if self.declared_terminal is not None:
            out["declared_terminal"] = self.declared_terminal
        if self.loop is not None:
            out["loop"] = self.loop
        return out


@dataclass
class Boundary:
    databases: tuple[str, ...] = ()
    objects: tuple[str, ...] = ()  # glob patterns over object keys or display names
    max_depth: int | None = None

    def to_dict(self) -> dict:
        return {"databases": list(self.databases), "objects": list(self.objects), "max_depth": self.max_depth}

    @property
    def is_set(self) -> bool:
        return bool(self.databases or self.objects or self.max_depth is not None)


@dataclass
class TraceResult:
    target: Node
    requested: str
    boundary: Boundary
    scan: dict                     # roots, file counts, statement counts, parse statistics, source revision
    nodes: dict[str, Node] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    gaps: dict[str, Gap] = field(default_factory=dict)
    stops: list[Stop] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)
    target_status: str = VERIFIED  # how the target itself was resolved
    target_gaps: list[str] = field(default_factory=list)
    resolved_gaps: list[dict] = field(default_factory=list)   # gaps explained by assertions (kept, not deleted)
    assertions_used: list[str] = field(default_factory=list)
    assertion_provenance: dict = field(default_factory=dict)   # id -> who made it, review status

    def add_node(self, node: Node) -> str:
        self.nodes.setdefault(node.id, node)
        return node.id

    def add_gap(self, gap: Gap) -> str:
        if gap.node is not None:
            self.add_node(gap.node)
        self.gaps.setdefault(gap.id, gap)
        return gap.id

    def steps_for(self, node_id: str) -> list[Step]:
        return [s for s in self.steps if s.target.id == node_id]

    def value_path(self) -> tuple[set[str], list[Step]]:
        """Nodes and steps reachable from the target through value, condition and window inputs."""
        by_target: dict[str, list[Step]] = {}
        for step in self.steps:
            by_target.setdefault(step.target.id, []).append(step)
        nodes, steps, frontier = set(), [], [self.target.id]
        while frontier:
            node = frontier.pop()
            if node in nodes:
                continue
            nodes.add(node)
            for step in by_target.get(node, []):
                steps.append(step)
                frontier.extend(i.node.id for i in step.inputs)
        return nodes, steps

    def gap_effects(self) -> dict[str, str]:
        """Gap id -> what it affects: value (the value path), row_selection (only which rows
        contribute) or context (procedure- or statement-level uncertainty around the path)."""
        value_nodes, value_steps = self.value_path()
        value_ids = set(self.target_gaps)
        row_ids: set[str] = set()
        row_nodes: set[str] = set()
        for step in value_steps:
            value_ids |= set(step.gaps) | {i.gap for i in step.inputs if i.gap}
        for step in self.steps:
            row_ids |= {r.gap for r in step.row_selection if r.gap}
            row_nodes |= {r.node.id for r in step.row_selection}
        effects = {}
        for gap in self.gaps.values():
            node = gap.node.id if gap.node else None
            if gap.id in value_ids or (node and node in value_nodes):
                effects[gap.id] = "value"
            elif gap.id in row_ids or (node and node in row_nodes) or gap.detail.get("raised_in_row_selection"):
                effects[gap.id] = "row_selection"
            elif node and node not in value_nodes:
                # Reached only through traced row-selection columns.
                effects[gap.id] = "row_selection"
            else:
                effects[gap.id] = "context"
        return effects

    def completeness(self) -> dict:
        """Summarise whether every value path ends at a boundary or a source without writers."""
        _, value_steps = self.value_path()
        effects = self.gap_effects()
        step_statuses = [s.status for s in value_steps]
        open_gaps = sorted(self.gaps.values(), key=lambda g: (g.kind, g.id))
        counts: dict[str, int] = {}
        by_effect: dict[str, dict[str, int]] = {"value": {}, "row_selection": {}, "context": {}}
        for gap in open_gaps:
            counts[gap.kind] = counts.get(gap.kind, 0) + 1
            bucket = by_effect[effects[gap.id]]
            bucket[gap.kind] = bucket.get(gap.kind, 0) + 1
        blocking = [g for g in open_gaps if effects[g.id] != "row_selection"]
        statuses: dict[str, int] = {}
        for status in step_statuses:
            statuses[status] = statuses.get(status, 0) + 1
        overall = worst(self.target_status, *step_statuses) if value_steps else UNRESOLVED
        if not value_steps and self.target_status == VERIFIED and not blocking:
            overall = VERIFIED  # the target is itself a source or boundary column
        if blocking and overall in (VERIFIED, INFERRED, ASSERTED):
            overall = PARTIAL
        complete = overall in (VERIFIED, INFERRED, ASSERTED) and not blocking
        asserted = sum(1 for s in value_steps if s.status == ASSERTED)
        ends = _count(END_MEANING.get(s.reason, s.reason) for s in self.stops)
        for gap in open_gaps:
            if gap.kind in ("unsupported_package_step", "package_runtime_value") and effects[gap.id] != "row_selection":
                ends[gap.kind] = ends.get(gap.kind, 0) + 1
        # A boundary stop is already the strongest end; a declaration only matters for sources (no writer
        # in the selected sources, external files). Depth cuts and parameters are never declared away.
        # An earlier state whose writers are all traced on the page adds no source of its own.
        ends_needing_proof = [s for s in self.stops if s.reason not in ("boundary", "constant", "prior_state")]
        declared = sum(1 for s in ends_needing_proof if s.declared_terminal is not None)
        undeclared = [s for s in ends_needing_proof if s.declared_terminal is None]
        if not complete:
            result = "incomplete"
        elif any(s.reason == "max_depth" for s in self.stops):
            result = "complete_to_requested_depth"
        elif declared and not undeclared:
            result = "complete_to_declared_boundary"
        elif any(s.reason == "boundary" for s in self.stops) and not ends_needing_proof:
            result = "reached_requested_boundary"
        else:
            result = "complete_within_selected_scope"
        return {"status": "complete" if complete else "incomplete", "result": result,
                "ends_by_meaning": dict(sorted(ends.items())), "declared_terminal_ends": declared,
                "overall_step_status": overall,
                "steps_by_status": dict(sorted(statuses.items())), "gaps_by_kind": dict(sorted(counts.items())),
                "value_gaps_by_kind": dict(sorted(by_effect["value"].items())),
                "context_gaps_by_kind": dict(sorted(by_effect["context"].items())),
                "row_selection_gaps_by_kind": dict(sorted(by_effect["row_selection"].items())),
                "row_selection_status": "incomplete" if by_effect["row_selection"] else "complete",
                "asserted_steps": asserted, "relies_on_assertions": asserted > 0,
                "gaps_resolved_by_assertions": len(self.resolved_gaps),
                "stops_by_reason": dict(sorted(_count(s.reason for s in self.stops).items())),
                "note": "Complete means every value path ends at the requested boundary or at a source "
                        "object with no writer in the selected scanned sources, with no value-path or context gaps. "
                        "result says which: reached_requested_boundary, complete_to_declared_boundary (every "
                        "other end is a terminal declared for this run; their original stop reasons are kept and "
                        "upstream population beyond them is not proved), or complete_within_selected_scope when "
                        "some paths end at objects with no writer in the selected sources (not proof that they "
                        "are ultimate sources). Where a write reads the value its column or variable already had, that read ends at "
                        "an 'earlier content' node (stop prior_state): the value before that write, with its candidate "
                        "earlier writers traced (which of them populated the rows is not proved). "
                        "Row-selection gaps (filters and joins that could not be fully resolved) are "
                        "reported separately and do not make the value lineage incomplete. A complete "
                        "trace does not claim its stops are ultimate source-system fields."}

    def to_dict(self) -> dict:
        steps = sorted(self.steps, key=lambda s: (s.target.id, s.order, s.id))
        effects = self.gap_effects()
        return {
            "schema_version": SCHEMA_VERSION,
            "requested": self.requested,
            "target": self.target.to_dict(),
            "target_status": self.target_status,
            "target_gaps": sorted(self.target_gaps),
            "boundary": self.boundary.to_dict(),
            "scan": self.scan,
            "completeness": self.completeness(),
            "nodes": [self.nodes[k].to_dict() for k in sorted(self.nodes)],
            "steps": [s.to_dict() for s in steps],
            "stops": [s.to_dict() for s in sorted(self.stops, key=lambda s: (s.node.id, s.reason))],
            "gaps": [{**g.to_dict(), "affects": effects[g.id]}
                     for g in sorted(self.gaps.values(), key=lambda g: (g.kind, g.id))],
            "cycles": sorted(self.cycles),
            "resolved_gaps": sorted(self.resolved_gaps, key=lambda g: (g["kind"], g["id"])),
            "assertions_used": sorted(set(self.assertions_used)),
            "assertion_provenance": [self.assertion_provenance[k] for k in sorted(self.assertion_provenance)],
        }


# What a value-path end means for the reader (stop reason -> meaning).
END_MEANING = {
    "boundary": "reached_requested_boundary",
    "source_without_writer": "no_writer_in_selected_scope",
    "constant": "constant",
    "parameter_without_caller": "parameter_without_caller",
    "max_depth": "max_depth",
    "external_file": "external_file_source",
    "prior_state": "earlier_content_traced",
}


def _count(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out
