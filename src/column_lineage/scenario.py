"""Scenario-specific lineage: assumed values for a procedure's input parameters (--scenario).

A scenario names a procedure and values for some of its parameters, e.g. ``dbo.publish_data @mode=0
@rebuild=1``. The procedure body is walked once with those values: an IF whose condition the values
settle keeps only the arm that runs, and statements after a RETURN that the values make certain are
unreachable. Writers, statement issues, variable assignments and procedure calls in unreachable
statements are removed from the catalog for this run and recorded with the reason, so the pages can
list them.

Everything uncertain is kept, never dropped: a condition that cannot be evaluated (other variables,
columns, functions, EXISTS, ...) keeps both arms; a parameter that is assigned inside the procedure
(SET, SELECT @p =, FETCH INTO, OUTPUT arguments, EXEC @p =, or a statement that could not be parsed)
is unknown from that point; a parameter assigned in a loop or a TRY block is unknown throughout it; a
procedure that uses GOTO is not constrained at all. Values are not carried into called procedures:
their writers keep every branch. Parameters the scenario does not name are unknown (their declared
defaults are not assumed).

The values are assumptions for this analysis, not evidence of what ran in production; the pages say so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from table_lineage.index import DEFINITION, GO, mask_noncode

from .control import _collapse, _Parser, header_end

UNKNOWN = object()          # a value or truth that cannot be settled
NULL = None                 # SQL NULL


class ScenarioError(ValueError):
    pass


@dataclass
class Scenario:
    procedure: str                       # as given on the command line
    values: dict[str, object]            # @param -> Decimal | str | None (NULL)
    texts: dict[str, str]                # @param -> the value as given


def parse_scenario(text: str) -> Scenario:
    """'dbo.proc @a=1 @b=NULL @c=EU' (or comma separated). A quoted value is a string; NULL is NULL; a
    number is a number; any other word is a string."""
    parts = text.replace(",", " ").split()
    if len(parts) < 2 or parts[0].startswith("@"):
        raise ScenarioError(f"--scenario {text!r}: expected a procedure name followed by @param=value items")
    values: dict[str, object] = {}
    texts: dict[str, str] = {}
    for item in parts[1:]:
        match = re.fullmatch(r"(@[\w$#@]+)=(.*)", item)
        if not match or not match.group(2):
            raise ScenarioError(f"--scenario {text!r}: {item!r} is not @param=value")
        name, raw = match.group(1).lower(), match.group(2)
        if name in values:
            raise ScenarioError(f"--scenario {text!r}: {name} is given twice")
        values[name] = literal_value(raw)
        texts[name] = raw
    return Scenario(parts[0], values, texts)


def literal_value(raw: str):
    if raw.upper() == "NULL":
        return NULL
    if len(raw) >= 2 and raw[0] == raw[-1] == "'" or (raw[:2].upper() == "N'" and raw.endswith("'")):
        return raw[raw.index("'") + 1:-1].replace("''", "'")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return raw


# ------------------------------------------------------------------------------------------- structure
@dataclass
class Stmt:
    start: int
    end: int
    word: str | None


@dataclass
class If:
    start: int
    condition: str
    then: object
    other: object | None
    loop: bool = False


@dataclass
class Block:
    items: list
    kind: str | None = None              # None, "TRY" or "CATCH"


@dataclass
class TryCatch:
    body: Block
    catch: Block | None


class _Tree(_Parser):
    """The control structure of one procedure body (IF/ELSE, WHILE, BEGIN...END, TRY/CATCH)."""

    def node(self, i: int, end: int):
        self.steps += 1
        if self.steps > 200000:
            raise RecursionError("scenario scan budget exceeded")
        i = self.skip_ws(i, end)
        if i >= end:
            return None, end
        word = self.word_at(i)
        if word is None:
            e = self.simple_end(i, end)
            return Stmt(i, e, None), e
        name, after = word
        if name in ("IF", "WHILE"):
            cond_end = self.condition_end(after, end)
            then, body_end = self.node(cond_end, end)
            other = None
            if name == "IF":
                j = self.skip_ws(body_end, end)
                nxt = self.word_at(j)
                if nxt and nxt[0] == "ELSE":
                    other, body_end = self.node(nxt[1], end)
            return If(i, self.text[after:cond_end], then, other, name == "WHILE"), body_end
        if name == "BEGIN":
            j = self.skip_ws(after, end)
            nxt = self.word_at(j)
            if nxt and nxt[0] in ("TRAN", "TRANSACTION", "DISTRIBUTED"):
                e = self.simple_end(i, end)
                return Stmt(i, e, "BEGIN"), e
            if nxt and nxt[0] == "TRY":
                body, body_end = self.block(nxt[1], end, "TRY")
                k = self.skip_ws(body_end, end)
                begin = self.word_at(k)
                if begin and begin[0] == "BEGIN":
                    catch = self.word_at(self.skip_ws(begin[1], end))
                    if catch and catch[0] == "CATCH":
                        handler, catch_end = self.block(catch[1], end, "CATCH")
                        return TryCatch(body, handler), catch_end
                return TryCatch(body, None), body_end
            return self.block(after, end, None)
        e = self.simple_end(i, end)
        return Stmt(i, e, name), e

    def block(self, i: int, end: int, kind: str | None):
        items = []
        while i < end:
            i = self.skip_ws(i, end)
            if i >= end:
                break
            word = self.word_at(i)
            if word and word[0] == "END":
                after = word[1]
                if kind:
                    nxt = self.word_at(self.skip_ws(after, end))
                    if nxt and nxt[0] == kind:
                        return Block(items, kind), nxt[1]
                return Block(items, kind), after
            item, nxt = self.node(i, end)
            if item is not None:
                items.append(item)
            i = nxt if nxt > i else i + 1
        return Block(items, kind), end


# ------------------------------------------------------------------------------------------- evaluation
def _number(value):
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            return UNKNOWN
    return UNKNOWN


def _compare(op: str, left, right):
    """Three-valued comparison: True, False, None (NULL) or UNKNOWN (cannot be settled)."""
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    if left is NULL or right is NULL:
        return NULL
    if isinstance(left, str) and isinstance(right, str):
        a, b = left.rstrip(" "), right.rstrip(" ")
        if op in ("=", "<>"):
            if a == b:
                return op == "="
            if a.casefold() == b.casefold():
                return UNKNOWN        # equal under a case-insensitive collation, not under a sensitive one
            return op == "<>"
        return UNKNOWN                # string ordering depends on the collation
    a, b = _number(left), _number(right)
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    return {"=": a == b, "<>": a != b, ">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]


def _not(v):
    return UNKNOWN if v is UNKNOWN else (NULL if v is NULL else not v)


def _and(a, b):
    if a is False or b is False:
        return False
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    if a is NULL or b is NULL:
        return NULL
    return True


def _or(a, b):
    if a is True or b is True:
        return True
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    if a is NULL or b is NULL:
        return NULL
    return False


COMPARISONS = {exp.EQ: "=", exp.NEQ: "<>", exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<="}


def evaluate(node, env: dict):
    """Value or truth of a condition/expression over literals and the scenario's parameters."""
    if isinstance(node, exp.Paren):
        return evaluate(node.this, env)
    if isinstance(node, exp.Null):
        return NULL
    if isinstance(node, exp.Literal):
        return node.this if node.is_string else _number(node.this)
    if isinstance(node, exp.Neg):
        value = evaluate(node.this, env)
        number = _number(value) if value is not NULL else NULL
        return NULL if number is NULL else (UNKNOWN if number is UNKNOWN else -number)
    if isinstance(node, exp.Parameter):
        return env.get(node.sql(dialect="tsql").lower(), UNKNOWN)
    if type(node) in COMPARISONS:
        return _compare(COMPARISONS[type(node)], evaluate(node.this, env), evaluate(node.expression, env))
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
        value = evaluate(node.this, env)
        return UNKNOWN if value is UNKNOWN else value is NULL
    if isinstance(node, exp.Not):
        return _not(evaluate(node.this, env))
    if isinstance(node, exp.And):
        return _and(evaluate(node.this, env), evaluate(node.expression, env))
    if isinstance(node, exp.Or):
        return _or(evaluate(node.this, env), evaluate(node.expression, env))
    if isinstance(node, exp.In) and not node.args.get("query"):
        left = evaluate(node.this, env)
        result = False
        for item in node.expressions:
            result = _or(result, _compare("=", left, evaluate(item, env)))
        return result
    if isinstance(node, exp.Between):
        value = evaluate(node.this, env)
        return _and(_compare(">=", value, evaluate(node.args.get("low"), env)),
                    _compare("<=", value, evaluate(node.args.get("high"), env)))
    return UNKNOWN


