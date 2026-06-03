"""Generic cache / filesystem utilities extracted from app.py.

- `_atomic_write`: write-replace with OneDrive-aware retry on Windows.
- `_ensure_daily_backup` / `init_backup_tracking`: daily snapshots of
  user-edited config files (metadata, regions, types, geocode cache), pruned
  after 7 days.
- `LRUCache`: thread-safe bounded ordered-dict LRU used by the in-memory
  activity cache.

Kept dependency-free so it can be imported before the Flask app is built.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path


# ─── Atomic writes ────────────────────────────────────────────────────────────

# Populated by `init_backup_tracking(paths, backup_dir)` once the caller knows
# which files to snapshot. `_atomic_write` consults this set on each write.
_BACKUP_TRACKED: set[Path] = set()
_BACKUP_DIR: Path | None   = None
_BACKUP_KEEP_DAYS          = 7


def _atomic_write(path: Path, text: str) -> None:
    """Write text to path atomically via temp file + os.replace.
    Snapshots the file into the configured backup dir before overwriting if
    the path is in the tracked set.
    """
    if path in _BACKUP_TRACKED and path.exists():
        _ensure_daily_backup(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        # Retry os.replace on Windows: OneDrive's sync engine briefly opens
        # newly-created files for upload, which makes os.replace fail with
        # PermissionError. A short retry burst handles the common cases.
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.1 * (attempt + 1))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─── Daily backups ────────────────────────────────────────────────────────────

def _ensure_daily_backup(path: Path) -> None:
    """Copy `path` to <backup_dir>/<stem>.YYYY-MM-DD<suffix> if today's snapshot
    doesn't already exist. Then prune snapshots older than _BACKUP_KEEP_DAYS.
    Best-effort — never blocks writes.
    """
    if _BACKUP_DIR is None:
        return
    try:
        _BACKUP_DIR.mkdir(exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        snap = _BACKUP_DIR / f"{path.stem}.{today}{path.suffix}"
        if not snap.exists():
            snap.write_bytes(path.read_bytes())
        cutoff = datetime.now() - timedelta(days=_BACKUP_KEEP_DAYS)
        for old in _BACKUP_DIR.glob(f"{path.stem}.*{path.suffix}"):
            try:
                date_part = old.name[len(path.stem) + 1: -len(path.suffix)]
                if datetime.strptime(date_part, "%Y-%m-%d") < cutoff:
                    old.unlink()
            except (ValueError, OSError):
                continue
    except Exception:
        pass


def init_backup_tracking(paths, backup_dir: Path) -> None:
    """Register the files to snapshot and snapshot any that exist now."""
    global _BACKUP_DIR
    _BACKUP_DIR = backup_dir
    _BACKUP_TRACKED.update(paths)
    for p in _BACKUP_TRACKED:
        if p.exists():
            _ensure_daily_backup(p)


# ─── Thread-safe LRU cache ────────────────────────────────────────────────────

class LRUCache:
    """Thread-safe LRU cache with a bounded size. Values are arbitrary dicts."""

    def __init__(self, maxsize: int = 400):
        self._data: OrderedDict[str, dict] = OrderedDict()
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def get(self, key: str) -> dict | None:
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return self._data[key]

    def set(self, key: str, value: dict) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = value
            if len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def evict(self, key: str) -> None:
        """Drop a key if present. Silent no-op when missing."""
        with self._lock:
            self._data.pop(key, None)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._data
