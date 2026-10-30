from __future__ import annotations

from dataclasses import dataclass, replace
import re

from sqlglot import exp

from .index import IDENT, SourceIndex, mask_noncode
from .dynamic import recover_select_into
from .models import Evidence, normalize_name
from .sources import SourceFile


@dataclass(frozen=True)
class ParsedDependency:
    downstream: str
    upstream: str
    writer: str | None
    evidence: Evidence
    scope: str | None = None


@dataclass(frozen=True)
class ProcedureCall:
    caller: str | None
    callee: str
    evidence: Evidence
    offset: int = 0


def _static_calls(source: SourceFile, index: SourceIndex) -> list[ProcedureCall]:
    """Collect every statically named EXEC in indexed statements, preserving offsets."""
    pattern = re.compile(rf'\bEXEC(?:UTE)?\s+(?:@\w+\s*=\s*)?(?P<name>{IDENT})(?![\w.#])', re.I)
    results: list[ProcedureCall] = []
    for statement in index.statements:
        masked = mask_noncode(statement.text)
        quoted_ranges = [(m.start(), m.end()) for m in re.finditer(r'\[(?:[^\]]|\]\])*\]|"(?:[^"]|"")*"', masked)]
        matches = [m for m in pattern.finditer(masked)
                   if not any(start <= m.start() < end for start, end in quoted_ranges)]
        for pos, match in enumerate(matches):
            callee = match.group('name')
            if normalize_name(callee) in ('sp_executesql', 'sys.sp_executesql', 'as'):
                continue
            end = matches[pos + 1].start() if pos + 1 < len(matches) else len(masked)
            semicolon = masked.find(';', match.end(), end)
            if semicolon >= 0:
                end = semicolon + 1
            # Preserve argument continuation lines; stop at the next unrelated line.
            for line in re.finditer(r'\n[ \t]*(?P<next>\S)', masked[match.end():end]):
                at = match.end() + line.start()
                next_char = line.group('next')
                previous = masked[match.end():at].rstrip()
                if next_char != '@' and not previous.endswith(','):
                    end = at
                    break
            offset = statement.start + match.start()
            line_number = source.text.count('\n', 0, offset) + 1
            excerpt = ' '.join(statement.text[match.start():end].split())[:500]
            results.append(ProcedureCall(statement.procedure, callee,
                Evidence(str(source.path), line_number, excerpt, 'calls', 'resolved', procedure=statement.procedure), offset))
    return results


def table_name(table: exp.Table) -> str:
    clean = table.copy()
    for node in clean.walk():
        node.comments = None
    for key in ('alias', 'joins', 'hints', 'pivots', 'sample'):
        clean.set(key, None)
    return clean.sql(dialect='tsql')


def _log_target(name: str) -> bool:
    key = normalize_name(name).rsplit('.', 1)[-1]
    return key == 'run_log' or key.endswith('_run_log')


def _assignment_variable(tree: exp.Expression) -> str | None:
    if isinstance(tree, exp.Set) and len(tree.expressions) == 1:
        item = tree.expressions[0].this
        if isinstance(item, exp.EQ) and isinstance(item.this, exp.Parameter):
            return item.this.sql(dialect='tsql')
    if isinstance(tree, exp.Declare) and len(tree.expressions) == 1:
        item = tree.expressions[0]
        if isinstance(item, exp.DeclareItem) and len(item.this) == 1 and isinstance(item.this[0], exp.Parameter):
            return item.this[0].sql(dialect='tsql')
    if isinstance(tree, exp.Select) and len(tree.expressions) == 1:
        item = tree.expressions[0]
        if isinstance(item, exp.EQ) and isinstance(item.this, exp.Parameter):
            return item.this.sql(dialect='tsql')
    return None


def _only_used_for_run_log(variable: str, source: SourceFile, index: SourceIndex) -> bool:
    """Require every visible reference to be a declaration, assignment, or log write."""
    masked = mask_noncode(source.text)
    occurrences = list(re.finditer(re.escape(variable) + r'(?![\w$#])', masked, re.I))
    if not occurrences:
        return False
    saw_log_write = False
    for occurrence in occurrences:
        statement = next((s for s in index.statements if s.start <= occurrence.start() < s.end), None)
        if statement is None:
            return False
        sql = mask_noncode(statement.text)
        if re.match(r'\s*DECLARE\s+' + re.escape(variable) + r'\b', sql, re.I) and '=' not in sql:
            continue
        if (re.search(r'(?:^|\n)\s*(?:SET|SELECT)\s+' + re.escape(variable) + r'\s*=', sql, re.I)
                and len(re.findall(re.escape(variable) + r'(?![\w$#])', sql, re.I)) == 1):
            continue
        match = re.match(rf'\s*UPDATE\s+(?P<target>{IDENT})', sql, re.I)
        if match and _log_target(match.group('target')):
            saw_log_write = True
            continue
        return False
    return saw_log_write


