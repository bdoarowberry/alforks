from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .parser import SqlSourceParser
from .index import SourceIndex
from .definitions import DefinitionCatalog, DefinitionSource
from .statement_evidence import StatementRegistry
from .progress import ProgressReporter
from .renderers import to_json, to_mermaid, to_text
from .sources import (
    DEFAULT_EXCLUDES,
    EnvironmentSourceProvider,
    FilesystemSourceProvider,
    SourceProgressEvent,
)
from .traversal import LineageAnalyzer


def positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number greater than zero") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Find recursive upstream SQL table lineage.")
    parser.add_argument("--target", required=True)
    parser.add_argument("--source-root", nargs="+", help="Override environment-provided SQL sources")
    parser.add_argument("--output", default=str(Path(__file__).resolve().parents[2] / 'outputs'))
    parser.add_argument("--format", nargs="+", choices=("json", "mermaid", "text"), default=["json", "mermaid", "text"])
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--include", nargs="+", default=[], help="Include file or directory glob patterns")
    parser.add_argument("--exclude", nargs="+", default=[], help="Exclude file or directory glob patterns")
    parser.add_argument("--include-dated-snapshots", action="store_true",
                        help="Include date-stamped SQL snapshots that directory scans ignore by default")
    parser.add_argument("--no-progress", action="store_true",
                        help="Disable progress and heartbeat messages")
    parser.add_argument("--progress-interval", type=positive_seconds, default=15.0,
                        metavar="SECONDS", help="Seconds between heartbeat messages (default: 15)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--include-definitions", action="store_true",
                        help="Write versioned column definition metadata")
    parser.add_argument("--legacy-evidence", action="store_true",
                        help="Use the previous inline-excerpt JSON evidence format")
    return parser


