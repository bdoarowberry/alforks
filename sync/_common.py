"""Helpers shared by the standalone sync CLIs (strava_sync.py and
garmin_sync.py).

Both run as `python sync/<name>.py` subprocesses (and the tests insert
sync/ onto sys.path), so `sync/` is always on the path and a plain
`import _common` resolves to this module in every execution context.

Only genuinely identical helpers live here. The credential parsers and
status-file persistence deliberately stay per-module — they differ in
required keys / merge semantics and run on a network path that isn't
covered by the offline test suite, so unifying them would be risk without
a safety net.
"""
from __future__ import annotations

from pathlib import Path


def secure_chmod(path: Path, mode: int) -> None:
    """Best-effort permission hardening. Posix honours the mode; Windows
    only toggles the read-only bit and relies on ACLs for access control."""
    try:
        path.chmod(mode)
    except OSError:
        pass
