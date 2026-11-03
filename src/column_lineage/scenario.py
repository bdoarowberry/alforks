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

from .control import STATEMENT_START, _collapse, _Parser, header_end

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
    if NUMBER.fullmatch(raw):
        value = Decimal(raw)
        return Decimal(0) if value == 0 else value      # -0 is 0 in T-SQL
    return raw


NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


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
    span: tuple[int, int] = (0, 0)       # the condition's text


@dataclass
class Block:
    items: list
    kind: str | None = None              # None, "TRY" or "CATCH"


@dataclass
class TryCatch:
    body: Block
    catch: Block | None


STRUCTURAL_BEGIN = ("TRAN", "TRANSACTION", "DISTRIBUTED", "DIALOG", "CONVERSATION")
OBJECT_WORDS = {"TABLE", "VIEW", "PROCEDURE", "PROC", "FUNCTION", "INDEX", "TRIGGER", "SCHEMA", "DATABASE",
                "SEQUENCE", "TYPE", "SYNONYM", "STATISTICS", "USER", "ROLE", "COLUMN", "CONSTRAINT", "DEFAULT"}
STATEMENT_WORDS = STATEMENT_START | {"ELSE", "END", "GOTO", "BREAK", "CONTINUE", "RETURN", "WHILE", "IF"}


class _Tree(_Parser):
    """The control structure of one procedure body (IF/ELSE, WHILE, BEGIN...END, TRY/CATCH).

    A statement ends at the first of: the start of the next indexed statement, a top-level ';', a
    statement keyword outside parentheses and CASE...END, and (for a statement the index does not know,
    such as PRINT or TRUNCATE) a line end outside parentheses and CASE. Splitting a statement in two is
    harmless here; letting one swallow an END, ELSE or WHILE would misplace the statements after it."""

    def __init__(self, text, masked, ends, starts):
        super().__init__(text, masked, ends)
        self.starts = starts          # offsets where indexed statements begin

    def simple_end(self, i: int, end: int) -> int:
        known = self.ends.get(i)
        indexed = known is not None and known > i
        limit = min(known, end) if indexed else end
        first = self.word_at(i)
        j = first[1] if first else i + 1
        depth = case = 0
        previous = first[0] if first else None
        while j < limit:
            char = self.masked[j]
            if char in "[\"":
                close = self.masked.find("]" if char == "[" else '"', j + 1, limit)
                j = close + 1 if close > j else limit       # [End], "Select": identifiers, not keywords
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
            elif char == ";" and depth == 0:
                return j + 1
            elif char == "\n" and not indexed and depth == 0 and case == 0:
                return j + 1
            elif j in self.starts and j > i:
                return j
            elif char.isalpha() or char == "_":
                word = self.word_at(j)
                if word:
                    name, after = word
                    if name == "CASE":
                        case += 1
                    elif name == "END" and case:
                        case -= 1
                    elif depth == 0 and not case and j > i and (
                            name in ("IF", "WHILE", "ELSE", "END", "BEGIN", "RETURN", "GOTO", "BREAK", "CONTINUE")
                            or (not indexed and name in STATEMENT_WORDS)) and \
                            not (name == "IF" and previous in OBJECT_WORDS) and \
                            not (name == "END" and previous == "BEGIN"):
                        return j
                    previous = name
                    j = after
                    continue
            j += 1
        return limit

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
            return If(i, self.text[after:cond_end], then, other, name == "WHILE", (after, cond_end)), body_end
        if name == "BEGIN":
            j = self.skip_ws(after, end)
            nxt = self.word_at(j)
            if nxt and nxt[0] in STRUCTURAL_BEGIN:
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
            nxt_word = self.word_at(self.skip_ws(word[1], end)) if word and word[0] == "END" else None
            if word and word[0] == "END" and not (nxt_word and nxt_word[0] == "CONVERSATION"):
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


ANSI = "\x00ansi_nulls"    # environment key: True when SET ANSI_NULLS ON is known to apply


def _compare(op: str, left, right, ansi: bool = True):
    """Three-valued comparison: True, False, None (NULL) or UNKNOWN (cannot be settled)."""
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    if left is NULL or right is NULL:
        if op in ("=", "<>") and not ansi:
            return UNKNOWN            # with ANSI_NULLS OFF, = NULL and <> NULL compare as values
        return NULL
    if isinstance(left, str) and isinstance(right, str):
        a, b = left.rstrip(" "), right.rstrip(" ")
        if op in ("=", "<>"):
            if a == b:
                return op == "="
            if not (a.isascii() and b.isascii()):
                return UNKNOWN        # accent, width or expansion rules of the collation are unknown
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
        return _compare(COMPARISONS[type(node)], evaluate(node.this, env), evaluate(node.expression, env),
                        env.get(ANSI, False))
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
            result = _or(result, _compare("=", left, evaluate(item, env), env.get(ANSI, False)))
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