def condition_truth(text: str, env: dict):
    """True, False or UNKNOWN for an IF/WHILE condition (SQL NULL is not true: the ELSE arm runs)."""
    try:
        tree = sqlglot.parse_one(f"SELECT 1 WHERE {text}", read="tsql", error_level=ErrorLevel.RAISE)
    except (SqlglotError, ValueError, RecursionError):
        return UNKNOWN
    where = tree.args.get("where") if isinstance(tree, exp.Select) else None
    if where is None:
        return UNKNOWN
    value = evaluate(where.this, env)
    if value is UNKNOWN:
        return UNKNOWN
    return value is True


def _coerce(value, declared: str | None):
    """A supplied value converted as SQL Server converts it to the parameter's declared type."""
    kind = (declared or "").lower()
    if value is NULL or not kind:
        return value
    if kind == "bit":
        if isinstance(value, str):
            word = value.strip().upper()
            if word in ("TRUE", "FALSE"):
                return Decimal(1 if word == "TRUE" else 0)
            number = _number(value)
            if number is UNKNOWN:
                raise ScenarioError(f"cannot convert {value!r} to bit")
            value = number
        return Decimal(1 if value != 0 else 0)
    if kind in ("int", "bigint", "smallint", "tinyint", "decimal", "numeric", "money", "smallmoney", "float", "real"):
        number = _number(value)
        if number is UNKNOWN:
            raise ScenarioError(f"cannot convert {value!r} to {kind}")
        return number
    if kind in ("char", "varchar", "nchar", "nvarchar", "sysname"):
        return value if isinstance(value, str) else format(value, "f")
    return UNKNOWN      # dates and other types: compared only as unknown


