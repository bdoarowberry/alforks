"""One pass over the scanned SQL that indexes objects, schemas, writers, calls and variables.

The catalog never decides lineage. It records what each statement explicitly
says, plus the context needed to bind names later: database, scope, control flow.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
import hashlib
from pathlib import Path
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from table_lineage.index import DEFINITION, GO, IDENT, SourceIndex, Statement, mask_noncode
from table_lineage.models import normalize_name
from table_lineage.sources import SourceFile

from .control import control_contexts, header_end
from .model import Evidence, ObjectRef

USE = re.compile(r"^[ \t]*USE[ \t]+(?P<db>\[(?:[^\]]|\]\])*\]|\"[^\"]+\"|[A-Za-z_][\w$#@]*)", re.I | re.M)
DYNAMIC = re.compile(r"\s*EXEC(?:UTE)?\s*(?:\(|(?:\[?sys\]?\s*\.\s*)?\[?sp_executesql\]?(?!\w)|@\w+\s*;?\s*$|@\w+\s+(?!=))", re.I)
INSERT_EXEC = re.compile(rf"\s*INSERT\s+(?:INTO\s+)?(?P<target>{IDENT})(?:\s*\([^)]*\))?\s+EXEC(?:UTE)?\b", re.I)
WRITE_TARGET = re.compile(
    rf"\b(?:INSERT\s+(?:INTO\s+)?|UPDATE\s+|MERGE\s+(?:INTO\s+)?|\bINTO\s+)(?P<target>{IDENT})", re.I)
SYNONYM = re.compile(rf"\bCREATE\s+SYNONYM\s+(?P<name>{IDENT})\s+FOR\s+(?P<target>{IDENT})", re.I)
ALTER_ADD = re.compile(rf"\bALTER\s+TABLE\s+(?P<name>{IDENT})\s+ADD\s+(?P<body>[^;]*)", re.I)
TEMP_MARK = "#"


def ident_text(node) -> str | None:
    if node is None:
        return None
    if isinstance(node, exp.Identifier):
        name = node.this
        return ("#" + name) if node.args.get("temporary") and not name.startswith("#") else name
    if isinstance(node, exp.Parameter):
        return node.sql(dialect="tsql")
    if isinstance(node, exp.Expression):
        return node.name or node.sql(dialect="tsql")
    return str(node)


def table_parts(table: exp.Table) -> tuple[str | None, str | None, str | None, str]:
    """(server, database, schema, name) as written, not normalized."""
    this = table.this
    if isinstance(this, exp.Dot):
        return (ident_text(table.args.get("catalog")), ident_text(table.args.get("db")),
                ident_text(this.this), ident_text(this.expression))
    name = ident_text(this)
    return None, ident_text(table.args.get("catalog")), ident_text(table.args.get("db")), name


def split_name(text: str) -> list[str]:
    parts = re.findall(r'\[(?:[^\]]|\]\])*\]|"(?:[^"]|"")*"|[^.\s]+', text.strip())
    out = []
    for part in parts:
        if part.startswith("[") and part.endswith("]"):
            part = part[1:-1].replace("]]", "]")
        elif part.startswith('"') and part.endswith('"'):
            part = part[1:-1].replace('""', '"')
        out.append(part)
    return out


def norm(value: str | None) -> str | None:
    return value.lower() if value else value


def collapse(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Unit:
    """One source file."""
    root_label: str
    root: Path
    rel_path: str
    path: str
    text: str
    masked: str
    index: SourceIndex
    use_points: list[tuple[int, str]]
    mapped_database: str | None
    controls: dict[int, tuple[str, ...]] = field(default_factory=dict)

    def database_at(self, offset: int, default: str | None) -> tuple[str | None, str | None]:
        used = [db for at, db in self.use_points if at <= offset]
        if used:
            return used[-1], "use_statement"
        if self.mapped_database:
            return self.mapped_database, "configured"
        if default:
            return default, "default"
        return None, None

    def line_at(self, offset: int) -> int:
        return self.text.count("\n", 0, offset) + 1


@dataclass
class ProcInfo:
    key: str
    ref: ObjectRef
    scope: str
    unit: Unit
    params: list[str]
    start: int
    line: int


@dataclass
class ColumnInfo:
    name: str
    ordinal: int
    identity: bool = False
    computed: str | None = None
    rowversion: bool = False
    default: str | None = None


@dataclass
class TableSchema:
    ref: ObjectRef
    columns: list[ColumnInfo]
    statement: Statement
    unit: Unit
    source: str  # create_table, table_variable, alter_add


@dataclass
class Writer:
    ref: ObjectRef
    kind: str          # insert, select_into, update, merge, view
    statement: Statement
    unit: Unit
    procedure: str | None
    order: int

    @property
    def controls(self) -> tuple[str, ...]:
        return self.unit.controls.get(self.statement.start, ())


@dataclass
class Call:
    caller_scope: str
    callee: ObjectRef
    statement: Statement
    unit: Unit
    args: list[tuple[str | None, exp.Expression | None]]


@dataclass
class Assignment:
    variable: str      # normalized, with @
    scope: str
    statement: Statement
    unit: Unit
    value: exp.Expression | None
    select: exp.Select | None   # SELECT @v = col FROM ... context
    kind: str          # set, select, declare, fetch, exec_output


@dataclass
class StatementIssue:
    kind: str          # dynamic_sql, parse_error, insert_exec, unsupported_statement
    statement: Statement
    unit: Unit
    targets: tuple[str, ...]   # normalized names mentioned as write targets, when known
    message: str


class Catalog:
    def __init__(self, default_database: str | None = None, default_schema: str = "dbo",
                 database_map: list[tuple[str, str]] | None = None):
        self.default_database = norm(default_database)
        self.default_schema = norm(default_schema) or "dbo"
        self.database_map = [(p.replace("\\", "/").rstrip("/").lower(), norm(db)) for p, db in (database_map or [])]
        self.units: list[Unit] = []
        self.units_by_path: dict[str, Unit] = {}
        self.schemas: dict[str, list[TableSchema]] = defaultdict(list)
        self.views: dict[str, list[Writer]] = defaultdict(list)
        self.writers: dict[str, list[Writer]] = defaultdict(list)
        self.procedures: dict[str, list[ProcInfo]] = defaultdict(list)
        self.proc_by_scope: dict[str, ProcInfo] = {}
        self.calls_by_callee: dict[str, list[Call]] = defaultdict(list)
        self.calls_by_caller: dict[str, list[Call]] = defaultdict(list)
        self.assignments: dict[tuple[str, str], list[Assignment]] = defaultdict(list)
        self.temp_creators: dict[str, set[str]] = defaultdict(set)   # scope -> {#name}
        self.temp_writer_scopes: dict[str, set[str]] = defaultdict(set)  # #name -> {scope}
        self.synonyms: dict[str, tuple[ObjectRef, Statement, Unit]] = {}
        self.issues: list[StatementIssue] = []
        self.issues_by_target: dict[str, list[StatementIssue]] = defaultdict(list)
        self.dynamic_by_scope: dict[str, list[StatementIssue]] = defaultdict(list)
        self.known_databases: dict[tuple[str, str], set[str]] = defaultdict(set)  # (schema, name) -> {db}
        self.unknown_db_writers: dict[tuple[str, str], list[Writer]] = defaultdict(list)
        self.stats = {"files": 0, "statements": 0, "parsed": 0, "fallback": 0}
        self.fallback_samples: list[tuple[Statement, Unit]] = []

    # ---------------------------------------------------------------- loading
    def _mapped_database(self, root: Path, rel: str, absolute: str) -> str | None:
        best = None
        for prefix, db in self.database_map:
            for candidate in (rel.lower(), absolute.replace("\\", "/").lower()):
                if candidate == prefix or candidate.startswith(prefix + "/"):
                    if best is None or len(prefix) > len(best[0]):
                        best = (prefix, db)
        return best[1] if best else None

    def add_file(self, source: SourceFile, root: Path, root_label: str) -> None:
        index = SourceIndex()
        index.add(source, include_ddl=True)
        rel = source.path.name if root.is_file() else source.path.relative_to(root).as_posix()
        # Scopes embed the absolute path; make them portable so identities are stable across machines.
        prefix = f"{source.path}::"
        portable = f"{root_label}/{rel}::"
        index.statements = [replace(s, scope=portable + s.scope[len(prefix):]) if s.scope.startswith(prefix) else s
                            for s in index.statements]
        text = source.text
        masked = mask_noncode(text)
        uses = [(m.start(), norm(split_name(m.group("db"))[0])) for m in USE.finditer(masked)]
        unit = Unit(root_label, root, rel, str(source.path), text, masked, index, uses,
                    self._mapped_database(root, rel, str(source.path)))
        unit.controls = control_contexts(text, masked, index.statements)
        self.units.append(unit)
        self.units_by_path[unit.path] = unit
        self.stats["files"] += 1
        self._procedures(unit)
        self._synonyms(unit)
        for order, statement in enumerate(index.statements):
            self.stats["statements"] += 1
            self.stats["parsed" if statement.parse_status == "parsed" else "fallback"] += 1
            try:
                self._statement(unit, statement, order)
            except (SqlglotError, ValueError, AttributeError, TypeError, RecursionError) as exc:
                self._issue("unsupported_statement", unit, statement, (), f"Statement could not be indexed: {type(exc).__name__}")

    # ------------------------------------------------------------- identities
    def ref(self, server, database, schema, name, unit: Unit, statement: Statement | None,
            kind: str = "table") -> ObjectRef:
        """Build an ObjectRef for a name written in ``statement``; temp scope is resolved separately."""
        name = name or ""
        if name.startswith("@"):
            return ObjectRef(norm(name), kind="table_variable", scope=statement.scope if statement else None)
        if name.startswith("#") and not name.startswith("##"):
            scope = statement.scope if statement else None
            return ObjectRef(norm(name), kind="temp_table", scope=scope)
        db_source = "explicit" if database else None
        if not database:
            database, db_source = unit.database_at(statement.start if statement else 0, self.default_database)
        schema_norm = norm(schema) if schema else self.default_schema
        return ObjectRef(norm(name), schema_norm, norm(database), norm(server), kind, None, db_source)

    def ref_from_table(self, table: exp.Table, unit: Unit, statement: Statement | None) -> ObjectRef:
        server, database, schema, name = table_parts(table)
        return self.ref(server, database, schema, name, unit, statement)

    def ref_from_text(self, text: str, unit: Unit, statement: Statement | None) -> ObjectRef:
        parts = split_name(text)
        parts = [None] * (4 - len(parts)) + parts[-4:]
        return self.ref(parts[0], parts[1], parts[2], parts[3], unit, statement)

    def canonical(self, ref: ObjectRef) -> tuple[ObjectRef, list[str]]:
        """Follow synonyms and unify an unknown database when exactly one is known.

        Returns the ref and the list of candidate databases when unification was
        not possible because several exist.
        """
        if ref.kind != "table" and ref.kind != "view":
            return ref, []
        seen = set()
        while ref.key in self.synonyms and ref.key not in seen:
            seen.add(ref.key)
            ref = self.synonyms[ref.key][0]
        if ref.database is None and ref.schema and ref.server is None:
            dbs = sorted(self.known_databases.get((ref.schema, ref.name), ()))
            if len(dbs) == 1:
                return ObjectRef(ref.name, ref.schema, dbs[0], None, ref.kind, None, "unified"), []
            if len(dbs) > 1:
                return ref, dbs
        return ref, []

    def _register(self, ref: ObjectRef) -> None:
        if ref.kind in ("table", "view") and ref.database and ref.schema and ref.server is None:
            self.known_databases[(ref.schema, ref.name)].add(ref.database)

    def temp_scopes(self, name: str, scope: str, depth: int = 0, seen: set | None = None) -> list[str]:
        """Scopes whose #temp table ``name`` is visible to code running in ``scope``."""
        if name in self.temp_creators.get(scope, ()):
            return [scope]
        if depth > 12:
            return []
        seen = seen or set()
        if scope in seen:
            return []
        seen.add(scope)
        proc = self.proc_by_scope.get(scope)
        if proc is None:
            return []
        found: list[str] = []
        for call in self.calls_by_callee.get(proc.key, []):
            for candidate in self.temp_scopes(name, call.caller_scope, depth + 1, seen):
                if candidate not in found:
                    found.append(candidate)
        return sorted(found)

    def is_parameter(self, scope: str, name: str) -> bool:
        proc = self.proc_by_scope.get(scope)
        return bool(proc and name in proc.params)

    def evidence(self, unit: Unit, statement: Statement, node: exp.Expression | None = None,
                 role: str = "statement", excerpt: str | None = None) -> Evidence:
        start_line = statement.line
        end_line = statement.line + statement.text.rstrip().count("\n")
        if node is not None:
            positions = [n.meta.get("start") for n in node.walk() if isinstance(n.meta.get("start"), int)]
            ends = [n.meta.get("end") for n in node.walk() if isinstance(n.meta.get("end"), int)]
            if positions and ends and 0 <= min(positions) <= max(ends) < len(statement.text):
                start_line = statement.line + statement.text.count("\n", 0, min(positions))
                end_line = statement.line + statement.text.count("\n", 0, max(ends))
        if excerpt is None:
            excerpt = node.sql(dialect="tsql") if node is not None else statement.text
        return Evidence(unit.rel_path, start_line, end_line, collapse(excerpt), role,
                        statement_id(unit, statement), unit.root_label)

    # --------------------------------------------------------------- indexing
    def _issue(self, kind, unit, statement, targets, message):
        issue = StatementIssue(kind, statement, unit, tuple(targets), message)
        self.issues.append(issue)
        for target in targets:
            self.issues_by_target[target].append(issue)
        if kind == "dynamic_sql":
            self.dynamic_by_scope[statement.scope].append(issue)
        return issue

    def _procedures(self, unit: Unit) -> None:
        masked = unit.masked
        procs = [m for m in DEFINITION.finditer(masked) if m.group("kind").lower().startswith("proc")]
        stops = sorted([m.start() for m in GO.finditer(masked)] + [m.start() for m in procs] + [len(masked)])
        for match in procs:
            limit = next((s for s in stops if s > match.start()), len(masked))
            end = header_end(masked, match.end(), limit)
            header = unit.text[match.end(): end - 2] if end else ""
            # Parameter defaults cannot reference variables, so every @name in the header is a parameter.
            params = list(dict.fromkeys(m.group().lower() for m in
                                        re.finditer(r"(?<![\w@])@[A-Za-z_][\w$#@]*", mask_noncode(header))))
            ref = self.ref_from_text(match.group("name"), unit, None)
            ref = ObjectRef(ref.name, ref.schema, ref.database, ref.server, "procedure", None, ref.database_source)
            scope = f"{unit.root_label}/{unit.rel_path}::{normalize_name(match.group('name'))}"
            info = ProcInfo(proc_key(ref), ref, scope, unit, params, match.start(), unit.line_at(match.start()))
            self.procedures[info.key].append(info)
            self.proc_by_scope[scope] = info

    def _synonyms(self, unit: Unit) -> None:
        for match in SYNONYM.finditer(unit.masked):
            statement = next((s for s in unit.index.statements if s.start <= match.start() < s.end), None)
            name = self.ref_from_text(match.group("name"), unit, statement)
            target = self.ref_from_text(match.group("target"), unit, statement)
            fake = Statement(unit.path, unit.line_at(match.start()), match.start(), match.end(),
                             unit.text[match.start():match.end()], None, f"{unit.path}::<synonym>", "parsed")
            self.synonyms[name.key] = (target, fake, unit)
            self._register(name)

    def _statement(self, unit: Unit, statement: Statement, order: int) -> None:
        tree = statement.tree
        text = statement.text
        if DYNAMIC.match(text):
            mentioned = tuple(sorted({normalize_name(m.group("target")) for m in WRITE_TARGET.finditer(unit.text[statement.start:statement.end])}))
            self._issue("dynamic_sql", unit, statement, mentioned,
                        "Dynamic SQL is executed here; its statements are not analysed.")
            return
        insert_exec = INSERT_EXEC.match(mask_noncode(text))
        if insert_exec:
            target = self.ref_from_text(insert_exec.group("target"), unit, statement)
            self._issue("insert_exec", unit, statement, (target.name,),
                        "INSERT ... EXEC: rows come from a procedure result set, which is not mapped to columns.")
            self._writer_ref(target, unit, statement)
            return
        if tree is None or isinstance(tree, exp.Command):
            targets = tuple(sorted({split_name(m.group("target"))[-1].lower()
                                    for m in WRITE_TARGET.finditer(mask_noncode(text))}))
            if targets:
                self._issue("parse_error", unit, statement, targets,
                            f"Statement could not be parsed: {(statement.parse_error or 'unsupported command')[:160]}")
            if len(self.fallback_samples) < 5000:
                self.fallback_samples.append((statement, unit))
            return
        if isinstance(tree, exp.Create):
            kind = str(tree.args.get("kind") or "").upper()
            if kind == "TABLE":
                self._create_table(unit, statement, tree)
                return
            if kind == "VIEW":
                table = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
                if isinstance(table, exp.Table):
                    ref = self.ref_from_table(table, unit, statement)
                    ref = ObjectRef(ref.name, ref.schema, ref.database, ref.server, "view", None, ref.database_source)
                    self._register(ref)
                    self._add_writer(Writer(ref, "view", statement, unit, statement.procedure, order))
                return
            return
        if isinstance(tree, exp.Alter):
            self._alter(unit, statement)
            return
        if isinstance(tree, exp.Declare):
            self._declare(unit, statement, tree)
            return
        if isinstance(tree, exp.Set):
            for item in tree.expressions:
                eq = item.this if isinstance(item, exp.SetItem) else item
                if isinstance(eq, exp.EQ) and isinstance(eq.this, exp.Parameter):
                    self._assign(unit, statement, eq.this.sql(dialect="tsql"), eq.expression, None, "set")
            return
        if isinstance(tree, exp.Execute):
            self._call(unit, statement, tree)
            return
        if isinstance(tree, exp.Insert):
            target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
            if isinstance(target, exp.Table) and not isinstance(target.this, exp.Func):
                self._add_writer(Writer(self._writer_ref(self.ref_from_table(target, unit, statement), unit, statement),
                                        "insert", statement, unit, statement.procedure, order))
            return
        if isinstance(tree, exp.Update):
            target = update_target(tree)
            if isinstance(target, exp.Table):
                self._add_writer(Writer(self._writer_ref(self.ref_from_table(target, unit, statement), unit, statement),
                                        "update", statement, unit, statement.procedure, order))
            return
        if isinstance(tree, exp.Merge):
            target = tree.this
            if isinstance(target, exp.Table):
                self._add_writer(Writer(self._writer_ref(self.ref_from_table(target, unit, statement), unit, statement),
                                        "merge", statement, unit, statement.procedure, order))
            return
        if isinstance(tree, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
            first = tree
            while isinstance(first, (exp.Union, exp.Except, exp.Intersect)):
                first = first.this
            into = first.args.get("into") if isinstance(first, exp.Select) else None
            if into is not None and isinstance(into.this, exp.Table):
                ref = self._writer_ref(self.ref_from_table(into.this, unit, statement), unit, statement)
                self._add_writer(Writer(ref, "select_into", statement, unit, statement.procedure, order))
                if ref.kind == "temp_table":
                    self.temp_creators[statement.scope].add(ref.name)
                return
            if isinstance(tree, exp.Select):
                for item in tree.expressions:
                    if isinstance(item, exp.EQ) and isinstance(item.this, exp.Parameter):
                        self._assign(unit, statement, item.this.sql(dialect="tsql"), item.expression, tree, "select")
            return

    def _writer_ref(self, ref: ObjectRef, unit: Unit, statement: Statement) -> ObjectRef:
        # Temp refs keep the writing statement's scope; callers are resolved at query time,
        # once every file (and so every call) is known.
        if ref.kind != "temp_table":
            self._register(ref)
        return ref

    def _add_writer(self, writer: Writer) -> None:
        self.writers[writer.ref.key].append(writer)
        if writer.ref.kind == "temp_table":
            self.temp_writer_scopes[writer.ref.name].add(writer.ref.scope)
        if writer.ref.kind in ("table", "view") and writer.ref.database is None:
            self.unknown_db_writers[(writer.ref.schema, writer.ref.name)].append(writer)

    def _create_table(self, unit: Unit, statement: Statement, tree: exp.Create) -> None:
        schema = tree.this
        if not isinstance(schema, exp.Schema) or not isinstance(schema.this, exp.Table):
            return
        ref = self.ref_from_table(schema.this, unit, statement)
        if ref.kind == "temp_table":
            self.temp_creators[statement.scope].add(ref.name)
        else:
            self._register(ref)
        columns = []
        masked = mask_noncode(statement.text)
        for definition in schema.expressions:
            if not isinstance(definition, exp.ColumnDef):
                continue
            info = column_info(definition, len(columns) + 1)
            if info:
                # SQLGlot reads ROWVERSION/TIMESTAMP as VARBINARY; check the source text.
                if re.search(r"(?:^|[\s,(])\[?" + re.escape(info.name) + r"\]?\s+(?:rowversion|timestamp)\b", masked, re.I):
                    info.rowversion = True
                columns.append(info)
        self.schemas[ref.key].append(TableSchema(ref, columns, statement, unit, "create_table"))

    def _alter(self, unit: Unit, statement: Statement) -> None:
        match = ALTER_ADD.search(statement.text)
        if not match:
            return
        ref = self.ref_from_text(match.group("name"), unit, statement)
        try:
            parsed = sqlglot.parse_one(f"CREATE TABLE x ({match.group('body')})", read="tsql", error_level=ErrorLevel.RAISE)
        except (SqlglotError, ValueError):
            return
        if not isinstance(parsed, exp.Create) or not isinstance(parsed.this, exp.Schema):
            return
        columns = [column_info(d, 0) for d in parsed.this.expressions if isinstance(d, exp.ColumnDef)]
        self.schemas[ref.key].append(TableSchema(ref, [c for c in columns if c], statement, unit, "alter_add"))

    def _declare(self, unit: Unit, statement: Statement, tree: exp.Declare) -> None:
        for item in tree.expressions:
            if not isinstance(item, exp.DeclareItem):
                continue
            names = item.this if isinstance(item.this, list) else [item.this]
            kind = item.args.get("kind")
            for name_node in names:
                if not isinstance(name_node, exp.Parameter):
                    continue
                name = name_node.sql(dialect="tsql")
                if isinstance(kind, exp.Schema):
                    ref = ObjectRef(norm(name), kind="table_variable", scope=statement.scope)
                    columns = [column_info(d, 0) for d in kind.expressions if isinstance(d, exp.ColumnDef)]
                    columns = [c for c in columns if c]
                    for i, c in enumerate(columns, 1):
                        c.ordinal = i
                    self.schemas[ref.key].append(TableSchema(ref, columns, statement, unit, "table_variable"))
                    continue
                default = item.args.get("default")
                if isinstance(default, exp.Expression):
                    self._assign(unit, statement, name, default, None, "declare")

    def _assign(self, unit, statement, name, value, select, kind) -> None:
        name = name.lower()
        if name.startswith("@@"):
            return
        self.assignments[(statement.scope, name)].append(Assignment(name, statement.scope, statement, unit, value, select, kind))

    def _call(self, unit: Unit, statement: Statement, tree: exp.Execute) -> None:
        target = tree.this
        if not isinstance(target, exp.Table):
            return
        callee = self.ref_from_table(target, unit, statement)
        callee = ObjectRef(callee.name, callee.schema, callee.database, callee.server, "procedure", None, callee.database_source)
        args: list[tuple[str | None, exp.Expression | None]] = []
        for arg in tree.expressions:
            output = False
            if isinstance(arg, exp.Alias) and str(arg.alias).upper() in ("OUTPUT", "OUT"):
                arg, output = arg.this, True
            if isinstance(arg, exp.EQ) and isinstance(arg.this, exp.Parameter):
                args.append((arg.this.sql(dialect="tsql").lower(), arg.expression))
                if output and isinstance(arg.expression, exp.Parameter):
                    self._assign(unit, statement, arg.expression.sql(dialect="tsql"), None, None, "exec_output")
            else:
                args.append((None, arg))
                if output and isinstance(arg, exp.Parameter):
                    self._assign(unit, statement, arg.sql(dialect="tsql"), None, None, "exec_output")
        call = Call(statement.scope, callee, statement, unit, args)
        self.calls_by_callee[proc_key(callee)].append(call)
        self.calls_by_caller[statement.scope].append(call)

    # ---------------------------------------------------------------- queries
    def writers_for(self, ref: ObjectRef) -> list[Writer]:
        """Writers of an object; for a #temp table, includes callees writing into it."""
        found = list(self.writers.get(ref.key, []))
        if ref.kind == "temp_table":
            for scope in sorted(self.temp_writer_scopes.get(ref.name, ())):
                if scope == ref.scope or ref.name in self.temp_creators.get(scope, ()):
                    continue
                if ref.scope in self.temp_scopes(ref.name, scope):
                    found.extend(self.writers.get(ObjectRef(ref.name, kind="temp_table", scope=scope).key, []))
        return found

    def procs_for(self, callee: ObjectRef) -> list[ProcInfo]:
        found = self.procedures.get(proc_key(callee))
        if found:
            return found
        if callee.database is None:
            matches = [p for key, procs in self.procedures.items() for p in procs
                       if p.ref.schema == callee.schema and p.ref.name == callee.name]
            return matches
        return []

    def schema_for(self, ref: ObjectRef) -> tuple[list[ColumnInfo] | None, str, TableSchema | None]:
        """Declared ordered columns. Status: declared, ambiguous, missing."""
        schemas = self.schemas.get(ref.key, [])
        creates = [s for s in schemas if s.source in ("create_table", "table_variable")]
        if ref.kind == "temp_table":
            creates = [s for s in creates if s.statement.scope == ref.scope]
        if not creates:
            return None, "missing", None
        distinct = {tuple((c.name, c.identity, c.computed, c.rowversion) for c in s.columns) for s in creates}
        if len(distinct) > 1:
            return None, "ambiguous", creates[0]
        columns = [ColumnInfo(c.name, c.ordinal, c.identity, c.computed, c.rowversion, c.default) for c in creates[0].columns]
        for extra in [s for s in schemas if s.source == "alter_add"]:
            for c in extra.columns:
                if all(x.name != c.name for x in columns):
                    columns.append(ColumnInfo(c.name, len(columns) + 1, c.identity, c.computed, c.rowversion, c.default))
        return columns, "declared", creates[0]

    def is_known(self, ref: ObjectRef) -> bool:
        return bool(self.schemas.get(ref.key) or self.writers.get(ref.key) or ref.key in self.synonyms)


def proc_key(ref: ObjectRef) -> str:
    return ".".join(p for p in (ref.server, ref.database or "?", ref.schema or "?", ref.name) if p is not None)


def statement_id(unit: Unit, statement: Statement) -> str:
    raw = f"{unit.root_label}|{unit.rel_path}|{statement.start}|{statement.end}"
    return "stmt-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def update_target(tree: exp.Update) -> exp.Expression | None:
    """The table an UPDATE writes, following a FROM alias when the target is one."""
    target = tree.this
    if not isinstance(target, exp.Table):
        return None
    _, database, schema, name = table_parts(target)
    if database or schema:
        return target
    sources = []
    from_ = tree.args.get("from_")
    if from_ is not None and from_.this is not None:
        sources.append(from_.this)
    sources.extend(j.this for j in tree.args.get("joins") or [])
    if from_ is not None:
        sources.extend(j.this for j in from_.args.get("joins") or [])
    matches = [s for s in sources if isinstance(s, exp.Table) and s.alias and s.alias.lower() == (name or "").lower()]
    return matches[0] if len(matches) == 1 else target


def column_info(definition: exp.ColumnDef, ordinal: int) -> ColumnInfo | None:
    name = ident_text(definition.this)
    if not name:
        return None
    identity = False
    computed = None
    default = None
    for constraint in definition.args.get("constraints") or []:
        kind = constraint.args.get("kind") if isinstance(constraint, exp.ColumnConstraint) else constraint
        if isinstance(kind, exp.GeneratedAsIdentityColumnConstraint):
            identity = True
        elif isinstance(kind, exp.ComputedColumnConstraint):
            computed = kind.this.sql(dialect="tsql") if kind.this is not None else "computed"
        elif isinstance(kind, exp.DefaultColumnConstraint):
            default = kind.this.sql(dialect="tsql") if kind.this is not None else None
    data_type = definition.args.get("kind")
    type_name = data_type.sql(dialect="tsql").upper() if data_type is not None else ""
    if data_type is None and computed is None:
        # `col AS (expr)` without a type is a computed column in T-SQL.
        computed = None
    rowversion = type_name.startswith(("ROWVERSION", "TIMESTAMP"))
    return ColumnInfo(name.lower(), ordinal, identity, computed, rowversion, default)
