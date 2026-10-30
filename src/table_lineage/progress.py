from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import sys
import threading
import time
from typing import Callable, TextIO


_UNSET = object()


@dataclass
class _ProgressState:
    phase: str = "starting"
    current: str | None = None
    completed: int = 0
    total: int | None = None
    ignored: int = 0
    dependencies: int = 0
    warnings: int = 0
    reason: str | None = None
    run_started: float = 0.0
    phase_started: float = 0.0
    current_started: float | None = None


@dataclass(frozen=True)
class ProgressSnapshot:
    phase: str
    current: str | None
    completed: int
    total: int | None
    ignored: int
    dependencies: int
    warnings: int
    reason: str | None
    total_elapsed: float
    current_elapsed: float | None


class ProgressReporter:
    """Thread-safe periodic progress reporting that never changes analyzer behavior."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        interval: float = 15.0,
        stream: TextIO | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
    ) -> None:
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("progress interval must be a finite number greater than zero")
        self.enabled = enabled
        self.interval = interval
        self.stream = stream if stream is not None else sys.stderr
        self._monotonic = monotonic
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        started = monotonic()
        self._state = _ProgressState(run_started=started, phase_started=started)
        self._state_lock = threading.Lock()
        self._emit_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._output_failed = False

    def __enter__(self) -> ProgressReporter:
        if self.enabled:
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                name="table-lineage-progress",
                daemon=True,
            )
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is None:
            self.finish("complete")
        elif issubclass(exc_type, KeyboardInterrupt):
            self.finish("interrupted", reason=exc_type.__name__)
        else:
            self.finish("failed", reason=exc_type.__name__)
        return False

    @property
    def thread_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def output_failed(self) -> bool:
        return self._output_failed

    def start_phase(
        self,
        phase: str,
        *,
        completed: int | None = None,
        total: int | None | object = _UNSET,
        current: str | None = None,
    ) -> None:
        now = self._monotonic()
        with self._state_lock:
            self._state.phase = phase
            self._state.phase_started = now
            self._state.current = self._clean(current) if current else None
            self._state.current_started = now if current else None
            self._state.reason = None
            if completed is not None:
                self._state.completed = max(0, completed)
            if total is not _UNSET:
                self._state.total = None if total is None else max(0, int(total))
        self.emit_now()

    def start_item(
        self,
        current: str,
        *,
        completed: int | None = None,
        total: int | None = None,
    ) -> None:
        now = self._monotonic()
        with self._state_lock:
            self._state.current = self._clean(current)
            self._state.current_started = now
            if completed is not None:
                self._state.completed = max(0, completed)
            if total is not None:
                self._state.total = max(0, total)

    def complete_item(
        self,
        completed: int,
        *,
        dependencies: int | None = None,
        warnings: int | None = None,
    ) -> None:
        with self._state_lock:
            self._state.completed = max(self._state.completed, completed)
            self._state.current = None
            self._state.current_started = None
            if dependencies is not None:
                self._state.dependencies = max(self._state.dependencies, dependencies)
            if warnings is not None:
                self._state.warnings = max(self._state.warnings, warnings)

    def update_counts(
        self,
        *,
        completed: int | None = None,
        total: int | None | object = _UNSET,
        ignored: int | None = None,
        dependencies: int | None = None,
        warnings: int | None = None,
    ) -> None:
        with self._state_lock:
            if completed is not None:
                self._state.completed = max(self._state.completed, completed)
            if total is not _UNSET:
                self._state.total = None if total is None else max(0, int(total))
            if ignored is not None:
                self._state.ignored = max(self._state.ignored, ignored)
            if dependencies is not None:
                self._state.dependencies = max(self._state.dependencies, dependencies)
            if warnings is not None:
                self._state.warnings = max(self._state.warnings, warnings)

    def snapshot(self) -> ProgressSnapshot:
        now = self._monotonic()
        with self._state_lock:
            state = self._state
            return ProgressSnapshot(
                phase=state.phase,
                current=state.current,
                completed=state.completed,
                total=state.total,
                ignored=state.ignored,
                dependencies=state.dependencies,
                warnings=state.warnings,
                reason=state.reason,
                total_elapsed=max(0.0, now - state.run_started),
                current_elapsed=(
                    max(0.0, now - state.current_started)
                    if state.current_started is not None else None
                ),
            )

    def emit_now(self) -> None:
        if not self.enabled or self._output_failed:
            return
        try:
            snapshot = self.snapshot()
            line = self.format_snapshot(snapshot, self._utcnow())
            with self._emit_lock:
                print(line, file=self.stream, flush=True)
        except Exception:
            self._output_failed = True

    def finish(self, phase: str, *, reason: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        with self._state_lock:
            self._state.phase = phase
            self._state.current = None
            self._state.current_started = None
            self._state.reason = self._clean(reason) if reason else None
        self.emit_now()

    @staticmethod
    def format_snapshot(snapshot: ProgressSnapshot, timestamp: datetime) -> str:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        stamp = timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fields = [f"[{stamp}]", f"phase={snapshot.phase}"]
        if snapshot.total is not None:
            fields.append(f"files={snapshot.completed}/{snapshot.total}")
            if snapshot.total > 0:
                fields.append(f"percent={snapshot.completed / snapshot.total * 100:.1f}")
        elif snapshot.completed:
            fields.append(f"files={snapshot.completed}/?")
        if snapshot.current:
            fields.append(f"current={json.dumps(snapshot.current, ensure_ascii=False)}")
        if snapshot.current_elapsed is not None:
            fields.append(f"current_elapsed={int(snapshot.current_elapsed)}s")
        fields.append(f"total_elapsed={int(snapshot.total_elapsed)}s")
        fields.append(f"ignored={snapshot.ignored}")
        fields.append(f"dependencies={snapshot.dependencies}")
        fields.append(f"warnings={snapshot.warnings}")
        if snapshot.reason:
            fields.append(f"reason={json.dumps(snapshot.reason, ensure_ascii=False)}")
        return " ".join(fields)

    @staticmethod
    def _clean(value: str) -> str:
        return " ".join(str(value).splitlines()).strip()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.emit_now()
