"""Bounded recovery of explicitly evidenced dynamic SELECT INTO candidates."""
from __future__ import annotations

from dataclasses import dataclass
import re

from .index import DEFINITION, IDENT, SourceIndex, Statement, mask_noncode
from .models import normalize_name
from .sources import SourceFile


def _has_declaration_initializer(segment: str, variable: str) -> bool:
    return bool(re.search(r'\bDECLARE\b[^;]*?' + re.escape(variable) +
                          r'\s+(?:\[[^\]]+\]|\w+)(?:\s*\([^)]*\))?\s*=', segment, re.I))


@dataclass(frozen=True)
class DynamicCandidate:
    target: str
    source: str
    condition: str | None
    condition_line: int | None
    condition_excerpt: str | None
    assignment_line: int | None
    assignment_excerpt: str | None
    template_line: int
    template_excerpt: str


def _inside_control(prefix: str) -> bool:
    """Reject a candidate nested inside another recognized conditional or loop."""
    stack: list[str] = []
    case_depth = 0
    for token in re.finditer(r'\b(?:BEGIN|END|CASE)\b', prefix, re.I):
        word = token.group().upper()
        if word == 'CASE':
            case_depth += 1
        elif word == 'END' and case_depth:
            case_depth -= 1
        elif word == 'END':
            if stack:
                stack.pop()
        elif word == 'BEGIN':
            before = prefix[max(0, token.start() - 400):token.start()]
            control = re.search(r'\b(?:IF|WHILE|ELSE)\b[^;\n]*(?:\n\s*)?$', before, re.I)
            stack.append('control' if control else 'block')
    # A control statement may govern the next statement without BEGIN/END.
    pending = re.search(r'\b(?:IF|WHILE)\b[^;\n]*\s*$', prefix, re.I)
    return 'control' in stack or bool(pending)


def _literal_assignment(masked: str, original: str, variable: str) -> tuple[str, int, str] | None:
    assignments = list(re.finditer(r'\b(?:SET|SELECT)\s+' + re.escape(variable) + r'\s*(?:\+=|=)', masked, re.I))
    if len(assignments) != 1:
        return None
    match = assignments[0]
    literal = re.match(r"\s*N?'((?:''|[^'])*)'\s*;", original[match.end():], re.I | re.S)
    if not literal:
        return None
    value = literal.group(1).replace("''", "'")
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$#]*', value):
        return None
    return value, match.start(), original[match.start():match.end() + literal.end()].strip()


def _branches(source: SourceFile, masked: str, start: int, end: int, variable: str) -> list[tuple[str, str, int, str, int, str]]:
    segment = masked[start:end]
    branch_pattern = re.compile(
        r'\bIF\s*\(\s*(?P<condition>@[A-Za-z_]\w*\s*=\s*\d+)\s*\)\s*'
        r'BEGIN\b(?P<yes>.*?)\bEND\s+ELSE\s+BEGIN\b(?P<no>.*?)\bEND\b', re.I | re.S)
    candidates = []
    for branch in branch_pattern.finditer(segment):
        bodies = (branch.group('yes'), branch.group('no'))
        if any(re.search(r'\b(?:IF|ELSE|WHILE|BEGIN|CASE)\b', body, re.I) for body in bodies):
            continue
        if _inside_control(segment[:branch.start()]):
            continue
        values = []
        for name in ('yes', 'no'):
            offset = start + branch.start(name)
            result = _literal_assignment(masked[offset:start + branch.end(name)], source.text[offset:start + branch.end(name)], variable)
            if result is None:
                break
            value, relative, excerpt = result
            absolute = offset + relative
            values.append((value, absolute, excerpt))
        if len(values) == 2:
            candidates.append((branch, values))
    if len(candidates) != 1:
        return []
    branch, values = candidates[0]
    # Any extra assignment or output-parameter mutation invalidates the candidates.
    writes = list(re.finditer(r'\b(?:SET|SELECT)\s+' + re.escape(variable) + r'\s*(?:\+=|=)', segment, re.I))
    if len(writes) != 2 or {start + m.start() for m in writes} != {v[1] for v in values}:
        return []
    assignments = list(re.finditer(re.escape(variable) + r'\s*(?:\+=|=)', segment, re.I))
    if len(assignments) != 2 or _has_declaration_initializer(segment, variable):
        return []
    if re.search(re.escape(variable) + r'\s+(?:OUT|OUTPUT)\b', segment, re.I):
        return []
    predicate = ' '.join(source.text[start + branch.start('condition'):start + branch.end('condition')].split())
    branch_line = source.text.count('\n', 0, start + branch.start()) + 1
    branch_excerpt = source.text[start + branch.start():start + branch.end()].strip()
    return [(values[0][0], predicate, branch_line, branch_excerpt, source.text.count('\n', 0, values[0][1]) + 1, values[0][2]),
            (values[1][0], f'ELSE ({predicate})', branch_line, branch_excerpt, source.text.count('\n', 0, values[1][1]) + 1, values[1][2])]