def main(argv: list[str] | None = None) -> int:
    argument_parser = build_parser()
    args = argument_parser.parse_args(argv)
    if args.max_depth is not None and args.max_depth < 0:
        argument_parser.error('--max-depth must be nonnegative')
    includes = tuple(args.include)
    excludes = DEFAULT_EXCLUDES + tuple(args.exclude)
    with ProgressReporter(
        enabled=not args.no_progress,
        interval=args.progress_interval,
    ) as progress:
        progress.start_phase("discovering", completed=0, total=None)

        def update_source_progress(event: SourceProgressEvent) -> None:
            if event.kind == "discovering":
                if event.path:
                    progress.start_item(event.path, completed=event.completed)
                progress.update_counts(completed=event.completed, ignored=event.ignored)
            elif event.kind == "discovery_complete":
                progress.update_counts(ignored=event.ignored)
                progress.start_phase("reading", completed=0, total=event.total)
            elif event.kind == "reading" and event.path:
                progress.start_item(
                    event.path, completed=event.completed, total=event.total,
                )
                progress.update_counts(ignored=event.ignored)
            elif event.kind == "read_complete":
                progress.complete_item(event.completed)
                progress.update_counts(ignored=event.ignored)

        try:
            provider = (FilesystemSourceProvider(
                            args.source_root, excludes, includes,
                            include_dated_snapshots=args.include_dated_snapshots,
                            progress_callback=update_source_progress,
                        ) if args.source_root else EnvironmentSourceProvider(
                            excludes, includes,
                            include_dated_snapshots=args.include_dated_snapshots,
                            progress_callback=update_source_progress,
                        ))
            provider.preserve_newlines = not args.legacy_evidence
            source_files = list(provider.files())
            if not source_files:
                raise ValueError("No SQL source files matched the selected roots and filters.")
        except (OSError, ValueError) as exc:
            argument_parser.error(str(exc))

        parser = SqlSourceParser(definition_mode=True) if args.include_definitions else SqlSourceParser()
        dependencies = []
        definition_sources = []
        source_labels = {}
        capture_references = not args.legacy_evidence and ('json' in args.format or args.include_definitions)
        progress.start_phase("parsing", completed=0, total=len(source_files))
        for completed, source in enumerate(source_files, start=1):
            progress.start_item(
                provider.display_path(source.path),
                completed=completed - 1,
                total=len(source_files),
            )
            if args.include_definitions or capture_references:
                index = SourceIndex()
                index.add(source, include_ddl=True)
                dependencies.extend(parser.parse(source, index))
                roots = sorted((root.resolve() for root in provider.roots
                                if root.resolve() == source.path or root.resolve() in source.path.parents), key=str)
                root = roots[0] if roots else source.path.parent
                if capture_references:
                    repository = next((p for p in source.path.parents if (p / '.git').exists()), None)
                    if repository is None:
                        repository = root.parent if root.is_file() else root
                    label = repository.name
                    previous = source_labels.setdefault(label, repository)
                    if previous != repository:
                        argument_parser.error(f'Ambiguous source repository label {label!r}; choose source roots with distinct repository names.')
                    relative = source.path.relative_to(repository).as_posix()
                else:
                    label = root.name
                    relative = source.path.name if root.is_file() else source.path.relative_to(root).as_posix()
                definition_sources.append(DefinitionSource(index, relative, label, source.text))
            else:
                dependencies.extend(parser.parse(source))
            progress.complete_item(
                completed,
                dependencies=len(dependencies),
                warnings=len(parser.warnings),
            )

        progress.start_phase(
            "analyzing", completed=len(source_files), total=len(source_files),
        )
        graph = LineageAnalyzer(
            dependencies, parser.calls, parser.procedures, parser.definitions,
            parser.procedure_definitions,
        ).analyze(args.target, args.max_depth)
        graph.warnings.extend(parser.warnings)
        graph.diagnostics.extend(parser.diagnostics)
        graph.files_scanned = [str(source.path) for source in source_files]
        graph.dated_snapshot_files_ignored = len(provider.ignored_dated_snapshots)
        graph.include_dated_snapshots = args.include_dated_snapshots

        output = Path(args.output)
        if output.suffix and not output.is_dir():
            output.parent.mkdir(parents=True, exist_ok=True)
            prefix = output.with_suffix('')
        else:
            output.mkdir(parents=True, exist_ok=True)
            prefix = output / 'lineage'

        renderers = {
            "json": (".json", to_json),
            "mermaid": (".mmd", to_mermaid),
            "text": (".txt", to_text),
        }
        if capture_references and 'json' in args.format:
            graph_registry = StatementRegistry(definition_sources)
            renderers['json'] = ('.json', lambda value: json.dumps(graph_registry.graph(value.to_dict()), indent=2) + '\n')
        for output_format in args.format:
            suffix, renderer = renderers[output_format]
            output_path = Path(str(prefix) + suffix)
            progress.start_phase(
                "rendering", completed=len(source_files), total=len(source_files),
                current=output_path.name,
            )
            rendered = renderer(graph)
            progress.start_phase(
                "writing", completed=len(source_files), total=len(source_files),
                current=output_path.name,
            )
            output_path.write_text(rendered, encoding="utf-8")
            progress.complete_item(len(source_files))

        if args.include_definitions:
            progress.start_phase("analyzing", completed=len(source_files), total=len(source_files))
            definitions = DefinitionCatalog(definition_sources, graph, capture_locations=capture_references,
                                            enable_variable_lineage=not args.legacy_evidence).render()
            if capture_references:
                definitions = StatementRegistry(definition_sources).definitions(definitions)
            output_path = Path(str(prefix) + '.definitions.json')
            progress.start_phase("writing", completed=len(source_files), total=len(source_files), current=output_path.name)
            output_path.write_text(json.dumps(definitions, indent=2) + '\n', encoding='utf-8')
            progress.complete_item(len(source_files))

        if args.verbose:
            print(json.dumps({
                "files": len(source_files),
                "dated_snapshot_files_ignored": len(provider.ignored_dated_snapshots),
                "include_dated_snapshots": args.include_dated_snapshots,
                "dependencies": len(dependencies),
                "output": str(output),
            }, indent=2))
            print(to_text(graph), end='')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
