"""A small parser for SSIS expressions (Derived Column, Conditional Split, variables).

Only enough of the language is understood to say which columns and variables an
expression reads and in which role: a reference inside the condition of ``c ? a : b``
is a condition input, references in ``a`` / ``b`` are value inputs of that branch.
Nothing is evaluated. Anything the tokenizer or parser does not understand raises
``ExpressionError`` so the caller can record a gap instead of guessing.

References:

- ``#{<lineage id>}`` (SSIS 2012+ stored form) and ``#<n>`` (SSIS 2008 stored form):
  an input column by lineage id - the metadata link the package itself uses;
- ``[name]`` or a bare identifier (the friendly form): an input column of the same
  component by name, which is how SSIS itself resolves friendly expressions;
- ``@[Namespace::name]`` / ``@name``: a variable or parameter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re


class ExpressionError(ValueError):
    pass


@dataclass
class Token:
    kind: str      # str, num, lineage, column, var, ident, cast, op
    value: str
    start: int
    end: int


@dataclass
class ENode:
    kind: str                       # ref, literal, cast, func, ternary, binary, unary, paren
    start: int
    end: int
    value: str = ""                 # ref kind+id, literal text, operator, function name, cast type
    ref_kind: str | None = None     # lineage, column, var
    children: list = field(default_factory=list)


@dataclass
class Reference:
    kind: str                       # lineage, column, var, literal
    value: str                      # lineage id, column name, variable name, literal text
    role: str                       # value, condition
    branch: str | None
    text: str                       # as written


TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>"(?:\\.|[^"\\])*")
  | (?P<lineage>\#\{[^}]*\}|\#\d+)
  | (?P<var>@\[[^\]]+\]|@[A-Za-z_$][\w:$]*)
  | (?P<column>\[(?:\]\]|[^\]])*\])
  | (?P<num>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?[A-Za-z]*)
  | (?P<ident>[A-Za-z_][\w]*)
  | (?P<op>==|!=|>=|<=|&&|\|\||[-+*/%!~&|^<>?:(),])
""", re.X)

BINARY = [("||",), ("&&",), ("|",), ("^",), ("&",), ("==", "!="), ("<", ">", "<=", ">="), ("+", "-"), ("*", "/", "%")]
CONSTANT_WORDS = {"true", "false"}


def tokenize(text: str) -> list[Token]:
    tokens = []
    pos = 0
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m:
            raise ExpressionError(f"unexpected character {text[pos]!r} at {pos}")
        kind = m.lastgroup
        if kind != "ws":
            tokens.append(Token(kind, m.group(), m.start(), m.end()))
        pos = m.end()
    return tokens