def declared_types(header: str) -> dict[str, str]:
    masked = mask_noncode(header)
    return {m.group(1).lower(): m.group(2).lower() for m in
            re.finditer(r"(@[\w$#@]+)\s+(?:AS\s+)?\[?([A-Za-z_]\w*)", masked)}


# ------------------------------------------------------------------------------------------- the walk
@dataclass
class Exclusion:
    start: int
    end: int
    reason: str


@dataclass
class ProcedureScenario:
    """The outcome for one procedure definition."""
    scope: str
    unit: object
    name: str
    line: int
    values: dict[str, object]
    texts: dict[str, str]
    exclusions: list[Exclusion] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)        # conditions kept with both arms, with why
    changed: dict[str, str] = field(default_factory=dict)     # parameter -> where it becomes unknown
    notes: list[str] = field(default_factory=list)
    body: tuple[int, int] = (0, 0)
    reachable: list[tuple[int, int]] = field(default_factory=list)   # spans of statements that can run

    def excluded(self, start: int) -> str | None:
        for item in self.exclusions:
            if item.start <= start < item.end:
                return item.reason
        return None


WRITE_PATTERNS = (
    r"\bSET\s+{p}\s*[-+*/%&|^]?=",          # SET @p = / += ...
    r"{p}\s+OUT(?:PUT)?\b",                 # passed as an OUTPUT argument
    r"\bINTO\s+(?:[^;]*?,\s*)?{p}(?![\w$#@])",   # FETCH ... INTO @a, @p
    r"\bEXEC(?:UTE)?\s+{p}\s*=",            # EXEC @p = proc (return code)
)


