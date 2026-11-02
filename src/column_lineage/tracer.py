"""Backward traversal from a target column to its contributors."""
from __future__ import annotations

from collections import deque
import copy
import fnmatch
from pathlib import Path
import re

from .analyzer import Analyzer
from table_lineage.index import mask_noncode

from .catalog import Catalog, controls_of, norm, split_name, writer_search
from .model import (
    AMBIGUOUS, ASSERTED, COLUMN, CONSTANT, FILTER, INFERRED, LOCAL_COLUMN, PARAMETER, TEMP_COLUMN, UNKNOWN, UNRESOLVED,
    VALUE, VARIABLE, VERIFIED, WINDOW, CONDITION, Boundary, Evidence, Gap, Input, Node, ObjectRef, RowSelection, Step,
    Stop, TraceResult, worst, FILE_COLUMN, PACKAGE_COLUMN, PACKAGE_VARIABLE, PRIOR_STATE,
)

OVERWRITING = ("update", "merge")
JOB_REASON_TEXT = {
    "writer_not_run_by_any_job_script": "a writer is not run by any job step in the scripts",
    "writer_run_by_several_jobs": "a writer is run by more than one job",
    "writer_reached_by_several_steps_or_paths": "a writer is reached by several steps or call paths",
    "writers_in_different_jobs": "the writers run in different jobs, so their schedules decide",
    "writer_only_in_disabled_job": "a writer runs only in a job its script disables (it may still be run by hand)",
    "job_runs_procedures_not_in_scan": "the job runs procedures that are not in the scanned files",
    "writers_overlap_in_chained_jobs": "a job started with sp_start_job runs alongside the rest of the job that "
                                       "started it",
    "call_on_the_way_is_conditional": "a call or write between the job step and the writer is conditional",
    "entry_point_and_job_scripts_disagree": "the entry procedure's call sequence and the job scripts give different orders",
    "asserted_writer_order_unknown": "a statement an assertion explains runs in another procedure and may overwrite; "
                                     "its run order is not known",
    "step_subsystem_not_read": "the job has a CmdExec, PowerShell or other step whose effect is not read",
    "step_command_not_followed": "a job step's command writes data itself, branches, stops early or runs dynamic SQL",
    "asynchronous_package_execution": "a job step starts a package without waiting for it (SSISDB, not SYNCHRONIZED)",
    "job_start_not_followed": "a job step starts a job at a named step, or a job whose name is not read",
    "job_step_loop": "the job's steps loop",
    "job_has_failure_branches": "the job continues to other steps when a step fails",
    "job_start_cycle": "jobs start each other in a cycle",
    "job_defined_more_than_once": "the job is defined in more than one script",
    "job_deleted_in_scripts": "a script deletes the job",
    "job_step_deleted_in_scripts": "a script deletes a job step (later steps are renumbered)",
    "job_step_update_not_followed": "a script updates a job step that is not found",
    "job_arguments_not_read": "a job script argument could not be read",
    "schedule_updated_in_scripts": "a script updates a schedule",
    "package_name_matches_several_copies": "the package name matches several package copies",
    "package_project_not_matched": "the job runs a package of another project",
    "ssis_step_package_not_read": "an SSIS job step names no package that could be read",
    "same_job_step_order_not_settled": "within one step, a call or write on the way is conditional",
}
UPDATING_STEPS = ("update", "merge_update")      # steps that change rows that already exist
ROW_CREATING_WRITERS = ("insert", "select_into", "view", "insert_exec")
MERGE_INSERTS = re.compile(r"\bWHEN\s+NOT\s+MATCHED\b(?!\s+BY\s+SOURCE\b)", re.I)
# Open points that may themselves add rows to an object.
GAPS_THAT_MAY_ADD_ROWS = ("insert_exec", "dynamic_sql", "parse_error", "unsupported_statement",
                          "unsupported_package_step")
# Nodes whose earlier state a loop may read back.
STATEFUL_KINDS = (COLUMN, TEMP_COLUMN, VARIABLE, PARAMETER, PACKAGE_VARIABLE)
TRACED_KINDS = (COLUMN, TEMP_COLUMN, VARIABLE, PARAMETER, PACKAGE_COLUMN, FILE_COLUMN, PACKAGE_VARIABLE)


