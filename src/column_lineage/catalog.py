"""One pass over the scanned SQL that indexes objects, schemas, writers, calls and variables.

The catalog never decides lineage. It records what each statement explicitly
says, plus the context needed to bind names later: database, scope, control flow.
"""
from __future__ import annotations

from collections import defaultdict
import logging
from dataclasses import dataclass, field, replace
import hashlib
from pathlib import Path
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from table_lineage.index import DEFINITION, GO, IDENT, SourceIndex, Statement, mask_noncode
from table_lineage.models import normalize_name
from table_lineage.sources import SourceFile, decode_source

from .control import control_contexts, header_end
from .model import Evidence, ObjectRef

USE = re.compile(r"^[ \t]*USE[ \t]+(?P<db>\[(?:[^\]]|\]\])*\]|\"[^\"]+\"|[A-Za-z_][\w$#@]*)", re.I | re.M)
DYNAMIC = re.compile(r"\s*EXEC(?:UTE)?\s*(?:\(|(?:\[?sys\]?\s*\.\s*)?\[?sp_executesql\]?(?!\w)|@\w+\s*;?\s*$|@\w+\s+(?!=))", re.I)
INSERT_EXEC = re.compile(rf"\s*INSERT\s+(?:INTO\s+)?(?P<target>{IDENT})(?:\s*\([^)]*\))?\s+EXEC(?:UTE)?\b", re.I)
TOP = r"(?:TOP\s*\([^)]*\)\s*(?:PERCENT\s+)?)?"
WRITE_TARGET = re.compile(
    rf"\b(?:INSERT\s+{TOP}(?:INTO\s+)?|UPDATE\s+{TOP}|MERGE\s+{TOP}(?:INTO\s+)?|\bINTO\s+)(?P<target>{IDENT})", re.I)
SYNONYM = re.compile(rf"\bCREATE\s+SYNONYM\s+(?P<name>{IDENT})\s+FOR\s+(?P<target>{IDENT})", re.I)
ALTER_ADD = re.compile(rf"\bALTER\s+TABLE\s+(?P<name>{IDENT})\s+ADD\s+(?P<body>[^;]*)", re.I)
TEMP_MARK = "#"
COMPOUND = re.compile(r"(?P<lhs>@[\w$#@]+|(?:\[[^\]]+\]|[A-Za-z_][\w$#]*)(?:\.(?:\[[^\]]+\]|[A-Za-z_][\w$#]*))*)"
                      r"\s*(?P<op>[-+*/%&|^])=(?!=)")
CURSOR = re.compile(r"\bDECLARE\s+(?P<cursor>[A-Za-z_][\w$#@]*)\s+(?:INSENSITIVE\s+|SCROLL\s+)*CURSOR\b[^;]*?"
                    r"(?P<for>\bFOR\s+)(?=SELECT\b|WITH\b|\()", re.I | re.S)
FETCH = re.compile(r"\bFETCH\s+(?:(?:NEXT|PRIOR|FIRST|LAST|ABSOLUTE\s+\S+|RELATIVE\s+\S+)\s+)?(?:FROM\s+)?(?:GLOBAL\s+)?"
                   r"(?P<cursor>[A-Za-z_][\w$#@]*)\s+INTO\s+(?P<vars>@[\w$#@]+(?:\s*,\s*@[\w$#@]+)*)", re.I)
ALTER_VIEW = re.compile(rf"\bALTER\s+VIEW\s+{IDENT}\s*(?:\([^)]*\)\s*)?(?:WITH\s+[\w\s,]+?\s+)?AS\b", re.I)
DML_TOP = re.compile(r"(?:^|(?<=[\s;]))(?:UPDATE|DELETE|INSERT)\s+(?P<top>TOP\s*\([^)]*\)\s*(?:PERCENT\b)?)", re.I)
INSERT_HINT = re.compile(rf"\bINSERT\s+(?:INTO\s+)?{IDENT}\s*(?P<hint>WITH\s*\(\s*[A-Za-z_]+(?:\s*,\s*[A-Za-z_]+)*\s*\))", re.I)
OUTPUT_CLAUSE = re.compile(rf"(?<![.\w\[\]])OUTPUT\s+(?=INSERTED\b|DELETED\b|\$ACTION\b|@)(?:(?!\bOUTPUT\b).)*?(?:\bINTO\s+(?P<target>{IDENT})(?:\s*\([^()]*\))?)?"
                           r"(?=\s*(?:SELECT|VALUES|FROM|WHERE|DEFAULT|;|$))", re.I | re.S)
INSERT_COLUMNS = re.compile(rf"\bINSERT\s+(?:INTO\s+)?(?:{IDENT}|@[\w$#@]+)\s*\((?P<cols>[^()]*)\)", re.I)
KEY_CLUSTERING = re.compile(r"\b(?:PRIMARY\s+KEY|UNIQUE)\s+(?P<kind>(?:NON)?CLUSTERED)\b", re.I)
SPACED_OPERATOR = re.compile(r"([!<>])[ \t]+([=>])|(?<=[\w\]])(\s*[-+*/%&|^])[ \t]+(=)(?!=)")
OPTION_HINT = re.compile(r"(?P<option>\bOPTION\s*\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\))\s*;?\s*$", re.I)
ALIAS_DEF = re.compile(rf"\b(?:FROM|JOIN|UPDATE|MERGE(?:\s+INTO)?)\s+(?P<name>{IDENT})\s+(?:WITH\s*\([^)]*\)\s*)?(?:AS\s+)?"
                       r"(?P<alias>\[[^\]]+\]|[A-Za-z_][\w$#]*)", re.I)
NON_LINEAGE = re.compile(
    r"\s*(?:RAISERROR\b|RETURN\b|PRINT\b|THROW\b|REVERT\b|(?:WITH\s+)?EXEC(?:UTE)?\s+AS\b|"
    # Cursor verbs and DROP are read separately from the source text; WHILE here is a condition fragment.
    r"DECLARE\s+[A-Za-z_][\w$#@]*\s+(?:INSENSITIVE\s+|SCROLL\s+)*CURSOR\b|(?:OPEN|FETCH|CLOSE|DEALLOCATE|DROP|WHILE)\b|"
    r"ALTER\s+TABLE\s+\S+\s+(?:ADD\s+(?:CONSTRAINT|PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK|INDEX|DEFAULT)|"
    r"DROP\s+CONSTRAINT|(?:WITH\s+)?(?:NO)?CHECK|REBUILD|SET\s*\()|"
    # Index options split off a CREATE INDEX / constraint: WITH (PAD_INDEX = OFF, ...) ON [PRIMARY]
    r"WITH\s*\(\s*[A-Za-z_]+\s*=\s*(?:ON|OFF|\d+)\b|"
    # ... and a filegroup clause on its own: ON [PRIMARY] [TEXTIMAGE_ON [PRIMARY]]
    r"ON\s+(?:\[[^\]]+\]|[A-Za-z_]\w*)(?:\s*\([^()]*\))?(?:\s+TEXTIMAGE_ON\s+(?:\[[^\]]+\]|[A-Za-z_]\w*))?\s*;?\s*$|"
    r"SET\s+(?:NOCOUNT|XACT_ABORT|ANSI_\w+|QUOTED_IDENTIFIER|ARITHABORT|CONCAT_NULL_YIELDS_NULL|NUMERIC_ROUNDABORT|"
    r"TRANSACTION|IDENTITY_INSERT|DATEFORMAT|DATEFIRST|LANGUAGE|LOCK_TIMEOUT|DEADLOCK_PRIORITY|ROWCOUNT|STATISTICS|"
    r"FMTONLY|NOEXEC|IMPLICIT_TRANSACTIONS|CURSOR_CLOSE_ON_COMMIT|TEXTSIZE|CONTEXT_INFO)\b)", re.I)
GLUE = re.compile(r"(?im)^[ \t]*(?P<word>TRUNCATE|DECLARE|SET|EXEC|EXECUTE|WHILE|IF|PRINT|DROP|CREATE|RAISERROR|RETURN|"
                  r"BEGIN|END|INSERT|UPDATE|DELETE|MERGE|SELECT|WITH|OPEN|FETCH|CLOSE|DEALLOCATE|COMMIT|ROLLBACK|THROW)\b")
DECLARE_ANYWHERE = re.compile(r"(?<![\w@#$])DECLARE\b", re.I)
LITERAL_WRITE_TARGET = re.compile(
    rf"\b(?:INSERT\s+{TOP}(?:INTO\s+)?|UPDATE\s+{TOP}|MERGE\s+{TOP}(?:INTO\s+)?|SELECT\b[^;]*?\bINTO\s+)(?P<target>{IDENT})", re.I)