class _Walker:
    def __init__(self, result: ProcedureScenario, text: str, masked: str, statements, line_at):
        self.r = result
        self.text = text
        self.masked = masked
        self.statements = statements      # indexed statements of the body: start -> Statement
        self.line_at = line_at
        self.context: list[str] = []      # decided conditions on the way to the current statement
        self.after_return = "after a RETURN that always runs"

    def line(self, offset: int) -> int:
        return self.line_at(offset)

    def assigned(self, start: int, end: int, env: dict) -> set[str]:
        """Parameters a statement may assign (conservative: any doubt counts as an assignment)."""
        out = set()
        text = self.masked[start:end]
        for name in env:
            p = re.escape(name)
            if not re.search(rf"(?<![\w$#@]){p}(?![\w$#@])", text, re.I):
                continue
            statement = self.statements.get(start)
            tree = statement.tree if statement is not None and statement.parse_status == "parsed" else None
            if tree is None or isinstance(tree, exp.Command):
                out.add(name)       # not parsed: cannot tell a read from a write
                continue
            if any(re.search(pattern.format(p=p), text, re.I) for pattern in WRITE_PATTERNS):
                out.add(name)
                continue
            for node in tree.find_all(exp.Parameter):
                if node.sql(dialect="tsql").lower() != name:
                    continue
                parent = node.parent
                if isinstance(parent, exp.EQ) and parent.this is node and \
                        isinstance(parent.parent, (exp.Select, exp.Update, exp.SetItem, exp.Set, exp.Merge)):
                    out.add(name)
        return out

    def assigned_in(self, item, env: dict) -> set[str]:
        found = set()
        for start, end in self.spans(item):
            found |= self.assigned(start, end, env)
        return found

    def spans(self, item):
        if item is None:
            return
        if isinstance(item, Stmt):
            yield item.start, item.end
        elif isinstance(item, If):
            yield from self.spans(item.then)
            yield from self.spans(item.other)
        elif isinstance(item, Block):
            for x in item.items:
                yield from self.spans(x)
        elif isinstance(item, TryCatch):
            yield from self.spans(item.body)
            yield from self.spans(item.catch)

    def exclude(self, item, reason: str) -> None:
        for start, end in self.spans(item):
            self.r.exclusions.append(Exclusion(start, end, reason))

    def unknown(self, env: dict, names, where: str) -> None:
        for name in names:
            if env.get(name, UNKNOWN) is not UNKNOWN or name in self.r.values:
                self.r.changed.setdefault(name, where)
            env[name] = UNKNOWN

    def walk(self, item, env: dict) -> tuple[bool, dict]:
        """Walk a reachable item; returns (whether control can continue after it, environment after)."""
        if item is None:
            return True, env
        if isinstance(item, Stmt):
            self.r.reachable.append((item.start, item.end))
            names = self.assigned(item.start, item.end, env)
            if names:
                statement = self.statements.get(item.start)
                value = UNKNOWN
                if statement is not None and isinstance(statement.tree, exp.Set) and len(names) == 1:
                    eq = statement.tree.expressions[0] if statement.tree.expressions else None
                    eq = eq.this if isinstance(eq, exp.SetItem) else eq
                    if isinstance(eq, exp.EQ) and isinstance(eq.this, exp.Parameter):
                        value = evaluate(eq.expression, env)
                name = next(iter(names))
                if value is not UNKNOWN and len(names) == 1:
                    env = dict(env)
                    env[name] = value
                    self.r.changed.setdefault(name, f"set at line {self.line(item.start)}")
                else:
                    env = dict(env)
                    self.unknown(env, names, f"may be changed at line {self.line(item.start)}")
            if item.word == "RETURN":
                self.after_return = (f"after the RETURN at line {self.line(item.start)}"
                                     + (f", reached because {self.context[-1]}" if self.context else ""))
                return False, env
            return True, env
        if isinstance(item, Block):
            alive = True
            for i, child in enumerate(item.items):
                if not alive:
                    self.exclude(Block(item.items[i:]), self.after_return)
                    break
                alive, env = self.walk(child, env)
            return alive, env
        if isinstance(item, TryCatch):
            changed = self.assigned_in(item, env)
            inner = dict(env)
            self.unknown(inner, changed, "changed inside a TRY/CATCH block")
            alive_try, _ = self.walk(item.body, dict(inner))
            alive_catch, _ = self.walk(item.catch, dict(inner)) if item.catch is not None else (False, inner)
            return alive_try or alive_catch, inner
        if isinstance(item, If):
            label = f"{'WHILE' if item.loop else 'IF'} {_collapse(item.condition, 120)}"
            at = self.line(item.start)
            if item.loop:
                changed = self.assigned_in(item.then, env)
                env = dict(env)
                self.unknown(env, changed, f"changed inside the loop at line {at}")
                truth = condition_truth(item.condition, env)
                if truth is False:
                    self.exclude(item.then, f"the body of '{label}' (line {at}), false with {self.values_text()}")
                else:
                    if truth is UNKNOWN:
                        self.r.uncertain.append(f"'{label}' (line {at}): {self.why_unknown(item.condition, env)}")
                    self.walk(item.then, dict(env))
                return True, env
            truth = condition_truth(item.condition, env)
            if truth is UNKNOWN:
                self.r.uncertain.append(f"'{label}' (line {at}): {self.why_unknown(item.condition, env)}; both "
                                        "arms are kept")
                alive_then, env_then = self.walk(item.then, dict(env))
                alive_else, env_else = self.walk(item.other, dict(env))
                if not (alive_then or alive_else):
                    self.after_return = f"after '{label}' (line {at}), every arm of which returns"
                return alive_then or alive_else, self.merge(env_then if alive_then else None,
                                                             env_else if alive_else else None, env)
            taken, skipped = (item.then, item.other) if truth else (item.other, item.then)
            arm = "THEN" if not truth else "ELSE"
            if skipped is not None:
                self.exclude(skipped, f"the {arm} arm of '{label}' (line {at}), "
                                      f"{'true' if truth else 'false'} with {self.values_text()}")
            self.context.append(f"'{label}' (line {at}) is {'true' if truth else 'false'} with "
                                f"{self.values_text()}")
            try:
                alive, env = self.walk(taken, dict(env))
            finally:
                self.context.pop()
            return alive, env
        return True, env

    @staticmethod
    def merge(a: dict | None, b: dict | None, before: dict) -> dict:
        if a is None and b is None:
            return before
        if a is None or b is None:
            return dict(a if a is not None else b)
        return {k: (a.get(k) if a.get(k, UNKNOWN) == b.get(k, UNKNOWN) and a.get(k, UNKNOWN) is not UNKNOWN
                    else UNKNOWN) for k in set(a) | set(b)}

    def values_text(self) -> str:
        return ", ".join(f"{k} = {v}" for k, v in self.r.texts.items())

    def why_unknown(self, condition: str, env: dict) -> str:
        names = {m.group().lower() for m in re.finditer(r"(?<![\w$#@])@[\w$#@]+", mask_noncode(condition))}
        unset = sorted(n for n in names if env.get(n, UNKNOWN) is UNKNOWN)
        changed = [n for n in unset if n in self.r.changed]
        if changed:
            return f"{', '.join(changed)} may have changed before it ({self.r.changed[changed[0]]})"
        if unset:
            return f"it depends on {', '.join(unset)}, which the scenario does not set"
        return "it reads more than the scenario's parameters (columns, functions or other expressions)"


