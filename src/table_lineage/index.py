"""Conservative source indexing; offsets always refer to the original source."""
from __future__ import annotations

from dataclasses import dataclass
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from .models import normalize_name
from .sources import SourceFile

IDENT = r'(?:\[(?:[^\]]|\]\])*\]|"(?:[^"]|"")*"|[A-Za-z_#][\w$#-]*)(?:\s*\.\s*(?:\[(?:[^\]]|\]\])*\]|"(?:[^"]|"")*"|[A-Za-z_#][\w$#-]*)){0,3}'
DEFINITION = re.compile(rf'\b(?:CREATE\s+(?:OR\s+ALTER\s+)?|ALTER\s+)(?P<kind>PROC(?:EDURE)?|TABLE|VIEW|FUNCTION|SYNONYM)\s+(?P<name>{IDENT})', re.I)
STATEMENT = re.compile(r'\b(?:CREATE\s+(?:OR\s+ALTER\s+)?VIEW|WITH|SELECT|INSERT|UPDATE|DELETE|MERGE|EXEC(?:UTE)?|SET|DECLARE|RAISERROR|RETURN)\b', re.I)
DEFINITION_STATEMENT = re.compile(r'\b(?:CREATE\s+TABLE|ALTER\s+TABLE|CREATE\s+(?:OR\s+ALTER\s+)?VIEW|WITH|SELECT|INSERT|UPDATE|DELETE|MERGE|EXEC(?:UTE)?|SET|DECLARE|RAISERROR|RETURN)\b', re.I)
GO = re.compile(r'^[ \t]*GO(?:[ \t]+\d+)?[ \t]*\r?$', re.I | re.M)
NEXT_STATEMENT = re.compile(r'\b(?:SELECT|INSERT|UPDATE|DELETE|MERGE|EXEC(?:UTE)?|SET|DECLARE|RAISERROR|RETURN|IF|ELSE|BEGIN|END|CASE)\b', re.I)
NEXT_DEFINITION_STATEMENT = re.compile(r'\b(?:CREATE|ALTER|DROP|SELECT|INSERT|UPDATE|DELETE|MERGE|EXEC(?:UTE)?|SET|DECLARE|RAISERROR|RETURN|IF|ELSE|BEGIN|END|CASE)\b', re.I)
WORD = re.compile(r'[\w$#]+')
VARIABLE_ASSIGNMENT = re.compile(r'\s+@')
IDENTITY_INSERT = re.compile(r'\s+IDENTITY_INSERT\b', re.I)
BROKEN_COMPARISON = re.compile(r'!\s+=')
INSERT_TABLOCK = re.compile(
    rf'^\s*INSERT\s+(?:INTO\s+)?{IDENT}\s+(?P<hint>WITH\s*\(\s*TABLOCK\s*\))', re.I)


