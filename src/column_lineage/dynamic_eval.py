"""Static evaluation of dynamic SQL whose text can be enumerated from the code.

For an ``EXEC (expr)`` / ``EXEC sp_executesql expr`` the evaluator computes every string the
expression can take, using only what the scanned code states:

- string literals (``'...'``, ``N'...'``), NULL, and numbers inside CAST/CONVERT;
- ``+`` and ``CONCAT``; ``QUOTENAME``, ``REPLACE``, ``CHAR``/``NCHAR``, ``CAST``/``CONVERT`` to a
  string type, ``ISNULL``/``COALESCE``, ``UPPER``/``LOWER``, ``LTRIM``/``RTRIM``, ``LEFT``/``RIGHT``;
- variables assigned such expressions (DECLARE, SET, SELECT without FROM), including assignments
  under IF/ELSE and TRY/CATCH, each alternative labelled with the branch that produces it;
- procedure parameters, from what each caller passes (recursively) or the parameter's default.

Anything else (a value from a table, a non-literal argument, a variable assigned in a loop or by
FETCH/UPDATE/OUTPUT, more than ``MAX_ALTERNATIVES`` alternatives) makes the piece *unknown*: no
alternative is produced and the reason is recorded. A parameter whose callers are not all in the
scanned files is *open*: its known alternatives are evaluated, but more may exist.

Each enumerated string is parsed as T-SQL; its statements become :class:`EvaluatedStatement`
writers in the scope of the EXEC. Variables read inside the string belong to the dynamic batch
(or to sp_executesql parameters) and are not followed: such a string is unknown.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from table_lineage.index import SourceIndex, Statement, mask_noncode
from table_lineage.sources import SourceFile

from .control import control_regions, header_end

MAX_ALTERNATIVES = 16
MAX_LENGTH = 200_000
MAX_DEPTH = 8                # procedure calls followed back to find a parameter's value
MAX_EXPRESSION_DEPTH = 120   # nesting of one expression (a long concatenation is a deep tree of +)
# Stands for a value that is not known but can only be text inside a quoted literal of the built SQL.
UNKNOWN_LITERAL = "\u2047unknown\u2047"
PLACEHOLDER_NOTE = "the evaluated SQL uses it only in a WHERE, ON or HAVING literal, so it was read with a placeholder"
MAX_NESTING = 3

EXEC_PAREN = re.compile(r"\s*EXEC(?:UTE)?\s*\(", re.I)
EXEC_SP = re.compile(r"\s*EXEC(?:UTE)?\s+(?:@[\w$#@]+\s*=\s*)?(?:(?:\[[^\]]*\]|\w+)\s*\.\s*){0,2}\[?sp_executesql\]?(?!\w)\s*",
                     re.I)
EXEC_VARIABLE = re.compile(r"\s*EXEC(?:UTE)?\s+@[\w$#@]+", re.I)
CONTROL_WORDS = re.compile(r"(?<![\w@#$])(?:IF|WHILE|GOTO|BEGIN\s+TRY|BEGIN\s+CATCH|RETURN|BREAK|CONTINUE)(?![\w$#@])", re.I)
STRING_TYPES = {"CHAR": 1, "NCHAR": 1, "VARCHAR": 1, "NVARCHAR": 1, "TEXT": None, "NTEXT": None}
NO_QUOTE_TYPES = {"DATE", "DATETIME", "DATETIME2", "SMALLDATETIME", "DATETIMEOFFSET", "TIME", "BIT", "TINYINT",
                  "SMALLINT", "INT", "BIGINT", "DECIMAL", "NUMERIC", "MONEY", "SMALLMONEY", "FLOAT", "REAL"}
TIME_FUNCTIONS = {"GETDATE", "GETUTCDATE", "SYSDATETIME", "SYSUTCDATETIME", "SYSDATETIMEOFFSET", "CURRENT_TIMESTAMP"}
HEADER_TYPE = r"\s+(?:AS\s+)?(?P<type>\[?[A-Za-z_]\w*\]?(?:\s*\.\s*\[?[A-Za-z_]\w*\]?)?)(?:\s*\(\s*(?P<length>\d+|max)\s*(?:,\s*\d+\s*)?\))?"


class Num(str):
    """A numeric literal, kept as written; it becomes text only through CAST/CONVERT."""


class Unknown(Exception):
    def __init__(self, reasons):
        self.reasons = list(dict.fromkeys(reasons if isinstance(reasons, (list, tuple)) else [reasons]))
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class Cond:
    """One runtime condition an alternative depends on. Same key and a different arm contradict."""
    key: tuple
    arm: str
    label: str = field(compare=False)
    order: tuple = field(default=(), compare=False)


@dataclass(frozen=True)
class Alt:
    value: object                       # str, Num or None (NULL)
    conds: frozenset = frozenset()
    built: tuple = ()                   # (rel_path, line) of statements that built the value


@dataclass(frozen=True)
class EvaluatedStatement(Statement):
    """A statement parsed from an evaluated dynamic SQL string, placed at its EXEC."""
    origin_line: int = 0
    origin_end_line: int = 0
    controls: tuple = ()
    eval_key: str = ""
    built_at: str = ""
    executed_at: str = ""


@dataclass
class Alternative:
    sql: str
    conditions: list[str]
    built_at: list[str]
    statements: list[EvaluatedStatement] = field(default_factory=list)


@dataclass
class Evaluation:
    status: str                         # complete, open, unknown
    alternatives: list[Alternative] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    open: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.status == "complete"

    @property
    def usable(self) -> bool:
        """Every enumerated alternative is fully known (more may exist when open)."""
        return self.status in ("complete", "open")

    def detail(self) -> dict:
        out = {"evaluation": self.status, "alternatives_enumerated": len(self.alternatives) if self.usable else 0}
        if self.unknown:
            out["unknown_pieces"] = list(self.unknown)
        if self.open:
            out["open_pieces"] = list(self.open)
            if self.status == "open" and all(PLACEHOLDER_NOTE in p for p in self.open):
                # Only filter literals are not known: which rows are selected is open, not where values come from.
                out["row_selection_only"] = True
        return out


def cond_negation(cond: Cond) -> Cond:
    if cond.arm == "then":
        return Cond(cond.key, "else", f"ELSE (of {cond.label})", cond.order)
    if cond.arm == "else":
        label = cond.label[len("ELSE (of "):-1] if cond.label.startswith("ELSE (of ") else f"NOT ({cond.label})"
        return Cond(cond.key, "then", label, cond.order)
    if cond.arm == "catch":
        return Cond(cond.key, "no_catch", "no error caught (CATCH not run)", cond.order)
    return Cond(cond.key, "not_" + cond.arm, f"NOT ({cond.label})", cond.order)


def consistent(conds) -> bool:
    arms: dict = {}
    for cond in conds:
        if arms.setdefault(cond.key, cond.arm) != cond.arm:
            return False
    return True


def sql_text(node) -> str:
    return node.sql(dialect="tsql", comments=False) if isinstance(node, exp.Expression) else str(node)


def quote_name(value: str, quote: str) -> str | None:
    if len(value) > 128:
        return None
    pairs = {"[": ("[", "]"), "]": ("[", "]"), '"': ('"', '"'), "'": ("'", "'"), "(": ("(", ")"), ")": ("(", ")"),
             "<": ("<", ">"), ">": ("<", ">"), "{": ("{", "}"), "}": ("{", "}"), "`": ("`", "`")}
    if quote not in pairs:
        return None
    left, right = pairs[quote]
    return left + value.replace(right, right + right) + right


def type_length(data_type) -> tuple[str, int | None]:
    """(kind, maximum length) of a declared type: kind is ``string``, ``other`` (not text) or ``unknown``
    (a user-defined type); the length is None when unbounded or not written."""
    if not isinstance(data_type, exp.DataType):
        return "unknown", None
    name = data_type.this.name if hasattr(data_type.this, "name") else str(data_type.this)
    name = name.upper()
    if name == "USERDEFINED":
        return ("string", 128) if data_type.sql(dialect="tsql").upper() in ("SYSNAME", "[SYSNAME]") else ("unknown", None)
    if name not in STRING_TYPES:
        return "other", None
    if STRING_TYPES[name] is None:
        return "string", None
    params = data_type.expressions
    if not params:
        return "string", None   # the caller decides the implicit length (DECLARE: 1, CAST: 30)
    value = params[0].this if isinstance(params[0], exp.DataTypeParam) else params[0]
    text = sql_text(value).upper()
    if text == "MAX":
        return "string", None
    return "string", (int(text) if text.isdigit() else None)


def implicit_length(data_type) -> bool:
    """A (n)char/(n)varchar written without a length."""
    return (isinstance(data_type, exp.DataType) and not data_type.expressions
            and getattr(data_type.this, "name", "").upper() in ("CHAR", "NCHAR", "VARCHAR", "NVARCHAR"))


class Evaluator:
    def __init__(self, catalog):
        self.catalog = catalog
        self._regions: dict[str, dict] = {}
        self._memo: dict[tuple, tuple] = {}
        self._active: set[tuple] = set()
        self._declared: dict[str, dict] = {}
        self._open: list[str] = []
        self._calls = 0                       # procedure calls being followed for parameter values
        self._literal_unknowns: list[str] = []

    # ---------------------------------------------------------------- entry
    def evaluate(self, issue) -> Evaluation:
        self._open = []
        self._calls = 0     # (the placeholder list is kept: memoized values refer to it by index)
        self._sp_call = None
        self._bindings = {}
        unit, statement = issue.unit, issue.statement
        try:
            expr = self.executed_expression(statement.text)
            alts = self.eval(expr, unit, statement, statement.scope, 0, None)
            alts = self.relative_to(alts, unit, statement)
            if not alts:
                raise Unknown("no value of the executed SQL could be enumerated")
            if self._sp_call is not None:
                self._bindings = self.sp_bindings(unit, statement)
            alternatives = self.alternatives(alts, issue, None, 0)
        except Unknown as exc:
            return Evaluation("unknown", unknown=exc.reasons, open=list(dict.fromkeys(self._open)))
        except (RecursionError, SqlglotError, ValueError) as exc:  # noqa: BLE001 - never fail the scan
            return Evaluation("unknown", unknown=[f"evaluation failed: {type(exc).__name__}"])
        status = "open" if self._open else "complete"
        return Evaluation(status, alternatives, open=list(dict.fromkeys(self._open)))

    def executed_expression(self, text: str) -> exp.Expression:
        masked = mask_noncode(text)
        match = EXEC_PAREN.match(masked)
        if match:
            depth, close = 0, None
            for i in range(match.end() - 1, len(masked)):
                if masked[i] == "(":
                    depth += 1
                elif masked[i] == ")":
                    depth -= 1
                    if depth == 0:
                        close = i
                        break
            if close is None:
                raise Unknown("the EXEC (...) text could not be delimited")
            rest = masked[close + 1:].strip(" \t\r\n;")
            if rest and rest.split()[0].upper() in ("AT", "AS"):
                raise Unknown(f"EXEC (...) {rest.split()[0].upper()} ... runs elsewhere or as another user; not evaluated")
            if rest:
                raise Unknown(f"the EXEC (...) statement also holds {shorten(' '.join(rest.split()), 40)!r} (no "
                              "statement boundary was found); not evaluated")
            return self.parse_expression(text[match.end():close])
        match = EXEC_SP.match(masked)
        if match:
            args = text[match.end():].rstrip().rstrip(";")
            tree = self.parse_select(args)
            first = tree.expressions[0] if tree.expressions else None
            if isinstance(first, exp.EQ) and isinstance(first.this, exp.Parameter):
                named = {sql_text(e.this).lower(): e.expression for e in tree.expressions
                         if isinstance(e, exp.EQ) and isinstance(e.this, exp.Parameter)}
                first = named.get("@stmt")
            if first is None:
                raise Unknown("sp_executesql is called without a statement argument that could be read")
            rest = [e for e in tree.expressions if e is not first]
            named = {sql_text(e.this).lower(): e.expression for e in rest
                     if isinstance(e, exp.EQ) and isinstance(e.this, exp.Parameter)}
            positional = [e for e in rest if not (isinstance(e, exp.EQ) and isinstance(e.this, exp.Parameter))]
            params = named.pop("@params", None)
            if params is None and positional:
                params = positional.pop(0)
            if params is not None or named or positional:
                self._sp_call = (params, positional, named)
            return first
        if EXEC_VARIABLE.match(masked):
            raise Unknown("EXEC @variable runs the procedure whose name the variable holds; not evaluated")
        raise Unknown("this form of dynamic SQL is not evaluated")

    def sp_bindings(self, unit, statement) -> dict[str, exp.Expression]:
        """sp_executesql @stmt, @params, @p1 = v1, ...: each declared parameter is bound to its argument, so
        the evaluated text reads the caller's value (a literal, or the caller's variable or parameter, which
        the trace then follows). OUTPUT parameters, a parameter list that is not one known text, and a
        declared parameter without an argument stay unevaluated."""
        params, positional, named = self._sp_call
        if params is None:
            raise Unknown("sp_executesql passes arguments but its parameter list could not be read")
        texts = self.eval(params, unit, statement, statement.scope, 0, None)
        if len(texts) != 1 or not isinstance(texts[0].value, str):
            raise Unknown("sp_executesql's parameter list is not one known text")
        # Split on top-level commas of the code (not inside comments or string defaults), comments dropped.
        code = re.sub(r"N?'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/",
                      lambda m: m.group() if m.group().endswith("'") else " ", texts[0].value, flags=re.S)
        masked = mask_noncode(code)
        declared, depth, begin = [], 0, 0
        for i, ch in enumerate(masked):
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if ch == "," and depth == 0:
                declared.append(code[begin:i])
                begin = i + 1
        declared.append(code[begin:])
        bindings: dict[str, exp.Expression] = {}
        for index, item in enumerate(d.strip() for d in declared):
            if not item:
                continue
            match = re.match(r"(@[\w$#@]+)\s+(.*)$", item, re.S)
            if not match:
                raise Unknown(f"sp_executesql parameter {shorten(item, 40)!r} could not be read")
            name, rest = match.group(1).lower(), match.group(2)
            if re.search(r"\b(?:OUTPUT|OUT)\b", rest, re.I):
                raise Unknown(f"sp_executesql parameter {name} is OUTPUT; values written back are not traced")
            value = named.get(name, positional[index] if index < len(positional) else None)
            if isinstance(value, exp.Alias) and str(value.alias).upper() in ("OUTPUT", "OUT"):
                raise Unknown(f"sp_executesql argument for {name} is passed OUTPUT; values written back are not traced")
            if (isinstance(value, exp.Var) or (isinstance(value, exp.Column) and not value.table)) and \
                    value.name.upper() == "DEFAULT":
                value = None   # the declared default, below
            if value is None and re.search(r"=", rest):
                default = rest.split("=", 1)[1].strip()
                try:
                    value = self.parse_expression(default)
                except Unknown:
                    value = None
            if value is None:
                raise Unknown(f"sp_executesql parameter {name} has no argument that could be read")
            if isinstance(value, exp.Var) or (isinstance(value, exp.Column) and not value.table):
                # SQL Server accepts an unquoted word as a string argument (@p = abc).
                value = exp.Literal.string(value.name)
            atomic = (exp.Literal, exp.Null, exp.Parameter)
            if not (isinstance(value, atomic) or (isinstance(value, exp.Neg) and isinstance(value.this, exp.Literal))):
                # A procedure argument is a constant or a variable; anything else was not read correctly.
                raise Unknown(f"sp_executesql argument for {name} ({shorten(sql_text(value), 40)}) is not a constant "
                              "or a variable")
            bindings[name] = value
        return bindings

    def bind_parameters(self, sql: str, issue) -> str:
        """The evaluated text with each bound sp_executesql parameter replaced by its argument (code only:
        never inside string literals or comments, never an EXEC argument name). Refused (Unknown) when
        the batch assigns a parameter, uses one in a statement that is not parsed or in nested dynamic
        SQL, or names a caller variable an argument reads (the batch's own variable of that name would
        be confused with the caller's)."""
        from .catalog import DYNAMIC
        if not self._bindings:
            return sql
        masked = mask_noncode(sql)
        token = re.compile(r"(?<![\w@$#])@[\w$#@]+")
        names = {m.group().lower() for m in token.finditer(masked)}
        clash = sorted((self.bound_variables() - set(self._bindings)) & names)
        if clash:
            raise Unknown(f"the evaluated SQL uses {', '.join(clash)}, which an sp_executesql argument passes from the "
                          "caller; which variable each use means is not settled")
        index = SourceIndex()
        try:
            index.add(SourceFile(issue.unit.root / "<dynamic>", sql), include_ddl=True)
        except Exception as exc:  # noqa: BLE001
            raise Unknown(f"the evaluated SQL could not be split into statements ({type(exc).__name__})") from None
        argument_names: set[int] = set()   # offsets of EXEC argument names: never replaced
        for st in index.statements:
            used = {m.group().lower() for m in token.finditer(masked, st.start, st.end)} & set(self._bindings)
            if not used:
                continue
            tree = st.tree if st.parse_status == "parsed" else None
            text = masked[st.start:st.end]
            if tree is None or isinstance(tree, exp.Command) or DYNAMIC.match(st.text):
                raise Unknown(f"sp_executesql parameter {sorted(used)[0]} is used in a statement that is not analysed")
            for p in tree.find_all(exp.Parameter):
                name = sql_text(p).lower()
                if name not in used:
                    continue
                parent = p.parent
                if isinstance(parent, exp.DeclareItem) or (
                        isinstance(parent, exp.EQ) and parent.this is p and not isinstance(parent.parent, exp.Execute)
                        and isinstance(parent.parent, (exp.Select, exp.Update, exp.SetItem, exp.Set, exp.Merge))):
                    raise Unknown(f"sp_executesql parameter {name} is assigned inside the evaluated SQL; its value "
                                  "there is not the argument")
            for name in used:
                if re.search(rf"(?<![\w@$#]){re.escape(name)}\s+OUT(?:PUT)?\b|\bINTO\s+{re.escape(name)}(?![\w$#@])",
                             text, re.I):
                    raise Unknown(f"sp_executesql parameter {name} is written inside the evaluated SQL")
            if isinstance(tree, exp.Execute):
                for m in token.finditer(masked, st.start, st.end):
                    if re.match(r"\s*=", masked[m.end():]):
                        argument_names.add(m.start())
        out, last = [], 0
        for match in token.finditer(masked):
            replacement = self._bindings.get(match.group().lower())
            if replacement is None or match.start() in argument_names:
                continue
            out.append(sql[last:match.start()])
            out.append(f" {sql_text(replacement)} ")   # spaced: "s.a-@p" with -1 must not become "s.a--1"
            last = match.end()
        out.append(sql[last:])
        return "".join(out)

    def bound_variables(self) -> set[str]:
        """Caller variables and parameters the bound arguments read: these belong to the caller's scope."""
        return {sql_text(p).lower() for value in self._bindings.values() for p in value.find_all(exp.Parameter)} | \
            {sql_text(value).lower() for value in self._bindings.values() if isinstance(value, exp.Parameter)}

    @staticmethod
    def parse_select(text: str) -> exp.Select:
        try:
            tree = sqlglot.parse_one(f"SELECT {text}", read="tsql", error_level=ErrorLevel.RAISE)
        except (SqlglotError, ValueError, RecursionError):
            raise Unknown("the executed expression could not be parsed") from None
        if not isinstance(tree, exp.Select) or not tree.expressions:
            raise Unknown("the executed expression could not be parsed")
        return tree

    def parse_expression(self, text: str) -> exp.Expression:
        tree = self.parse_select(text)
        if len(tree.expressions) != 1 or tree.args.get("from_") is not None:
            raise Unknown("the executed expression could not be parsed")
        return tree.expressions[0]

    # -------------------------------------------------------- control flow
    def regions(self, unit, statement) -> tuple:
        if unit.path not in self._regions:
            try:
                self._regions[unit.path] = control_regions(unit.text, unit.masked, unit.index.statements)
            except Exception:  # noqa: BLE001 - control flow is optional, but then nothing here is evaluated
                self._regions[unit.path] = {}
        found = self._regions[unit.path].get(statement.start)
        if found is None:
            # Without its IF/ELSE/WHILE context an assignment would look unconditional and could hide
            # the alternatives before it.
            raise Unknown(f"the control flow around {unit.rel_path}:{statement.line} could not be followed")
        return found

    def region_conds(self, unit, statement) -> tuple[list[Cond], list[tuple]]:
        """Conditions of the regions enclosing ``statement`` (outermost first) and its loops."""
        conds, loops = [], []
        for start, end, label, owner, arm in self.regions(unit, statement):
            key = (unit.path, owner)
            if arm in ("then", "else"):
                conds.append(Cond(("if",) + key, arm, label, (unit.path, start)))
            elif arm == "catch":
                conds.append(Cond(("catch",) + key, "catch", "CATCH", (unit.path, start)))
            elif arm == "loop":
                loops.append((start, end, label))
        return conds, loops

    def relative_to(self, alts: list[Alt], unit, statement) -> list[Alt]:
        """Drop conditions implied by the point's own regions, and alternatives they exclude."""
        given, _ = self.region_conds(unit, statement)
        given_set = set(given)
        out = []
        for alt in alts:
            if not consistent(set(alt.conds) | given_set):
                continue
            out.append(Alt(alt.value, frozenset(c for c in alt.conds if c not in given_set), alt.built))
        return dedupe(out)

    # ----------------------------------------------------------- expressions
    def eval(self, node, unit, point, scope, depth, env) -> list[Alt]:
        if depth > MAX_EXPRESSION_DEPTH:
            raise Unknown("the expression is nested too deeply")
        if isinstance(node, exp.Paren):
            return self.eval(node.this, unit, point, scope, depth + 1, env)
        if isinstance(node, exp.National):
            return [Alt(node.this or "")]
        if isinstance(node, exp.Literal):
            return [Alt(node.this if node.is_string else Num(node.this))]
        if isinstance(node, exp.Null):
            return [Alt(None)]
        if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and not node.this.is_string:
            return [Alt(Num("-" + node.this.this))]
        if isinstance(node, exp.Parameter):
            return self.variable(node, unit, point, scope, depth, env)
        args = lambda *nodes: [self.eval(n, unit, point, scope, depth + 1, env) for n in nodes]  # noqa: E731
        if isinstance(node, (exp.Add, exp.DPipe)):
            return self.combine(self.collect(args, node.this, node.expression), concat_plus, sql_text(node))
        if isinstance(node, exp.ConcatWs):
            # CONCAT_WS(sep, a, b, ...): the separator goes between the non-NULL pieces. (A subclass of
            # Concat in SQLGlot, so it must be tested first.)
            def concat_ws(vs):
                sep, pieces = vs[0], [v for v in vs[1:] if v is not None]
                if sep is None:
                    return None
                if any(not isinstance(v, str) for v in [sep] + pieces):
                    raise Unknown("CONCAT_WS over a number is not evaluated")
                return sep.join(pieces)
            return self.combine(self.collect(args, *node.expressions), concat_ws, sql_text(node))
        if isinstance(node, exp.Concat):
            return self.combine(self.collect(args, *node.expressions), concat_function, sql_text(node))
        if isinstance(node, exp.Coalesce) and node.args.get("is_null"):
            # ISNULL(a, b) returns a's type: the replacement is cut to a's declared length. Only a variable
            # with a known declared length is evaluated; anything else is not.
            first = node.this
            from .catalog import restore_variable
            name = restore_variable(sql_text(first)).lower() if isinstance(first, exp.Parameter) else None
            declared = self.declared_types(scope, unit).get(name) if name else None
            kind, length = type_length(declared) if declared is not None else ("unknown", None)
            if kind != "string" or (length is None and not (isinstance(declared, exp.DataType)
                                                             and "MAX" in sql_text(declared).upper())):
                raise Unknown(f"{shorten(sql_text(node))}: ISNULL takes the first argument's type, which is not known")

            def isnull(vs):
                value = vs[0] if vs[0] is not None else vs[1]
                return value[:length] if isinstance(value, str) and length is not None else value
            return self.combine(self.collect(args, node.this, *node.expressions), isnull, sql_text(node))
        if isinstance(node, exp.Coalesce):
            return self.combine(self.collect(args, node.this, *node.expressions),
                                lambda vs: next((v for v in vs if v is not None), None), sql_text(node))
        if isinstance(node, exp.Replace):
            return self.combine(self.collect(args, node.this, node.expression, node.args.get("replacement")),
                                replace_ci, sql_text(node))
        if isinstance(node, exp.Chr) and len(node.expressions) == 1:
            return self.combine(self.collect(args, node.expressions[0]), char_of, sql_text(node))
        if isinstance(node, (exp.Upper, exp.Lower)):
            fn = str.upper if isinstance(node, exp.Upper) else str.lower
            return self.combine(self.collect(args, node.this), lambda vs: string_op(vs[0], fn), sql_text(node))
        if isinstance(node, exp.Trim) and node.args.get("expression") is None:
            position = str(node.args.get("position") or "BOTH").upper()
            fn = {"LEADING": lambda s: s.lstrip(" "), "TRAILING": lambda s: s.rstrip(" ")}.get(position, lambda s: s.strip(" "))
            return self.combine(self.collect(args, node.this), lambda vs: string_op(vs[0], fn), sql_text(node))
        if isinstance(node, (exp.Left, exp.Right)):
            left = isinstance(node, exp.Left)
            return self.combine(self.collect(args, node.this, node.expression),
                                lambda vs: left_right(vs[0], vs[1], left), sql_text(node))
        if isinstance(node, (exp.Cast, exp.TryCast, exp.Convert)):
            inner, to = (node.expression, node.this) if isinstance(node, exp.Convert) else \
                (node.this, node.args.get("to"))
            try:
                if isinstance(node, exp.Convert) and node.args.get("style"):
                    raise Unknown(f"{shorten(sql_text(node))}: CONVERT with a style is not evaluated")
                return self.convert(inner, to, args, node)
            except Unknown as exc:
                if type_length(to)[0] == "string" and self.holds_no_quote(inner, unit, scope):
                    # Text of a date or number never contains a quote: inside a quoted literal of the built SQL it
                    # can only change that literal. pieces() checks where it lands.
                    piece = shorten(sql_text(node))
                    why = exc.reasons[0]
                    why = why[len(piece) + 2:] if why.startswith(piece + ": ") else why
                    self._literal_unknowns.append(f"{piece} ({why})")
                    return [Alt(f"\u2047{len(self._literal_unknowns) - 1}\u2047")]
                raise
        if isinstance(node, exp.Anonymous):
            name = str(node.this).upper()
            if name == "QUOTENAME" and 1 <= len(node.expressions) <= 2:
                return self.combine(self.collect(args, *node.expressions), quotename, sql_text(node))
            if name == "NCHAR" and len(node.expressions) == 1:
                return self.combine(self.collect(args, node.expressions[0]), char_of, sql_text(node))
        raise Unknown(f"{shorten(sql_text(node))}: not built from literals (only literals, variables, parameters, "
                      "+, CONCAT, QUOTENAME, REPLACE, CHAR, CAST/CONVERT and a few string functions are evaluated)")

    def holds_no_quote(self, node, unit, scope) -> bool:
        """Whether the value of ``node`` is a date or number (its text can never contain a quote)."""
        if isinstance(node, exp.Paren):
            return self.holds_no_quote(node.this, unit, scope)
        if isinstance(node, (exp.CurrentTimestamp, exp.CurrentDate, exp.CurrentDatetime)):
            return True
        if isinstance(node, exp.Anonymous) and str(node.this).upper() in TIME_FUNCTIONS:
            return True
        if isinstance(node, exp.Parameter) and unit is not None and scope is not None:
            from .catalog import restore_variable
            name = restore_variable(sql_text(node)).lower()
            return self.declared_types(scope, unit).get(name, ("unknown",))[0] == "other"
        if isinstance(node, (exp.Cast, exp.TryCast)):
            to = node.args.get("to")
            return isinstance(to, exp.DataType) and type_length(to)[0] == "other"
        return False

    @staticmethod
    def collect(args, *nodes) -> list[list[Alt]]:
        """Evaluate every argument, reporting all unknown pieces rather than the first."""
        results, reasons = [], []
        for node in nodes:
            try:
                results.append(args(node)[0])
            except Unknown as exc:
                reasons.extend(exc.reasons)
        if reasons:
            raise Unknown(reasons)
        return results

    def combine(self, lists: list[list[Alt]], fn, what: str) -> list[Alt]:
        combos = [Alt(None, frozenset(), ())]
        values: list[list] = [[]]
        for options in lists:
            new_combos, new_values = [], []
            for combo, vals in zip(combos, values):
                for option in options:
                    conds = combo.conds | option.conds
                    if not consistent(conds):
                        continue
                    new_combos.append(Alt(None, conds, merge_built(combo.built, option.built)))
                    new_values.append(vals + [option.value])
                    if len(new_combos) > MAX_ALTERNATIVES * MAX_ALTERNATIVES:
                        raise Unknown(f"{shorten(what)}: more than {MAX_ALTERNATIVES} alternatives")
            combos, values = new_combos, new_values
        out = []
        for combo, vals in zip(combos, values):
            value = fn(vals)
            if isinstance(value, str) and len(value) > MAX_LENGTH:
                raise Unknown(f"{shorten(what)}: the built string is too long to evaluate")
            out.append(Alt(value, combo.conds, combo.built))
        return self.bounded(dedupe(out), what)

    @staticmethod
    def bounded(alts: list[Alt], what: str) -> list[Alt]:
        if len(alts) > MAX_ALTERNATIVES:
            raise Unknown(f"{shorten(what)}: more than {MAX_ALTERNATIVES} alternatives")
        return alts

    def convert(self, value_node, data_type, args, node) -> list[Alt]:
        kind, length = type_length(data_type)
        if kind != "string":
            raise Unknown(f"{shorten(sql_text(node))}: conversion to a non-string type is not evaluated")
        if implicit_length(data_type):
            length = 30   # CAST/CONVERT to (n)varchar without a length
        def cast(vs):
            value = vs[0]
            if value is None:
                return None
            text = str(value)
            return text[:length] if length is not None else text
        return self.combine(self.collect(args, value_node), cast, sql_text(node))

    # ------------------------------------------------------------ variables
    def variable(self, node, unit, point, scope, depth, env) -> list[Alt]:
        from .catalog import restore_variable
        name = restore_variable(sql_text(node)).lower()
        if name.startswith("@@"):
            raise Unknown(f"{name}: a system function, known only at runtime")
        if env is not None:
            if name not in env:
                raise Unknown(f"{name}: not set inside the evaluated SQL (variables of the outer code are not visible "
                              "to dynamic SQL)")
            return env[name]
        key = (scope, name, unit.path, point.start)
        if key in self._memo:
            alts, opened = self._memo[key]
            self._open.extend(opened)
            if isinstance(alts, Unknown):
                raise alts
            return alts
        if key in self._active:
            raise Unknown(f"{name}: its value depends on itself through procedure calls")
        if depth > MAX_EXPRESSION_DEPTH:
            raise Unknown(f"{name}: the expression is nested too deeply")
        self._active.add(key)
        before = len(self._open)
        try:
            alts = self._variable(name, unit, point, scope, depth)
        except Unknown as exc:
            self._memo[key] = (exc, tuple(self._open[before:]))
            raise
        finally:
            self._active.discard(key)
        self._memo[key] = (alts, tuple(self._open[before:]))
        return alts

    def _variable(self, name, unit, point, scope, depth) -> list[Alt]:
        catalog = self.catalog
        where = lambda st: f"{unit.rel_path}:{st.line}"  # noqa: E731
        unparsed = re.compile(re.escape(name) + r"(?![\w$#@])\s*[-+*/%&|^]?=(?!=)", re.I)
        for other in catalog.fallback_by_scope.get(scope, []):
            if unparsed.search(mask_noncode(other.text)):
                raise Unknown(f"{name}: may be assigned by a statement that could not be parsed ({where(other)})")
        assignments = [a for a in catalog.assignments.get((scope, name), []) if a.unit.path == unit.path]
        given, loops = self.region_conds(unit, point)
        given_set = set(given)
        loop_spans = {(s, e) for s, e, _ in loops}
        for a in assignments:
            if a.statement.start >= point.start and not loop_spans:
                continue   # runs after the EXEC, and the EXEC is not repeated
            _, a_loops = self.region_conds(a.unit, a.statement)
            if a_loops and (a.statement.start < point.start or {(s, e) for s, e, _ in a_loops} & loop_spans):
                raise Unknown(f"{name}: assigned inside a loop ({a_loops[0][2][:60]}) at {where(a.statement)}")
        prior = sorted((a for a in assignments if a.statement.start < point.start), key=lambda a: a.statement.start)
        initial = Alt(INITIAL)
        state = [initial]
        for a in prior:
            a_conds, _ = self.region_conds(a.unit, a.statement)
            if not consistent(set(a_conds) | given_set):
                continue   # in a branch that excludes the EXEC
            extra = [c for c in a_conds if c not in given_set]
            values = self.assigned_values(a, name, depth)
            values = [Alt(v.value, v.conds, merge_built(v.built, ((a.unit.rel_path, a.statement.line),))) for v in values]
            if not extra:
                state = values
            else:
                kept = []
                for i, cond in enumerate(extra):
                    negated = frozenset(extra[:i]) | {cond_negation(cond)}
                    kept.extend(Alt(s.value, s.conds | negated, s.built) for s in state if consistent(s.conds | negated))
                state = kept + [Alt(v.value, v.conds | frozenset(extra), v.built) for v in values
                                if consistent(v.conds | frozenset(extra))]
            state = self.bounded(dedupe(state), name)
        out = []
        for alt in state:
            if alt.value is not INITIAL:
                out.append(alt)
                continue
            if catalog.is_parameter(scope, name):
                for p in self.parameter(name, scope, depth):
                    if consistent(alt.conds | p.conds):
                        out.append(Alt(p.value, alt.conds | p.conds, merge_built(alt.built, p.built)))
            else:
                out.append(Alt(None, alt.conds, alt.built))   # declared without a value: NULL
        return self.bounded(dedupe(out), name)

    def assigned_values(self, assignment, name, depth) -> list[Alt]:
        where = f"{assignment.unit.rel_path}:{assignment.statement.line}"
        kind = assignment.kind
        if kind in ("fetch", "update", "exec_output") or assignment.value is None:
            how = {"fetch": "FETCH from a cursor", "update": "an UPDATE", "exec_output": "an EXEC OUTPUT parameter or "
                   "return code"}.get(kind, "a statement that was not analysed")
            raise Unknown(f"{name}: assigned by {how} at {where}")
        if kind == "select" and assignment.select is not None and any(
                assignment.select.args.get(k) is not None for k in ("from_", "where", "joins")):
            raise Unknown(f"{name}: assigned from a query (SELECT ... FROM) at {where}")
        if any(isinstance(n, (exp.Select, exp.Subquery)) for n in assignment.value.walk()):
            raise Unknown(f"{name}: assigned from a query at {where}")
        values = self.eval(assignment.value, assignment.unit, assignment.statement, assignment.scope, depth + 1, None)
        return self.truncate(values, assignment.scope, name, assignment.unit)

    def truncate(self, values: list[Alt], scope, name, unit) -> list[Alt]:
        """Apply the declared length of the variable it is assigned to."""
        declared = self.declared_types(scope, unit).get(name)
        if declared is None:
            return values
        kind, length = declared
        if kind == "other":
            # A numeric variable keeps a numeric literal (it becomes text only through CAST/CONVERT);
            # text assigned to it would be converted at runtime, which is not evaluated.
            if all(v.value is None or isinstance(v.value, Num) for v in values):
                return values
            raise Unknown(f"{name}: declared with a non-string type but assigned text")
        if length is None:
            return values
        return [Alt(v.value[:length] if isinstance(v.value, str) else v.value, v.conds, v.built) for v in values]

    def declared_types(self, scope, unit) -> dict:
        if scope in self._declared:
            return self._declared[scope]
        found: dict = {}
        for st in unit.index.statements:
            if st.scope != scope or not isinstance(st.tree, exp.Declare):
                continue
            for item in st.tree.expressions:
                if not isinstance(item, exp.DeclareItem) or not isinstance(item.args.get("kind"), exp.DataType):
                    continue
                names = item.this if isinstance(item.this, list) else [item.this]
                for n in names:
                    if isinstance(n, exp.Parameter):
                        data_type = item.args["kind"]
                        kind, length = type_length(data_type)
                        if implicit_length(data_type):
                            length = 1   # DECLARE @v varchar  means varchar(1)
                        found[sql_text(n).lower()] = (kind, length)
        proc = self.catalog.proc_by_scope.get(scope)
        if proc is not None:
            end = header_end(proc.unit.masked, proc.start, len(proc.unit.masked)) or proc.start
            header = proc.unit.text[proc.start:end]
            for param in proc.params:
                match = re.search(r"(?<![\w@])" + re.escape(param) + HEADER_TYPE, mask_noncode(header), re.I)
                if not match:
                    continue
                type_name = match.group("type").strip("[]").split(".")[-1].strip("[]").upper()
                length = match.group("length")
                if type_name == "SYSNAME":
                    found[param] = ("string", 128)
                elif type_name in STRING_TYPES:
                    found[param] = ("string", None if (length or "").upper() == "MAX" or STRING_TYPES[type_name] is None
                                    else int(length) if length else 1)
                elif type_name in NO_QUOTE_TYPES:
                    found[param] = ("other", None)    # a date or number: its text never holds a quote
        self._declared[scope] = found
        return found

    def parameter(self, name, scope, depth) -> list[Alt]:
        catalog = self.catalog
        proc = catalog.proc_by_scope[scope]
        label = proc.ref.display if hasattr(proc.ref, "display") else proc.key
        if self._calls >= MAX_DEPTH:
            raise Unknown(f"{name}: the call chain is deeper than {MAX_DEPTH} procedures")
        calls = list(catalog.calls_by_callee.get(proc.key, []))
        if proc.ref.database:
            unknown = ".".join(p for p in ("?", proc.ref.schema or "?", proc.ref.name))
            calls += catalog.calls_by_callee.get(unknown, [])
        try:
            position = proc.params.index(name)
        except ValueError:
            position = None
        default = proc.defaults.get(name)
        key = ("call", scope)
        if not calls:
            if default is None:
                raise Unknown(f"{name}: a parameter of {label} with no caller in the scanned files and no default")
            values = self.eval(default[0], proc.unit, default[1], scope, depth + 1, None)
            text = shorten(sql_text(default[0]))
            self._open.append(f"{name}: no caller of {label} in the scanned files; callers outside them may pass other "
                              f"values (only its default {text} is known)")
            cond = Cond(key, "default", f"{name} left to its default {text} (no caller in the scanned files)",
                        (proc.unit.path, proc.start))
            return [Alt(v.value, v.conds | {cond}, merge_built(v.built, ((proc.unit.rel_path, proc.line),)))
                    for v in self.truncate(values, scope, name, proc.unit)]
        out: list[Alt] = []
        reasons: list[str] = []
        for call in sorted(calls, key=lambda c: (c.unit.path, c.statement.start)):
            value = next((arg for arg_name, arg in call.args if arg_name == name), None)
            if value is None and position is not None and position < len(call.args) and call.args[position][0] is None:
                value = call.args[position][1]
            at = f"{call.unit.rel_path}:{call.statement.line}"
            arm = f"{call.unit.path}:{call.statement.start}"
            if value is None:
                if default is None:
                    continue   # the call fails at runtime without a value for this parameter
                text = shorten(sql_text(default[0]))
                cond = Cond(key, arm, f"called at {at} without {name} (its default {text})",
                            (call.unit.path, call.statement.start))
                values = self.eval(default[0], proc.unit, default[1], scope, depth + 1, None)
                built = ((call.unit.rel_path, call.statement.line), (proc.unit.rel_path, proc.line))
            else:
                if isinstance(value, exp.Var) or (isinstance(value, exp.Column) and not value.table):
                    # SQL Server accepts an unquoted word as a string argument (EXEC p @x = abc).
                    value = exp.Literal.string(value.name)
                cond = Cond(key, arm, f"called at {at} with {name} = {shorten(sql_text(value))}",
                            (call.unit.path, call.statement.start))
                try:
                    self._calls += 1
                    try:
                        values = self.eval(value, call.unit, call.statement, call.caller_scope, depth + 1, None)
                    finally:
                        self._calls -= 1
                    values = self.relative_to(values, call.unit, call.statement)
                except Unknown as exc:
                    reasons.extend(f"{name}: passed by the call at {at}; {r}" for r in exc.reasons)
                    continue
                built = ((call.unit.rel_path, call.statement.line),)
            for v in self.truncate(values, scope, name, proc.unit):
                out.append(Alt(v.value, v.conds | {cond}, merge_built(v.built, built)))
        if reasons:
            raise Unknown(reasons)
        return self.bounded(dedupe(out), name)

    # -------------------------------------------------------- alternatives
    def alternatives(self, alts: list[Alt], issue, env, nesting) -> list[Alternative]:
        alts = self.bounded(alts, "the executed SQL")
        unit, statement = issue.unit, issue.statement
        executed_at = f"{unit.rel_path}:{statement.line}"
        controls = tuple(unit.controls.get(statement.start, ()))
        out = []
        ordered = sorted(alts, key=lambda a: (sorted(c.order + (c.label,) for c in a.conds), str(a.value)))
        reasons = []
        for i, alt in enumerate(ordered, 1):
            if isinstance(alt.value, Num):
                reasons.append("the executed expression is a number, not SQL text")
                continue
            sql = alt.value or ""
            conditions = [c.label for c in sorted(alt.conds, key=lambda c: c.order + (c.label,))]
            built = [f"{p}:{line}" for p, line in sorted(set(alt.built))] or [executed_at]
            try:
                pieces = self.pieces(sql, issue, nesting)
            except Unknown as exc:
                reasons.extend(f"alternative {i} of {len(ordered)}: {r}" for r in exc.reasons)
                continue
            label = [f"dynamic SQL alternative {i} of {len(ordered)}"] if len(ordered) > 1 else []
            alternative = Alternative(sql, conditions, built)
            for j, (text, tree) in enumerate(pieces, 1):
                alternative.statements.append(EvaluatedStatement(
                    unit.path, statement.line, statement.start, statement.end, text, statement.procedure,
                    statement.scope, "parsed", tree, None,
                    origin_line=statement.line, origin_end_line=statement.line + statement.text.rstrip().count("\n"),
                    controls=controls + tuple(label) + tuple(conditions), eval_key=f"{i}.{j}",
                    built_at=", ".join(built), executed_at=executed_at))
            out.append(alternative)
        if reasons:
            raise Unknown(reasons)
        return out

    def pieces(self, sql: str, issue, nesting) -> list[tuple[str, exp.Expression]]:
        """Parse an evaluated string into (text, tree) writer statements, or raise Unknown."""
        from .catalog import DYNAMIC, merge_split_inserts, non_lineage
        if not sql.strip():
            return []
        if nesting == 0:
            sql = self.bind_parameters(sql, issue)
        masked = mask_noncode(sql)
        if "\u2047" in sql:
            # A date or number not known: it must land inside a quoted literal (masked away), never in code.
            for match in re.finditer("\u2047([^\u2047]*)\u2047", sql):
                if masked[match.start():match.end()].strip():
                    raise Unknown(f"{self.literal_piece(match.group(1))}: its value is not known and it is not "
                                  "inside a quoted literal of the built SQL")
        control = CONTROL_WORDS.search(masked)
        if control:
            raise Unknown(f"the evaluated SQL contains {control.group().upper()} control flow, which is not evaluated")
        index = SourceIndex()
        try:
            index.add(SourceFile(issue.unit.root / "<dynamic>", sql), include_ddl=True)
        except Exception as exc:  # noqa: BLE001
            raise Unknown(f"the evaluated SQL could not be split into statements ({type(exc).__name__})") from None
        statements = merge_split_inserts(index.statements, sql, masked)
        uncovered = uncovered_text(masked, statements)
        if uncovered:
            # The indexer skips statements it does not know (USE, ALTER VIEW, CREATE PROCEDURE, GRANT, ...);
            # silently skipping them could change what the rest of the string means.
            raise Unknown(f"the evaluated SQL contains {shorten(uncovered, 60)!r}, which is not analysed")
        if not statements:
            raise Unknown("no statement was recognised in the evaluated SQL")
        env: dict[str, list[Alt]] = {}
        out: list[tuple[str, exp.Expression]] = []
        for st in statements:
            text = st.text
            if DYNAMIC.match(text):
                if nesting >= MAX_NESTING:
                    raise Unknown("dynamic SQL is nested too deeply")
                expr = self.executed_expression(text)
                alts = self.eval(expr, issue.unit, st, st.scope, 0, env)
                if len(alts) != 1 or isinstance(alts[0].value, Num):
                    raise Unknown("nested dynamic SQL has more than one possible text")
                out.extend(self.pieces(alts[0].value or "", issue, nesting + 1))
                continue
            tree = st.tree if st.parse_status == "parsed" else None
            if tree is None:
                repaired = self.repair(issue, st)
                if all(p.parse_status == "parsed" for p in repaired):
                    for piece in repaired:
                        out.extend(self.piece(piece.text, piece.tree, env, nesting))
                    continue
                if non_lineage(text) or MAINTENANCE.match(mask_noncode(text)):
                    continue
                raise Unknown(f"a statement in the evaluated SQL could not be parsed: {shorten(' '.join(text.split()))}")
            out.extend(self.piece(text, tree, env, nesting))
        return out

    def repair(self, issue, statement) -> list[Statement]:
        """The catalog's statement repair, without its side effects on scan statistics."""
        catalog = self.catalog
        saved = (catalog.stats["repaired"], set(catalog.rewritten), len(catalog.pending_outputs))
        try:
            return catalog._repair(issue.unit.root_label, issue.unit.rel_path, statement)
        finally:
            catalog.stats["repaired"] = saved[0]
            catalog.rewritten = saved[1]
            outputs = catalog.pending_outputs[saved[2]:]
            del catalog.pending_outputs[saved[2]:]
            if outputs:
                raise Unknown("the evaluated SQL has an OUTPUT ... INTO clause, which is not analysed")

    def literal_piece(self, index: str) -> str:
        i = int(index) if index.isdigit() else -1
        return self._literal_unknowns[i] if 0 <= i < len(self._literal_unknowns) else "a value"

    def check_unknown_literals(self, tree) -> None:
        """A literal holding an unknown value may only decide which rows are selected (WHERE, ON, HAVING)."""
        marked = [n for n in tree.find_all(exp.Literal, exp.National) if "\u2047" in str(n.this or "")]
        if not marked:
            return
        pieces = [self.literal_piece(m) for n in marked for m in re.findall("\u2047([^\u2047]*)\u2047", str(n.this))]
        for literal in marked:
            node, ok = literal, False
            while node is not None and node is not tree:
                parent = node.parent
                if isinstance(parent, (exp.Where, exp.Having)) or (isinstance(parent, exp.Join)
                                                                    and node is parent.args.get("on")):
                    ok = True
                    break
                node = parent
            if not ok:
                raise Unknown(f"{'; '.join(dict.fromkeys(pieces))}: its value is not known and would be a value or "
                              "name in the evaluated SQL, not only a filter")
        reason = (f"{'; '.join(dict.fromkeys(pieces))}: not known; {PLACEHOLDER_NOTE} "
                  "(it can change which rows are selected, not where values come from)")
        if reason not in self._open:
            self._open.append(reason)

    def piece(self, text: str, tree, env, nesting: int = 0) -> list[tuple[str, exp.Expression]]:
        if isinstance(tree, exp.Declare):
            for item in tree.expressions:
                if not isinstance(item, exp.DeclareItem):
                    continue
                names = item.this if isinstance(item.this, list) else [item.this]
                default = item.args.get("default")
                for n in names:
                    if isinstance(n, exp.Parameter):
                        env[sql_text(n).lower()] = (self.eval(default, None, None, None, 0, env)
                                                    if isinstance(default, exp.Expression) else [Alt(None)])
            return []
        if isinstance(tree, exp.Set):
            for item in tree.expressions:
                eq = item.this if isinstance(item, exp.SetItem) else item
                if isinstance(eq, exp.EQ) and isinstance(eq.this, exp.Parameter):
                    env[sql_text(eq.this).lower()] = self.eval(eq.expression, None, None, None, 0, env)
            return []
        # The caller's variables an sp_executesql argument put into the top-level batch (not into nested
        # dynamic SQL, which runs in a batch of its own).
        allowed = self.bound_variables() if getattr(self, "_bindings", None) and nesting == 0 else set()
        variables = sorted({sql_text(p).lower() for p in tree.find_all(exp.Parameter)
                            if not sql_text(p).startswith("@@") and not is_argument_name(p)} - allowed)
        if variables:
            raise Unknown(f"the evaluated SQL reads {', '.join(variables)}; variables inside dynamic SQL belong to its "
                          "own batch (or to sp_executesql parameters) and are not traced")
        if isinstance(tree, exp.Expression):
            self.check_unknown_literals(tree)
        if isinstance(tree, exp.Select) and not tree.args.get("into"):
            return []   # a result set: no column is written
        if isinstance(tree, (exp.Delete, exp.TruncateTable, exp.Drop)):
            return []   # removes rows or objects: no column value is written
        kind = str(tree.args.get("kind") or "").upper() if isinstance(tree, exp.Create) else ""
        if isinstance(tree, (exp.Insert, exp.Update, exp.Merge, exp.Select, exp.Union, exp.Except, exp.Intersect)) \
                or kind in ("TABLE", "VIEW") or (isinstance(tree, exp.Execute) and isinstance(tree.this, exp.Table)):
            return [(text, tree)]
        what = f"CREATE {kind}" if kind else type(tree).__name__.upper()
        raise Unknown(f"the evaluated SQL contains a {what} statement, which is not analysed as dynamic SQL: "
                      f"{shorten(' '.join(text.split()), 60)}")


INITIAL = object()

HARMLESS = re.compile(
    r"(?:(?:BEGIN|COMMIT|ROLLBACK|SAVE)(?:\s+(?:TRAN|TRANSACTION|WORK)\b.*)?|END|GO|PRINT\b.*|"
    r"TRUNCATE\s+TABLE\s+\S+|DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?\S+|"
    r"CREATE\s+(?:UNIQUE\s+)?(?:(?:NON)?CLUSTERED\s+)?(?:COLUMNSTORE\s+)?INDEX\b.*|"
    r"ALTER\s+INDEX\b.*|DBCC\b.*|UPDATE\s+STATISTICS\b.*)", re.I)
# Maintenance statements with no column lineage that SQLGlot does not parse.
MAINTENANCE = re.compile(r"\s*(?:UPDATE\s+STATISTICS|ALTER\s+INDEX|DBCC)\b", re.I)


def uncovered_text(masked: str, statements) -> str:
    """Code outside every indexed statement that is not a harmless line (BEGIN/END, TRUNCATE, CREATE INDEX...)."""
    covered = [False] * len(masked)
    for st in statements:
        for i in range(max(st.start, 0), min(st.end, len(masked))):
            covered[i] = True
    rest = "".join(" " if covered[i] else c for i, c in enumerate(masked))
    for chunk in re.split(r"[;\n]", rest):
        chunk = chunk.strip()
        if chunk and not HARMLESS.fullmatch(chunk):
            return chunk
    return ""


def is_argument_name(node: exp.Parameter) -> bool:
    """The @name in ``EXEC proc @name = value`` names a parameter; it is not a variable read."""
    parent = node.parent
    return isinstance(parent, exp.EQ) and parent.this is node and isinstance(parent.parent, exp.Execute)


def dedupe(alts: list[Alt]) -> list[Alt]:
    seen, out = set(), []
    for alt in alts:
        key = (type(alt.value).__name__, alt.value if alt.value is not INITIAL else "<initial>", alt.conds)
        if key in seen:
            continue
        seen.add(key)
        out.append(alt)
    return out


def merge_built(a: tuple, b: tuple) -> tuple:
    return tuple(dict.fromkeys(a + b))


def shorten(text: str, limit: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def concat_plus(values: list):
    if any(v is None for v in values):
        return None
    if any(isinstance(v, Num) for v in values):
        raise Unknown("+ between a number and text is arithmetic or a conversion error, not concatenation")
    return "".join(values)


def concat_function(values: list):
    return "".join("" if v is None else str(v) for v in values)


def replace_ci(values: list):
    text, old, new = values
    if text is None or old is None or new is None:
        return None
    if isinstance(text, Num) or isinstance(old, Num) or isinstance(new, Num):
        raise Unknown("REPLACE over a number is not evaluated")
    if old == "":
        return text
    # SQL Server's default collations compare case-insensitively.
    return re.sub(re.escape(old), lambda _: new, text, flags=re.I)


def char_of(values: list):
    value = values[0]
    if value is None:
        return None
    if not isinstance(value, Num) or not value.lstrip("-").isdigit():
        raise Unknown("CHAR/NCHAR of a non-integer is not evaluated")
    code = int(value)
    if not 0 <= code <= 0x10FFFF:
        return None
    return chr(code)


def string_op(value, fn):
    if value is None:
        return None
    if isinstance(value, Num):
        raise Unknown("a string function over a number is not evaluated")
    return fn(value)


def left_right(value, count, left: bool):
    if value is None or count is None:
        return None
    if isinstance(value, Num) or not isinstance(count, Num) or not count.isdigit():
        raise Unknown("LEFT/RIGHT needs text and a literal length")
    n = int(count)
    return value[:n] if left else (value[-n:] if n else "")


def quotename(values: list):
    value = values[0]
    quote = values[1] if len(values) > 1 else "["
    if value is None or quote is None:
        return None
    if isinstance(value, Num) or isinstance(quote, Num):
        raise Unknown("QUOTENAME of a number is not evaluated")
    return quote_name(value, quote[:1]) if quote else None