def analyse(catalog, proc, scenario: Scenario) -> ProcedureScenario:
    """Reachability of one procedure definition's statements under the scenario."""
    unit = proc.unit
    masked = unit.masked
    procs = [m for m in DEFINITION.finditer(masked) if m.group("kind").lower().startswith("proc")]
    stops = sorted([m.start() for m in GO.finditer(masked)] + [m.start() for m in procs] + [len(masked)])
    limit = next((s for s in stops if s > proc.start), len(masked))
    match = next(m for m in procs if m.start() == proc.start)
    body = header_end(masked, match.end(), limit)
    if body is None:
        raise ScenarioError(f"the header of {proc.ref.display} could not be read")
    header = unit.text[match.end():body - 2]
    types = declared_types(header)
    values = {}
    for name, value in scenario.values.items():
        try:
            values[name] = _coerce(value, types.get(name))
        except ScenarioError as exc:
            raise ScenarioError(f"--scenario {scenario.procedure}: {name}: {exc}") from None
    result = ProcedureScenario(proc.scope, unit, proc.ref.display, proc.line, values, dict(scenario.texts),
                               body=(body, limit))
    statements = {s.start: s for s in unit.index.statements if body <= s.start < limit}
    ends = {s.start: s.end for s in unit.index.statements}
    if re.search(r"\bGOTO\b", masked[body:limit], re.I):
        result.notes.append("The procedure uses GOTO, whose jumps are not followed: no statement is excluded.")
        return result
    tree = _Tree(unit.text, masked, ends)
    try:
        block, _ = tree.block(body, limit, None)
    except RecursionError:
        result.notes.append("The control flow could not be followed: no statement is excluded.")
        return result
    walker = _Walker(result, unit.text, masked, statements, unit.line_at)
    walker.walk(block, {name: value for name, value in values.items()})
    for name, value in values.items():
        if value is UNKNOWN:
            result.notes.append(f"{name}: the value {scenario.texts[name]} is not compared (its declared type is not "
                                "handled); conditions on it keep both arms.")
    calls = re.findall(r"\bEXEC(?:UTE)?\s+(?!@|\()([\w.\[\]]+)", masked[body:limit], re.I)
    if calls:
        result.notes.append("Scenario values are not carried into called procedures ("
                            + ", ".join(sorted({c.lower() for c in calls})[:5]) + "): their branches are all kept.")
    return result


