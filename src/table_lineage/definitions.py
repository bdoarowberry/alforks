"""Evidence-backed column definitions for the reachable table-lineage graph."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib
import json
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from .index import IDENT, SourceIndex, Statement, mask_noncode
from .models import LineageGraph, normalize_name
from .parser import SqlSourceParser, table_name
from .traversal import object_key
from .statement_evidence import expression_span, identifier_span
from .variable_lineage import VariableCatalog


@dataclass
class DefinitionSource:
    index: SourceIndex
    path: str
    repository: str
    text: str = ''


@dataclass
class Schema:
    name: str
    kind: str
    scope: str | None
    columns: list[dict]
    statement: Statement
    status: str = 'declared'


@dataclass
class Writer:
    name: str
    kind: str
    scope: str | None
    statement: Statement
    tree: exp.Expression


class DefinitionCatalog:
    """Extract schemas and mappings without asserting unproved runtime bindings."""

    def __init__(self, sources: list[DefinitionSource], graph: LineageGraph, capture_locations: bool = False, enable_variable_lineage: bool = True):
        self.sources = sources
        self.graph = graph
        self.capture_locations = capture_locations
        self.enable_variable_lineage = enable_variable_lineage
        self.variable_catalog = None
        self.paths = {s.index.statements[0].path: (s.repository, s.path)
                      for s in sources if s.index.statements}
        self.source_text = {s.index.statements[0].path: s.text for s in sources if s.index.statements}
        self.schemas: dict[str, list[Schema]] = defaultdict(list)
        self.writers: dict[str, list[Writer]] = defaultdict(list)
        self.failed_writers: dict[str, list[tuple[Statement, str]]] = defaultdict(list)
        self.alterations: dict[str, list[Statement]] = defaultdict(list)
        self.names: dict[str, str] = {}
        self.kinds: dict[str, str] = {}
        self.scopes: dict[str, str | None] = {}
        self.diagnostics: dict[str, dict] = {}
        self.cache: dict[str, tuple[list[dict], str]] = {}
        self.active: set[str] = set()
        self.mapping_active: set[str] = set()
        self.max_depth = 100
        self._collect()

    def _portable_scope(self, scope: str | None) -> str | None:
        if not scope:
            return None
        path, separator, tail = scope.partition('::')
        repo, relative = self.paths.get(path, ('source', 'unknown'))
        return f'{repo}:{relative}::{tail}' if separator else None

    def _identity(self, name: str, scope: str | None) -> str:
        return object_key(name, scope)

    def _evidence(self, statement: Statement) -> dict:
        repo, path = self.paths.get(statement.path, ('source', 'unknown'))
        return {'repository': repo, 'path': path, 'line': statement.line,
                'offset': statement.start, 'procedure': statement.procedure,
                'excerpt': ' '.join(statement.text.split())[:500]}

    def _diagnostic(self, statement: Statement, category: str, reason: str, relation: str = '') -> str:
        evidence = self._evidence(statement)
        raw = json.dumps([evidence, category, reason, relation], sort_keys=True)
        identifier = hashlib.sha256(raw.encode()).hexdigest()[:16]
        self.diagnostics[identifier] = {'id': identifier, 'category': category,
                                        'reason': reason, 'relation': relation, 'evidence': evidence}
        return identifier

    def _register(self, name: str, kind: str, scope: str | None) -> str:
        key = self._identity(name, scope)
        self.names.setdefault(key, name)
        self.kinds.setdefault(key, kind)
        self.scopes.setdefault(key, scope if normalize_name(name).startswith('#') else None)
        return key

    def _collect(self) -> None:
        for source in self.sources:
            for statement in self._statements(source):
                tree = statement.tree
                if re.match(r'\s*EXEC(?:UTE)?\s*(?:\(|(?:\[?sys\]?\.)?\[?sp_executesql\]?\b|@\w+)', statement.text, re.I):
                    self._diagnostic(statement, 'dynamic_sql_boundary', 'Dynamic SQL can contain additional column definitions or writers; only complete literal statements are analyzed.')
                if isinstance(tree, exp.Create) and str(tree.args.get('kind', '')).lower() == 'table':
                    schema = tree.this
                    target = schema.this if isinstance(schema, exp.Schema) else schema
                    if not isinstance(target, exp.Table):
                        continue
                    name = table_name(target)
                    kind = 'temp_table' if normalize_name(name).startswith('#') and not normalize_name(name).startswith('##') else 'table'
                    scope = statement.scope if kind == 'temp_table' else None
                    key = self._register(name, kind, scope)
                    columns = []
                    if isinstance(schema, exp.Schema):
                        for item in schema.expressions:
                            if not isinstance(item, exp.ColumnDef):
                                continue
                            computed = next((c.kind for c in item.constraints if isinstance(c.kind, exp.ComputedColumnConstraint)), None)
                            identity = any(isinstance(c.kind, exp.GeneratedAsIdentityColumnConstraint) for c in item.constraints)
                            column = {'name': item.name, 'ordinal': len(columns) + 1,
                                            'computed': computed.this.sql(dialect='tsql') if computed else None,
                                            'identity': identity}
                            if self.capture_locations:
                                column['_definition_span'] = identifier_span(statement, item.this)
                                column['_definition_statement'] = statement
                                if computed:
                                    column['_computed_span'] = expression_span(statement, computed.this)
                            columns.append(column)
                    self.schemas[key].append(Schema(name, kind, scope, columns, statement))
                    continue
                if isinstance(tree, exp.Alter):
                    target = tree.this
                    if isinstance(target, exp.Table):
                        name = table_name(target)
                        key = self._register(name, 'temp_table' if name.startswith('#') else 'table', statement.scope)
                        self.alterations[key].append(statement)
                    continue
                parser = SqlSourceParser(definition_mode=True)
                target_name = parser._target(tree) if tree is not None else None
                if isinstance(tree, exp.Create) and str(tree.args.get('kind', '')).lower() == 'view':
                    target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
                    target_name = table_name(target) if isinstance(target, exp.Table) else None
                if not target_name:
                    if (tree is None or isinstance(tree, exp.Command)) and re.match(r'\s*(?:INSERT|UPDATE|MERGE|CREATE\s+(?:(?:OR\s+ALTER\s+)?VIEW|TABLE)|ALTER\s+TABLE)\b', statement.text, re.I):
                        diagnostic = self._diagnostic(statement, 'unparsed_writer', 'A possible writer could not be parsed.')
                        match = re.match(rf'\s*(?:INSERT\s+(?:INTO\s+)?|UPDATE\s+|MERGE\s+(?:INTO\s+)?|CREATE\s+(?:OR\s+ALTER\s+)?VIEW\s+|CREATE\s+TABLE\s+|ALTER\s+TABLE\s+)(?P<name>{IDENT})', mask_noncode(statement.text), re.I)
                        if match:
                            name = match.group('name')
                            kind = 'temp_table' if normalize_name(name).startswith('#') else 'view' if re.match(r'\s*CREATE\s+(?:OR\s+ALTER\s+)?VIEW', statement.text, re.I) else 'table'
                            key = self._register(name, kind, statement.scope)
                            self.failed_writers[key].append((statement, diagnostic))
                    continue
                kind = ('view' if isinstance(tree, exp.Create) else
                        'temp_table' if normalize_name(target_name).startswith('#') and not normalize_name(target_name).startswith('##') else 'table')
                scope = statement.scope if kind == 'temp_table' else None
                key = self._register(target_name, kind, scope)
                self.writers[key].append(Writer(target_name, kind, scope, statement, tree))

    def _statements(self, source: DefinitionSource):
        """Retain the execution evidence while parsing a bounded literal payload."""
        for statement in source.index.statements:
            yield statement
            literal = re.fullmatch(r"\s*EXEC(?:UTE)?\s*(?:\(\s*N?'((?:''|[^'])*)'\s*\)|(?:\[?sys\]?\.)?\[?sp_executesql\]?\s+N?'((?:''|[^'])*)')\s*;?\s*", statement.text, re.I | re.S)
            if not literal:
                continue
            payload = (literal.group(1) if literal.group(1) is not None else literal.group(2)).replace("''", "'")
            try:
                trees = sqlglot.parse(payload, read='tsql', error_level=ErrorLevel.RAISE)
            except (SqlglotError, ValueError):
                continue
            if len(trees) == 1 and trees[0] is not None and not isinstance(trees[0], exp.Command):
                yield replace(statement, tree=trees[0], parse_status='parsed', parse_error=None)

    def _resolve(self, name: str, scope: str | None) -> tuple[str | None, str, list[str]]:
        key = self._identity(name, scope)
        if key in self.names:
            # A repeated schema definition is ambiguous even with identical SQL names.
            if len([s for s in self.schemas.get(key, []) if s.status == 'declared']) > 1:
                return None, 'ambiguous', [key]
            return key, 'resolved', []
        normalized = normalize_name(name)
        if normalized.startswith('#'):
            return None, 'missing', []
        matches = sorted(k for k in self.names if not k.startswith('temp:') and
                         (k.endswith('.' + normalized) or normalized.endswith('.' + k)))
        return None, 'ambiguous' if len(matches) > 1 else 'candidate' if matches else 'missing', matches

    @staticmethod
    def _query_branches(query: exp.Expression) -> list[exp.Select]:
        if isinstance(query, exp.Select):
            return [query]
        if isinstance(query, (exp.Union, exp.Except, exp.Intersect)):
            return DefinitionCatalog._query_branches(query.this) + DefinitionCatalog._query_branches(query.expression)
        return []

    def _source(self, node: exp.Expression, scope: str, ctes: dict, statement: Statement, depth: int) -> tuple[list[dict], str, str]:
        if isinstance(node, exp.Subquery):
            cols, status = self._query_schema(node.this, scope, ctes, statement, depth + 1)
            return cols, status, node.alias_or_name
        if not isinstance(node, exp.Table) or not isinstance(node.this, exp.Identifier):
            return [], 'missing', node.alias_or_name
        name = table_name(node)
        alias = node.alias_or_name
        if normalize_name(name) in ctes:
            cols, status = self._query_schema(ctes[normalize_name(name)], scope, ctes, statement, depth + 1)
            return cols, status, alias
        key, status, candidates = self._resolve(name, scope)
        if key is None:
            self._diagnostic(statement, 'schema_' + status, f'Source schema {name} is {status}; candidates: {", ".join(candidates)}', name)
            return [], status, alias
        cols, state = self._schema(key, depth + 1)
        if state in ('declared', 'derived') and key not in self.mapping_active:
            self.mapping_active.add(key)
            try:
                cols, _ = self._writes(key, cols, state)
            finally:
                self.mapping_active.remove(key)
        return cols, state, alias

    def _query_schema(self, query: exp.Expression, scope: str, inherited: dict, statement: Statement, depth: int) -> tuple[list[dict], str]:
        if depth > self.max_depth:
            self._diagnostic(statement, 'max_depth', 'Maximum schema expansion depth reached.')
            return [], 'partial'
        ctes = dict(inherited)
        with_clause = query.args.get('with_')
        if with_clause:
            for cte in with_clause.expressions:
                ctes[normalize_name(cte.alias_or_name)] = cte.this
        branches = self._query_branches(query)
        if not branches:
            self._diagnostic(statement, 'unsupported_query', 'Query projections could not be extracted.')
            return [], 'partial'
        result, status = self._projection(branches[0], scope, ctes, statement, depth + 1)
        for col in result:
            col['mappings'] = [{'name': col.get('name'), 'alias': col.get('alias'),
                                'code': col.get('code'), 'mapping_method': 'derived_projection',
                                'status': col.get('status', 'resolved'),
                                'source_mappings': col.get('source_mappings', []),
                                'evidence': [self._evidence(statement)]}]
            if self.capture_locations:
                col['mappings'][0]['_evidence_spans'] = col.get('_evidence_spans', [('statement', None)])
            self._variable_lineage(col['mappings'][0], statement)
        return result, status

    def _projection(self, query: exp.Select, scope: str, ctes: dict, statement: Statement, depth: int) -> tuple[list[dict], str]:
        with_clause = query.args.get('with_')
        if with_clause:
            ctes = dict(ctes)
            ctes.update({normalize_name(c.alias_or_name): c.this for c in with_clause.expressions})
        from_clause = query.args.get('from_')
        nodes = [from_clause.this] if from_clause and from_clause.this else []
        nodes.extend(j.this for j in query.args.get('joins') or [])
        visible = []
        for node in nodes:
            cols, status, alias = self._source(node, scope, ctes, statement, depth + 1)
            visible.append((alias, cols, status))
        result = []
        complete = True
        for item in query.expressions:
            expression = item.this if isinstance(item, exp.Alias) else item
            alias = item.alias if isinstance(item, exp.Alias) else None
            wildcard = isinstance(expression, exp.Star) or isinstance(expression, exp.Column) and isinstance(expression.this, exp.Star)
            location = [('wildcard' if wildcard else 'projection_expression', expression_span(statement, expression))] if self.capture_locations else None
            if wildcard:
                qualifier = expression.table if isinstance(expression, exp.Column) else None
                chosen = [s for s in visible if not qualifier or normalize_name(s[0]) == normalize_name(qualifier)]
                if not chosen or any(s[2] not in ('declared', 'derived') for s in chosen):
                    complete = False
                    diag = self._diagnostic(statement, 'wildcard_schema_required', f'Cannot expand {expression.sql(dialect="tsql")} without complete source schemas.')
                    entry = {'name': None, 'alias': None, 'code': expression.sql(dialect='tsql'), 'status': 'unresolved', 'diagnostics': [diag], 'wildcard': True}
                    if location is not None:
                        entry['_evidence_spans'] = location
                    result.append(entry)
                    continue
                for source_alias, columns, _ in chosen:
                    for col in columns:
                        reference = f'{source_alias}.{col["name"]}' if source_alias else col['name']
                        entry = {'name': col['name'], 'alias': None, 'code': reference,
                                       'status': 'resolved', 'source_columns': [reference],
                                       'source_mappings': col.get('mappings', []), 'wildcard': True}
                        if location is not None:
                            entry['_evidence_spans'] = location
                        result.append(entry)
                continue
            code = expression.sql(dialect='tsql')
            name = alias or (expression.name if isinstance(expression, exp.Column) else None)
            refs = [c.sql(dialect='tsql') for c in expression.find_all(exp.Column) if not isinstance(c.this, exp.Star)]
            ambiguous = False
            upstream_mappings = []
            for col in expression.find_all(exp.Column):
                if isinstance(col.this, exp.Star):
                    continue
                candidates = [s for s in visible if (not col.table or normalize_name(s[0]) == normalize_name(col.table)) and
                              (s[2] not in ('declared', 'derived') or any(normalize_name(c['name']) == normalize_name(col.name) for c in s[1] if c.get('name')))]
                if visible and (len(candidates) != 1 or candidates[0][2] not in ('declared', 'derived')):
                    ambiguous = True
                elif len(candidates) == 1:
                    matching = [c for c in candidates[0][1] if c.get('name') and normalize_name(c['name']) == normalize_name(col.name)]
                    if len(matching) == 1:
                        upstream_mappings.extend(matching[0].get('mappings', []))
                    elif len(matching) > 1:
                        ambiguous = True
            entry = {'name': name, 'alias': alias or None, 'code': code,
                           'status': 'partial' if ambiguous else 'resolved', 'source_columns': refs,
                           'diagnostics': [self._diagnostic(statement, 'column_binding_ambiguous', 'One or more column inputs could not be uniquely bound.')] if ambiguous else [],
                           'source_mappings': upstream_mappings,
                           'wildcard': False}
            if location is not None:
                entry['_evidence_spans'] = location
            result.append(entry)
        return result, 'derived' if complete else 'partial'

    def _schema(self, key: str, depth: int = 0) -> tuple[list[dict], str]:
        if key in self.cache:
            return self.cache[key]
        if key in self.active or depth > self.max_depth:
            writers = self.writers.get(key, [])
            if writers:
                self._diagnostic(writers[0].statement, 'cycle_or_depth', 'Schema expansion encountered a cycle or depth limit.', self.names[key])
            return [], 'partial'
        self.active.add(key)
        schemas = self.schemas.get(key, [])
        declared = [s for s in schemas if s.status == 'declared']
        if len(declared) > 1:
            result = ([], 'ambiguous')
        elif declared:
            cols = [dict(c, mappings=[]) for c in declared[0].columns]
            state = 'partial' if any(s.status != 'declared' for s in schemas) else 'declared'
            for alteration in sorted(self.alterations.get(key, []), key=lambda s: (s.path, s.start)):
                if alteration.path != declared[0].statement.path or alteration.start < declared[0].statement.start:
                    state = 'partial'
                    self._diagnostic(alteration, 'alter_table_unapplied', 'Cross-file or preceding ALTER TABLE order is not established.', self.names[key])
                    continue
                text = self.source_text.get(alteration.path, '')
                between = mask_noncode(text[declared[0].statement.start:alteration.start])
                if not text or alteration.procedure or re.search(r'\b(?:IF|WHILE|BEGIN|ELSE|GOTO|EXEC(?:UTE)?)\b', between, re.I):
                    state = 'partial'
                    self._diagnostic(alteration, 'alter_table_unapplied', 'Conditional or procedural ALTER TABLE order is not established.', self.names[key])
                    continue
                for action in alteration.tree.args.get('actions') or []:
                    if isinstance(action, exp.ColumnDef) and not any(normalize_name(c['name']) == normalize_name(action.name) for c in cols):
                        computed = next((c.kind for c in action.constraints if isinstance(c.kind, exp.ComputedColumnConstraint)), None)
                        added = {'name': action.name, 'ordinal': len(cols) + 1, 'mappings': [],
                                     'computed': computed.this.sql(dialect='tsql') if computed else None,
                                     'identity': any(isinstance(c.kind, exp.GeneratedAsIdentityColumnConstraint) for c in action.constraints)}
                        if self.capture_locations:
                            added['_definition_statement'] = alteration
                            added['_definition_span'] = identifier_span(alteration, action.this)
                            if computed:
                                added['_computed_span'] = expression_span(alteration, computed.this)
                        cols.append(added)
                    elif isinstance(action, exp.Drop) and str(action.args.get('kind', '')).upper() == 'COLUMN':
                        names = {normalize_name(c.name) for c in action.args.get('tables') or []}
                        if not names or not names <= {normalize_name(c['name']) for c in cols}:
                            state = 'partial'
                        cols = [c for c in cols if normalize_name(c['name']) not in names]
                    elif isinstance(action, exp.AlterColumn) and any(normalize_name(c['name']) == normalize_name(action.name) for c in cols):
                        pass  # The requested metadata does not include types.
                    else:
                        state = 'partial'
                        self._diagnostic(alteration, 'alter_table_unapplied', 'Unsupported or ambiguous ALTER TABLE action.', self.names[key])
                for ordinal, col in enumerate(cols, 1):
                    col['ordinal'] = ordinal
            result = (cols, state)
        else:
            for alteration in self.alterations.get(key, []):
                self._diagnostic(alteration, 'alter_table_unapplied', 'Base table schema is missing or ambiguous.', self.names[key])
            candidates = []
            for writer in self.writers.get(key, []):
                tree = writer.tree
                query = tree.expression if isinstance(tree, exp.Create) else tree if isinstance(tree, (exp.Select, exp.Union, exp.Except, exp.Intersect)) else None
                if query is None:
                    continue
                cols, status = self._query_schema(query, writer.statement.scope, {}, writer.statement, depth + 1)
                explicit = tree.this.expressions if isinstance(tree, exp.Create) and isinstance(tree.this, exp.Schema) else None
                if explicit and len(explicit) == len(cols):
                    for col, identifier in zip(cols, explicit):
                        col['name'] = identifier.name
                if any(not c.get('name') for c in cols):
                    status = 'partial'
                names = [normalize_name(c['name']) for c in cols if c.get('name')]
                if len(names) != len(set(names)):
                    status = 'partial'
                    self._diagnostic(writer.statement, 'duplicate_output_name', 'The projection contains duplicate output names.', writer.name)
                candidates.append((cols, status))
            if len(candidates) == 1:
                cols, status = candidates[0]
                result = ([{'name': c['name'], 'ordinal': i + 1, 'mappings': []} for i, c in enumerate(cols) if c.get('name')], status)
            elif len(candidates) > 1:
                result = ([], 'ambiguous')
            else:
                names = []
                for writer in self.writers.get(key, []):
                    if isinstance(writer.tree, exp.Insert) and isinstance(writer.tree.this, exp.Schema):
                        for target in writer.tree.this.expressions:
                            if normalize_name(target.name) not in {normalize_name(n) for n in names}:
                                names.append(target.name)
                result = ([{'name': name, 'ordinal': None, 'mappings': []} for name in names],
                          'partial' if names or schemas else 'missing')
        self.active.remove(key)
        self.cache[key] = result
        return result

    def _mapping(self, writer: Writer, item: dict, name: str | None, method: str, branch: int, ordinal: int) -> dict:
        evidence = self._evidence(writer.statement)
        operation_id = hashlib.sha256(json.dumps([evidence['repository'], evidence['path'], evidence['offset'], writer.name, branch], sort_keys=True).encode()).hexdigest()[:16]
        status = item.get('status', 'resolved') if name else 'unresolved'
        diagnostics = list(item.get('diagnostics', []))
        if method in ('assignment', 'merge_update', 'merge_insert') and item.get('source_columns'):
            status = 'partial'
            diagnostics.append(self._diagnostic(writer.statement, 'assignment_binding_unverified', 'Assignment source columns require binding validation.', writer.name))
        mapping = {'name': name, 'alias': item.get('alias'), 'code': item.get('code'),
                'operation_id': operation_id, 'branch': branch, 'ordinal': ordinal,
                'mapping_method': method, 'status': status, 'source_columns': item.get('source_columns', []),
                'source_mappings': item.get('source_mappings', []),
                'diagnostics': diagnostics, 'evidence': [evidence]}
        if self.capture_locations:
            mapping['_evidence_spans'] = item.get('_evidence_spans', [('statement', None)])
        self._variable_lineage(mapping, writer.statement)
        return mapping

    def _variable_lineage(self, mapping: dict, statement: Statement) -> None:
        if not self.enable_variable_lineage:
            return
        code = mapping.get('code')
        if not code or '@' not in code:
            return
        if self.variable_catalog is None:
            self.variable_catalog = VariableCatalog(self.sources, self._evidence, capture_locations=self.capture_locations)
        lineage = self.variable_catalog.expression(code, statement)
        if lineage:
            mapping['variable_lineage'] = lineage

    def _writes(self, key: str, columns: list[dict], status: str) -> tuple[list[dict], list[dict]]:
        cols = [dict(c, mappings=list(c.get('mappings', []))) for c in columns]
        unresolved = []
        for statement, diagnostic in self.failed_writers.get(key, []):
            unresolved.append({'name': None, 'alias': None, 'code': None,
                               'mapping_method': 'unparsed_writer', 'status': 'unresolved',
                               'diagnostics': [diagnostic], 'evidence': [self._evidence(statement)]})
        by_name = {normalize_name(c['name']): c for c in cols}
        for writer in self.writers.get(key, []):
            tree, statement = writer.tree, writer.statement
            if isinstance(tree, exp.Merge):
                whens = tree.args.get('whens')
                for branch, when in enumerate(whens.expressions if whens else [], 1):
                    action = when.args.get('then')
                    if isinstance(action, exp.Update):
                        for i, assignment in enumerate(action.expressions, 1):
                            if not isinstance(assignment, exp.EQ):
                                continue
                            name = assignment.this.name
                            item = {'code': assignment.expression.sql(dialect='tsql'), 'alias': None,
                                    'source_columns': [c.sql(dialect='tsql') for c in assignment.expression.find_all(exp.Column)]}
                            if self.capture_locations:
                                item['_evidence_spans'] = [('merge_assignment', identifier_span(statement, assignment.this)), ('merge_assignment', expression_span(statement, assignment.expression))]
                            mapping = self._mapping(writer, item, name, 'merge_update', branch, i)
                            if normalize_name(name) in by_name:
                                by_name[normalize_name(name)]['mappings'].append(mapping)
                            else:
                                unresolved.append(mapping)
                    elif isinstance(action, exp.Insert):
                        target = action.this.expressions if isinstance(action.this, exp.Tuple) else []
                        values = action.expression.expressions if isinstance(action.expression, exp.Tuple) else []
                        if len(target) != len(values) or not target:
                            self._diagnostic(statement, 'merge_target_unresolved', 'MERGE insert target and value lists cannot be mapped.', writer.name)
                            continue
                        for i, (column, value) in enumerate(zip(target, values), 1):
                            item = {'code': value.sql(dialect='tsql'), 'alias': None,
                                    'source_columns': [c.sql(dialect='tsql') for c in value.find_all(exp.Column)]}
                            if self.capture_locations:
                                item['_evidence_spans'] = [('insert_target_column', identifier_span(statement, column)), ('merge_assignment', expression_span(statement, value))]
                            mapping = self._mapping(writer, item, column.name, 'merge_insert', branch, i)
                            if normalize_name(column.name) in by_name:
                                by_name[normalize_name(column.name)]['mappings'].append(mapping)
                            else:
                                unresolved.append(mapping)
                continue
            if isinstance(tree, exp.Update):
                for i, assignment in enumerate(tree.expressions, 1):
                    if not isinstance(assignment, exp.EQ):
                        continue
                    name = assignment.this.name
                    item = {'code': assignment.expression.sql(dialect='tsql'), 'alias': None,
                            'source_columns': [c.sql(dialect='tsql') for c in assignment.expression.find_all(exp.Column)]}
                    if self.capture_locations:
                        item['_evidence_spans'] = [('update_assignment', identifier_span(statement, assignment.this)), ('update_assignment', expression_span(statement, assignment.expression))]
                    mapping = self._mapping(writer, item, name, 'assignment', 0, i)
                    if normalize_name(name) in by_name:
                        by_name[normalize_name(name)]['mappings'].append(mapping)
                    else:
                        unresolved.append(mapping)
                continue
            query = tree.expression if isinstance(tree, (exp.Insert, exp.Create)) else tree if isinstance(tree, (exp.Select, exp.Union, exp.Except, exp.Intersect)) else None
            if query is None:
                self._diagnostic(statement, 'unsupported_writer', 'Writer column mappings are not supported.', writer.name)
                continue
            target_list = [x.name for x in tree.this.expressions] if isinstance(tree.this, exp.Schema) and isinstance(tree, (exp.Insert, exp.Create)) else None
            branches = ([exp.Select(expressions=row.expressions) for row in query.expressions if isinstance(row, exp.Tuple)]
                        if isinstance(query, exp.Values) else self._query_branches(query))
            inherited = {normalize_name(c.alias_or_name): c.this for c in (query.args.get('with_').expressions if query.args.get('with_') else [])}
            if tree.args.get('with_'):
                inherited.update({normalize_name(c.alias_or_name): c.this for c in tree.args['with_'].expressions})
            if not branches:
                self._diagnostic(statement, 'unsupported_writer', 'Writer has no supported SELECT projection.', writer.name)
                continue
            for branch, select in enumerate(branches, 1):
                items, projected_status = self._projection(select, statement.scope, inherited, statement, 0)
                if target_list is not None:
                    names, method = target_list, 'insert_target_list' if isinstance(tree, exp.Insert) else 'view_column_list'
                elif isinstance(tree, exp.Insert):
                    # Identity/default insertability requires runtime state; avoid unsafe ordinal guesses.
                    if status == 'declared' and not any(c.get('identity') or c.get('computed') for c in cols):
                        names, method = [c['name'] for c in cols], 'target_schema_ordinal'
                    else:
                        names, method = [], 'target_schema_unresolved'
                else:
                    names = [c['name'] for c in cols] if len(branches) > 1 else [c.get('name') for c in items]
                    method = 'projection'
                valid = projected_status == 'derived' and len(names) == len(items)
                if not valid:
                    self._diagnostic(statement, 'projection_count_or_schema', 'Projection and destination order cannot be completely mapped.', writer.name)
                for i, item in enumerate(items, 1):
                    name = names[i - 1] if valid else None
                    if self.capture_locations and isinstance(tree, exp.Insert) and isinstance(tree.this, exp.Schema) and i <= len(tree.this.expressions):
                        item = dict(item)
                        item['_evidence_spans'] = list(item.get('_evidence_spans', [])) + [('insert_target_column', identifier_span(statement, tree.this.expressions[i - 1]))]
                    mapping = self._mapping(writer, item, name, method, branch, i)
                    if name and not isinstance(tree, exp.Insert) and i <= len(cols):
                        cols[i - 1]['mappings'].append(mapping)
                    elif name and normalize_name(name) in by_name:
                        by_name[normalize_name(name)]['mappings'].append(mapping)
                    else:
                        unresolved.append(mapping)
        for c in cols:
            definition_span = c.pop('_definition_span', None)
            computed_span = c.pop('_computed_span', None)
            definition_statement = c.pop('_definition_statement', None)
            if self.capture_locations and self.schemas.get(key):
                definition_statement = definition_statement or self.schemas[key][0].statement
                c['definition_evidence'] = [self._evidence(definition_statement)]
                c['_definition_evidence_spans'] = [('column_definition', definition_span)]
            if c.get('computed'):
                c['mappings'].append({'name': c['name'], 'alias': None, 'code': c['computed'], 'mapping_method': 'computed_ddl', 'status': 'resolved', 'evidence': [self._evidence(definition_statement or self.schemas[key][0].statement)]})
                if self.capture_locations:
                    c['mappings'][-1]['_evidence_spans'] = [('projection_expression', computed_span)]
            elif not c['mappings']:
                c['mappings'].append({'name': c['name'], 'alias': None, 'code': None, 'mapping_method': 'declared', 'status': 'declared_without_writer', 'evidence': [self._evidence(definition_statement or self.schemas[key][0].statement)] if self.schemas.get(key) else []})
            c.pop('computed', None)
            c.pop('identity', None)
        return cols, unresolved

    def render(self) -> dict:
        collections = {'tables': [], 'temp_tables': [], 'views': []}
        keys = set()
        for node in self.graph.nodes.values():
            if node.id.startswith('procedure:'):
                continue
            key, status, candidates = self._resolve(node.name, self._scope_from_node(node.id))
            if key:
                keys.add(key)
            else:
                key = node.id
                self.names.setdefault(key, node.name)
                self.kinds.setdefault(key, 'temp_table' if node.node_type == 'temporary_table' else 'table')
                self.scopes.setdefault(key, self._scope_from_node(node.id))
                self.cache[key] = ([], status)
                keys.add(key)
        for key in sorted(keys):
            columns, status = self._schema(key)
            columns, unresolved = self._writes(key, columns, status)
            kind = self.kinds.get(key, 'table')
            portable_id = self.names[key] if kind != 'temp_table' else f'{self._portable_scope(self.scopes.get(key))}::{normalize_name(self.names[key])}'
            entry = {'relation_id': normalize_name(portable_id) if kind != 'temp_table' else portable_id,
                     'display_name': self.names[key], 'kind': kind, 'scope_id': self._portable_scope(self.scopes.get(key)),
                     'schema_status': status, 'schema_evidence': [self._evidence(s.statement) for s in self.schemas.get(key, [])],
                     'columns': columns, 'unresolved_mappings': unresolved}
            entry['schema_evidence'].extend(self._evidence(s) for s in self.alterations.get(key, []))
            collections[{'table': 'tables', 'temp_table': 'temp_tables', 'view': 'views'}[kind]].append(entry)
        for values in collections.values():
            values.sort(key=lambda item: item['relation_id'])
        return {'schema_version': '1.0', 'target': self.graph.target, 'definitions': collections,
                'diagnostics': sorted(self.diagnostics.values(), key=lambda d: (d['evidence']['repository'], d['evidence']['path'], d['evidence']['offset'], d['category'], d['id'])),
                'statistics': {'relations': len(keys), 'diagnostics': len(self.diagnostics)}}

    @staticmethod
    def _scope_from_node(identifier: str) -> str | None:
        if identifier.startswith('temp:') and '::' in identifier:
            return identifier[5:].rsplit('::', 1)[0]
        return None