def _next_statement_boundary(text: str, masked: str, start: int, limit: int, matcher: re.Pattern = STATEMENT) -> int:
    """Find a boundary lexically, without parsing incomplete SQL prefixes."""
    depth = 0
    case_depth = 0
    first = matcher.match(masked, start)
    kind = first.group().upper() if first else ''
    select_seen = kind == 'SELECT'
    cte_consumer_seen = False
    malformed_comparison = False
    last_word = ''
    line_has_code = False
    i = start
    while i < limit:
        char = masked[i]
        if char == '\n':
            line_has_code = False
        # Keep ambiguous boundaries after the malformed `! =` operator on the
        # established fallback path instead of inferring new lineage from them.
        if char == '!' and BROKEN_COMPARISON.match(masked, i, limit):
            malformed_comparison = True
        if char in '["':
            line_has_code = True
            close = ']' if char == '[' else '"'
            i += 1
            while i < limit:
                if masked[i] == close:
                    if i + 1 < limit and masked[i + 1] == close:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if char == '(':
            depth += 1
        elif char == ')':
            depth = max(0, depth - 1)
        elif char == ';' and depth == 0:
            return i + 1
        elif depth == 0 and i > start:
            next_matcher = NEXT_DEFINITION_STATEMENT if matcher is DEFINITION_STATEMENT else NEXT_STATEMENT
            match = next_matcher.match(masked, i)
            if match and (i == 0 or not (masked[i - 1].isalnum() or masked[i - 1] in '_#$')):
                keyword = match.group().upper()
                if keyword == 'CASE':
                    case_depth += 1
                    line_has_code = True
                    i = match.end()
                    continue
                if keyword == 'END' and case_depth:
                    case_depth -= 1
                    line_has_code = True
                    i = match.end()
                    continue
                if case_depth:
                    line_has_code = True
                    i = match.end()
                    continue
                leading = not line_has_code
                if kind in ('EXEC', 'EXECUTE') and keyword not in ('RAISERROR', 'RETURN', 'IF', 'ELSE', 'BEGIN', 'END'):
                    pass
                elif kind == 'WITH' and not cte_consumer_seen and keyword in ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'MERGE'):
                    cte_consumer_seen = True
                    select_seen = keyword == 'SELECT'
                    if keyword != 'SELECT':
                        kind = keyword
                elif keyword == 'SELECT' and not select_seen and kind in ('INSERT', 'CREATE VIEW', 'CREATE OR ALTER VIEW'):
                    select_seen = True
                elif keyword == 'SELECT' and last_word in ('UNION', 'ALL', 'EXCEPT', 'INTERSECT'):
                    pass
                elif kind == 'MERGE' and keyword in ('UPDATE', 'INSERT', 'DELETE', 'SET'):
                    pass
                elif keyword == 'SET' and leading and IDENTITY_INSERT.match(masked, match.end(), limit):
                    return i
                elif keyword == 'SET' and not VARIABLE_ASSIGNMENT.match(masked, match.end(), limit):
                    pass
                elif keyword in ('RAISERROR', 'RETURN', 'IF', 'ELSE', 'BEGIN', 'END') or (leading and not malformed_comparison):
                    return i
                line_has_code = True
                i = match.end()
                continue
        if depth == 0 and (char.isalpha() or char == '_'):
            word = WORD.match(masked, i, limit)
            if word:
                last_word = word.group().upper()
                line_has_code = True
                i = word.end()
                continue
        if not char.isspace():
            line_has_code = True
        i += 1
    return limit


