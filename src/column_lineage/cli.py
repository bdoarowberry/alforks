"""Command line for the column lineage analyzer. See docs/column-lineage-design.md."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import re
from concurrent.futures import ProcessPoolExecutor
import os
import subprocess
import sys
import time

from table_lineage.progress import ProgressReporter
from table_lineage.sources import DEFAULT_EXCLUDES, FilesystemSourceProvider

from . import gap_summary
from .catalog import Catalog, prepare_in_worker
from .model import UNKNOWN, UNRESOLVED, Boundary, Gap, Node, TraceResult
from .tracer import Tracer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="column_lineage",
                                     description="Trace a SQL column back to the columns and inputs that produce it.")
    parser.add_argument("--column", action="append", required=True, metavar="OBJECT.COLUMN",
                        help="Target column, e.g. db.schema.table.column (repeatable)")
    parser.add_argument("--source-root", nargs="+", action="extend", required=True, help="SQL files or directories to scan")
    parser.add_argument("--output", default="column-lineage-output", help="Output directory")
    parser.add_argument("--stop-at-database", nargs="+", action="extend", default=[], metavar="DB")
    parser.add_argument("--stop-at-object", nargs="+", action="extend", default=[], metavar="GLOB")
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--default-database", default=None)
    parser.add_argument("--database-map", nargs="+", action="extend", default=[], metavar="PATH=DB",
                        help="Files under PATH (relative to a source root, or absolute) belong to database DB")
    parser.add_argument("--default-schema", default="dbo")
    parser.add_argument("--trace-row-selection", action="store_true")
    parser.add_argument("--include", nargs="+", action="extend", default=[])
    parser.add_argument("--exclude", nargs="+", action="extend", default=[])
    parser.add_argument("--include-dated-snapshots", action="store_true")
    parser.add_argument("--gap-summary", action="store_true", help="Write sanitized gap-summary.json")
    parser.add_argument("--fail-on-incomplete", action="store_true")
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--progress-interval", type=float, default=15.0)
    parser.add_argument("--jobs", type=int, default=None,
                        help="Worker processes for parsing (default: number of CPUs, at most 8; 1 = no workers)")
    parser.add_argument("--verbose", action="store_true")
    return parser


def stem(spec: str) -> str:
    value = spec.strip().lower().replace("[", "").replace("]", "").replace('"', "")
    return re.sub(r"[^a-z0-9._-]", "_", value)


def source_revision(roots: list[Path]) -> str | None:
    revisions = []
    for root in roots:
        folder = root if root.is_dir() else root.parent
        try:
            out = subprocess.run(["git", "-C", str(folder), "rev-parse", "HEAD"], capture_output=True, text=True,
                                 timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            revisions.append(out.stdout.strip()[:12])
    return ",".join(sorted(set(revisions))) or None


def root_labels(roots: list[Path]) -> dict[Path, str]:
    labels: dict[Path, str] = {}
    used: dict[str, int] = {}
    for root in roots:
        base = (root.name or "root") if root.is_dir() else root.stem
        used[base] = used.get(base, 0) + 1
        labels[root] = base if used[base] == 1 else f"{base}-{used[base]}"
    return labels


def main(argv: list[str] | None = None) -> int:
    # SQLGlot logs a warning for every statement it falls back on; parse failures are reported in
    # scan-diagnostics.json instead.
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_depth is not None and args.max_depth < 0:
        parser.error("--max-depth must be nonnegative")
    mapping = []
    for item in args.database_map:
        if "=" not in item:
            parser.error(f"--database-map expects PATH=DB, got {item!r}")
        path, db = item.rsplit("=", 1)
        mapping.append((path, db))
    roots = [Path(r).resolve() for r in args.source_root]
    for root in roots:
        if not root.exists():
            parser.error(f"Source root does not exist: {root}")
    labels = root_labels(roots)
    started = time.monotonic()
    catalog = Catalog(args.default_database, args.default_schema, mapping)
    with ProgressReporter(enabled=not args.no_progress, interval=args.progress_interval) as progress:
        progress.start_phase("discovering", completed=0, total=None)
        files = []
        for root in roots:
            provider = FilesystemSourceProvider([str(root)], DEFAULT_EXCLUDES + tuple(args.exclude), tuple(args.include),
                                                include_dated_snapshots=args.include_dated_snapshots)
            provider.preserve_newlines = True
            try:
                files.extend((root, source) for source in provider.files())
            except (OSError, ValueError) as exc:
                parser.error(str(exc))
        if not files:
            parser.error("No SQL source files matched the selected roots and filters.")
        progress.start_phase("parsing", completed=0, total=len(files))
        jobs = args.jobs if args.jobs is not None else min(8, os.cpu_count() or 1)
        prepared_stream = None
        if jobs > 1 and len(files) >= 20:
            settings = (args.default_database, args.default_schema, mapping)
            work = [(str(source.path), source.text, str(root), labels[root], settings) for root, source in files]
            try:
                pool = ProcessPoolExecutor(max_workers=jobs)
                prepared_stream = pool.map(prepare_in_worker, work, chunksize=8)
            except (OSError, ValueError, NotImplementedError):
                prepared_stream = None
        for done, (root, source) in enumerate(files, 1):
            progress.start_item(source.path.name, completed=done - 1, total=len(files))
            if prepared_stream is not None:
                catalog.add_prepared(source, root, labels[root], next(prepared_stream))
            else:
                catalog.add_file(source, root, labels[root])
            progress.complete_item(done)
        if prepared_stream is not None:
            pool.shutdown()
        progress.start_phase("analyzing", completed=len(files), total=len(files))
        parse_seconds = time.monotonic() - started
        scan = {
            "roots": [labels[r] for r in roots],
            "files": catalog.stats["files"],
            "statements": catalog.stats["statements"],
            "parsed": catalog.stats["parsed"],
            "fallback": catalog.stats["fallback"],
            "ignored_non_lineage": catalog.stats["ignored"],
            "repaired": catalog.stats["repaired"],
            "statement_issues": len(catalog.issues),
            "file_errors": len(catalog.file_errors),
            "source_revision": source_revision(roots),
            "options": {"default_database": args.default_database, "default_schema": args.default_schema,
                        "database_map": [f"{p}={d}" for p, d in mapping],
                        "trace_row_selection": args.trace_row_selection},
        }
        boundary = Boundary(tuple(d.lower() for d in args.stop_at_database), tuple(args.stop_at_object), args.max_depth)
        tracer = Tracer(catalog, trace_row_selection=args.trace_row_selection)
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        traces = []
        incomplete = False
        written = []
        try:
            from .render_markdown import render_markdown
        except ImportError:  # pragma: no cover - renderer ships with the package
            render_markdown = None
        for spec in args.column:
            progress.start_phase("analyzing", completed=len(files), total=len(files), current=spec)
            try:
                result = tracer.trace(spec, boundary, scan)
            except Exception as exc:  # noqa: BLE001 - report the failure in the trace instead of stopping the run
                result = TraceResult(Node(UNKNOWN, spec), spec, boundary, scan)
                result.target_status = UNRESOLVED
                result.target_gaps = [result.add_gap(Gap("unsupported_statement",
                                      f"The trace failed with {type(exc).__name__}: {str(exc)[:200]}"))]
            data = result.to_dict()
            traces.append(data)
            incomplete = incomplete or data["completeness"]["status"] != "complete"
            name = stem(spec)
            json_path = output / f"{name}.lineage.json"
            json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            written.append(json_path)
            if render_markdown is not None:
                md_path = output / f"{name}.lineage.md"
                md_path.write_text(render_markdown(data, json_path.name), encoding="utf-8")
                written.append(md_path)
        diagnostics = {
            "schema_version": "column-lineage-scan/1.0",
            "scan": scan,
            "file_errors": [{"root": r, "path": p, "error": e} for r, p, e in sorted(catalog.file_errors)],
            "statement_issues": [
                {"kind": i.kind, "message": i.message, "targets": list(i.targets),
                 "evidence": catalog.evidence(i.unit, i.statement, role=i.kind).to_dict()}
                for i in sorted(catalog.issues, key=lambda i: (i.unit.root_label, i.unit.rel_path, i.statement.start))
            ],
        }
        (output / "scan-diagnostics.json").write_text(json.dumps(diagnostics, indent=2, ensure_ascii=False) + "\n",
                                                      encoding="utf-8")
        if args.gap_summary:
            summary = gap_summary.build(catalog, traces)
            (output / "gap-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if args.verbose:
        print(json.dumps({"files": len(files), "statements": catalog.stats["statements"],
                          "parse_seconds": round(parse_seconds, 2),
                          "total_seconds": round(time.monotonic() - started, 2),
                          "traces": [{"column": t["requested"], **{k: t["completeness"][k] for k in ("status", "gaps_by_kind")}}
                                     for t in traces],
                          "written": [str(p) for p in written]}, indent=2))
    return 3 if (args.fail_on_incomplete and incomplete) else 0


if __name__ == "__main__":
    sys.exit(main())