class Tracer:
    def __init__(self, catalog: Catalog, analyzer: Analyzer | None = None, trace_row_selection: bool = False,
                 assertions: list | None = None, packages=None):
        self.catalog = catalog
        self.packages = packages   # optional ssis.PackageCatalog: package destinations as writers
        self.terminals: list[tuple[str, str]] = []   # (glob, reason) from --declared-terminals
        self.entry_points: list[str] = []            # procedure scopes the run starts from (--entry-point)
        self.jobs: list = []                         # SQL Agent jobs read from --job-scripts
        self._keys: dict = {}
        self.analyzer = analyzer or Analyzer(catalog)
        self.trace_row_selection = trace_row_selection
        self.analyzer.expand_row_selection = trace_row_selection
        self.assertions = list(assertions or [])
        self._asserted: dict[str, list] | None = None
        self.assertion_errors: list[str] = []
        self.assertions_seen: set[str] = set()
        self.assertion_effects: dict[str, list[dict]] = {}   # assertion id -> gaps it resolved
        self._memo: dict[tuple, tuple] = {}   # shared between traces in one run
        if packages is not None:
            packages.attach(self.analyzer)
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
        if not self.catalog.is_known(canonical) and not self.catalog.writers_for(canonical) \
                and not (self.packages is not None and self.packages.writes_object(canonical)):
            gaps.append(Gap("missing_definition", f"No definition or writer of {canonical.display} was found in the "
                            "scanned files.", node))
            return node, UNRESOLVED, gaps
        columns, status_schema, _ = self.catalog.schema_for(canonical)
        if status_schema == "declared" and column not in [c.name for c in columns]:
            gaps.append(Gap("missing_definition", f"{canonical.display} is declared without a column {column}.", node))
            status = UNRESOLVED
        return node, status, gaps

    # ------------------------------------------------------------ boundary
    def at_boundary(self, node: Node, boundary: Boundary) -> str | None:
        ref = node.object
        if ref is None or ref.kind not in ("table", "view"):
            return None
        if ref.server:
            return None   # an object on a linked server is not this environment's database of that name
        if ref.database and ref.database in {d.lower() for d in boundary.databases}:
            return f"database {ref.database}"
        names = [ref.key, f"{ref.schema}.{ref.name}", ref.name]
        for syn_key, (target, _, _) in self.catalog.synonyms.items():
            if self.catalog.canonical(target)[0].key == ref.key:
                names += [syn_key, ".".join(split_name(syn_key)[-2:]), split_name(syn_key)[-1]]
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
                gaps = self._assertion_order(node, asserted, steps, gaps)
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
                    if item.node.kind in TRACED_KINDS:
                        queue.append((item.node, self.read_point(item.node, position), depth + 1))
                for row in step.row_selection:
                    result.add_node(row.node)
                    if self.trace_row_selection and row.node.kind in TRACED_KINDS:
                        queue.append((row.node, self.read_point(row.node, position), depth + 1))
        # Dynamic SQL with a computed target in any procedure this trace passed through.
        used = [a for a in self.assertions if a.id in result.assertions_used and a.writer_path]
        for scope in sorted(scopes_seen):
            for issue in self.catalog.dynamic_by_scope.get(scope, []):
                if self.catalog.dead_dynamic(issue) or self.catalog.dynamic_resolved(issue):
                    continue
                evaluation = self.catalog.dynamic_evaluation(issue)
                if issue.targets and evaluation is None:
                    continue  # targets named in literals and nothing suggests a runtime-built target
                cited = {ev.statement_id for g in result.gaps.values() for ev in g.evidence}
                cited |= {ev.get("statement_id") for g in result.resolved_gaps for ev in g.get("evidence", [])}
                if self.catalog.evidence(issue.unit, issue.statement).statement_id in cited:
                    continue  # this statement already has a gap on the path
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
        self._earlier_states(result)
        edges = {}
        for step in result.steps:
            for item in step.inputs:
                edges.setdefault(step.target.id, set()).add(item.node.id)
        result.cycles.extend(c for c in find_cycles(edges) if c not in result.cycles)
        result.stops = dedupe_stops(result.stops)
        self._explain_ends(result)
        for stop in result.stops:
            if stop.reason in ("source_without_writer", "external_file", "boundary"):
                stop.declared_terminal = self._declared(stop.node)
        return result

    def _declared(self, node: Node) -> str | None:
        """A declared terminal may be named by any synonym of the object as well as by its own name."""
        reason = declared_terminal(self.terminals, node)
        if reason or node.object is None or not self.terminals:
            return reason
        for syn_key, (target, _, _) in self.catalog.synonyms.items():
            if self.catalog.canonical(target)[0].key != node.object.key:
                continue
            column, obj = f"{syn_key}.{node.name}".lower(), syn_key.lower()
            for pattern, why in self.terminals:
                if fnmatch.fnmatchcase(column, pattern) or fnmatch.fnmatchcase(obj, pattern):
                    return why
        return None

    # ------------------------------------------------------ earlier states
    def _creates_rows(self, ref: ObjectRef) -> bool:
        """Whether any statement in the selected sources adds rows to ``ref`` (not only changes them)."""
        for writer in self.catalog.writers_for(ref):
            if writer.kind in ROW_CREATING_WRITERS:
                return True
            if writer.kind == "merge" and MERGE_INSERTS.search(mask_noncode(writer.statement.text)):
                return True
        # Package data-flow destinations add rows (OLE DB commands only change them and are not listed).
        return self.packages is not None and ref.kind in ("table", "view") and self.packages.writes_object(ref)

    def _issue_for_gap(self, gap: dict):
        ids = {ev.get("statement_id") for ev in gap.get("evidence") or []}
        for issue in self.catalog.issues:
            if self.catalog.evidence(issue.unit, issue.statement).statement_id in ids:
                return issue
        return None

    def _scope_literals(self, unit, scope: str) -> str:
        """String-literal content of every statement in ``scope`` (dynamic SQL is built in literals)."""
        parts = []
        for other in unit.index.statements:
            if other.scope != scope:
                continue
            masked = mask_noncode(other.text)
            parts.append("".join(c if m == " " and c != " " else " " for c, m in zip(other.text, masked)))
        return " ".join(parts).replace("'", " ")

    def _hidden_statement_may_add_rows(self, gap: dict, ref: ObjectRef) -> bool:
        """Whether the statement behind a resolved gap may insert rows into ``ref``: INSERT ... EXEC and
        unparsed statements may; dynamic SQL may unless the literals that build it only UPDATE/DELETE."""
        issue = self._issue_for_gap(gap)
        if issue is None or issue.kind != "dynamic_sql":
            return True
        literals = self._scope_literals(issue.unit, issue.statement.scope)
        name = re.escape(ref.name)
        if re.search(rf"\b(?:INSERT\s+(?:INTO\s+)?|MERGE\s+(?:INTO\s+)?|INTO\s+)(?:\[?[\w$#]+\]?\s*\.\s*)*\[?{name}\]?(?![\w$#])",
                     literals, re.I):
            return True
        if re.search(r"\b(?:INSERT|MERGE|SELECT\s+[^;]*\bINTO)\b", literals, re.I) and not re.search(
                rf"\b(?:UPDATE|DELETE)\b[^;]*\b{name}\b", literals, re.I):
            return True   # some insert whose target is not legible: do not claim no rows
        return not re.search(r"\b(?:UPDATE|DELETE)\b", literals, re.I)

    def _hidden_statement_may_overwrite(self, assertion) -> bool:
        """Whether the dynamic statement an assertion explains may change existing rows (UPDATE/MERGE)."""
        scope = self._assertion_scope(assertion)
        if scope is None:
            return True
        unit, statement = scope
        literals = self._scope_literals(unit, statement.scope)
        return bool(re.search(r"\b(?:UPDATE|MERGE)\b", literals, re.I)) or not literals.strip()

    def _assertion_scope(self, assertion):
        """(unit, statement) the assertion's writer location names, or None."""
        if not assertion.writer_path:
            return None
        want = assertion.writer_path.replace("\\", "/").lower().lstrip("./")
        for unit in self.catalog.units:
            rel = unit.rel_path.replace("\\", "/").lower()
            if rel == want or rel.endswith("/" + want) or want.endswith("/" + rel):
                if assertion.writer_line is None:
                    return None
                for statement in unit.index.statements:
                    if statement.line <= assertion.writer_line <= statement.line + statement.text.count("\n"):
                        return unit, statement
        return None

    def _assertion_order(self, node: Node, assertions: list, steps, gaps) -> list:
        """D2: an asserted writer in another procedure than the SQL writers takes part in writer-order
        reasoning. Its run order is unknown, so when either side may overwrite, the order is a gap;
        with job scripts, it also gets a run context."""
        sql = [s for s in steps if s.target.id == node.id and s.writer_kind not in ("asserted", "retained")
               and getattr(s, "_scope", None)]
        asserted_steps = [s for s in steps if s.target.id == node.id and s.writer_kind == "asserted"]
        if not sql or not asserted_steps:
            return gaps
        if any(g.kind == "ambiguous_writer_order" and g.node is not None and g.node.id == node.id for g in gaps):
            return gaps
        sql_scopes = {s._scope for s in sql}
        conflict = False
        for assertion in assertions:
            located = self._assertion_scope(assertion)
            scope = located[1].scope if located else None
            if self.jobs and scope:
                shim = type("W", (), {})()
                shim.statement = type("S", (), {"scope": scope})()
                context = self._run_context(("sql", shim, None, None))
                for step in asserted_steps:
                    if step.run_context is None:
                        step.run_context = context
                        step.notes.append(self._run_context_note(context))
            other = scope is None or scope not in sql_scopes
            overwrite = any(s.writer_kind in UPDATING_STEPS for s in sql) or self._hidden_statement_may_overwrite(assertion)
            conflict = conflict or (other and overwrite)
        if conflict:
            names = sorted({s.procedure or "a script" for s in sql} | {f"asserted mapping {a.id}" for a in assertions})
            gaps.append(Gap("ambiguous_writer_order",
                            f"{node.display} is written by SQL and by a statement an assertion explains "
                            f"({', '.join(names)}); the asserted statement runs in another procedure and one side "
                            "may overwrite existing rows, so which value survives depends on run order.",
                            node, [e for s in sql + asserted_steps for e in s.evidence[:1]][:10],
                            {"reason": "asserted_writer_order_unknown"}))
        return gaps

    def _earlier_states(self, result: TraceResult) -> None:
        """Represent the value *before* a write wherever the trace reads it.

        Two shapes read a column's earlier content. (1) A column written only by UPDATE / MERGE-update
        steps: rows the update does not match keep the value they already had, and no traced statement
        adds the rows. (2) A loop: a write reads, directly or through other objects, the column it writes
        (preserve-and-restore of rows, aggregate-then-update of a working table, ``SET a = a * 2``,
        ``SET @s = @s + ...``). Each loop is cut where it reads back into a node already on the walk: that
        read now points to an "earlier content of" node with an explicit stop, so the dependency stays on
        the page and the walk ends with a stated reason instead of an unexplained cycle."""
        # Expansion results are memoized across the targets of one run: never change a cached step,
        # work on this trace's own copies.
        result.steps = [_own_copy(step) for step in result.steps]
        by_target: dict[str, list[Step]] = {}
        for step in result.steps:
            by_target.setdefault(step.target.id, []).append(step)
        open_objects = {g.node.object.key for g in result.gaps.values()
                        if g.kind in GAPS_THAT_MAY_ADD_ROWS and g.node is not None and g.node.object is not None}
        # A gap an assertion explained still hides a statement: it may add rows unless its visible text
        # (the string literals building the dynamic SQL) only updates or deletes.
        for g in result.resolved_gaps:
            node_id = g.get("node")
            node = result.nodes.get(node_id) if node_id else None
            if g.get("kind") in GAPS_THAT_MAY_ADD_ROWS and node is not None and node.object is not None \
                    and self._hidden_statement_may_add_rows(g, node.object):
                open_objects.add(node.object.key)
        creates: dict[str, bool] = {}

        def adds_rows(node: Node) -> bool:
            if node.kind not in (COLUMN, TEMP_COLUMN) or node.object is None:
                return True   # a variable starts as NULL or its declared value; other writes are traced
            if node.id not in creates:
                # An asserted mapping says what a value is, not that rows are created.
                creates[node.id] = (any(s.writer_kind not in UPDATING_STEPS + ("retained", "asserted")
                                        for s in by_target.get(node.id, []))
                                    or node.object.key in open_objects or self._creates_rows(node.object))
            return creates[node.id]

        priors: dict[str, dict] = {}   # prior node id -> {"node", "later", "reads"}

        def prior_of(node: Node) -> Node:
            prior = Node(PRIOR_STATE, node.name, node.object, node.scope, of=node.kind)
            priors.setdefault(prior.id, {"node": prior, "later": node, "reads": []})
            result.add_node(prior)
            return prior

        # (1) Columns that only UPDATE / MERGE-update steps write.
        for node_id in sorted(by_target):
            steps = by_target[node_id]
            node = steps[0].target
            if node.kind not in (COLUMN, TEMP_COLUMN) or any(s.writer_kind not in UPDATING_STEPS + ("asserted",)
                                                              for s in steps):
                continue   # a row-creating writer exists (an asserted mapping is not one)
            if adds_rows(node):
                continue
            table = node.object.display
            filtered = [s for s in steps if s.row_selection]
            if not filtered:
                for step in steps:
                    step.notes.append(f"No statement in the selected sources adds rows to {table}; this update "
                                      "sets every row that exists, so which rows exist is not traced here.")
                continue
            prior = prior_of(node)
            retained = Step(target=node, writer_kind="retained", expression=f"earlier content of {node.name}",
                            operation="direct",
                            inputs=[Input(prior, VALUE, f"earlier content of {node.name}",
                                          note="rows the update does not match")],
                            mapping_method="rows_not_updated", status=INFERRED, procedure=filtered[0].procedure,
                            evidence=[s.evidence[0] for s in filtered if s.evidence][:5],
                            notes=[f"Rows the {'UPDATE' if len(filtered) == 1 else 'UPDATEs'} above do not match keep "
                                   f"the value they already had, and no statement in the selected sources adds rows "
                                   f"to {table}: that earlier content is a value source whose origin is not traced."])
            result.steps.append(retained)
            steps.append(retained)
            priors[prior.id]["reads"].append({"step": retained.id, "target": node.id,
                                              "why": "rows_not_matched_by_update"})

        # (2) Loops. Within each group of nodes that feed each other, cut the reads into the table column or
        # variable nearest the target (all reads into it from inside the group), and repeat until no such
        # group remains. A cut always lands on a stateful node, never on a CTE or derived-table column,
        # so a multi-column UPDATE whose derived expression reads several of the columns it sets ends
        # with every right-hand-side read on the earlier content of that column.
        edges: dict[str, set[str]] = {}
        for step in result.steps:
            for item in step.inputs:
                edges.setdefault(step.target.id, set()).add(item.node.id)
        depth = {result.target.id: 0}
        frontier = deque([result.target.id])
        while frontier:
            current = frontier.popleft()
            for child in sorted(edges.get(current, ())):
                if child not in depth:
                    depth[child] = depth[current] + 1
                    frontier.append(child)
        cut: set[tuple[str, str]] = set()
        loops: dict[str, list[str]] = {}
        while True:
            remaining = {u: {v for v in vs if (u, v) not in cut} for u, vs in edges.items()}
            progressed = False
            for component in find_cycles(remaining):
                heads = sorted((n for n in component if result.nodes.get(n) is not None
                                and result.nodes[n].kind in STATEFUL_KINDS),
                               key=lambda n: (depth.get(n, 10 ** 9), n))
                if not heads:
                    continue   # e.g. a recursive CTE: reported as a cycle
                head = heads[0]
                for reader in component:
                    if head in remaining.get(reader, ()):
                        cut.add((reader, head))
                        progressed = True
                loops.setdefault(head, sorted(component))
            if not progressed:
                break
        for reader, read in sorted(cut):
            prior = prior_of(result.nodes[read])
            for step in by_target.get(reader, []):
                for item in step.inputs:
                    if item.node.id == read:
                        item.node = prior
                        item.note = "; ".join(x for x in (item.note, "reads the value before this write") if x)
                        priors[prior.id]["reads"].append({"step": step.id, "target": reader,
                                                          "why": "reads_back_in_loop"})
            priors[prior.id]["loop"] = loops.get(read, [read])

        for entry in priors.values():
            prior, later = entry["node"], entry["later"]
            in_loop = set(entry.get("loop") or ()) | {prior.id}
            outside = [s for s in by_target.get(later.id, []) if not any(i.node.id in in_loop for i in s.inputs)]
            loop = {"earlier_state_of": later.id, "read_by_steps": entry["reads"]}
            if entry.get("loop"):
                loop["loop_nodes"] = entry["loop"]
            if adds_rows(later):
                noun = "value" if later.kind not in (COLUMN, TEMP_COLUMN) else "content"
                which = ("the code does not prove which of them populated the rows before this run" if later.kind == COLUMN
                         else "the code does not prove which of them ran last before this read")
                detail = (f"The {noun} of {later.display} before this write. The writes of {later.display} that "
                          f"could have produced it are shown on this page as candidate earlier writers "
                          f"({_count_text(len(outside), 'step')} outside this read-back); {which}.")
                result.stops.append(Stop(prior, "prior_state", detail, loop=loop))
            else:
                detail = (f"No statement in the selected sources adds rows to {later.object.display}; the updates "
                          "change existing rows only, so the earlier content they keep or read is not traced.")
                result.stops.append(Stop(prior, "source_without_writer", detail, loop=loop))

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
            result.assertion_provenance[assertion.id] = assertion.provenance()
            evidence = Evidence(assertion.source, assertion.line, assertion.line,
                                (assertion.reason or "asserted mapping")[:240], "assertion", None, None)
            for i, alt in enumerate(assertion.alternatives, 1):
                inputs = []
                for spec in alt.inputs:
                    source = self._spec_node(spec)
                    if source is None:
                        self.assertion_errors.append(f"{assertion.id}: input {spec.column!r} could not be resolved")
                        source = Node(UNKNOWN, spec.column)
                    elif source.object is not None and source.object.kind in ("table", "view") \
                            and not self.catalog.is_known(source.object):
                        self.assertion_errors.append(f"{assertion.id}: input {spec.column!r} names an object with no "
                                                     "definition or writer in the scanned files (check the spelling)")
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
                            mapping_method="assertion", status=ASSERTED,
                            procedure=getattr(assertion.target, "procedure", None), evidence=[evidence],
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
            if self.packages is not None and self.packages.handles(node):
                found = self.packages.expand(node)
            elif node.kind in (COLUMN, TEMP_COLUMN):
                found = self._expand_column(node, before)
                if self.packages is not None and node.kind == COLUMN:
                    found = self.packages.merge(node, found)
                    if self.jobs:
                        found = self._package_job_context(node, found)
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
        issues = []
        for name in sorted(self.catalog.issue_names(ref)):
            issues.extend(i for i in self.catalog.issues_by_target.get(name, [])
                          if i not in issues and self.catalog.issue_reaches(i, ref))
        for call, issue in self.catalog.dynamic_literal_args().get(ref.name, []):
            gaps.append(Gap("dynamic_sql", f"{call.statement.procedure or call.unit.rel_path} passes {ref.name} to "
                            f"{issue.statement.procedure or 'a procedure'}, which builds and runs dynamic SQL; "
                            "it may write this object.", node,
                            [self.catalog.evidence(call.unit, call.statement, role="call"),
                             self.catalog.evidence(issue.unit, issue.statement, role="dynamic_sql")],
                            self.dynamic_detail(issue)))
        for issue in issues:
            if self.catalog.dead_dynamic(issue) or self.catalog.dynamic_resolved(issue):
                continue
            mapped = self.analyzer.result_set(issue) if issue.kind == "insert_exec" else None
            if mapped is not None and mapped[0] is not None:
                continue   # mapped from the callee's result set (writer kind insert_exec)
            wording = {"dynamic_sql": "Dynamic SQL in {where} names {obj} as a write target; what it writes is not analysed.",
                       "parse_error": "An unparsed statement in {where} appears to write {obj}.",
                       "insert_exec": "INSERT ... EXEC in {where} writes {obj} from a procedure result set; columns are not mapped.",
                       }.get(issue.kind, issue.message)
            evaluation = self.catalog.dynamic_evaluation(issue)
            if evaluation is not None and evaluation.status == "open":
                wording = ("Dynamic SQL in {where} names {obj} as a write target; the texts known from the code are "
                           "analysed, but others may be executed (see the open pieces).")
            where = issue.statement.procedure or issue.unit.rel_path
            detail = self.dynamic_detail(issue)
            if mapped is not None:
                detail = {**detail, "reason": mapped[1]}
            gaps.append(Gap(issue.kind, wording.format(where=where, obj=ref.name), node,
                            [self.catalog.evidence(issue.unit, issue.statement, role=issue.kind)], detail))
        if ref.kind in ("table", "view") and ref.database:
            for writer in self.catalog.unknown_db_writers.get((ref.schema, ref.name), []):
                dbs = sorted(self.catalog.known_databases.get((ref.schema, ref.name), ()))
                if len(dbs) > 1:
                    gaps.append(Gap("ambiguous_object", f"A writer of {ref.schema}.{ref.name} has no database context; "
                                    f"it may write this object or the same name in {', '.join(d for d in dbs if d != ref.database)}.",
                                    node, [self.catalog.evidence(writer.unit, writer.statement, role="writer")],
                                    {"candidates": dbs}))
        if ref.server:
            scanned = {db for dbs in self.catalog.known_databases.values() for db in dbs if db}
            scanned |= {norm(self.catalog.default_database)} if self.catalog.default_database else set()
            scanned |= {db for _, db in self.catalog.database_map}
            same_name = bool(ref.database) and norm(ref.database) in scanned
            gaps.append(Gap("external_object", f"{ref.display} is on linked server {ref.server}; it is not in the scanned "
                            "files." + (f" A database named {ref.database} is in the scan: if {ref.server} is this "
                                        "server, name it with --local-server." if same_name else ""), node, [],
                            {"reason": "linked_server_to_a_scanned_database_name" if same_name
                             else "linked_server_to_a_database_not_in_scan"}))
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
                # Steps mapped from another statement (a callee's result set) read where that statement runs.
                origin = getattr(step, "_origin", None)
                step._scope = origin[2] if origin else writer.statement.scope
                step._position = origin or (writer.unit.path, writer.statement.start, writer.statement.scope)
                if step.target.id == node.id:
                    step._writer = writer
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
        # Overwrite order across procedures is unknown, unless the call sequence from a declared entry
        # point (--entry-point) fixes it.
        scopes = {w.statement.scope for w, _ in writing}
        overwrites = len(scopes) > 1 and any(w.kind in OVERWRITING for w, _ in writing)
        ordered, why, reason, job_result = False, None, "no_job_scripts_or_entry_point", None
        items = [("sql", w, s, None) for w, s in writing]
        if self.jobs:
            for writer, step in writing:
                if getattr(step, "run_context", None) is None:
                    step.run_context = self._run_context(("sql", writer, step, None))
                    step.notes.append(self._run_context_note(step.run_context))
        if overwrites and self.entry_points:
            ordered, why = self._call_order(writing)
            reason = "entry_point_does_not_settle"
            if ordered and self.jobs:
                entry_keys = dict(self._keys)
                by_jobs, _, job_result = self._order_by_jobs(items)
                if by_jobs:
                    entry_order = sorted(range(len(items)), key=lambda m: [o for o, _ in entry_keys[id(items[m][1])]])
                    job_order = sorted(range(len(items)), key=lambda m: [o for o, _ in job_result[1][m]])
                    if entry_order != job_order:
                        ordered, why, reason = False, None, "entry_point_and_job_scripts_disagree"
                job_result = None
                self._keys = entry_keys
        if overwrites and not ordered and self.jobs and reason != "entry_point_and_job_scripts_disagree":
            ordered, job_why, job_result = self._order_by_jobs(items)
            reason = job_why or reason
        if overwrites and ordered and job_result is not None:
            self._note_job_order(items, job_result)
        elif overwrites and ordered:
            self._note_call_order(writing)
        elif overwrites:
            names = sorted({(w.statement.procedure or w.unit.rel_path) for w, _ in writing})
            gaps.append(Gap("ambiguous_writer_order",
                            f"{node.display} is written by {len(names)} procedures/scripts ({', '.join(names)}), at least "
                            "one of which overwrites existing rows; which value survives depends on run order."
                            + (f" The call sequence from the entry point does not settle it: {why}." if why else "")
                            + (f" The job scripts do not settle it ({JOB_REASON_TEXT.get(reason, reason)})."
                               if self.jobs else ""),
                            node, [self.catalog.evidence(w.unit, w.statement, role="writer") for w, _ in writing][:10],
                            {"reason": reason, **({"writers": len(items), "writers_in_job_scripts": sum(
                                1 for i in items if self._placements(i, success_only=False))} if self.jobs else {})}))
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
                    if ref.kind in ("table", "view") and self._declared(node):
                        # The reviewer accepts this object as a terminal for the run: an end, not a gap.
                        stops.append(Stop(node, "source_without_writer", "No definition or writer of this object "
                                          "in the scanned files; declared as a terminal for this run."))
                    else:
                        gaps.append(Gap("missing_definition", f"{ref.display} has no definition or writer in the "
                                        "scanned files.", node))
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
        for stop in stops:
            if stop.reason == "source_without_writer" and ref.kind in ("table", "view"):
                self._explain_no_writer(stop, ref, column, writers)
        return steps, gaps, stops, cycles

    # ------------------------------------------------------- call order
    def _call_paths(self, scope: str, limit: int = 3, entries: list[str] | None = None) -> list[tuple[str, list]]:
        """(entry scope, calls) for each call path from an entry point to ``scope`` (at most ``limit``)."""
        found: list[tuple[str, list]] = []
        for entry in (self.entry_points if entries is None else entries):
            stack = [(entry, [])]
            while stack and len(found) < limit:
                current, path = stack.pop()
                if current == scope:
                    found.append((entry, path))
                    continue
                if len(path) >= 12:
                    continue
                for call in self.catalog.calls_by_caller.get(current, []):
                    for proc in self.catalog.procs_for(call.callee):
                        if proc.scope not in {c.caller_scope for c in path} | {current}:
                            stack.append((proc.scope, path + [call]))
        return found

    # -------------------------------------------------------- job order
    def _resolve_step_procedure(self, name: str, database: str | None) -> tuple[str, str | None]:
        """(status, scope): status is resolved, system (msdb/master, an unresolved sp_/xp_; no lineage),
        other_database (no definition in the step's database, but one of that name elsewhere: counted,
        never used) or unresolved."""
        parts = [norm(p) for p in split_name(name)][-3:]
        parts = [None] * (3 - len(parts)) + parts
        db = parts[0] or norm(database)
        procs = self.catalog.procs_for(ObjectRef(parts[2], parts[1] or self.catalog.default_schema, db, None,
                                                 "procedure"))
        if len(procs) == 1:
            return "resolved", procs[0].scope
        if procs:
            return "several_definitions", None
        if (db or "") in ("msdb", "master") and (parts[2] or "").startswith(("sp_", "xp_", "sys")):
            return "system", None
        if (parts[2] or "").startswith(("sp_", "xp_")) and not parts[0]:
            return "system", None
        elsewhere = self.catalog.procs_for(ObjectRef(parts[2], parts[1] or self.catalog.default_schema, None, None,
                                                     "procedure"))
        return ("other_database" if elsewhere else "unresolved"), None

    def _step_scopes(self, step) -> list[tuple[str, str | None]]:
        out = []
        for _, kind, name, database, _ in step.actions():
            if kind != "proc":
                continue
            status, scope = self._resolve_step_procedure(name, database)
            if status != "system":
                out.append((name, scope))
        return out

    def _reach(self, scope: str) -> set[str]:
        """Procedure scopes ``scope`` runs, itself included (through EXEC calls)."""
        cache = self.__dict__.setdefault("_reach_cache", {})
        if scope not in cache:
            seen, frontier = {scope}, [scope]
            while frontier:
                current = frontier.pop()
                for call in self.catalog.calls_by_caller.get(current, []):
                    for proc in self.catalog.procs_for(call.callee):
                        if proc.scope not in seen:
                            seen.add(proc.scope)
                            frontier.append(proc.scope)
            cache[scope] = seen
        return cache[scope]

    def _package_copies(self, stem: str) -> list:
        if self.packages is None:
            return []
        return [p for p in self.packages.packages if p.path.stem.lower() == stem]

    def _package_entries(self, stem: str, step, seen=()) -> list[dict]:
        """The package a step runs and the child packages its Execute Package tasks run (same place)."""
        copies = self._package_copies(stem)
        problem = None
        if len(copies) > 1:
            problem = "package_name_matches_several_copies"
        project = step.package_project()
        if copies and project and not any(project.lower() in [part.lower() for part in Path(c.rel).parts[:-1]]
                                          or len(Path(c.rel).parts) == 1 for c in copies):
            problem = problem or "package_project_not_matched"
        out = [{"package": stem, "problem": problem, "via_parent": bool(seen)}]
        for copy in copies:
            for _, ref, _ in copy.data.child_packages:
                if ref:
                    child = re.split(r"[\\/]", ref.strip())[-1].lower()
                    child = child[:-5] if child.endswith(".dtsx") else child
                    if child and child not in seen and child != stem:
                        out.extend(self._package_entries(child, step, seen + (stem,)))
        return out

    def _job_index(self) -> list[dict]:
        """One entry per procedure or package a job step runs, with the step's place in the run: its
        path [(job, position)] from the scheduled job down through jobs started with sp_start_job. A job
        started by exactly one step and with no enabled schedule of its own is part of that chain. Steps
        on failure paths are indexed for run context only (on_success_path False)."""
        cached = self.__dict__.get("_jobs_index")
        if cached is not None:
            return cached
        by_name = {}
        for job in self.jobs:
            by_name.setdefault(job.name.lower(), []).append(job)
        starters: dict[str, list] = {}
        for job in self.jobs:
            for position, step in enumerate(job.sequence()):
                for name in step.started_jobs():
                    starters.setdefault(name.lower(), []).append((job, position))
        # A job that (transitively) starts itself never settles.
        for job in self.jobs:
            frontier, seen = [job], set()
            while frontier:
                current = frontier.pop()
                for step in current.steps:
                    for name in step.started_jobs():
                        for started in by_name.get(name.lower(), []):
                            if started is job and "job_start_cycle" not in job.problems:
                                job.problems.append("job_start_cycle")
                            if id(started) not in seen:
                                seen.add(id(started))
                                frontier.append(started)
        for job in self.jobs:
            for step in job.reachable():
                reason = step.opaque_reason()
                if reason and reason not in job.problems:
                    job.problems.append(reason)

        def path_of(job, seen=()):
            found = starters.get(job.name.lower(), [])
            if job.scheduled or len(found) != 1 or job.name.lower() in seen:
                return [], job
            parent, position = found[0]
            prefix, root = path_of(parent, seen + (job.name.lower(),))
            return prefix + [(parent.name, position)], root

        entries = []
        for job in self.jobs:
            prefix, root = path_of(job)
            success = job.sequence()
            positions = {id(st): i for i, st in enumerate(success)}
            for step in job.reachable():
                on_path = id(step) in positions
                position = positions.get(id(step), 10 ** 6 + step.step_id)
                base = {"job": job, "root": root, "path": prefix + [(job.name, position)], "step": step,
                        "on_success_path": on_path}
                sub = 0
                for _, kind, name, database, _ in step.actions():
                    if kind == "proc":
                        status, scope = self._resolve_step_procedure(name, database)
                        if status == "system":
                            continue
                        entries.append({**base, "sub": sub, "scope": scope, "package": None, "problem": None,
                                        "reach": self._reach(scope) if scope else set(), "resolution": status})
                        sub += 1
                    elif kind == "package":
                        for entry in self._package_entries(name, step):
                            entries.append({**base, "sub": sub, "scope": None, "reach": set(), **entry})
                        sub += 1
        self._jobs_index = entries
        return entries

    def job_diagnostics(self) -> dict:
        """Anonymous counts: how job-step procedures resolved and why jobs cannot order writers."""
        index = self._job_index()
        resolution = {}
        for job in self.jobs:
            for step in job.steps:
                for _, kind, name, database, _ in step.actions():
                    if kind == "proc":
                        status, _ = self._resolve_step_procedure(name, database)
                        resolution[status] = resolution.get(status, 0) + 1
        problems = {}
        for job in self.jobs:
            for problem in job.problems:
                problems[problem] = problems.get(problem, 0) + 1
        reached = set()
        for entry in index:
            reached |= entry["reach"]
        return {"step_procedures": dict(sorted(resolution.items())),
                "jobs_by_problem": dict(sorted(problems.items())),
                "procedures_reached_from_job_steps": len(reached),
                "packages_named_in_steps": len({e["package"] for e in index if e["package"]})}

    def _placements(self, item, success_only: bool = True) -> list[dict]:
        kind, writer, _, package = item
        entries = [e for e in self._job_index() if e["on_success_path"] or not success_only]
        if kind == "sql":
            return [e for e in entries if e["scope"] and writer.statement.scope in e["reach"]]
        return [e for e in entries if e["package"] == package]

    def _run_context(self, item) -> dict:
        """Which jobs in the scripts run this writer (context for the pages; not an order)."""
        found = self._placements(item, success_only=False)
        if not found:
            return {"status": "not_in_job_scripts"}
        enabled = [e for e in found if e["root"].enabled]
        jobs = []
        for e in (enabled or found)[:5]:
            via = [name for name, _ in e["path"][1:]] if len(e["path"]) > 1 else []
            jobs.append({"job": e["root"].name, **({"through": via} if via else {}), "step": e["step"].step_id,
                         "step_name": e["step"].name, "schedule": e["root"].schedule_text(),
                         **({"only_on_failure": True} if not e["on_success_path"] else {})})
        return {"status": "run_by_enabled_job" if enabled else "only_disabled_jobs", "jobs": jobs}

    @staticmethod
    def _run_context_note(context: dict) -> str:
        if context["status"] == "not_in_job_scripts":
            return ("No job in the job scripts runs this writer: it may be run by hand, by another scheduler or "
                    "caller, or be a copy that is not deployed.")
        jobs = []
        for j in context["jobs"]:
            text = f"job {j['job']}" + (f" (through {', '.join(j['through'])}, which it starts)" if j.get("through") else "")
            text += f", step {j['step']}" + (f" {j['step_name']}" if j["step_name"] else "")
            if j.get("only_on_failure"):
                text += " (only when an earlier step fails)"
            if j["schedule"]:
                text += f", scheduled {j['schedule']}"
            jobs.append(text)
        lead = "Run by " if context["status"] == "run_by_enabled_job" else "Run only by disabled "
        return lead + "; ".join(jobs) + " (from the job scripts: a static copy, not confirmed as the deployed jobs)."

    def _unconditional_from(self, entry: str, writer) -> bool:
        """Whether ``writer`` always runs once its job step's procedure ``entry`` runs: one call path, and
        no call or write on it inside IF / ELSE / WHILE / CATCH."""
        paths = self._call_paths(writer.statement.scope, entries=[entry])
        if len(paths) != 1:
            return False
        _, calls = paths[0]
        controls = [tuple(controls_of(c.unit, c.statement)) for c in calls] + [tuple(writer.controls)]
        return not any(x for c in controls for x in c if not x.startswith("TRY"))

    def _order_by_jobs(self, items):
        """(ordered, reason, (placements, keys)): whether the job scripts order every pair of writers.
        ``items`` are ("sql", writer, step, None) or ("package", None, step, package name). Writers in
        different scheduled jobs, run more than once, run conditionally, run after an asynchronous start
        alongside the started job, or run by a job that is not fully understood stay unordered."""
        chosen = []
        for item in items:
            found = self._placements(item)
            if not found:
                return False, "writer_not_run_by_any_job_script", None
            if len({id(e["root"]) for e in found}) > 1:
                return False, "writer_run_by_several_jobs", None
            if len(found) > 1 or (item[0] == "sql" and
                                  len(self._call_paths(item[1].statement.scope, entries=[found[0]["scope"]])) > 1):
                return False, "writer_reached_by_several_steps_or_paths", None
            if found[0].get("problem"):
                return False, found[0]["problem"], None
            if item[0] == "sql" and not self._unconditional_from(found[0]["scope"], item[1]):
                return False, "call_on_the_way_is_conditional", None
            chosen.append(found[0])
        if any(not e["root"].enabled for e in chosen):
            return False, "writer_only_in_disabled_job", None
        root = chosen[0]["root"]
        if any(e["root"] is not root for e in chosen):
            return False, "writers_in_different_jobs", None
        involved = {id(root)} | {id(e["job"]) for e in self._job_index() if e["root"] is root}
        for job in self.jobs:
            if id(job) in involved and job.problems:
                return False, job.problems[0], None
        if any(e["root"] is root and e["package"] is None and e["scope"] is None for e in self._job_index()):
            return False, "job_runs_procedures_not_in_scan", None
        for i in range(len(chosen)):
            for j in range(i + 1, len(chosen)):
                relation = _path_relation(chosen[i]["path"], chosen[j]["path"])
                if relation is None:
                    return False, "writers_overlap_in_chained_jobs", None
        keys: dict[int, list] = {}
        groups: dict[tuple, list[int]] = {}
        for index, e in enumerate(chosen):
            groups.setdefault((tuple(e["path"]), e["sub"]), []).append(index)
        saved = self.entry_points
        try:
            for (path, sub), members in groups.items():
                if len(members) > 1:
                    if any(items[m][0] != "sql" for m in members):
                        return False, "same_job_step_order_not_settled", None
                    self.entry_points = [chosen[members[0]]["scope"]]
                    ok, _ = self._call_order([(items[m][1], items[m][2]) for m in members])
                    if not ok:
                        return False, "same_job_step_order_not_settled", None
                    inner = {m: list(self._keys[id(items[m][1])]) for m in members}
                else:
                    m = members[0]
                    inner = {m: [(items[m][1].statement.start if items[m][0] == "sql" else 0, ())]}
                for m in members:
                    keys[m] = [(p, ()) for _, p in path] + [(sub, ())] + inner[m]
        finally:
            self.entry_points = saved
        return True, None, (chosen, keys)

    def _note_job_order(self, items, result) -> None:
        chosen, keys = result
        order = sorted(range(len(items)), key=lambda m: [o for o, _ in keys[m]])
        label = lambda m: (items[m][1].statement.procedure or items[m][1].unit.rel_path) if items[m][0] == "sql" \
            else f"package {items[m][3]}"
        names = [label(m) for m in order]
        for rank, m in enumerate(order):
            _, writer, step, _ = items[m]
            placed = chosen[m]
            earlier = sorted(set(names[:rank]) - {names[rank]})
            later = sorted(set(names[rank + 1:]) - {names[rank]})
            text = (f"From the step order of SQL Agent job {placed['root'].name} in its script (a static copy, not "
                    "confirmed as the deployed job), this write runs")
            if earlier:
                text += f" after {', '.join(earlier)}"
            if later:
                text += f"{' and' if earlier else ''} before {', '.join(later)}"
            if step.writer_kind in ("update", "merge_update") and earlier:
                text += "; it overwrites the rows it matches"
            step.notes.append(text + ".")
            step.status = worst(step.status, INFERRED)
            job_step = placed["step"]
            step.evidence.append(Evidence(job_step.path, job_step.line, job_step.line,
                                          f"job {placed['job'].name}, step {job_step.step_id} {job_step.name}".strip(),
                                          "job_step", None, "jobs"))

    def _package_job_context(self, node: Node, found):
        """Run context for package writers, and one job order over SQL and package writers when they
        conflict (the package adapter's writer-order gap)."""
        steps, gaps, stops, cycles = found
        items = []
        for step in steps:
            if step.target.id != node.id:
                continue
            if (step.procedure or "").startswith("package "):
                stem = next((Path(e.path).stem.lower() for e in step.evidence if e.path.lower().endswith(".dtsx")),
                            None)
                if stem is None:
                    continue
                item = ("package", None, step, stem)
                if getattr(step, "run_context", None) is None:
                    step.run_context = self._run_context(item)
                    step.notes.append(self._run_context_note(step.run_context))
                items.append(item)
            elif getattr(step, "_writer", None) is not None:
                items.append(("sql", step._writer, step, None))
        conflicts = [g for g in gaps if g.kind == "ambiguous_writer_order" and g.node is not None and g.node.id == node.id]
        if not conflicts or not any(i[0] == "package" for i in items):
            return found
        ok, why, result = self._order_by_jobs(items)
        if ok:
            self._note_job_order(items, result)
            return steps, [g for g in gaps if g not in conflicts], stops, cycles
        for gap in conflicts:
            gap.detail = {**gap.detail, "reason": why}
        return found

    def _call_key(self, writer) -> tuple[list, str | None]:
        """[(offset, controls)] from the entry point down to the writer, or (None, reason)."""
        paths = self._call_paths(writer.statement.scope)
        label = writer.statement.procedure or writer.unit.rel_path
        if not paths:
            return None, f"{label} is not called from the entry point"
        if len(paths) > 1:
            return None, f"{label} is reached by several call paths from the entry point"
        entry, calls = paths[0]
        key = [(c.statement.start, tuple(controls_of(c.unit, c.statement))) for c in calls]
        return key + [(writer.statement.start, tuple(writer.controls))], None

    def _call_order(self, writing) -> tuple[bool, str | None]:
        """Whether the call sequence from the entry point orders every pair of writers."""
        keys = []
        for writer, _ in writing:
            key, why = self._call_key(writer)
            if key is None:
                return False, why
            keys.append(key)
        conditional = lambda c: any(not x.startswith("TRY") for x in c)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i], keys[j]
                d = next((k for k in range(min(len(a), len(b))) if a[k][0] != b[k][0]), None)
                if d is None:
                    continue   # the same statement (two steps of one writer)
                # a[d] and b[d] are statements of the same procedure; their controls share the blocks
                # that enclose both. What lies beyond that, and everything inside the callees below,
                # must run unconditionally for the order to be certain.
                ca, cb = a[d][1], b[d][1]
                common = 0
                while common < min(len(ca), len(cb)) and ca[common] == cb[common]:
                    common += 1
                extra_a, extra_b = ca[common:], cb[common:]
                if extra_a and extra_b and (extra_b[0] == f"ELSE (of {extra_a[0]})"
                                            or extra_a[0] == f"ELSE (of {extra_b[0]})"):
                    continue   # the two arms of one IF never both run
                deeper = [c for _, c in a[d + 1:]] + [c for _, c in b[d + 1:]]
                if conditional(extra_a) or conditional(extra_b) or any(conditional(c) for c in deeper):
                    return False, "a call or write on the way is conditional (IF/ELSE/WHILE/CATCH)"
        self._keys = {id(w): k for (w, _), k in zip(writing, keys)}
        return True, None

    def _note_call_order(self, writing) -> None:
        order = sorted(writing, key=lambda ws: [o for o, _ in self._keys[id(ws[0])]])
        names = [w.statement.procedure or w.unit.rel_path for w, _ in order]
        for rank, (writer, step) in enumerate(order):
            earlier = sorted(set(names[:rank]) - {names[rank]})
            later = sorted(set(names[rank + 1:]) - {names[rank]})
            text = "From the entry point's call sequence, this write runs"
            if earlier:
                text += f" after {', '.join(earlier)}"
            if later:
                text += f"{' and' if earlier else ''} before {', '.join(later)}"
            if writer.kind in OVERWRITING and earlier:
                text += "; it overwrites the rows it matches"
            step.notes.append(text + ".")

    def _explain_no_writer(self, stop: Stop, ref, column: str, writers) -> None:
        """Say where the search for a writer looked, and list near misses, so a no-writer stop is
        never mistaken for proof that the object is an ultimate source."""
        found = writer_search(self.catalog, ref, column, writers)
        stop.writer_search = found
        scope = "the selected scanned SQL sources" + (" and packages" if getattr(self, "packages", None) else "")
        unreadable = self.catalog.stats.get("files_without_statements", 0)
        if unreadable:
            scope += (f" ({unreadable} non-empty file(s) yielded no statements and could not be searched; see "
                      "files_without_statements in the scan)")
        if not found:
            stop.detail = f"{stop.detail} Searched {scope}; no statement there writes an object of this name."
            return
        shown = "; ".join(f"{f['location']}: {f['detail']}" for f in found[:5])
        more = f"; and {len(found) - 5} more" if len(found) > 5 else ""
        stop.detail = (f"{stop.detail} Searched {scope}; {len(found)} statement(s) that look like writers were not "
                       f"used: {shown}{more}.")

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