INTEGERS = {"tinyint": (0, 255), "smallint": (-2 ** 15, 2 ** 15 - 1), "int": (-2 ** 31, 2 ** 31 - 1),
            "bigint": (-2 ** 63, 2 ** 63 - 1)}
STRINGS = {"char", "varchar", "nchar", "nvarchar", "sysname"}


def _coerce(value, declared: tuple | None):
    """A value converted as SQL Server converts it to the declared type (base, length or precision, scale).
    Raises ScenarioError when SQL Server would fail; returns UNKNOWN for types not compared exactly."""
    from decimal import ROUND_DOWN, ROUND_HALF_UP
    if value is NULL or value is UNKNOWN or not declared:
        return value
    kind, size, scale = declared
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
    if kind in INTEGERS:
        if isinstance(value, str):
            if not re.fullmatch(r"\s*[+-]?\d+\s*", value):
                raise ScenarioError(f"cannot convert {value!r} to {kind}")
            number = Decimal(value.strip())
        else:
            number = value.to_integral_value(rounding=ROUND_DOWN)    # a decimal converted to int is truncated
        low, high = INTEGERS[kind]
        if not low <= number <= high:
            raise ScenarioError(f"{value} is out of range for {kind}")
        return Decimal(0) if number == 0 else number
    if kind in ("decimal", "numeric", "money", "smallmoney"):
        number = _number(value)
        if number is UNKNOWN:
            raise ScenarioError(f"cannot convert {value!r} to {kind}")
        places = 4 if kind in ("money", "smallmoney") else (scale or 0)
        return number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if kind in STRINGS:
        text = value if isinstance(value, str) else format(value, "f")
        length = 128 if kind == "sysname" else size
        return text[:length] if length else text
    return UNKNOWN      # float, real, dates and other types: not compared exactly


def declared_types(header: str) -> dict[str, tuple]:
    """@param -> (type, length or precision, scale); a parameter's char/varchar without a length is 1 long,
    (max) has no limit."""
    masked = mask_noncode(header)
    out = {}
    for m in re.finditer(r"(@[\w$#@]+)\s+(?:AS\s+)?\[?([A-Za-z_]\w*)\]?\s*(?:\(\s*(\w+)\s*(?:,\s*(\d+)\s*)?\))?",
                         masked):
        kind = m.group(2).lower()
        size = m.group(3)
        if size is None:
            size = 1 if kind in ("char", "varchar", "nchar", "nvarchar") else None
        elif size.lower() == "max":
            size = None
        else:
            size = int(size) if size.isdigit() else None
        out[m.group(1).lower()] = (kind, size, int(m.group(4)) if m.group(4) else None)
    return out


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

    applied: bool = True          # False when the scenario could not be applied (nothing is excluded)

    def reached(self, statement) -> bool:
        """The walk reached this statement on a path the values allow (positive evidence that it can run)."""
        if not self.applied:
            return False
        start = anchor(self.unit.masked, statement)
        return self.excluded(statement.start, statement) is None and \
            any(a <= start < b for a, b in self.reachable)

    def excluded(self, start: int, statement=None) -> str | None:
        start = anchor(self.unit.masked, statement) if statement is not None else code_start(self.unit.masked, start)
        for item in self.exclusions:
            if item.start <= start < item.end:
                return item.reason
        return None


DML_WORDS = {exp.Insert: ("INSERT",), exp.Update: ("UPDATE",), exp.Merge: ("MERGE",), exp.Delete: ("DELETE",),
             exp.Select: ("SELECT", "WITH"), exp.Union: ("SELECT", "WITH"), exp.Execute: ("EXEC", "EXECUTE")}
CONTROL_WORDS = ("RETURN", "IF", "ELSE", "WHILE", "END", "BEGIN", "GOTO", "BREAK", "CONTINUE")


def anchor(masked: str, statement) -> int:
    """Where a statement's own code begins: its first code character, or, when the index merged a leading
    control keyword into a DML statement (IF @p = 1 RETURN INSERT ...), the DML statement's own keyword."""
    start = code_start(masked, statement.start)
    first = re.match(r"[A-Za-z_]+", masked[start:start + 20])
    words = DML_WORDS.get(type(statement.tree)) if statement.tree is not None and statement.parse_status == "parsed" \
        else ("INSERT", "UPDATE", "MERGE", "DELETE", "SELECT", "WITH", "EXEC", "EXECUTE")   # unparsed: any of them
    if first and first.group().upper() in CONTROL_WORDS and words:
        found = re.compile(r"(?<![\w@#$.\[])(" + "|".join(words) + r")\b", re.I).search(masked, start, statement.end)
        if found:
            return found.start()
    return start


