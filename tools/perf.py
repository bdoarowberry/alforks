#!/usr/bin/env python3
"""Performance and accuracy harness for the lineage CLIs on a generated corpus.

    python tools/perf.py --corpus DIR [--columns K] [--tool table|column|both|index|all]
                         [--targets N] [--tracemalloc] [--profile] [--timeout S]

* ``table``  runs the existing table-level CLI (``python -m table_lineage.cli``) on N mart
  targets from ``DIR/manifest.json`` and checks that the upstream objects known from the
  generator's ground truth appear in the table graph.
* ``column`` runs ``python -m column_lineage`` once with K known-chain columns (sharing the
  parse) using the manifest's ``--database-map`` folders, then compares the reached leaf
  columns with each chain's expected leaves (precision and recall per chain). It is skipped
  with a message when ``column_lineage`` has no ``__main__`` yet.
* ``index``  indexes every file with ``table_lineage.index.SourceIndex`` (the parse every
  tool pays for) and reports statement and parse-fallback statistics.

Every run happens in a fresh child interpreter, so wall time and peak RSS
(``resource.getrusage``) are per run. ``--tracemalloc`` adds the Python-heap peak (slower).
Per-phase timings come from the table tool's ``ProgressReporter`` phases, or from a
``timings``/``phases`` object in the column tool's ``scan-diagnostics.json`` when present.
``--profile`` writes a cProfile dump per run and prints the top functions by cumulative time.

Results are printed as a compact table and written to ``DIR/perf-report.json``.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
LEAF_STOPS = {"source_without_writer", "boundary"}


# ---------------------------------------------------------------------------
# child process side

def _child(kind: str, spec: dict, result_path: str) -> int:
    sys.path.insert(0, str(SRC))
    import cProfile
    import pstats
    import resource
    import tracemalloc

    phases: list[tuple[str, float]] = []
    try:
        from table_lineage import progress

        original = progress.ProgressReporter.start_phase

        def start_phase(self, phase, *args, **kwargs):
            if not phases or phases[-1][0] != phase:
                phases.append((phase, time.perf_counter()))
            return original(self, phase, *args, **kwargs)

        progress.ProgressReporter.start_phase = start_phase
    except Exception:  # pragma: no cover - harness must not depend on internals
        pass

    if spec.get("tracemalloc"):
        tracemalloc.start()
    profiler = cProfile.Profile() if spec.get("profile") else None
    extra: dict = {}
    rc = 0
    error = None
    started = time.perf_counter()
    if profiler:
        profiler.enable()
    try:
        if kind == "table":
            from table_lineage.cli import main
            rc = main(spec["argv"])
        elif kind == "column":
            import runpy
            sys.argv = ["column_lineage"] + spec["argv"]
            try:
                runpy.run_module("column_lineage", run_name="__main__", alter_sys=True)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        elif kind == "index":
            extra = _index_stats(spec)
        else:
            raise ValueError(kind)
    except Exception as exc:  # report, do not crash the harness
        import traceback
        error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        extra["traceback"] = traceback.format_exc()[-4000:]
        rc = 1
    finally:
        if profiler:
            profiler.disable()
    ended = time.perf_counter()
    result = {
        "rc": rc,
        "error": error,
        "wall_s": round(ended - started, 3),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "phases": _phase_durations(phases, started, ended),
        **extra,
    }
    if spec.get("tracemalloc"):
        result["tracemalloc_peak_mb"] = round(tracemalloc.get_traced_memory()[1] / 2 ** 20, 1)
    if profiler:
        profiler.dump_stats(spec["profile"])
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(spec.get("profile_top", 15))
        result["profile_path"] = spec["profile"]
        result["profile_top"] = _trim_profile(stream.getvalue())
    Path(result_path).write_text(json.dumps(result), encoding="utf-8")
    return 0


def _phase_durations(phases: list[tuple[str, float]], started: float, ended: float) -> dict[str, float]:
    out: dict[str, float] = {}
    if phases:
        out["startup"] = round(phases[0][1] - started, 3)
    for i, (name, at) in enumerate(phases):
        until = phases[i + 1][1] if i + 1 < len(phases) else ended
        out[name] = round(out.get(name, 0.0) + until - at, 3)
    return out


def _trim_profile(text: str) -> str:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip().startswith("ncalls")), 0)
    return "\n".join(line.replace(str(ROOT) + os.sep, "") for line in lines[start:] if line.strip())


def _index_stats(spec: dict) -> dict:
    from table_lineage.index import SourceIndex
    from table_lineage.sources import DEFAULT_EXCLUDES, FilesystemSourceProvider

    provider = FilesystemSourceProvider([spec["corpus"]], DEFAULT_EXCLUDES)
    provider.preserve_newlines = True
    shared = SourceIndex() if spec.get("shared") else None
    files = statements = parsed = 0
    fallback_by_keyword: dict[str, int] = {}
    worst: list[tuple[float, str, int]] = []
    biggest = (0, "")
    for source in provider.files():
        index = shared or SourceIndex()
        before = len(index.statements)
        index.add(source, include_ddl=True)
        mine = index.statements[before:]
        files += 1
        statements += len(mine)
        ok = sum(1 for s in mine if s.parse_status == "parsed")
        parsed += ok
        for s in mine:
            if s.parse_status != "parsed":
                word = (re.match(r"\s*([A-Za-z]+)", s.text) or re.match("", "")).group(1) or "?"
                fallback_by_keyword[word.upper()] = fallback_by_keyword.get(word.upper(), 0) + 1
        if mine:
            worst.append((ok / len(mine), str(source.path), len(mine)))
        biggest = max(biggest, (len(source.text), str(source.path)))
    worst.sort()
    root = str(Path(spec["corpus"]).resolve()) + os.sep
    return {
        "files": files,
        "statements": statements,
        "parsed": parsed,
        "fallback": statements - parsed,
        "fallback_rate": round((statements - parsed) / statements, 4) if statements else 0.0,
        "fallback_by_keyword": dict(sorted(fallback_by_keyword.items(), key=lambda kv: -kv[1])),
        "worst_files": [{"path": p.replace(root, ""), "parsed_ratio": round(r, 3), "statements": n}
                        for r, p, n in worst[:5]],
        "largest_file": {"path": biggest[1].replace(root, ""), "chars": biggest[0]},
        "symbols": len(shared.symbols) if shared else None,
    }


# ---------------------------------------------------------------------------
# parent side

def run_child(kind: str, spec: dict, timeout: float) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        result_path = handle.name
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    cmd = [sys.executable, str(Path(__file__).resolve()), "--_child", kind, "--_spec", json.dumps(spec),
           "--_result", result_path]
    started = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return {"rc": None, "error": f"timeout after {timeout:.0f}s", "wall_s": round(time.perf_counter() - started, 3)}
    try:
        result = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = {"rc": proc.returncode, "error": "child produced no result",
                  "wall_s": round(time.perf_counter() - started, 3)}
    finally:
        try:
            os.unlink(result_path)
        except OSError:
            pass
    if proc.returncode != 0 or result.get("error"):
        result["stderr_tail"] = proc.stderr[-2000:]
    return result


def column_tool_available() -> tuple[bool, str]:
    sys.path.insert(0, str(SRC))
    try:
        if importlib.util.find_spec("column_lineage") is None:
            return False, "package column_lineage not found under src/"
        if importlib.util.find_spec("column_lineage.__main__") is None:
            return False, "column_lineage has no __main__ module yet (python -m column_lineage unavailable)"
    except Exception as exc:  # broken package
        return False, f"column_lineage import failed: {type(exc).__name__}: {exc}"
    finally:
        sys.path.remove(str(SRC))
    return True, ""


def stem_for(spec: str) -> str:
    """Output stem per docs/column-lineage-design.md."""
    return re.sub(r"[^a-z0-9._-]", "_", spec.lower().replace("[", "").replace("]", ""))


def norm_node(value) -> str:
    if isinstance(value, dict):
        value = value.get("id") or value.get("node") or ""
    return str(value).lower().replace("[", "").replace("]", "")


def suffix3(node: str) -> str:
    return ".".join(node.split(".")[-3:])


def ratio(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def value_path_leaves(trace: dict) -> set[str]:
    """Leaves reached from the target through step inputs only (row selection excluded)."""
    target = norm_node(trace.get("target"))
    by_target: dict[str, list[dict]] = {}
    for step in trace.get("steps", []):
        by_target.setdefault(norm_node(step.get("target")), []).append(step)
    leaf_stops = {norm_node(stop.get("node")) for stop in trace.get("stops", []) if stop.get("reason") in LEAF_STOPS}
    seen, leaves, stack = set(), set(), [target]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        steps = by_target.get(node)
        if not steps:
            if node != target and node in leaf_stops:
                leaves.add(node)
            continue
        for step in steps:
            for item in step.get("inputs", []):
                stack.append(norm_node(item.get("node")))
    return leaves


def evaluate_chain(chain: dict, trace: dict | None) -> dict:
    expected = set(chain["expected_leaves"])
    static = set(chain["static_expected_leaves"])
    row = {"id": chain["id"], "column": chain["column"], "layers": chain["expected_layers"],
           "opaque": chain["opaque_features"], "expected": len(expected), "static_expected": len(static)}
    if trace is None:
        row["error"] = "trace JSON not found"
        return row
    target = norm_node(trace.get("target"))
    stop_leaves = {norm_node(stop.get("node")) for stop in trace.get("stops", []) if stop.get("reason") in LEAF_STOPS}
    stop_leaves -= {"", target}
    # Score the value path (inputs only); row-selection leaves in `stops` are counted separately.
    reached = value_path_leaves(trace)
    hits = reached & expected
    lenient_expected = {suffix3(n) for n in expected}
    lenient_hits = {n for n in reached if suffix3(n) in lenient_expected}
    completeness = trace.get("completeness", {}) or {}
    gaps = completeness.get("gaps_by_kind") or {}
    if not gaps:
        for gap in trace.get("gaps", []):
            gaps[gap.get("kind", "?")] = gaps.get(gap.get("kind", "?"), 0) + 1
    row.update({
        "reached": len(reached),
        "stop_leaves": len(stop_leaves),
        "stop_leaves_outside_value_path": len(stop_leaves - reached),
        "precision": ratio(len(hits), len(reached)),
        "recall_static": ratio(len(reached & static), len(static)),
        "recall_all": ratio(len(hits), len(expected)),
        "recall_lenient": ratio(len({suffix3(n) for n in lenient_hits}), len(lenient_expected)),
        "unknown_db_leaves": sum(1 for n in reached if n.startswith("?.")),
        "missed": sorted(static - reached)[:10],
        "unexpected": sorted(reached - expected)[:10],
        "status": completeness.get("status"),
        "gaps_by_kind": gaps,
        "likely_gaps_reported": all(kind in gaps for kind in chain.get("likely_gap_kinds", [])),
        "steps": len(trace.get("steps", [])),
        "nodes": len(trace.get("nodes", [])),
    })
    return row


def table_object_recall(graph: dict, expected: list[str]) -> dict:
    keys = set()
    for node in graph.get("nodes", []):
        name = norm_node(node.get("normalized_name") or node.get("id") or node.get("name"))
        keys.add(name)
    got = {".".join(k.split(".")[-2:]) for k in keys}
    want = {".".join(e.split(".")[-2:]) for e in expected}
    return {"expected_objects": len(want), "found": len(want & got), "recall": ratio(len(want & got), len(want)),
            "missing": sorted(want - got)[:10]}


def fmt_phases(phases: dict) -> str:
    return " ".join(f"{k}={v:.1f}" for k, v in phases.items() if v >= 0.05)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Time the lineage CLIs on a generated corpus.")
    parser.add_argument("--corpus", help="generated corpus directory (contains manifest.json)")
    parser.add_argument("--tool", choices=("table", "column", "both", "index", "all"), default="both")
    parser.add_argument("--columns", type=int, default=20, help="known-chain columns to trace (column tool)")
    parser.add_argument("--targets", type=int, default=3, help="mart targets for the table tool")
    parser.add_argument("--per-column", type=int, default=0, help="also time the first N chains one per run")
    parser.add_argument("--map-style", choices=("repeat", "list"), default="list",
                        help="pass all mappings after one --database-map (list, default) or one flag per "
                             "mapping (repeat; an nargs='+' option without action='extend' keeps only the last)")
    parser.add_argument("--column-arg", action="append", default=[], help="extra argument for the column CLI")
    parser.add_argument("--table-arg", action="append", default=[], help="extra argument for the table CLI")
    parser.add_argument("--shared-index", action="store_true", help="index tool: one SourceIndex for all files")
    parser.add_argument("--tracemalloc", action="store_true")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--profile-top", type=int, default=15)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--report", default=None, help="report path (default DIR/perf-report.json)")
    parser.add_argument("--_child", help=argparse.SUPPRESS)
    parser.add_argument("--_spec", help=argparse.SUPPRESS)
    parser.add_argument("--_result", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._child:
        return _child(args._child, json.loads(args._spec), args._result)
    if not args.corpus:
        parser.error("--corpus is required")

    corpus = Path(args.corpus).resolve()
    manifest_path = corpus / "manifest.json"
    if not manifest_path.exists():
        parser.error(f"{manifest_path} not found; generate the corpus with tools/generate_corpus.py first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    work = corpus.parent / (corpus.name + "-perf-out")
    work.mkdir(exist_ok=True)
    tools = {"table": ["table"], "column": ["column"], "both": ["table", "column"], "index": ["index"],
             "all": ["index", "table", "column"]}[args.tool]
    common = {"tracemalloc": args.tracemalloc, "profile_top": args.profile_top}
    report: dict = {
        "corpus": str(corpus),
        "manifest_stats": manifest.get("stats"),
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "cpus": os.cpu_count(), "sqlglot": _sqlglot_version()},
        "runs": [],
        "chains": [],
        "skipped": [],
    }
    rows: list[list[str]] = []

    def spec_for(name: str, i: int, **extra) -> dict:
        spec = dict(common, **extra)
        if args.profile:
            spec["profile"] = str(work / f"{name}-{i}.prof")
        return spec

    if "index" in tools:
        spec = spec_for("index", 0, corpus=str(corpus), shared=args.shared_index)
        result = run_child("index", spec, args.timeout)
        result.update(tool="index", label="shared SourceIndex" if args.shared_index else "SourceIndex per file")
        report["runs"].append(result)
        rows.append(["index", result["label"], _s(result.get("wall_s")), _s(result.get("peak_rss_mb")),
                     f"stmts={result.get('statements')} fallback={result.get('fallback_rate')}" if not result.get("error")
                     else f"ERROR {result['error']}"])

    if "table" in tools:
        targets = manifest.get("table_targets", [])[: args.targets]
        for i, target in enumerate(targets):
            # The table tool has no database context: name the target as the SQL writes it.
            name = (target.get("table_tool_target") or target["object"]) if isinstance(target, dict) else target
            out = work / f"table-{i}"
            argv_ = ["--target", name, "--source-root", str(corpus), "--output", str(out), "--no-progress"] + args.table_arg
            result = run_child("table", spec_for("table", i, argv=argv_), args.timeout)
            result.update(tool="table", label=name)
            graph_path = out / "lineage.json"
            if graph_path.exists():
                graph = json.loads(graph_path.read_text(encoding="utf-8"))
                result["graph"] = {"nodes": len(graph.get("nodes", [])), "edges": len(graph.get("edges", [])),
                                   "unresolved": len(graph.get("unresolved", [])),
                                   "warnings": len(graph.get("warnings", []))}
                if isinstance(target, dict):
                    result["object_recall"] = table_object_recall(graph, target.get("upstream_objects", []))
            report["runs"].append(result)
            detail = (f"nodes={result.get('graph', {}).get('nodes')} edges={result.get('graph', {}).get('edges')} "
                      f"obj_recall={result.get('object_recall', {}).get('recall')}") if not result.get("error") \
                else f"ERROR {result['error']}"
            rows.append(["table", name, _s(result.get("wall_s")), _s(result.get("peak_rss_mb")),
                         f"{detail}  [{fmt_phases(result.get('phases', {}))}]"])

    if "column" in tools:
        available, why = column_tool_available()
        if not available:
            message = f"column tool skipped: {why}"
            print(message)
            report["skipped"].append(message)
        else:
            chains = manifest.get("chains", [])[: args.columns]
            maps = manifest.get("database_map_args", [])
            map_argv = ([x for m in maps for x in ("--database-map", m)] if args.map_style == "repeat"
                        else (["--database-map"] + maps if maps else []))

            def column_run(label: str, subset: list[dict], out: Path, i: int) -> dict:
                argv_ = [x for c in subset for x in ("--column", c["column"])]
                argv_ += ["--source-root", str(corpus), "--output", str(out), "--no-progress"] + map_argv + args.column_arg
                result = run_child("column", spec_for("column", i, argv=argv_), args.timeout)
                result.update(tool="column", label=label)
                diag = out / "scan-diagnostics.json"
                if diag.exists():
                    try:
                        data = json.loads(diag.read_text(encoding="utf-8"))
                        timings = data.get("timings") or data.get("phases") or data.get("timing")
                        if isinstance(timings, dict):
                            result["phases"] = {k: v for k, v in timings.items() if isinstance(v, (int, float))}
                        result["scan"] = {k: data.get(k) for k in ("files", "statements", "parsed", "fallback")
                                          if k in data}
                    except ValueError:
                        pass
                return result

            out = work / "column-batch"
            result = column_run(f"{len(chains)} chains in one run", chains, out, 0)
            report["runs"].append(result)
            rows.append(["column", result["label"], _s(result.get("wall_s")), _s(result.get("peak_rss_mb")),
                         f"rc={result.get('rc')} [{fmt_phases(result.get('phases', {}))}]"
                         + (f" ERROR {result['error']}" if result.get("error") else "")])
            for chain in chains:
                path = out / f"{stem_for(chain['column'])}.lineage.json"
                trace = None
                if path.exists():
                    try:
                        trace = json.loads(path.read_text(encoding="utf-8"))
                    except ValueError:
                        trace = None
                report["chains"].append(evaluate_chain(chain, trace))
            for i, chain in enumerate(chains[: args.per_column], start=1):
                single = column_run(chain["id"], [chain], work / f"column-{chain['id']}", i)
                report["runs"].append(single)
                rows.append(["column", f"{chain['id']} alone", _s(single.get("wall_s")), _s(single.get("peak_rss_mb")),
                             f"rc={single.get('rc')}"])

    report["summary"] = _summary(report)
    report_path = Path(args.report) if args.report else corpus / "perf-report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    stats = manifest.get("stats", {})
    print(f"corpus {corpus}: {stats.get('files')} files, {stats.get('objects')} objects "
          f"{stats.get('objects_by_kind')}, {len(manifest.get('chains', []))} chains")
    _print_table(["tool", "run", "wall_s", "rss_mb", "detail"], rows)
    if report["chains"]:
        print()
        _print_table(["chain", "layers", "exp", "reached", "prec", "rec_static", "rec_all", "status", "opaque/gaps"],
                     [[c["id"], _s(c.get("layers")), _s(c.get("expected")), _s(c.get("reached")), _s(c.get("precision")),
                       _s(c.get("recall_static")), _s(c.get("recall_all")), _s(c.get("status") or c.get("error")),
                       ",".join(c.get("opaque") or []) + (" -> " + ",".join(sorted(c.get("gaps_by_kind", {}))) if c.get("gaps_by_kind") else "")]
                      for c in report["chains"]])
        s = report["summary"].get("column", {})
        print(f"mean precision {s.get('mean_precision')}  mean static recall {s.get('mean_recall_static')}  "
              f"traceable chains fully recalled {s.get('traceable_full_recall')}/{s.get('traceable_chains')}")
    for run in report["runs"]:
        if run.get("profile_top"):
            print(f"\n--- profile: {run['tool']} {run['label']} (top {args.profile_top} by cumulative time) ---")
            print(run["profile_top"])
        if run.get("error"):
            print(f"\n--- {run['tool']} {run['label']} failed: {run['error']}")
            if run.get("stderr_tail"):
                print(run["stderr_tail"])
    print(f"\nreport: {report_path}")
    return 0 if not any(r.get("error") for r in report["runs"]) else 1


def _summary(report: dict) -> dict:
    out: dict = {}
    tables = [r for r in report["runs"] if r["tool"] == "table" and not r.get("error")]
    if tables:
        out["table"] = {"runs": len(tables), "mean_wall_s": round(sum(r["wall_s"] for r in tables) / len(tables), 3),
                        "max_peak_rss_mb": max(r.get("peak_rss_mb", 0) for r in tables)}
    chains = [c for c in report["chains"] if "precision" in c]
    if chains:
        def mean(key, items):
            values = [c[key] for c in items if c.get(key) is not None]
            return round(sum(values) / len(values), 3) if values else None
        traceable = [c for c in chains if not c["opaque"]]
        out["column"] = {"chains": len(chains), "mean_precision": mean("precision", chains),
                         "mean_recall_static": mean("recall_static", chains), "mean_recall_all": mean("recall_all", chains),
                         "traceable_chains": len(traceable),
                         "traceable_full_recall": sum(1 for c in traceable if c.get("recall_static") == 1.0),
                         "complete": sum(1 for c in chains if c.get("status") == "complete")}
    return out


def _sqlglot_version() -> str | None:
    try:
        import sqlglot
        return sqlglot.__version__
    except Exception:
        return None


def _s(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}" if value < 10 else f"{value:.1f}"
    return str(value)


def _print_table(header: list[str], rows: list[list[str]]) -> None:
    widths = [max(len(header[i]), *(len(r[i]) for r in rows)) if rows else len(header[i]) for i in range(len(header))]
    widths[-1] = len(header[-1])
    print("  ".join(h.ljust(w) for h, w in zip(header, widths)))
    for row in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip())


if __name__ == "__main__":
    sys.exit(main())