SWITCH = re.compile(rf"\bALTER\s+TABLE\s+(?P<source>{IDENT})\s+SWITCH\s+(?:PARTITION\s+\S+\s+)?TO\s+(?P<target>{IDENT})", re.I)
ALTER_DEFAULT = re.compile(rf"\bALTER\s+TABLE\s+(?P<name>{IDENT})\s+(?:WITH\s+(?:NO)?CHECK\s+)?ADD\s+(?:CONSTRAINT\s+\S+\s+)?"
                           r"DEFAULT\s*(?P<expr>\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)|N?'(?:''|[^'])*'|[-\w.]+)\s+FOR\s+"
                           r"(?P<col>\[[^\]]+\]|[A-Za-z_][\w$#@]*)", re.I)
# Index options left over from a CREATE INDEX / constraint the indexer did not keep, glued to the next statement.
INDEX_OPTIONS_PREFIX = re.compile(r"\s*WITH\s*\(\s*[A-Za-z_]+\s*=\s*(?:ON|OFF|\d+)\b(?:[^()]|\([^()]*\))*\)\s*"
                                  r"(?:ON\s+(?:\[[^\]]+\]|[A-Za-z_]\w*)(?:\s*\([^()]*\))?\s*)?(?=\S)", re.I)
EXEC_AS_HEADER = re.compile(r"\s*WITH\s+EXEC(?:UTE)?\s+AS\s+(?:N?'[^']*'|[A-Za-z_]\w*)\s*(?:,\s*[A-Za-z_]\w*\s*)*AS\b", re.I)
DROP_TEMP = re.compile(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?P<name>#[A-Za-z_][\w$#@]*)", re.I)


def prepare_in_worker(job):
    """Process-pool entry point: parse and repair one file with a scratch catalog."""
    path, text, root, root_label, settings = job
    # Workers do not inherit the parent's logging setup (spawn on Windows), and SQLGlot's fallback
    # warnings quote raw SQL: silence them here too.
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    scratch = Catalog(*settings)
    source = SourceFile(Path(path), text)
    try:
        prepared = scratch.prepare(source, Path(root), root_label)
    except Exception as exc:  # noqa: BLE001
        prepared = {"index": SourceIndex(), "repaired": 0, "rewritten": [], "outputs": [],
                    "errors": [(root_label, scratch._rel(source, Path(root)), f"{type(exc).__name__}: {str(exc)[:200]}")]}
    prepared["foreign"] = True
    return prepared


def merge_split_inserts(statements: list[Statement], text: str, masked: str) -> list[Statement]:
    """Rejoin an INSERT the indexer cut off from the SELECT/VALUES/WITH that follows it."""
    out: list[Statement] = []
    i = 0
    while i < len(statements):
        current = statements[i]
        tree = current.tree
        if (i + 1 < len(statements) and isinstance(tree, exp.Insert) and tree.expression is None
                and re.match(r"\s*(?:SELECT|VALUES|WITH|\()", statements[i + 1].text, re.I)
                and not masked[current.end:statements[i + 1].start].strip()):
            nxt = statements[i + 1]
            joined = text[current.start:nxt.end]
            merged = parse_or_none(joined)
            out.append(replace(current, end=nxt.end, text=joined, tree=merged,
                               parse_status="parsed" if merged is not None else "fallback",
                               parse_error=None if merged is not None else "statement could not be parsed"))
            i += 2
            continue
        out.append(current)
        i += 1
    return out


STATEMENT_KEYWORD = re.compile(r"(?<![\w@#$.])(?:INSERT|UPDATE|DELETE|MERGE|SELECT|EXEC(?:UTE)?|SET|DECLARE|WITH|"
                               r"TRUNCATE)\b", re.I)


def expression_end(masked: str, start: int, text: str | None = None) -> int:
    """End of the expression starting at ``start``: a top-level comma, clause keyword or the end.

    Trailing whitespace is trimmed on ``text`` (the unmasked SQL) when given: in the masked text a
    final string literal is blank too."""
    text = masked if text is None else text
    depth = 0
    i = start
    while i < len(masked):
        char = masked[i]
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return i
            depth -= 1
        elif depth == 0 and (char in ",;" or re.match(r"(?i)(?<![\w@#$])(?:FROM|WHERE|OUTPUT|OPTION)\b", masked[i:i + 7])
                             and (i == 0 or not (masked[i - 1].isalnum() or masked[i - 1] in "_@#$"))):
            end = i
            while end > start and text[end - 1].isspace():
                end -= 1
            return end
        i += 1
    end = len(masked)
    while end > start and text[end - 1].isspace():
        end -= 1
    return end


def unmatched_close(masked: str) -> int | None:
    """Offset of the first ')' with no matching '(' before it."""
    depth = 0
    for i, char in enumerate(masked):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return i
    return None


def depth_at(masked: str, start: int, end: int) -> int:
    segment = masked[start:end]
    return segment.count("(") - segment.count(")")


def non_lineage(text: str) -> bool:
    """Statements that never carry column lineage, or a condition fragment like `... ) ) OR (...`
    split off an IF/WHILE by the indexer (more closing than opening parentheses).

    Never true for text that names a write target: a writer glued after a non-lineage statement
    must stay visible (traced, or at worst reported as a parse error), never silently dropped."""
    masked = mask_noncode(text)
    if write_targets(masked):
        return False
    if NON_LINEAGE.match(text) or NON_LINEAGE.match(re.sub(r"--[^\n]*|/\*.*?\*/", " ", text, flags=re.S)):
        return True
    return masked.count(")") > masked.count("(")


def parse_or_none(text: str):
    try:
        tree = sqlglot.parse_one(text, read="tsql", error_level=ErrorLevel.RAISE)
    except (SqlglotError, ValueError, RecursionError, AttributeError, TypeError):
        return None
    if tree is None or isinstance(tree, exp.Command):
        return None
    # SQLGlot accepts some statements cut short, e.g. an INSERT with no source.
    if isinstance(tree, exp.Insert) and tree.expression is None:
        return None
    if isinstance(tree, exp.Update) and not tree.expressions:
        return None
    return tree


def glued_boundaries(masked: str, limit: int = 60) -> list[int]:
    """Offsets of line-start statement keywords (and DECLARE anywhere) at parenthesis depth 0,
    outside CASE ... END."""
    cuts = []
    depth = 0
    case = 0
    position = 0
    starts = sorted({m.start(): m for m in list(GLUE.finditer(masked)) + list(DECLARE_ANYWHERE.finditer(masked))}.items())
    for _, match in starts:
        segment = masked[position:match.start()]
        depth += segment.count("(") - segment.count(")")
        case += len(re.findall(r"\bCASE\b", segment, re.I)) - len(re.findall(r"\bEND\b", segment, re.I))
        case = max(case, 0)
        position = match.start()
        if match.start() > 0 and depth <= 0 and case == 0 and masked[:match.start()].strip():
            cuts.append(match.start())
        if len(cuts) >= limit:
            break
    return cuts


EXECUTED_VARIABLE = re.compile(
    r"\s*EXEC(?:UTE)?\s*(?:\(\s*(@[\w$#@]+)\s*\)|(?:\[?sys\]?\s*\.\s*)?\[?sp_executesql\]?\s+(@[\w$#@]+)\s*(?:;|$)|"
    r"(@[\w$#@]+)\s*(?:;|$))", re.I)


def empty_literal(value) -> bool:
    """An empty string or NULL literal (N'' included)."""
    if isinstance(value, exp.Null):
        return True
    if isinstance(value, exp.National) or (isinstance(value, exp.Literal) and value.is_string):
        return (value.this or "") == ""
    return False


# Reserved words, which cannot be unquoted object names: matched only as keywords (UPDATE STATISTICS x).
NOT_TARGETS = {"set", "select", "from", "where", "values", "top", "into", "table", "statistics", "with"}


def write_targets(text: str, literal: bool = False) -> tuple[str, ...]:
    """Object names a SQL text writes, resolving `UPDATE alias ... FROM table AS alias`.

    An alias defined more than once (e.g. again inside a subquery) maps to every table it names.
    With ``literal`` the text is string-literal content: a bare `INTO x` only counts after
    INSERT or SELECT, so prose such as 'copying rows into the archive' names no target."""
    stop = {"WITH", "ON", "WHERE", "SET", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "JOIN", "USING", "WHEN", "OUTER",
            "GROUP", "ORDER", "UNION", "AS", "OUTPUT", "VALUES", "SELECT"}
    aliases: dict[str, set[str]] = {}
    for match in ALIAS_DEF.finditer(text):
        alias = split_name(match.group("alias"))[-1].lower()
        if alias.upper() not in stop:
            aliases.setdefault(alias, set()).add(split_name(match.group("name"))[-1].lower())
    names = set()
    for match in (LITERAL_WRITE_TARGET if literal else WRITE_TARGET).finditer(text):
        name = split_name(match.group("target"))[-1].lower()
        if name in NOT_TARGETS:
            continue
        names |= aliases.get(name, {name})
    return tuple(sorted(names - NOT_TARGETS))


def procedure_scopes(text: str, masked: str) -> list[tuple[int, str, str]]:
    """(start, normalized name, scope name) for each procedure; repeated names get @line."""
    found = [(m.start(), normalize_name(m.group("name"))) for m in DEFINITION.finditer(masked)
             if m.group("kind").lower().startswith("proc")]
    counts: dict[str, int] = defaultdict(int)
    for _, name in found:
        counts[name] += 1
    return [(start, name, name if counts[name] == 1 else f"{name}@{text.count(chr(10), 0, start) + 1}")
            for start, name in found]


def parameter_defaults(header: str, statement: Statement) -> dict:
    """@param -> (default expression, header statement) from a CREATE PROCEDURE header."""
    masked = mask_noncode(header)
    defaults = {}
    params = list(re.finditer(r"(?<![\w@])@[A-Za-z_][\w$#@]*", masked))
    for i, param in enumerate(params):
        end = params[i + 1].start() if i + 1 < len(params) else len(masked)
        segment = masked[param.end():end]
        eq = segment.find("=")
        if eq < 0:
            continue
        start = param.end() + eq + 1
        stop = param.end() + len(segment.rstrip().rstrip(",").rstrip())
        # A string literal is blank in the masked text: a default that ends with one ends at its closing quote.
        stop = literal_end(header, stop, end)
        tail = re.search(r"\b(?:OUTPUT|OUT|READONLY)\b|\bAS\s*$|\)\s*(?:AS\s*)?$|,\s*$", masked[start:stop], re.I)
        value_end = start + tail.start() if tail else stop
        value = header[start:value_end].strip().rstrip(",")
        if not value:
            continue
        try:
            parsed = sqlglot.parse_one(f"SELECT {value}", read="tsql", error_level=ErrorLevel.RAISE)
        except (SqlglotError, ValueError, RecursionError):
            continue
        if isinstance(parsed, exp.Select) and parsed.expressions:
            defaults[param.group().lower()] = (parsed.expressions[0], statement)
    return defaults


def literal_end(text: str, at: int, limit: int) -> int:
    """If a string literal starts at ``at`` (after spaces), the offset after its closing quote; else ``at``."""
    i = at
    while i < limit and text[i] in " \t":
        i += 1
    if i >= limit or text[i] != "'":
        return at
    i += 1
    while i < limit:
        if text[i] == "'":
            if i + 1 < limit and text[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return at


def ident_text(node) -> str | None:
    if node is None:
        return None
    if isinstance(node, exp.Identifier):
        name = node.this
        return ("#" + name) if node.args.get("temporary") and not name.startswith("#") else name
    if isinstance(node, exp.Parameter):
        return restore_variable(node.sql(dialect="tsql", comments=False))
    if isinstance(node, exp.Expression):
        return node.name or node.sql(dialect="tsql", comments=False)
    return str(node)


DIGIT_VARIABLE = re.compile(r"(?<![\w@])@(?=\d)")
DIGIT_PLACEHOLDER = "@__d"


def restore_variable(name: str) -> str:
    """Undo the placeholder given to variables whose names start with a digit (@2_x)."""
    return re.sub(r"@__d(?=\d)", "@", name, flags=re.I) if DIGIT_PLACEHOLDER in name.lower() else name


def table_parts(table: exp.Table) -> tuple[str | None, str | None, str | None, str]:
    """(server, database, schema, name) as written, not normalized."""
    this = table.this
    if isinstance(this, exp.Dot):
        return (ident_text(table.args.get("catalog")), ident_text(table.args.get("db")),
                ident_text(this.this), ident_text(this.expression))
    name = ident_text(this)
    return None, ident_text(table.args.get("catalog")), ident_text(table.args.get("db")), name


def split_name(text: str) -> list[str]:
    # `db..name` (default schema) keeps an empty schema part.
    parts = re.findall(r'\[(?:[^\]]|\]\])*\]|"(?:[^"]|"")*"|[^.\s]+|(?<=\.)(?=\.)', text.strip())
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
    defaults: dict = field(default_factory=dict)   # @param -> (expression, header Statement)


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
    via: tuple | None = None   # ("cte", name) or ("view", view key): written through an updatable CTE/view

    @property
    def controls(self) -> tuple[str, ...]:
        return controls_of(self.unit, self.statement)


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
    select: exp.Expression | None   # SELECT @v = col FROM ... / UPDATE ... SET @v = col context
    kind: str          # set, select, declare, fetch, update, exec_output
    note: str | None = None


@dataclass
class StatementIssue:
    kind: str          # dynamic_sql, parse_error, insert_exec, unsupported_statement
    statement: Statement
    unit: Unit
    targets: tuple[str, ...]   # normalized names mentioned as write targets, when known
    message: str


class Catalog:
    def __init__(self, default_database: str | None = None, default_schema: str = "dbo",
                 database_map: list[tuple[str, str]] | None = None, local_servers: tuple[str, ...] = ()):
        self.local_servers = {norm(s) for s in local_servers}
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
        self.stats = {"files": 0, "statements": 0, "parsed": 0, "fallback": 0, "ignored": 0, "repaired": 0,
                      "files_without_statements": 0}
        self.fallback_samples: list[tuple[Statement, Unit]] = []
        self.file_errors: list[tuple[str, str, str]] = []
        self.rewritten: set[tuple[str, int]] = set()
        self.pending_outputs: list[tuple[Statement, str]] = []   # OUTPUT ... INTO targets blanked by _repair       # statements parsed after a compound-assignment rewrite
        self.temp_resets: dict[tuple[str, str], list[int]] = defaultdict(list)  # (scope, #name) -> DROP/CREATE offsets
        self.fallback_by_scope: dict[str, list[Statement]] = defaultdict(list)
        self._view_bases: dict[str, list[str]] | None = None
        self._dead_dynamic: dict[int, bool] = {}
        self.column_defaults: dict[str, dict[str, str]] = defaultdict(dict)   # ALTER TABLE ... DEFAULT ... FOR col
        self.top_limited: set[tuple[str, int]] = set()                        # statements whose TOP (n) was blanked
        self._dynamic_args: dict[str, list] | None = None
        self._evaluations: dict[int, object] | None = None   # id(issue) -> dynamic_eval.Evaluation
        self.skipped_files: list[tuple[str, str]] = []   # (label, path) of files the filters skipped
        self._write_mentions: dict | None = None
        self._functions: dict | None = None
        self._writers_by_statement: dict | None = None

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
        self.add_prepared(source, root, root_label, self.prepare(source, root, root_label))

    def prepare(self, source: SourceFile, root: Path, root_label: str) -> dict:
        """Per-file parsing and repair. Independent of other files, so it can run in a worker process."""
        before = (self.stats["repaired"], len(self.rewritten), len(self.pending_outputs), len(self.file_errors))
        index = self._parse_file(source, root, root_label)
        return {"index": index, "repaired": self.stats["repaired"] - before[0],
                "rewritten": [r for r in self.rewritten if r[0] == f"{root_label}/{self._rel(source, root)}"],
                "outputs": self.pending_outputs[before[2]:], "errors": self.file_errors[before[3]:]}

    @staticmethod
    def _rel(source: SourceFile, root: Path) -> str:
        return source.path.name if root.is_file() else source.path.relative_to(root).as_posix()

    def add_prepared(self, source: SourceFile, root: Path, root_label: str, prepared: dict) -> None:
        """Index a prepared file into the catalog (sequential, in file order)."""
        index = prepared["index"]
        rel = self._rel(source, root)
        if prepared.get("foreign"):
            # Prepared in a worker: merge its side effects.
            self.stats["repaired"] += prepared["repaired"]
            self.rewritten.update(prepared["rewritten"])
            self.pending_outputs.extend(prepared["outputs"])
            self.file_errors.extend(prepared["errors"])
        text = source.text
        masked = mask_noncode(text)
        self._ingest(source, root, root_label, rel, index, text, masked)

    def add_embedded(self, source: SourceFile, root: Path, root_label: str, rel: str, adjust=None) -> Unit:
        """Index SQL embedded in a non-SQL file (an SSIS Execute SQL task) under that file's root-relative
        path ``rel``, so evidence cites the file. ``adjust`` may rewrite each statement (scope, label).
        Not counted in ``stats["files"]``."""
        index = self._parse_file(source, root, root_label)
        if adjust is not None:
            index.statements = [adjust(s) for s in index.statements]
        self._ingest(source, root, root_label, rel, index, source.text, mask_noncode(source.text))
        self.stats["files"] -= 1
        return self.units[-1]

    def _parse_file(self, source: SourceFile, root: Path, root_label: str) -> SourceIndex:
        index = SourceIndex()
        rel = self._rel(source, root)
        try:
            index.add(source, include_ddl=True)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the scan
            self.file_errors.append((root_label, rel, f"{type(exc).__name__}: {str(exc)[:200]}"))
            index = SourceIndex()
        # Scopes embed the absolute path; make them portable so identities are stable across machines.
        prefix = f"{source.path}::"
        portable = f"{root_label}/{rel}::"
        text = source.text
        masked = mask_noncode(text)
        proc_scopes = procedure_scopes(text, masked)
        statements = []
        for statement in merge_split_inserts(index.statements, text, masked):
            if statement.scope.startswith(prefix):
                name = statement.scope[len(prefix):]
                if not name.startswith("<batch@"):
                    owner = [p for p in proc_scopes if p[0] <= statement.start]
                    name = owner[-1][2] if owner else name
                statement = replace(statement, scope=portable + name)
            if statement.parse_status != "parsed":
                statement = self._after_condition(statement)
                statements.extend(self._repair(root_label, rel, statement))
                continue
            statements.append(statement)
        index.statements = statements
        return index

    def _ingest(self, source, root, root_label, rel, index, text, masked) -> None:
        uses = [(m.start(), norm(split_name(m.group("db"))[0])) for m in USE.finditer(masked)]
        unit = Unit(root_label, root, rel, str(source.path), text, masked, index, uses,
                    self._mapped_database(root, rel, str(source.path)))
        try:
            unit.controls = control_contexts(text, masked, index.statements)
        except Exception as exc:  # noqa: BLE001 - control context is optional
            self.file_errors.append((root_label, rel, f"control flow: {type(exc).__name__}"))
        self.units.append(unit)
        self.units_by_path[unit.path] = unit
        self.stats["files"] += 1
        if not index.statements and masked.strip():
            self.stats["files_without_statements"] += 1
        for step in (self._procedures, self._synonyms):
            try:
                step(unit)
            except Exception as exc:  # noqa: BLE001
                self.file_errors.append((root_label, rel, f"{step.__name__}: {type(exc).__name__}"))
        for order, statement in enumerate(index.statements):
            self.stats["statements"] += 1
            if statement.parse_status != "parsed" and non_lineage(statement.text):
                self._alter_default(unit, statement)
                self.stats["ignored"] += 1
                continue
            self.stats["parsed" if statement.parse_status == "parsed" else "fallback"] += 1
            if statement.parse_status != "parsed":
                self.fallback_by_scope[statement.scope].append(statement)
            try:
                self._statement(unit, statement, order)
            except Exception as exc:  # noqa: BLE001 - one bad statement must not stop the scan
                self._issue("unsupported_statement", unit, statement, (), f"Statement could not be indexed: {type(exc).__name__}")
        for statement, name in self.pending_outputs:
            self._issue("unsupported_statement", unit, statement, (name,),
                        "OUTPUT ... INTO writes rows to this object; the OUTPUT column mapping is not analysed.")
        self.pending_outputs = []
        for step in (self._cursors, self._temp_drops, self._alter_views):
            try:
                step(unit)
            except Exception as exc:  # noqa: BLE001
                self.file_errors.append((root_label, rel, f"{step.__name__}: {type(exc).__name__}"))

    # ------------------------------------------------------------ add_file helpers
    def _after_condition(self, statement: Statement) -> Statement:
        """`IF [NOT] EXISTS (SELECT ...) INSERT ...` on one line reaches us as `SELECT ...) INSERT ...`:
        the indexer starts at the SELECT inside the condition. Drop the condition's tail and keep the
        statement that follows it, which may be a writer."""
        masked = mask_noncode(statement.text)
        close = unmatched_close(masked)
        if close is None:
            return statement
        follow = STATEMENT_KEYWORD.search(masked, close + 1)
        if follow is None or depth_at(masked, close + 1, follow.start()) != 0:
            return statement
        offset = follow.start()
        text = statement.text[offset:]
        tree = parse_or_none(text)
        return replace(statement, start=statement.start + offset, text=text,
                       line=statement.line + statement.text.count("\n", 0, offset), tree=tree,
                       parse_status="parsed" if tree is not None else statement.parse_status,
                       parse_error=None if tree is not None else statement.parse_error)

    def _repair(self, root_label: str, rel: str, statement: Statement) -> list[Statement]:
        """Recover statements SQLGlot rejects, without changing what they mean for lineage.

        1. Blank out syntax with no column lineage, keeping offsets: INSERT target table hints,
           OUTPUT clauses (their targets are reported as gaps), TOP (n) on UPDATE/DELETE/INSERT.
        2. Rewrite compound assignment `x += y` as `x = x + y` (offsets change: evidence then
           uses whole-statement lines).
        3. If it still fails, the indexer may have glued several statements together (a missing
           semicolon before TRUNCATE, WHILE, SET NOCOUNT ...): split at line-start statement
           keywords and parse each piece on its own.
        """
        for prefix in (EXEC_AS_HEADER, INDEX_OPTIONS_PREFIX):
            header = prefix.match(mask_noncode(statement.text))
            if not header:
                continue
            offset = header.end()
            body = statement.text[offset:]
            if not body.strip():
                break   # a header on its own (the body was indexed separately): non-lineage as it is
            tree = parse_or_none(body)
            statement = replace(statement, start=statement.start + offset, text=body,
                                line=statement.line + statement.text.count("\n", 0, offset), tree=tree,
                                parse_status="parsed" if tree is not None else statement.parse_status)
            if tree is not None:
                return [statement]
            break
        if non_lineage(statement.text) or INSERT_EXEC.match(mask_noncode(statement.text)):
            return [statement]
        repaired = self._reparse(root_label, rel, statement)
        if repaired is not None:
            return [repaired]
        pieces = self._split_glued(root_label, rel, statement)
        return pieces or [statement]

    def _blank(self, text: str) -> tuple[str, list[str]]:
        masked = mask_noncode(text)
        outputs = []
        spans = []
        for pattern, group in ((INSERT_HINT, "hint"), (DML_TOP, "top"), (OPTION_HINT, "option"), (KEY_CLUSTERING, "kind")):
            for match in pattern.finditer(masked):
                spans.append(match.span(group))
        target_list = INSERT_COLUMNS.search(masked)
        if target_list:
            base = target_list.start("cols")
            for qualifier in re.finditer(r"(?:\[[^\]]+\]|[A-Za-z_][\w$#]*)\s*\.\s*(?=\[|[A-Za-z_])", target_list.group("cols")):
                spans.append((base + qualifier.start(), base + qualifier.end()))
        for match in OUTPUT_CLAUSE.finditer(masked):
            spans.append(match.span())
            if match.group("target"):
                outputs.append(split_name(match.group("target"))[-1].lower())
        for start, end in sorted(spans, reverse=True):
            text = text[:start] + "".join(c if c in "\r\n" else " " for c in text[start:end]) + text[end:]
        for match in SPACED_OPERATOR.finditer(mask_noncode(text)):
            # `a ! = b`, `a < = b` and `@x + = y` are accepted by SQL Server: close the gap, keeping the length.
            op, eq = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), match.group(4))
            gap = match.end() - match.start() - len(op) - len(eq)
            text = text[:match.start()] + op + eq + " " * gap + text[match.end():]
        return text, outputs

    def _reparse(self, root_label: str, rel: str, statement: Statement) -> Statement | None:
        text, outputs = self._blank(statement.text)
        masked = mask_noncode(text)
        compound = list(COMPOUND.finditer(masked))
        for match in reversed(compound):
            lhs = text[match.start("lhs"):match.end("lhs")]
            end = expression_end(mask_noncode(text), match.end(), text)
            text = (text[:match.start()] + f"{lhs} = {lhs} {match.group('op')} (" + text[match.end():end] + ")"
                    + text[end:])
        # SQLGlot splits @2_name into '@', '2' and '_name'; give such variables a parseable
        # placeholder name (restored by restore_variable wherever names are read).
        digits = [] if DIGIT_PLACEHOLDER in text.lower() else list(DIGIT_VARIABLE.finditer(mask_noncode(text)))
        for match in reversed(digits):
            text = text[:match.start()] + DIGIT_PLACEHOLDER + text[match.end():]
        compound = compound or digits
        if text == statement.text:
            return None
        tree = parse_or_none(text)
        if tree is None:
            return None
        if compound:
            self.rewritten.add((root_label + "/" + rel, statement.start))
        if DML_TOP.search(mask_noncode(statement.text)):
            self.top_limited.add((root_label + "/" + rel, statement.start))
        self.stats["repaired"] += 1
        self.pending_outputs.extend((statement, name) for name in outputs)
        return replace(statement, tree=tree, parse_status="parsed", parse_error=None)

    def _split_glued(self, root_label: str, rel: str, statement: Statement) -> list[Statement]:
        masked = mask_noncode(statement.text)
        cuts = glued_boundaries(masked)
        if not cuts:
            return []
        pieces: list[Statement] = []
        start = 0
        bounds = cuts + [len(masked)]
        i = 0
        any_parsed = False
        while i < len(bounds):
            end = bounds[i]
            chunk = statement.text[start:end]
            piece = replace(statement, start=statement.start + start, end=statement.start + end, text=chunk,
                            line=statement.line + statement.text.count("\n", 0, start))
            tree = parse_or_none(chunk) if chunk.strip() else None
            if tree is None and chunk.strip():
                fixed = self._reparse(root_label, rel, piece)
                tree = fixed.tree if fixed is not None else None
            if tree is not None:
                pieces.append(replace(piece, tree=tree, parse_status="parsed", parse_error=None))
                any_parsed = True
                start = end
            elif i == len(bounds) - 1:
                if chunk.strip():
                    pieces.append(replace(piece, tree=None, parse_status="fallback"))
            else:
                # Extend the piece to the next boundary (e.g. INSERT ... on one line, SELECT on the next).
                i += 1
                continue
            i += 1
        if not any_parsed:
            return []
        if any(p.parse_status != "parsed" and not non_lineage(p.text) for p in pieces):
            # Some text did not form a statement (e.g. a line-start WITH (NOLOCK) hint): the cut was
            # inside one statement, so keep it whole and report it as unparsed rather than lose a part.
            return []
        self.stats["repaired"] += 1
        return pieces

    def _scope_at(self, unit: Unit, offset: int) -> tuple[str | None, str | None, Statement | None]:
        """Scope and procedure of the indexed statement nearest before ``offset``."""
        before = [s for s in unit.index.statements if s.start <= offset]
        if before:
            return before[-1].scope, before[-1].procedure, before[-1]
        return None, None, None

    def _cursors(self, unit: Unit) -> None:
        cursors: dict[str, list[tuple[int, Statement | None, exp.Expression | None]]] = defaultdict(list)
        for match in CURSOR.finditer(unit.masked):
            query_at = match.end("for")
            statement = next((s for s in unit.index.statements if s.start <= query_at < s.end), None)
            select = None
            if statement is not None and statement.start == query_at and statement.tree is not None:
                select = statement.tree
            elif statement is not None:
                try:
                    select = sqlglot.parse_one(unit.text[query_at:statement.end], read="tsql", error_level=ErrorLevel.RAISE)
                    statement = replace(statement, start=query_at, text=unit.text[query_at:statement.end],
                                        line=unit.line_at(query_at), tree=select)
                except (SqlglotError, ValueError, RecursionError):
                    select = None
            cursors[match.group("cursor").lower()].append((match.start(), statement, select))
        for match in FETCH.finditer(unit.masked):
            scope, procedure, near = self._scope_at(unit, match.start())
            if scope is None:
                continue
            declared = [c for c in cursors.get(match.group("cursor").lower(), []) if c[0] < match.start()]
            names = [v.strip().lower() for v in match.group("vars").split(",")]
            line = unit.line_at(match.start())
            fetch_statement = Statement(unit.path, line, match.start(), match.end(), unit.text[match.start():match.end()],
                                        procedure, scope, "parsed")
            if not declared or declared[-1][2] is None:
                for name in names:
                    self.assignments[(scope, name)].append(Assignment(name, scope, fetch_statement, unit, None, None, "fetch",
                                                                      f"FETCH at line {line} from a cursor whose query was not analysed"))
                continue
            _, statement, select = declared[-1]
            first = select
            while isinstance(first, (exp.Union, exp.Except, exp.Intersect)):
                first = first.this
            items = first.expressions if isinstance(first, exp.Select) else []
            for i, name in enumerate(names):
                value = items[i] if i < len(items) else None
                value = value.this if isinstance(value, exp.Alias) else value
                holder = replace(statement, scope=scope, procedure=procedure)
                self.assignments[(scope, name)].append(Assignment(
                    name, scope, holder if value is not None else fetch_statement, unit, value,
                    first if isinstance(first, exp.Select) else None, "fetch",
                    f"FETCH at line {line} from cursor {match.group('cursor')} (column {i + 1} of its query)"))

    def _alter_views(self, unit: Unit) -> None:
        """ALTER VIEW defines a view like CREATE VIEW; the indexer keeps only its SELECT."""
        for match in ALTER_VIEW.finditer(unit.masked):
            body = next((st for st in unit.index.statements if st.start >= match.end() and
                         not unit.masked[match.end():st.start].strip()), None)
            if body is None:
                continue
            original = unit.text[match.start():body.end]
            text = "CREATE" + original[len("ALTER"):]
            try:
                tree = sqlglot.parse_one(text, read="tsql", error_level=ErrorLevel.RAISE)
            except (SqlglotError, ValueError, RecursionError):
                continue
            if not isinstance(tree, exp.Create) or str(tree.args.get("kind") or "").upper() != "VIEW":
                continue
            statement = replace(body, start=match.start(), line=unit.line_at(match.start()), text=original, tree=tree,
                                parse_status="parsed")
            table = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
            if isinstance(table, exp.Table):
                ref = self.ref_from_table(table, unit, statement)
                ref = ObjectRef(ref.name, ref.schema, ref.database, ref.server, "view", None, ref.database_source)
                self._register(ref)
                self._add_writer(Writer(ref, "view", statement, unit, statement.procedure, 0))

    def _temp_drops(self, unit: Unit) -> None:
        for match in DROP_TEMP.finditer(unit.masked):
            scope, _, _ = self._scope_at(unit, match.start())
            if scope:
                self.temp_resets[(scope, norm(match.group("name")))].append(match.start())

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
        if server and norm(server) in self.local_servers:
            server = None  # a linked server that points at the databases being scanned
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
            batch = re.search(r"::<batch@(\d+)>$", scope)
            if batch:
                # A script's GO batches share one session, so #temp tables persist between them.
                prefix = scope[:batch.start()]
                earlier = sorted((int(m.group(1)), other) for other, names in self.temp_creators.items()
                                 if name in names and other.startswith(prefix + "::<batch@")
                                 for m in [re.search(r"<batch@(\d+)>$", other)] if m and int(m.group(1)) < int(batch.group(1)))
                return [earlier[-1][1]] if earlier else []
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
        evaluated = getattr(statement, "origin_line", None)
        if evaluated:
            # Text evaluated from dynamic SQL: cite the EXEC's lines; the excerpt is the evaluated SQL.
            start_line, end_line = statement.origin_line, max(statement.origin_end_line, statement.origin_line)
        elif node is not None and (f"{unit.root_label}/{unit.rel_path}", statement.start) not in self.rewritten:
            positions = [n.meta.get("start") for n in node.walk() if isinstance(n.meta.get("start"), int)]
            ends = [n.meta.get("end") for n in node.walk() if isinstance(n.meta.get("end"), int)]
            if positions and ends and 0 <= min(positions) <= max(ends) < len(statement.text):
                start_line = statement.line + statement.text.count("\n", 0, min(positions))
                end_line = statement.line + statement.text.count("\n", 0, max(ends))
        if excerpt is None:
            excerpt = node.sql(dialect="tsql", comments=False) if node is not None else statement.text
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
            fake = Statement(unit.path, unit.line_at(match.start()), match.start(), match.start(), "", None, "", "parsed")
            ref = self.ref_from_text(match.group("name"), unit, fake)
            ref = ObjectRef(ref.name, ref.schema, ref.database, ref.server, "procedure", None, ref.database_source)
            names = {start: name for start, _, name in procedure_scopes(unit.text, masked)}
            scope = f"{unit.root_label}/{unit.rel_path}::{names.get(match.start(), normalize_name(match.group('name')))}"
            info = ProcInfo(proc_key(ref), ref, scope, unit, params, match.start(), unit.line_at(match.start()))
            if end:
                header_statement = Statement(unit.path, unit.line_at(match.start()), match.start(), end,
                                             unit.text[match.start():end], match.group("name"), scope, "parsed")
                info.defaults = parameter_defaults(unit.text[match.start():end], header_statement)
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
        switch = SWITCH.search(mask_noncode(text))
        if switch:
            target = self.ref_from_text(switch.group("target"), unit, statement)
            self._issue("unsupported_statement", unit, statement, (target.name,),
                        f"ALTER TABLE ... SWITCH moves every row of {split_name(switch.group('source'))[-1]} into this "
                        "table; its columns correspond by position and are not mapped.")
            return
        self._alter_default(unit, statement)
        returning = tree.args.get("returning") if isinstance(tree, exp.Expression) else None
        into = returning.args.get("into") if isinstance(returning, exp.Expression) else None
        if into is not None:
            # SQLGlot parses OUTPUT ... INTO natively; report the OUTPUT target instead of losing it.
            node = into.this if isinstance(into, exp.Into) else into
            name = table_parts(node)[3] if isinstance(node, exp.Table) else ident_text(node)
            if name:
                self._issue("unsupported_statement", unit, statement, (norm(name),),
                            "OUTPUT ... INTO writes rows to this object; the OUTPUT column mapping is not analysed.")
        if DYNAMIC.match(text):
            self._issue("dynamic_sql", unit, statement, self._literal_targets(unit, statement),
                        "Dynamic SQL is executed here; its statements are not analysed.")
            return
        insert_exec = INSERT_EXEC.match(mask_noncode(text))
        if insert_exec is None and re.match(r"\s*INSERT\b", text, re.I) and (
                tree is None or isinstance(tree, exp.Command) or (isinstance(tree, exp.Insert) and tree.expression is None)):
            # The indexer splits INSERT ... EXEC into two statements; look at what follows.
            if re.match(r"\s*EXEC(?:UTE)?\b", unit.masked[statement.end:statement.end + 200], re.I):
                insert_exec = INSERT_EXEC.match(mask_noncode(text) + " EXEC")
        if insert_exec:
            target = self.ref_from_text(insert_exec.group("target"), unit, statement)
            self._issue("insert_exec", unit, statement, (target.name,),
                        "INSERT ... EXEC: rows come from a procedure result set, which is not mapped to columns.")
            self._writer_ref(target, unit, statement)
            return
        if tree is None or isinstance(tree, exp.Command):
            targets = write_targets(mask_noncode(text))
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
                schema = tree.this
                if isinstance(schema, exp.Schema) and isinstance(schema.this, exp.Table):
                    ref = self.ref_from_table(schema.this, unit, statement)
                    if ref.kind == "temp_table":
                        self.temp_resets[(statement.scope, ref.name)].append(statement.start)
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
                    self._assign(unit, statement, eq.this.sql(dialect="tsql", comments=False), eq.expression, None, "set")
            return
        if isinstance(tree, exp.Execute):
            self._call(unit, statement, tree)
            return
        if isinstance(tree, exp.Insert):
            target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
            if isinstance(target, exp.Table) and not isinstance(target.this, exp.Func):
                if self._via_cte(tree, target, "insert", unit, statement, order):
                    return
                self._add_writer(Writer(self._writer_ref(self.ref_from_table(target, unit, statement), unit, statement),
                                        "insert", statement, unit, statement.procedure, order))
            return
        if isinstance(tree, exp.Update):
            for item in tree.expressions:
                if isinstance(item, exp.EQ) and isinstance(item.this, exp.Parameter):
                    self._assign(unit, statement, item.this.sql(dialect="tsql", comments=False), item.expression, tree, "update")
            target = update_target(tree)
            if isinstance(target, exp.Table):
                if self._via_cte(tree, target, "update", unit, statement, order):
                    return
                self._add_writer(Writer(self._writer_ref(self.ref_from_table(target, unit, statement), unit, statement),
                                        "update", statement, unit, statement.procedure, order))
            return
        if isinstance(tree, exp.Merge):
            target = tree.this
            if isinstance(target, exp.Table):
                if self._via_cte(tree, target, "merge", unit, statement, order):
                    return
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
                    self.temp_resets[(statement.scope, ref.name)].append(statement.start)
                return
            if isinstance(tree, exp.Select):
                for item in tree.expressions:
                    if isinstance(item, exp.EQ) and isinstance(item.this, exp.Parameter):
                        self._assign(unit, statement, item.this.sql(dialect="tsql", comments=False), item.expression, tree, "select")
            return

    def _via_cte(self, tree, target: exp.Table, kind: str, unit: Unit, statement: Statement, order: int) -> bool:
        """A write whose target is a CTE writes the CTE's base table(s)."""
        _, database, schema, name = table_parts(target)
        if database or schema:
            return False
        with_ = tree.args.get("with_")
        ctes = {norm(c.alias_or_name): c for c in (with_.expressions if with_ is not None else [])}
        cte = ctes.get(norm(name))
        if cte is None:
            return False
        query = cte.this
        while isinstance(query, (exp.Union, exp.Except, exp.Intersect)):
            query = query.this
        tables = []
        if isinstance(query, exp.Select):
            from_ = query.args.get("from_")
            if from_ is not None and isinstance(from_.this, exp.Table):
                tables.append(from_.this)
            for join in list(query.args.get("joins") or []) + (list(from_.this.args.get("joins") or []) if from_ is not None and isinstance(from_.this, exp.Expression) else []):
                if isinstance(join.this, exp.Table):
                    tables.append(join.this)
        for table in tables:
            _, _, _, base_name = table_parts(table)
            if norm(base_name) in ctes:
                continue
            ref = self._writer_ref(self.ref_from_table(table, unit, statement), unit, statement)
            self._add_writer(Writer(ref, kind, statement, unit, statement.procedure, order, ("cte", norm(name))))
        return True

    def _literal_targets(self, unit: Unit, statement: Statement) -> tuple[str, ...]:
        """Object names that string literals in the same scope name as write targets.

        Dynamic SQL is usually assembled in variables before the EXEC, so look at every
        literal in the procedure or batch, not only the EXEC statement.
        """
        literals = []
        for other in unit.index.statements:
            if other.scope != statement.scope:
                continue
            masked = mask_noncode(other.text)
            # Keep only string-literal (and comment) content, joined across concatenations.
            literals.append("".join(c if m == " " and c != " " else " " for c, m in zip(other.text, masked)))
        return write_targets(" ".join(literals).replace("'", " "), literal=True)

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

    def _alter_default(self, unit: Unit, statement: Statement) -> None:
        """ALTER TABLE ... ADD [CONSTRAINT x] DEFAULT (expr) FOR col."""
        for match in ALTER_DEFAULT.finditer(statement.text):
            ref = self.ref_from_text(match.group("name"), unit, statement)
            expr = match.group("expr").strip()
            while expr.startswith("(") and expr.endswith(")") and depth_at(expr, 1, len(expr) - 1) == 0:
                expr = expr[1:-1].strip()
            self.column_defaults[ref.key][norm(split_name(match.group("col"))[-1])] = expr

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
                name = restore_variable(name_node.sql(dialect="tsql", comments=False))
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

    def _assign(self, unit, statement, name, value, select, kind, note=None) -> None:
        name = restore_variable(name).lower()
        if name.startswith("@@"):
            return
        self.assignments[(statement.scope, name)].append(Assignment(name, statement.scope, statement, unit, value, select,
                                                                    kind, note))

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
                args.append((restore_variable(arg.this.sql(dialect="tsql", comments=False)).lower(), arg.expression))
                if output and isinstance(arg.expression, exp.Parameter):
                    self._assign(unit, statement, arg.expression.sql(dialect="tsql", comments=False), None, None, "exec_output")
            else:
                args.append((None, arg))
                if output and isinstance(arg, exp.Parameter):
                    self._assign(unit, statement, arg.sql(dialect="tsql", comments=False), None, None, "exec_output")
        returned = re.match(r"\s*EXEC(?:UTE)?\s+(@[\w$#@]+)\s*=", mask_noncode(statement.text), re.I)
        if returned:
            self._assign(unit, statement, returned.group(1), None, None, "exec_output")
        call = Call(statement.scope, callee, statement, unit, args)
        self.calls_by_callee[proc_key(callee)].append(call)
        self.calls_by_caller[statement.scope].append(call)

    # ---------------------------------------------------------------- queries
    def aliases_of(self, ref: ObjectRef) -> list[str]:
        """Other keys under which writes to ``ref`` are recorded: same name with no database
        context (when only this database is known for it) and synonyms that resolve to it."""
        keys = []
        if ref.kind in ("table", "view") and ref.database and ref.schema and not ref.server:
            if self.known_databases.get((ref.schema, ref.name)) == {ref.database}:
                keys.append(ObjectRef(ref.name, ref.schema, None, None).key)
            for syn_key, (target, _, _) in self.synonyms.items():
                if self.canonical(target)[0].key == ref.key and syn_key != ref.key:
                    keys.append(syn_key)
        return keys

    def view_bases(self) -> dict[str, list[str]]:
        """View key -> keys of tables in its top-level FROM/JOIN (for writes through a view)."""
        if self._view_bases is None:
            self._view_bases = defaultdict(list)
            for key, writers in self.writers.items():
                for writer in writers:
                    if writer.kind != "view" or writer.via:
                        continue
                    query = writer.statement.tree.args.get("expression")
                    while isinstance(query, (exp.Union, exp.Except, exp.Intersect)):
                        query = query.this
                    if not isinstance(query, exp.Select):
                        continue
                    from_ = query.args.get("from_")
                    tables = [from_.this] if from_ is not None and isinstance(from_.this, exp.Table) else []
                    joins = list(query.args.get("joins") or [])
                    if from_ is not None and isinstance(from_.this, exp.Expression):
                        joins += list(from_.this.args.get("joins") or [])
                    tables += [j.this for j in joins if isinstance(j.this, exp.Table)]
                    for table in tables:
                        base, _ = self.canonical(self.ref_from_table(table, writer.unit, writer.statement))
                        self._view_bases[key].append(base.key)
        return self._view_bases

    def dead_dynamic(self, issue: StatementIssue) -> bool:
        """Dynamic SQL that can only execute an empty string or NULL, so it never runs anything.

        True only when the executed text is a single variable that is not a parameter, every
        assignment to it in the scope is an empty-string or NULL literal (or there are none), and
        no unparsed statement in the scope could assign it.
        """
        if issue.kind != "dynamic_sql":
            return False
        cached = self._dead_dynamic.get(id(issue))
        if cached is not None:
            return cached
        match = EXECUTED_VARIABLE.match(mask_noncode(issue.statement.text))
        dead = False
        if match:
            name = restore_variable(next(g for g in match.groups() if g)).lower()
            scope = issue.statement.scope
            assignments = self.assignments.get((scope, name), [])
            unparsed = re.compile(re.escape(name) + r"(?![\w$#@])\s*[-+*/%&|^]?=(?!=)", re.I)
            dead = (not self.is_parameter(scope, name)
                    and all(a.kind in ("set", "declare", "select") and empty_literal(a.value) for a in assignments)
                    and not any(unparsed.search(mask_noncode(s.text)) for s in self.fallback_by_scope.get(scope, [])))
        self._dead_dynamic[id(issue)] = dead
        return dead

    def evaluate_dynamic(self) -> None:
        """Evaluate every dynamic SQL statement whose text can be enumerated (once, after loading).

        The statements of each enumerated string are indexed as writers at the EXEC, in its scope.
        """
        if self._evaluations is not None:
            return
        from .dynamic_eval import Evaluator
        self._evaluations = {}
        evaluator = Evaluator(self)
        pending = []
        for issue in list(self.issues):
            if issue.kind != "dynamic_sql" or self.dead_dynamic(issue):
                continue
            evaluation = evaluator.evaluate(issue)
            self._evaluations[id(issue)] = evaluation
            if evaluation.usable:
                pending.append((issue, evaluation))
        for issue, evaluation in pending:
            order = next((i for i, s in enumerate(issue.unit.index.statements) if s.start == issue.statement.start), 0)
            for alternative in evaluation.alternatives:
                for statement in alternative.statements:
                    try:
                        self._statement(issue.unit, statement, order)
                    except Exception as exc:  # noqa: BLE001
                        self._issue("unsupported_statement", issue.unit, statement, (),
                                    f"Evaluated dynamic SQL could not be indexed: {type(exc).__name__}")

    def dynamic_evaluation(self, issue: StatementIssue):
        """The evaluation of a dynamic_sql issue (None when not evaluated)."""
        if issue.kind != "dynamic_sql":
            return None
        self.evaluate_dynamic()
        return self._evaluations.get(id(issue))

    def dynamic_resolved(self, issue: StatementIssue) -> bool:
        """Dynamic SQL every possible text of which was enumerated and analysed as writers."""
        evaluation = self.dynamic_evaluation(issue)
        return bool(evaluation is not None and evaluation.complete)

    def issue_names(self, ref: ObjectRef) -> set[str]:
        """Names under which statement issues for ``ref`` may be recorded: its own name, synonyms
        that resolve to it and updatable views over it."""
        names = {ref.name}
        if ref.kind in ("table", "view"):
            for syn_key, (target, _, _) in self.synonyms.items():
                if self.canonical(target)[0].key == ref.key:
                    names.add(norm(split_name(syn_key)[-1]))
            for view_key, bases in self.view_bases().items():
                if ref.key in bases:
                    views = self.writers.get(view_key, [])
                    if views:
                        names.add(views[0].ref.name)
        return names

    def dynamic_literal_args(self) -> dict[str, list]:
        """Object name -> (call, issue) for calls passing that name as a string literal to a procedure
        that runs dynamic SQL not fully evaluated (a generic loader called with its target's name)."""
        if self._dynamic_args is None:
            found: dict[str, list] = defaultdict(list)
            for scope, issues in self.dynamic_by_scope.items():
                live = [i for i in issues if not self.dead_dynamic(i) and not self.dynamic_resolved(i)]
                proc = self.proc_by_scope.get(scope)
                if not live or proc is None:
                    continue
                calls = list(self.calls_by_callee.get(proc.key, []))
                if proc.ref.database:
                    calls += self.calls_by_callee.get(".".join(("?", proc.ref.schema or "?", proc.ref.name)), [])
                for call in calls:
                    for _, arg in call.args:
                        if isinstance(arg, (exp.Literal, exp.National)) and (isinstance(arg, exp.National) or arg.is_string):
                            parts = split_name(arg.this or "")
                            if 1 <= len(parts) <= 4 and all(re.fullmatch(r"[A-Za-z_#][\w$#@ ]*", p) for p in parts):
                                found[parts[-1].lower()].append((call, live[0]))
            self._dynamic_args = found
        return self._dynamic_args

    def writers_for(self, ref: ObjectRef) -> list[Writer]:
        """Writers of an object; for a #temp table, includes callees writing into it."""
        found = list(self.writers.get(ref.key, []))
        for key in self.aliases_of(ref):
            found.extend(self.writers.get(key, []))
        if ref.kind in ("table", "view"):
            for view_key, bases in self.view_bases().items():
                if ref.key in bases and view_key != ref.key:
                    for writer in self.writers.get(view_key, []):
                        if writer.kind in ("insert", "update", "merge") and not writer.via:
                            found.append(Writer(ref, writer.kind, writer.statement, writer.unit, writer.procedure,
                                                writer.order, ("view", view_key)))
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
        schemas = list(self.schemas.get(ref.key, []))
        if not schemas:
            for key in self.aliases_of(ref):
                schemas.extend(self.schemas.get(key, []))
        creates = [s for s in schemas if s.source in ("create_table", "table_variable")]
        if ref.kind == "temp_table":
            creates = [s for s in creates if s.statement.scope == ref.scope]
        if not creates:
            return None, "missing", None
        distinct = {tuple((c.name, c.identity, c.computed, c.rowversion) for c in s.columns) for s in creates}
        if len(distinct) > 1:
            return None, "ambiguous", creates[0]
        defaults = self.column_defaults.get(ref.key, {})
        columns = [ColumnInfo(c.name, c.ordinal, c.identity, c.computed, c.rowversion, c.default or defaults.get(c.name))
                   for c in creates[0].columns]
        for extra in [s for s in schemas if s.source == "alter_add"]:
            for c in extra.columns:
                if all(x.name != c.name for x in columns):
                    columns.append(ColumnInfo(c.name, len(columns) + 1, c.identity, c.computed, c.rowversion, c.default))
        return columns, "declared", creates[0]

    def is_known(self, ref: ObjectRef) -> bool:
        keys = [ref.key] + self.aliases_of(ref)
        return any(self.schemas.get(k) or self.writers.get(k) or k in self.synonyms for k in keys)