def code_start(masked: str, i: int) -> int:
    """The first code character at or after ``i``: the index may start a statement at the beginning of its
    line (indentation, or the line after a trailing comment), the walk at its first word."""
    while i < len(masked) and masked[i].isspace():
        i += 1
    return i


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
        self.types: dict[str, tuple] = {}
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
            if name == ANSI:
                continue
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
                    try:
                        value = _coerce(value, self.types.get(name))
                    except ScenarioError:
                        value = UNKNOWN          # the SET fails at run time: nothing is settled
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
            # The TRY body runs in order with the current values. An error can stop it after any statement,
            # so the CATCH sees, for each parameter the TRY may assign, any value it had on the way: unknown.
            reached = len(self.r.reachable)
            alive_try, env_try = self.walk(item.body, dict(env))
            catch_env = dict(env)
            changed = set()
            for start, end in self.r.reachable[reached:]:     # assignments that can run, not excluded ones
                changed |= self.assigned(start, end, env)
            for name in changed:
                catch_env[name] = UNKNOWN
            if item.catch is None:
                return alive_try, env_try
            catch_changed = self.assigned_in(item.catch, catch_env)
            alive_catch, env_catch = self.walk(item.catch, catch_env)
            if changed or catch_changed:
                for name in changed | catch_changed:
                    self.r.changed.setdefault(name, "changed inside a TRY/CATCH block")
            return alive_try or alive_catch, self.merge(env_try if alive_try else None,
                                                         env_catch if alive_catch else None, env)
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
        return ("the values do not settle it: it reads more than the supplied parameters (columns, functions, other "
                "expressions), or the comparison depends on the collation, the ANSI_NULLS setting or a type the "
                "scenario does not compare")


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
    statements = {anchor(masked, s): s for s in unit.index.statements if body <= s.start < limit}
    ends = {anchor(masked, s): s.end for s in unit.index.statements}
    if re.search(r"\bGOTO\b", masked[body:limit], re.I):
        result.notes.append("The procedure uses GOTO, whose jumps are not followed: no statement is excluded.")
        result.applied = False
        return result
    tree = _Tree(unit.text, masked, ends, set(statements))
    try:
        block, _ = tree.block(body, limit, None)
    except RecursionError:
        result.notes.append("The control flow could not be followed: no statement is excluded.")
        result.applied = False
        return result
    # Sanity net: every indexed statement must begin one of the walked statements, and none may begin with
    # a control keyword (the index merged it with the statement after it). Otherwise exclude nothing.
    walked = {start for start, _ in _Walker(result, unit.text, masked, statements, unit.line_at).spans(block)}
    conditions = []
    heads = set()          # IF / WHILE keywords: the index may record "WHILE @n < 2" as a statement

    def collect(item):
        if isinstance(item, If):
            conditions.append(item.span)
            heads.add(item.start)
            collect(item.then)
            collect(item.other)
        elif isinstance(item, Block):
            for x in item.items:
                collect(x)
        elif isinstance(item, TryCatch):
            collect(item.body)
            collect(item.catch)
    collect(block)
    inside_condition = {start for start in statements if any(a <= start < b for a, b in conditions)}
    misplaced = [start for start, s in statements.items()
                 if (start not in walked and start not in inside_condition and start not in heads)
                 or ((tree.word_at(start) or ("",))[0] in ("RETURN", "IF", "ELSE", "WHILE", "END", "BEGIN", "GOTO")
                     and isinstance(s.tree, (exp.Insert, exp.Update, exp.Merge, exp.Delete, exp.Select, exp.Execute)))]
    if misplaced:
        first = statements[misplaced[0]]
        kind = type(first.tree).__name__ if first.tree is not None else first.parse_status
        word = (tree.word_at(misplaced[0]) or ("?",))[0]
        result.notes.append(f"The control flow around line {unit.line_at(misplaced[0])} could not be lined up "
                            f"with the statements (an indexed {kind} statement starting with {word} does not begin "
                            "a walked statement): no statement is excluded.")
        result.applied = False
        return result
    settings = list(re.finditer(r"\bSET\s+ANSI_NULLS\s+(ON|OFF)\b", masked[:proc.start], re.I))
    ansi = bool(settings) and settings[-1].group(1).upper() == "ON"
    if not ansi and any(v is NULL for v in values.values()):
        result.notes.append("SET ANSI_NULLS ON is not seen before the procedure, so = NULL and <> NULL comparisons "
                            "are left undecided.")
    walker = _Walker(result, unit.text, masked, statements, unit.line_at)
    walker.types = types
    env = {name: value for name, value in values.items()}
    env[ANSI] = ansi
    walker.walk(block, env)
    for name, value in values.items():
        if value is UNKNOWN:
            result.notes.append(f"{name}: the value {scenario.texts[name]} is not compared (its declared type is not "
                                "handled); conditions on it keep both arms.")
    return result