def _own_copy(step: Step) -> Step:
    """A copy of a step whose inputs and notes can change without touching the memoized original."""
    clone = copy.copy(step)
    clone.inputs = [copy.copy(item) for item in step.inputs]
    clone.notes = list(step.notes)
    return clone


def _path_relation(a: list, b: list) -> int | None:
    """Order of two job-step paths [(job, position), ...]: -1 / 1, 0 for the same step, None when they may
    overlap (one runs in a job that the other's step started with sp_start_job, which does not wait, or
    they belong to different jobs)."""
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    if i == len(a) and i == len(b):
        return 0
    if i == min(len(a), len(b)):
        return None
    (job_a, pos_a), (job_b, pos_b) = a[i], b[i]
    if job_a != job_b:
        return None
    first = a if pos_a < pos_b else b
    if len(first) > i + 1:
        return None   # the earlier step started a job that keeps running while the later steps run
    return -1 if pos_a < pos_b else 1


def _count_text(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


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


def declared_terminal(terminals: list[tuple[str, str]], node: Node) -> str | None:
    """The reason a stop's node was declared an acceptable terminal for this run, if it was. A pattern
    matches the column id (db.schema.table.column, or a package file column's id) or its object."""
    if not terminals or node.kind in (CONSTANT, UNKNOWN):
        return None
    column = node.id.lower()
    obj = column.rsplit(".", 1)[0]
    for pattern, reason in terminals:
        if fnmatch.fnmatchcase(column, pattern) or fnmatch.fnmatchcase(obj, pattern):
            return reason
    return None
