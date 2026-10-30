"""Portable, deduplicated source statements and verified element locations."""
from __future__ import annotations

from bisect import bisect_right
from functools import lru_cache
import hashlib
import json
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError, ErrorLevel


@lru_cache(maxsize=32)
def _tokens(text):
    return tuple(sqlglot.Dialect.get_or_raise('tsql').tokenizer().tokenize(text))


def _line_at(text: str, offset: int, first_line: int) -> int:
    return first_line + text.count('\n', 0, offset)


def _last_line(text: str, first_line: int) -> int:
    return _line_at(text, max(0, len(text) - 1), first_line)


def identifier_span(statement, expression):
    """Use identifier token metadata only when it points to the original token."""
    if re.match(r'\s*EXEC(?:UTE)?\b', statement.text, re.I):
        return None
    node = expression.this if isinstance(expression, exp.Column) else expression
    if not isinstance(node, exp.Identifier):
        return None
    start, end = node.meta.get('start'), node.meta.get('end')
    if not isinstance(start, int) or not isinstance(end, int) or not (0 <= start <= end < len(statement.text)):
        return None
    raw = statement.text[start:end + 1]
    try:
        parsed = sqlglot.parse_one('SELECT ' + raw, read='tsql', error_level=ErrorLevel.RAISE)
    except (SqlglotError, ValueError, AttributeError):
        return None
    if not isinstance(parsed, exp.Select) or len(parsed.expressions) != 1:
        return None
    if parsed.expressions[0].name.lower() != node.name.lower():
        return None
    return (start, end + 1)


def expression_span(statement, expression):
    """Find an AST-anchored span only after reparsing it verifies the expression."""
    if re.match(r'\s*EXEC(?:UTE)?\b', statement.text, re.I):
        return None
    positions = [(n.meta.get('start'), n.meta.get('end')) for n in expression.walk()
                 if isinstance(n.meta.get('start'), int) and isinstance(n.meta.get('end'), int)]
    if not positions:
        # SQLGlot currently omits source metadata for T-SQL variables. A
        # unique token sequence is safe to locate; repeated variables are not.
        if isinstance(expression, exp.Parameter):
            try:
                tokens = _tokens(statement.text)
            except (SqlglotError, ValueError, AttributeError):
                return None
            expected = expression.sql(dialect='tsql').casefold()
            matches = [(tokens[i].start, tokens[i + 1].end + 1) for i in range(len(tokens) - 1)
                       if tokens[i].text == '@' and tokens[i].end + 1 == tokens[i + 1].start
                       and ('@' + tokens[i + 1].text).casefold() == expected]
            return matches[0] if len(matches) == 1 else None
        return None
    start = min(p[0] for p in positions)
    end = max(p[1] for p in positions)
    if not (0 <= start <= end < len(statement.text)):
        return None
    try:
        tokens = _tokens(statement.text)
    except (SqlglotError, ValueError, AttributeError):
        return None
    left = next((i for i, t in enumerate(tokens) if t.start <= start <= t.end), None)
    right = next((i for i, t in enumerate(tokens) if t.start <= end <= t.end), None)
    if left is None or right is None or left > right:
        return None
    expected = expression.sql(dialect='tsql')
    # Metadata marks leaf tokens. Include nearby punctuation/keywords when a
    # parent expression lacks its own span; never search by column name alone.
    for extra in range(0, 25):
        for before in range(extra + 1):
            after = extra - before
            lo, hi = left - before, right + after
            if lo < 0 or hi >= len(tokens):
                continue
            first, last = tokens[lo].start, tokens[hi].end + 1
            if first > start or last <= end:
                continue
            candidate = statement.text[first:last]
            try:
                parsed = sqlglot.parse_one('SELECT ' + candidate, read='tsql', error_level=ErrorLevel.RAISE)
            except (SqlglotError, ValueError, AttributeError):
                continue
            if isinstance(parsed, exp.Select) and len(parsed.expressions) == 1 and parsed.expressions[0].sql(dialect='tsql') == expected:
                return (first, last)
    return None