def controls_of(unit: Unit, statement: Statement) -> tuple[str, ...]:
    """Control-flow labels of a statement; evaluated dynamic SQL carries its own (EXEC's plus its alternative's)."""
    controls = getattr(statement, "controls", None)
    return tuple(controls) if controls is not None else unit.controls.get(statement.start, ())


def proc_key(ref: ObjectRef) -> str:
    return ".".join(p for p in (ref.server, ref.database or "?", ref.schema or "?", ref.name) if p is not None)


def statement_id(unit: Unit, statement: Statement) -> str:
    raw = f"{unit.root_label}|{unit.rel_path}|{statement.start}|{statement.end}"
    if getattr(statement, "eval_key", None):
        raw += f"|dynamic:{statement.eval_key}"   # one EXEC can yield several evaluated statements
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
    if not matches:
        # An unaliased table's exposed name is its object name: UPDATE output ... FROM demo.output
        matches = [s for s in sources if isinstance(s, exp.Table) and not s.alias and
                   (table_parts(s)[3] or "").lower() == (name or "").lower() and (table_parts(s)[2] or table_parts(s)[1])]
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
            computed = kind.this.sql(dialect="tsql", comments=False) if kind.this is not None else "computed"
        elif isinstance(kind, exp.DefaultColumnConstraint):
            default = kind.this.sql(dialect="tsql", comments=False) if kind.this is not None else None
    data_type = definition.args.get("kind")
    type_name = data_type.sql(dialect="tsql", comments=False).upper() if data_type is not None else ""
    if data_type is None and computed is None:
        # `col AS (expr)` without a type is a computed column in T-SQL.
        computed = None
    rowversion = type_name.startswith(("ROWVERSION", "TIMESTAMP"))
    return ColumnInfo(name.lower(), ordinal, identity, computed, rowversion, default)


