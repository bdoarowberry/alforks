"""Per-file sidebar entry cache.

Each `_build_activity_entry` result is persisted to disk with a fingerprint
of its inputs. On cold start (or after syncing a single new file) we serve
each entry directly when the fingerprint still matches — turning the
historical ~40 s rebuild of all sidebar rows into a per-file stat plus
recompute of only the stale rows.

Kept dependency-free of Flask / app-level state so tests can import it
without triggering app.py's autosync side effects.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cache_utils import _atomic_write


def stat_mtime(p: Path | None) -> float:
    if p is None:
        return -1.0
    try: return p.stat().st_mtime
    except OSError: return -1.0


def hr_file_mtime(hr_cache_dir: Path, date_str: str) -> float:
    """mtime of the HR cache JSON for `date_str`, or -1 if absent.

    The fingerprint deliberately doesn't include HR mtime because the
    activity's date isn't known before the entry is built; instead this is
    stashed alongside the cached entry and re-stat'd on read.
    """
    if not date_str:
        return -1.0
    return stat_mtime(hr_cache_dir / f"{date_str}.json")


# Bump when the SHAPE of a cached entry changes (a new baked stat field, a
# renamed key, etc.) so every entry re-bakes even though its inputs (gpx mtime,
# meta, regions) are unchanged. Distinct from `algo_sig`, which tracks
# detection-algorithm *output* changes — a schema change is not an algo change.
#   1: baseline (hr_avg/hr_max baked)
#   2: + hr_zones baked into stats (saves a per-ride HR re-merge in fitness rollups)
ENTRY_SCHEMA_VERSION = 2


def sidebar_fingerprint(*, gpx_mtime: float, file_meta: dict,
                        regions_mtime: float, types_mtime: float,
                        algo_sig: str, region_match_version: int) -> str:
    """Hash of every input that can change a sidebar entry's contents."""
    payload = json.dumps({
        "gpx_mtime":     round(gpx_mtime,     3),
        "regions_mtime": round(regions_mtime, 3),
        "types_mtime":   round(types_mtime,   3),
        "meta":          file_meta or {},
        "algo_sig":      algo_sig,
        "region_match_version": region_match_version,
        "entry_schema":  ENTRY_SCHEMA_VERSION,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


def _entry_path(sidebar_cache_dir: Path, filename: str) -> Path:
    return sidebar_cache_dir / f"{filename}.json"


def _entry_date(entry: dict) -> str:
    """The entry's `YYYY-MM-DD` date prefix (the HR-cache lookup key), or ""."""
    return (entry.get("date") or "")[:10]


def read_sidebar_entry(*, sidebar_cache_dir: Path, hr_cache_dir: Path,
                       filename: str, expected_fp: str
                       ) -> tuple[dict, dict] | None:
    """Return (entry, aux) on fingerprint + HR-mtime match, else None.
    Tolerant of missing / corrupt files."""
    p = _entry_path(sidebar_cache_dir, filename)
    if not p.exists():
        return None
    # Tolerant of corrupt/malformed entries: the guard must cover not just the
    # json parse but the post-parse field access too — a structurally-valid blob
    # with a wrong-typed `date` or `start_latlon` would otherwise raise
    # (TypeError) past the json.loads guard and 500 the whole sidebar render.
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
        if blob.get("fp") != expected_fp:
            return None
        entry = blob.get("entry")
        if not isinstance(entry, dict):
            return None
        date_str = _entry_date(entry)
        if hr_file_mtime(hr_cache_dir, date_str) != blob.get("hr_mtime", -1.0):
            return None
        sl = entry.get("start_latlon")
        aux = {"_start_latlon": tuple(sl)} if sl else {}
        return entry, aux
    except (OSError, ValueError, TypeError):
        return None


def write_sidebar_entry(*, sidebar_cache_dir: Path, hr_cache_dir: Path,
                        filename: str, entry: dict, fp: str) -> None:
    """Persist an entry. Best-effort — a write failure shouldn't break a
    page render; worst case is we recompute next time."""
    try:
        sidebar_cache_dir.mkdir(parents=True, exist_ok=True)
        date_str = _entry_date(entry)
        blob = {"fp": fp,
                "hr_mtime": hr_file_mtime(hr_cache_dir, date_str),
                "entry": entry}
        _atomic_write(_entry_path(sidebar_cache_dir, filename),
                      json.dumps(blob, ensure_ascii=False))
    except (OSError, ValueError):
        pass


def delete_sidebar_entry(sidebar_cache_dir: Path, filename: str) -> None:
    try:
        _entry_path(sidebar_cache_dir, filename).unlink(missing_ok=True)
    except OSError:
        pass