def strip_lineage(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("#{") and raw.endswith("}"):
        return raw[2:-1]
    if raw.startswith("#"):
        return raw[1:]
    return raw


class Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = tokenize(text)
        self.pos = 0

    def peek(self, offset: int = 0) -> Token | None:
        i = self.pos + offset
        return self.tokens[i] if i < len(self.tokens) else None

    def take(self, value: str | None = None) -> Token:
        token = self.peek()
        if token is None or (value is not None and token.value != value):
            raise ExpressionError(f"expected {value or 'a token'} at {token.start if token else 'end'}")
        self.pos += 1
        return token

    def parse(self) -> ENode:
        if not self.tokens:
            raise ExpressionError("empty expression")
        node = self.ternary()
        if self.peek() is not None:
            raise ExpressionError(f"unexpected {self.peek().value!r} at {self.peek().start}")
        return node

    def ternary(self) -> ENode:
        cond = self.binary(0)
        token = self.peek()
        if token is not None and token.value == "?":
            self.take("?")
            a = self.ternary()
            self.take(":")
            b = self.ternary()
            return ENode("ternary", cond.start, b.end, children=[cond, a, b])
        return cond

    def binary(self, level: int) -> ENode:
        if level >= len(BINARY):
            return self.unary()
        left = self.binary(level + 1)
        while True:
            token = self.peek()
            if token is None or token.kind != "op" or token.value not in BINARY[level]:
                return left
            self.take()
            right = self.binary(level + 1)
            left = ENode("binary", left.start, right.end, token.value, children=[left, right])

    def unary(self) -> ENode:
        token = self.peek()
        if token is not None and token.kind == "op" and token.value in ("!", "-", "~"):
            self.take()
            inner = self.unary()
            return ENode("unary", token.start, inner.end, token.value, children=[inner])
        return self.primary()

    def primary(self) -> ENode:
        token = self.take()
        if token.kind == "op" and token.value == "(":
            nxt = self.peek()
            if nxt is not None and nxt.kind == "ident" and nxt.value.upper().startswith("DT_"):
                # Cast: (DT_TYPE[, n, ...]) operand
                depth = 1
                while depth:
                    t = self.take()
                    depth += {"(": 1, ")": -1}.get(t.value, 0) if t.kind == "op" else 0
                spec = self.text[token.start:self.tokens[self.pos - 1].end]
                inner = self.unary()
                return ENode("cast", token.start, inner.end, spec, children=[inner])
            inner = self.ternary()
            close = self.take(")")
            return ENode("paren", token.start, close.end, children=[inner])
        if token.kind == "str":
            return ENode("literal", token.start, token.end, token.value)
        if token.kind == "num":
            return ENode("literal", token.start, token.end, token.value)
        if token.kind == "lineage":
            return ENode("ref", token.start, token.end, strip_lineage(token.value), "lineage")
        if token.kind == "column":
            return ENode("ref", token.start, token.end, token.value[1:-1].replace("]]", "]"), "column")
        if token.kind == "var":
            name = token.value[2:-1] if token.value.startswith("@[") else token.value[1:]
            return ENode("ref", token.start, token.end, name, "var")
        if token.kind == "ident":
            nxt = self.peek()
            if nxt is not None and nxt.value == "(":
                self.take("(")
                args = []
                if self.peek() is not None and self.peek().value != ")":
                    while True:
                        nxt_arg = self.peek()
                        if nxt_arg is not None and nxt_arg.kind == "ident" and nxt_arg.value.upper().startswith("DT_") \
                                and token.value.upper() == "NULL":
                            # NULL(DT_TYPE, n): type arguments are not operands.
                            self.take()
                            args.append(ENode("literal", nxt_arg.start, nxt_arg.end, nxt_arg.value))
                        else:
                            args.append(self.ternary())
                        if self.peek() is not None and self.peek().value == ",":
                            self.take(",")
                            continue
                        break
                close = self.take(")")
                return ENode("func", token.start, close.end, token.value.upper(), children=args)
            if token.value.lower() in CONSTANT_WORDS:
                return ENode("literal", token.start, token.end, token.value.upper())
            return ENode("ref", token.start, token.end, token.value, "column")
        raise ExpressionError(f"unexpected {token.value!r} at {token.start}")


def parse(text: str) -> ENode:
    return Parser(text).parse()


def unparen(node: ENode) -> ENode:
    while node.kind == "paren":
        node = node.children[0]
    return node


def join_branch(outer: str | None, inner: str) -> str:
    return f"{outer} / {inner}" if outer else inner


def references(text: str, tree: ENode | None = None) -> list[Reference]:
    """Every column/variable reference with its role and branch; literals that are a whole value
    alternative (or an operand of arithmetic in one) are included as ``literal`` references."""
    tree = tree or parse(text)
    out: list[Reference] = []

    def src(node: ENode) -> str:
        return " ".join(text[node.start:node.end].split())

    def walk(node: ENode, role: str, branch: str | None, const_ok: bool) -> None:
        node = unparen(node)
        if node.kind == "ref":
            out.append(Reference(node.ref_kind, node.value, role, branch, src(node)))
        elif node.kind == "literal":
            if role == "value" and const_ok:
                out.append(Reference("literal", node.value, role, branch, src(node)))
        elif node.kind == "ternary":
            cond, a, b = node.children
            label = f"WHEN {src(cond)}"
            walk(cond, "condition", join_branch(branch, label), False)
            walk(a, role, join_branch(branch, label + " THEN"), True)
            walk(b, role, join_branch(branch, "ELSE"), True)
        elif node.kind == "func" and node.value == "NULL":
            if role == "value" and const_ok:
                out.append(Reference("literal", "NULL", role, branch, src(node)))
        elif node.kind == "func" and node.value == "REPLACENULL" and len(node.children) == 2:
            walk(node.children[0], role, join_branch(branch, "REPLACENULL argument 1"), True)
            walk(node.children[1], role, join_branch(branch, "REPLACENULL argument 2"), True)
        elif node.kind == "binary" and node.value in ("==", "!=", "<", ">", "<=", ">=", "&&", "||"):
            for child in node.children:
                walk(child, role, branch, False)
        else:
            arithmetic = node.kind in ("binary", "cast") or (node.kind == "unary" and node.value == "-")
            for child in node.children:
                walk(child, role, branch, const_ok and arithmetic)

    walk(tree, "value", None, True)
    return out


def operation(tree: ENode) -> str:
    """Operation label in the vocabulary of the SQL analyzer."""
    node = unparen(tree)
    if node.kind == "ref":
        return "direct"
    if node.kind == "literal":
        return "constant"
    if node.kind == "cast":
        return "cast" if unparen(node.children[0]).kind == "ref" else "expression"
    if node.kind == "ternary":
        return "case"
    if node.kind == "func":
        if node.value == "REPLACENULL":
            return "coalesce"
        if node.value in ("UPPER", "LOWER", "TRIM", "LTRIM", "RTRIM", "SUBSTRING", "LEFT", "RIGHT", "REPLACE"):
            return "string"
        return "function"
    if node.kind in ("binary", "unary"):
        if node.value == "+" and any(unparen(c).kind == "literal" and unparen(c).value.startswith('"')
                                     for c in node.children):
            return "string"
        return "arithmetic"
    return "expression"


def friendly(text: str, names: dict[str, str]) -> str:
    """Replace lineage references by column names (``names``: lineage id -> name) for display."""
    def sub(m):
        key = strip_lineage(m.group())
        return f"[{names[key]}]" if key in names else m.group()
    return re.sub(r"#\{[^}]*\}|#\d+", sub, text)


def lineage_refs(text: str) -> list[str]:
    """Lineage ids written in an expression, found without parsing (for unparseable text)."""
    return [strip_lineage(m.group()) for m in re.finditer(r"#\{[^}]*\}|#\d+", text)]