# ----------------------------------------------------------------- writer search
WRITER_SEARCH_LABELS = {
    "other_object": "writes a same-named object that resolves to a different identity",
    "same_object_column_not_mapped": "writes this object and names the column, but no value was mapped to it",
    "unparsed": "statement could not be parsed",
    "ignored_non_lineage": "statement was classed as non-lineage and ignored",
    "parsed_no_writer": "statement parsed, but no writer was recorded from it",
    "parsed_other_writer": "statement parsed, but only a writer of another object was recorded from it",
    "same_object_other_columns": "writes this object, but does not name the column",
    "not_indexed": "text is outside every indexed statement",
    "comment_or_string": "write text inside a comment or string literal",
    "skipped_file": "in a file skipped by the file filters (dated snapshot)",
}


def _search_catalog(catalog: "Catalog") -> dict[str, list[tuple]]:
    """Object name -> (unit or path label, offset, in_code) for every write-shaped mention."""
    found: dict[str, list[tuple]] = defaultdict(list)
    for unit in catalog.units:
        for match in WRITE_TARGET.finditer(unit.text):
            name = norm(split_name(match.group("target"))[-1])
            at = match.start("target")
            found[name].append((unit, at, unit.masked[at] == unit.text[at]))
    for label, path in catalog.skipped_files:
        try:
            text = decode_source(Path(path).read_bytes())[0]
        except OSError:
            continue
        for match in WRITE_TARGET.finditer(mask_noncode(text)):
            name = norm(split_name(match.group("target"))[-1])
            found[name].append((f"{label}:{text.count(chr(10), 0, match.start()) + 1}", match.start(), True))
    return found