def recover_select_into(source: SourceFile, index: SourceIndex, execute: Statement) -> list[DynamicCandidate]:
    call = re.fullmatch(r'\s*EXEC(?:UTE)?\s+(?:\[?sys\]?\.)?\[?sp_executesql\]?\s+(?P<var>@\w+)\s*;?\s*', execute.text, re.I)
    if not call or not execute.procedure:
        return []
    sql_var = call.group('var')
    masked = mask_noncode(source.text)
    procedures = [m for m in DEFINITION.finditer(masked) if m.start() < execute.start and
                  m.group('kind').lower().startswith('proc') and
                  normalize_name(m.group('name')) == normalize_name(execute.procedure)]
    if not procedures:
        return []
    proc_start = procedures[-1].start()
    prior = masked[proc_start:execute.start]
    writes = list(re.finditer(r'\b(?:SET|SELECT)\s+' + re.escape(sql_var) + r'\s*(?:\+=|=)', prior, re.I))
    assignments = list(re.finditer(re.escape(sql_var) + r'\s*(?:\+=|=)', prior, re.I))
    if (len(writes) != 1 or len(assignments) != 1 or
            _has_declaration_initializer(prior, sql_var) or
            re.search(re.escape(sql_var) + r'\s+(?:OUT|OUTPUT)\b', prior, re.I)):
        return []
    assignment = next((s for s in index.statements if s.scope == execute.scope and
                       s.start == proc_start + writes[0].start()), None)
    if assignment is None:
        return []
    if _inside_control(masked[proc_start:assignment.start]) or _inside_control(prior):
        return []
    expr = assignment.text[writes[0].end() - writes[0].start():].strip().removesuffix(';').strip()
    token = re.compile(r"\s*(?:(N?'(?:''|[^'])*')|(@[A-Za-z_]\w*)|(\+))", re.I | re.S)
    pieces: list[str] = []
    variables: dict[str, str] = {}
    pos = 0
    expect_value = True
    while pos < len(expr):
        match = token.match(expr, pos)
        if not match:
            return []
        literal, variable, plus = match.groups()
        if bool(plus) == expect_value:
            return []
        if literal:
            value = literal[1:] if literal[0].lower() == 'n' else literal
            pieces.append(value[1:-1].replace("''", "'"))
        elif variable:
            marker = f'__dynamic_variable_{len(variables)}__'
            variables[marker] = variable
            pieces.append(marker)
        expect_value = bool(plus)
        pos = match.end()
    if expect_value or not pieces:
        return []
    template = ''.join(pieces)
    template_masked = mask_noncode(template)
    pattern = re.compile(rf'\bSELECT\b[^;]*?\bINTO\s+(?P<target>{IDENT})\s+FROM\s+(?P<source>{IDENT})', re.I | re.S)
    matches = list(pattern.finditer(template_masked))
    if len(matches) != 1:
        return []
    match = matches[0]
    target = template[match.start('target'):match.end('target')]
    object_name = template[match.start('source'):match.end('source')]
    if any(marker in target for marker in variables):
        return []
    embedded = [marker for marker in variables if marker in object_name]
    if len(embedded) != 1:
        return []
    marker = embedded[0]
    if not re.search(r'(?<![\w])(?:\[' + re.escape(marker) + r'\]|' + re.escape(marker) + r')(?![\w])', object_name):
        return []
    branches = _branches(source, masked, proc_start, assignment.start, variables[marker])
    return [DynamicCandidate(target, object_name.replace(marker, value), condition, line, excerpt, assignment_line, assignment_excerpt,
                             assignment.line, ' '.join(assignment.text.split()))
            for value, condition, line, excerpt, assignment_line, assignment_excerpt in branches]