class ScenarioSet:
    """Every scenario of the run, applied to the catalog."""

    def __init__(self, results: list[ProcedureScenario]):
        self.results = results
        self.by_scope = {r.scope: r for r in results}
        self.removed_writers: dict[str, list] = {}       # object key -> writers excluded
        self.removed: list[dict] = []                    # every excluded statement, for the pages
        self.calls: dict[str, list[dict]] = {}           # scope -> procedures it calls on reachable lines

    def excluded(self, unit, statement) -> str | None:
        result = self.by_scope.get(statement.scope)
        if result is None or result.unit.path != unit.path:
            return None
        return result.excluded(statement.start)

    def to_dict(self) -> dict:
        return {
            "note": "Assumed parameter values for this analysis only; they are not evidence of what ran in "
                    "production. Statements the values make unreachable are excluded; uncertain branches are kept. "
                    "Run without --scenario for every statically possible branch.",
            "procedures": [{
                "procedure": r.name, "definition": f"{r.unit.root_label}/{r.unit.rel_path}:{r.line}",
                "values": dict(r.texts),
                "excluded_statements": [x for x in self.removed if x["procedure_scope"] == r.scope],
                "uncertain_conditions": list(dict.fromkeys(r.uncertain)),
                "parameters_changed_in_procedure": dict(r.changed),
                "calls": list(self.calls.get(r.scope, [])),
                "notes": list(r.notes)} for r in self.results],
        }


