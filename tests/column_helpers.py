"""Shared helpers for the column lineage contract tests.

The tests exercise the documented CLI (docs/column-lineage-design.md) and read the
versioned JSON trace. ``column_lineage`` is imported lazily so these modules collect
even before the implementation exists.

Node ids are compared case-insensitively: the contract lower-cases object keys, and
SQL Server identifiers are case-insensitive. Tests that care about a single identity
per column check separately that no two ids differ only by case.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = REPO / "fixtures" / "column"


def fixture(name: str) -> Path:
    path = FIXTURES / name
    assert path.is_dir(), f"missing fixture directory {path}"
    return path


def norm(node_id: str | None) -> str | None:
    return node_id.lower() if isinstance(node_id, str) else node_id


def stem_for(spec: str) -> str:
    """Output file stem of a column spec, exactly as the CLI computes it."""
    from column_lineage.cli import stem
    return stem(spec)


# --------------------------------------------------------------------------- CLI

def run_cli(args: list[str]) -> int:
    """Run ``column_lineage.cli.main`` and return its exit code (argparse exits included)."""
    from column_lineage.cli import main  # lazy: the implementation may not exist yet

    try:
        code = main([str(a) for a in args])
    except SystemExit as exc:  # argparse usage errors
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
    return 0 if code is None else int(code)


def run_cli_subprocess(args: list[str], timeout: float = 120) -> subprocess.CompletedProcess:
    """Run the CLI in a child process so a hang fails the test instead of blocking it."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "from column_lineage.cli import main; "
            "sys.exit(main(sys.argv[2:]) or 0)")
    return subprocess.run([sys.executable, "-c", code, str(SRC), *[str(a) for a in args]],
                          capture_output=True, text=True, timeout=timeout)


def cli_args(root: Path | str, columns: str | list[str], out_dir: Path, *extra: str) -> list[str]:
    columns = [columns] if isinstance(columns, str) else list(columns)
    args: list[str] = []
    for column in columns:
        args += ["--column", column]
    args += ["--source-root", str(root), "--output", str(out_dir), "--no-progress", *extra]
    return args


def trace(root: str | Path, column: str, out_dir: Path, *extra: str, expect_exit: int = 0) -> "Trace":
    """Run one trace against a fixture name (under fixtures/column) or a path; load its JSON."""
    root_path = Path(root) if isinstance(root, Path) or "/" in str(root) else fixture(str(root))
    code = run_cli(cli_args(root_path, column, out_dir, *extra))
    assert code == expect_exit, f"exit code {code}, expected {expect_exit}"
    return load_trace(out_dir, column, root_path)


def load_trace(out_dir: Path, column: str, root: Path | None = None) -> "Trace":
    stem = stem_for(column)
    json_path = Path(out_dir) / f"{stem}.lineage.json"
    md_path = Path(out_dir) / f"{stem}.lineage.md"
    assert json_path.exists(), f"missing {json_path.name}; found {sorted(p.name for p in Path(out_dir).iterdir())}"
    return Trace(json.loads(json_path.read_text(encoding="utf-8")), json_path, md_path, root)


# ------------------------------------------------------------------- step views

def input_items(step: dict, role: str | None = None) -> list[dict]:
    return [i for i in step.get("inputs", []) if role is None or i.get("role") == role]


def inputs(step: dict, role: str | None = None) -> list[str]:
    """Input node ids (lower-cased) of a step, optionally only one role."""
    return [norm(i["node"]) for i in input_items(step, role)]


def branch_of(step: dict, node_id: str, role: str | None = None) -> str | None:
    matches = [i.get("branch") for i in input_items(step, role) if norm(i["node"]) == norm(node_id)]
    assert matches, f"{node_id} is not an input of step {step.get('target')}"
    assert len(matches) == 1, f"{node_id} appears {len(matches)} times in step {step.get('target')}"
    return matches[0]


def row_selection(step: dict, role: str | None = None) -> list[str]:
    return [norm(r["node"]) for r in step.get("row_selection", []) if role is None or r.get("role") == role]


def row_selection_pairs(step: dict) -> set[tuple[str, str]]:
    return {(norm(r["node"]), r.get("role")) for r in step.get("row_selection", [])}


# ------------------------------------------------------------------ trace view