class StatementRegistry:
    def __init__(self, sources):
        self.sources = {}
        self.by_portable = {}
        self.records = {}
        self.issues = []
        for source in sources:
            statements = source.index.statements + source.index.procedure_headers
            if not statements:
                continue
            path = statements[0].path
            portable = (source.repository, source.path)
            previous = self.by_portable.get(portable)
            if previous is not None and previous != path:
                raise ValueError(f'Ambiguous source label and path: {portable[0]}:{portable[1]}')
            self.by_portable[portable] = path
            ordered = sorted(statements, key=lambda s: (s.start, s.end))
            self.sources[path] = (source, ordered, [s.line for s in ordered])
            if '\ufffd' in source.text:
                self.issues.append({'category': 'source_replacement_character', 'repository': source.repository,
                                    'path': source.path, 'reason': 'Decoded source contains U+FFFD; invalid input bytes may have been replaced.'})

    def _record(self, statement):
        source, _, _ = self.sources[statement.path]
        raw = json.dumps([source.repository, source.path, statement.start, statement.end,
                          hashlib.sha256(statement.text.encode('utf-8')).hexdigest()], ensure_ascii=False, separators=(',', ':'))
        identifier = 'stmt-' + hashlib.sha256(raw.encode('utf-8')).hexdigest()
        self.records.setdefault(identifier, {'id': identifier, 'repository': source.repository, 'path': source.path,
                            'start_line': statement.line, 'end_line': _last_line(statement.text, statement.line),
                            'sql': statement.text})
        return identifier

    def _find(self, path, line, offset=None, excerpt=None):
        info = self.sources.get(path)
        if not info:
            return None
        _, statements, lines = info
        if offset is not None:
            matches = [s for s in statements if s.start == offset]
            return matches[0] if len(matches) == 1 else None
        if not isinstance(line, int):
            return None
        end = bisect_right(lines, line)
        matches = [s for s in statements[:end] if s.line <= line <= _last_line(s.text, s.line)]
        if matches and excerpt:
            prefix = ' '.join(excerpt.split())
            exact = [s for s in matches if prefix in ' '.join(s.text.split())]
            matches = exact
        return matches[0] if len(matches) == 1 else None

    def reference(self, statement, role='statement', span=None):
        identifier = self._record(statement)
        valid = span is not None and 0 <= span[0] < span[1] <= len(statement.text)
        if valid:
            first = _line_at(statement.text, span[0], statement.line)
            last = _line_at(statement.text, span[1] - 1, statement.line)
        else:
            first, last = statement.line, _last_line(statement.text, statement.line)
        return {'statement_id': identifier, 'role': role,
                'location_status': 'exact' if valid else 'statement_only',
                'source': {'start_line': first, 'end_line': last},
                'within_statement': {'start_offset': span[0] if valid else None,
                                     'end_offset': span[1] if valid else None}}

    def convert_evidence(self, evidence, spans=None):
        portable = (evidence.get('repository'), evidence.get('path'))
        path = self.by_portable.get(portable, evidence.get('path'))
        statement = self._find(path, evidence.get('line'), evidence.get('offset'), evidence.get('excerpt'))
        extra = {k: v for k, v in evidence.items() if k not in ('excerpt', 'path', 'repository', 'offset', 'line')}
        if not statement:
            source = self.sources.get(path)
            repo, relative = (source[0].repository, source[0].path) if source else portable
            return [{**extra, 'statement_id': None, 'role': 'statement', 'location_status': 'unavailable',
                     'repository': repo, 'path': relative, 'line': evidence.get('line'),
                     'reason': extra.get('reason') or 'No unique indexed statement could be established.'}]
        refs = [self.reference(statement, role, span) for role, span in (spans or [('statement', None)])]
        return [{**extra, **ref} for ref in refs]

    def graph(self, data):
        for edge in data['edges']:
            edge['evidence'] = [ref for ev in edge['evidence'] for ref in self.convert_evidence(ev)]
        data['diagnostics'] = [ref for ev in data['diagnostics'] for ref in self.convert_evidence(ev)]
        # Parser warnings can embed entire failed statements (or dynamic SQL
        # excerpts). Their structured diagnostics retain the source reference.
        warnings = []
        for warning in data['warnings']:
            if '; statement: ' in warning:
                warning = warning.split('; statement: ', 1)[0] + '; see statement evidence in diagnostics'
            elif ': dynamic SQL; lineage may be incomplete: ' in warning:
                warning = warning.split(': dynamic SQL; lineage may be incomplete: ', 1)[0] + ': dynamic SQL; lineage may be incomplete; see diagnostics'
            warnings.append(warning)
        data['warnings'] = sorted(set(warnings))
        data['schema_version'] = '1.0'
        data['source_diagnostics'] = sorted(self.issues, key=lambda x: (x['repository'], x['path']))
        data['statements'] = self.sorted_records()
        return data

    def definitions(self, data):
        def visit(value):
            if isinstance(value, list):
                return [visit(item) for item in value]
            if not isinstance(value, dict):
                return value
            spans = value.get('_evidence_spans')
            result = {}
            for key, item in value.items():
                if key in ('_evidence_spans', '_definition_evidence_spans'):
                    continue
                if key in ('evidence', 'schema_evidence', 'definition_evidence', 'call_evidence'):
                    relevant_spans = (value.get('_definition_evidence_spans') if key == 'definition_evidence'
                                      else None if key == 'call_evidence' else spans)
                    if isinstance(item, list):
                        result[key] = [ref for ev in item for ref in self.convert_evidence(ev, relevant_spans)]
                    elif isinstance(item, dict):
                        result[key] = self.convert_evidence(item, relevant_spans)[0]
                    else:
                        result[key] = item
                else:
                    result[key] = visit(item)
            return result
        result = visit(data)
        result['schema_version'] = '2.1'
        result['source_diagnostics'] = sorted(self.issues, key=lambda x: (x['repository'], x['path']))
        result['statements'] = self.sorted_records()
        return result

    def sorted_records(self):
        return sorted(self.records.values(), key=lambda r: (r['repository'], r['path'], r['start_line'], r['id']))
