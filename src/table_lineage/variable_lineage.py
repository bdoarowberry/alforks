"""Conservative static variable and procedure-argument lineage."""
from __future__ import annotations

from collections import defaultdict
from bisect import bisect_left
from dataclasses import dataclass
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from .index import DEFINITION, GO, Statement, mask_noncode
from .models import normalize_name
from .parser import table_name
from .statement_evidence import expression_span


def _name(node):
    if not isinstance(node, exp.Parameter):
        return None
    if isinstance(node.this, exp.Parameter):
        return '@@' + node.this.name.lstrip('@')
    name = node.name.lstrip('@')
    return '@' + name if name else None


def variables(expression):
    if not isinstance(expression, exp.Expression):
        return []
    result = []
    for node in expression.find_all(exp.Parameter):
        if isinstance(node.parent, exp.Parameter):
            continue
        name = _name(node)
        if name and name.casefold() not in {item.casefold() for item in result}:
            result.append(name)
    return result


def _parse_expression(code):
    try:
        query = sqlglot.parse_one('SELECT ' + code, read='tsql', error_level=ErrorLevel.RAISE)
        return query.expressions[0] if isinstance(query, exp.Select) and len(query.expressions) == 1 else None
    except (SqlglotError, ValueError, AttributeError):
        return None


def _segments(text, prefix):
    """Find comma-separated top-level declarators in original source text."""
    masked = mask_noncode(text)
    match = re.match(prefix, masked, re.I)
    if not match:
        return []
    start = match.end()
    depth = 0
    result = []
    i = start
    while i < len(masked):
        c = masked[i]
        if c in '["':
            close = ']' if c == '[' else '"'
            i += 1
            while i < len(masked):
                if masked[i] == close:
                    if i + 1 < len(masked) and masked[i + 1] == close:
                        i += 2
                        continue
                    break
                i += 1
        elif c == '(':
            depth += 1
        elif c == ')':
            depth = max(0, depth - 1)
        elif depth == 0 and c in ',;':
            result.append((start, i))
            start = i + 1
            if c == ';':
                break
        i += 1
    if start < len(masked):
        result.append((start, len(masked)))
    return result


def _declaration_span(statement, name):
    matches = []
    masked = mask_noncode(statement.text)
    for start, end in _segments(statement.text, r'\s*DECLARE\b'):
        match = re.match(r'\s*(@[A-Za-z_][\w$#]*)\b', masked[start:end], re.I)
        if match and match.group(1).casefold() == name.casefold():
            first = start + match.start(1)
            last = end
            while last > first and statement.text[last - 1].isspace():
                last -= 1
            matches.append((first, last))
    return matches[0] if len(matches) == 1 else None


def _statement_name_span(statement, name):
    masked = mask_noncode(statement.text)
    pattern = r'(?<![\w@])' + re.escape(name) + r'(?![\w$#])'
    matches = list(re.finditer(pattern, masked, re.I))
    return (matches[0].start(), matches[0].end()) if len(matches) == 1 else None


def _value_code(statement, value, declaration_span=None, compound=False):
    """Prefer verified original SQL; SQLGlot can normalize function names."""
    if not isinstance(value, exp.Expression):
        return None
    if compound:
        match = re.match(r'\s*SET\s+@[A-Za-z_]\w*\s*(?:\+=|-=|\*=|/=)\s*(.*?)\s*;?\s*$', statement.text, re.I | re.S)
        if match:
            candidate = match.group(1).rstrip(';').rstrip()
            parsed = _parse_expression(candidate)
            if parsed is not None and parsed.sql(dialect='tsql') == value.sql(dialect='tsql'):
                return candidate
    span = None if compound else expression_span(statement, value)
    if span is None and declaration_span:
        start, end = declaration_span
        masked = mask_noncode(statement.text[start:end])
        match = re.search(r'(?:\+=|-=|\*=|/=|=)', masked)
        if match:
            first = start + match.end()
            while first < end and statement.text[first].isspace():
                first += 1
            last = end
            while last > first and statement.text[last - 1].isspace():
                last -= 1
            candidate = statement.text[first:last].rstrip(';').rstrip()
            parsed = _parse_expression(candidate)
            if parsed is not None and parsed.sql(dialect='tsql') == value.sql(dialect='tsql'):
                return candidate
    if span:
        return statement.text[span[0]:span[1]]
    return value.sql(dialect='tsql')


