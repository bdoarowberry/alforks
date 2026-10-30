from __future__ import annotations

import json
from .models import LineageEdge, LineageGraph


def _conditions(edge: LineageEdge) -> str:
    conditions = sorted({item.condition for item in edge.evidence if item.condition})
    if not conditions:
        return ''
    text = ' OR '.join(conditions)
    if any(not item.condition and item.relationship != 'identifier_resolution' for item in edge.evidence):
        text += ' (also unconditional evidence)'
    return text


def to_json(graph: LineageGraph) -> str:
    return json.dumps(graph.to_dict(), indent=2) + "\n"


def to_mermaid(graph: LineageGraph) -> str:
    lines = ["flowchart TD"]
    ids = {key: f'n{index}' for index, key in enumerate(sorted(graph.nodes))}
    for key, node_id in ids.items():
        label = graph.nodes[key].name.replace('&', '#38;').replace('"', '#34;').replace('<', '#60;').replace('>', '#62;').replace('\n', ' ')
        if graph.nodes[key].node_type == 'temporary_table':
            lines.append(f'  {node_id}(["{label}"])')
        else:
            lines.append(f'  {node_id}["{label}"]')
    for edge in sorted(graph.edges, key=lambda item: (item.downstream, item.upstream, item.relationship, item.writer or '')):
        condition = _conditions(edge)
        if condition:
            label = condition.replace('&', '#38;').replace('"', '#34;').replace('|', '#124;').replace('<', '#60;').replace('>', '#62;').replace('\n', ' ')
            lines.append(f'  {ids[edge.downstream]} -->|"{label}"| {ids[edge.upstream]}')
        else:
            lines.append(f'  {ids[edge.downstream]} --> {ids[edge.upstream]}')
    return "\n".join(lines) + "\n"


def to_text(graph: LineageGraph) -> str:
    lines = [f"Target: {graph.target}", f"Nodes: {len(graph.nodes)}", f"Edges: {len(graph.edges)}",
             f"Files scanned: {len(graph.files_scanned)}",
             f"Dated snapshots ignored: {graph.dated_snapshot_files_ignored}",
             f"Dated snapshots included by override: {'yes' if graph.include_dated_snapshots else 'no'}",
             f"Unresolved references: {len(set(graph.unresolved))}",
             f"Cycles: {len(graph.cycles)}", f"Warnings: {len(set(graph.warnings))}"]
    for edge in sorted(graph.edges, key=lambda item: (item.downstream, item.upstream)):
        condition = _conditions(edge)
        lines.append(f"  {graph.nodes[edge.downstream].name} <- {graph.nodes[edge.upstream].name}" + (f" [when {condition}]" if condition else ''))
    if graph.unresolved:
        lines.append("Unresolved: " + ", ".join(sorted(set(graph.unresolved))))
    for warning in sorted(set(graph.warnings)):
        lines.append(f"Warning: {warning}")
    return "\n".join(lines) + "\n"