class Trace:
    def __init__(self, data: dict, json_path: Path, md_path: Path, root: Path | None) -> None:
        self.data = data
        self.json_path = json_path
        self.md_path = md_path
        self.root = root
        self.nodes = {norm(n["id"]): n for n in data.get("nodes", [])}

    # -- basics
    @property
    def target_id(self) -> str:
        return norm(self.data["target"]["id"])

    @property
    def steps(self) -> list[dict]:
        return self.data.get("steps", [])

    @property
    def completeness(self) -> dict:
        return self.data["completeness"]

    @property
    def complete(self) -> bool:
        return self.data["completeness"]["status"] == "complete"

    @property
    def markdown(self) -> str:
        assert self.md_path.exists(), f"missing {self.md_path.name}"
        return self.md_path.read_text(encoding="utf-8")

    def steps_for(self, node_id: str) -> list[dict]:
        return [s for s in self.steps if norm(s["target"]) == norm(node_id)]

    def only_step_for(self, node_id: str) -> dict:
        """The one writing step of a node; the synthetic "rows not updated keep earlier content" step of
        an update-only column is not a writer and is left out."""
        found = [s for s in self.steps_for(node_id) if s["writer_kind"] != "retained"]
        assert len(found) == 1, f"expected one step for {node_id}, got {len(found)}: {[s['writer_kind'] for s in found]}"
        return found[0]

    # -- gaps and stops
    def gaps(self, kind: str | None = None) -> list[dict]:
        return [g for g in self.data.get("gaps", []) if kind is None or g["kind"] == kind]

    @property
    def gap_kinds(self) -> set[str]:
        return {g["kind"] for g in self.data.get("gaps", [])}

    def stops(self, reason: str | None = None) -> list[dict]:
        return [s for s in self.data.get("stops", []) if reason is None or s["reason"] == reason]

    def stop_pairs(self) -> set[tuple[str, str]]:
        return {(norm(s["node"]), s["reason"]) for s in self.data.get("stops", [])}

    def stop_nodes(self, reason: str | None = None) -> set[str]:
        return {norm(s["node"]) for s in self.stops(reason)}

    # -- identities
    def all_ids(self) -> set[str]:
        ids = set(self.nodes)
        for step in self.steps:
            ids.add(norm(step["target"]))
            ids.update(inputs(step))
            ids.update(row_selection(step))
        ids.update(norm(s["node"]) for s in self.data.get("stops", []))
        ids.update(norm(g["node"]) for g in self.data.get("gaps", []) if g.get("node"))
        return ids

    def raw_ids(self) -> set[str]:
        """Ids exactly as written (not lower-cased), for case-consistency checks."""
        ids = {n["id"] for n in self.data.get("nodes", [])}
        for step in self.steps:
            ids.add(step["target"])
            ids.update(i["node"] for i in step.get("inputs", []))
            ids.update(r["node"] for r in step.get("row_selection", []))
        return ids

    def ids_matching(self, prefix: str, suffix: str) -> list[str]:
        prefix, suffix = prefix.lower(), suffix.lower()
        return sorted(i for i in self.all_ids() if i.startswith(prefix) and i.endswith(suffix))

    def one_id(self, prefix: str, suffix: str) -> str:
        found = self.ids_matching(prefix, suffix)
        assert len(found) == 1, f"expected one id {prefix}...{suffix}, got {found}"
        return found[0]

    def temp_column(self, table: str, column: str) -> str:
        """The single node id of ``#table.column`` (``temp_table:<scope>::#table.column``)."""
        return self.one_id("temp_table:", f"::{table}.{column}")

    def all_input_ids(self, roles: tuple[str, ...] | None = None) -> set[str]:
        return {norm(i["node"]) for s in self.steps for i in s.get("inputs", [])
                if roles is None or i.get("role") in roles}

    def upstream(self, node_id: str, roles: tuple[str, ...] = ("value",)) -> set[str]:
        """Node ids reachable from ``node_id`` through step inputs with the given roles."""
        seen: set[str] = set()
        frontier = [norm(node_id)]
        while frontier:
            current = frontier.pop()
            for step in self.steps_for(current):
                for item in input_items(step):
                    if item.get("role") not in roles:
                        continue
                    nid = norm(item["node"])
                    if nid not in seen:
                        seen.add(nid)
                        frontier.append(nid)
        return seen

    def json_text(self) -> str:
        return self.json_path.read_text(encoding="utf-8")


def assert_step_evidence(t: Trace) -> None:
    """Every step carries file/line evidence relative to its source root."""
    assert t.steps, "trace has no steps"
    for step in t.steps:
        assert step.get("evidence"), f"step for {step['target']} has no evidence"
        for ev in step["evidence"]:
            assert ev["path"] and not Path(ev["path"]).is_absolute(), ev["path"]
            assert "\\" not in ev["path"]
            assert isinstance(ev["start_line"], int) and ev["start_line"] >= 1
            assert ev["end_line"] >= ev["start_line"]
            assert len(ev.get("excerpt") or "") <= 240
            if t.root is not None and t.root.is_dir():
                assert (t.root / ev["path"]).exists(), f"evidence path {ev['path']} not under {t.root}"
