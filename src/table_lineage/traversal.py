from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from pathlib import Path
import re

from .index import Symbol
from .models import Evidence, LineageEdge, LineageGraph, normalize_name
from .parser import ParsedDependency, ProcedureCall


def object_key(name: str, scope: str | None = None) -> str:
    key = normalize_name(name)
    if key.startswith('#') and not key.startswith('##') and scope:
        return f'temp:{scope}::{key}'
    return key


def _database_folder(path: str) -> tuple[str, str] | None:
    """Identify the repository-local SQL database folder, without asserting deployment."""
    parts = Path(path).parts
    for index, part in enumerate(parts[:-2]):
        if part.lower() in ('sql_root_a', 'sql_root_b'):
            return str(Path(*parts[:index + 1])).lower(), parts[index + 1].lower()
    return None


class LineageAnalyzer:
    def __init__(self, dependencies: list[ParsedDependency], calls: list[ProcedureCall] | None = None,
                 procedures: set[str] | None = None, definitions: list[Symbol] | None = None,
                 procedure_definitions: list[Symbol] | None = None) -> None:
        self.dependencies = sorted(dependencies, key=lambda d: (d.evidence.path, d.evidence.line, d.downstream, d.upstream, d.writer or ''))
        self.calls = sorted(calls or [], key=lambda c: (c.evidence.path, c.offset, c.caller or '', c.callee))
        self.by_downstream: dict[str, list[ParsedDependency]] = defaultdict(list)
        self.names: dict[str, str] = {}
        self.definitions = sorted((s for s in (definitions or []) if not normalize_name(s.name).startswith('#')),
                                  key=lambda s: (normalize_name(s.name), s.path, s.line))
        self.inferred: dict[ParsedDependency, Symbol] = {}
        self.inferred_sources: dict[ParsedDependency, Symbol] = {}
        self.layout_candidates: dict[ParsedDependency, Symbol] = {}
        self.uncertain_writers: dict[str, tuple[str, ...]] = {}
        qualified = {normalize_name(name): name for dep in self.dependencies for name in (dep.downstream, dep.upstream)
                     if re.fullmatch(r'[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+', normalize_name(name))}
        for dep in self.dependencies:
            downstream, symbol, candidates = self._writer_identity(dep.downstream)
            folder = _database_folder(dep.evidence.path)
            name = normalize_name(downstream)
            if folder and re.fullmatch(r'[a-z0-9_]+\.[a-z0-9_]+', name):
                full_name = f'{folder[1]}.{name}'
                definitions = [s for s in self.definitions if normalize_name(s.name) == name and _database_folder(s.path) == folder]
                locations = {s.path for s in definitions}
                if full_name in qualified and len(locations) == 1:
                    self.layout_candidates[dep] = definitions[0]
                    self.names.setdefault(object_key(downstream, dep.scope), downstream)
                    self.by_downstream[object_key(downstream, dep.scope)].append(dep)
                    downstream = qualified[full_name]
            if symbol:
                self.inferred[dep] = symbol
            elif candidates:
                self.uncertain_writers[normalize_name(dep.downstream)] = candidates
            upstream, upstream_symbol, _ = self._writer_identity(dep.upstream)
            if upstream_symbol:
                self.inferred_sources[dep] = upstream_symbol
            for name in (downstream, upstream):
                self.names.setdefault(object_key(name, dep.scope), name)
            self.by_downstream[object_key(downstream, dep.scope)].append(dep)
        self.procedures = {normalize_name(dep.writer): dep.writer for dep in self.dependencies if dep.writer}
        self.procedures.update({normalize_name(name): name for name in sorted(procedures or set())})
        for call in self.calls:
            if call.caller:
                self.procedures.setdefault(normalize_name(call.caller), call.caller)
        self.procedure_definitions = procedure_definitions

    def _writer_identity(self, name: str) -> tuple[str, Symbol | None, tuple[str, ...]]:
        key = normalize_name(name)
        if '.' in key or key.startswith('#'):
            return name, None, ()
        matches = [s for s in self.definitions if normalize_name(s.name) == key or normalize_name(s.name).endswith('.' + key)]
        identities = {(normalize_name(s.name), s.path) for s in matches}
        if len(identities) == 1 and normalize_name(matches[0].name) != key:
            return matches[0].name, matches[0], ()
        if len(identities) <= 1:
            return name, None, ()
        return name, None, tuple(sorted(f'{s.name} ({s.path}:{s.line})' for s in matches))

    def _resolve_object(self, name: str, scope: str | None) -> tuple[str, str]:
        key = object_key(name, scope)
        normalized = normalize_name(name)
        if not normalized.startswith('#'):
            candidate, symbol, ambiguous = self._writer_identity(name)
            if ambiguous:
                return key, 'ambiguous'
            if symbol:
                canonical = object_key(candidate, scope)
                if canonical in self.names:
                    return canonical, 'resolved'
        if key in self.names:
            return key, 'resolved'
        if normalized.startswith('#'):
            candidates = [k for k, n in self.names.items() if normalize_name(n) == normalized] if scope is None else []
        else:
            candidates = [k for k, n in self.names.items()
                          if normalize_name(n).endswith('.' + normalized)]
        if len(candidates) == 1:
            return candidates[0], 'resolved'
        return key, 'ambiguous' if candidates else 'unresolved'

    def analyze(self, target: str, max_depth: int | None = None) -> LineageGraph:
        if max_depth is not None and max_depth < 0:
            raise ValueError('max-depth must be nonnegative')
        graph = LineageGraph(target)
        active: list[str] = []
        visited: dict[str, int] = {}
        seen_call_edges: set[tuple[str, str, int]] = set()

        def add_object(name: str, key: str, status: str) -> None:
            actual = self.names.get(key, name)
            node_type = ('temporary_table' if normalize_name(actual).startswith('#') else 'table') if status == 'resolved' else 'unresolved'
            graph.add_node(actual, node_type, key)

        def explain_unqualified_writers(name: str, key: str) -> None:
            suffix = normalize_name(name).split('.')[-1]
            for writer, candidates in self.uncertain_writers.items():
                if writer == suffix:
                    graph.unresolved.append(f'{name}: unqualified writer {writer} has ambiguous definitions: {", ".join(candidates)}')
            if suffix in self.by_downstream and suffix != key and suffix not in self.uncertain_writers:
                graph.unresolved.append(f'{name}: unqualified writer {suffix} has no unique matching definition')

        def visit_calls(proc: str, proc_key: str, call_active: tuple[str, ...], depth: int) -> None:
            if max_depth is not None and depth >= max_depth:
                if any(call.caller and normalize_name(call.caller) == normalize_name(proc) for call in self.calls):
                    graph.warnings.append(f'Maximum depth reached at procedure {proc}')
                return
            for call in self.calls:
                if not call.caller or normalize_name(call.caller) != normalize_name(proc):
                    continue
                called = normalize_name(call.callee)
                available = (self.procedures if self.procedure_definitions is None else
                             {normalize_name(s.name): s.name for s in self.procedure_definitions})
                matches = [key for key in available if key == called or key.endswith('.' + called)]
                exact = [key for key in matches if key == called]
                matches = exact or matches
                matching_definitions = [s for s in (self.procedure_definitions or []) if normalize_name(s.name) in matches]
                locations = {(normalize_name(s.name), s.path) for s in matching_definitions}
                resolved = len(matches) == 1 and (self.procedure_definitions is None or len(locations) == 1)
                identifier = matches[0] if resolved else called
                called_key = 'procedure:' + identifier
                graph.add_node(available.get(identifier, call.callee), 'stored_procedure' if resolved else 'unresolved', called_key)
                evidence = call.evidence if resolved else replace(call.evidence, status='unresolved')
                call_key = (proc_key, call.evidence.path, call.offset)
                if call_key not in seen_call_edges:
                    graph.edges.append(LineageEdge(proc_key, called_key, proc, 'calls', [evidence]))
                    seen_call_edges.add(call_key)
                if not resolved:
                    if matches:
                        candidates = ', '.join(sorted(f'{s.name} ({s.path}:{s.line})' for s in matching_definitions)) or ', '.join(sorted(matches))
                        graph.unresolved.append(f'{call.callee}: ambiguous procedure definitions: {candidates}')
                    else:
                        graph.unresolved.append(f'{call.callee}: procedure definition not found in scanned sources')
                elif called_key in call_active:
                    cycle = list(call_active[call_active.index(called_key):]) + [called_key]
                    if cycle not in graph.cycles:
                        graph.cycles.append(cycle)
                else:
                    visit_calls(available[identifier], called_key, call_active + (called_key,), depth + 1)

        def visit(name: str, scope: str | None, depth: int) -> str:
            key, status = self._resolve_object(name, scope)
            add_object(name, key, status)
            if status != 'resolved':
                graph.unresolved.append(f'{name}: {status} reference')
                _, _, candidates = self._writer_identity(name)
                if candidates:
                    graph.unresolved.append(f'{name}: ambiguous definitions: {", ".join(candidates)}')
                explain_unqualified_writers(name, key)
                return key
            if key in active:
                cycle = active[active.index(key):] + [key]
                if cycle not in graph.cycles:
                    graph.cycles.append(cycle)
                return key
            if max_depth is not None and depth >= max_depth:
                if self.by_downstream.get(key):
                    graph.warnings.append(f'Maximum depth reached at {name}')
                return key
            if key in visited and visited[key] <= depth:
                return key
            visited[key] = depth
            active.append(key)
            dependencies = self.by_downstream.get(key, [])
            if not dependencies:
                graph.unresolved.append(f'{name}: no writer found in scanned sources')
                explain_unqualified_writers(name, key)
            for dep in dependencies:
                upstream_key, upstream_status = self._resolve_object(dep.upstream, dep.scope)
                add_object(dep.upstream, upstream_key, upstream_status)
                symbol = self.inferred.get(dep)
                source_symbol = self.inferred_sources.get(dep)
                layout_symbol = self.layout_candidates.get(dep) if key != object_key(dep.downstream, dep.scope) else None
                evidence = dep.evidence if dep.evidence.status == 'dynamic_sql' or (upstream_status == 'resolved' and not symbol and not source_symbol and not layout_symbol) else replace(dep.evidence, status='unresolved')
                graph.add_edge_keys(key, upstream_key, dep.writer, 'reads_from', evidence)
                if layout_symbol:
                    resolution = Evidence(layout_symbol.path, layout_symbol.line, layout_symbol.excerpt, 'identifier_resolution', 'unresolved', category='ambiguous', reason='Repository database-folder context is a candidate; runtime deployment is not established.')
                    graph.add_edge_keys(key, upstream_key, dep.writer, 'reads_from', resolution)
                    graph.diagnostics.append(resolution)
                    graph.warnings.append(f'{dep.evidence.path}:{dep.evidence.line}: {dep.downstream} is a candidate writer for {key} based on database-folder context and {layout_symbol.path}:{layout_symbol.line}; runtime database binding is not established')
                    graph.unresolved.append(f'{key}: database-folder candidate binding is not verified at runtime')
                if symbol:
                    resolution = Evidence(symbol.path, symbol.line, symbol.excerpt, 'identifier_resolution', 'unresolved', category='ambiguous')
                    graph.add_edge_keys(key, upstream_key, dep.writer, 'reads_from', resolution)
                    graph.diagnostics.append(resolution)
                    graph.warnings.append(f'{dep.evidence.path}:{dep.evidence.line}: unqualified writer {dep.downstream} is a candidate for {symbol.name} based on {symbol.path}:{symbol.line}; runtime schema and database are not established')
                if source_symbol:
                    resolution = Evidence(source_symbol.path, source_symbol.line, source_symbol.excerpt, 'identifier_resolution', 'unresolved', category='ambiguous')
                    graph.add_edge_keys(key, upstream_key, dep.writer, 'reads_from', resolution)
                    graph.diagnostics.append(resolution)
                    graph.warnings.append(f'{dep.evidence.path}:{dep.evidence.line}: unqualified source {dep.upstream} is a candidate for {source_symbol.name} based on {source_symbol.path}:{source_symbol.line}; runtime schema and database are not established')
                if dep.writer:
                    proc_key = 'procedure:' + normalize_name(dep.writer)
                    graph.add_node(dep.writer, 'stored_procedure', proc_key)
                    graph.add_edge_keys(key, proc_key, dep.writer, 'writes_to', evidence)
                    graph.add_edge_keys(proc_key, upstream_key, dep.writer, 'reads_from', evidence)
                    visit_calls(dep.writer, proc_key, (proc_key,), depth)
                visit(dep.upstream, dep.scope, depth + 1)
            active.pop()
            return key

        visit(target, None, 0)
        return graph