def apply(catalog, scenarios: list[Scenario]) -> ScenarioSet:
    """Analyse each scenario's procedure and remove what its values make unreachable from the catalog."""
    from .catalog import ObjectRef, split_name
    results = []
    for scenario in scenarios:
        parts = [p.lower() for p in split_name(scenario.procedure)]
        parts = [None] * (3 - len(parts[-3:])) + parts[-3:]
        procs = catalog.procs_for(ObjectRef(parts[2], parts[1] or catalog.default_schema, parts[0], None, "procedure"))
        if not procs:
            raise ScenarioError(f"--scenario: procedure {scenario.procedure} is not defined in the scanned files")
        for proc in procs:
            unknown = sorted(set(scenario.values) - set(proc.params))
            if unknown:
                raise ScenarioError(f"--scenario: {proc.ref.display} has no parameter {', '.join(unknown)} "
                                    f"(its parameters: {', '.join(proc.params) or 'none'})")
            results.append(analyse(catalog, proc, scenario))
    applied = ScenarioSet(results)

    def describe(unit, statement, reason, what):
        result = applied.by_scope[statement.scope]
        applied.removed.append({"procedure_scope": result.scope, "procedure": result.name,
                                "path": f"{unit.root_label}/{unit.rel_path}", "line": statement.line,
                                "statement": _collapse(statement.text, 160), "what": what, "reason": reason})

    seen = set()
    for key, writers in list(catalog.writers.items()):
        kept = []
        for writer in writers:
            reason = applied.excluded(writer.unit, writer.statement)
            if reason is None:
                kept.append(writer)
                continue
            applied.removed_writers.setdefault(key, []).append((writer, reason))
            if id(writer.statement) not in seen:
                seen.add(id(writer.statement))
                describe(writer.unit, writer.statement, reason, f"writes {writer.ref.display}")
                applied.removed[-1]["writes"] = [writer.ref.key]
            else:
                entry = next(x for x in applied.removed if x["line"] == writer.statement.line
                             and x["path"] == f"{writer.unit.root_label}/{writer.unit.rel_path}")
                entry.setdefault("writes", []).append(writer.ref.key)
        catalog.writers[key] = kept
    for target, issues in list(catalog.issues_by_target.items()):
        kept = []
        for issue in issues:
            reason = applied.excluded(issue.unit, issue.statement)
            if reason is None:
                kept.append(issue)
            elif id(issue.statement) not in seen:
                seen.add(id(issue.statement))
                describe(issue.unit, issue.statement, reason, f"{issue.kind} issue")
        catalog.issues_by_target[target] = kept
    for key, assignments in list(catalog.assignments.items()):
        kept = []
        for assignment in assignments:
            reason = applied.excluded(assignment.unit, assignment.statement)
            if reason is None:
                kept.append(assignment)
            elif id(assignment.statement) not in seen:
                seen.add(id(assignment.statement))
                describe(assignment.unit, assignment.statement, reason, f"assigns {assignment.name}")
        catalog.assignments[key] = kept
    for key, calls in list(catalog.calls_by_callee.items()):
        kept = []
        for call in calls:
            reason = applied.excluded(call.unit, call.statement)
            if reason is None:
                kept.append(call)
            elif id(call.statement) not in seen:
                seen.add(id(call.statement))
                describe(call.unit, call.statement, reason,
                         f"calls {call.callee.display} (its own writers are kept: it may run from elsewhere)")
        catalog.calls_by_callee[key] = kept
        for call in kept:
            if call.statement.scope in applied.by_scope:
                applied.calls.setdefault(call.statement.scope, []).append(
                    {"procedure": call.callee.display, "line": call.statement.line})
    applied.removed.sort(key=lambda x: (x["path"], x["line"]))
    return applied


def reads_of(catalog, applied: ScenarioSet, ref) -> list[dict]:
    """Reachable statements of a scenario procedure that read ``ref`` without writing it (e.g. a SELECT
    returning the existing rows)."""
    out = []
    for result in applied.results:
        unit = result.unit
        for statement in unit.index.statements:
            if statement.scope != result.scope or result.excluded(statement.start) is not None:
                continue
            tree = statement.tree
            if not isinstance(tree, exp.Expression) or isinstance(tree, (exp.Insert, exp.Update, exp.Merge, exp.Delete)):
                continue
            if isinstance(tree, exp.Select) and tree.args.get("into"):
                continue
            for table in tree.find_all(exp.Table):
                try:
                    found = catalog.ref_from_table(table, unit, statement)
                except Exception:   # noqa: BLE001 - a table name that cannot be resolved is not this object
                    continue
                if catalog.canonical(found)[0].key == ref.key or found.key == ref.key:
                    out.append({"procedure": result.name, "path": f"{unit.root_label}/{unit.rel_path}",
                                "line": statement.line, "statement": _collapse(statement.text, 120)})
                    break
    return out
