from __future__ import annotations

import json
import os
import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .path_filters import is_dated_snapshot

SUPPORTED_SUFFIXES = {".sql", ".tsql", ".ddl", ".prc", ".proc"}
DEFAULT_EXCLUDES = (".git", ".venv", ".pytest_cache", ".pytest-run-*", "node_modules", "bin", "obj")


@dataclass(frozen=True)
class SourceFile:
    path: Path
    text: str


@dataclass(frozen=True)
class SourceProgressEvent:
    kind: str
    path: str | None = None
    completed: int = 0
    total: int | None = None
    ignored: int = 0


class SourceProvider:
    def files(self) -> Iterable[SourceFile]:
        raise NotImplementedError


class FilesystemSourceProvider(SourceProvider):
    def __init__(self, roots: list[str], excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
                 includes: tuple[str, ...] = (), include_dated_snapshots: bool = False,
                 progress_callback: Callable[[SourceProgressEvent], None] | None = None) -> None:
        self.roots = [Path(root) for root in roots]
        self.excludes = excludes
        self.includes = includes
        self.include_dated_snapshots = include_dated_snapshots
        self.progress_callback = progress_callback
        self.preserve_newlines = False
        self.ignored_dated_snapshots: list[Path] = []
        self._display_paths: dict[str, str] = {}

    @staticmethod
    def _matches(path: Path, root: Path, patterns: tuple[str, ...]) -> bool:
        relative = path.name if root.is_file() else path.relative_to(root).as_posix()
        return any(fnmatch.fnmatchcase(relative.lower(), pattern.lower()) or
                   any(fnmatch.fnmatchcase(part.lower(), pattern.lower()) for part in Path(relative).parts)
                   for pattern in patterns)

    @staticmethod
    def _is_snapshot_filtered_source_path(path: Path) -> bool:
        return any(part.lower() in ("sql_root_a", "sql_root_b") for part in path.parts)

    @staticmethod
    def _display_path(path: Path, root: Path) -> str:
        if root.is_file():
            return path.name
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            return path.name
        if root.name.lower() in ("sql_root_a", "sql_root_b"):
            return f"{root.name}/{relative}"
        return relative

    def display_path(self, path: Path) -> str:
        key = os.path.normcase(str(path.resolve()))
        return self._display_paths.get(key, path.name)

    def _notify(self, event: SourceProgressEvent) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event)
        except Exception:
            self.progress_callback = None

    def files(self) -> Iterable[SourceFile]:
        paths: dict[str, Path] = {}
        ignored: dict[str, Path] = {}
        explicit: set[str] = set()
        self.ignored_dated_snapshots = []
        self._display_paths = {}
        for root in self.roots:
            root = root.resolve()
            if root.is_file():
                candidates = [root]
                direct_file = True
            elif root.is_dir():
                direct_file = False
                def walk() -> Iterable[Path]:
                    def raise_error(error: OSError) -> None:
                        raise error
                    for directory, subdirs, names in os.walk(root, onerror=raise_error):
                        base = Path(directory)
                        subdirs[:] = sorted(name for name in subdirs
                                            if not self._matches(base / name, root, self.excludes))
                        for name in sorted(names):
                            yield base / name
                candidates = walk()
            else:
                raise FileNotFoundError(f"SQL source root does not exist: {root}")
            for path in candidates:
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                if self._matches(path, root, self.excludes):
                    continue
                if self.includes and not self._matches(path, root, self.includes):
                    continue
                resolved = path.resolve()
                key = os.path.normcase(str(resolved))
                display = self._display_path(resolved, root)
                if direct_file:
                    explicit.add(key)
                    ignored.pop(key, None)
                    paths[key] = resolved
                    self._display_paths[key] = display
                    self._notify(SourceProgressEvent(
                        "discovering", display, len(paths), ignored=len(ignored)))
                    continue
                if key in explicit:
                    paths[key] = resolved
                    continue
                if (not self.include_dated_snapshots and self._is_snapshot_filtered_source_path(resolved)
                        and is_dated_snapshot(resolved)):
                    ignored[key] = resolved
                    paths.pop(key, None)
                    self._display_paths.pop(key, None)
                    self._notify(SourceProgressEvent(
                        "discovering", display, len(paths), ignored=len(ignored)))
                    continue
                paths[key] = resolved
                self._display_paths[key] = display
                self._notify(SourceProgressEvent(
                    "discovering", display, len(paths), ignored=len(ignored)))
        self.ignored_dated_snapshots = [ignored[key] for key in sorted(ignored) if key not in paths]
        self._notify(SourceProgressEvent(
            "discovery_complete", completed=0, total=len(paths),
            ignored=len(self.ignored_dated_snapshots)))
        for completed, key in enumerate(sorted(paths), start=1):
            path = paths[key]
            display = self._display_paths.get(key, path.name)
            self._notify(SourceProgressEvent(
                "reading", display, completed - 1, len(paths),
                len(self.ignored_dated_snapshots)))
            if self.preserve_newlines:
                with path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
                    text = stream.read()
            else:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            self._notify(SourceProgressEvent(
                "read_complete", display, completed, len(paths),
                len(self.ignored_dated_snapshots)))
            yield SourceFile(path, text)


class EnvironmentSourceProvider(FilesystemSourceProvider):
    """Default provider boundary for agent/skill-managed sources.

    A standalone Python process cannot directly inspect agent skills. This adapter
    defaults to the containing repository and accepts roots exported by the host.
    The host can set
    LINEAGE_SOURCE_ROOTS to an OS-separated list or LINEAGE_SOURCE_MANIFEST to a
    JSON file containing {"roots": ["..."]}.
    """

    def __init__(self, excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
                 includes: tuple[str, ...] = (), include_dated_snapshots: bool = False,
                 progress_callback: Callable[[SourceProgressEvent], None] | None = None) -> None:
        roots: list[str] = []
        if os.getenv("LINEAGE_SOURCE_ROOTS"):
            roots = os.environ["LINEAGE_SOURCE_ROOTS"].split(os.pathsep)
        manifest = os.getenv("LINEAGE_SOURCE_MANIFEST")
        if manifest:
            data = json.loads(Path(manifest).read_text(encoding="utf-8"))
            roots.extend(str((Path(manifest).resolve().parent / root).resolve()) for root in data.get("roots", []))
        if not roots:
            project_root = Path(__file__).resolve().parents[2]
            repo_root = next((parent for parent in project_root.parents if (parent / ".git").exists()), project_root)
            roots = [str(repo_root)]
        super().__init__(roots, excludes, includes, include_dated_snapshots, progress_callback)
