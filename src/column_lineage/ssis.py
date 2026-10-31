"""Optional SSIS (.dtsx) adapter: package data flows as writers of database columns.

With ``--package-root`` the CLI builds a ``PackageCatalog`` next to the SQL catalog.
The tracer asks it, for every database column it reaches, which in-scope package
destinations map an input column to that column, and expands package-internal
nodes (data-flow columns, flat-file columns, package variables) into steps.

Edges come only from package metadata links: a destination input column's
``externalMetadataColumnId`` (the destination column), its ``lineageId`` (the
upstream column), path start/end ids, synchronous-output ids and the lineage ids
written in component properties (``SourceInputColumnLineageID``, ``#{...}`` / ``#n`` in
expressions, ``OutputColumnLineageID``, ``InputColumnID``, ``SortColumnId``,
``AggregationColumnId``, ``copyColumnId``) and Lookup reference column properties.
Package, task, component, table and column names never establish a mapping. See
docs/column-lineage-design.md ("SSIS packages").
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import fnmatch
from pathlib import Path
import re

import sqlglot
from sqlglot import exp

from table_lineage.index import SourceIndex, Statement, mask_noncode
from table_lineage.sources import SourceFile

from . import ssis_expression as sx
from .catalog import Catalog, Unit, Writer, collapse, norm, split_name
from .model import (
    AMBIGUOUS, CONDITION, CONSTANT, COLUMN, FILE_COLUMN, FILTER, GROUP_BY, INFERRED, JOIN_KEY, PACKAGE_COLUMN,
    PACKAGE_VARIABLE, PARTIAL, UNKNOWN, UNRESOLVED, VALUE, VARIABLE, VERIFIED, Evidence, Gap, Input, Node,
    ObjectRef, RowSelection, Step, Stop, stable_id, worst,
)
from .ssis_xml import Col, Component, Flow, PackageError, PackageFile, Port, bracket_name, normalize_ref, read_package

PACKAGE_GAP_KINDS = ("unsupported_package_step", "ambiguous_package_copy", "package_runtime_value",
                     "package_expression", "package_metadata", "package_column_not_mapped")
PACKAGE_STOP_REASONS = ("external_file", "package_parameter", "package_runtime")
PACKAGE_NODE_KINDS = (PACKAGE_COLUMN, FILE_COLUMN, PACKAGE_VARIABLE)

SOURCES = ("flat_file_source", "oledb_source", "adonet_source")
DESTINATIONS = ("oledb_destination", "adonet_destination", "sqlserver_destination")
# Synchronous components whose effect on rows passing through is understood.
PASS_THROUGH = ("data_conversion", "derived_column", "conditional_split", "lookup", "multicast", "row_count",
                "copy_column")
AGGREGATIONS = {"0": "GROUP BY", "1": "COUNT", "2": "COUNT(*)", "3": "COUNT DISTINCT", "4": "SUM", "5": "AVG",
                "6": "MIN", "7": "MAX"}
PLACEHOLDER = "@ssis_param_"


@dataclass
class Package:
    path: Path
    root: Path
    root_label: str
    rel: str
    data: PackageFile
    issues: list = field(default_factory=list)     # Gap objects (context gaps for traces through this package)

    @property
    def label(self) -> str:
        return f"{self.root_label}/{self.rel}"


@dataclass
class Destination:
    package: Package
    flow: Flow
    component: Component
    port: Port
    ref: ObjectRef
    status: str
    notes: list
    table_text: str
    mapping: dict          # destination column (lower) -> input Col


@dataclass
class Resolved:
    node: Node | None
    rows: list
    branches: list
    gaps: list
    status: str = VERIFIED


def matches(rel: str, patterns) -> bool:
    rel = rel.lower()
    return any(fnmatch.fnmatchcase(rel, p.replace("\\", "/").lower()) for p in patterns)


class PackageCatalog:
    def __init__(self, catalog: Catalog, connection_map: dict[str, str] | None = None):
        self.catalog = catalog
        self.connection_map = {k.lower(): norm(v) for k, v in (connection_map or {}).items()}
        self.analyzer = None
        self.packages: list[Package] = []
        self.destinations: dict[str, list[Destination]] = {}
        self.defs: dict[str, tuple] = {}          # package node id -> definition
        self.placeholders: dict[tuple[str, str], tuple] = {}   # (scope, @name) -> (package, variable name)
        self.counts = {"scanned": 0, "in_scope": 0, "excluded": 0, "unreadable": 0, "data_flows": 0,
                       "destinations": 0, "execute_sql_tasks": 0, "execute_sql_indexed": 0}
        self.file_errors: list[tuple[str, str, str]] = []
        self.component_classes: dict[str, int] = {}

    # ================================================================ loading
    def scan_root(self, root: Path, root_label: str, include=(), exclude=()) -> None:
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.suffix.lower() == ".dtsx")
        for path in files:
            rel = path.name if root.is_file() else path.relative_to(root).as_posix()
            self.counts["scanned"] += 1
            if (include and not matches(rel, include)) or (exclude and matches(rel, exclude)):
                self.counts["excluded"] += 1
                continue
            self.counts["in_scope"] += 1
            try:
                data = read_package(path.read_bytes())
            except (OSError, PackageError) as exc:
                self.counts["unreadable"] += 1
                self.file_errors.append((root_label, rel, f"{type(exc).__name__}: {str(exc)[:200]}"))
                continue
            self.add(Package(path, root, root_label, rel, data))

    def add(self, pkg: Package) -> None:
        self.packages.append(pkg)
        data = pkg.data
        for task in data.tasks:
            task.branches = self._branches(data, task)
        for kind, message, line in data.issues:
            pkg.issues.append(Gap(kind, message, None, [self.evidence(pkg, line, message, "package")]))
        for flow in data.flows:
            self.counts["data_flows"] += 1
            for comp in flow.components:
                self.component_classes[comp.cls] = self.component_classes.get(comp.cls, 0) + 1
                if comp.cls in DESTINATIONS:
                    self._destination(pkg, flow, comp)
        self._sql_tasks(pkg)

    def attach(self, analyzer) -> None:
        self.analyzer = analyzer

    # ----------------------------------------------------------- identities
    def evidence(self, pkg: Package, line: int, excerpt: str, role: str) -> Evidence:
        return Evidence(pkg.rel, max(1, line), max(1, line), collapse(excerpt), role,
                        stable_id("pkg", pkg.root_label, pkg.rel, line), pkg.root_label)

    def scope(self, pkg: Package, flow_or_task) -> str:
        task = getattr(flow_or_task, "task", flow_or_task)
        return f"{pkg.label}::{task.path}"

    def identity(self, pkg: Package, task, comp: Component | None = None) -> str:
        text = f"package {pkg.rel}, task {task.path}"
        return text + (f", component {comp.name}" if comp is not None else "")

    def connection(self, pkg: Package, ref: str | None):
        if not ref:
            return None
        ref_key = ref.split(":external")[0]
        for cm in pkg.data.connections:
            if ref_key in (cm.ref_id, cm.dtsid, cm.name):
                return cm
        return None

    def database_for(self, pkg: Package, ref: str | None) -> tuple[str | None, str | None, list[str], str]:
        """(database, database_source, notes, status) for a connection reference."""
        cm = self.connection(pkg, ref)
        name = cm.name if cm is not None else bracket_name(ref)
        notes = []
        if name and name.lower() in self.connection_map:
            return self.connection_map[name.lower()], "configured", [
                f"Database {self.connection_map[name.lower()]} for connection {name} is from --connection-map."], VERIFIED
        if cm is None:
            notes.append(f"Connection {name or ref} is not defined in this package (a project connection?); "
                         "give its database with --connection-map.")
        elif cm.database:
            status = VERIFIED
            runtime = {k: v for k, v in cm.expressions.items()
                       if k.lower() in ("connectionstring", "initialcatalog", "servername")}
            if runtime:
                status = INFERRED
                notes.append(f"Connection {cm.name} has property expressions ({'; '.join(f'{k} = {v}' for k, v in sorted(runtime.items()))}) "
                             f"that can change its database at run time; its design-time database {cm.database} is used.")
            return norm(cm.database), "connection_manager", notes, status
        else:
            notes.append(f"Connection {cm.name} names no database (Initial Catalog); give one with --connection-map.")
        if self.catalog.default_database:
            return self.catalog.default_database, "default", notes, INFERRED
        return None, None, notes, VERIFIED

    def table_ref(self, text: str, database: str | None, source: str | None) -> ObjectRef | None:
        parts = [p for p in split_name(text.strip().strip('"')) if p]
        if not parts or len(parts) > 4:
            return None
        parts = [None] * (4 - len(parts)) + parts
        server, db, schema, name = parts
        if db:
            database, source = db, "explicit"
        ref = ObjectRef(norm(name), norm(schema) or self.catalog.default_schema, norm(database), norm(server),
                        "table", None, source)
        canonical, _ = self.catalog.canonical(ref)
        return canonical

    def column_node(self, pkg: Package, flow: Flow, col: Col) -> Node:
        port = col.port
        node = Node(PACKAGE_COLUMN, col.name, ObjectRef(f"{port.component.name}.{port.name}", kind="package_flow",
                                                          scope=self.scope(pkg, flow)))
        self.defs.setdefault(node.id, ("column", pkg, flow, col))
        return node

    def replaced_node(self, pkg: Package, flow: Flow, comp: Component, ic: Col) -> Node:
        node = Node(PACKAGE_COLUMN, ic.name, ObjectRef(f"{comp.name}.{ic.port.name}", kind="package_flow",
                                                         scope=self.scope(pkg, flow)))
        self.defs.setdefault(node.id, ("replaced", pkg, flow, comp, ic))
        return node

    def file_node(self, pkg: Package, cm, name: str) -> Node:
        node = Node(FILE_COLUMN, name, ObjectRef(cm.name if cm is not None else "?", kind="flat_file",
                                                 scope=pkg.label))
        self.defs.setdefault(node.id, ("file", pkg, cm, name))
        return node

    def variable_node(self, pkg: Package, name: str) -> Node:
        if "::" not in name and not name.startswith("$"):
            name = f"User::{name}"
        node = Node(PACKAGE_VARIABLE, name, ObjectRef("variables", kind="package_flow", scope=pkg.label))
        self.defs.setdefault(node.id, ("variable", pkg, name))
        return node

    # ------------------------------------------------------------- branches
    def _branches(self, data: PackageFile, task) -> list[str]:
        out = []
        chain = []
        current = task
        while current is not None and current.kind != "package":
            chain.append(current)
            current = current.parent
        by_id = {}
        for t in data.tasks:
            by_id[t.key] = t
            if t.dtsid:
                by_id[t.dtsid] = t
        for item in reversed(chain):
            if item.kind == "for_loop":
                out.append(f"FOR LOOP container {item.name}")
            elif item.kind == "foreach_loop":
                out.append(f"FOREACH LOOP container {item.name}")
            for pc in data.constraints:
                if pc.target not in (item.key, item.dtsid):
                    continue
                source = by_id.get(pc.source)
                before = source.name if source is not None else pc.source
                outcome = {"1": "fails", "2": "completes"}.get(pc.value, "succeeds")
                # EvalOp: 1 expression, 2 constraint (the default when absent), 3 both, 4 either.
                expression = pc.expression if pc.expression and pc.eval_op in ("1", "3", "4") else None
                any_of = "" if pc.logical_and else " (one of several alternative constraints)"
                if expression and pc.eval_op == "3":
                    out.append(f"IF {expression} AND {before} {outcome} (precedence constraint){any_of}")
                elif expression and pc.eval_op == "4":
                    out.append(f"IF {expression} OR {before} {outcome} (precedence constraint){any_of}")
                elif expression:
                    out.append(f"IF {expression} (precedence constraint after {before}){any_of}")
                elif pc.value in ("1", "2"):
                    out.append(f"AFTER {before} {outcome} (precedence constraint){any_of}")
            if item.disabled:
                out.append(f"DISABLED: {item.name} is disabled in the package and does not run as saved")
        return out

    # ========================================================= destinations
    def _destination(self, pkg: Package, flow: Flow, comp: Component) -> None:
        props = {k: v.value for k, v in comp.props.items()}
        mode = props.get("AccessMode", "0")
        table = None
        if comp.cls == "oledb_destination":
            if mode in ("0", "3", ""):
                table = props.get("OpenRowset")
            elif mode in ("1", "4"):
                table = self._static_variable(pkg, props.get("OpenRowsetVariable"), comp, "destination table")
        elif comp.cls == "adonet_destination":
            table = props.get("TableOrViewName")
        elif comp.cls == "sqlserver_destination":
            table = props.get("BulkInsertTableName")
        port = comp.inputs[0] if comp.inputs else None
        if port is None:
            return
        if not table:
            pkg.issues.append(Gap("unsupported_package_step",
                                  f"Destination {comp.name} in {self.identity(pkg, flow.task)} writes a table that is not "
                                  "named statically (access mode or table from a variable/expression); the columns it "
                                  "loads are not mapped.", None,
                                  [self.evidence(pkg, comp.line, f"destination {comp.name}", "destination")],
                                  {"package": pkg.label, "task": flow.task.path, "component": comp.name}))
            return
        if isinstance(table, tuple):        # (table, note) from a variable
            table, note = table
        else:
            note = None
        db, source, notes, status = self.database_for(pkg, comp.connections[0][1] if comp.connections else None)
        ref = self.table_ref(table, db, source)
        if ref is None:
            return
        if note:
            notes = notes + [note]
            status = worst(status, INFERRED)
        if ref.database_source == "unified":
            notes = notes + [f"The destination's database is unknown; {ref.database} is the only database known for "
                             f"{ref.schema}.{ref.name}."]
            status = worst(status, INFERRED)
        elif ref.database_source == "default":
            status = worst(status, INFERRED)
        mapping = {}
        for ic in port.columns:
            name = port.external_name(ic)
            if name:
                mapping.setdefault(norm(name), ic)
        self.counts["destinations"] += 1
        self.destinations.setdefault(ref.key, []).append(
            Destination(pkg, flow, comp, port, ref, status, notes, table, mapping))
        if ref.database and ref.schema:
            self.catalog._register(ref)

    def _static_variable(self, pkg: Package, name: str | None, comp: Component, what: str):
        var = pkg.data.variables.get(name or "")
        if var is None or var.expression or name in pkg.data.runtime_setters or not var.value:
            return None
        return var.value, f"The {what} comes from variable {name} (its design-time value {var.value}); it can be " \
                          "changed at run time."

    def writes_object(self, ref: ObjectRef) -> bool:
        keys = [ref.key] + self.catalog.aliases_of(ref)
        return any(self.destinations.get(k) for k in keys)

    # =========================================================== Execute SQL
    def _sql_tasks(self, pkg: Package) -> None:
        for n, task in enumerate(pkg.data.sql_tasks):
            self.counts["execute_sql_tasks"] += 1
            where = self.identity(pkg, task.task)
            evidence = [self.evidence(pkg, task.line, f"Execute SQL task {task.task.name}", "execute_sql")]
            detail = {"package": pkg.label, "task": task.task.path}
            runtime = task.task.props.get("SqlStatementSource")
            if task.source_type.lower() != "directinput" or runtime:
                why = (f"its statement is set by the property expression {runtime}" if runtime else
                       f"its statement comes from {'variable' if task.source_type.lower() == 'variable' else 'a file connection'} "
                       f"{task.sql}")
                pkg.issues.append(Gap("package_runtime_value" if runtime or task.source_type.lower() == "variable"
                                      else "unsupported_package_step",
                                      f"Execute SQL task {task.task.path} in {pkg.rel} runs SQL that is not in the package "
                                      f"file ({why}); what it writes is not analysed.", None, evidence,
                                      {**detail, "source_type": task.source_type, "expression": runtime,
                                       "variable": task.sql if task.source_type.lower() == "variable" else None}))
                continue
            if not task.sql.strip():
                continue
            db, _, _, _ = self.database_for(pkg, task.connection)
            scope = self.scope(pkg, task.task)
            sql = self._placeholders(task.sql, scope, pkg, {p[0]: p[1] for p in task.parameters})
            line = max(task.line, 2)
            text = "\n" * (line - 2) + (f"USE [{db}];" if db else "") + "\n" + sql + "\n"
            source = SourceFile(pkg.path.parent / f"{pkg.path.name}#sql{n}", text)
            label = f"{where}" + (" (disabled)" if task.task.disabled else "")

            def adjust(statement, scope=scope, label=label):
                return replace(statement, scope=scope, procedure=label)

            try:
                unit = self.catalog.add_embedded(source, pkg.root, pkg.root_label, pkg.rel, adjust)
                self.counts["execute_sql_indexed"] += 1
                # Precedence constraints and loop containers around the task become branch context.
                for statement in unit.index.statements:
                    unit.controls[statement.start] = tuple(task.task.branches) + tuple(
                        unit.controls.get(statement.start, ()))
            except Exception as exc:  # noqa: BLE001 - one task must not stop the scan
                pkg.issues.append(Gap("unsupported_package_step", f"Execute SQL task {task.task.path} could not be "
                                      f"indexed ({type(exc).__name__}).", None, evidence, detail))

    def _placeholders(self, sql: str, scope: str, pkg: Package, bindings: dict) -> str:
        """Replace OLE DB ``?`` markers by ``@ssis_param_<n>`` so the SQL parses and each marker is a
        variable the adapter can bind to the package variable mapped to it."""
        masked = mask_noncode(sql)
        out, last, n = [], 0, 0
        for m in re.finditer(r"\?", masked):
            name = f"{PLACEHOLDER}{n}"
            out.append(sql[last:m.start()] + name)
            last = m.end()
            self.placeholders[(scope, name)] = (pkg, bindings.get(str(n)), f"parameter {n}")
            n += 1
        out.append(sql[last:])
        for key, var in bindings.items():
            if key and key.startswith("@") and var:
                self.placeholders.setdefault((scope, key.lower()), (pkg, var, f"parameter {key}"))
        return "".join(out)

    # ============================================================== tracing
    def handles(self, node: Node) -> bool:
        if node.kind in PACKAGE_NODE_KINDS:
            return True
        return node.kind == VARIABLE and (node.scope, node.name) in self.placeholders \
            and not self.catalog.assignments.get((node.scope, node.name))

    def expand(self, node: Node):
        """Steps, gaps, stops and cycles for a package-internal node."""
        if node.kind == VARIABLE:
            return self._placeholder_steps(node)
        definition = self.defs.get(node.id)
        if definition is None:
            return [], [Gap("package_metadata", "This package node has no definition in the scanned packages.", node)], [], []
        kind = definition[0]
        try:
            if kind == "column":
                steps, gaps, stops = self._column_steps(node, *definition[1:])
            elif kind == "replaced":
                steps, gaps, stops = self._replaced_steps(node, *definition[1:])
            elif kind == "file":
                steps, gaps, stops = self._file_stop(node, *definition[1:])
            elif kind == "variable":
                steps, gaps, stops = self._variable_steps(node, *definition[1:])
            elif kind == "reference":
                steps, gaps, stops = self._reference_steps(node, *definition[1:])
            else:
                steps, gaps, stops = [], [], []
        except Exception as exc:  # noqa: BLE001 - one component must not stop the trace
            pkg = definition[1]
            steps, stops = [], []
            gaps = [Gap("unsupported_package_step", f"The package adapter could not process this node "
                        f"({type(exc).__name__}: {str(exc)[:120]}).", node,
                        [self.evidence(pkg, 1, str(node.display), "package")])]
        return steps, gaps, stops, []

    def merge(self, node: Node, found):
        """Add package destination steps to the SQL result for a database column."""
        steps, gaps, stops, cycles = found
        ref = node.object
        if ref is None or ref.kind not in ("table", "view"):
            return found
        keys = [ref.key] + self.catalog.aliases_of(ref)
        # A destination named without database context matches only when one database is known for the
        # name (the catalog's unification rule), so that binding is inferred.
        dests = [(d, VERIFIED if k == ref.key else INFERRED, None)
                 for k in dict.fromkeys(keys) for d in self.destinations.get(k, [])]
        extra_gaps: list[Gap] = []
        if ref.database and ref.schema:
            unknown = ObjectRef(ref.name, ref.schema, None, None).key
            dbs = sorted(self.catalog.known_databases.get((ref.schema, ref.name), ()))
            if unknown not in keys and len(dbs) > 1:
                for d in self.destinations.get(unknown, []):
                    gap = Gap("ambiguous_object", f"Destination {d.component.name} in {d.package.rel} has no database "
                              f"context for {ref.schema}.{ref.name}; it may load this object or the same name in "
                              f"{', '.join(x for x in dbs if x != ref.database)}.", node,
                              [self.evidence(d.package, d.component.line, f"destination {d.table_text}", "destination")],
                              {"candidates": dbs})
                    extra_gaps.append(gap)
                    dests.append((d, AMBIGUOUS, gap.id))
        files = self._package_files()
        package_sql = [s for s in steps if s.target.id == node.id and s.evidence
                       and (s.evidence[0].root, s.evidence[0].path) in files]
        if not dests and len({files[(s.evidence[0].root, s.evidence[0].path)] for s in package_sql}) < 2:
            return found
        writing, loading = [], []
        for d, status, gap_id in dests:
            ic = d.mapping.get(node.name)
            (writing if ic is not None else loading).append((d, ic, status, gap_id))
        new_steps: list[Step] = []
        new_gaps: list[Gap] = list(extra_gaps)
        for d, ic, status, gap_id in writing:
            s, g = self._destination_steps(node, d, ic)
            for step in s:
                step.status = worst(step.status, status)
                if status == INFERRED and d.ref.database is None:
                    step.notes.append(f"The destination names {d.table_text} without a database; it is matched to "
                                      f"{ref.display} because that is the only database known for the name.")
                if gap_id:
                    step.gaps = sorted(set(step.gaps) | {gap_id})
            new_steps.extend(s)
            new_gaps.extend(g)
            new_gaps.extend(d.package.issues)
        copies = sorted({d.package.label for d, *_ in writing} |
                        {files[(s.evidence[0].root, s.evidence[0].path)] for s in package_sql})
        if len(copies) > 1:
            gap = Gap("ambiguous_package_copy",
                      f"{len(copies)} package files in scope write {node.display} ({', '.join(copies)}). They may be "
                      "environment, test or historical copies of one package; which one is deployed and runs (and in "
                      "which order) needs evidence outside the files. Narrow the scope with --package-include / "
                      "--package-exclude.", node,
                      [self.evidence(d.package, d.component.line, f"destination {d.component.name} ({d.table_text})",
                                     "destination") for d, *_ in writing][:10],
                      {"copies": copies})
            gap.evidence.extend(s.evidence[0] for s in package_sql[:5])
            new_gaps.append(gap)
            for step in new_steps + package_sql:
                if step.target.id == node.id:
                    step.status = worst(step.status, AMBIGUOUS)
                    step.gaps = sorted(set(step.gaps) | {gap.id})
        columns, _, _ = self.catalog.schema_for(ref)
        info = next((c for c in columns or [] if c.name == node.name), None)
        generated = info is not None and (info.identity or info.computed or info.rowversion)
        loaders = []
        for d, _, _, _ in loading:
            loaders.append(f"{d.package.rel} ({d.flow.task.path}, destination {d.component.name})")
            if generated:
                continue
            new_gaps.append(Gap("package_column_not_mapped",
                                f"Destination {d.component.name} in {self.identity(d.package, d.flow.task)} loads "
                                f"{d.table_text} but maps no input column to {node.name}. The rows it adds hold the "
                                "column's default or NULL; the package does not explain this column.", node,
                                [self.evidence(d.package, d.port.line, f"destination input of {d.component.name}: "
                                               f"{len(d.mapping)} mapped column(s), none is {node.name}", "destination")],
                                {"package": d.package.label, "task": d.flow.task.path, "component": d.component.name,
                                 "mapped_columns": len(d.mapping)}))
        if new_steps:
            stops = [s for s in stops if not (s.node.id == node.id and s.reason == "source_without_writer")]
            gaps = [g for g in gaps if not (g.kind == "missing_definition" and g.node is not None and g.node.id == node.id)]
            overwriting = [s for s in steps if s.target.id == node.id and s.writer_kind in ("update", "merge_update")]
            if overwriting:
                new_gaps.append(Gap("ambiguous_writer_order",
                                    f"{node.display} is loaded by a package and overwritten by SQL (UPDATE/MERGE); which "
                                    "value survives depends on run order.", node,
                                    [s.evidence[0] for s in overwriting + new_steps if s.evidence][:10]))
        if loaders:
            text = (f" In-scope package destination(s) load this table but map no input to this column: "
                    f"{'; '.join(sorted(loaders))}.")
            stops = [Stop(s.node, s.reason, (s.detail or "") + text) if s.node.id == node.id and
                     s.reason == "source_without_writer" else s for s in stops]
            for gap in gaps:
                if gap.node is not None and gap.node.id == node.id and gap.kind == "missing_definition":
                    gap.detail = {**gap.detail, "package_loaders_without_this_column": sorted(loaders)}
        return steps + new_steps, gaps + new_gaps, stops, cycles

    def _destination_steps(self, node: Node, d: Destination, ic: Col) -> tuple[list[Step], list[Gap]]:
        r = self.resolve(d.package, d.flow, d.component, d.port, ic.lineage)
        item, _ = self._input(r, ic.name)
        operation = "direct" if item.node.name.lower() == node.name.lower() else "rename"
        step, gaps = self._step(d.package, d.flow, d.component, node, "package_destination", ic.name, operation,
                                "destination_column_mapping", [item], ic.line,
                                f"{d.component.name}: {d.table_text}.{node.name} <- {ic.name}", "destination", [r],
                                status=d.status, notes=d.notes)
        return [step], gaps

    def _package_files(self) -> dict:
        """(root label, root-relative path) -> package label, to recognise steps from Execute SQL tasks."""
        return {(p.root_label, p.rel): p.label for p in self.packages}

    # ---------------------------------------------------------- step helpers
    def _step(self, pkg: Package, flow: Flow, comp: Component, target: Node, writer_kind: str, expression: str,
              operation: str, method: str, inputs: list[Input], line: int, excerpt: str, role: str,
              resolved: list[Resolved] = (), status: str = VERIFIED, notes=(), gaps=()) -> tuple[Step, list[Gap]]:
        rows, branches, all_gaps = [], list(flow.task.branches), list(gaps)
        for r in resolved:
            for row in r.rows:
                if all((row.node.id, row.role, row.expression) != (x.node.id, x.role, x.expression) for x in rows):
                    rows.append(row)
            branches.extend(b for b in r.branches if b not in branches)
            all_gaps.extend(r.gaps)
            status = worst(status, r.status)
        statuses = [i.status for i in inputs]
        status = worst(status, *statuses) if statuses else status
        step = Step(target=target, writer_kind=writer_kind, expression=expression, operation=operation,
                    inputs=inputs, row_selection=rows, branch_context=branches, mapping_method=method, status=status,
                    procedure=self.identity(pkg, flow.task, comp),
                    evidence=[self.evidence(pkg, line, excerpt, role)], order=line)
        value_gaps = [g for g in all_gaps if not g.detail.get("raised_in_row_selection")]
        step.gaps = sorted({g.id for g in value_gaps} | {i.gap for i in inputs if i.gap})
        if step.gaps and step.status in (VERIFIED, INFERRED):
            step.status = PARTIAL
        step.notes.append(f"From package copy {pkg.label}.")
        step.notes.extend(notes)
        if flow.task.disabled or any(b.startswith("DISABLED") for b in flow.task.branches):
            step.notes.append("The data flow task (or a container around it) is disabled in this package file.")
        return step, all_gaps

    def _input(self, r: Resolved, expression: str, role: str = VALUE, branch: str | None = None) -> tuple[Input, list[Gap]]:
        if r.node is not None:
            return Input(r.node, role, expression, branch, r.status), []
        gap = next((g for g in r.gaps if not g.detail.get("raised_in_row_selection")), None)
        return Input(Node(UNKNOWN, expression), role, expression, branch, UNRESOLVED, gap.id if gap else None), []

    def _gap(self, kind: str, message: str, node: Node | None, pkg: Package, line: int, excerpt: str,
             detail: dict | None = None, row: bool = False) -> Gap:
        detail = dict(detail or {})
        if row:
            detail["raised_in_row_selection"] = True
        return Gap(kind, message, node, [self.evidence(pkg, line, excerpt, "gap")], detail)

    # ------------------------------------------------------------- resolution
    def resolve(self, pkg: Package, flow: Flow, comp: Component, port: Port, lineage: str | None,
                row: bool = False, depth: int = 0) -> Resolved:
        """The node whose value input column ``lineage`` of ``port`` carries, and the row selection and
        branches of the path between them."""
        rows, branches, gaps = [], [], []
        status = VERIFIED
        current = port
        if lineage is None:
            return Resolved(None, rows, branches, [self._gap("package_metadata", f"An input column of {comp.name} has "
                                                             "no lineage id.", None, pkg, port.line, port.name, row=row)],
                            UNRESOLVED)
        for _ in range(500):
            out = flow.upstream.get(current.id)
            if out is None:
                gaps.append(self._gap("package_metadata", f"Input {current.name} of {current.component.name} is not "
                                      "connected to any output by a data-flow path.", None, pkg, current.line,
                                      current.name, row=row))
                return Resolved(None, rows, branches, gaps, UNRESOLVED)
            up = out.component
            if depth < 4:
                b, r, g = self.port_context(pkg, flow, up, out, depth)
                branches = b + [x for x in branches if x not in b]
                rows = r + rows
                gaps.extend(g)
            col = flow.lineage.get(lineage)
            if col is not None and col.port is out:
                return Resolved(self.column_node(pkg, flow, col), rows, branches, gaps, status)
            if col is not None and col.port.component is up:
                gaps.append(self._gap("package_metadata", f"Column {col.name} is produced on output {col.port.name} of "
                                      f"{up.name}, not on the output {out.name} this path leaves from.", None, pkg,
                                      out.line, out.name, row=row))
                return Resolved(None, rows, branches, gaps, UNRESOLVED)
            rewrite = next((ic for p in up.inputs for ic in p.columns if ic.lineage == lineage and
                            ic.usage == "readwrite"), None)
            if rewrite is not None:
                if up.cls == "derived_column" and (rewrite.props.get("Expression") or rewrite.props.get("FriendlyExpression")):
                    return Resolved(self.replaced_node(pkg, flow, up, rewrite), rows, branches, gaps, status)
                gaps.append(self._gap("unsupported_package_step", f"{up.name} ({up.cls_raw}) may change {rewrite.name} "
                                      "in place (read/write input column); what it does is not analysed.", None, pkg,
                                      rewrite.line, f"{up.name}: {rewrite.name} (read/write)",
                                      {"package": pkg.label, "task": flow.task.path, "component": up.name,
                                       "component_class": up.cls}, row=row))
                return Resolved(None, rows, branches, gaps, UNRESOLVED)
            if out.sync_input is None:
                gaps.append(self._gap("package_metadata", f"Lineage id {lineage} is read by {comp.name} but is not "
                                      f"produced upstream of it (it does not pass the asynchronous output {out.name} "
                                      f"of {up.name}).", None, pkg, out.line, out.name, row=row))
                return Resolved(None, rows, branches, gaps, UNRESOLVED)
            if up.cls not in PASS_THROUGH:
                gaps.append(self._gap("unsupported_package_step", f"{up.name} ({up.cls_raw}) is on the data-flow path; "
                                      "its effect on the rows and values passing through it is not analysed.", None,
                                      pkg, up.line, f"component {up.name} ({up.cls_raw})",
                                      {"package": pkg.label, "task": flow.task.path, "component": up.name,
                                       "component_class": up.cls}, row=row))
                status = PARTIAL
            nxt = flow.ports.get(out.sync_input)
            if nxt is None:
                gaps.append(self._gap("package_metadata", f"Output {out.name} of {up.name} names a synchronous input "
                                      "that does not exist.", None, pkg, out.line, out.name, row=row))
                return Resolved(None, rows, branches, gaps, UNRESOLVED)
            current = nxt
        gaps.append(self._gap("package_metadata", "The data-flow path is too long or cyclic.", None, pkg, port.line,
                              port.name, row=row))
        return Resolved(None, rows, branches, gaps, UNRESOLVED)

    def port_context(self, pkg: Package, flow: Flow, comp: Component, out: Port, depth: int):
        """Branches, row selection and gaps of rows leaving ``comp`` through ``out``."""
        branches, rows, gaps = [], [], []
        if out.is_error:
            branches.append(f"error output {out.name} of {comp.name} (rows that failed in {comp.name})")
        if comp.cls == "conditional_split" and not out.is_error:
            others = [o for o in comp.outputs if o is not out and not o.is_error and not _is_default(o)]
            if _is_default(out):
                conditions = [(o, _condition(o)) for o in sorted(others, key=_evaluation_order)]
                parts = [f"NOT ({c})" for _, c in conditions if c]
                text = " AND ".join(parts) or "(no other output has a condition)"
                sources = [(o, c) for o, c in conditions if c]
                label = f"Conditional Split {comp.name} default output {out.name}: {self._display(flow, text)}"
            else:
                text = _condition(out) or ""
                sources = [(out, text)] if text else []
                label = f"Conditional Split {comp.name} output {out.name}: {self._display(flow, text)}"
            branches.append(label)
            port = comp.inputs[0] if comp.inputs else None
            for o, cond in sources:
                for ref, node, gap in self._expression_nodes(pkg, flow, comp, port, cond, depth, row=True):
                    rows.append(RowSelection(node or Node(UNKNOWN, ref.text), FILTER, label,
                                             self.evidence(pkg, _condition_line(o), f"{comp.name} {o.name}: {cond}", "filter"),
                                             VERIFIED if node is not None else UNRESOLVED, gap.id if gap else None))
                    if gap is not None:
                        gaps.append(gap)
        if comp.cls == "lookup":
            kind = "no match output" if "nomatch" in out.name.lower().replace(" ", "") or \
                (out.props.get("IsNoMatchOutput") and out.props["IsNoMatchOutput"].value.lower() == "true") \
                else ("error output" if out.is_error else "match output")
            branches.append(f"Lookup {comp.name} {kind} {out.name}")
            r, g = self.lookup_keys(pkg, flow, comp, depth, kind)
            rows.extend(r)
            gaps.extend(g)
        return branches, rows, gaps

    def _display(self, flow: Flow, text: str) -> str:
        names = {k: c.name for k, c in flow.lineage.items()}
        names.update({k: c.name for k, c in flow.input_cols.items()})
        for c in flow.input_cols.values():
            if c.lineage:
                names.setdefault(c.lineage, c.name)
        return sx.friendly(text or "", names)

    def lookup_keys(self, pkg: Package, flow: Flow, comp: Component, depth: int, kind: str = "match output"):
        rows, gaps = [], []
        port = comp.inputs[0] if comp.inputs else None
        for ic in (port.columns if port else []):
            ref_col = ic.props.get("JoinToReferenceColumn")
            if not ref_col or not ref_col.value:
                continue
            r = self.resolve(pkg, flow, comp, port, ic.lineage, row=True, depth=depth + 1)
            expression = f"{comp.name}: {ic.name} = reference.{ref_col.value}" + (" (no match)" if kind == "no match output" else "")
            ev = self.evidence(pkg, ic.line, expression, "join")
            gap = next((g for g in r.gaps), None)
            gaps.extend(r.gaps)
            rows.append(RowSelection(r.node or Node(UNKNOWN, ic.name), JOIN_KEY, expression, ev,
                                     VERIFIED if r.node is not None else UNRESOLVED, gap.id if gap and r.node is None else None))
            ref_node = self.reference_node(pkg, flow, comp, ref_col.value)
            rows.append(RowSelection(ref_node, JOIN_KEY, expression, ev))
        return rows, gaps

    def reference_node(self, pkg: Package, flow: Flow, comp: Component, column: str) -> Node:
        node = Node(PACKAGE_COLUMN, column, ObjectRef(f"{comp.name}.reference", kind="package_flow",
                                                      scope=self.scope(pkg, flow)))
        self.defs.setdefault(node.id, ("reference", pkg, flow, comp, column))
        return node

    def _expression_nodes(self, pkg: Package, flow: Flow, comp: Component, port: Port | None, text: str,
                          depth: int, row: bool = False):
        """(reference, node or None, gap or None) for every reference in an expression (conditions)."""
        out = []
        try:
            refs = sx.references(text)
        except sx.ExpressionError as exc:
            gap = self._gap("package_expression", f"The expression of {comp.name} could not be parsed ({exc}); the "
                            "columns it reads are not identified.", None, pkg, comp.line, text, row=row)
            return [(sx.Reference("column", text, VALUE, None, text), None, gap)]
        for ref in refs:
            if ref.kind == "literal":
                continue
            node, gap = self._ref_node(pkg, flow, comp, port, ref, depth, row)
            out.append((ref, node, gap))
        return out

    def _ref_node(self, pkg, flow, comp, port, ref, depth, row=False):
        if ref.kind == "var":
            return self.variable_node(pkg, ref.value), None
        lineage = None
        if ref.kind == "lineage":
            lineage = ref.value
        elif port is not None:
            ic = next((c for c in port.columns if c.name.lower() == ref.value.lower()), None)
            lineage = ic.lineage if ic is not None else None
        if lineage is None or port is None:
            return None, self._gap("package_metadata", f"{comp.name} refers to {ref.text}, which is not one of its "
                                   "input columns.", None, pkg, comp.line, ref.text, row=row)
        r = self.resolve(pkg, flow, comp, port, lineage, row=row, depth=depth + 1)
        return r.node, (r.gaps[0] if r.node is None and r.gaps else None)

    # ------------------------------------------------------- column steps
    def _column_steps(self, node: Node, pkg: Package, flow: Flow, col: Col):
        comp = col.port.component
        cls = comp.cls
        if col.special in ("1", "2") or (col.port.is_error and col.name in ("ErrorCode", "ErrorColumn")):
            return [], [], [Stop(node, "package_runtime", f"Error information generated by SSIS on error output "
                                 f"{col.port.name} of {comp.name}.")]
        if cls == "flat_file_source":
            return self._flat_file_source(node, pkg, flow, comp, col)
        if cls in ("oledb_source", "adonet_source"):
            return self._db_source(node, pkg, flow, comp, col)
        if cls == "data_conversion":
            source = normalize_ref(col.props["SourceInputColumnLineageID"].value) if col.props.get("SourceInputColumnLineageID") else None
            port = comp.inputs[0] if comp.inputs else None
            return self._mapped(node, pkg, flow, comp, col, port, source, "package_data_conversion", "cast",
                                "source_input_lineage", lambda name: f"({_ssis_type(col)}){name}")
        if cls == "copy_column":
            source = normalize_ref(col.props["copyColumnId"].value) if col.props.get("copyColumnId") else None
            ic = flow.input_cols.get(source) if source else None
            lineage = ic.lineage if ic is not None else source
            port = comp.inputs[0] if comp.inputs else None
            return self._mapped(node, pkg, flow, comp, col, port, lineage, "package_copy_column", "direct",
                                "copy_column_id", lambda name: f"copy of {name}")
        if cls == "sort":
            source = normalize_ref(col.props["SortColumnId"].value) if col.props.get("SortColumnId") else None
            port = comp.inputs[0] if comp.inputs else None
            notes = []
            if comp.props.get("EliminateDuplicates") and comp.props["EliminateDuplicates"].value.lower() in ("true", "1", "-1"):
                notes.append(f"Sort {comp.name} removes duplicate rows.")
            return self._mapped(node, pkg, flow, comp, col, port, source, "package_sort", "direct", "sort_column_id",
                                lambda name: name, notes)
        if cls == "derived_column":
            expression = col.props.get("Expression") or col.props.get("FriendlyExpression")
            port = comp.inputs[0] if comp.inputs else None
            return self._expression_step(node, pkg, flow, comp, port, col, expression, "package_derived_column")
        if cls == "lookup":
            return self._lookup_output(node, pkg, flow, comp, col)
        if cls in ("union_all", "merge"):
            return self._union(node, pkg, flow, comp, col)
        if cls == "merge_join":
            return self._merge_join(node, pkg, flow, comp, col)
        if cls == "aggregate":
            return self._aggregate(node, pkg, flow, comp, col)
        gap = self._gap("unsupported_package_step", f"{node.name} is produced by {comp.name} ({comp.cls_raw}), a "
                        "component the package adapter does not analyse.", node, pkg, col.line,
                        f"component {comp.name} ({comp.cls_raw}) output column {col.name}",
                        {"package": pkg.label, "task": flow.task.path, "component": comp.name, "component_class": comp.cls})
        return [], [gap], []

    def _mapped(self, node, pkg, flow, comp, col, port, lineage, kind, operation, method, render, notes=()):
        if port is None or lineage is None:
            gap = self._gap("package_metadata", f"{comp.name} does not say which input column {col.name} is made from.",
                            node, pkg, col.line, col.name)
            return [], [gap], []
        r = self.resolve(pkg, flow, comp, port, lineage)
        ic = next((c for c in port.columns if c.lineage == lineage), None)
        name = ic.name if ic is not None else (r.node.name if r.node is not None else lineage)
        expression = render(name)
        item, _ = self._input(r, name)
        if operation == "direct" and item.node.name.lower() != node.name.lower():
            operation = "rename"
        step, gaps = self._step(pkg, flow, comp, node, kind, expression, operation, method, [item], col.line,
                                f"{comp.name}: {col.name} = {expression}", "transformation", [r], notes=notes)
        return [step], gaps, []

    def _expression_step(self, node, pkg, flow, comp, port, col, prop, kind):
        if prop is None or not prop.value:
            gap = self._gap("package_metadata", f"{comp.name} has no expression for {col.name}.", node, pkg, col.line,
                            col.name)
            return [], [gap], []
        text = prop.value
        shown = self._display(flow, (col.props.get("FriendlyExpression") or prop).value)
        try:
            tree = sx.parse(text)
            refs = sx.references(text, tree)
            operation = sx.operation(tree)
        except sx.ExpressionError as exc:
            gap = self._gap("package_expression", f"The expression for {col.name} in {comp.name} could not be parsed "
                            f"({exc}); only the lineage ids written in it are shown, without roles.", node, pkg,
                            prop.line, shown)
            inputs, resolved = [], []
            for lineage in sx.lineage_refs(text):
                r = self.resolve(pkg, flow, comp, port, lineage)
                resolved.append(r)
                item, _ = self._input(r, lineage)
                item.status = worst(item.status, PARTIAL)
                inputs.append(item)
            step, gaps = self._step(pkg, flow, comp, node, kind, shown, "expression", "expression", inputs, prop.line,
                                    f"{comp.name}: {col.name} = {shown}", "expression", resolved, PARTIAL, gaps=[gap])
            return [step], gaps, []
        inputs, resolved, gaps = [], [], []
        for ref in refs:
            ref.branch = self._display(flow, ref.branch) if ref.branch else None
            if ref.kind == "literal":
                inputs.append(Input(Node(CONSTANT, ref.value), VALUE, ref.text, ref.branch))
                continue
            if ref.kind == "var":
                inputs.append(Input(self.variable_node(pkg, ref.value), ref.role, ref.text, ref.branch))
                continue
            lineage = ref.value if ref.kind == "lineage" else next(
                (c.lineage for c in (port.columns if port else []) if c.name.lower() == ref.value.lower()), None)
            if lineage is None or port is None:
                gap = self._gap("package_metadata", f"The expression for {col.name} refers to {ref.text}, which is not an "
                                f"input column of {comp.name}.", node, pkg, prop.line, ref.text)
                gaps.append(gap)
                inputs.append(Input(Node(UNKNOWN, ref.text), ref.role, ref.text, ref.branch, UNRESOLVED, gap.id))
                continue
            r = self.resolve(pkg, flow, comp, port, lineage)
            resolved.append(r)
            item, _ = self._input(r, self._display(flow, ref.text), ref.role, ref.branch)
            inputs.append(item)
        if operation == "direct" and inputs and inputs[0].node.name.lower() != node.name.lower():
            operation = "rename"
        step, more = self._step(pkg, flow, comp, node, kind, shown, operation, "expression", inputs, prop.line,
                                f"{comp.name}: {col.name} = {shown}", "expression", resolved, gaps=gaps)
        return [step], more, []

    def _replaced_steps(self, node, pkg, flow, comp, ic):
        prop = ic.props.get("Expression") or ic.props.get("FriendlyExpression")
        steps, gaps, stops = self._expression_step(node, pkg, flow, comp, ic.port, ic, prop, "package_derived_column")
        for step in steps:
            step.notes.append(f"{comp.name} replaces the value of {ic.name} in place; the expression reads the value "
                              "it had before.")
        return steps, gaps, stops

    # ------------------------------------------------------------- sources
    def _external(self, node, pkg, flow, comp, col):
        name = col.port.external_name(col)
        if name is None:
            return None, self._gap("package_metadata", f"Output column {col.name} of {comp.name} is not linked to an "
                                   "external (source) column.", node, pkg, col.line, col.name)
        return name, None

    def _flat_file_source(self, node, pkg, flow, comp, col):
        name, gap = self._external(node, pkg, flow, comp, col)
        if gap is not None:
            return [], [gap], []
        cm = self.connection(pkg, comp.connections[0][1] if comp.connections else None)
        gaps = []
        if cm is None:
            gaps.append(self._gap("package_metadata", f"The flat-file connection of {comp.name} is not defined in the "
                                  "package.", node, pkg, comp.line, comp.name))
        elif cm.columns and name not in cm.columns:
            gaps.append(self._gap("package_metadata", f"Flat-file connection {cm.name} declares no column {name}.", node,
                                  pkg, cm.line, cm.name))
        file_node = self.file_node(pkg, cm, name)
        operation = "direct" if name.lower() == col.name.lower() else "rename"
        step, more = self._step(pkg, flow, comp, node, "package_source", name, operation, "external_metadata_column",
                                [Input(file_node, VALUE, name)], col.line,
                                f"{comp.name}: {col.name} <- file column {name}", "source", gaps=gaps)
        return [step], more, []

    def _file_stop(self, node, pkg, cm, name):
        if cm is None:
            return [], [], [Stop(node, "external_file", f"Flat-file column {name} (connection not defined in {pkg.rel}).")]
        where = cm.file_path or "?"
        runtime = cm.expressions.get("ConnectionString")
        detail = f"Flat-file connection {cm.name} in {pkg.rel}: {where}"
        if runtime:
            detail += f" (path set at run time by the expression {runtime})"
        return [], [], [Stop(node, "external_file", detail + ".")]

    def _db_source(self, node, pkg, flow, comp, col):
        name, gap = self._external(node, pkg, flow, comp, col)
        if gap is not None:
            return [], [gap], []
        props = {k: v.value for k, v in comp.props.items()}
        mode = props.get("AccessMode", "")
        db, source, notes, status = self.database_for(pkg, comp.connections[0][1] if comp.connections else None)
        identity = {"package": pkg.label, "task": flow.task.path, "component": comp.name}
        if comp.cls == "adonet_source":
            table = props.get("TableOrViewName") if mode in ("", "0") else None
            sql = props.get("SqlCommand") if mode == "2" or (not table and props.get("SqlCommand")) else None
            variable = None
        else:
            table = props.get("OpenRowset") if mode in ("", "0") else None
            sql = props.get("SqlCommand") if mode == "2" or (mode == "" and not table) else None
            variable = props.get("SqlCommandVariable") if mode == "3" else (
                props.get("OpenRowsetVariable") if mode == "1" else None)
            if mode == "1" and variable:
                value = self._static_variable(pkg, variable, comp, "source table")
                if value is None:
                    return [], [self._runtime_variable_gap(node, pkg, comp, variable, "the source table")], []
                table = value[0]
                notes = notes + [value[1]]
                status = worst(status, INFERRED)
                variable = None
        if variable:
            var = pkg.data.variables.get(variable)
            if var is None or var.expression or variable in pkg.data.runtime_setters or not var.value:
                return [], [self._runtime_variable_gap(node, pkg, comp, variable, "the source query")], []
            sql = var.value
            notes = notes + [f"The query is the design-time value of variable {variable}; configurations, "
                             "parameters or dtexec /SET can change it at run time."]
            status = worst(status, INFERRED)
        if table:
            ref = self.table_ref(table, db, source)
            if ref is None:
                gap = self._gap("package_metadata", f"Source table name {table} of {comp.name} could not be read.", node,
                                pkg, comp.line, table, identity)
                return [], [gap], []
            column = Node(COLUMN, norm(name), ref)
            status = worst(status, INFERRED if ref.database_source in ("default", "unified") else VERIFIED)
            operation = "direct" if name.lower() == col.name.lower() else "rename"
            step, more = self._step(pkg, flow, comp, node, "package_source", f"{table}.{name}", operation,
                                    "external_metadata_column", [Input(column, VALUE, f"{table}.{name}", None, status)],
                                    col.line, f"{comp.name}: {col.name} <- {table}.{name}", "source", status=status,
                                    notes=notes)
            return [step], more, []
        if sql:
            steps, gaps = self.query_steps(pkg, flow, comp, sql, db, node, name, comp.props.get("SqlCommand").line
                                           if comp.props.get("SqlCommand") else comp.line, "source query")
            for step in steps:
                if step.target.id == node.id:
                    step.status = worst(step.status, status)
                    step.notes.extend(notes)
            return steps, gaps, []
        gap = self._gap("unsupported_package_step", f"Source {comp.name} uses an access mode the package adapter does not "
                        f"read (AccessMode {mode or '?'}).", node, pkg, comp.line, comp.name, identity)
        return [], [gap], []

    def _runtime_variable_gap(self, node, pkg, comp, variable, what):
        var = pkg.data.variables.get(variable)
        setters = pkg.data.runtime_setters.get(variable, [])
        if var is None:
            why = f"variable {variable} is not defined in this package"
        elif var.expression:
            why = f"variable {variable} is the expression {var.expression}"
        elif setters:
            why = f"variable {variable} is set at run time by {', '.join(sorted(set(setters)))}"
        else:
            why = f"variable {variable} has no design-time value"
        return self._gap("package_runtime_value", f"{comp.name} reads {what} from a variable whose value is only known "
                         f"at run time: {why}. The columns it returns are not mapped; every candidate stays open.",
                         node, pkg, comp.line, f"{comp.name}: {what} from {variable}",
                         {"package": pkg.label, "component": comp.name, "variable": variable,
                          "expression": var.expression if var is not None else None,
                          "set_by": sorted(set(setters)),
                          "design_time_value": collapse(var.value or "", 200) if var is not None and var.value else None})

    def query_steps(self, pkg, flow, comp, sql, db, target, column, line, what) -> tuple[list[Step], list[Gap]]:
        """Map output ``column`` of an embedded query to its sources with the SQL analyzer."""
        scope = f"{self.scope(pkg, flow)}::{comp.name}"
        identity = {"package": pkg.label, "task": flow.task.path, "component": comp.name}
        sql = self._placeholders(sql, scope, pkg, self._source_parameters(pkg, comp))
        try:
            tree = sqlglot.parse_one(sql, read="tsql")
        except Exception as exc:  # noqa: BLE001 - sqlglot raises several error types
            tree = None
            error = f"{type(exc).__name__}: {str(exc)[:120]}"
        else:
            error = None
        if tree is None or not isinstance(tree, (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery)) \
                or tree.args.get("into") is not None:
            kind = "parse_error" if tree is None else "unsupported_package_step"
            gap = self._gap(kind, f"The {what} of {comp.name} could not be analysed as a SELECT"
                            f"{f' ({error})' if error else ''}; output column {column} is not mapped.", target, pkg,
                            line, sql, identity)
            item = Input(Node(UNKNOWN, f"{what} column {column}"), VALUE, column, None, UNRESOLVED, gap.id)
            step, gaps = self._step(pkg, flow, comp, target, "package_source_query", column, "expression",
                                    "query_output_column", [item], line, f"{comp.name}: {what}", "source", status=PARTIAL,
                                    gaps=[gap])
            return [step], gaps
        unit, statement = self._query_unit(pkg, comp, sql, db, line, scope, tree)
        writer = Writer(ObjectRef(comp.name, kind="derived", scope=scope), "select_into", statement, unit,
                        statement.procedure, 0)
        steps, gaps, _ = self.analyzer.writer_steps(writer, norm(column), target)
        if not any(s.target.id == target.id for s in steps):
            gaps.append(self._gap("package_metadata", f"The {what} of {comp.name} does not return a column {column}.",
                                  target, pkg, line, sql, identity))
        for step in steps:
            step.notes.append(f"From package copy {pkg.label}; SQL embedded in {comp.name}.")
            step.branch_context = list(flow.task.branches) + [b for b in step.branch_context if b not in flow.task.branches]
            if step.target.id == target.id:
                step.writer_kind = "package_source_query"
        return steps, gaps

    def _source_parameters(self, pkg, comp) -> dict:
        """OLE DB source ``ParameterMapping``: ``"Parameter0:Input",{variable DTSID};`` or ``...,User::v;``."""
        raw = comp.props.get("ParameterMapping")
        out = {}
        if raw is None or not raw.value:
            return out
        by_id = {v.dtsid: v.name for v in pkg.data.variables.values() if v.dtsid}
        for n, m in enumerate(re.finditer(r'"([^"]*)"\s*,\s*([^;]+);', raw.value)):
            target = m.group(2).strip()
            out[str(n)] = by_id.get(target, target)
        return out

    def _query_unit(self, pkg, comp, sql, db, line, scope, tree):
        path = f"{pkg.path}#{comp.id}"
        unit = Unit(pkg.root_label, pkg.root, pkg.rel, path, sql, mask_noncode(sql), SourceIndex(), [], db)
        statement = Statement(path, line, 0, len(sql), sql, self.identity(pkg, comp_task(comp, pkg), comp), scope,
                              "parsed", tree)
        return unit, statement

    # ---------------------------------------------------- lookup / unions
    def _lookup_output(self, node, pkg, flow, comp, col):
        ref_col = col.props.get("CopyFromReferenceColumn")
        if ref_col is None or not ref_col.value:
            gap = self._gap("package_metadata", f"Lookup output column {col.name} names no reference column.", node, pkg,
                            col.line, col.name)
            return [], [gap], []
        reference = self.reference_node(pkg, flow, comp, ref_col.value)
        rows, gaps = self.lookup_keys(pkg, flow, comp, 0)
        step, more = self._step(pkg, flow, comp, node, "package_lookup", f"reference.{ref_col.value}",
                                "direct" if ref_col.value.lower() == col.name.lower() else "rename", "reference_column",
                                [Input(reference, VALUE, f"reference.{ref_col.value}")], col.line,
                                f"{comp.name}: {col.name} <- reference.{ref_col.value}", "lookup", gaps=gaps)
        step.row_selection.extend(rows)
        behaviour = comp.props.get("NoMatchBehavior")
        if behaviour is not None and behaviour.value == "0":
            step.notes.append("Rows without a match are treated as errors (NoMatchBehavior 0); "
                              "with error handling set to ignore, they pass with NULL here.")
        return [step], more, []

    def _reference_steps(self, node, pkg, flow, comp, column):
        props = {k: v.value for k, v in comp.props.items()}
        sql = props.get("SqlCommand") or ""
        db, source, notes, status = self.database_for(pkg, comp.connections[0][1] if comp.connections else None)
        if not sql.strip():
            table = props.get("OpenRowset") or props.get("TableOrViewName")
            if table:
                ref = self.table_ref(table, db, source)
                if ref is not None:
                    step, gaps = self._step(pkg, flow, comp, node, "package_lookup_reference", f"{table}.{column}",
                                            "direct", "reference_table", [Input(Node(COLUMN, norm(column), ref), VALUE,
                                                                                f"{table}.{column}", None, status)],
                                            comp.line, f"{comp.name} reference {table}", "lookup", status=status,
                                            notes=notes)
                    return [step], gaps, []
            gap = self._gap("package_metadata", f"The reference data of Lookup {comp.name} (cache or a query set at run "
                            "time) is not in the package; its columns are not mapped.", node, pkg, comp.line, comp.name,
                            {"package": pkg.label, "component": comp.name})
            return [], [gap], []
        line = comp.props["SqlCommand"].line
        steps, gaps = self.query_steps(pkg, flow, comp, sql, db, node, column, line, "reference query")
        for step in steps:
            if step.target.id == node.id:
                step.writer_kind = "package_lookup_reference"
                step.status = worst(step.status, status)
                step.notes.extend(notes)
        return steps, gaps, []

    def _union(self, node, pkg, flow, comp, col):
        steps, gaps = [], []
        inputs = comp.inputs
        for i, port in enumerate(inputs, 1):
            label = f"{'Union All' if comp.cls == 'union_all' else 'Merge'} {comp.name} input {i} of {len(inputs)} ({port.name})"
            ic = next((c for c in port.columns if c.props.get("OutputColumnLineageID")
                       and normalize_ref(c.props["OutputColumnLineageID"].value) == col.lineage), None)
            if ic is None:
                step, more = self._step(pkg, flow, comp, node, f"package_{comp.cls}", "NULL", "constant",
                                        "output_column_lineage", [Input(Node(CONSTANT, "NULL"), VALUE, "NULL")],
                                        port.line, f"{comp.name}: input {port.name} supplies no {col.name}",
                                        "transformation")
                step.branch_context.append(label)
                step.notes.append(f"Input {port.name} maps no column to {col.name}; its rows hold NULL.")
            else:
                r = self.resolve(pkg, flow, comp, port, ic.lineage)
                item, _ = self._input(r, ic.name)
                step, more = self._step(pkg, flow, comp, node, f"package_{comp.cls}", ic.name,
                                        "direct" if ic.name.lower() == col.name.lower() else "rename",
                                        "output_column_lineage", [item], ic.line,
                                        f"{comp.name}: {col.name} <- {port.name}.{ic.name}", "transformation", [r])
                step.branch_context.append(label)
            steps.append(step)
            gaps.extend(more)
        return steps, gaps, []

    def _merge_join(self, node, pkg, flow, comp, col):
        source = normalize_ref(col.props["InputColumnID"].value) if col.props.get("InputColumnID") else None
        ic = flow.input_cols.get(source) if source else None
        if ic is None:
            gap = self._gap("package_metadata", f"Merge Join output column {col.name} names no input column.", node, pkg,
                            col.line, col.name)
            return [], [gap], []
        r = self.resolve(pkg, flow, comp, ic.port, ic.lineage)
        item, _ = self._input(r, ic.name)
        join = {"0": "full outer", "1": "left outer", "2": "inner"}.get(
            comp.props["JoinType"].value if comp.props.get("JoinType") else "", "unknown")
        step, gaps = self._step(pkg, flow, comp, node, "package_merge_join", f"{ic.port.name}.{ic.name}",
                                "direct" if ic.name.lower() == col.name.lower() else "rename", "input_column_id", [item],
                                col.line, f"{comp.name}: {col.name} <- {ic.port.name}.{ic.name}", "join", [r],
                                notes=[f"Merge Join {comp.name} is a {join} join."])
        rows, more = self._merge_keys(pkg, flow, comp)
        step.row_selection.extend(rows)
        if not rows:
            more.append(self._gap("package_metadata", f"The join key columns of Merge Join {comp.name} could not be "
                                  "identified (no sort key positions on its inputs).", None, pkg, comp.line, comp.name,
                                  row=True))
        return [step], gaps + more, []

    def _merge_keys(self, pkg, flow, comp):
        count = int(comp.props["NumKeyColumns"].value) if comp.props.get("NumKeyColumns") and \
            comp.props["NumKeyColumns"].value.isdigit() else 0
        keyed = []
        for port in comp.inputs:
            cols = []
            for ic in port.columns:
                upstream = flow.lineage.get(ic.lineage or "")
                position = (upstream.sort_key if upstream is not None else None) or ""
                if position.lstrip("-").isdigit() and 0 < abs(int(position)) <= (count or 99):
                    cols.append((abs(int(position)), ic))
            keyed.append((port, sorted(cols, key=lambda x: x[0])))
        rows, gaps = [], []
        if len(keyed) == 2:
            for (pos, left), (_, right) in zip(keyed[0][1], keyed[1][1]):
                expression = f"{comp.name}: {keyed[0][0].name}.{left.name} = {keyed[1][0].name}.{right.name}"
                for port, ic in ((keyed[0][0], left), (keyed[1][0], right)):
                    r = self.resolve(pkg, flow, comp, port, ic.lineage, row=True, depth=1)
                    gaps.extend(r.gaps)
                    rows.append(RowSelection(r.node or Node(UNKNOWN, ic.name), JOIN_KEY, expression,
                                             self.evidence(pkg, ic.line, expression, "join"),
                                             VERIFIED if r.node is not None else UNRESOLVED))
        return rows, gaps

    def _aggregate(self, node, pkg, flow, comp, col):
        agg = col.props.get("AggregationType")
        agg_type = agg.value if agg is not None else ""
        source = normalize_ref(col.props["AggregationColumnId"].value) if col.props.get("AggregationColumnId") else None
        port = comp.inputs[0] if comp.inputs else None
        func = AGGREGATIONS.get(agg_type)
        if func is None or port is None:
            gap = self._gap("package_metadata", f"Aggregate output column {col.name} has no known aggregation.", node, pkg,
                            col.line, col.name)
            return [], [gap], []
        inputs, resolved = [], []
        name = "*"
        if agg_type != "2":
            ic = flow.input_cols.get(source or "") or next((c for c in port.columns if c.lineage == source), None)
            lineage = ic.lineage if ic is not None else source
            if lineage is None:
                gap = self._gap("package_metadata", f"Aggregate output column {col.name} names no input column.", node,
                                pkg, col.line, col.name)
                return [], [gap], []
            r = self.resolve(pkg, flow, comp, port, lineage)
            resolved.append(r)
            name = ic.name if ic is not None else lineage
            inputs.append(self._input(r, name)[0])
        expression = name if agg_type == "0" else ("COUNT(*)" if agg_type == "2" else f"{func}({name})")
        step, gaps = self._step(pkg, flow, comp, node, "package_aggregate", expression,
                                "direct" if agg_type == "0" else "aggregate", "aggregation_column_id", inputs, col.line,
                                f"{comp.name}: {col.name} = {expression}", "aggregate", resolved)
        for out in comp.outputs:
            for other in out.columns:
                if other.props.get("AggregationType") and other.props["AggregationType"].value == "0":
                    key = normalize_ref(other.props["AggregationColumnId"].value) if other.props.get("AggregationColumnId") else None
                    ic = flow.input_cols.get(key or "") or next((c for c in port.columns if c.lineage == key), None)
                    lineage = ic.lineage if ic is not None else key
                    r = self.resolve(pkg, flow, comp, port, lineage, row=True, depth=1)
                    gaps.extend(r.gaps)
                    step.row_selection.append(RowSelection(r.node or Node(UNKNOWN, other.name), GROUP_BY,
                                                           f"{comp.name}: group by {ic.name if ic else other.name}",
                                                           self.evidence(pkg, other.line, other.name, "group_by"),
                                                           VERIFIED if r.node is not None else UNRESOLVED))
        return [step], gaps, []

    # ------------------------------------------------------------ variables
    def _variable_steps(self, node, pkg, name):
        if name.startswith("System::"):
            return [], [], [Stop(node, "package_runtime", f"SSIS system variable {name}, set by SSIS at run time.")]
        if name.startswith("$"):
            return [], [], [Stop(node, "package_parameter", f"{name} is a project or package parameter; its value "
                                 "comes from the deployment environment or the execution.")]
        var = pkg.data.variables.get(name)
        line = var.line if var is not None else 1
        if var is None:
            return [], [self._gap("package_metadata", f"Variable {name} is not defined in this package.", node, pkg, 1,
                                  name)], []
        setters = sorted(set(pkg.data.runtime_setters.get(name, [])))
        flowless = _TaskOnly(pkg)
        gaps = []
        if setters:
            gaps.append(self._gap("package_runtime_value", f"Variable {name} is set at run time by "
                                  f"{', '.join(setters)}; its value is not known from the package file.", node, pkg,
                                  line, name, {"variable": name, "set_by": setters}))
        if var.expression:
            try:
                refs = sx.references(var.expression)
                operation = sx.operation(sx.parse(var.expression))
            except sx.ExpressionError as exc:
                gaps.append(self._gap("package_expression", f"The expression of variable {name} could not be parsed "
                                      f"({exc}).", node, pkg, line, var.expression))
                return [], gaps, []
            inputs = []
            for ref in refs:
                if ref.kind == "literal":
                    inputs.append(Input(Node(CONSTANT, ref.value), VALUE, ref.text, ref.branch))
                elif ref.kind == "var":
                    inputs.append(Input(self.variable_node(pkg, ref.value), ref.role, ref.text, ref.branch))
                else:
                    gap = self._gap("package_expression", f"The expression of variable {name} refers to {ref.text}, "
                                    "which is not a variable.", node, pkg, line, var.expression)
                    gaps.append(gap)
                    inputs.append(Input(Node(UNKNOWN, ref.text), ref.role, ref.text, ref.branch, UNRESOLVED, gap.id))
            step, more = self._step(pkg, flowless, None, node, "package_variable", var.expression, operation,
                                    "variable_expression", inputs, line, f"{name} = {var.expression}", "variable",
                                    gaps=gaps)
            return [step], more, []
        if setters:
            return [], gaps, []
        value = var.value if var.value is not None else ""
        step, more = self._step(pkg, flowless, None, node, "package_variable", value, "constant", "design_time_value",
                                [Input(Node(CONSTANT, _literal(value)), VALUE, value)], line, f"{name} = {value}",
                                "variable", status=INFERRED,
                                notes=["Design-time value; configurations, parameters or dtexec /SET can override it."])
        return [step], more, []

    def _placeholder_steps(self, node: Node):
        pkg, variable, what = self.placeholders[(node.scope, node.name)]
        if not variable:
            gap = Gap("package_runtime_value", f"SQL {what} ({node.name}) in {pkg.rel} has no variable bound to it in "
                      "the package.", node, [self.evidence(pkg, 1, node.name, "gap")], {"parameter": node.name})
            return [], [gap], [], []
        var_node = self.variable_node(pkg, variable)
        step, gaps = self._step(pkg, _TaskOnly(pkg), None, node, "package_parameter_binding", variable, "direct",
                                "parameter_binding", [Input(var_node, VALUE, variable)],
                                (pkg.data.variables.get(variable).line if pkg.data.variables.get(variable) else 1),
                                f"{what} <- {variable}", "parameter")
        step.notes.append(f"The package binds {variable} to {what} of this SQL ({node.name} stands for the ? marker).")
        return [step], gaps, [], []

    # =============================================================== reports
    def scan_info(self, include=(), exclude=()) -> dict:
        return {**self.counts, "issues": sum(len(p.issues) for p in self.packages),
                "file_errors": len(self.file_errors), "include": list(include), "exclude": list(exclude),
                "connection_map": [f"{k}={v}" for k, v in sorted(self.connection_map.items())]}

    def issue_entries(self) -> list[dict]:
        out = [{**g.to_dict(), "package": p.label} for p in self.packages for g in p.issues]
        out += [{"kind": "unreadable_package", "root": r, "path": path, "error": e} for r, path, e in self.file_errors]
        return sorted(out, key=lambda e: (e.get("package") or e.get("path") or "", e.get("kind")))

    def summary(self) -> dict:
        """Counts only, for gap-summary.json: no package, task, component, table or column names."""
        issues: dict[str, int] = {}
        for p in self.packages:
            for g in p.issues:
                issues[g.kind] = issues.get(g.kind, 0) + 1
        formats: dict[str, int] = {}
        for p in self.packages:
            formats[p.data.format] = formats.get(p.data.format, 0) + 1
        known = {k: v for k, v in sorted(self.component_classes.items())}
        return {"packages_scanned": self.counts["scanned"], "packages_in_scope": self.counts["in_scope"],
                "packages_excluded": self.counts["excluded"], "packages_unreadable": self.counts["unreadable"],
                "package_formats": dict(sorted(formats.items())), "data_flows": self.counts["data_flows"],
                "destinations": self.counts["destinations"], "execute_sql_tasks": self.counts["execute_sql_tasks"],
                "execute_sql_indexed": self.counts["execute_sql_indexed"], "components_by_class": known,
                "package_issues_by_kind": dict(sorted(issues.items()))}


class _TaskOnly:
    """Stand-in flow for package-level steps (variables, parameter bindings): no task branches."""

    def __init__(self, pkg: Package):
        self.task = next((t for t in pkg.data.tasks if t.kind == "package"), None) or _Root()


class _Root:
    path = "Package"
    branches: list = []
    disabled = False


def comp_task(comp: Component, pkg: Package):
    for flow in pkg.data.flows:
        if comp in flow.components:
            return flow.task
    return _Root()


def _is_default(port: Port) -> bool:
    prop = port.props.get("IsDefaultOut")
    return prop is not None and prop.value.lower() in ("true", "-1", "1")


def _condition(port: Port) -> str | None:
    prop = port.props.get("Expression") or port.props.get("FriendlyExpression")
    return prop.value if prop is not None and prop.value else None


def _condition_line(port: Port) -> int:
    prop = port.props.get("Expression") or port.props.get("FriendlyExpression")
    return prop.line if prop is not None else port.line


def _evaluation_order(port: Port) -> int:
    prop = port.props.get("EvaluationOrder")
    return int(prop.value) if prop is not None and prop.value.lstrip("-").isdigit() else 0


def _ssis_type(col: Col) -> str:
    kind = (col.data_type or "?").upper()
    return f"DT_{kind},{col.length}" if col.length and col.length != "0" else f"DT_{kind}"


def _literal(value: str) -> str:
    return "N'" + value.replace("'", "''") + "'" if not re.fullmatch(r"-?\d+(?:\.\d+)?", value) else value


def parse_connection_map(items) -> dict[str, str]:
    out = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--connection-map expects NAME=DB, got {item!r}")
        name, db = item.rsplit("=", 1)
        out[name.strip()] = db.strip()
    return out