class ScenarioSet:
    """Every scenario of the run, applied to the catalog."""

    def __init__(self, results: list[ProcedureScenario]):
        self.results = results
        self.by_scope = {r.scope: r for r in results}
        self.removed_writers: dict[str, list] = {}       # object key -> writers excluded
        self.removed: list[dict] = []                    # every excluded statement, for the pages
        self.calls: dict[str, list[dict]] = {}           # scope -> procedures it calls on reachable lines
        self.invocation: dict[str, list[dict]] = {}      # scope -> procedures this invocation can run
        self.excluded_callees: dict[str, list[str]] = {} # scope -> procedures called only from excluded lines
        self.reads: dict[str, list[dict]] = {}           # scope -> objects read by statements that can run
        self.placement: dict[str, dict] = {}             # scope -> statements reached / excluded / neither

    def excluded(self, unit, statement) -> str | None:
        result = self.by_scope.get(statement.scope)
        if result is None or result.unit.path != unit.path:
            return None
        return result.excluded(statement.start, statement)

    def to_dict(self) -> dict:
        return {
            "note": "Assumed parameter values for this analysis only; they are not evidence of what ran in "
                    "production. Statements the values make unreachable are excluded; uncertain branches are kept. "
                    "Run without --scenario for every statically possible branch.",
            "procedures": [{
                "procedure": r.name, "definition": f"{r.unit.root_label}/{r.unit.rel_path}:{r.line}",
                "applied": r.applied,
                "values": dict(r.texts),
                "excluded_statements": [x for x in self.removed if x["procedure_scope"] == r.scope],
                "uncertain_conditions": list(dict.fromkeys(r.uncertain)),
                "parameters_changed_in_procedure": dict(r.changed),
                "calls": list(self.calls.get(r.scope, [])),
                "invocation": list(self.invocation.get(r.scope, [])),
                "called_only_from_excluded_branches": list(self.excluded_callees.get(r.scope, [])),
                "reads": list(self.reads.get(r.scope, [])),
                "statements": dict(self.placement.get(r.scope, {})),
                "notes": list(r.notes)} for r in self.results],
        }