def mask_noncode(text: str) -> str:
    """Mask comments and string literals without changing offsets or line breaks."""
    result = list(text)
    i = 0
    while i < len(text):
        start = i
        if text.startswith('--', i):
            end = text.find('\n', i)
            i = len(text) if end < 0 else end
        elif text.startswith('/*', i):
            i += 2
            depth = 1
            while i < len(text) and depth:
                if text.startswith('/*', i):
                    depth += 1
                    i += 2
                elif text.startswith('*/', i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
        elif text[i] == "'":
            i += 1
            while i < len(text):
                if text[i] == "'":
                    if i + 1 < len(text) and text[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif text[i] in '["':
            close = ']' if text[i] == '[' else '"'
            i += 1
            while i < len(text):
                if text[i] == close:
                    if i + 1 < len(text) and text[i + 1] == close:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        else:
            i += 1
            continue
        for pos in range(start, i):
            if text[pos] not in '\r\n':
                result[pos] = ' '
    return ''.join(result)


def _sql_for_parsing(sql: str) -> str:
    """Remove only a supported INSERT target TABLOCK from SQLGlot input.

    SQL Server allows this target hint, but SQLGlot's T-SQL INSERT grammar
    currently does not. Preserve offsets, line breaks, and original evidence.
    """
    match = INSERT_TABLOCK.match(mask_noncode(sql))
    if not match:
        return sql
    start, end = match.span('hint')
    replacement = ''.join(c if c in '\r\n' else ' ' for c in sql[start:end])
    return sql[:start] + replacement + sql[end:]


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    path: str
    line: int
    scope: str | None = None
    excerpt: str = ''


@dataclass(frozen=True)
class Statement:
    path: str
    line: int
    start: int
    end: int
    text: str
    procedure: str | None
    scope: str
    parse_status: str
    tree: exp.Expression | None = None
    parse_error: str | None = None


@dataclass(frozen=True)
class Resolution:
    status: str
    candidates: tuple[Symbol, ...]


class SourceIndex:
    def __init__(self) -> None:
        self.symbols: list[Symbol] = []
        self.statements: list[Statement] = []
        # Populated by variable analysis when procedure signatures are needed.
        self.procedure_headers: list[Statement] = []

    def add(self, source: SourceFile, include_ddl: bool = False) -> None:
        text = source.text
        masked = mask_noncode(text)
        definitions = list(DEFINITION.finditer(masked))
        procedures = [m for m in definitions if m.group('kind').lower().startswith('proc')]
        boundaries = sorted([m.start() for m in GO.finditer(masked)] + [m.start() for m in procedures] + [len(text)])

        def context(offset: int) -> tuple[str | None, str]:
            previous = [m for m in procedures if m.start() <= offset]
            proc = previous[-1] if previous else None
            prior_batches = [m.start() for m in GO.finditer(masked) if m.start() < offset]
            if proc and prior_batches and proc.start() < prior_batches[-1]:
                proc = None
            name = proc.group('name') if proc else None
            batch = f'<batch@{prior_batches[-1] if prior_batches else 0}>'
            return name, f'{source.path}::{normalize_name(name) if name else batch}'

        for match in definitions:
            kind = match.group('kind').lower()
            kind = 'stored_procedure' if kind.startswith('proc') else kind
            self.symbols.append(Symbol(match.group('name'), kind, str(source.path),
                                       text.count('\n', 0, match.start()) + 1,
                                       excerpt=text[match.start():text.find('\n', match.start()) if '\n' in text[match.start():] else len(text)].strip()))
        matcher = DEFINITION_STATEMENT if include_ddl else STATEMENT
        for match in matcher.finditer(masked):
            start = match.start()
            # Nested SELECTs belong to the containing statement, not separate records.
            if self.statements and self.statements[-1].path == str(source.path) and start < self.statements[-1].end:
                continue
            next_boundary = next(boundary for boundary in boundaries if boundary > start)
            end = _next_statement_boundary(text, masked, start, next_boundary, matcher)
            sql = text[start:end]
            procedure, scope = context(start)
            status = 'parsed'
            parsed = None
            error = None
            try:
                parsed = sqlglot.parse_one(_sql_for_parsing(sql), read='tsql', error_level=ErrorLevel.RAISE)
                if parsed is None or isinstance(parsed, exp.Command):
                    status = 'fallback'
                    error = 'SQLGlot returned no statement' if parsed is None else 'SQLGlot returned an unsupported Command'
            except (SqlglotError, ValueError) as exc:
                status = 'fallback'
                error = re.sub(r'\x1b\[[0-9;]*m', '', f'{type(exc).__name__}: {exc}')
            self.statements.append(Statement(str(source.path), text.count('\n', 0, start) + 1,
                                             start, end, sql, procedure, scope, status, parsed, error))
            for temp in re.finditer(r'(?<![\w#])#(?!#)[A-Za-z_][\w$#]*', mask_noncode(sql)):
                name = temp.group()
                symbol = Symbol(name, 'temporary_table', str(source.path),
                                text.count('\n', 0, start + temp.start()) + 1, scope)
                if not any(normalize_name(s.name) == normalize_name(name) and s.scope == scope for s in self.symbols):
                    self.symbols.append(symbol)

    def resolve(self, name: str, scope: str | None = None) -> Resolution:
        key = normalize_name(name)
        candidates = [s for s in self.symbols if (s.scope is None or s.scope == scope)]
        exact = [s for s in candidates if normalize_name(s.name) == key]
        matches = exact or [s for s in candidates if normalize_name(s.name).endswith('.' + key)]
        identities = {(normalize_name(s.name), s.scope, s.kind) for s in matches}
        status = 'unresolved' if not matches else 'resolved' if len(identities) == 1 else 'ambiguous'
        return Resolution(status, tuple(sorted(matches, key=lambda s: (normalize_name(s.name), s.path, s.line))))