@dataclass
class Assignment:
    name: str
    scope: str
    offset: int
    statement: Statement
    value: exp.Expression | None
    kind: str
    span: tuple[int, int] | None
    conditional: bool = False
    row_dependent: bool = False
    compound: bool = False
    sources: tuple[str, ...] = ()


@dataclass
class Signature:
    name: str
    scope: str
    statement: Statement
    parameters: list
    parsed: bool


@dataclass
class Call:
    callee: str
    statement: Statement
    arguments: list
    incomplete: bool = False


class VariableCatalog:
    def __init__(self, sources, evidence, max_depth=30, capture_locations=True):
        self.sources = sources
        self.evidence = evidence
        self.max_depth = max_depth
        self.capture_locations = capture_locations
        self.assignments = defaultdict(list)
        self.signatures = defaultdict(list)
        self.calls = []
        self.controls = {}
        self.scope_starts = {}
        self.scope_collisions = set()
        self.procedure_scopes = set()
        self._index()

    def _evidence(self, statement, role, span=None):
        item = {'evidence': [self.evidence(statement)]}
        if self.capture_locations:
            item['_evidence_spans'] = [(role, span)]
        return item

    @staticmethod
    def _header_end(masked, start, limit):
        depth = 0
        i = start
        while i < limit:
            char = masked[i]
            if char in '["':
                close = ']' if char == '[' else '"'
                i += 1
                while i < limit:
                    if masked[i] == close:
                        if i + 1 < limit and masked[i + 1] == close:
                            i += 2
                            continue
                        break
                    i += 1
            elif char == '(':
                depth += 1
            elif char == ')':
                depth = max(0, depth - 1)
            elif depth == 0 and char.upper() == 'A' and re.match(r'(?i)AS\b', masked[i:i + 3]) and (i == 0 or not (masked[i - 1].isalnum() or masked[i - 1] in '_$#')):
                if not re.search(r'\bEXEC(?:UTE)?\s*$', masked[start:i], re.I):
                    return i + 2
            elif depth == 0 and char == ';':
                return None
            i += 1
        return None

    def _headers(self, source, masked):
        matches = [m for m in DEFINITION.finditer(masked) if m.group('kind').lower().startswith('proc')]
        batches = list(GO.finditer(masked))
        for match in matches:
            limit = min([m.start() for m in matches if m.start() > match.start()] +
                        [m.start() for m in batches if m.start() > match.start()] + [len(masked)])
            end = self._header_end(masked, match.end(), limit)
            scope = f'{source.index.symbols[0].path}::{normalize_name(match.group("name"))}' if source.index.symbols else None
            if not end or not scope:
                continue
            text = source.text[match.start():end]
            tree = None
            try:
                tree = sqlglot.parse_one(text + ' SELECT 1;', read='tsql', error_level=ErrorLevel.RAISE)
            except (SqlglotError, ValueError, AttributeError):
                pass
            statement = Statement(source.index.symbols[0].path, source.text.count('\n', 0, match.start()) + 1,
                                  match.start(), end, text, match.group('name'), scope,
                                  'parsed' if tree is not None else 'fallback', tree)
            if not any(existing.start == statement.start and existing.end == statement.end for existing in source.index.procedure_headers):
                source.index.procedure_headers.append(statement)
            if scope in self.scope_starts:
                self.scope_collisions.add(scope)
            self.scope_starts[scope] = end
            params = []
            if isinstance(tree, exp.Create) and isinstance(tree.this, exp.StoredProcedure):
                for column in tree.this.expressions:
                    name = _name(column.this) if isinstance(column, exp.ColumnDef) else None
                    if name:
                        default = column.args.get('default')
                        params.append((name, default if isinstance(default, exp.Expression) else None))
            parsed = isinstance(tree, exp.Create) and isinstance(tree.this, exp.StoredProcedure)
            self.signatures[normalize_name(match.group('name'))].append(Signature(match.group('name'), scope, statement, params, parsed))

    def _conditional(self, statement):
        start = self.scope_starts.get(statement.scope)
        if start is None:
            batch = re.search(r'<batch@(\d+)>$', statement.scope)
            start = int(batch.group(1)) if batch else 0
        positions = self.controls.get(statement.path, [])
        position = bisect_left(positions, start)
        return position < len(positions) and positions[position] < statement.start

    def _add(self, statement, name, value, kind, span=None, **kwargs):
        if not name or name.startswith('@@'):
            return
        if isinstance(value, exp.Expression) and value.find(exp.Select):
            kwargs['row_dependent'] = True
            kwargs.setdefault('sources', tuple(table_name(t) for t in value.find_all(exp.Table)))
        self.assignments[(statement.scope, name.casefold())].append(
            Assignment(name, statement.scope, statement.start + (span[0] if span else 0), statement, value, kind, span,
                       conditional=self._conditional(statement), **kwargs))

    def _index(self):
        for source in self.sources:
            if not source.index.statements:
                continue
            path = source.index.statements[0].path
            masked = mask_noncode(source.text)
            self.controls[path] = [m.start() for m in re.finditer(r'\b(?:IF|ELSE|WHILE|GOTO|RETURN|TRY|CATCH)\b', masked, re.I)]
            self._headers(source, masked)
            for statement in source.index.statements:
                if statement.procedure:
                    self.procedure_scopes.add(statement.scope)
                tree = statement.tree
                if isinstance(tree, exp.Declare):
                    for item in tree.expressions:
                        if not isinstance(item, exp.DeclareItem):
                            continue
                        default = item.args.get('default')
                        for target in item.this or []:
                            name = _name(target)
                            self._add(statement, name, default if isinstance(default, exp.Expression) else None,
                                      'declare_initializer' if isinstance(default, exp.Expression) else 'declaration',
                                      _declaration_span(statement, name) if name else None)
                elif isinstance(tree, exp.Set):
                    for item in tree.expressions:
                        eq = item.this
                        if isinstance(eq, exp.EQ) and isinstance(eq.this, exp.Parameter):
                            name = _name(eq.this)
                            self._add(statement, name, eq.expression, 'set', _statement_name_span(statement, name) if name else None)
                elif isinstance(tree, exp.Select) and not tree.args.get('into'):
                    sources = tuple(table_name(t) for t in tree.find_all(exp.Table))
                    for eq in tree.expressions:
                        if isinstance(eq, exp.EQ) and isinstance(eq.this, exp.Parameter):
                            name = _name(eq.this)
                            self._add(statement, name, eq.expression, 'select_assignment', _statement_name_span(statement, name) if name else None,
                                      row_dependent=any(tree.args.get(key) for key in ('from_', 'where', 'having', 'group', 'qualify', 'limit', 'offset', 'joins')),
                                      sources=sources)
                elif isinstance(tree, exp.Execute):
                    if isinstance(tree.this, exp.Table):
                        self.calls.append(Call(table_name(tree.this), statement, list(tree.expressions),
                                               len(re.findall(r'\bEXEC(?:UTE)?\b', mask_noncode(statement.text), re.I)) > 1))
                    for argument in tree.expressions:
                        value = argument.expression if isinstance(argument, exp.EQ) else argument
                        if isinstance(value, exp.Alias) and value.alias.upper() in ('OUT', 'OUTPUT'):
                            output_value = value.this.expression if isinstance(value.this, exp.EQ) else value.this
                            name = _name(output_value)
                            self._add(statement, name, None, 'output_parameter', _statement_name_span(statement, name) if name else None)
                    returned = tree.args.get('return_status')
                    if isinstance(returned, exp.Parameter):
                        name = _name(returned)
                        self._add(statement, name, None, 'return_status', _statement_name_span(statement, name) if name else None)
                    # SQLGlot may retain only the first of adjacent EXEC calls.
                    # Detect output mutations in the original statement too.
                    masked_exec = mask_noncode(statement.text)
                    for match in re.finditer(r'(?<![\w@])(?P<name>@[A-Za-z_]\w*)\s+(?:OUT|OUTPUT)\b', masked_exec, re.I):
                        name = match.group('name')
                        if not any(a.statement is statement and a.kind == 'output_parameter' for a in self.assignments.get((statement.scope, name.casefold()), [])):
                            self._add(statement, name, None, 'output_parameter', (match.start('name'), match.end('name')))
                    for match in re.finditer(r'\bEXEC(?:UTE)?\s+(?P<name>@[A-Za-z_]\w*)\s*=', masked_exec, re.I):
                        name = match.group('name')
                        if not any(a.statement is statement and a.kind == 'return_status' for a in self.assignments.get((statement.scope, name.casefold()), [])):
                            self._add(statement, name, None, 'return_status', (match.start('name'), match.end('name')))
                if tree is None or isinstance(tree, exp.Command):
                    match = re.match(r'\s*SET\s+(?P<name>@[A-Za-z_]\w*)\s*(?P<op>\+=|-=|\*=|/=)\s*(?P<value>.*?)\s*;?\s*$', statement.text, re.I | re.S)
                    if match:
                        expression = _parse_expression(match.group('value'))
                        self._add(statement, match.group('name'), expression, 'compound_assignment',
                                  (match.start('name'), match.end('name')), compound=True)
                    else:
                        masked = mask_noncode(statement.text)
                        if re.match(r'\s*(?:SET|SELECT|DECLARE)\b', masked, re.I):
                            # A failed multi-assignment may mutate any of these
                            # variables. Preserve an unknown boundary for each.
                            for match in re.finditer(r'(?<![\w@])(?P<name>@[A-Za-z_]\w*)\s*(?:\+?=)', masked, re.I):
                                self._add(statement, match.group('name'), None, 'unsupported_assignment', (match.start('name'), match.end('name')))
                            if re.match(r'\s*DECLARE\b', masked, re.I):
                                for start, end in _segments(statement.text, r'\s*DECLARE\b'):
                                    match = re.match(r'\s*(?P<name>@[A-Za-z_]\w*)\b', masked[start:end], re.I)
                                    if match and '=' in masked[start:end]:
                                        self._add(statement, match.group('name'), None, 'unsupported_assignment', (start + match.start('name'), start + match.end('name')))
                        elif re.match(r'\s*EXEC(?:UTE)?\b', masked, re.I):
                            for match in re.finditer(r'(?<![\w@])(?P<name>@[A-Za-z_]\w*)\s+(?:OUT|OUTPUT)\b', masked, re.I):
                                self._add(statement, match.group('name'), None, 'output_parameter', (match.start('name'), match.end('name')))
        for items in self.assignments.values():
            items.sort(key=lambda a: (a.offset, a.span or (0, 0)))
        self.calls.sort(key=lambda c: (c.statement.path, c.statement.start, c.callee))

    def expression(self, code, statement):
        expression = _parse_expression(code)
        if expression is None:
            masked = mask_noncode(code)
            names = []
            for match in re.finditer(r'(?<![\w@])@@?(?:\[(?:[^\]]|\]\])*\]|[A-Za-z_][\w$#]*)', masked):
                name = match.group().replace('[', '').replace(']', '')
                if name.casefold() not in {existing.casefold() for existing in names}:
                    names.append(name)
            return [{'name': name, 'kind': 'system_variable' if name.startswith('@@') else 'unknown',
                     'status': 'unresolved', 'assignments': [], 'boundaries': [{'kind': 'expression_parse_failed'}]}
                    for name in names]
        if re.match(r'\s*EXEC(?:UTE)?\b', statement.text, re.I):
            return [{'name': name, 'kind': 'system_variable' if name.startswith('@@') else 'local_variable',
                     'status': 'unresolved', 'assignments': [], 'boundaries': [{'kind': 'dynamic_sql_scope'}]}
                    for name in variables(expression)]
        return [self.trace(name, statement.scope, statement.start, ()) for name in variables(expression)]

    def trace(self, name, scope, before, active):
        key = (scope, name.casefold(), before)
        if name.startswith('@@'):
            return {'name': name, 'kind': 'system_variable', 'status': 'partial', 'assignments': [],
                    'boundaries': [{'kind': 'runtime_system_variable'}]}
        if scope in self.scope_collisions:
            return {'name': name, 'kind': 'local_variable', 'status': 'unresolved', 'assignments': [],
                    'boundaries': [{'kind': 'ambiguous_procedure_scope'}]}
        if key in active or len(active) >= self.max_depth:
            return {'name': name, 'kind': 'local_variable', 'status': 'unresolved', 'assignments': [],
                    'boundaries': [{'kind': 'cycle_or_depth'}]}
        active = active + (key,)
        prior = [a for a in self.assignments.get((scope, name.casefold()), []) if a.offset < before]
        # Uninitialized declarations identify scope but do not supply a value.
        candidates = [a for a in prior if a.kind != 'declaration']
        if candidates:
            selected = []
            for assignment in reversed(candidates):
                selected.append(assignment)
                if not assignment.conditional and not assignment.row_dependent and assignment.value is not None:
                    break
            selected.reverse()
            output = []
            boundaries = []
            for a in selected:
                inputs = [self.trace(n, a.scope, a.offset, active) for n in variables(a.value)]
                if a.compound and not any(v['name'].casefold() == a.name.casefold() for v in inputs):
                    inputs.append(self.trace(a.name, a.scope, a.offset, active))
                entry = {'value_code': _value_code(a.statement, a.value, a.span, a.compound),
                         'assignment_kind': a.kind, 'inputs': inputs,
                         'source_columns': [c.sql(dialect='tsql') for c in a.value.find_all(exp.Column)] if a.value is not None else [],
                         'source_relations': list(a.sources),
                         **self._evidence(a.statement, 'variable_declaration' if a.kind == 'declare_initializer' else 'variable_assignment', a.span)}
                if self.capture_locations and a.value is not None and not a.compound and a.kind != 'declare_initializer':
                    entry['_evidence_spans'].append(('variable_value', expression_span(a.statement, a.value)))
                output.append(entry)
                if a.row_dependent:
                    boundaries.append({'kind': 'query_row_dependent'})
                if a.kind in ('output_parameter', 'return_status', 'unsupported_assignment') or a.value is None:
                    boundaries.append({'kind': a.kind})
                if a.conditional:
                    boundaries.append({'kind': 'control_flow', 'reason': 'Assignment reachability is not proven.'})
                if any(v['status'] != 'resolved' for v in inputs):
                    boundaries.append({'kind': 'input_unresolved'})
            prior_possible = selected[0].conditional or selected[0].row_dependent or selected[0].value is None
            signature = next((sig for group in self.signatures.values() for sig in group if sig.scope == scope and
                              any(p[0].casefold() == name.casefold() for p in sig.parameters)), None)
            prior_value = self._parameter(name, signature, active) if prior_possible and signature else None
            if prior_possible and prior_value is None:
                boundaries.append({'kind': 'prior_value_unknown'})
            result = {'name': name, 'kind': 'procedure_parameter' if signature else 'local_variable',
                      'status': 'partial' if boundaries or prior_value else 'resolved',
                      'assignments': output, 'boundaries': boundaries}
            if prior_value is not None:
                result['prior_value'] = prior_value
            return result
        signature = next((sig for group in self.signatures.values() for sig in group if sig.scope == scope and
                          any(p[0].casefold() == name.casefold() for p in sig.parameters)), None)
        if signature:
            return self._parameter(name, signature, active)
        scope_signatures = [sig for group in self.signatures.values() for sig in group if sig.scope == scope]
        if not prior and scope in self.procedure_scopes and (not scope_signatures or not all(sig.parsed for sig in scope_signatures)):
            return {'name': name, 'kind': 'unknown', 'status': 'unresolved', 'assignments': [],
                    'boundaries': [{'kind': 'unparsed_procedure_signature'}]}
        return {'name': name, 'kind': 'local_variable', 'status': 'unresolved', 'assignments': [],
                'boundaries': [{'kind': 'uninitialized_or_missing_assignment'}]}

    def _parameter(self, name, signature, active):
        key = normalize_name(signature.name)
        definitions = self.signatures.get(key, [])
        boundaries = [{'kind': 'external_caller', 'reason': 'Repository calls may not be exhaustive.'}]
        assignments = []
        index = next(i for i, (param, _) in enumerate(signature.parameters) if param.casefold() == name.casefold())
        default = signature.parameters[index][1]
        if len(definitions) != 1:
            boundaries.append({'kind': 'ambiguous_procedure_definition'})
        else:
            for call in self.calls:
                called = normalize_name(call.callee)
                matching = [candidate for candidate in self.signatures if candidate == called or candidate.endswith('.' + called)]
                if key not in matching or len(matching) != 1:
                    continue
                named = {}
                positional = []
                output_names = set()
                output_positions = set()
                invalid_arguments = False
                for arg in call.arguments:
                    is_output = isinstance(arg, exp.Alias) and arg.alias.upper() in ('OUT', 'OUTPUT')
                    actual = arg.this if is_output else arg
                    if isinstance(actual, exp.EQ) and isinstance(actual.this, exp.Parameter):
                        argument_name = _name(actual.this).casefold()
                        if argument_name in named:
                            invalid_arguments = True
                        named[argument_name] = actual.expression
                        if is_output:
                            output_names.add(argument_name)
                    else:
                        if named:
                            invalid_arguments = True
                        if is_output:
                            output_positions.add(len(positional))
                        positional.append(actual)
                valid_names = {param.casefold() for param, _ in signature.parameters}
                if len(positional) > len(signature.parameters) or any(param not in valid_names for param in named):
                    invalid_arguments = True
                if any(signature.parameters[i][0].casefold() in named for i in range(min(len(positional), len(signature.parameters)))):
                    invalid_arguments = True
                value = named.get(name.casefold())
                if value is None and index < len(positional):
                    value = positional[index]
                using_default = value is None or isinstance(value, exp.Column) and value.name.upper() == 'DEFAULT'
                if call.incomplete or invalid_arguments:
                    boundaries.append({'kind': 'ambiguous_call_arguments', **self._evidence(call.statement, 'call_argument')})
                    continue
                if name.casefold() in output_names or index in output_positions:
                    boundaries.append({'kind': 'output_parameter_mutation', **self._evidence(call.statement, 'call_argument')})
                if using_default:
                    if default is None:
                        boundaries.append({'kind': 'missing_argument', **self._evidence(call.statement, 'call_argument')})
                        continue
                    value = default
                inputs = [] if using_default else [self.trace(n, call.statement.scope, call.statement.start, active) for n in variables(value)]
                evidence_statement = signature.statement if using_default else call.statement
                span = _statement_name_span(signature.statement, name) if using_default else expression_span(call.statement, value)
                assignments.append({'value_code': _value_code(evidence_statement, value),
                                    'assignment_kind': 'parameter_default' if using_default else 'call_argument',
                                    'inputs': inputs, **self._evidence(evidence_statement, 'parameter_default' if using_default else 'call_argument', span)})
                if using_default:
                    assignments[-1]['call_evidence'] = [self.evidence(call.statement)]
                if any(v['status'] != 'resolved' for v in inputs):
                    boundaries.append({'kind': 'input_unresolved'})
        return {'name': name, 'kind': 'procedure_parameter', 'status': 'partial' if assignments else 'unresolved',
                'assignments': assignments, 'boundaries': boundaries,
                'declared_default_code': _value_code(signature.statement, default),
                **self._evidence(signature.statement, 'procedure_parameter', _statement_name_span(signature.statement, name))}
