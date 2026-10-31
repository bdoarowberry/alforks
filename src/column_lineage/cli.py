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

from . import assertions, gap_summary
from .catalog import Catalog, prepare_in_worker, split_name
from .model import UNKNOWN, UNRESOLVED, Boundary, Gap, Node, ObjectRef, TraceResult
from .tracer import Tracer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="column_lineage",
                                     description="Trace a SQL column back to the columns and inputs that produce it.")
    parser.add_argument("--columns-file", metavar="FILE",
                        help="Text file of target columns, one per line (blank lines and # comments ignored)")
    parser.add_argument("--column", action="append", default=[], metavar="OBJECT.COLUMN",
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
    parser.add_argument("--local-server", nargs="+", action="extend", default=[], metavar="NAME",
                        help="Linked-server name(s) whose four-part references mean the same databases as the "
                             "scanned files (add the remote database's SQL as another --source-root)")
    parser.add_argument("--trace-row-selection", action="store_true")
    parser.add_argument("--include", nargs="+", action="extend", default=[])
    parser.add_argument("--exclude", nargs="+", action="extend", default=[])
    parser.add_argument("--include-dated-snapshots", action="store_true")
    parser.add_argument("--assertions", nargs="+", action="extend", default=[], metavar="FILE",
                        help="JSON file(s) of human-asserted mappings for steps SQL cannot prove (see README)")
    parser.add_argument("--package-root", nargs="+", action="extend", default=[], metavar="DIR|FILE",
                        help="SSIS package files (.dtsx) or folders to scan; their data flows become writers")
    parser.add_argument("--package-include", nargs="+", action="extend", default=[], metavar="GLOB",
                        help="Only packages whose path relative to a package root matches")
    parser.add_argument("--package-exclude", nargs="+", action="extend", default=[], metavar="GLOB",
                        help="Leave out packages whose path relative to a package root matches")
    parser.add_argument("--connection-map", nargs="+", action="extend", default=[], metavar="NAME=DB",
                        help="Database of an SSIS connection manager (overrides its Initial Catalog)")
    parser.add_argument("--declared-terminals", metavar="FILE", default=None,
                        help="Objects or columns accepted as terminal sources for this run: one glob per line "
                             "(db.schema.table or db.schema.table.column), optionally followed by '# reason'. "
                             "Stops there keep their reason and are labelled declared_terminal.")
    parser.add_argument("--entry-point", nargs="+", action="extend", default=[], metavar="PROCEDURE",
                        help="Procedure(s) the run starts from. When writers in several procedures conflict and the "
                             "call sequence from here fixes their order, the order is noted instead of a gap.")
    parser.add_argument("--no-overview", action="store_true",
                        help="Do not write the <column>.overview.md pages (routes-index.md/.json are still written)")
    parser.add_argument("--business-glossary", metavar="FILE", default=None,
                        help='Business definitions for the overview pages: JSON {"columns": [{"column": '
                             '"db.schema.table.col" (glob ok), "definition": ..., "status": "approved"|"proposed", '
                             '"source": ...}]}. Never derived from names.')
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


def blocking_statements(traces: list[dict], limit: int = 20) -> list[dict]:
    """Statements behind value or context gaps, by how many traces they block (local diagnostics)."""
    found: dict[str, dict] = {}
    for data in traces:
        seen = set()
        for gap in data.get("gaps", []):
            if gap.get("affects") == "row_selection":
                continue
            for ev in gap.get("evidence", [])[:1]:
                key = ev.get("statement_id") or f"{ev.get('path')}:{ev.get('start_line')}"
                entry = found.setdefault(key, {"location": f"{ev.get('root') + '/' if ev.get('root') else ''}"
                                                           f"{ev.get('path')}:{ev.get('start_line')}",
                                               "excerpt": ev.get("excerpt"), "kinds": set(), "traces": 0})
                entry["kinds"].add(gap["kind"])
                if key not in seen:
                    seen.add(key)
                    entry["traces"] += 1
    ranked = sorted(found.values(), key=lambda e: (-e["traces"], e["location"]))[:limit]
    return [{**e, "kinds": sorted(e["kinds"])} for e in ranked]


def main(argv: list[str] | None = None) -> int:
    # SQLGlot logs a warning for every statement it falls back on; parse failures are reported in
    # scan-diagnostics.json instead.
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.columns_file:
        try:
            lines = Path(args.columns_file).read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            parser.error(f"--columns-file: {exc}")
        args.column += [line.split("#", 1)[0].strip() for line in lines if line.split("#", 1)[0].strip()]
    args.column = list(dict.fromkeys(args.column))
    if not args.column:
        parser.error("give at least one --column or a --columns-file")
    if args.max_depth is not None and args.max_depth < 0:
        parser.error("--max-depth must be nonnegative")
    mapping = []
    for item in args.database_map:
        if "=" not in item:
            parser.error(f"--database-map expects PATH=DB, got {item!r}")
        path, db = item.rsplit("=", 1)
        mapping.append((path, db))
    asserted = []
    for path in args.assertions:
        try:
            asserted.extend(assertions.load(path))
        except (OSError, ValueError) as exc:
            parser.error(f"--assertions: {exc}")
    terminals = []
    if args.declared_terminals:
        try:
            terminals = load_terminals(Path(args.declared_terminals))
        except (OSError, ValueError) as exc:
            parser.error(f"--declared-terminals: {exc}")
    glossary = None
    if args.business_glossary:
        from .render_overview import load_glossary
        try:
            glossary = load_glossary(args.business_glossary)
        except (OSError, ValueError) as exc:
            parser.error(f"--business-glossary: {exc}")
    # Cite assertion files relative to the output folder, where the pages are written.
    out_dir = Path(args.output).resolve()
    for assertion in asserted:
        assertion.source = Path(os.path.relpath(Path(assertion.source).resolve(), out_dir)).as_posix()
    duplicate = sorted({a.id for a in asserted if [b.id for b in asserted].count(a.id) > 1})
    if duplicate:
        parser.error(f"--assertions: duplicate assertion ids across files: {', '.join(duplicate)}")
    roots = [Path(r).resolve() for r in args.source_root]
    for root in roots:
        if not root.exists():
            parser.error(f"Source root does not exist: {root}")
    package_roots = list(dict.fromkeys(Path(r).resolve() for r in args.package_root))
    for root in package_roots:
        if not root.exists():
            parser.error(f"Package root does not exist: {root}")
    connection_map = {}
    if package_roots or args.connection_map:
        from .ssis import parse_connection_map
        try:
            connection_map = parse_connection_map(args.connection_map)
        except ValueError as exc:
            parser.error(str(exc))
    labels = root_labels(roots + [r for r in package_roots if r not in roots])
    started = time.monotonic()
    catalog = Catalog(args.default_database, args.default_schema, mapping, tuple(args.local_server))
    with ProgressReporter(enabled=not args.no_progress, interval=args.progress_interval) as progress:
        progress.start_phase("discovering", completed=0, total=None)
        files = []
        encodings: dict[str, int] = {}
        for root in roots:
            provider = FilesystemSourceProvider([str(root)], DEFAULT_EXCLUDES + tuple(args.exclude), tuple(args.include),
                                                include_dated_snapshots=args.include_dated_snapshots)
            provider.preserve_newlines = True
            try:
                files.extend((root, source) for source in provider.files())
            except (OSError, ValueError) as exc:
                parser.error(str(exc))
            for name, count in provider.encodings.items():
                encodings[name] = encodings.get(name, 0) + count
            catalog.skipped_files.extend(
                (f"{labels[root]}/{Path(p).relative_to(root).as_posix() if root.is_dir() else Path(p).name}", str(p))
                for p in provider.ignored_dated_snapshots)
        catalog.file_encodings = encodings
        if not files:
            parser.error("No SQL source files matched the selected roots and filters.")
        progress.start_phase("parsing", completed=0, total=len(files))
        jobs = args.jobs if args.jobs is not None else min(8, os.cpu_count() or 1)
        prepared_stream = None
        if jobs > 1 and len(files) >= 20:
            settings = (args.default_database, args.default_schema, mapping, tuple(args.local_server))
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
        packages = None
        if package_roots:
            from .ssis import PackageCatalog
            progress.start_phase("packages", completed=0, total=len(package_roots))
            packages = PackageCatalog(catalog, connection_map)
            for root in package_roots:
                packages.scan_root(root, labels[root], tuple(args.package_include), tuple(args.package_exclude))
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
            "skipped_dated_snapshots": len(catalog.skipped_files),
            "file_encodings": dict(sorted(encodings.items())),
            "files_without_statements": catalog.stats["files_without_statements"],
            "statement_issues": len(catalog.issues),
            "file_errors": len(catalog.file_errors),
            "source_revision": source_revision(roots),
            "options": {"default_database": args.default_database, "default_schema": args.default_schema,
                        "local_servers": sorted({s.lower() for s in args.local_server}),
                        "database_map": [f"{p}={d}" for p, d in mapping],
                        "trace_row_selection": args.trace_row_selection},
        }
        if packages is not None:
            scan["packages"] = {"roots": [labels[r] for r in package_roots],
                                **packages.scan_info(args.package_include, args.package_exclude)}
        boundary = Boundary(tuple(d.lower() for d in args.stop_at_database), tuple(args.stop_at_object), args.max_depth)
        tracer = Tracer(catalog, trace_row_selection=args.trace_row_selection, assertions=asserted, packages=packages)
        scan["assertions"] = len(asserted)
        tracer.terminals = terminals
        for name in args.entry_point:
            parts = [p.lower() for p in split_name(name)]
            parts = [None] * (3 - len(parts[-3:])) + parts[-3:]
            procs = catalog.procs_for(ObjectRef(parts[2], parts[1] or catalog.default_schema, parts[0], None, "procedure"))
            if not procs:
                parser.error(f"--entry-point: procedure {name} is not defined in the scanned files")
            if len(procs) > 1:
                parser.error(f"--entry-point: {name} has {len(procs)} definitions; exclude the copies that do not run")
            tracer.entry_points.append(procs[0].scope)
        scan["entry_points"] = len(tracer.entry_points)
        scan["declared_terminals"] = len(terminals)
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        traces = []
        trace_files = []
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
            # Additive block: route families derived from the steps, stops and gaps above (nothing is dropped).
            from .render_overview import route_families_block
            data["route_families"] = route_families_block(data)
            traces.append(data)
            incomplete = incomplete or data["completeness"]["status"] != "complete"
            name = stem(spec)
            json_path = output / f"{name}.lineage.json"
            json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            written.append(json_path)
            trace_files.append((data, json_path))
            if render_markdown is not None:
                md_path = output / f"{name}.lineage.md"
                md_path.write_text(render_markdown(data, json_path.name), encoding="utf-8")
                written.append(md_path)
        diagnostics = {
            "schema_version": "column-lineage-scan/1.0",
            "scan": scan,
            "file_errors": [{"root": r, "path": p, "error": e} for r, p, e in sorted(catalog.file_errors)],
            "assertion_errors": sorted(set(tracer.assertion_errors)),
            "assertions_not_used": sorted(a.id for a in asserted if a.id not in tracer.assertions_seen),
            "assertion_effects": {k: sorted(v, key=lambda g: (g["kind"], g["location"]))
                                  for k, v in sorted(tracer.assertion_effects.items())},
            "statements_blocking_most_traces": blocking_statements(traces),
            "statement_issues": [issue_entry(catalog, i) for i in
                                 sorted(catalog.issues, key=lambda i: (i.unit.root_label, i.unit.rel_path, i.statement.start))],
        }
        if packages is not None:
            diagnostics["package_issues"] = packages.issue_entries()
        (output / "scan-diagnostics.json").write_text(json.dumps(diagnostics, indent=2, ensure_ascii=False) + "\n",
                                                      encoding="utf-8")
        # Reader-facing outputs, rendered from the trace JSON only: overview pages and the run's route index.
        from .render_overview import index_counts, write_outputs
        pages, route_index = write_outputs(trace_files, index_dir=output, glossary=glossary,
                                           overviews=not args.no_overview)
        written += pages
        if args.gap_summary:
            summary = gap_summary.build(catalog, traces, packages=packages)
            summary["route_index"] = index_counts(route_index)  # counts only
            (output / "gap-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if args.verbose:
        print(json.dumps({"files": len(files), "statements": catalog.stats["statements"],
                          "parse_seconds": round(parse_seconds, 2),
                          "total_seconds": round(time.monotonic() - started, 2),
                          "traces": [{"column": t["requested"], **{k: t["completeness"][k] for k in ("status", "gaps_by_kind")}}
                                     for t in traces],
                          "written": [str(p) for p in written]}, indent=2))
    return 3 if (args.fail_on_incomplete and incomplete) else 0


def load_terminals(path: Path) -> list[tuple[str, str]]:
    """(lower-cased glob, reason) pairs. Text: one glob per line, then optionally ' # reason' (the '#'
    must follow whitespace, so #temp patterns work); lines starting with '# ' are comments. JSON:
    {"terminals": [{"pattern": "...", "reason": "..."}]}."""
    text = path.read_text(encoding="utf-8-sig")

    def clean(pattern: str) -> str:
        return pattern.strip().lower().replace("[", "").replace("]", "")

    if text.lstrip().startswith(("{", "[")):
        data = json.loads(text)
        items = data.get("terminals") if isinstance(data, dict) else None
        if not isinstance(items, list) or not all(
                isinstance(i, dict) and isinstance(i.get("pattern"), str) and i["pattern"].strip() for i in items):
            raise ValueError('expected {"terminals": [{"pattern": "<glob>", "reason": "..."}]} with string patterns')
        return [(clean(i["pattern"]), str(i.get("reason") or "declared terminal")) for i in items]
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("# ") or stripped == "#":
            continue
        separator = re.search(r"\s#", stripped)
        pattern = stripped[:separator.start()] if separator else stripped
        reason = stripped[separator.end():] if separator else ""
        out.append((clean(pattern), reason.strip() or "declared terminal"))
    return out


def issue_entry(catalog, issue) -> dict:
    """One statement issue for scan-diagnostics.json, with the outcome of dynamic SQL evaluation."""
    kind, message = issue.kind, issue.message
    evaluation = catalog.dynamic_evaluation(issue)
    if catalog.dead_dynamic(issue):
        kind, message = "dead_dynamic_sql", "Dynamic SQL that can only execute an empty string or NULL; not a writer."
    elif evaluation is not None and evaluation.complete:
        kind = "evaluated_dynamic_sql"
        message = (f"Dynamic SQL evaluated statically: {len(evaluation.alternatives)} possible text(s), all enumerated "
                   "from literals and analysed as writers.")
    entry = {"kind": kind, "message": message, "targets": list(issue.targets),
             "evidence": catalog.evidence(issue.unit, issue.statement, role=issue.kind).to_dict()}
    if evaluation is not None:
        entry["evaluation"] = {**evaluation.detail(),
                               "alternatives": [{"conditions": a.conditions, "built_at": a.built_at,
                                                 "statements": len(a.statements), "sql": a.sql[:2000]}
                                                for a in evaluation.alternatives]}
    return entry


if __name__ == "__main__":
    sys.exit(main())