def _differs(ref: ObjectRef, other: ObjectRef) -> str:
    parts = []
    for field_name in ("server", "database", "schema", "kind"):
        a, b = getattr(ref, field_name), getattr(other, field_name)
        if a != b:
            source = {"explicit": "named in the statement", "use_statement": "from a USE statement",
                      "configured": "from --database-map", "default": "from --default-database",
                      "unified": "the only database declaring it"}.get(other.database_source or "")
            how = f" ({source})" if field_name == "database" and source else ""
            parts.append(f"{field_name} {b or '?'}{how} instead of {a or '?'}")
    if not parts and ref.scope != other.scope:
        parts.append("a different procedure scope")
    return ", ".join(parts) or "a different identity"


def writer_search(catalog: "Catalog", ref: ObjectRef, column: str, writers: list) -> list[dict]:
    """Statements that look like they write ``ref`` but gave no step for ``column``, with the reason.

    Used to explain a `source_without_writer` stop: a near miss is usually a database-context
    difference, a statement the parser could not read, or a file the filters skipped."""
    if catalog._write_mentions is None:
        catalog._write_mentions = _search_catalog(catalog)
        catalog._writers_by_statement = defaultdict(list)
        for items in catalog.writers.values():
            for writer in items:
                catalog._writers_by_statement[id(writer.statement)].append(writer)
    findings: list[dict] = []
    seen: set = set()
    own = {id(w.statement) for w in writers}
    column_word = re.compile(rf"(?<![\w@#$]){re.escape(column)}(?![\w$#])", re.I) if column else None
    for unit, at, in_code in catalog._write_mentions.get(norm(ref.name), []):
        if isinstance(unit, str):
            findings.append({"category": "skipped_file", "location": unit, "detail": WRITER_SEARCH_LABELS["skipped_file"]})
            continue
        location = f"{unit.root_label}/{unit.rel_path}:{unit.line_at(at)}"
        if not in_code:
            findings.append({"category": "comment_or_string", "location": location,
                             "detail": WRITER_SEARCH_LABELS["comment_or_string"]})
            continue
        statement = next((s for s in unit.index.statements if s.start <= at < max(s.end, s.start + len(s.text))), None)
        if statement is None:
            findings.append({"category": "not_indexed", "location": location, "detail": WRITER_SEARCH_LABELS["not_indexed"]})
            continue
        if id(statement) in seen:
            continue
        seen.add(id(statement))
        if id(statement) in own:
            named = bool(column_word and column_word.search(mask_noncode(statement.text)))
            category = "same_object_column_not_mapped" if named else "same_object_other_columns"
            findings.append({"category": category, "location": location, "detail": WRITER_SEARCH_LABELS[category]})
            continue
        if statement.parse_status != "parsed":
            category = "ignored_non_lineage" if non_lineage(statement.text) else "unparsed"
            findings.append({"category": category, "location": location, "detail": WRITER_SEARCH_LABELS[category]})
            continue
        recorded = [w for w in catalog._writers_by_statement.get(id(statement), []) if w.ref.name == norm(ref.name)]
        if recorded:
            for writer in recorded[:1]:
                findings.append({"category": "other_object", "location": location,
                                 "detail": f"{WRITER_SEARCH_LABELS['other_object']}: {writer.ref.display} "
                                           f"({_differs(ref, writer.ref)})"})
            continue
        others = catalog._writers_by_statement.get(id(statement))
        category = "parsed_other_writer" if others else "parsed_no_writer"
        findings.append({"category": category, "location": location,
                         "detail": f"{WRITER_SEARCH_LABELS[category]} (parsed as {type(statement.tree).__name__}"
                                   + (f", writing {others[0].ref.display})" if others else ")")})
    return findings


