"""Backward traversal from a target column to its contributors."""
from __future__ import annotations

from collections import deque
import fnmatch
import re

from .analyzer import Analyzer
from table_lineage.index import mask_noncode

from .catalog import Catalog, norm, split_name
from .model import (
    AMBIGUOUS, ASSERTED, COLUMN, CONSTANT, FILTER, INFERRED, LOCAL_COLUMN, PARAMETER, TEMP_COLUMN, UNKNOWN, UNRESOLVED,
    VALUE, VARIABLE, VERIFIED, WINDOW, CONDITION, Boundary, Evidence, Gap, Input, Node, ObjectRef, RowSelection, Step,
    Stop, TraceResult, worst,
)

OVERWRITING = ("update", "merge")


class Tracer:
    def __init__(self, catalog: Catalog, analyzer: Analyzer | None = None, trace_row_selection: bool = False,
                 assertions: list | None = None):
        self.catalog = catalog
        self.analyzer = analyzer or Analyzer(catalog)
        self.trace_row_selection = trace_row_selection
        self.analyzer.expand_row_selection = trace_row_selection
        self.assertions = list(assertions or [])
        self._asserted: dict[str, list] | None = None
        self.assertion_errors: list[str] = []
        self.assertions_seen: set[str] = set()
        self.assertion_effects: dict[str, list[dict]] = {}   # assertion id -> gaps it resolved
        self._memo: dict[tuple, tuple] = {}   # shared between traces in one run
        catalog.evaluate_dynamic()

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
        if target is None or target_status in (UNRESOLVED, AMBIGUOUS):
            return result
        visited: set[tuple] = set()
        step_ids: set[str] = set()
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
            steps, gaps, stops, cycles = self.expand(node, before)
            asserted = self.asserted_for(node)
            if asserted:
                steps, gaps, stops = self.apply_assertions(node, asserted, steps, gaps, stops, result)
            for gap in gaps:
                result.add_gap(gap)
            for stop in stops:
                result.stops.append(stop)
            for cycle in cycles:
                if cycle not in result.cycles:
                    result.cycles.append(cycle)
            for step in steps:
                if step.id in step_ids:
                    continue
                step_ids.add(step.id)
                result.steps.append(step)
                result.add_node(step.target)
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
        used = [a for a in self.assertions if a.id in result.assertions_used and a.writer_path]
        for scope in sorted(scopes_seen):
            for issue in self.catalog.dynamic_by_scope.get(scope, []):
                if issue.targets or self.catalog.dead_dynamic(issue) or self.catalog.dynamic_resolved(issue):
                    continue
                gap = Gap("dynamic_sql", "A procedure on this path executes dynamic SQL whose target is "
                          "built at runtime; it may write columns on this path.", None,
                          [self.catalog.evidence(issue.unit, issue.statement, role="dynamic_sql")],
                          self.dynamic_detail(issue))
                explained = next((a for a in used if gap.kind in a.resolves and a.explains(gap.evidence)), None)
                if explained is not None:
                    result.resolved_gaps.append({**gap.to_dict(), "affects": "context", "resolved_by": explained.id})
                    self._record_effect(explained.id, gap, result.requested)
                    continue
                result.add_gap(gap)
        result.cycles.extend(c for c in find_cycles(edges) if c not in result.cycles)
        result.stops = dedupe_stops(result.stops)
        self._explain_ends(result)
        return result

    @staticmethod
    def _explain_ends(result: TraceResult) -> None:
        """Every end of a value path must carry a stop or a gap; record any that does not."""
        nodes, steps = result.value_path()
        targets = {s.target.id for s in result.steps}
        stopped = {s.node.id for s in result.stops}
        gapped = {g.node.id for g in result.gaps.values() if g.node is not None}
        gapped |= {i.node.id for s in steps for i in s.inputs if i.gap}
        cyclic = {n for c in result.cycles for n in c}
        for node_id in sorted(nodes - targets - stopped - gapped - cyclic):
            node = result.nodes.get(node_id)
            if node is None:
                continue
            if node_id == result.target.id and not result.steps:
                continue
            result.add_gap(Gap("unexplained_end", "The trace ends here without a recorded reason; treat paths "
                               "through this node as unproven.", node))

    # ---------------------------------------------------------- assertions
    def _spec_node(self, spec, require_known: bool = False) -> Node | None:
        column = spec.column
        if column.lower().startswith("constant:"):
            return Node(CONSTANT, column.split(":", 1)[1].strip())
        parts = split_name(column)
        if parts and parts[0].startswith("#"):
            if len(parts) != 2:
                return None
            name, col = norm(parts[0]), norm(parts[1])
            want = norm(spec.procedure or "")
            # A #temp table filled only by dynamic SQL has no static writer, so look at creators too.
            candidates = {w.ref.scope for ws in self.catalog.writers.values() for w in ws
                          if w.ref.kind == "temp_table" and w.ref.name == name}
            candidates |= {scope for scope, names in self.catalog.temp_creators.items() if name in names}
            scopes = sorted(s for s in candidates
                            if not want or (s or "").split("::")[-1].split("@")[0] == want)
            if len(scopes) != 1:
                return None
            return Node(TEMP_COLUMN, col, ObjectRef(name, kind="temp_table", scope=scopes[0]))
        node, status, _ = self.target_node(column)
        if node is None or node.kind == UNKNOWN or status == AMBIGUOUS or (require_known and status == UNRESOLVED):
            return None
        return node

    def asserted_for(self, node: Node) -> list:
        if not self.assertions:
            return []
        if self._asserted is None:
            self._asserted = {}
            for assertion in self.assertions:
                target = self._spec_node(assertion.target, require_known=True)
                if target is None:
                    self.assertion_errors.append(f"{assertion.id}: target {assertion.target.column!r} could not be "
                                                 "resolved to one column in the scanned files")
                    continue
                self._asserted.setdefault(target.id, []).append(assertion)
        return self._asserted.get(node.id, [])

    def _record_effect(self, assertion_id: str, gap: Gap, trace: str) -> None:
        ev = gap.evidence[0] if gap.evidence else None
        entry = {"trace": trace, "kind": gap.kind, "node": gap.node.id if gap.node else None,
                 "location": ev.location if ev else None}
        effects = self.assertion_effects.setdefault(assertion_id, [])
        if entry not in effects:
            effects.append(entry)

    def apply_assertions(self, node: Node, assertions: list, steps, gaps, stops, result: TraceResult):
        """Add asserted steps for ``node`` and move the gaps they explain to resolved_gaps."""
        steps, gaps = list(steps), list(gaps)
        for assertion in assertions:
            self.assertions_seen.add(assertion.id)
            result.assertions_used.append(assertion.id)
            evidence = Evidence(assertion.source, assertion.line, assertion.line,
                                (assertion.reason or "asserted mapping")[:240], "assertion", None, None)
            for i, alt in enumerate(assertion.alternatives, 1):
                inputs = []
                for spec in alt.inputs:
                    source = self._spec_node(spec)
                    if source is None:
                        self.assertion_errors.append(f"{assertion.id}: input {spec.column!r} could not be resolved")
                        source = Node(UNKNOWN, spec.column)
                    inputs.append(Input(source, VALUE, spec.column, alt.branch, ASSERTED))
                rows = []
                for spec in alt.row_selection:
                    source = self._spec_node(spec)
                    if source is not None:
                        rows.append(RowSelection(source, FILTER, f"asserted: {spec.column}", evidence, ASSERTED))
                label = f"asserted alternative {i} of {len(assertion.alternatives)}"
                step = Step(target=node, writer_kind="asserted", expression=alt.expression or
                            " / ".join(s.column for s in alt.inputs), operation=alt.operation or "asserted",
                            inputs=inputs, row_selection=rows,
                            branch_context=[label + (f": {alt.branch}" if alt.branch else "")],
                            mapping_method="assertion", status=ASSERTED, procedure=None, evidence=[evidence],
                            order=assertion.line * 100 + i)
                step.notes.append(f"Assertion {assertion.id}" + (f" by {assertion.asserted_by}" if assertion.asserted_by else "")
                                  + (f": {assertion.reason.rstrip('.')}" if assertion.reason else "") + ". Not proved from SQL.")
                steps.append(step)
            kept = []
            for gap in gaps:
                if gap.node is not None and gap.node.id == node.id and gap.kind in assertion.resolves \
                        and assertion.explains(gap.evidence):
                    result.resolved_gaps.append({**gap.to_dict(), "affects": "value", "resolved_by": assertion.id})
                    self._record_effect(assertion.id, gap, result.requested)
                else:
                    kept.append(gap)
            gaps = kept
        stops = [s for s in stops if s.reason not in ("source_without_writer",)]
        return steps, gaps, stops

    @staticmethod
    def read_point(node: Node, position):
        """Temp tables and variables are read at a point in a scope; persistent objects are not."""
        if position is None:
            return None
        path, offset, scope = position
        if node.kind == TEMP_COLUMN and node.object is not None and node.object.scope == scope:
            return position
        if node.kind in (VARIABLE, PARAMETER) and node.scope == scope:
            return position
        return None

    # -------------------------------------------------------------- expand
    def expand(self, node: Node, before):
        memo_key = (node.id, before)
        if memo_key not in self._memo:
            if node.kind in (COLUMN, TEMP_COLUMN):
                found = self._expand_column(node, before)
            elif node.kind == VARIABLE:
                found = self._expand_variable(node, before)
            elif node.kind == PARAMETER:
                found = self._expand_parameter(node, before)
            else:
                found = ([], [], [], [])
            self._memo[memo_key] = found
        return self._memo[memo_key]

    def _object_gaps(self, node: Node) -> list[Gap]:
        ref = node.object
        gaps = []
        for issue in self.catalog.issues_by_target.get(ref.name, []):
            if self.catalog.dead_dynamic(issue) or self.catalog.dynamic_resolved(issue):
                continue
            wording = {"dynamic_sql": "Dynamic SQL in {where} names {obj} as a write target; what it writes is not analysed.",
                       "parse_error": "An unparsed statement in {where} appears to write {obj}.",
                       "insert_exec": "INSERT ... EXEC in {where} writes {obj} from a procedure result set; columns are not mapped.",
                       }.get(issue.kind, issue.message)
            evaluation = self.catalog.dynamic_evaluation(issue)
            if evaluation is not None and evaluation.status == "open":
                wording = ("Dynamic SQL in {where} names {obj} as a write target; the texts known from the code are "
                           "analysed, but others may be executed (see the open pieces).")
            where = issue.statement.procedure or issue.unit.rel_path
            gaps.append(Gap(issue.kind, wording.format(where=where, obj=ref.name), node,
                            [self.catalog.evidence(issue.unit, issue.statement, role=issue.kind)],
                            self.dynamic_detail(issue)))
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

    def dynamic_detail(self, issue) -> dict:
        """Why a dynamic SQL statement could not be fully evaluated (empty for other issues)."""
        evaluation = self.catalog.dynamic_evaluation(issue)
        return evaluation.detail() if evaluation is not None else {}

    def _expand_column(self, node: Node, before):
        ref = node.object
        column = node.name
        gaps = self._object_gaps(node)
        steps = []
        stops: list[Stop] = []
        cycles: list[list[str]] = []
        writers = self.catalog.writers_for(ref)
        if before is not None and ref.kind == "temp_table":
            writers = [w for w in writers if self._temp_writer_reaches(w, ref, before)]
        columns_declared, _, schema = self.catalog.schema_for(ref)
        info = next((c for c in columns_declared or [] if c.name == column), None)
        if info is not None and info.computed:
            try:
                found, found_gaps = self.analyzer.computed_step(ref, info, schema, node)
            except Exception as exc:  # noqa: BLE001
                return [], gaps + [internal_gap(self.catalog, schema.unit, schema.statement, node, exc)], [], []
            return found, gaps + found_gaps, [], []
        writing = []
        for writer in sorted(writers, key=lambda w: (w.unit.path, w.statement.start)):
            try:
                found, found_gaps, found_cycles = self.analyzer.writer_steps(writer, column, node)
            except Exception as exc:  # noqa: BLE001 - one unanalysable writer must not stop the trace
                gaps.append(internal_gap(self.catalog, writer.unit, writer.statement, node, exc))
                continue
            gaps.extend(found_gaps)
            cycles.extend(found_cycles)
            mark_evaluated(found, writer.statement)
            for step in found:
                step._scope = writer.statement.scope
                step._position = (writer.unit.path, writer.statement.start, writer.statement.scope)
                if step.target.id == node.id:
                    writing.append((writer, step))
                    if ref.kind == "temp_table" and writer.statement.scope != ref.scope:
                        step.status = worst(step.status, INFERRED)
                        step.notes.append(f"Writes {ref.name}, created by caller {ref.scope_label}; SQL Server makes a "
                                          "caller's #temp table visible to the procedures it calls.")
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
        elif len({id(w.statement) for w, _ in writing}) > 1:
            first = min(w.statement.start for w, _ in writing)
            for writer, step in writing:
                if writer.statement.start > first and step.writer_kind in ("update", "merge_update"):
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

    def _temp_writer_reaches(self, writer, ref, before) -> bool:
        path, offset, scope = before
        resets = self.catalog.temp_resets.get((ref.scope, ref.name), [])
        if writer.unit.path == path and writer.statement.scope == scope:
            in_loop = any(c.startswith("WHILE") for c in writer.controls)
            if writer.statement.start > offset and not in_loop:
                return False
            # DROP TABLE or re-creation between this write and the read discards it.
            return not any(writer.statement.start < r < offset for r in resets)
        if writer.statement.scope != ref.scope and scope == ref.scope:
            calls = [c for c in self.catalog.calls_by_caller.get(ref.scope, [])
                     if any(p.scope == writer.statement.scope for p in self.catalog.procs_for(c.callee))]
            if calls:
                before_read = [c.statement.start for c in calls if c.statement.start < offset]
                if not before_read:
                    return False   # the callee only runs after this read
                return not any(max(before_read) < r < offset for r in resets)
        return True

    def _reaching(self, node: Node, before):
        """Assignments that can supply the value read at ``before``, and whether one of them
        definitely replaces any earlier value (so callers' arguments no longer reach)."""
        assignments = list(self.catalog.assignments.get((node.scope, node.name), []))
        if before is None:
            return assignments, False
        path, offset, scope = before
        unit = self.catalog.units_by_path.get(path)
        read_controls = clean_controls(unit.controls.get(offset, ()) if unit else ())
        prior = sorted([a for a in assignments if a.unit.path == path and a.statement.start < offset],
                       key=lambda a: a.statement.start)
        loops = {c for c in read_controls if c.startswith("WHILE")}
        later = [a for a in assignments if a not in prior and loops and
                 loops & set(a.unit.controls.get(a.statement.start, ()))]
        dominating = None
        for a in prior:
            controls = clean_controls(a.unit.controls.get(a.statement.start, ()))
            if read_controls[:len(controls)] == controls and not (set(controls) & loops - set(read_controls)):
                dominating = a
        if dominating is not None:
            prior = [a for a in prior if a.statement.start >= dominating.statement.start]
        return prior + later, dominating is not None

    def _unparsed_assignment_gaps(self, node: Node) -> list[Gap]:
        pattern = re.compile(re.escape(node.name) + r"(?![\w$#@])\s*[-+*/%&|^]?=(?!=)", re.I)
        gaps = []
        for statement in self.catalog.fallback_by_scope.get(node.scope, []):
            if pattern.search(mask_noncode(statement.text)):
                unit = self.catalog.units_by_path.get(statement.path)
                gaps.append(Gap("parse_error", f"An unparsed statement may assign {node.name}.", node,
                                [self.catalog.evidence(unit, statement, role="parse_error")] if unit else []))
        return gaps

    def _expand_variable(self, node: Node, before):
        assignments, _ = self._reaching(node, before)
        steps, gaps = [], self._unparsed_assignment_gaps(node)
        for assignment in assignments:
            try:
                found, found_gaps = self.analyzer.assignment_steps(assignment, node)
            except Exception as exc:  # noqa: BLE001
                gaps.append(internal_gap(self.catalog, assignment.unit, assignment.statement, node, exc))
                continue
            for step in found:
                step._scope = assignment.statement.scope
                step._position = (assignment.unit.path, assignment.statement.start, assignment.statement.scope)
            steps.extend(found)
            gaps.extend(found_gaps)
        if not assignments and not gaps:
            gaps.append(Gap("unresolved_variable", f"No assignment of {node.name} was found before it is read "
                            "(it is NULL unless dynamic SQL or an unanalysed statement sets it).", node))
        elif len([a for a in assignments if a.kind != "exec_output"]) > 1:
            for step in steps:
                step.notes.append(f"{node.name} is assigned {len(assignments)} times before this read; each assignment "
                                  "is shown as an alternative.")
        return steps, gaps, [], []

    def _expand_parameter(self, node: Node, before=None):
        proc = self.catalog.proc_by_scope.get(node.scope)
        if proc is None:
            return [], [Gap("unresolved_variable", f"{node.name} is not a parameter of a known procedure.", node)], [], []
        # Assignments in the body come first; an unconditional one hides the caller's argument.
        steps, gaps, _, _ = self._expand_variable(node, before) if self.catalog.assignments.get((node.scope, node.name)) \
            else ([], self._unparsed_assignment_gaps(node), [], [])
        gaps = [g for g in gaps if g.kind != "unresolved_variable"]
        _, replaced = self._reaching(node, before)
        stops: list[Stop] = []
        if replaced:
            return steps, gaps, stops, []
        try:
            position = proc.params.index(node.name)
        except ValueError:
            position = None
        calls = [(c, False) for c in self.catalog.calls_by_callee.get(proc.key, [])]
        if proc.ref.database:
            unknown = ".".join(p for p in ("?", proc.ref.schema or "?", proc.ref.name))
            calls += [(c, len(self.catalog.procs_for(c.callee)) > 1) for c in self.catalog.calls_by_callee.get(unknown, [])]
        bound = 0
        omitted = []
        for call, ambiguous in calls:
            value = None
            for name, arg in call.args:
                if name == node.name:
                    value = arg
            if value is None and position is not None and position < len(call.args) and call.args[position][0] is None:
                value = call.args[position][1]
            if value is None:
                omitted.append(call)
                continue
            try:
                found, found_gaps = self.analyzer.argument_step(call, value, node)
            except Exception as exc:  # noqa: BLE001
                gaps.append(internal_gap(self.catalog, call.unit, call.statement, node, exc))
                continue
            mark_evaluated(found, call.statement)
            for step in found:
                step._scope = call.statement.scope
                step._position = (call.unit.path, call.statement.start, call.statement.scope)
                if ambiguous:
                    candidates = sorted(p.ref.display for p in self.catalog.procs_for(call.callee))
                    gap = Gap("ambiguous_object", f"This EXEC has no database context and matches {len(candidates)} "
                              f"procedures ({', '.join(candidates)}); it may not call this one.", node,
                              [self.catalog.evidence(call.unit, call.statement, role="call")], {"candidates": candidates})
                    gaps.append(gap)
                    step.status = worst(step.status, AMBIGUOUS)
                    step.gaps = sorted(set(step.gaps) | {gap.id})
            steps.extend(found)
            gaps.extend(found_gaps)
            bound += 1
        default = proc.defaults.get(node.name)
        if default is not None and (omitted or not bound):
            try:
                found, found_gaps = self.analyzer.default_step(proc, default[0], default[1], node,
                                                               "used when a caller omits it" if omitted else
                                                               "used if the (unknown) caller omits it")
                steps.extend(found)
                gaps.extend(found_gaps)
            except Exception as exc:  # noqa: BLE001
                gaps.append(internal_gap(self.catalog, proc.unit, default[1], node, exc))
        if not bound:
            stops.append(Stop(node, "parameter_without_caller",
                              "No EXEC in the scanned files passes this parameter (it may be set by a job, an SSIS "
                              "package or an application, or left to its default)."))
        return steps, gaps, stops, []


def mark_evaluated(steps, statement) -> None:
    """Steps from SQL evaluated out of a dynamic string are inferred, never verified."""
    if not getattr(statement, "eval_key", None):
        return
    for step in steps:
        step.status = worst(step.status, INFERRED)
        where = (f"built at {statement.built_at} (executed at {statement.executed_at})"
                 if statement.built_at != statement.executed_at else f"built and executed at {statement.executed_at}")
        note = f"Evaluated from dynamic SQL {where}; the string was enumerated from literals in the code."
        if note not in step.notes:
            step.notes.append(note)


def clean_controls(controls) -> tuple[str, ...]:
    """Control labels that make execution conditional (TRY blocks run unconditionally)."""
    return tuple(c for c in controls if c != "TRY")


def internal_gap(catalog, unit, statement, node, exc) -> Gap:
    return Gap("unsupported_statement", f"The analyzer could not process this statement ({type(exc).__name__}: "
               f"{str(exc)[:120]}); lineage through it is unknown.", node,
               [catalog.evidence(unit, statement, role="statement")], {"error": type(exc).__name__})


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