def apply(catalog, scenarios: list[Scenario]) -> ScenarioSet:
    """Analyse each scenario's procedure and remove what its values make unreachable from the catalog."""
    from .catalog import ObjectRef, split_name
    catalog.evaluate_dynamic()      # its writers are placed at their EXEC, so they are filtered too
    results = []
    seen_procs: dict[str, str] = {}
    for scenario in scenarios:
        parts = [p.lower() for p in split_name(scenario.procedure)]
        parts = [None] * (3 - len(parts[-3:])) + parts[-3:]
        procs = catalog.procs_for(ObjectRef(parts[2], parts[1] or catalog.default_schema, parts[0], None, "procedure"))
        if not procs:
            raise ScenarioError(f"--scenario: procedure {scenario.procedure} is not defined in the scanned files")
        for proc in procs:
            if proc.scope in seen_procs:
                raise ScenarioError(f"--scenario: {proc.ref.display} is given twice ({seen_procs[proc.scope]!r} and "
                                    f"{scenario.procedure!r}); run each scenario separately")
            seen_procs[proc.scope] = scenario.procedure
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
                describe(assignment.unit, assignment.statement, reason, f"assigns {assignment.variable}")
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
            result = applied.by_scope.get(call.statement.scope)
            if result is not None and result.unit.path == call.unit.path and result.reached(call.statement):
                applied.calls.setdefault(call.statement.scope, []).append(
                    {"procedure": call.callee.display, "line": call.statement.line})
    applied.removed.sort(key=lambda x: (x["path"], x["line"]))
    # The procedures this invocation can run: the scenario procedure and, transitively, what it calls on
    # lines that can run (callees are not constrained, so all their calls count).
    by_caller: dict[str, list] = {}
    for calls in catalog.calls_by_callee.values():
        for call in calls:
            owner = applied.by_scope.get(call.statement.scope)
            if owner is not None and not owner.reached(call.statement):
                continue        # a scenario procedure's call counts only where the walk reached it
            by_caller.setdefault(call.statement.scope, []).append(call)
    for result in applied.results:
        found = [{"procedure": result.name, "how": "the scenario's entry procedure"}]
        seen_scopes, frontier = {result.scope}, [result.scope]
        while frontier:
            scope = frontier.pop(0)
            for call in sorted(by_caller.get(scope, []), key=lambda c: c.statement.start):
                for proc in catalog.procs_for(call.callee):
                    if proc.scope in seen_scopes:
                        continue
                    seen_scopes.add(proc.scope)
                    frontier.append(proc.scope)
                    caller = catalog.proc_by_scope.get(scope)
                    found.append({"procedure": proc.ref.display,
                                  "how": f"called at line {call.statement.line}"
                                         + (f" by {caller.ref.display}" if caller and scope != result.scope else "")})
        if not result.applied:
            found = found[:1]
        applied.invocation[result.scope] = found
        applied.reads[result.scope] = reads_in(catalog, result)
        callees = sorted({c["procedure"] for c in applied.calls.get(result.scope, [])})
        if callees:
            result.notes.append("Scenario values are not carried into called procedures ("
                                + ", ".join(callees[:5]) + (", ..." if len(callees) > 5 else "")
                                + "): their branches are all kept.")
        body = [s for s in result.unit.index.statements
                if s.scope == result.scope and result.body[0] <= s.start < result.body[1]]
        unplaced = [s.line for s in body if result.excluded(s.start, s) is None and not result.reached(s)]
        applied.placement[result.scope] = {
            "reached": sum(1 for s in body if result.reached(s)),
            "excluded": sum(1 for s in body if result.excluded(s.start, s) is not None),
            "neither_lines": unplaced[:20]}
        names = {f["procedure"] for f in found}
        applied.excluded_callees[result.scope] = sorted({
            x["what"].split(" ", 2)[1] for x in applied.removed
            if x["procedure_scope"] == result.scope and x["what"].startswith("calls ")} - names)
    return applied


def reads_in(catalog, result: ProcedureScenario) -> list[dict]:
    """Objects read (not written) by statements of the scenario procedure that can run: {object, line}."""
    out, seen = [], set()
    unit = result.unit
    for statement in unit.index.statements:
        if statement.scope != result.scope or not result.reached(statement):
            continue
        tree = statement.tree
        if not isinstance(tree, (exp.Select, exp.Union, exp.Insert, exp.Update, exp.Merge, exp.Delete)):
            continue
        target = tree.this if isinstance(tree, (exp.Insert, exp.Update, exp.Merge, exp.Delete)) else tree.args.get("into")
        target = target.this if isinstance(target, (exp.Schema, exp.Into)) else target
        for table in tree.find_all(exp.Table):
            if table is target:
                continue
            try:
                ref = catalog.canonical(catalog.ref_from_table(table, unit, statement))[0]
            except Exception:   # noqa: BLE001 - a name that cannot be resolved is left out
                continue
            if (ref.key, statement.line) not in seen:
                seen.add((ref.key, statement.line))
                out.append({"object": ref.key, "display": ref.display, "line": statement.line})
    return out


def reads_of(catalog, applied: ScenarioSet, ref) -> list[dict]:
    """Reachable statements of a scenario procedure that read ``ref`` without writing it (e.g. a SELECT
    returning the existing rows)."""
    out = []
    for result in applied.results:
        unit = result.unit
        for statement in unit.index.statements:
            if statement.scope != result.scope or not result.reached(statement):
                continue
            tree = statement.tree
            if not isinstance(tree, (exp.Select, exp.Union, exp.Insert, exp.Update, exp.Merge, exp.Delete)):
                continue        # CREATE, DROP, TRUNCATE, EXEC ...: not a read of the rows
            # The table a DML statement writes is its target, not a read (unless it also appears as a source).
            target = tree.this if isinstance(tree, (exp.Insert, exp.Update, exp.Merge, exp.Delete)) else \
                tree.args.get("into")
            target = target.this if isinstance(target, (exp.Schema, exp.Into)) else target
            for table in tree.find_all(exp.Table):
                if table is target:
                    continue
                try:
                    found = catalog.ref_from_table(table, unit, statement)
                except Exception:   # noqa: BLE001 - a table name that cannot be resolved is not this object
                    continue
                if catalog.canonical(found)[0].key == ref.key or found.key == ref.key:
                    out.append({"procedure": result.name, "path": f"{unit.root_label}/{unit.rel_path}",
                                "line": statement.line, "statement": _collapse(statement.text, 120)})
                    break
    return out