# ----------------------------------------------------------------- scalar function summaries
FUNCTION_HEADER = re.compile(rf"\b(?:CREATE\s+(?:OR\s+ALTER\s+)?|ALTER\s+)FUNCTION\s+(?P<name>{IDENT})\s*\(", re.I)
READS_DATA = re.compile(r"(?<![\w@#$.])(?:FROM|JOIN|INTO|EXEC(?:UTE)?|UPDATE|INSERT|DELETE|MERGE|OPENQUERY|OPENROWSET)\b",
                        re.I)
USER_CALL = re.compile(rf"(?<![\w@#$.])(?P<name>(?:\[[^\]]+\]|[A-Za-z_][\w$#]*)(?:\s*\.\s*(?:\[[^\]]+\]|[A-Za-z_][\w$#]*))+)\s*\(")
NONDETERMINISTIC = re.compile(r"\b(?:GETDATE|GETUTCDATE|SYSDATETIME|SYSUTCDATETIME|SYSDATETIMEOFFSET|CURRENT_TIMESTAMP|"
                              r"NEWID|NEWSEQUENTIALID|RAND|CRYPT_GEN_RANDOM)\b", re.I)


@dataclass
class FunctionSummary:
    """What a scalar user function's result can depend on, read from its body."""
    key: tuple[str, str]               # (schema, name)
    params: list[str]
    used: list[bool]                   # per parameter: referenced in the body
    unit: Unit
    line: int
    pure: bool                         # reads no tables and calls only pure user functions
    reason: str | None = None          # why it is not pure
    time_dependent: bool = False


