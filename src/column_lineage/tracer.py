"""Backward traversal from a target column to its contributors."""
from __future__ import annotations

from collections import deque
import fnmatch
import re

from .analyzer import Analyzer
from .catalog import Catalog, norm, split_name
from .model import (
    AMBIGUOUS, COLUMN, CONSTANT, INFERRED, LOCAL_COLUMN, PARAMETER, TEMP_COLUMN, UNKNOWN, UNRESOLVED, VALUE,
    VARIABLE, VERIFIED, WINDOW, CONDITION, Boundary, Gap, Node, ObjectRef, Stop, TraceResult, worst,
)

OVERWRITING = ("update", "merge")


class Tracer:
    def __init__(self, catalog: Catalog, analyzer: Analyzer | None = None, trace_row_selection: bool = False):
        self.catalog = catalog
        self.analyzer = analyzer or Analyzer(catalog)
        self.trace_row_selection = trace_row_selection
        self._memo: dict[tuple, tuple] = {}   # shared between traces in one run

    # ------------------------------------------------------------- targets
    def target_node(self, spec: str) -> tuple[Node | None, str, list[Gap]]:
        parts = split_name(spec)
        if len(parts) < 2:
            return None, UNRESOLVED, [Gap("missing_definition", f"Target '{spec}' must be <object>.<column>.")]
        column = norm(parts[-1])
        obj = parts[:-1][-4:]
        obj = [None] * (4 - len(obj)) + obj
        server, database, schema, name = obj
        if name.startswith("#"):
            matches = sorted({w.ref for ws in self.catalog.writers.values() for w in ws
                              if w.ref.kind == "temp_table" and w.ref.name == name.lower()}, key=lambda r: r.key)
            if len(matches) == 1:
                return Node(TEMP_COLUMN, column, matches[0]), VERIFIED, []
            gap = Gap("ambiguous_object" if matches else "missing_definition",
                      f"Temp table {name} is written in {len(matches)} scopes; trace a persistent column instead."
                      if matches else f"No writer of temp table {name} was found.")
            return Node(UNKNOWN, spec), UNRESOLVED, [gap]
        db_source = "explicit" if database else None
        if not database and self.catalog.default_database:
            database, db_source = self.catalog.default_database, "default"
        ref = ObjectRef(norm(name), norm(schema) or self.catalog.default_schema, norm(database), norm(server),
                        "table", None, db_source)
        canonical, candidates = self.catalog.canonical(ref)
        status = VERIFIED
        gaps: list[Gap] = []
        if candidates:
            node = Node(COLUMN, column, ref)
            gaps.append(Gap("ambiguous_object", f"{ref.schema}.{ref.name} exists in several databases; "
                            "qualify the target with a database.", node, detail={"candidates": candidates}))
            return node, AMBIGUOUS, gaps
        if canonical.database_source == "unified":
            status = INFERRED
        if any(w.kind == "view" for w in self.catalog.writers_for(canonical)):
            canonical = ObjectRef(canonical.name, canonical.schema, canonical.database, canonical.server, "view",
                                  None, canonical.database_source)
        node = Node(COLUMN, column, canonical)
        if not self.catalog.is_known(canonical) and not self.catalog.writers_for(canonical):
            gaps.append(Gap("missing_definition", f"No definition or writer of {canonical.display} was found in the "
                            "scanned files.", node))
            return node, UNRESOLVED, gaps
        columns, status_schema, _ = self.catalog.schema_for(canonical)
        if status_schema == "declared" and column not in [c.name for c in columns]:
            gaps.append(Gap("missing_definition", f"{canonical.display} is declared without a column {column}.", node))
            status = UNRESOLVED
        return node, status, gaps

    # ------------------------------------------------------------ boundary
    @staticmethod
    def at_boundary(node: Node, boundary: Boundary) -> str | None:
        ref = node.object
        if ref is None or ref.kind not in ("table", "view"):
            return None
        if ref.database and ref.database in {d.lower() for d in boundary.databases}:
            return f"database {ref.database}"
        names = [ref.key, f"{ref.schema}.{ref.name}", ref.name]
        for pattern in boundary.objects:
            if any(fnmatch.fnmatchcase(n, pattern.lower()) for n in names):
                return f"object pattern {pattern}"
        return None

    # --------------------------------------------------------------- trace
    def trace(self, spec: str, boundary: Boundary, scan: dict) -> TraceResult:
        target, target_status, target_gaps = self.target_node(spec)
        result = TraceResult(target or Node(UNKNOWN, spec), spec, boundary, scan)
        result.add_node(result.target)
        result.target_status = target_status
        result.target_gaps = [result.add_gap(g) for g in target_gaps]
        if target is None or target_status == UNRESOLVED and target.kind == UNKNOWN:
            return result
        if target_status == AMBIGUOUS:
            return result
        visited: set[tuple] = set()
        visited_objects: set[str] = set()
        scopes_seen: set[str] = set()
        edges: dict[str, set[str]] = {}
        queue: deque = deque([(target, None, 0)])
        while queue:
            node, before, depth = queue.popleft()
            key = (node.id, before)
            if key in visited:
                continue
            visited.add(key)
            result.add_node(node)
            if depth > 0:
                reason = self.at_boundary(node, boundary)
                if reason:
                    result.stops.append(Stop(node, "boundary", reason))
                    continue
            if boundary.max_depth is not None and depth >= boundary.max_depth:
                result.stops.append(Stop(node, "max_depth", f"depth {depth}"))
                continue
            steps, gaps, stops, cycles = self.expand(node, before, visited_objects)
            for gap in gaps:
                result.add_gap(gap)
            for stop in stops:
                result.stops.append(stop)
            for cycle in cycles:
                if cycle not in result.cycles:
                    result.cycles.append(cycle)
            for step in steps:
                if any(s.id == step.id for s in result.steps):
                    continue
                result.steps.append(step)
                result.add_node(step.target)
                for evidence in step.evidence:
                    pass
                scope = getattr(step, "_scope", None)
                if scope:
                    scopes_seen.add(scope)
                position = getattr(step, "_position", None)
                for item in step.inputs:
                    result.add_node(item.node)
                    edges.setdefault(step.target.id, set()).add(item.node.id)
                    if item.node.kind == CONSTANT and item.role == VALUE:
                        result.stops.append(Stop(item.node, "constant"))
                    if item.node.kind in (COLUMN, TEMP_COLUMN, VARIABLE, PARAMETER):
                        queue.append((item.node, self.read_point(item.node, position), depth + 1))
                for row in step.row_selection:
                    result.add_node(row.node)
                    if self.trace_row_selection and row.node.kind in (COLUMN, TEMP_COLUMN, VARIABLE, PARAMETER):
                        queue.append((row.node, self.read_point(row.node, position), depth + 1))
        # Dynamic SQL with a computed target in any procedure this trace passed through.
        for scope in sorted(scopes_seen):
            for issue in self.catalog.dynamic_by_scope.get(scope, []):
                if issue.targets:
                    continue
                result.add_gap(Gap("dynamic_sql", "A procedure on this path executes dynamic SQL whose target is "
                                   "built at runtime; it may write columns on this path.", None,
                                   [self.catalog.evidence(issue.unit, issue.statement, role="dynamic_sql")]))
        result.cycles.extend(c for c in find_cycles(edges) if c not in result.cycles)
        result.stops = dedupe_stops(result.stops)
        return result

    @staticmethod
    def read_point(node: Node, position):
        """Temp tables and variables are read at a point in a scope; persistent objects are not."""
        if position is None:
            return None
        path, offset, scope = position
        if node.kind == TEMP_COLUMN and node.object is not None and node.object.scope == scope:
            return position
        if node.kind == VARIABLE and node.scope == scope:
            return position
        return None

    # -------------------------------------------------------------- expand
    def expand(self, node: Node, before, visited_objects: set[str]):
        memo_key = (node.id, before)
        if memo_key in self._memo:
            steps, gaps, stops, cycles, objects = self._memo[memo_key]
        else:
            if node.kind in (COLUMN, TEMP_COLUMN):
                steps, gaps, stops, cycles = self._expand_column(node, before)
            elif node.kind == VARIABLE:
                steps, gaps, stops, cycles = self._expand_variable(node, before)
            elif node.kind == PARAMETER:
                steps, gaps, stops, cycles = self._expand_parameter(node)
            else:
                steps, gaps, stops, cycles = [], [], [], []
            objects = None
            self._memo[memo_key] = (steps, gaps, stops, cycles, objects)
        extra: list[Gap] = []
        if node.object is not None and node.kind in (COLUMN, TEMP_COLUMN) and node.object.key not in visited_objects:
            visited_objects.add(node.object.key)
        return steps, gaps + extra, stops, cycles

    def _object_gaps(self, node: Node) -> list[Gap]:
        ref = node.object
        gaps = []
        for issue in self.catalog.issues_by_target.get(ref.name, []):
            wording = {"dynamic_sql": "Dynamic SQL in {where} names {obj} as a write target; what it writes is not analysed.",
                       "parse_error": "An unparsed statement in {where} appears to write {obj}.",
                       "insert_exec": "INSERT ... EXEC in {where} writes {obj} from a procedure result set; columns are not mapped.",
                       }.get(issue.kind, issue.message)
            where = issue.statement.procedure or issue.unit.rel_path
            gaps.append(Gap(issue.kind, wording.format(where=where, obj=ref.name), node,
                            [self.catalog.evidence(issue.unit, issue.statement, role=issue.kind)]))
        if ref.kind in ("table", "view") and ref.database:
            for writer in self.catalog.unknown_db_writers.get((ref.schema, ref.name), []):
                dbs = sorted(self.catalog.known_databases.get((ref.schema, ref.name), ()))
                if len(dbs) > 1:
                    gaps.append(Gap("ambiguous_object", f"A writer of {ref.schema}.{ref.name} has no database context; "
                                    f"it may write this object or the same name in {', '.join(d for d in dbs if d != ref.database)}.",
                                    node, [self.catalog.evidence(writer.unit, writer.statement, role="writer")],
                                    {"candidates": dbs}))
        if ref.server:
            gaps.append(Gap("external_object", f"{ref.display} is on linked server {ref.server}; it is not in the scanned files.", node))
        return gaps

    def _expand_column(self, node: Node, before):
        ref = node.object
        column = node.name
        gaps = self._object_gaps(node)
        steps = []
        stops: list[Stop] = []
        cycles: list[list[str]] = []
        writers = self.catalog.writers_for(ref)
        if before is not None and ref.kind == "temp_table":
            path, offset, scope = before
            writers = [w for w in writers if not (w.unit.path == path and w.statement.scope == scope and
                                                  w.statement.start > offset and
                                                  not any(c.startswith("WHILE") for c in w.controls))]
        writing = []
        for writer in sorted(writers, key=lambda w: (w.unit.path, w.statement.start)):
            found, found_gaps, found_cycles = self.analyzer.writer_steps(writer, column, node)
            gaps.extend(found_gaps)
            cycles.extend(found_cycles)
            for step in found:
                step._scope = writer.statement.scope
                step._position = (writer.unit.path, writer.statement.start, writer.statement.scope)
                if step.target.id == node.id:
                    writing.append((writer, step))
            for step in found:
                if step.target.kind == LOCAL_COLUMN:
                    step._scope = writer.statement.scope
                    step._position = (writer.unit.path, writer.statement.start, writer.statement.scope)
            steps.extend(found)
        # Overwrite order across procedures cannot be established statically.
        scopes = {w.statement.scope for w, _ in writing}
        if len(scopes) > 1 and any(w.kind in OVERWRITING for w, _ in writing):
            names = sorted({(w.statement.procedure or w.unit.rel_path) for w, _ in writing})
            gaps.append(Gap("ambiguous_writer_order",
                            f"{node.display} is written by {len(names)} procedures/scripts ({', '.join(names)}), at least "
                            "one of which overwrites existing rows; which value survives depends on run order.",
                            node, [self.catalog.evidence(w.unit, w.statement, role="writer") for w, _ in writing][:10]))
        elif len(writing) > 1:
            ordered = sorted(writing, key=lambda ws: ws[0].statement.start)
            for writer, step in ordered[1:]:
                if writer.kind in OVERWRITING:
                    step.notes.append("Runs after earlier writers in the same scope and overwrites the rows it matches.")
        if not writing:
            columns, schema_status, _ = self.catalog.schema_for(ref)
            declared = {c.name: c for c in columns} if columns else {}
            if not writers and not gaps:
                if ref.kind in ("temp_table", "table_variable"):
                    gaps.append(Gap("missing_definition", f"No writer of {ref.display} was found before this read.", node))
                elif schema_status == "missing" and not self.catalog.is_known(ref):
                    gaps.append(Gap("missing_definition", f"{ref.display} has no definition or writer in the scanned "
                                    "files.", node))
                else:
                    stops.append(Stop(node, "source_without_writer", "No statement in the scanned files writes this object."))
            elif not writers:
                pass
            elif column in declared and (declared[column].identity or declared[column].default or declared[column].computed):
                info = declared[column]
                what = "identity" if info.identity else ("computed" if info.computed else f"default {info.default}")
                stops.append(Stop(node, "source_without_writer", f"No writer sets this column; its value comes from its {what}."))
            elif not any(g.kind in ("positional_without_schema", "wildcard_without_schema", "column_count_mismatch",
                                    "parse_error", "insert_exec", "dynamic_sql") for g in gaps):
                stops.append(Stop(node, "source_without_writer",
                                  f"{len(writers)} statement(s) write this object but none sets this column."))
        return steps, gaps, stops, cycles

    def _expand_variable(self, node: Node, before):
        assignments = self.catalog.assignments.get((node.scope, node.name), [])
        if before is not None:
            path, offset, scope = before
            prior = [a for a in assignments if not (a.unit.path == path and a.statement.start >= offset)]
            in_loop = [a for a in assignments if a not in prior and any(c.startswith("WHILE") for c in
                                                                         a.unit.controls.get(a.statement.start, ()))]
            assignments = prior + in_loop
        steps, gaps = [], []
        for assignment in assignments:
            found, found_gaps = self.analyzer.assignment_steps(assignment, node)
            for step in found:
                step._scope = assignment.statement.scope
                step._position = (assignment.unit.path, assignment.statement.start, assignment.statement.scope)
            steps.extend(found)
            gaps.extend(found_gaps)
        if not assignments:
            gaps.append(Gap("unresolved_variable", f"No assignment of {node.name} was found before it is read "
                            "(cursor FETCH, OUTPUT parameter or dynamic SQL may set it).", node))
        elif len([a for a in assignments if a.kind != "exec_output"]) > 1:
            for step in steps:
                step.notes.append(f"{node.name} is assigned {len(assignments)} times before this read; each assignment "
                                  "is shown as an alternative.")
        return steps, gaps, [], []

    def _expand_parameter(self, node: Node):
        proc = self.catalog.proc_by_scope.get(node.scope)
        if proc is None:
            return [], [Gap("unresolved_variable", f"{node.name} is not a parameter of a known procedure.", node)], [], []
        try:
            position = proc.params.index(node.name)
        except ValueError:
            position = None
        steps, gaps, stops = [], [], []
        calls = list(self.catalog.calls_by_callee.get(proc.key, []))
        if not calls and proc.ref.database:
            unknown = ".".join(p for p in ("?", proc.ref.schema or "?", proc.ref.name))
            calls = list(self.catalog.calls_by_callee.get(unknown, []))
        bound = 0
        for call in calls:
            value = None
            for name, arg in call.args:
                if name == node.name:
                    value = arg
            if value is None and position is not None and position < len(call.args) and call.args[position][0] is None:
                value = call.args[position][1]
            if value is None:
                continue
            found, found_gaps = self.analyzer.argument_step(call, value, node)
            for step in found:
                step._scope = call.statement.scope
                step._position = (call.unit.path, call.statement.start, call.statement.scope)
            steps.extend(found)
            gaps.extend(found_gaps)
            bound += 1
        if not bound:
            stops.append(Stop(node, "parameter_without_caller",
                              "No EXEC in the scanned files passes this parameter (it may be set by a job, an SSIS "
                              "package or an application, or left to its default)."))
        return steps, gaps, stops, []


def dedupe_stops(stops: list[Stop]) -> list[Stop]:
    seen = {}
    for stop in stops:
        seen.setdefault((stop.node.id, stop.reason), stop)
    return list(seen.values())


def find_cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components with more than one node, or a self-loop (Tarjan, iterative)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0
    nodes = sorted(set(edges) | {v for vs in edges.values() for v in vs})
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(edges.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    item = stack.pop()
                    on_stack.discard(item)
                    component.append(item)
                    if item == node:
                        break
                if len(component) > 1 or node in edges.get(node, ()):
                    result.append(sorted(component))
    return sorted(result)