def _is_run_id_control(tree: exp.Expression, variable: str, index: SourceIndex, scope: str) -> bool:
    if variable.lower() != '@run_id':
        return False
    if not any(isinstance(node.this, exp.Column) and node.this.name.lower() == 'run_id'
               for node in tree.find_all(exp.Max)):
        return False
    return any(s.scope == scope and re.search(r'\bSET\s+(?:\w+\.)?\[?run_id\]?\s*=\s*@run_id\b',
                                               mask_noncode(s.text), re.I) for s in index.statements)


class SqlSourceParser:
    def __init__(self, *, definition_mode: bool = False) -> None:
        self.definition_mode = definition_mode
        self.warnings: list[str] = []
        self.calls: list[ProcedureCall] = []
        self.procedures: set[str] = set()
        self.diagnostics: list[Evidence] = []
        self.definitions = []
        self.procedure_definitions = []

    def parse(self, source: SourceFile, index: SourceIndex | None = None) -> list[ParsedDependency]:
        if index is None:
            index = SourceIndex()
            index.add(source)
        self.calls.extend(_static_calls(source, index))
        self.procedure_definitions.extend(symbol for symbol in index.symbols if symbol.kind == 'stored_procedure')
        self.definitions.extend(symbol for symbol in index.symbols if symbol.kind in ('table', 'view', 'synonym', 'function'))
        self.procedures.update(symbol.name for symbol in index.symbols if symbol.kind == 'stored_procedure')
        dependencies: list[ParsedDependency] = []
        synonym_pattern = re.compile(rf'\bCREATE\s+SYNONYM\s+(?P<target>{IDENT})\s+FOR\s+(?P<source>{IDENT})\s*;?', re.I)
        for match in synonym_pattern.finditer(mask_noncode(source.text)):
            line = source.text.count('\n', 0, match.start()) + 1
            excerpt = ' '.join(source.text[match.start():match.end()].split())
            dependencies.append(ParsedDependency(match.group('target'), match.group('source'), None,
                                                 Evidence(str(source.path), line, excerpt, 'writes_to', 'resolved')))
        for statement in index.statements:
            if statement.path != str(source.path):
                continue
            if isinstance(statement.tree, exp.Create) and str(statement.tree.args.get('kind', '')).lower() == 'table':
                continue
            if isinstance(statement.tree, exp.Alter):
                continue
            dynamic = re.match(r'\s*EXEC(?:UTE)?\s*(?:\(|(?:\[?sys\]?\.)?\[?sp_executesql\]?(?!\w)|@\w+\b(?!\s*=))', statement.text, re.I)
            if dynamic:
                excerpt = ' '.join(statement.text.split())[:500]
                diagnostic = Evidence(statement.path, statement.line, excerpt, 'dynamic_sql', 'dynamic_sql',
                                      procedure=statement.procedure, dialect='tsql', category='unsupported',
                                      reason='Dynamic SQL may contain additional lineage.')
                self.diagnostics.append(diagnostic)
                self.warnings.append(f'{statement.path}:{statement.line}: dynamic SQL; lineage may be incomplete: {excerpt}')
                literal = re.fullmatch(r"\s*EXEC(?:UTE)?\s*(?:\(\s*N?'((?:''|[^'])*)'\s*\)|(?:\[?sys\]?\.)?\[?sp_executesql\]?\s+N?'((?:''|[^'])*)')\s*;?\s*", statement.text, re.I | re.S)
                if literal:
                    payload = (literal.group(1) if literal.group(1) is not None else literal.group(2)).replace("''", "'")
                    nested = SqlSourceParser()
                    for item in nested.parse(SourceFile(source.path, payload)):
                        dependencies.append(replace(item, writer=statement.procedure, scope=statement.scope,
                            evidence=replace(diagnostic, relationship='writes_to')))
                    if nested.warnings:
                        self.warnings.append(f'{statement.path}:{statement.line}: dynamic SQL literal could not be fully parsed')
                else:
                    for candidate in recover_select_into(source, index, statement):
                        evidence = replace(diagnostic, relationship='writes_to', category='row_lineage',
                            reason='Conditional candidate recovered from a dynamic SQL template; other expressions remain unresolved.',
                            condition=candidate.condition, condition_line=candidate.condition_line,
                            condition_excerpt=candidate.condition_excerpt, assignment_line=candidate.assignment_line,
                            assignment_excerpt=candidate.assignment_excerpt, template_line=candidate.template_line,
                            template_excerpt=candidate.template_excerpt)
                        dependencies.append(ParsedDependency(candidate.target, candidate.source, statement.procedure,
                                                             evidence, statement.scope))
                continue
            call = re.match(rf'\s*EXEC(?:UTE)?\s+(?:@\w+\s*=\s*)?(?P<name>{IDENT})(?![\w.#])', mask_noncode(statement.text), re.I)
            if call:
                continue
            tree = statement.tree
            error = statement.parse_error
            if tree is None or isinstance(tree, exp.Command):
                excerpt = statement.text.strip()
                self.diagnostics.append(Evidence(statement.path, statement.line, excerpt, 'parse_error', 'unresolved', error, statement.procedure, 'tsql', category='unsupported'))
                self.warnings.append(f'{statement.path}:{statement.line}: unsupported or ambiguous SQL; lineage may be incomplete; {error}; statement: {excerpt}')
                continue
            objects = tuple(dict.fromkeys(table_name(t) for t in tree.find_all(exp.Table)))
            excerpt = ' '.join(statement.text.split())[:500]
            variable = _assignment_variable(tree)
            is_read = (isinstance(tree, (exp.Set, exp.Declare)) or
                       isinstance(tree, exp.Select) and tree.args.get('into') is None)
            if is_read and objects:
                if variable and _is_run_id_control(tree, variable, index, statement.scope):
                    category, reason = 'control_metadata', 'Run identifier is calculated from a table and assigned to a run_id column.'
                elif variable and _only_used_for_run_log(variable, source, index):
                    category, reason = 'non_lineage', 'Assigned value is used only in run-log writes.'
                elif variable:
                    category, reason = 'ambiguous', 'Variable use is not established as exclusively logging or control; it may affect row data.'
                    self.warnings.append(f'{statement.path}:{statement.line}: {reason} Variable: {variable}')
                else:
                    category, reason = 'non_lineage', 'Result-returning read; this statement does not write report rows.'
                self.diagnostics.append(Evidence(statement.path, statement.line, excerpt, category,
                    'unresolved' if category == 'ambiguous' else 'resolved', procedure=statement.procedure,
                    dialect='tsql', category=category, objects=objects, reason=reason))
                continue
            prior_warnings = len(self.warnings)
            target = self._target(tree)
            if target is None:
                if len(self.warnings) > prior_warnings:
                    self.diagnostics.append(Evidence(statement.path, statement.line, excerpt, 'ambiguous', 'unresolved',
                        error=self.warnings[-1], procedure=statement.procedure, dialect='tsql', category='ambiguous', objects=objects))
                continue
            if _log_target(target):
                self.diagnostics.append(Evidence(statement.path, statement.line, excerpt, 'non_lineage', 'resolved',
                    procedure=statement.procedure, dialect='tsql', category='non_lineage', objects=objects,
                    reason='Write to a run-log table; excluded from report row lineage.'))
                continue
            ctes = {normalize_name(cte.alias_or_name) for cte in tree.find_all(exp.CTE)}
            target_key = normalize_name(target)
            target_aliases = {normalize_name(table.alias) for table in tree.find_all(exp.Table)
                              if table.alias and normalize_name(table_name(table)) == target_key}
            evidence = Evidence(statement.path, statement.line, excerpt, 'writes_to', 'resolved',
                                procedure=statement.procedure, dialect='tsql', category='row_lineage')
            self.diagnostics.append(replace(evidence, objects=objects, reason='Statement writes a data object.'))
            for table in tree.find_all(exp.Table):
                if isinstance(tree, (exp.Update, exp.Delete)) and table is tree.this:
                    continue
                name = table_name(table)
                key = normalize_name(name)
                if not name or key == target_key or key in ctes or key in target_aliases:
                    continue
                # A function call is not a physical table; keep it explicit for later resolution.
                status = 'unresolved' if isinstance(table.this, exp.Func) else 'resolved'
                item = ParsedDependency(target, name, statement.procedure,
                                        replace(evidence, status=status), statement.scope)
                if item not in dependencies:
                    dependencies.append(item)
        return dependencies

    def _target(self, tree: exp.Expression) -> str | None:
        if isinstance(tree, exp.Insert):
            target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        elif isinstance(tree, (exp.Update, exp.Delete, exp.Merge)):
            target = tree.this
        elif isinstance(tree, exp.Select):
            into = tree.args.get('into')
            target = into.this if into else None
        elif self.definition_mode and isinstance(tree, (exp.Union, exp.Except, exp.Intersect)):
            first = tree
            while isinstance(first, (exp.Union, exp.Except, exp.Intersect)):
                first = first.this
            into = first.args.get('into') if isinstance(first, exp.Select) else None
            target = into.this if into else None
        elif isinstance(tree, exp.Create) and str(tree.args.get('kind', '')).lower() in ('view', 'table'):
            target = tree.this.this if self.definition_mode and isinstance(tree.this, exp.Schema) else tree.this
        else:
            return None
        if not isinstance(target, exp.Table):
            return None
        name = table_name(target)
        # UPDATE/DELETE may identify the target using a FROM alias.
        if isinstance(tree, (exp.Update, exp.Delete)) and not target.db and not target.catalog:
            matches = [table for table in tree.find_all(exp.Table)
                       if table is not target and table.alias and
                       normalize_name(table.alias) == normalize_name(name)]
            if len(matches) == 1:
                return table_name(matches[0])
            if len(matches) > 1:
                self.warnings.append(f'ambiguous target alias {name}; write omitted')
                return None
        return name