def _function_definitions(catalog: "Catalog") -> dict[tuple[str, str], list[FunctionSummary]]:
    found: dict[tuple[str, str], list[FunctionSummary]] = defaultdict(list)
    for unit in catalog.units:
        masked = unit.masked
        for match in FUNCTION_HEADER.finditer(masked):
            parts = split_name(match.group("name"))
            key = (norm(parts[-2]) if len(parts) > 1 and parts[-2] else catalog.default_schema, norm(parts[-1]))
            close = match.end()
            depth = 1
            while close < len(masked) and depth:
                depth += {"(": 1, ")": -1}.get(masked[close], 0)
                close += 1
            params = re.findall(r"@[\w$#@]+", masked[match.end():close - 1])
            header_end = re.compile(r"\bRETURNS\b", re.I).search(masked, close)
            if header_end is None:
                continue
            after = masked[header_end.end():header_end.end() + 200]
            if re.match(r"\s*(?:@[\w$#@]+\s+)?TABLE\b", after, re.I):
                continue   # table-valued: not a scalar function
            end = len(masked)
            for boundary in (GO.search(masked, close), FUNCTION_HEADER.search(masked, close), DEFINITION.search(masked, close)):
                if boundary is not None:
                    end = min(end, boundary.start())
            body = masked[header_end.end():end]
            used = [re.search(rf"(?<![\w@#$]){re.escape(p)}(?![\w$#@])", body, re.I) is not None for p in params]
            reads = READS_DATA.search(body)
            summary = FunctionSummary(key, [p.lower() for p in params], used, unit, unit.line_at(match.start()),
                                      reads is None, None if reads is None else f"its body uses {reads.group().upper()}",
                                      NONDETERMINISTIC.search(body) is not None)
            summary._calls = [m.group("name") for m in USER_CALL.finditer(body)]
            found[key].append(summary)
    return found


def function_summary(catalog: "Catalog", schema: str | None, name: str, _seen: set | None = None) -> FunctionSummary | str:
    """The summary of scalar function schema.name, or a string saying why there is none."""
    if catalog._functions is None:
        catalog._functions = _function_definitions(catalog)
    key = (norm(schema) or catalog.default_schema, norm(name))
    candidates = catalog._functions.get(key, [])
    if not candidates:
        return "its definition is not in the scanned files"
    if len(candidates) > 1:
        shapes = {(tuple(c.params), tuple(c.used), c.pure) for c in candidates}
        if len(shapes) > 1:
            return f"it has {len(candidates)} different definitions in the scanned files"
    summary = candidates[0]
    if not summary.pure:
        return summary.reason or "its body reads data"
    seen = set(_seen or ()) | {key}
    for call in getattr(summary, "_calls", []):
        parts = split_name(call)
        inner_key = (norm(parts[-2]) if len(parts) > 1 else catalog.default_schema, norm(parts[-1]))
        if inner_key in seen:
            return "it calls itself recursively"
        inner = function_summary(catalog, inner_key[0], inner_key[1], seen)
        if isinstance(inner, str):
            return f"it calls {call}, and {inner}"
    return summary
