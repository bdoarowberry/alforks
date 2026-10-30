from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
import re


def normalize_name(value: str) -> str:
    """Normalize a SQL Server identifier without changing its semantic parts."""
    value = value.strip().rstrip(",;")
    parts = re.findall(r'\[(?:[^\]]|\]\])*\]|"(?:[^"]|"")*"|[^.]+', value)
    normalized = []
    for part in parts:
        part = part.strip()
        if part.startswith("[") and part.endswith("]"):
            part = part[1:-1].replace("]]", "]")
        elif part.startswith('"') and part.endswith('"'):
            part = part[1:-1].replace('""', '"')
        part = part.lower()
        if not part:
            continue
        normalized.append(f'[{part.replace("]", "]]")}]' if "." in part or "]" in part else part)
    return ".".join(normalized)


def display_name(value: str) -> str:
    return value.strip().rstrip(",;").replace("[", "").replace("]", "")


@dataclass(frozen=True)
class Evidence:
    path: str
    line: int
    excerpt: str
    relationship: str = "reads_from"
    status: str = "resolved"
    error: str | None = None
    procedure: str | None = None
    dialect: str | None = None
    category: str | None = None
    objects: tuple[str, ...] = ()
    reason: str | None = None
    condition: str | None = None
    condition_line: int | None = None
    condition_excerpt: str | None = None
    assignment_line: int | None = None
    assignment_excerpt: str | None = None
    template_line: int | None = None
    template_excerpt: str | None = None


@dataclass
class LineageNode:
    name: str
    node_type: str = "unresolved"
    normalized_name: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        if not self.normalized_name:
            self.normalized_name = normalize_name(self.name)


@dataclass
class LineageEdge:
    downstream: str
    upstream: str
    writer: str | None
    relationship: str
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class LineageGraph:
    target: str
    nodes: dict[str, LineageNode] = field(default_factory=dict)
    edges: list[LineageEdge] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)
    files_scanned: list[str] = field(default_factory=list)
    dated_snapshot_files_ignored: int = 0
    include_dated_snapshots: bool = False
    warnings: list[str] = field(default_factory=list)
    diagnostics: list[Evidence] = field(default_factory=list)

    def add_node(self, name: str, node_type: str = "unresolved", key: str | None = None) -> str:
        key = key or normalize_name(name)
        if not key:
            return key
        existing = self.nodes.get(key)
        if existing is None:
            self.nodes[key] = LineageNode(display_name(name), node_type, normalize_name(name), key)
        elif existing.node_type == "unresolved" and node_type != "unresolved":
            existing.node_type = node_type
        return key

    def add_edge(self, downstream: str, upstream: str, writer: str | None,
                 relationship: str, evidence: Evidence) -> None:
        downstream_key = self.add_node(downstream)
        upstream_key = self.add_node(upstream)
        self.add_edge_keys(downstream_key, upstream_key, writer, relationship, evidence)

    def add_edge_keys(self, downstream_key: str, upstream_key: str, writer: str | None,
                      relationship: str, evidence: Evidence) -> None:
        for edge in self.edges:
            if (edge.downstream == downstream_key and edge.upstream == upstream_key and
                    edge.writer == writer and edge.relationship == relationship):
                if evidence not in edge.evidence:
                    edge.evidence.append(evidence)
                return
        self.edges.append(LineageEdge(downstream_key, upstream_key, writer, relationship, [evidence]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "nodes": [asdict(self.nodes[key]) for key in sorted(self.nodes)],
            "edges": [
                {**asdict(edge), "evidence": [asdict(item) for item in sorted(edge.evidence, key=lambda e: (e.path, e.line, e.relationship, e.status, e.excerpt))]}
                for edge in sorted(self.edges, key=lambda item: (item.downstream, item.upstream, item.writer or "", item.relationship))
            ],
            "unresolved": sorted(set(self.unresolved)),
            "cycles": sorted(self.cycles),
            "files_scanned": sorted(self.files_scanned),
            "scan_statistics": {
                "files_scanned": len(self.files_scanned),
                "dated_snapshot_files_ignored": self.dated_snapshot_files_ignored,
                "include_dated_snapshots": self.include_dated_snapshots,
                "nodes": len(self.nodes),
                "edges": len(self.edges),
            },
            "warnings": sorted(set(self.warnings)),
            "diagnostics": [asdict(item) for item in sorted(set(self.diagnostics), key=lambda e: (e.path, e.line, e.excerpt))],
        }
