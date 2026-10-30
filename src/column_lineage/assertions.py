"""Human-asserted mappings for lineage that static analysis cannot prove.

Some steps can never be proved from SQL text: dynamic SQL whose column list comes from
metadata, objects defined and loaded outside the scanned files. An assertions file lets
someone who knows the system state those steps. The tracer then continues through them,
marking every asserted step `asserted` and moving the gaps it explains to `resolved_gaps`;
nothing is deleted, and a complete result says that it relies on assertions.

File format (JSON):

    {
      "schema_version": "column-lineage-assertions/1.0",
      "assertions": [
        {
          "id": "A1",
          "target": "db.schema.table.column",          # or "#temp.column" with "procedure"
          "procedure": "schema.proc",                   # only for #temp targets
          "writer": {"path": "folder/file.sql", "line": 120},   # optional: the statement explained
          "resolves": ["dynamic_sql"],                  # optional: gap kinds it explains
          "reason": "why this is believed",
          "asserted_by": "a role, not a person's name",
          "alternatives": [
            {"branch": "metadata selects source X",
             "inputs": ["db.schema.source_x.col_a"],
             "row_selection": ["db.schema.source_x.flag"],
             "expression": "x.col_a",
             "operation": "direct"}
          ]
        }
      ]
    }

Inputs are column specs like the CLI's --column, a "#temp.column" with a "procedure"
(as an object {"column": ..., "procedure": ...}), or "constant:<sql literal>".
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

SCHEMA_VERSION = "column-lineage-assertions/1.0"
DEFAULT_RESOLVES = ("dynamic_sql", "insert_exec", "parse_error", "unsupported_statement", "external_object",
                    "missing_definition", "opaque_function", "table_valued_function")


@dataclass
class ColumnSpec:
    column: str
    procedure: str | None = None


@dataclass
class Alternative:
    branch: str | None
    inputs: list[ColumnSpec]
    row_selection: list[ColumnSpec] = field(default_factory=list)
    expression: str | None = None
    operation: str | None = None


@dataclass
class Assertion:
    id: str
    target: ColumnSpec
    alternatives: list[Alternative]
    writer_path: str | None = None
    writer_line: int | None = None
    resolves: tuple[str, ...] = DEFAULT_RESOLVES
    reason: str | None = None
    asserted_by: str | None = None
    source: str = ""
    line: int = 1

    def explains(self, evidence) -> bool:
        """Whether a gap's evidence points at the statement this assertion explains."""
        if self.writer_path is None:
            return True
        for ev in evidence:
            path = (ev.path or "").replace("\\", "/").lower()
            want = self.writer_path.replace("\\", "/").lower().lstrip("./")
            if path == want or path.endswith("/" + want) or want.endswith("/" + path):
                if self.writer_line is None or ev.start_line <= self.writer_line <= ev.end_line:
                    return True
        return False


def _spec(value, where: str) -> ColumnSpec:
    if isinstance(value, str) and value.strip():
        return ColumnSpec(value.strip())
    if isinstance(value, dict) and isinstance(value.get("column"), str):
        return ColumnSpec(value["column"].strip(), value.get("procedure"))
    raise ValueError(f"{where}: expected a column spec string or {{'column': ..., 'procedure': ...}}")


def load(path: str | Path) -> list[Assertion]:
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path.name}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("assertions"), list):
        raise ValueError(f"{path.name}: expected an object with an 'assertions' list")
    version = data.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise ValueError(f"{path.name}: unsupported schema_version {version!r} (expected {SCHEMA_VERSION})")
    out: list[Assertion] = []
    seen: set[str] = set()
    for index, item in enumerate(data["assertions"], 1):
        where = f"{path.name}: assertion {index}"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: expected an object")
        ident = str(item.get("id") or f"A{index}")
        if ident in seen:
            raise ValueError(f"{where}: duplicate id {ident!r}")
        seen.add(ident)
        target = _spec(item.get("target"), f"{where} target")
        if item.get("procedure") and target.procedure is None:
            target.procedure = item["procedure"]
        alternatives = []
        for j, alt in enumerate(item.get("alternatives") or [], 1):
            if not isinstance(alt, dict) or not alt.get("inputs"):
                raise ValueError(f"{where} alternative {j}: needs a non-empty 'inputs' list")
            alternatives.append(Alternative(
                alt.get("branch"), [_spec(v, f"{where} alternative {j} input") for v in alt["inputs"]],
                [_spec(v, f"{where} alternative {j} row_selection") for v in alt.get("row_selection") or []],
                alt.get("expression"), alt.get("operation")))
        if not alternatives:
            raise ValueError(f"{where}: needs at least one alternative")
        writer = item.get("writer") or {}
        resolves = item.get("resolves")
        line = _line_of(text, ident)
        out.append(Assertion(ident, target, alternatives, writer.get("path"),
                             int(writer["line"]) if writer.get("line") is not None else None,
                             tuple(resolves) if resolves else DEFAULT_RESOLVES,
                             item.get("reason"), item.get("asserted_by"), path.as_posix(), line))
    return out


def _line_of(text: str, ident: str) -> int:
    marker = json.dumps(ident)
    for number, line in enumerate(text.splitlines(), 1):
        if '"id"' in line and marker in line:
            return number
    return 1
