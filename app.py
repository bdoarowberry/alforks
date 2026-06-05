"""Flask backend for GPX activity viewer."""

import gzip
import hashlib
import json
import logging
import math
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

import gpxpy
from flask import Flask, Response, abort, jsonify, redirect, render_template, request

from cache_utils import LRUCache, _atomic_write, init_backup_tracking
import osm_breaker
import trail_match
import route_builder
import route_attempts
import route_suggestions
import secrets
from geo import point_in_polygon as _point_in_polygon
from detection import (
    ALGO_SIG,
    DETECTION_ALGORITHMS,
    _algo_lift,
    _algo_mtb,
    _build_segments,
    _compute_algo_stats,
    _detect_assisted,
    _median_filter,
    _merge_stats,
    _per_pt_from_points,
    haversine,
)

# Module-level logger. Configured in __main__; tests / imports get a default
# null handler so unconfigured emits don't print warnings to stderr.
logger = logging.getLogger("alforks")
logger.addHandler(logging.NullHandler())

_ROOT         = Path(__file__).parent
GPX_DIR       = _ROOT / "tracks"
CACHE_DIR     = _ROOT / "cache"
METADATA_FILE = _ROOT / "metadata.json"
CONFIG_FILE   = _ROOT / "config.json"
REGIONS_FILE  = _ROOT / "regions.json"
TYPES_FILE    = _ROOT / "types.json"
DUP_DISMISSALS_FILE = _ROOT / "dup_dismissals.json"
WEIGHTS_FILE  = _ROOT / "weights.json"

_DEFAULT_TYPES = [
    {"id": "mtb",       "label": "Mountain Bike", "glyph": "MTB", "color": "#4ade80", "bg": "#1a3a2a"},
    {"id": "snowboard", "label": "Snowboard",     "glyph": "SNO", "color": "#60a5fa", "bg": "#1a2a4a"},
    {"id": "ski",       "label": "Ski",           "glyph": "SKI", "color": "#a78bfa", "bg": "#2a1a4a"},
    {"id": "hike",      "label": "Hike",          "glyph": "HIK", "color": "#fb923c", "bg": "#3a2a1a"},
    {"id": "other",     "label": "Unknown",       "glyph": "UNK", "color": "#9ca3af", "bg": "#2a2a2a"},
]


_types_cache: list | None = None
_types_lock = threading.Lock()

# GPX producers known to emit wall-clock-local time tagged with a fake +00:00
# offset rather than genuine UTC. TrailForks-direct exports use this; our
# `sync/strava_sync.py` deliberately matches the convention so HR-merge math
# stays consistent (see the docstring at the top of strava_sync.py). Used in
# `parse_gpx` to decide whether a +00:00 timestamp should be `replace`d
# (offset stripped, re-anchored at the activity zone) or honestly converted.
_FAKE_UTC_CREATORS = ("trailforks", "alforks strava sync")


def _safe_json(obj) -> str:
    """`json.dumps` for values that get injected raw into a `<script>` block
    via `{{ x | safe }}`. Standard `json.dumps` doesn't escape the literal
    `</script>` sequence, so a user-supplied string like a type or region
    name containing it would break out of the script element. Escaping the
    forward slash neutralizes that without affecting JSON correctness."""
    return json.dumps(obj).replace("</", "<\\/")


# Fields stripped from each region for the "lite" payload used by pages that
# only need metadata (chip rendering, dropdowns, lookups by id). `geometry`
# is the giant polygon array (3.5+ MB across all regions); other lite-omitted
# fields are similarly only useful inside the Setup map editor.
_REGION_HEAVY_FIELDS = ("geometry", "source")


def _regions_lite(regions: list[dict]) -> list[dict]:
    """Strip geometry + other map-only fields from each region. Cuts the
    inlined `const REGIONS = ...` payload on /logs and /routes from
    ~1.27 MB to ~5 KB while still carrying id/name/color/default_type/
    winter_default_type/winter_months/assisted — everything the chip
    rendering and region dropdowns need."""
    return [
        {k: v for k, v in r.items() if k not in _REGION_HEAVY_FIELDS}
        for r in regions
    ]


def load_types() -> list[dict]:
    global _types_cache
    if _types_cache is not None:
        return _types_cache
    with _types_lock:
        if _types_cache is not None:
            return _types_cache
        if TYPES_FILE.exists():
            try:
                _types_cache = json.loads(TYPES_FILE.read_text(encoding="utf-8"))
                return _types_cache
            except Exception:
                pass
        _types_cache = [t.copy() for t in _DEFAULT_TYPES]
        return _types_cache


def save_types(types: list[dict]):
    global _types_cache
    _atomic_write(TYPES_FILE, json.dumps(types, indent=2, ensure_ascii=False))
    with _types_lock:
        _types_cache = types
    # TYPES_FILE mtime is part of _activities_cache_key; bust the throttled
    # key cache so the next request re-stats eagerly after a type change.
    _invalidate_acts_key_cache()

GPX_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)
for _sub in ("gpx", "lifts", "weather", "hr"):
    (CACHE_DIR / _sub).mkdir(exist_ok=True)


def _migrate_cache_layout() -> None:
    """One-time migration: cache files used to sit flat in cache/. Move any
    legacy ones into the per-type subdirectories. Idempotent — re-running
    after migration is a no-op."""
    moved = 0
    try:
        for p in CACHE_DIR.iterdir():
            if not p.is_file():
                continue
            name = p.name
            if name == "geocode.json":
                continue
            if name.startswith("lifts_") and name.endswith(".json"):
                dest = CACHE_DIR / "lifts" / name[len("lifts_"):]
            elif name.startswith("weather_") and name.endswith(".json"):
                dest = CACHE_DIR / "weather" / name[len("weather_"):]
            elif name.endswith(".gpx.json"):
                dest = CACHE_DIR / "gpx" / name
            else:
                continue
            if dest.exists():
                try: p.unlink()
                except OSError: pass
            else:
                try: p.rename(dest); moved += 1
                except OSError: pass
    except Exception:
        pass
    if moved:
        logger.info("Cache layout migrated: %d file(s) moved to subdirectories", moved)


_migrate_cache_layout()

app = Flask(__name__)
# Always auto-reload templates so iterating on `templates/*.html` doesn't
# need a Flask restart. Independent of debug mode (which still gates the
# Python reloader and traceback verbosity).
app.config["TEMPLATES_AUTO_RELOAD"] = True


# ─── Response compression ─────────────────────────────────────────────────────
# Gzip JSON / HTML / JS / CSS responses above 1 KB when the client sends
# Accept-Encoding: gzip. Halves /api/activities (~500 KB → ~80 KB on a typical
# library) and any text payload above the threshold. Skips streaming responses
# (SSE), already-encoded payloads, and 3xx/4xx/5xx statuses. Stdlib only —
# no flask-compress dep.

_GZIP_MIN_BYTES = 1024
_GZIP_TYPES = ("application/json", "text/html", "text/css",
               "text/plain", "application/javascript", "text/javascript")


@app.after_request
def _gzip_response(response):
    if response.status_code < 200 or response.status_code >= 300:
        return response
    ctype = response.headers.get("Content-Type", "")
    if not any(ctype.startswith(t) for t in _GZIP_TYPES):
        return response
    # Compressible content type — declare Vary so caches partition responses
    # by Accept-Encoding, even on the small responses we leave untouched.
    response.headers["Vary"] = "Accept-Encoding"
    if "gzip" not in request.headers.get("Accept-Encoding", "").lower():
        return response
    if response.direct_passthrough or response.is_streamed:
        # send_file-style responses and SSE generators — leave alone
        return response
    if response.headers.get("Content-Encoding"):
        return response
    data = response.get_data()
    if len(data) < _GZIP_MIN_BYTES:
        return response
    response.set_data(gzip.compress(data, compresslevel=6))
    response.headers["Content-Encoding"] = "gzip"
    return response


# ─── Static asset caching ─────────────────────────────────────────────────────
# Flask's default for /static/ is `Cache-Control: no-cache`, which forces a
# revalidation round-trip per asset per page load. Combined with the
# template `?v={{ asset_v(...) }}` query-string buster, we can promote
# static assets to year-long immutable caching: the browser stops re-asking
# until the URL itself changes (which happens whenever the file mtime
# changes). Without ?v= we leave the conservative no-cache so iterating
# on assets locally doesn't need a Ctrl+F5.

_STATIC_MAX_AGE_SEC = 31_536_000   # 1 year


@app.after_request
def _static_cache_headers(response):
    if not request.path.startswith("/static/"):
        return response
    if not request.args.get("v"):
        # Missing or empty ?v= — could be a stale URL or asset_v() falling
        # back when the file is gone. Keep Flask's safe no-cache default so
        # we don't pin a 404 in the browser cache for a year.
        return response
    response.headers["Cache-Control"] = f"public, max-age={_STATIC_MAX_AGE_SEC}, immutable"
    return response


# Asset versioning helper — exposed to Jinja as `asset_v(filename)`.
# Returns the file's mtime as an integer; templates append it as ?v=... so
# any edit busts the cache instantly. Cached per process: stat once, reuse
# until restart (and Flask is restarted any time app.py changes anyway).

_asset_version_cache: dict[str, str] = {}
_asset_version_lock                  = threading.Lock()


def _asset_version(filename: str) -> str:
    """Returns mtime-based version string for a /static file path.
    Filename is relative to the static directory (e.g. 'base.css').
    Returns '' if the file is missing — caller should still emit the URL."""
    with _asset_version_lock:
        if filename in _asset_version_cache:
            return _asset_version_cache[filename]
    path = Path(app.static_folder) / filename
    try:
        v = str(int(path.stat().st_mtime))
    except (FileNotFoundError, OSError):
        return ""
    with _asset_version_lock:
        _asset_version_cache[filename] = v
    return v


@app.context_processor
def _inject_asset_helper():
    return {"asset_v": _asset_version}


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    cfg: dict = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Environment variable takes precedence over config file
    if token := os.environ.get("MAPBOX_TOKEN"):
        cfg["mapbox_token"] = token
    return cfg


def save_config(cfg: dict):
    _atomic_write(CONFIG_FILE, json.dumps(cfg, indent=2, ensure_ascii=False))


# ─── Backup setup (atomic writes + LRU live in cache_utils) ───────────────────
BACKUP_DIR = _ROOT / "backups"


def _init_backup_tracking():
    """Register tracked files and snapshot any that exist on startup."""
    init_backup_tracking(
        {METADATA_FILE, REGIONS_FILE, TYPES_FILE, _GEOCODE_CACHE_FILE},
        BACKUP_DIR,
    )


# ─── OSM lift fetching (detection algorithms live in detection.py) ───────────

_LIFT_CACHE_TTL_SEC = 30 * 24 * 3600   # 30 days
_AERIALWAY_TYPES = "gondola|chair_lift|cable_car|mixed_lift|drag_lift|t-bar|j-bar|platter|rope_tow|zip_line"


# ─── OSM Overpass ─────────────────────────────────────────────────────────────

_osm_locks:    dict[str, threading.Lock] = {}
_osm_locks_mu = threading.Lock()

# In-memory mirror of `_lift_cache_path` files. Populated lazily on read
# (and pre-warmed once before an ALGO_SIG-bump storm via
# `_prewarm_disk_caches`). 554 GPX files in the same mountain area resolve
# to the same bbox and therefore the same cache file — without this
# every worker thread re-reads the same JSON. Map: file stem (md5) -> lifts.
_OSM_LIFT_MEM_CACHE: dict[str, list[dict]] = {}


def _osm_lock_for(cp: Path) -> threading.Lock:
    key = cp.name
    with _osm_locks_mu:
        if key not in _osm_locks:
            _osm_locks[key] = threading.Lock()
        return _osm_locks[key]


def _lift_cache_path(bbox) -> Path:
    s, w, n, e = bbox
    key = f"{round(s,2)},{round(w,2)},{round(n,2)},{round(e,2)}"
    h = hashlib.md5(key.encode()).hexdigest()[:12]
    return CACHE_DIR / "lifts" / f"{h}.json"


def _try_read_osm_cache(cp: Path) -> list[dict] | None:
    # Memory hit short-circuits the disk read. The mem cache is dict-thread-
    # safe under the GIL for simple get/set; we don't lock because the
    # cache value is immutable (a list we hand out by reference) and a
    # benign race that double-loads the same file is harmless.
    mem = _OSM_LIFT_MEM_CACHE.get(cp.stem)
    if mem is not None:
        return mem
    if not cp.exists():
        return None
    try:
        entry = json.loads(cp.read_text(encoding="utf-8"))
        if time.time() - entry.get("fetched", 0) < _LIFT_CACHE_TTL_SEC:
            lifts = entry["lifts"]
            _OSM_LIFT_MEM_CACHE[cp.stem] = lifts
            return lifts
    except Exception:
        pass
    return None


def _read_osm_cache_stale(cp: Path) -> list[dict] | None:
    """Read the lifts cache ignoring TTL. Used as a fallback when Overpass
    is unreachable — stale data is correct OSM data, just old, and ski-lift
    geometry barely changes. Returns None only when the file is missing or
    corrupt."""
    if not cp.exists():
        return None
    try:
        entry = json.loads(cp.read_text(encoding="utf-8"))
        return entry.get("lifts")
    except Exception:
        return None


def _fetch_osm_lifts(bbox) -> list[dict]:
    """Return aerialway segments for bbox, cached to disk for 30 days.
    Per-bbox locking prevents duplicate Overpass requests during parallel prewarm.

    When Overpass is unreachable (timeout, breaker open, network error),
    falls back to the on-disk cache ignoring TTL before returning [].
    """
    cp = _lift_cache_path(bbox)

    # Fast path — no lock needed for a cache hit
    cached = _try_read_osm_cache(cp)
    if cached is not None:
        return cached

    with _osm_lock_for(cp):
        # Re-check inside lock — another thread may have fetched while we waited
        cached = _try_read_osm_cache(cp)
        if cached is not None:
            return cached

        # Breaker open: don't even try Overpass. Serve stale cache if we have
        # one for this bbox. New bboxes seen during an outage still return [].
        if osm_breaker.should_skip():
            stale = _read_osm_cache_stale(cp)
            return stale if stale is not None else []

        s, w, n, e = bbox
        pad = 0.01
        query = (
            f"[out:json][timeout:20];"
            f"(way({s-pad},{w-pad},{n+pad},{e+pad})"
            f'[aerialway~"{_AERIALWAY_TYPES}"];);'
            f"out geom;"
        )
        try:
            data = urllib.parse.urlencode({"data": query}).encode()
            req  = urllib.request.Request(
                "https://overpass-api.de/api/interpreter",
                data=data,
                headers={"User-Agent": "AlanForks-GPX-Viewer/1.0"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            lifts = []
            for el in result.get("elements", []):
                geom = el.get("geometry", [])
                if len(geom) < 2:
                    continue
                a = (geom[0]["lat"],  geom[0]["lon"])
                b = (geom[-1]["lat"], geom[-1]["lon"])
                lifts.append({"name": el.get("tags", {}).get("name", ""), "a": a, "b": b})

            _atomic_write(cp, json.dumps({"fetched": time.time(), "lifts": lifts}, ensure_ascii=False))
            _OSM_LIFT_MEM_CACHE[cp.stem] = lifts
            osm_breaker.record_success()
            return lifts

        except Exception:
            osm_breaker.record_failure()
            stale = _read_osm_cache_stale(cp)
            return stale if stale is not None else []


# ─── Metadata ─────────────────────────────────────────────────────────────────

_metadata_cache: dict | None = None
# Reentrant so route handlers can hold the lock across a read-mutate-
# write block while `save_metadata()` (which itself acquires the lock)
# runs inside the same thread.
_metadata_lock  = threading.RLock()
# Set when metadata.json failed to parse on load. Reads fall back to {} so
# the sidebar doesn't 500, but writes are refused — otherwise the first PATCH
# would silently overwrite the corrupt file with an empty dict and destroy
# everything. Manual repair / restore from backups/ is required to clear this.
_metadata_corrupt = False


def load_metadata() -> dict:
    global _metadata_cache, _metadata_corrupt
    if _metadata_cache is not None:
        return _metadata_cache
    with _metadata_lock:
        if _metadata_cache is not None:
            return _metadata_cache
        if METADATA_FILE.exists():
            try:
                _metadata_cache = json.loads(METADATA_FILE.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(
                    "Failed to parse %s: %s — reads will use empty dict; "
                    "writes will be refused until repaired",
                    METADATA_FILE.name, e,
                )
                _metadata_cache = {}
                _metadata_corrupt = True
        else:
            _metadata_cache = {}
        return _metadata_cache


def save_metadata(meta: dict, changed_filenames: list[str] | None = None):
    """Persist metadata and refresh derived caches.

    If `changed_filenames` is provided, we surgically refresh just those
    entries — avoids a ~40 s rebuild of the 669-activity sidebar cache on
    every single-activity edit (title, type, trim, smoothing, exclude, etc.).
    Pass None only when the caller can't enumerate what changed (e.g. a
    migration that rewrote the whole file).
    """
    global _metadata_cache
    if _metadata_corrupt:
        raise RuntimeError(
            f"Refusing to write {METADATA_FILE.name}: file was corrupt on load. "
            f"Restore from backups/ or fix the file manually, then restart."
        )
    _atomic_write(METADATA_FILE, json.dumps(meta, indent=2, ensure_ascii=False))
    with _metadata_lock:
        _metadata_cache = meta
    # METADATA_FILE mtime changed — bust the throttled key cache so the next
    # _activities_cache_key() call re-stats eagerly regardless of TTL.
    _invalidate_acts_key_cache()
    if changed_filenames is None:
        _invalidate_activities_cache()
        _invalidate_mtb_cache()
    else:
        # MTB re-segmentation is keyed on (filename, GPX mtime) — independent
        # of metadata. Only invalidate it when the original GPX changed, which
        # isn't a metadata-edit scenario. Leaving it alone avoids re-running
        # _algo_mtb on every title/notes tweak for MTB-tagged activities.
        for fn in changed_filenames:
            _update_activity_entry(fn)
        # Summary cache includes titles and exclude flags from metadata;
        # must bust it even on surgical updates.
        with _summary_data_lock:
            _summary_data_cache = None
            _summary_data_key   = None


# ─── Activities list cache ─────────────────────────────────────────────────────
# Cached result of all_activities() — invalidated when the GPX directory changes
# (new file added/removed) or when metadata is saved.

_activities_cache:            list | None  = None
_activities_cache_dir_mtime: tuple | None  = None
_activities_cache_lock                     = threading.Lock()

# ─── Summary data cache ────────────────────────────────────────────────────────
# _summary_data() is O(n_activities) in Python and is called on every page load.
# Cache the result keyed on (days_back, units, today_iso, activities_cache_key)
# so that repeated requests within the same day — and across the rolling window
# — are served from memory without re-iterating 800+ activities.
# Invalidated explicitly whenever the activities cache is invalidated.

_summary_data_cache: dict | None  = None
_summary_data_key:   tuple | None = None
_summary_data_lock                = threading.Lock()


def _invalidate_activities_cache():
    global _activities_cache, _summary_data_cache, _summary_data_key
    with _activities_cache_lock:
        _activities_cache = None
    with _summary_data_lock:
        _summary_data_cache = None
        _summary_data_key   = None
    _invalidate_acts_key_cache()


# Throttled cache-key computation.
#
# _activities_cache_key() touches 5+ filesystem paths on every call
# (GPX dir, metadata, regions, types, plus a 443-file HR directory scan).
# On a OneDrive-backed path each stat() is a network round-trip, adding
# 200-400 ms to every request that calls all_activities().
#
# We throttle the full key computation to once per _ACTS_KEY_TTL seconds.
# The summary cache then uses the stored key directly, so the fast path
# has zero file I/O: just a monotonic clock read and a tuple comparison.
#
# Safety: a write that bumps any of the tracked files also calls
# _invalidate_activities_cache() (or save_metadata / save_types /
# save_regions), which clears _acts_key_cache immediately — so the next
# request after a real change always sees the fresh key within one TTL
# worth of lag at most.  For the HR scan (sync path only), the TTL is
# fine because the sidebar build is much slower than _ACTS_KEY_TTL anyway.

_acts_key_cache: tuple | None = None
_acts_key_time:  float        = 0.0
_acts_key_lock               = threading.Lock()
_ACTS_KEY_TTL                = 30.0  # seconds — single-user local server; write paths clear eagerly


def _invalidate_acts_key_cache() -> None:
    """Called by any write path so the next request re-stats eagerly."""
    global _acts_key_cache
    with _acts_key_lock:
        _acts_key_cache = None


def _activities_cache_key() -> tuple:
    """Cache key combining GPX dir + metadata/regions/types mtimes, plus a
    count of HR cache files. Previously used the HR dir's mtime, but on Windows
    that bumps every time any HR file is added/removed — so every Garmin sync
    invalidated the entire sidebar cache. The file count only changes when a
    new date appears, and the per-activity `has_hr` flag is re-stat'd anyway.

    The full key is throttled: stat() calls on OneDrive paths are
    network-latency I/O (~200-400 ms total for 4 files + 443-file HR scan).
    We recompute at most once per _ACTS_KEY_TTL seconds and return the cached
    value otherwise.  Write paths call _invalidate_acts_key_cache() to force
    an immediate refresh on the next read.
    """
    global _acts_key_cache, _acts_key_time
    now = time.monotonic()
    with _acts_key_lock:
        if _acts_key_cache is not None and now - _acts_key_time < _ACTS_KEY_TTL:
            return _acts_key_cache
        key = _activities_cache_key_compute()
        _acts_key_cache = key
        _acts_key_time  = now
        return key


def _stat_mtime(p) -> float:
    """File/dir mtime, or -1.0 when it can't be stat'd (missing /
    racing delete). Used to build cache keys that change when inputs do."""
    try:
        return p.stat().st_mtime
    except OSError:
        return -1.0


def _activities_cache_key_compute() -> tuple:
    """Unconditional key computation — stat every tracked path."""
    hr_dir = CACHE_DIR / "hr"
    hr_count = 0
    hr_max_mtime = 0.0
    try:
        for p in hr_dir.glob("*.json"):
            hr_count += 1
            try:
                mt = p.stat().st_mtime
                if mt > hr_max_mtime:
                    hr_max_mtime = mt
            except OSError:
                pass
    except OSError:
        pass
    # _REGION_MATCH_VERSION is included so a bump to the region-matching
    # algorithm forces every cached sidebar row to recompute its `regions`
    # field on the next load — without it the cache would happily serve
    # the old centroid-matched answers until the user touched a file.
    # hr_max_mtime invalidates when an existing HR file is overwritten by
    # an incomplete-date re-fetch; previously only count changes were caught.
    return (_stat_mtime(GPX_DIR), _stat_mtime(METADATA_FILE), _stat_mtime(REGIONS_FILE), _stat_mtime(TYPES_FILE),
            hr_count, hr_max_mtime, _REGION_MATCH_VERSION)


# ─── HR coverage check ────────────────────────────────────────────────────────
# Tolerance applied at both ends of the activity window. Garmin's daily-wellness
# HR is sampled every ~2 minutes; activities recorded right at the edge of a
# sample window would otherwise look uncovered even though HR is effectively
# present.
_HR_COVERAGE_TOL_MS = 5 * 60 * 1000

# date_str -> (file mtime, (first_ms, last_ms, count) | None).
# Mutated from `_hr_sample_range`, which is now called from worker threads
# inside `all_activities`'s parallel build loop. Single-key dict mutation
# is GIL-safe but the read-then-write pattern below is not, so we guard
# all touches with `_hr_range_cache_lock`.
_hr_range_cache: dict[str, tuple[float, tuple[int, int, int] | None]] = {}
_hr_range_cache_lock = threading.Lock()


def _hr_sample_range(date_str: str) -> tuple[int, int, int] | None:
    """First / last sample timestamp (utc ms) and count for a cached HR date,
    or None when the cache is absent / unreadable / empty. Memoized by mtime."""
    if not date_str:
        return None
    p = CACHE_DIR / "hr" / f"{date_str}.json"
    try:
        mtime = p.stat().st_mtime
    except OSError:
        with _hr_range_cache_lock:
            _hr_range_cache.pop(date_str, None)
        return None
    with _hr_range_cache_lock:
        cached = _hr_range_cache.get(date_str)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        samples = payload.get("samples") or []
    except (OSError, ValueError):
        with _hr_range_cache_lock:
            _hr_range_cache[date_str] = (mtime, None)
        return None
    rng = (samples[0][0], samples[-1][0], len(samples)) if samples else None
    with _hr_range_cache_lock:
        _hr_range_cache[date_str] = (mtime, rng)
    return rng


def _hr_covers_window(date_str: str, start_ms: int, end_ms: int) -> bool:
    """True only if the HR cache for this date spans the activity window
    (within `_HR_COVERAGE_TOL_MS` at each end). Used to set `has_hr` honestly:
    a cache file existing is not enough — it has to actually cover the ride."""
    rng = _hr_sample_range(date_str)
    if not rng:
        return False
    first_ms, last_ms, _ = rng
    return first_ms <= start_ms + _HR_COVERAGE_TOL_MS \
       and last_ms  >= end_ms   - _HR_COVERAGE_TOL_MS


# ─── Per-file sidebar cache ───────────────────────────────────────────────────
# Each successful `_build_activity_entry` is persisted to disk with a
# fingerprint of its inputs. On the next cold start (or after a sync of one
# new file) we serve the entry directly when the fingerprint still matches,
# turning the historical ~40 s rebuild of all sidebar rows into ~one-stat
# per file plus the recompute of whatever's actually stale.
# The pure helpers live in `sidebar_cache.py` so they can be unit-tested
# without importing the Flask app.

import sidebar_cache

SIDEBAR_CACHE_DIR = CACHE_DIR / "sidebar"
HR_CACHE_DIR      = CACHE_DIR / "hr"
_stat_mtime       = sidebar_cache.stat_mtime


def _sidebar_fingerprint(gpx_mtime: float, file_meta: dict,
                         regions_mtime: float, types_mtime: float) -> str:
    # `_REGION_MATCH_VERSION` (defined ~line 4163) is resolved at call
    # time, not import time — Python's late binding makes the forward
    # reference work despite the placement. Don't hoist this wrapper
    # above the constant assuming a value capture: the wrapper would
    # still resolve correctly, but a future reader who *did* assume
    # value capture might also move the constant and trip over the
    # actual NameError when this fires before line 4163 executes
    # (e.g. during an early test import).
    return sidebar_cache.sidebar_fingerprint(
        gpx_mtime=gpx_mtime, file_meta=file_meta,
        regions_mtime=regions_mtime, types_mtime=types_mtime,
        algo_sig=ALGO_SIG, region_match_version=_REGION_MATCH_VERSION,
    )


def _read_sidebar_entry(filename: str, expected_fp: str):
    return sidebar_cache.read_sidebar_entry(
        sidebar_cache_dir=SIDEBAR_CACHE_DIR, hr_cache_dir=HR_CACHE_DIR,
        filename=filename, expected_fp=expected_fp,
    )


def _write_sidebar_entry(filename: str, entry: dict, fp: str) -> None:
    sidebar_cache.write_sidebar_entry(
        sidebar_cache_dir=SIDEBAR_CACHE_DIR, hr_cache_dir=HR_CACHE_DIR,
        filename=filename, entry=entry, fp=fp,
    )


def _delete_sidebar_entry(filename: str) -> None:
    sidebar_cache.delete_sidebar_entry(SIDEBAR_CACHE_DIR, filename)


# ─── Disk-cache prewarm (OSM lifts + TZ LRU) ─────────────────────────────────
# When ALGO_SIG bumps invalidate the sidebar cache, all ~554 GPX files
# re-parse. Each parse calls `_fetch_osm_lifts` (file read per bbox) and
# `_weather_timezone_name` (file read per coord cluster). With 6 worker
# threads hitting the same disk files repeatedly, network is no longer the
# bottleneck — file I/O contention is. Pre-loading both disk caches into
# their in-memory dicts before the worker pool starts converts those reads
# into dict lookups.
_DISK_CACHES_PREWARMED = False
_disk_caches_prewarm_lock = threading.Lock()


def _prewarm_disk_caches() -> None:
    """Idempotent. Cheap to call (~10 ms on a few hundred cache files);
    the first call does real work, subsequent calls short-circuit on the
    flag. Failures are swallowed — the lazy disk-read path in each cache's
    own resolver is the fallback."""
    global _DISK_CACHES_PREWARMED
    if _DISK_CACHES_PREWARMED:
        return
    with _disk_caches_prewarm_lock:
        if _DISK_CACHES_PREWARMED:
            return
        _DISK_CACHES_PREWARMED = True

        now = time.time()
        lifts_dir = CACHE_DIR / "lifts"
        if lifts_dir.exists():
            for p in lifts_dir.glob("*.json"):
                try:
                    entry = json.loads(p.read_text(encoding="utf-8"))
                    if now - entry.get("fetched", 0) < _LIFT_CACHE_TTL_SEC:
                        _OSM_LIFT_MEM_CACHE[p.stem] = entry.get("lifts") or []
                except (OSError, ValueError, KeyError):
                    continue

        weather_dir = CACHE_DIR / "weather"
        if weather_dir.exists():
            for p in weather_dir.glob("*.json"):
                try:
                    entry = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                tz_name = entry.get("timezone_name")
                k = entry.get("tz_lru_key")
                # Older entries don't carry tz_lru_key — they upgrade on
                # next network refetch. Don't try to back-fill: writes
                # under a prewarm hold would race with worker writes.
                if tz_name and isinstance(k, list) and len(k) == 2:
                    _TZ_LRU[(k[0], k[1])] = tz_name


def _build_activity_entry(filename: str, meta: dict, regions: list[dict]) -> tuple[dict, dict] | None:
    """Compute the sidebar entry for one activity. Returns (entry, aux) where
    `aux` holds side-data `all_activities` uses (start-coord for geocode
    prewarm). Returns None if the GPX file is missing or unparseable.

    Pulled out of `all_activities` so `_update_activity_entry` can recompute a
    single row on a metadata save without rebuilding every row.
    """
    data = get_activity(filename)
    if not data:
        return None
    file_meta = meta.get(data["filename"], {})
    eff = _effective_data(data["filename"], data, file_meta.get('type', ''))
    # Time-shift runs first — it only changes wall-clock and doesn't
    # interact with the subsequent geometry / segmentation transforms,
    # but it's logically the "data correctness" fix.
    if file_meta.get("time_shift_hours"):
        eff = _apply_time_shift(eff, file_meta["time_shift_hours"])
    trim = file_meta.get("trim") or {}
    if trim:
        eff = _apply_trim(eff, trim)
    if file_meta.get("spike_repair"):
        eff = _apply_spike_repair(eff)
    sm = file_meta.get("smoothing") or {}
    if isinstance(sm, dict) and int(sm.get("window") or 0) > 1:
        eff = _apply_smoothing(eff, sm)
    # Sparkline: downsample elevation to ~40 points for a thumbnail
    pts = eff.get("points", [])
    spark = None
    start_latlon = None
    if pts:
        step = max(1, len(pts) // 40)
        spark = [p.get("ele") for p in pts[::step] if p.get("ele") is not None]
        if len(spark) < 2:
            spark = None
        start_latlon = (pts[0]["lat"], pts[0]["lon"])
    # has_hr = HR cache exists AND its samples actually cover the activity's
    # time window. The previous size-based heuristic flagged any non-empty
    # cache as present — which lit the HR icon for rides whose window had no
    # overlapping samples (e.g. post-ride wellness data hadn't propagated to
    # Garmin yet).
    date_str = (eff.get("date") or "")[:10]
    has_hr = False
    if date_str and pts:
        try:
            t0 = datetime.fromisoformat(pts[0]["time"])
            t1 = datetime.fromisoformat(pts[-1]["time"])
            start_ms = int(t0.timestamp() * 1000)
            end_ms   = int(t1.timestamp() * 1000)
            has_hr = _hr_covers_window(date_str, start_ms, end_ms)
        except (KeyError, TypeError, ValueError):
            has_hr = False
    matched_regions = _effective_regions(eff, file_meta, regions)
    # Bake hr_avg / hr_max / hr_zones into the sidebar entry's stats once at
    # build time so the Summary / Training Load aggregations don't have to
    # re-merge HR per request. The cache invalidates correctly already —
    # `has_hr` tracks the HR cache's mtime via the activities-cache key, and
    # the hr_zones addition is covered by sidebar_cache.ENTRY_SCHEMA_VERSION.
    stats = dict(eff["stats"])
    if has_hr:
        try:
            merged_stats = (_merge_hr_into_data(eff).get("stats") or {})
            if merged_stats.get("hr_avg") is not None:
                stats["hr_avg"] = int(merged_stats["hr_avg"])
            if merged_stats.get("hr_max") is not None:
                stats["hr_max"] = int(merged_stats["hr_max"])
            if merged_stats.get("hr_zones") is not None:
                stats["hr_zones"] = [int(x) for x in merged_stats["hr_zones"]]
        except Exception:
            pass
    entry = {
        "filename": eff["filename"],
        "name":     eff["name"],
        "date":     eff["date"],
        "start_time": (pts[0].get("time")  if pts else None),
        "end_time":   (pts[-1].get("time") if pts else None),
        "tz_name":  eff.get("tz_name"),
        "start_latlon": list(start_latlon) if start_latlon else None,
        "stats":    stats,
        "meta":     file_meta,
        "spark":    spark,
        # 50-point map-shape thumbnail for the /logs mini-maps, baked once
        # here from the same post-transform `pts` the sparkline uses. Lets
        # /api/comparison skip the per-request get_activity + _effective_data
        # + downsample over every activity's full point list.
        "polyline": _downsample_polyline(pts, 50),
        "regions":  matched_regions,
        "has_hr":   has_hr,
        "effective_type": _effective_type_for(file_meta.get("type", ""),
                                              matched_regions, regions,
                                              eff.get("date") or ""),
        "effective_assisted": _is_effectively_assisted(file_meta, matched_regions, regions),
        "issues":   [] if file_meta.get("issues_approved") else _detect_issues_cached(eff),
        "excluded": bool(file_meta.get("excluded_from_stats")),
    }
    aux = {"_start_latlon": start_latlon} if start_latlon else {}
    return entry, aux


def all_activities() -> list[dict]:
    global _activities_cache, _activities_cache_dir_mtime

    key = _activities_cache_key()
    if _activities_cache is not None and key == _activities_cache_dir_mtime:
        return _activities_cache

    with _activities_cache_lock:
        key = _activities_cache_key()
        if _activities_cache is not None and key == _activities_cache_dir_mtime:
            return _activities_cache

        meta    = load_metadata()
        regions = load_regions()
        # Two-phase build:
        #   1. Try each file's persisted sidebar entry — when the
        #      fingerprint matches, no parse/region-match/issue-detect
        #      work is needed.
        #   2. Whatever didn't hit is rebuilt in parallel through the
        #      existing `_build_activity_entry` path, then persisted.
        # Sort filenames first so the result ordering is stable.
        gpx_files = sorted(GPX_DIR.glob("*.gpx"))
        result  = []
        start_coords = []

        regions_mtime = _stat_mtime(REGIONS_FILE)
        types_mtime   = _stat_mtime(TYPES_FILE)

        misses: list[tuple[Path, float]] = []
        cached_builds: dict[str, tuple[dict, dict]] = {}
        for f in gpx_files:
            gpx_mtime = _stat_mtime(f)
            if gpx_mtime < 0:
                continue
            file_meta = meta.get(f.name, {})
            fp = _sidebar_fingerprint(gpx_mtime, file_meta, regions_mtime, types_mtime)
            hit = _read_sidebar_entry(f.name, fp)
            if hit is None:
                # Pair the file with the mtime we just stat'd so the
                # post-build fingerprint matches the snapshot we built
                # from. Re-stat'ing after the build would race with any
                # in-flight write to the GPX (e.g. a still-flushing
                # sync) and could persist a fingerprint that's stale
                # the moment it's written — triggering a needless
                # rebuild on the next cold start.
                misses.append((f, gpx_mtime))
            else:
                cached_builds[f.name] = hit

        def _safe_build(f):
            # Wrap so an unexpected error in any single file doesn't blow
            # up the whole sidebar build — match the existing
            # _build_activity_entry contract: return None on failure.
            try:
                return _build_activity_entry(f.name, meta, regions)
            except Exception:
                logger.exception("sidebar build failed for %s", f.name)
                return None

        rebuilt_builds: dict[str, tuple[dict, dict]] = {}
        if misses:
            # Pre-load OSM lift + TZ disk caches into their in-memory
            # mirrors before the worker pool spins up. Skipped when no
            # misses (warm cache: nothing to parse, no I/O storm to
            # mitigate). Idempotent across calls.
            _prewarm_disk_caches()
            with ThreadPoolExecutor(max_workers=6) as pool:
                builds = list(pool.map(_safe_build, [f for f, _ in misses]))
            for (f, gpx_mtime), built in zip(misses, builds):
                if built is None:
                    continue
                entry, aux = built
                fp = _sidebar_fingerprint(gpx_mtime, meta.get(f.name, {}),
                                          regions_mtime, types_mtime)
                _write_sidebar_entry(f.name, entry, fp)
                rebuilt_builds[f.name] = (entry, aux)

        for f in gpx_files:
            built = cached_builds.get(f.name) or rebuilt_builds.get(f.name)
            if built is None:
                continue
            entry, aux = built
            result.append(entry)
            if aux.get("_start_latlon"):
                start_coords.append(aux)

        _activities_cache            = result
        _activities_cache_dir_mtime  = key
        # Kick off background geocode prewarming so place lines appear
        # without blocking the activity API on first view.
        prewarm_geocode(start_coords)
        return result


def _update_activity_entry(filename: str) -> None:
    """Surgical cache update: recompute one entry and refresh both the
    in-memory list and the on-disk per-file sidebar cache.

    Called from metadata/segment save paths and from the sync-completion
    path, both of which know exactly which files changed and would
    otherwise pay a ~40 s full rebuild. Safe to call before the in-memory
    cache has been built — the disk persistence still applies, and the
    next `all_activities()` picks the new entry up via a fingerprint hit.
    """
    global _activities_cache, _activities_cache_dir_mtime
    meta    = load_metadata()
    regions = load_regions()
    # Stat the GPX *before* building so the persisted fingerprint
    # describes the snapshot the build actually saw. Stat'ing after the
    # build would race with an in-flight write to the file (e.g. a
    # still-flushing Strava sync) and could persist a fingerprint that's
    # stale at write time, triggering a needless rebuild on next start.
    path = _safe_gpx_path(filename)
    gpx_mtime = _stat_mtime(path) if path is not None else -1.0
    built   = _build_activity_entry(filename, meta, regions)

    # Persist (or remove) the on-disk entry up front so that even a
    # cold-cache caller — e.g. a sync subprocess that finishes before
    # any HTTP request hits all_activities — benefits next start.
    if built is None:
        _delete_sidebar_entry(filename)
    else:
        entry, _ = built
        fp = _sidebar_fingerprint(gpx_mtime, meta.get(filename, {}),
                                  _stat_mtime(REGIONS_FILE),
                                  _stat_mtime(TYPES_FILE))
        _write_sidebar_entry(filename, entry, fp)

    with _activities_cache_lock:
        if _activities_cache is None:
            return
        # Locate the existing entry (if any) and replace or remove it
        idx = next((i for i, e in enumerate(_activities_cache)
                    if e["filename"] == filename), None)
        if built is None:
            # File is gone — drop the entry
            if idx is not None:
                _activities_cache.pop(idx)
        else:
            entry, _aux = built
            if idx is None:
                # New file — insert sorted by filename to match full-rebuild order
                _activities_cache.append(entry)
                _activities_cache.sort(key=lambda e: e["filename"])
            else:
                _activities_cache[idx] = entry
        # Re-anchor the cache key so the next all_activities() short-circuits
        # (instead of mistakenly rebuilding from scratch because the metadata
        # file's mtime bumped).
        _activities_cache_dir_mtime = _activities_cache_key()




# ─── MTB re-segmentation cache ────────────────────────────────────────────────
# Default (lift) segmentation is cached via the disk cache. MTB-tagged
# activities are re-segmented at request time using the cached per-point data;
# results are cached in memory, keyed by (filename, mtime).

_mtb_seg_cache: dict[str, dict] = {}
_mtb_seg_lock  = threading.Lock()


def _invalidate_mtb_cache(filename: str | None = None):
    """Drop MTB re-segmentation cache. With a filename, drops only that
    entry (fast; used on single-activity metadata saves). Without, clears
    everything (used when region geometry / types change)."""
    with _mtb_seg_lock:
        if filename is None:
            _mtb_seg_cache.clear()
        else:
            _mtb_seg_cache.pop(filename, None)


def _effective_data(filename: str, data: dict, meta_type: str) -> dict:
    """Return activity data with segments/stats appropriate for the activity type.

    Non-MTB activities use the cached default (lift) segmentation. MTB-tagged
    activities get re-segmented with _algo_mtb, cached by filename+mtime.
    """
    if meta_type != 'mtb':
        return data

    path = _safe_gpx_path(filename)
    mtime = path.stat().st_mtime if (path and path.exists()) else 0

    with _mtb_seg_lock:
        cached = _mtb_seg_cache.get(filename)
        if cached and abs(cached['mtime'] - mtime) < 1:
            return {**data, 'segments': cached['segments'], 'stats': cached['stats']}

    # Recompute MTB segmentation
    per_pt, latlons = _per_pt_from_points(data['points'])
    pts  = data['points']
    bbox = (min(p['lat'] for p in pts), min(p['lon'] for p in pts),
            max(p['lat'] for p in pts), max(p['lon'] for p in pts))
    osm_lifts   = _fetch_osm_lifts(bbox)
    is_assisted = _algo_mtb(per_pt, latlons, osm_lifts)
    segments    = _build_segments(is_assisted)
    stats       = _merge_stats(is_assisted, per_pt, data['stats'])

    with _mtb_seg_lock:
        _mtb_seg_cache[filename] = {'mtime': mtime, 'segments': segments, 'stats': stats}

    return {**data, 'segments': segments, 'stats': stats}


# Elevation smoothing window — see scripts/elev_smoothing.py for the
# calibration analysis that picked k=20.
_ELE_SMOOTH_K = 20


def _smooth_elevations(eles: list, k: int = _ELE_SMOOTH_K) -> list:
    """Centred moving-average smoothing of an elevation series. None
    values are excluded from each window but their output slot is
    preserved (so callers can iterate index-aligned with the source).
    A slot whose window contains no real readings stays None.

    O(n) via prefix sums: each window's average is one subtraction in
    the sum / count arrays, no inner loop. Matters because this runs
    twice per trim or spike-repair request on multi-thousand-point tracks.
    """
    n = len(eles)
    if n == 0:
        return []
    half = max(0, k // 2)
    # Prefix sums of values + counts, indexed [0..n] so any window's
    # totals are psum[hi] - psum[lo] / pcnt[hi] - pcnt[lo].
    psum = [0.0] * (n + 1)
    pcnt = [0]   * (n + 1)
    for i, v in enumerate(eles):
        psum[i+1] = psum[i] + (v if v is not None else 0.0)
        pcnt[i+1] = pcnt[i] + (1 if v is not None else 0)
    out: list = [None] * n
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        cnt = pcnt[hi] - pcnt[lo]
        if cnt > 0:
            out[i] = (psum[hi] - psum[lo]) / cnt
    return out


def parse_gpx(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        gpx = gpxpy.parse(f)

    raw = [pt for track in gpx.tracks for seg in track.segments for pt in seg.points]
    if len(raw) < 2:
        return None

    per_pt = [{'dt': 0, 'dist': 0.0, 'speed': None, 'ele_delta': 0.0}]
    for i in range(1, len(raw)):
        prev, curr = raw[i - 1], raw[i]
        d   = haversine((prev.latitude, prev.longitude), (curr.latitude, curr.longitude))
        dt  = (curr.time - prev.time).total_seconds() if prev.time and curr.time else 0
        # Duplicate or out-of-order GPS timestamps (dt < 0) can't be trusted for
        # distance either — drop both so they don't inflate total_dist / riding_dist.
        if dt < 0:
            dt = 0
            d = 0.0
        spd = None
        if dt > 0 and d > 0:
            spd = (d / dt) * 3.6
            if spd > 150:
                spd = None
        ele_d = ((curr.elevation - prev.elevation)
                 if curr.elevation is not None and prev.elevation is not None else 0.0)
        per_pt.append({'dt': dt, 'dist': d, 'speed': spd, 'ele_delta': ele_d})

    smooth = _median_filter([p['speed'] for p in per_pt], k=5)
    for i, s in enumerate(smooth):
        per_pt[i]['speed'] = s

    # ma20-smoothed elevation series for gain/loss accumulation. We don't
    # feed the smoothed values back into per_pt['ele_delta'] — lift
    # detection (_algo_lift, called below) keys off raw deltas and changing
    # that would shift segment boundaries; the per-point `ele` rendered on
    # charts also stays raw. Only the headline gain numbers consume this.
    _ele_smooth = _smooth_elevations([pt.elevation for pt in raw])

    latlons  = [(pt.latitude, pt.longitude) for pt in raw]
    lats_raw = [pt.latitude  for pt in raw]
    lons_raw = [pt.longitude for pt in raw]
    raw_bbox = (min(lats_raw), min(lons_raw), max(lats_raw), max(lons_raw))

    osm_lifts = _fetch_osm_lifts(raw_bbox)
    # Default (lift-oriented) segmentation — MTB-tagged activities get
    # re-segmented at request time via _effective_data().
    is_assisted = _algo_lift(per_pt, latlons, osm_lifts)
    segments = _build_segments(is_assisted)

    # Resolve the activity-location IANA timezone so naive GPX timestamps
    # (TrailForks-style "wall clock as +00:00") become unambiguous absolute
    # moments downstream. Network failures are non-fatal — we fall back to
    # leaving timestamps as-emitted by gpxpy.
    tz_name = None
    tz_zone = None
    if raw[0].time is not None:
        date_str = raw[0].time.strftime("%Y-%m-%d")
        try:
            tz_name = _weather_timezone_name(raw[0].latitude, raw[0].longitude, date_str)
            if tz_name:
                tz_zone = ZoneInfo(tz_name)
        except Exception:
            tz_name = None
            tz_zone = None

    # See _FAKE_UTC_CREATORS at module level. For these producers, +00:00
    # timestamps must be `replace`d (offset stripped, then re-anchored at the
    # activity zone), not `astimezone`d. Anything else is trusted at face value.
    creator = (gpx.creator or "").lower()
    fake_utc = any(
        creator == s or creator.startswith(s + " ") or creator.startswith(s + ".")
        for s in _FAKE_UTC_CREATORS
    )

    def _iso_localized(t):
        # Emit every timestamp in the activity-location's IANA zone so the
        # wall-clock portion of the ISO string is always the rider's clock.
        # Naive timestamps and fake-UTC timestamps from known producers both
        # need their wall-clock preserved (replace, not convert). Genuine
        # offsets — including real UTC from non-fake sources — get converted.
        # HR matching and duration math go through absolute moments so the
        # downstream stays aligned regardless of which branch runs.
        if t is None:
            return None
        if tz_zone is not None:
            if t.tzinfo is None or (fake_utc and t.utcoffset() == timedelta(0)):
                t = t.replace(tzinfo=tz_zone)
            else:
                t = t.astimezone(tz_zone)
        return t.isoformat()

    total_dist = riding_dist = 0.0
    elev_gain = elev_loss = assisted_gain = max_speed = max_speed_riding = 0.0
    riding_dur_sec = 0.0
    points = []

    for i, pt in enumerate(raw):
        if i > 0:
            p = per_pt[i]
            total_dist += p['dist']
            # Use the smoothed elevation series for gain / loss / assisted_gain.
            # Falls back to a 0 delta if either neighbour has no elevation.
            sm_prev, sm_curr = _ele_smooth[i-1], _ele_smooth[i]
            sm_delta = ((sm_curr - sm_prev)
                        if sm_prev is not None and sm_curr is not None else 0.0)
            if is_assisted[i]:
                if sm_delta > 0:
                    assisted_gain += sm_delta
            else:
                riding_dist += p['dist']
                riding_dur_sec += p['dt']
                if sm_delta > 0:
                    elev_gain += sm_delta
                elif sm_delta < 0:
                    elev_loss += abs(sm_delta)
            if p['speed'] is not None:
                if p['speed'] > max_speed:
                    max_speed = p['speed']
                # Riding-only max excludes lift/shuttle samples so vehicle/lift
                # telemetry doesn't appear in Top Speed records.
                if not is_assisted[i] and p['speed'] > max_speed_riding:
                    max_speed_riding = p['speed']

        sm_i = _ele_smooth[i]
        points.append({
            "lat":     pt.latitude,
            "lon":     pt.longitude,
            "ele":     round(pt.elevation, 1) if pt.elevation is not None else None,
            # Smoothed ele used by client-side recomputeStats so detail-view
            # gain matches the cached stat (raw deltas inflate gain by GPS noise).
            "ele_sm":  round(sm_i, 1) if sm_i is not None else None,
            "time":    _iso_localized(pt.time),
            "dist_km": round(total_dist / 1000, 3),
            "speed":   round(per_pt[i]['speed'], 1) if per_pt[i]['speed'] is not None else None,
            # Persist the leg-arriving-here's assisted flag so downstream
            # consumers (spike detection, trimmed-stats recompute) can be
            # per-segment aware without re-running the lift algorithms.
            "assisted": bool(is_assisted[i]),
        })

    start, end = raw[0].time, raw[-1].time
    dur       = (end - start).total_seconds() if start and end else None
    # avg_speed uses riding time only — lift/assisted segments are excluded
    # from both numerator and denominator so the number reflects pace when
    # actually moving, not overall wall-clock pace.
    avg_speed = (riding_dist / 1000) / (riding_dur_sec / 3600) if riding_dur_sec > 0 else None

    lats = [p["lat"] for p in points]
    lons = [p["lon"] for p in points]

    return {
        "filename": path.name,
        "name":     (gpx.tracks[0].name or "").strip() or path.stem,
        "date":     _iso_localized(start),
        "tz_name":  tz_name,
        "bbox":     [min(lats), min(lons), max(lats), max(lons)],
        "points":   points,
        "segments": segments,
        "stats": {
            "distance_km":     round(riding_dist / 1000, 2),
            "duration_sec":    round(dur) if dur else None,
            "elev_gain_m":     round(elev_gain),
            "elev_loss_m":     round(elev_loss),
            "assisted_gain_m": round(assisted_gain),
            "avg_speed_kmh":   round(avg_speed, 1) if avg_speed is not None else None,
            "max_speed_kmh":   round(max_speed, 1),
            "max_speed_kmh_riding": round(max_speed_riding, 1) if max_speed_riding > 0 else None,
            "lift_count":      sum(1 for s in segments if s["type"] == "assisted"),
            "peak_ele_m":      round(max((p["ele"] for p in points if p["ele"] is not None), default=0)) or None,
        },
    }


# ─── Disk-backed cache ────────────────────────────────────────────────────────

CACHE_VERSION = hashlib.md5(ALGO_SIG.encode()).hexdigest()[:8]


_mem_cache     = LRUCache(400)
_file_locks:   dict[str, threading.Lock] = {}
_file_locks_mu = threading.Lock()

# Sentinel cached in _mem_cache when parse_gpx can't produce usable data
# (e.g. a single-point GPX). Prevents re-parsing the same broken file on
# every request; get_activity returns None for these.
_UNPARSEABLE = object()


def _lock_for(filename: str) -> threading.Lock:
    with _file_locks_mu:
        if filename not in _file_locks:
            _file_locks[filename] = threading.Lock()
        return _file_locks[filename]


def _cache_path(filename: str) -> Path:
    return CACHE_DIR / "gpx" / f"{filename}.json"


def _read_disk_cache(filename: str, mtime: float) -> dict | None:
    cp = _cache_path(filename)
    if not cp.exists():
        return None
    try:
        entry = json.loads(cp.read_text(encoding="utf-8"))
        if entry.get("version") == CACHE_VERSION and abs(entry.get("mtime", 0) - mtime) < 1:
            return entry["data"]
    except Exception:
        pass
    return None


def _write_disk_cache(filename: str, mtime: float, data: dict):
    entry = {"version": CACHE_VERSION, "mtime": mtime, "data": data}
    _atomic_write(_cache_path(filename), json.dumps(entry, ensure_ascii=False))


def _safe_gpx_path(filename: str) -> Path | None:
    """Return the resolved GPX Path only if it stays inside GPX_DIR."""
    try:
        resolved = (GPX_DIR / filename).resolve()
    except Exception:
        return None
    if not resolved.is_relative_to(GPX_DIR.resolve()) or resolved.suffix != ".gpx":
        return None
    return resolved


def get_activity(filename: str) -> dict | None:
    cached = _mem_cache.get(filename)
    if cached is _UNPARSEABLE:
        return None
    if cached is not None:
        return cached

    with _lock_for(filename):
        cached = _mem_cache.get(filename)
        if cached is _UNPARSEABLE:
            return None
        if cached is not None:
            return cached

        path = _safe_gpx_path(filename)
        if path is None or not path.exists():
            return None

        mtime = path.stat().st_mtime
        data  = _read_disk_cache(filename, mtime)
        if data is None:
            data = parse_gpx(path)
            if data:
                _write_disk_cache(filename, mtime, data)

        if data:
            _mem_cache.set(filename, data)
        else:
            _mem_cache.set(filename, _UNPARSEABLE)
        return data


def _prewarm():
    """Parse every GPX file in parallel, newest first. No longer launched
    at startup — the per-file sidebar cache means `all_activities()` no
    longer needs the full parsed payloads in `_mem_cache`. Kept available
    for callers that explicitly want to warm the parsed-GPX cache (e.g.
    a future "Warm cache now" admin action)."""
    def _mtime_or_zero(f):
        try:
            return f.stat().st_mtime
        except OSError:
            return 0
    files = sorted(GPX_DIR.glob("*.gpx"), key=_mtime_or_zero, reverse=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda f: get_activity(f.name), files))
    all_activities()


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    """Home page — Summary view. Old `/?file=…` bookmarks redirect to
    the activity-detail page at its current canonical path."""
    if "file" in request.args:
        return redirect(f"/log/{request.args['file']}", code=301)
    return render_template("summary.html", types_json=_safe_json(load_types()))


@app.route("/logs")
def logs():
    """Logs landing — rich list of activities with mini-maps and stats.
    Click a row to open the detail view at /log/<filename>; pair-select
    two rows to overlay them at /compare."""
    return render_template("logs.html",
                           types_json=_safe_json(load_types()),
                           regions_json=_safe_json(_regions_lite(load_regions())),
                           mapbox_token=load_config().get("mapbox_token", ""))


@app.route("/log/<path:filename>")
def log_detail(filename):
    """Per-activity detail — map + chart + stats + sidebar nav. The
    sidebar still lists every activity for quick jumping; the canonical
    way in is via the Logs landing at /logs."""
    return render_template("index.html",
        mapbox_token=load_config().get("mapbox_token", ""),
        types_json=_safe_json(load_types()),
        current_filename=filename)


@app.route("/rides")
def rides_redirect():
    """Old route — `/rides?file=X` was the activity detail; `/rides`
    alone was the list-with-sidebar landing. Both moved: detail to
    /log/<filename>, list to /logs."""
    file = request.args.get("file")
    if file:
        return redirect(f"/log/{file}", code=301)
    return redirect("/logs", code=302)


@app.route("/summary")
def summary_archived():
    """Archived. Linked from Setup → Archived; not in the main nav."""
    return render_template("summary_archived.html", types_json=_safe_json(load_types()))


@app.route("/summary/v2")
def summary_v2_redirect():
    """Old route — Summary is now the canonical home page at `/`. 301 so
    historical bookmarks resolve there directly."""
    return redirect("/", code=301)


@app.route("/training")
def training_load():
    """Coach-view fitness page (per design/training-load). Data fetched from
    /api/training; template stays thin."""
    return render_template("training_load.html", types_json=_safe_json(load_types()))


@app.route("/review")
def review_page():
    """Curation / data-quality dashboard — duplicate detection, odd-time
    flagging, GPS speed-spike flagging, and missing-type flagging on one
    page. Replaces the diagnostic tabs that previously lived under Setup."""
    return render_template("review.html", types_json=_safe_json(load_types()))


@app.route("/routes")
def routes_list_page():
    """List of saved routes. Shares a sub-nav with /trails so the two
    pages feel like one Trails-and-Routes view. Region payload is lite
    (no geometry) — the list view only needs name + colour for the
    region chip + dropdown."""
    return render_template(
        "routes.html",
        regions_json=_safe_json(_regions_lite(load_regions())),
    )


@app.route("/routes/<route_id>")
def route_detail_page(route_id: str):
    """Read-only route page: map of the highlighted segments + attempts
    leaderboard below. The builder lives at /routes/edit?id=... — this
    page is meant for showing off a saved route, not editing it."""
    route = _load_route(route_id)
    if route is None:
        abort(404)
    region = _region_by_id(route["region_id"])
    return render_template(
        "route_detail.html",
        route_json=_safe_json(route),
        region_json=_safe_json(region) if region else "null",
    )


@app.route("/routes/edit")
def routes_edit_page():
    """Builder: pick trails from a region's map, ordered into a route.

    `?id=<route_id>` loads an existing route; without it, a fresh blank
    builder. See route_builder.py for the underlying region artifact.

    Region payload is lite — the builder fetches per-region geometry
    via /api/regions/<id>/trails-geometry once the user picks one,
    so the initial bundle is just the dropdown's id/name list.
    """
    return render_template(
        "routes_edit.html",
        regions_json=_safe_json(_regions_lite(load_regions())),
        route_id=request.args.get("id") or "",
    )


@app.route("/training-load")
def training_load_redirect():
    """Old route — `/training-load` shortened to `/training`. 301 so any
    historical bookmarks resolve cleanly."""
    return redirect("/training", code=301)


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    """#3b82f6 → 'rgba(59, 130, 246, 0.18)'. Falls back to the input on
    parse failure so the page still renders."""
    h = (hex_color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return hex_color
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return hex_color
    return f"rgba({r}, {g}, {b}, {alpha})"


def _three_letter_short(type_id: str, label: str) -> str:
    """Prefer the type id for short labels: 'mtb' → 'MTB', 'fat_biking' →
    'FAT', 'snowboard' → 'SNO'. Falls back to first-letters-of-words on the
    label if the id is unusable."""
    if type_id:
        clean = "".join(c for c in type_id if c.isalpha())
        if clean:
            return clean[:3].upper()
    parts = [p for p in (label or "").split() if p]
    if len(parts) >= 2:
        return "".join(p[0] for p in parts[:3]).upper()
    return parts[0][:3].upper() if parts else "???"


_SUMMARY_V2_TYPE_ORDER = ("mtb", "snowboard", "ski", "hike", "other")


def _summary_data(days_back: int, units: str) -> dict:
    """Build the data contract documented in design/summary/README.md from
    the user's real activities. Window is rolling-`days_back`-days back from
    today, inclusive of today.

    Result is memoised in _summary_data_cache.  The cache key piggybacks on
    _activities_cache_dir_mtime — the tuple already computed and stored by
    all_activities() — rather than calling _activities_cache_key() again.
    This avoids a second set of stat() calls (and the HR-dir scan) on the
    hot path: after all_activities() runs, the key is already in memory.

    Invalidation: _invalidate_activities_cache() also clears this cache, so
    any write path that bumps the activities cache automatically busts the
    summary cache too.
    """
    global _summary_data_cache, _summary_data_key

    today_dt  = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_iso = today_dt.strftime("%Y-%m-%d")

    # Ensure the activities cache is warm and _activities_cache_dir_mtime is
    # populated — this is the only stat() I/O on the hot path (and it returns
    # from the in-memory cache on all but the first call or after a file change).
    all_activities()

    # Use the already-stored key.  If _activities_cache_dir_mtime is still
    # None (shouldn't happen after the call above, but be defensive) fall
    # through to compute.
    acts_key  = _activities_cache_dir_mtime  # tuple set by all_activities()
    cache_key = (days_back, units, today_iso, acts_key)

    with _summary_data_lock:
        if _summary_data_cache is not None and _summary_data_key == cache_key:
            return _summary_data_cache

    result = _summary_data_compute(days_back, units, today_dt, today_iso)

    with _summary_data_lock:
        _summary_data_cache = result
        _summary_data_key   = cache_key

    return result


def _active_day_streaks(sorted_dates: list, today_date) -> tuple:
    """(longest, current) consecutive-day streaks from a sorted list of
    unique YYYY-MM-DD active-day strings. Current = the streak ending today
    or yesterday (0 if the most recent active day is older). Both 0 when
    there are no dates."""
    if not sorted_dates:
        return 0, 0
    # date.fromisoformat is ~3x faster than datetime.strptime for
    # YYYY-MM-DD strings; parse once into a list then index.
    date_objs = [date.fromisoformat(d) for d in sorted_dates]
    longest = run = 1
    for i in range(1, len(date_objs)):
        if (date_objs[i] - date_objs[i - 1]).days == 1:
            run += 1
            longest = max(longest, run)
        else:
            run = 1
    current = 0
    # Current streak is consecutive days ending at today (or yesterday).
    if (today_date - date_objs[-1]).days <= 1:
        run = 1
        for i in range(len(date_objs) - 1, 0, -1):
            if (date_objs[i] - date_objs[i - 1]).days == 1:
                run += 1
            else:
                break
        current = run
    return longest, current


def _summary_data_compute(days_back: int, units: str,
                           today_dt: datetime, today_iso: str) -> dict:
    """Internal implementation — called only on cache miss."""
    earliest_dt = today_dt - timedelta(days=days_back - 1)
    earliest_iso = earliest_dt.strftime("%Y-%m-%d")

    acts = [a for a in all_activities() if not a.get("excluded")]
    types = load_types()
    type_lookup = {t["id"]: t for t in types}

    # Group by effective type. Activities without a type fall under "other".
    by_type: dict[str, list[dict]] = {}
    for a in acts:
        tid = (a.get("meta") or {}).get("type") or a.get("effective_type") or "other"
        by_type.setdefault(tid, []).append(a)

    # Sort by which activity type was active most recently — the type whose
    # newest activity is closest to today comes first. Stable on type id to
    # break ties deterministically.
    def _last_date_for_type(tid: str) -> str:
        dates = [(a.get("date") or "")[:10] for a in by_type.get(tid, []) if a.get("date")]
        return max(dates) if dates else ""
    ordered_ids: list[str] = sorted(
        by_type.keys(),
        key=lambda tid: (_last_date_for_type(tid), tid),
        reverse=True,
    )

    activities_def = []
    for tid in ordered_ids:
        td = type_lookup.get(tid, {})
        is_snow = tid in ("snowboard", "ski")
        label = td.get("label") or tid.title()
        accent = td.get("color") or "#3b82f6"
        activities_def.append({
            "id":         tid,
            "label":      label,
            "short":      td.get("glyph") or _three_letter_short(tid, label),
            "glyph":      td.get("glyph") or label[:1].upper() or "?",
            "accent":     accent,
            "accentSoft": _hex_to_rgba(accent, 0.18),
            "metrics":    (["days", "descent", "distance", "moving"] if is_snow
                           else ["days", "distance", "ascent", "moving"]),
            "chartMetrics": ([
                {"key": "count",    "label": "Activities"},
                {"key": "descent",  "label": "Vertical"},
                {"key": "duration", "label": "Duration"},
                {"key": "distance", "label": "Distance"},
            ] if is_snow else [
                {"key": "count",    "label": "Activities"},
                {"key": "distance", "label": "Distance"},
                {"key": "duration", "label": "Duration"},
                {"key": "ascent",   "label": "Climbing"},
            ]),
            "isSnow": is_snow,
        })

    # Discover years that have any activity, capped at the last 8 (mirrors
    # the design — yearsToShow = 4 by default).
    years_set: set[int] = set()
    for a in acts:
        d = (a.get("date") or "")[:10]
        if len(d) == 10:
            try:
                years_set.add(int(d[:4]))
            except ValueError:
                pass
    years_sorted = sorted(years_set)

    rollups: dict = {}
    history: dict = {}
    recent_combined: list = []
    active_dates_dominant: dict[str, str] = {}
    active_dates_set: set[str] = set()

    for tid in ordered_ids:
        type_acts = by_type[tid]
        # Rolling-window subset — ISO strings compare lexicographically
        # correctly for YYYY-MM-DD, so no datetime parse needed here.
        in_window = []
        for a in type_acts:
            d = (a.get("date") or "")[:10]
            if len(d) != 10:
                continue
            if earliest_iso <= d <= today_iso:
                in_window.append(a)

        # Days set + dominant type for the ribbon
        for a in in_window:
            iso = (a.get("date") or "")[:10]
            if not iso:
                continue
            active_dates_set.add(iso)
            # First-write wins per date, mirroring the README's "RECENT
            # overrides HISTORY" rule (we go newest-first below).
            active_dates_dominant.setdefault(iso, tid)

        unique_dates = {(a.get("date") or "")[:10] for a in in_window if a.get("date")}
        # Streaks (current + longest) from unique active dates within window
        sorted_dates = sorted(d for d in unique_dates if d)
        longest_streak, current_streak = _active_day_streaks(sorted_dates, today_dt.date())

        def _stat_sum(key):
            return sum((a.get("stats") or {}).get(key) or 0 for a in in_window)
        def _stat_max(key):
            vals = [(a.get("stats") or {}).get(key) for a in in_window]
            vals = [v for v in vals if v is not None]
            return max(vals) if vals else 0

        # avg_hr / max_hr — `_activity_payload` bakes these into entry.stats
        # at sidebar-build time so we can aggregate cheaply here.
        hr_acts = [a for a in in_window if (a.get("stats") or {}).get("hr_avg") is not None]
        avg_hr = round(sum(a["stats"]["hr_avg"] for a in hr_acts) / len(hr_acts)) if hr_acts else None
        max_hr_val = max((a["stats"]["hr_max"] for a in hr_acts if a["stats"].get("hr_max")), default=None)

        last_date_iso = max(unique_dates) if unique_dates else None

        # Records — emitted twice: once from the rolling window (period
        # records) and once from full history (all-time records). The
        # frontend renders them side-by-side so the user can compare a
        # year's bests against their lifetime bests.
        def _build_prs(source):
            # Distance and Duration records exclude lift/shuttle-assisted
            # rides (bike park lap days, ski-hill rides) at the activity
            # level so they don't dominate rankings against point-to-point
            # efforts. Top speed uses a per-segment riding-only field
            # (max_speed_kmh_riding) so mixed activities still contribute
            # their actual riding/skiing top speed without lift/shuttle
            # telemetry leaking in. Climbing / Descent / Vertical remain
            # inclusive: a lift-assisted ski day's descent is still the
            # athlete descending.
            unassisted = [a for a in source if not a.get("effective_assisted")]

            def _rank(key, top=1, src=None):
                items = src if src is not None else source
                ranked = sorted(
                    (a for a in items if (a.get("stats") or {}).get(key) is not None),
                    key=lambda a: a["stats"][key], reverse=True,
                )
                return ranked[:top]

            def _entry(top, label, value_key, value_field, kind):
                return {
                    "label":     label,
                    value_field: top["stats"][value_key],
                    "kind":      kind,
                    "name":      (top.get("meta") or {}).get("title") or top.get("name") or top["filename"],
                    "date":      (top.get("date") or "")[:10],
                    "filename":  top["filename"],
                }

            out = []
            # Snow types lead with descent ("vertical"), others with distance.
            if tid in ("snowboard", "ski"):
                for top in _rank("elev_loss_m"):                       out.append(_entry(top, "Vertical",  "elev_loss_m",          "value_m",   "elev"))
                for top in _rank("distance_km",          src=unassisted): out.append(_entry(top, "Longest",   "distance_km",          "value_km",  "dist"))
                for top in _rank("max_speed_kmh_riding"):              out.append(_entry(top, "Top speed", "max_speed_kmh_riding", "value_kmh", "speed"))
                for top in _rank("duration_sec",         src=unassisted): out.append(_entry(top, "Duration",  "duration_sec",         "value_sec", "dur"))
            else:
                for top in _rank("distance_km",          src=unassisted): out.append(_entry(top, "Longest",   "distance_km",          "value_km",  "dist"))
                for top in _rank("elev_gain_m"):                       out.append(_entry(top, "Climbing",  "elev_gain_m",          "value_m",   "elev"))
                for top in _rank("elev_loss_m"):                       out.append(_entry(top, "Descent",   "elev_loss_m",          "value_m",   "elev"))
                for top in _rank("max_speed_kmh_riding"):              out.append(_entry(top, "Top speed", "max_speed_kmh_riding", "value_kmh", "speed"))
                for top in _rank("duration_sec",         src=unassisted): out.append(_entry(top, "Duration",  "duration_sec",         "value_sec", "dur"))
            return out

        prs = _build_prs(type_acts)   # all-time
        # Per-year top-1 snapshots so the frontend can merge any year-chip
        # selection client-side. Top-1 per year, then max across selected
        # years, is equivalent to running the rank on the union of those
        # years' activities (which is what the user expects from the
        # "Records" column when they toggle year chips).
        prs_by_year: dict = {}
        acts_by_year: dict = {}
        for a in type_acts:
            yr = (a.get("date") or "")[:4]
            if yr.isdigit():
                acts_by_year.setdefault(yr, []).append(a)
        for yr, acts in acts_by_year.items():
            prs_by_year[yr] = _build_prs(acts)

        rollups[tid] = {
            "days":            len(unique_dates),
            "activity_count":  len(in_window),
            "distance_km":     round(_stat_sum("distance_km"), 1),
            "elev_gain_m":     round(_stat_sum("elev_gain_m")),
            "elev_loss_m":     round(_stat_sum("elev_loss_m")),
            "moving_h":        round(_stat_sum("duration_sec") / 3600.0, 2),
            "avg_speed_kmh":   None,  # computed below from total dist / total moving
            "max_speed_kmh":   round(_stat_max("max_speed_kmh"), 1) or None,
            "avg_hr":          avg_hr,
            "max_hr":          max_hr_val,
            "longest_streak":  longest_streak,
            "current_streak":  current_streak,
            "last_date":       last_date_iso,
            "prs":             prs,
            "prs_by_year":     prs_by_year,
        }
        # avg_speed: total distance / total riding time
        total_dist = rollups[tid]["distance_km"]
        total_h = rollups[tid]["moving_h"]
        rollups[tid]["avg_speed_kmh"] = round(total_dist / total_h, 1) if total_h > 0 else None

        # ── HISTORY: per-year monthly buckets, all years (not just window) ──
        h: dict = {}
        for year in years_sorted:
            h[str(year)] = {
                "count":    [None] * 12,
                "distance": [None] * 12,
                "duration": [None] * 12,
                "ascent":   [None] * 12,
                "descent":  [None] * 12,
            }
        # Aggregate — extract year/month directly from the ISO string to
        # avoid datetime.strptime overhead on every activity.
        for a in type_acts:
            d = (a.get("date") or "")[:10]
            if len(d) != 10:
                continue
            try:
                yr_str = d[:4]
                mi = int(d[5:7]) - 1
            except (ValueError, IndexError):
                continue
            if yr_str not in h:
                continue
            s = a.get("stats") or {}
            for key, val_key, unit_factor in (
                ("count",    None,            1),  # +1 per activity
                ("distance", "distance_km",   1),
                ("duration", "duration_sec",  1.0 / 3600.0),
                ("ascent",   "elev_gain_m",   1),
                ("descent",  "elev_loss_m",   1),
            ):
                if key == "count":
                    h[yr_str][key][mi] = (h[yr_str][key][mi] or 0) + 1
                else:
                    v = s.get(val_key)
                    if v is None:
                        continue
                    h[yr_str][key][mi] = round((h[yr_str][key][mi] or 0) + v * unit_factor, 1)

        # Determine the first (year, month) this activity ever appeared so
        # we can zero "active but empty" months while leaving truly
        # out-of-range months as null (the design renders 0 vs null
        # differently — solid empty-bar vs faint stub).
        first_year = first_month = None
        for a in type_acts:
            d = (a.get("date") or "")[:10]
            if len(d) != 10:
                continue
            try:
                yr = int(d[:4])
                mo = int(d[5:7])
            except (ValueError, IndexError):
                continue
            if first_year is None or (yr, mo) < (first_year, first_month):
                first_year, first_month = yr, mo
        cur_year = today_dt.year
        cur_month_idx = today_dt.month - 1
        for yr_str, monthly in h.items():
            yr = int(yr_str)
            for mi in range(12):
                in_future = (yr > cur_year) or (yr == cur_year and mi > cur_month_idx)
                pre_first = (first_year is not None and (
                    yr < first_year or (yr == first_year and mi < first_month - 1)))
                if in_future or pre_first:
                    for key in ("count", "distance", "duration", "ascent", "descent"):
                        monthly[key][mi] = None
                else:
                    for key in ("count", "distance", "duration", "ascent", "descent"):
                        if monthly[key][mi] is None:
                            monthly[key][mi] = 0

        history[tid] = h

        # Recent log — last 5 in window for this type, newest first
        recent_for_type = sorted(
            (a for a in in_window if a.get("date")),
            key=lambda a: a["date"], reverse=True,
        )[:5]
        for a in recent_for_type:
            s = a.get("stats") or {}
            dur_sec = s.get("duration_sec") or 0
            dur_h = int(dur_sec // 3600)
            dur_m = int((dur_sec % 3600) // 60)
            recent_combined.append({
                "id":   a["filename"],
                "type": tid,
                "date": (a.get("date") or "")[:10],
                "name": (a.get("meta") or {}).get("title") or a.get("name") or a["filename"],
                "dist": round(s.get("distance_km") or 0, 1),
                "elev": round((s.get("elev_loss_m") if tid in ("snowboard", "ski") else s.get("elev_gain_m")) or 0),
                "dur":  f"{dur_h}:{str(dur_m).zfill(2)}",
                "max":  round(s.get("max_speed_kmh") or 0, 1) if s.get("max_speed_kmh") else None,
                "hr":   s.get("hr_avg"),
            })

    # ─── Cross-activity totals ──────────────────────────────────────────────
    # ISO string compare replaces datetime.strptime for the window filter.
    all_in_window = [a for tid in ordered_ids for a in by_type[tid]
                     if earliest_iso <= (a.get("date") or "")[:10] <= today_iso
                     and len((a.get("date") or "")[:10]) == 10]
    unique_active = {a["date"][:10] for a in all_in_window if a.get("date")}
    sorted_dates_all = sorted(unique_active)
    longest_streak_all, current_streak_all = _active_day_streaks(sorted_dates_all, today_dt.date())

    last_date_all = max(unique_active) if unique_active else None
    days_since = None
    if last_date_all:
        days_since = (today_dt.date() - date.fromisoformat(last_date_all)).days

    # Active days in the last 14 days (rolling fortnight ending today).
    fortnight_start = (today_dt - timedelta(days=13)).strftime("%Y-%m-%d")
    last_14d_active_days = sum(1 for d in unique_active if d >= fortnight_start)

    totals = {
        "days":            len(unique_active),
        "distance_km":     round(sum((a.get("stats") or {}).get("distance_km") or 0 for a in all_in_window), 1),
        "elev_gain_m":     sum((a.get("stats") or {}).get("elev_gain_m") or 0 for a in all_in_window),
        "elev_loss_m":     sum((a.get("stats") or {}).get("elev_loss_m") or 0 for a in all_in_window),
        "moving_h":        round(sum((a.get("stats") or {}).get("duration_sec") or 0 for a in all_in_window) / 3600.0, 2),
        "longest_streak":  longest_streak_all,
        "current_streak":  current_streak_all,
        "last_14d_active_days": last_14d_active_days,
        "last_date":       last_date_all,
        "days_since":      days_since,
    }

    # Sort the combined recent log newest-first (across all activities)
    recent_combined.sort(key=lambda r: r["date"], reverse=True)

    return {
        "today":      today_iso,
        "daysBack":   days_back,
        "units":      units,
        "years":      years_sorted,
        "activities": activities_def,
        "rollups":    rollups,
        "history":    history,
        "recent":     recent_combined[:25],   # cap; per-activity slicing happens client-side
        "activeDates": sorted(active_dates_set),
        "dominantByDate": active_dates_dominant,
        "totals":     totals,
    }


@app.route("/api/summary")
def api_summary():
    try:
        days_back = max(1, min(3650, int(request.args.get("days", 365))))
    except (TypeError, ValueError):
        days_back = 365
    units = request.args.get("units", "metric")
    if units not in ("metric", "imperial"):
        units = "metric"
    return jsonify(_summary_data(days_back, units))


def _make_etag(*parts) -> str:
    return hashlib.md5(repr(parts).encode()).hexdigest()


# Bump when changing the shape/contents of /api/activity responses so clients
# refetch even if all input files are unchanged.
_ACTIVITY_RESPONSE_VERSION = 13

# Trail-matching is now applied to every MTB activity, regardless of
# region. (Originally gated to Moose Mountain while the snap algorithm
# was being validated — see git history for that scope evolution.) Ski
# and snowboard activities are still excluded because OSM doesn't map
# ski runs reliably.
TRAIL_MATCH_CACHE_DIR    = CACHE_DIR / "trail_match"
OSM_PATHS_CACHE_DIR      = CACHE_DIR / "osm_paths"
# v13: trail_match also snaps GPS against named OSM road ways. Roads have
# their own bbox cache (matches the route_builder pattern) so the trail
# fetch and road fetch can invalidate independently. Kept adjacent to
# OSM_PATHS_CACHE_DIR so a future "third kind of OSM cache" lands here too.
OSM_ROADS_CACHE_DIR      = CACHE_DIR / "osm_roads"


# ─── Trail leaderboard cache ─────────────────────────────────────────────────
# `build_leaderboards()` scans the trail_match cache directory, which is
# fast (small JSON files) but still O(rides). We memoise the result and
# only rebuild when the directory's mtime or metadata.json changes — both
# bump when a new ride is processed (atomic file rename touches dir
# mtime; metadata edits bump that file directly).

_trail_leaderboard_cache: dict[str, list[dict]] | None = None
_trail_leaderboard_key:   tuple | None = None
_trail_leaderboard_lock                = threading.Lock()

# Same invalidation pattern as the leaderboards — share the dir-mtime key
# so a single file write invalidates both caches consistently.
_trail_region_index_cache: dict | None = None
_trail_region_index_key:   tuple | None = None


def _trail_leaderboard_cache_key() -> tuple:
    return (_stat_mtime(TRAIL_MATCH_CACHE_DIR), _stat_mtime(METADATA_FILE),
            trail_match.TRAIL_MATCH_VERSION)


def _get_leaderboards() -> dict[str, list[dict]]:
    global _trail_leaderboard_cache, _trail_leaderboard_key
    key = _trail_leaderboard_cache_key()
    with _trail_leaderboard_lock:
        if _trail_leaderboard_cache is not None and _trail_leaderboard_key == key:
            return _trail_leaderboard_cache
        boards = trail_match.build_leaderboards(TRAIL_MATCH_CACHE_DIR,
                                                activity_meta=load_metadata())
        _trail_leaderboard_cache = boards
        _trail_leaderboard_key   = key
        return boards


def _get_region_trail_index(activity_regions: dict[str, list[str]]) -> dict:
    """Cached wrapper around build_region_trail_index. Uses the same
    invalidation key as the leaderboard cache plus the regions-file mtime
    (so a region rename invalidates the cached index). Shares the
    leaderboard lock — both caches are read together and the write path
    is dominated by the build call so contention is negligible."""
    global _trail_region_index_cache, _trail_region_index_key
    region_files_key = (_trail_leaderboard_cache_key(),
                        _stat_mtime(REGIONS_FILE))
    with _trail_leaderboard_lock:
        if (_trail_region_index_cache is not None
                and _trail_region_index_key == region_files_key):
            return _trail_region_index_cache
        index = trail_match.build_region_trail_index(
            TRAIL_MATCH_CACHE_DIR,
            activity_meta=load_metadata(),
            activity_regions=activity_regions,
        )
        _trail_region_index_cache = index
        _trail_region_index_key   = region_files_key
        return index


def _invalidate_trail_leaderboards() -> None:
    """Called after a trail_match cache file is written by the prewarm or
    the activity API. Forces trail leaderboard and region-index caches to
    rebuild on next read.

    Route-attempt cache invalidation is NOT cascaded from here. Cascading
    would clear the route_attempts mem cache 554 times during prewarm
    (once per ride), guaranteeing the /routes page hits a cold cache for
    every request during the prewarm window. Instead, the rescan
    endpoints and the prewarm tail call `_invalidate_route_attempts()`
    explicitly — exactly once per logical operation."""
    global _trail_leaderboard_cache, _trail_leaderboard_key
    global _trail_region_index_cache, _trail_region_index_key
    with _trail_leaderboard_lock:
        _trail_leaderboard_cache = None
        _trail_leaderboard_key   = None
        _trail_region_index_cache = None
        _trail_region_index_key   = None


# ─── Route attempts cache (Stage C) ─────────────────────────────────────────
# Per-route leaderboard data derived from the per-file trail_match cache.
# Disk-persisted at ROUTE_ATTEMPTS_CACHE_DIR/<route_id>.json with a key
# tuple covering everything that could change the result: route shape,
# trail_match output, region-artifact geometry, the matcher version, and
# the user's metadata (titles surface in the rows).

_route_attempts_mem_cache: dict[str, dict] = {}
_route_attempts_keys:      dict[str, tuple] = {}
_route_attempts_locks:     dict[str, threading.Lock] = {}
_route_attempts_locks_mu = threading.Lock()


def _route_attempts_lock_for(route_id: str) -> threading.Lock:
    with _route_attempts_locks_mu:
        lk = _route_attempts_locks.get(route_id)
        if lk is None:
            lk = threading.Lock()
            _route_attempts_locks[route_id] = lk
        return lk


def _route_attempts_cache_key(route: dict,
                                trail_match_fp: tuple | None = None) -> tuple:
    """Anything in this tuple that changes triggers a fresh compute.

    Windows note: in-place file replacement (`tmp.replace(dst)`) does NOT
    bump the parent directory's mtime, so a directory-mtime key would
    serve stale data after every trail_match rewrite. Use (file_count,
    max_file_mtime) of the trail_match cache contents — both update
    reliably on overwrite.

    `trail_match_fp` lets the caller hoist the directory-scan cost out
    of a per-route loop — see `api_activity_routes_ridden`, which
    iterates every saved route on every request.
    """
    region_artifact_p = REGION_TRAILS_CACHE_DIR / f"{route['region_id']}.json"
    return (
        route_attempts.ROUTE_ATTEMPTS_VERSION,
        trail_match.TRAIL_MATCH_VERSION,
        route.get("modified") or route.get("created") or "",
        trail_match_fp if trail_match_fp is not None else _trail_match_dir_fingerprint(),
        _stat_mtime(METADATA_FILE),
        _stat_mtime(region_artifact_p),
    )


def _trail_match_dir_fingerprint() -> tuple[int, float]:
    """`(file_count, max_mtime)` of the trail_match cache directory.
    Both move on every new write — Windows-safe versus a directory-mtime
    check which doesn't tick on in-place file replacement."""
    if not TRAIL_MATCH_CACHE_DIR.exists():
        return (0, -1.0)
    count = 0
    max_mtime = -1.0
    for p in TRAIL_MATCH_CACHE_DIR.iterdir():
        if p.suffix != ".json":
            continue
        count += 1
        try:
            mt = p.stat().st_mtime
            if mt > max_mtime:
                max_mtime = mt
        except OSError:
            pass
    return (count, max_mtime)


def _get_route_attempts(route_id: str, route: dict | None = None,
                         trail_match_fp: tuple | None = None) -> dict | None:
    """Return {attempts, attempt_count, best_*} for `route_id`, building
    it if absent or stale. Returns None if the route doesn't exist.

    `trail_match_fp` is forwarded to `_route_attempts_cache_key` — pass
    a pre-computed fingerprint when calling this in a per-route loop
    (e.g. the routes-ridden endpoint) to avoid rescanning the
    trail_match cache directory once per saved route.
    """
    route = route or _load_route(route_id)
    if route is None:
        return None
    key = _route_attempts_cache_key(route, trail_match_fp=trail_match_fp)
    cached = _route_attempts_mem_cache.get(route_id)
    if cached is not None and _route_attempts_keys.get(route_id) == key:
        return cached

    with _route_attempts_lock_for(route_id):
        cached = _route_attempts_mem_cache.get(route_id)
        if cached is not None and _route_attempts_keys.get(route_id) == key:
            return cached

        # Try disk cache before recomputing.
        ROUTE_ATTEMPTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        disk = ROUTE_ATTEMPTS_CACHE_DIR / f"{route_id}.json"
        if disk.exists():
            try:
                entry = json.loads(disk.read_text(encoding="utf-8"))
                if tuple(entry.get("key") or ()) == key:
                    payload = entry.get("payload") or {}
                    _route_attempts_mem_cache[route_id] = payload
                    _route_attempts_keys[route_id]      = key
                    return payload
            except Exception:
                pass

        # Recompute. Region artifact is loaded via route_builder so we
        # get the same cached shape the /api/regions/... endpoint serves.
        region = _region_by_id(route["region_id"])
        if region is None:
            logger.warning("route %s references unknown region %s",
                            route_id, route.get("region_id"))
            empty = {"version": route_attempts.ROUTE_ATTEMPTS_VERSION,
                      "attempts": [], "attempt_count": 0,
                      "best_duration_sec": None, "best_filename": None,
                      "best_date": None}
            _route_attempts_mem_cache[route_id] = empty
            _route_attempts_keys[route_id]      = key
            return empty
        artifact = route_builder.get_region_artifact(
            region,
            artifacts_dir=REGION_TRAILS_CACHE_DIR,
            osm_paths_dir=OSM_PATHS_CACHE_DIR,
            osm_roads_dir=OSM_ROADS_CACHE_DIR,
        )
        payload = route_attempts.build_route_leaderboard(
            route, artifact, TRAIL_MATCH_CACHE_DIR,
            activity_loader=get_activity,
            activity_meta=load_metadata() or {},
        )
        try:
            tmp = disk.with_suffix(disk.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"key": list(key), "payload": payload},
                            ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(disk)
        except OSError as exc:
            logger.warning("Failed to persist route_attempts %s: %s", disk, exc)
        _route_attempts_mem_cache[route_id] = payload
        _route_attempts_keys[route_id]      = key
        return payload


def _invalidate_route_attempts(route_id: str | None = None) -> None:
    """Drop the in-memory route_attempts cache. If `route_id` is given,
    only that route's entry is dropped (used after PUT/DELETE on a single
    route). Otherwise clear everything (after rescan-all)."""
    if route_id is None:
        _route_attempts_mem_cache.clear()
        _route_attempts_keys.clear()
    else:
        _route_attempts_mem_cache.pop(route_id, None)
        _route_attempts_keys.pop(route_id, None)


# ─── Background prewarm of missing trail-match results ──────────────────────
# At startup we scan all MTB+Moose-Mountain rides and queue background
# compute for any without a current-version trail_match cache. This is
# what populates the leaderboard before the user clicks anything.
# Runs in a daemon thread so it doesn't block app.run(). Idempotent —
# repeat calls early-return immediately if the queue is empty.
#
# Triggered both at startup and after a sync (so new rides land on the
# leaderboard without a Flask restart). The thread serially walks the
# queue: trail-match is CPU-bound per file and parallelism would just
# starve the request handlers.

_TRAIL_PREWARM_RUNNING = False
_TRAIL_PREWARM_RESCAN  = False   # set when a trigger arrives mid-run
_trail_prewarm_lock    = threading.Lock()
# Progress for the user-facing rescan indicator (exposed by
# /api/trail-match/status). Guarded by _trail_prewarm_lock. `total`/`done`
# track the current (or most recent) pass; `finished_at` is stamped when the
# worker drops _TRAIL_PREWARM_RUNNING so the UI can show a final "done".
_trail_prewarm_progress = {"done": 0, "total": 0, "started_at": None, "finished_at": None}


def _meta_fp(file_meta: dict) -> str:
    """Short fingerprint of the per-file metadata that affects snapped GPX
    data (trim, smoothing, time-shift, spike-repair), passed to
    trail_match.cached_match so a metadata edit invalidates the trail cache
    even when the raw GPX mtime is unchanged. ALL call sites must use this
    so their MD5s agree — a mismatch silently recomputes."""
    return hashlib.md5(json.dumps({
        "trim":             file_meta.get("trim"),
        "smoothing":        file_meta.get("smoothing"),
        "time_shift_hours": file_meta.get("time_shift_hours"),
        "spike_repair":     file_meta.get("spike_repair"),
    }, sort_keys=True).encode()).hexdigest()[:12]


def _cached_match(filename: str, mtime: float, data: dict, meta_fp: str):
    """`trail_match.cached_match` bound to this app's three cache dirs. Callers
    still pass `mtime` explicitly — sites differ (`p.stat().st_mtime` on the
    request paths vs `_stat_mtime(p)` on the activity-detail path)."""
    return trail_match.cached_match(
        filename, mtime, data,
        cache_dir_osm=OSM_PATHS_CACHE_DIR,
        cache_dir_results=TRAIL_MATCH_CACHE_DIR,
        cache_dir_roads=OSM_ROADS_CACHE_DIR,
        meta_fp=meta_fp,
    )


def _effective_for_match(filename: str, data: dict, file_meta: dict,
                         eff_type: str | None = None) -> dict:
    """Apply the full effective-data pipeline (type re-segmentation, time-shift,
    trim, spike-repair, smoothing) to RAW parsed data before trail matching, so
    the snapped points match the trim/smoothing that `meta_fp` encodes.

    `trail_match.cached_match` keys on `meta_fp` (which fingerprints trim /
    smoothing / time-shift / spike-repair) but NOT on `data` itself — so every
    site that writes the cache must snap the SAME transformed points, or a site
    that snaps raw points writes an entry the others read back. That silently
    serves raw-track trail attempts for any ride carrying trim/smoothing/shift/
    spike-repair metadata (prewarm/rescan used to do exactly this).

    Pass `eff_type` when the caller already resolved it (the detail handler);
    otherwise it is resolved here the same way the detail handler does, off the
    RAW data, so all sites agree."""
    if eff_type is None:
        all_regions = load_regions()
        matched_regions = _effective_regions(data, file_meta, all_regions)
        eff_type = _effective_type_for(file_meta.get("type", ""), matched_regions,
                                       all_regions, data.get("date") or "")
    eff = _effective_data(filename, data, eff_type)
    if file_meta.get("time_shift_hours"):
        eff = _apply_time_shift(eff, file_meta["time_shift_hours"])
    trim = file_meta.get("trim") or {}
    if trim:
        eff = _apply_trim(eff, trim)
    if file_meta.get("spike_repair"):
        eff = _apply_spike_repair(eff)
    smoothing = file_meta.get("smoothing") or {}
    if isinstance(smoothing, dict) and int(smoothing.get("window") or 0) > 1:
        eff = _apply_smoothing(eff, smoothing)
    return eff


def _prewarm_trail_matches_async() -> None:
    """Idempotent with rescan-on-trigger semantics. If a worker is already
    running, set the rescan flag so the worker loops again after it
    finishes its current pass — otherwise a sync that lands mid-scan
    would silently drop the new ride."""
    global _TRAIL_PREWARM_RUNNING, _TRAIL_PREWARM_RESCAN
    with _trail_prewarm_lock:
        if _TRAIL_PREWARM_RUNNING:
            _TRAIL_PREWARM_RESCAN = True
            return
        _TRAIL_PREWARM_RUNNING = True
        _TRAIL_PREWARM_RESCAN  = False
    t = threading.Thread(target=_trail_prewarm_worker,
                         name="trail-prewarm", daemon=True)
    t.start()


def _trail_prewarm_worker() -> None:
    global _TRAIL_PREWARM_RUNNING, _TRAIL_PREWARM_RESCAN
    try:
        # Loop on rescan: if a trigger arrived while we were processing
        # the previous batch, do another pass. New files added during
        # that batch will surface in the next scan.
        while True:
            _trail_prewarm_worker_inner()
            with _trail_prewarm_lock:
                if not _TRAIL_PREWARM_RESCAN:
                    break
                _TRAIL_PREWARM_RESCAN = False
    finally:
        # Done in finally so an exception in the inner doesn't leave the
        # running flag stuck and block future triggers.
        with _trail_prewarm_lock:
            _TRAIL_PREWARM_RUNNING = False
            _trail_prewarm_progress["finished_at"] = time.time()


def _trail_prewarm_worker_inner() -> None:
    try:
        # Defer the imports/reads until the thread is alive so module
        # load isn't blocked by metadata.json/regions.json I/O.
        activities = all_activities()
    except Exception:
        logger.exception("trail prewarm: failed to enumerate activities")
        return

    meta = load_metadata()
    todo: list[str] = []
    for a in activities:
        if a.get("effective_type") != "mtb":
            continue
        fn = a.get("filename")
        if not fn:
            continue
        # Mirror the fingerprint logic from cached_match so the prewarm
        # queue is in sync with what cached_match would consider valid.
        # Without this, a trim edit would silently skip prewarm and
        # leave the leaderboard stale until the user opened the page.
        file_meta = meta.get(fn, {})
        expected_fp = _meta_fp(file_meta)
        disk = TRAIL_MATCH_CACHE_DIR / f"{fn}.json"
        if disk.exists():
            try:
                entry = json.loads(disk.read_text(encoding="utf-8"))
                if (entry.get("version") == trail_match.TRAIL_MATCH_VERSION
                        and entry.get("mtime") is not None
                        and (entry.get("meta_fp") or "") == expected_fp):
                    continue
            except Exception:
                pass
        todo.append(fn)

    # Publish the size of this pass so the rescan indicator can show a count.
    with _trail_prewarm_lock:
        _trail_prewarm_progress.update(done=0, total=len(todo),
                                       started_at=time.time(), finished_at=None)

    if not todo:
        logger.info("trail prewarm: nothing to do (%d MTB activities, all cached)",
                    len(activities))
        return

    logger.info("trail prewarm: %d/%d MTB activities need compute",
                len(todo), len(activities))
    meta = load_metadata()
    done = 0
    for fn in todo:
        try:
            p = _safe_gpx_path(fn)
            if p is None or not p.exists():
                continue
            data = get_activity(fn)
            if data is None:
                continue
            # Same metadata fingerprint logic as the live route — keeps
            # cache keys in sync so prewarm and request paths don't
            # accidentally produce two different cache entries for the
            # same activity.
            file_meta = meta.get(fn, {})
            meta_fp = _meta_fp(file_meta)
            # Snap the SAME transformed points the detail view fingerprints —
            # these rides are MTB by construction (filtered above), so passing
            # eff_type explicitly skips re-resolving it. Snapping raw `data`
            # here would poison the cache the detail view reads back.
            match_data = _effective_for_match(fn, data, file_meta, "mtb")
            _cached_match(fn, p.stat().st_mtime, match_data, meta_fp)
            done += 1
            with _trail_prewarm_lock:
                _trail_prewarm_progress["done"] = done
            # Each successful compute invalidates the leaderboard so the
            # next /api/trails/leaderboard or /api/activity request sees
            # the new ride.
            _invalidate_trail_leaderboards()
        except Exception:
            logger.exception("trail prewarm: cached_match failed for %s", fn)
    # Once at the end so the /routes page can hit a warm cache during
    # prewarm windows. Individual rescans still invalidate explicitly.
    _invalidate_route_attempts()
    logger.info("trail prewarm: processed %d/%d", done, len(todo))


@app.route("/api/trails/regions")
def api_trail_regions():
    """All completed trail attempts grouped by region.

    Returns a list of region cards, each containing the region's name +
    polygon-derived metadata + the trails ridden in that region with
    their per-region attempt count and best time. Powers the /trails
    page.

    Trails appearing in rides across multiple regions show up under each
    region with that region's local stats — see build_region_trail_index.
    """
    regions = load_regions()
    region_by_id = {r["id"]: r for r in regions}
    # Build {filename: [region_id, ...]} from the activities list so the
    # aggregator doesn't need to recompute centroids.
    acts = all_activities()
    activity_regions = {
        (a.get("filename") or ""): (a.get("regions") or [])
        for a in acts
    }
    index = _get_region_trail_index(activity_regions)
    # Hidden trails are a /trails-page-only preference (file-backed). Flag
    # them rather than dropping them so the client's "Show hidden" toggle can
    # reveal/restore them; leaderboards + log-detail timelines are unaffected.
    hidden = _load_hidden_trails()
    out = []
    for region_id, trails_dict in index.items():
        region = region_by_id.get(region_id)
        if region is None:
            continue   # stale region id from a deleted polygon
        # trails_dict is keyed on (name, direction). Each value already
        # carries its own `name` and `direction` fields from the
        # aggregator. Sort by attempts desc, then by name, then direction.
        # Shallow-copy each so the per-request `hidden` flag never mutates
        # the (possibly cached) aggregator dicts.
        trails = [
            {**t, "hidden": f"{t['name']}|{t.get('direction') or 'mixed'}" in hidden}
            for t in sorted(
                trails_dict.values(),
                key=lambda t: (-t["attempts"], t["name"], t.get("direction", "")),
            )
        ]
        # Canonical OSM trail length (stable, matches the map). The client
        # falls back to best_distance_km when this is absent.
        osm_lengths = _region_osm_lengths(region_id)
        for t in trails:
            km = osm_lengths.get(t["name"])
            if km:
                t["osm_length_km"] = round(km, 2)
        out.append({
            "id":     region_id,
            "name":   region.get("name") or region_id,
            "color":  region.get("color"),
            "trails": trails,
            "total_attempts": sum(t["attempts"] for t in trails),
            "trail_count":    len(trails),
        })
    # Sort regions by total attempts desc — the regions you ride most
    # appear at the top.
    out.sort(key=lambda r: (-r["total_attempts"], r["name"]))
    return jsonify({"regions": out})


# ── Hidden trails — a /trails-page-only display preference ────────────────
# File-backed (survives cache clears / browser changes) and flagged at serve
# time in api_trail_regions. Deliberately scoped to the Trails list: the
# leaderboards, log-detail "Trails Ridden" timeline, and stats all ignore it.
TRAIL_HIDDEN_CACHE_DIR = CACHE_DIR / "trail_dismissals"


def _trail_hidden_path() -> Path:
    return TRAIL_HIDDEN_CACHE_DIR / "hidden.json"


def _load_hidden_trails() -> set:
    """Set of "name|direction" keys the rider has hidden from /trails."""
    try:
        return set(json.loads(_trail_hidden_path().read_text(encoding="utf-8")) or [])
    except Exception:
        return set()


def _save_hidden_trails(keys: set) -> None:
    TRAIL_HIDDEN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(_trail_hidden_path(), json.dumps(sorted(keys), ensure_ascii=False))


def _hidden_trail_key_from_request():
    """(name, "name|direction") from the JSON body, direction defaulting to mixed."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    direction = (body.get("direction") or "mixed").strip() or "mixed"
    return name, f"{name}|{direction}"


@app.route("/api/trails/hidden", methods=["POST"])
def api_trail_hide():
    """Hide a (trail, direction) from the /trails list."""
    name, key = _hidden_trail_key_from_request()
    if not name:
        abort(400)
    keys = _load_hidden_trails()
    keys.add(key)
    _save_hidden_trails(keys)
    return jsonify({"ok": True, "hidden_count": len(keys)})


@app.route("/api/trails/hidden", methods=["DELETE"])
def api_trail_unhide():
    """Restore a previously hidden (trail, direction)."""
    name, key = _hidden_trail_key_from_request()
    keys = _load_hidden_trails()
    keys.discard(key)
    _save_hidden_trails(keys)
    return jsonify({"ok": True, "hidden_count": len(keys)})


# Canonical OSM trail lengths, read from the already-cached region artifact
# (no Overpass build), memoized by file mtime. Used to show a stable trail
# distance on /trails — independent of any one ride's GPS.
_REGION_OSM_LENGTH_MEMO = {}   # region_id -> (mtime, {name: length_km})


def _region_osm_lengths(region_id: str) -> dict:
    """{trail/road name: length_km} from the cached region artifact, or {}
    if the region has no cached geometry yet (caller falls back to the
    ridden best-attempt distance)."""
    ap = REGION_TRAILS_CACHE_DIR / f"{region_id}.json"
    try:
        mtime = ap.stat().st_mtime
    except OSError:
        return {}
    memo = _REGION_OSM_LENGTH_MEMO.get(region_id)
    if memo and memo[0] == mtime:
        return memo[1]
    try:
        art = json.loads(ap.read_text(encoding="utf-8"))
    except Exception:
        return {}
    lengths = {}
    for entry in (art.get("trails") or []) + (art.get("roads") or []):
        name = entry.get("name")
        if name:
            lengths[name] = (entry.get("total_length_m") or 0.0) / 1000.0
    _REGION_OSM_LENGTH_MEMO[region_id] = (mtime, lengths)
    return lengths


@app.route("/api/activity/<path:filename>/rescan-trails", methods=["POST"])
def api_activity_rescan_trails(filename: str):
    """Recompute the trail_match result for a single activity, inline.

    Deletes the per-file cache + recomputes (so a fresh result is
    returned synchronously). Used by the rescan button on the activity
    detail page after a route is saved or the matcher changes.
    """
    p = _safe_gpx_path(filename)
    if p is None or not p.exists():
        abort(404)
    disk = TRAIL_MATCH_CACHE_DIR / f"{filename}.json"
    if disk.exists():
        try:
            disk.unlink()
        except OSError as exc:
            logger.warning("Failed to delete trail_match cache %s: %s", disk, exc)
    # Force-evict from the in-memory result cache too.
    with trail_match._RESULT_MEM_LOCK:
        for k in list(trail_match._RESULT_MEM_CACHE):
            if k[0] == filename:
                trail_match._RESULT_MEM_CACHE.pop(k, None)
    data = parse_gpx(p)
    if data is None:
        return jsonify({"error": "could not parse GPX"}), 500
    file_meta = (load_metadata() or {}).get(filename) or {}
    # KEEP IN SYNC with the prewarm + activity-detail call sites — using
    # different keys here would produce a different MD5 and the next
    # request would silently recompute against the matching-shape entry.
    # The snapped DATA must also match: transform to effective points before
    # matching, or this writes a raw-snapped entry the detail view reads back.
    meta_fp = _meta_fp(file_meta)
    match_data = _effective_for_match(filename, data, file_meta)
    result = _cached_match(filename, p.stat().st_mtime, match_data, meta_fp)
    _invalidate_trail_leaderboards()
    _invalidate_route_attempts()   # one ride's match changed → any route attempt may have changed
    return jsonify({"ok": True, "result": result})


def _route_proposal_for_ride(filename: str, data: dict | None = None
                             ) -> tuple[dict | None, str | None, int]:
    """Build an unsaved route proposal from a ride's detected trail timeline.

    The reverse of attempt detection: take the trails the matcher snapped
    this ride onto and rebuild them as ordered edge segments (see
    `route_attempts.build_segments_from_ride`), picking the region whose
    graph resolves the most segments. Returns `(proposal, None, 200)` on
    success, else `(None, error_message, status)`. Shared by the per-ride
    endpoint and the recurring-route suggestions builder."""
    p = _safe_gpx_path(filename)
    if p is None or not p.exists():
        return None, "not found", 404
    if data is None:
        data = get_activity(filename)
    if data is None:
        return None, "could not parse GPX", 500

    file_meta = (load_metadata() or {}).get(filename) or {}
    # Snap (and build segments below, off data["points"]) on the same transformed
    # points the detail view caches under this meta_fp — see _effective_for_match.
    # Both callers pass data=None, so this never double-applies the pipeline.
    data = _effective_for_match(filename, data, file_meta)
    meta_fp = _meta_fp(file_meta)
    result = _cached_match(filename, p.stat().st_mtime, data, meta_fp)
    timeline = (result or {}).get("timeline") or []
    if not timeline:
        return None, "No trails detected in this ride — nothing to build a route from.", 422

    # Candidate regions: the ride's matched regions first; fall back to all.
    act = next((a for a in all_activities() if a.get("filename") == filename), None)
    region_ids = list((act or {}).get("regions") or []) or [r["id"] for r in load_regions()]

    # Pick the region whose route graph resolves the most segments — a ride
    # tagged with two overlapping regions should build against the one that
    # actually owns the trails it rode.
    best_segs: list[dict] = []
    best_region: str | None = None
    for rid in region_ids:
        region = _region_by_id(rid)
        if region is None:
            continue
        try:
            art = route_builder.get_region_artifact(
                region,
                artifacts_dir=REGION_TRAILS_CACHE_DIR,
                osm_paths_dir=OSM_PATHS_CACHE_DIR,
                osm_roads_dir=OSM_ROADS_CACHE_DIR,
            )
        except Exception:
            continue
        segs = route_attempts.build_segments_from_ride(timeline, data["points"], art)
        if len(segs) > len(best_segs):
            best_segs, best_region = segs, rid

    if not best_segs:
        return None, "Detected trails couldn't be mapped to a region's route graph.", 422

    # Suggested name: the de-duplicated consecutive trail-name chain.
    chain: list[str] = []
    for s in best_segs:
        if not chain or chain[-1] != s["trail_name"]:
            chain.append(s["trail_name"])
    name = " → ".join(chain)
    if len(name) > 80:
        name = " → ".join(chain[:3]) + f" → +{len(chain) - 3} more"

    return ({"region_id": best_region, "name": name,
             "segments": best_segs, "trail_chain": chain}, None, 200)


@app.route("/api/activity/<path:filename>/route-suggestion")
def api_activity_route_suggestion(filename: str):
    """Propose a route built from a ride's detected trail timeline (unsaved).

    The builder opens it pre-filled (`/routes/edit?from=<filename>`) so the
    rider can trim detours / trailing trails before saving."""
    proposal, err, status = _route_proposal_for_ride(filename)
    if proposal is None:
        if status == 404:
            abort(404)
        return jsonify({"error": err}), status
    return jsonify({"version": ROUTES_API_VERSION, "filename": filename, **proposal})


@app.route("/api/trail-match/rescan-all", methods=["POST"])
def api_trail_match_rescan_all():
    """Wipe every per-file trail_match cache + kick off a background
    prewarm. Returns immediately; status is observable via the existing
    prewarm log lines and the trail leaderboards refreshing.

    Use after bumping the matcher (a TRAIL_MATCH_VERSION bump already
    forces recompute via cache-version checks, but this endpoint is the
    explicit user-triggered "redo everything" path)."""
    removed = 0
    if TRAIL_MATCH_CACHE_DIR.exists():
        for p in TRAIL_MATCH_CACHE_DIR.glob("*.json"):
            try:
                p.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("Failed to delete %s during rescan-all: %s", p, exc)
    with trail_match._RESULT_MEM_LOCK:
        trail_match._RESULT_MEM_CACHE.clear()
    _invalidate_trail_leaderboards()
    _invalidate_route_attempts()   # blanket wipe; prewarm-tail will re-warm
    _prewarm_trail_matches_async()
    return jsonify({"ok": True, "removed": removed,
                     "message": "Rescan started — prewarm runs in background."})


@app.route("/api/trail-match/status")
def api_trail_match_status():
    """Progress of the background trail-match prewarm (what 'Rescan all logs'
    kicks off). `running` flips false when the worker finishes; `done`/`total`
    track the current pass so the UI can show a live count and a final done."""
    with _trail_prewarm_lock:
        running = _TRAIL_PREWARM_RUNNING
        prog = dict(_trail_prewarm_progress)
    return jsonify({"running": running, **prog})


# ─── Route builder ──────────────────────────────────────────────────────────
# Saved routes are user-defined orderings of trail segments within a region
# (see route_builder.py for the upstream artifact that powers selection).
# Bump ROUTES_API_VERSION alongside route_builder.ROUTE_BUILDER_VERSION
# whenever the wire shape changes — clients use it to invalidate caches.
ROUTES_API_VERSION       = 6   # v6: added 50-point preview polyline to the list payload
REGION_TRAILS_CACHE_DIR  = CACHE_DIR / "region_trails"
# OSM_ROADS_CACHE_DIR moved up next to OSM_PATHS_CACHE_DIR — see there.
ROUTES_DIR               = CACHE_DIR / "routes"
ROUTE_ATTEMPTS_CACHE_DIR = CACHE_DIR / "route_attempts"
ROUTE_SUGGESTIONS_CACHE_DIR = CACHE_DIR / "route_suggestions"

# The id charset we mint ourselves for routes / suggestions. Validating
# against it before touching the filesystem defends against path traversal
# from a crafted client-supplied id.
_ID_RE = re.compile(r"[a-f0-9]{12}")

# Single-payload in-memory cache for the recurring-route suggestions
# (the whole library produces one result, unlike per-route attempts).
# Clustering the full library is a multi-second pass, so it runs in a
# background thread; requests return the last-known payload with a
# `computing` flag and the page polls until it settles.
_route_suggestions_mem: dict = {"key": None, "payload": None}
_route_suggestions_lock = threading.Lock()
_SUGGESTIONS_COMPUTING = False


def _route_path(route_id: str) -> Path:
    # Allow only the id charset we generate ourselves — defends against
    # path traversal if a client supplies a crafted id.
    if not _ID_RE.fullmatch(route_id or ""):
        return None
    return ROUTES_DIR / f"{route_id}.json"


def _load_route(route_id: str) -> dict | None:
    p = _route_path(route_id)
    if p is None or not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _all_routes() -> list[dict]:
    ROUTES_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in ROUTES_DIR.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    out.sort(key=lambda r: r.get("modified") or r.get("created") or "", reverse=True)
    return out


def _validate_route_payload(payload) -> tuple[dict | None, str | None]:
    """Return (cleaned_payload, error_message)."""
    if not isinstance(payload, dict):
        return None, "payload must be a JSON object"
    name = (payload.get("name") or "").strip()
    if not name:
        return None, "name is required"
    region_id = (payload.get("region_id") or "").strip()
    if not region_id:
        return None, "region_id is required"
    if _region_by_id(region_id) is None:
        return None, f"unknown region_id {region_id!r}"
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        return None, "segments must be a non-empty list"
    clean_segments = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            return None, f"segments[{i}] must be an object"
        trail_name = (seg.get("trail_name") or "").strip()
        if not trail_name:
            return None, f"segments[{i}].trail_name is required"
        direction = seg.get("direction") or "forward"
        # Accept the full trail_match direction taxonomy (forward/reverse
        # for linear-untagged + loops; up/down for sloped linears). The
        # builder writes forward/reverse today; the wider set future-proofs
        # routes saved after a builder upgrade.
        if direction not in ("forward", "reverse", "up", "down"):
            return None, f"segments[{i}].direction must be one of forward/reverse/up/down"
        cleaned = {"trail_name": trail_name, "direction": direction}
        kind = seg.get("kind")
        if kind in ("trail", "road"):
            cleaned["kind"] = kind
        # Carry edge/junction bounds through. In the edge-based model the
        # client always supplies edge_id + the two junctions; older route
        # docs from before this migration may only have the junctions.
        eid = seg.get("edge_id")
        sj  = seg.get("start_junction"); ej = seg.get("end_junction")
        if eid:
            cleaned["edge_id"] = eid
        if sj and ej:
            cleaned["start_junction"] = sj
            cleaned["end_junction"]   = ej
        clean_segments.append(cleaned)
    return {"name": name, "region_id": region_id, "segments": clean_segments}, None


@app.route("/api/regions/<region_id>/trails-geometry")
def api_region_trails_geometry(region_id: str):
    """Return the cached trails + roads + junctions artifact for `region_id`.

    ETag-tagged with the on-disk artifact mtime so back-to-back navigations
    to different routes in the same region hit the browser cache (304)
    instead of re-downloading ~600 KB. Cache-Control: no-cache forces a
    revalidation each request — cheap, since the server-side artifact read
    is from memory after first build."""
    region = _region_by_id(region_id)
    if region is None:
        abort(404)
    force = request.args.get("force") == "1"
    ap = REGION_TRAILS_CACHE_DIR / f"{region_id}.json"
    # Fast path: satisfy a revalidation from the on-disk mtime alone, so a 304
    # never builds or parses the ~600 KB artifact. The etag uses the current
    # ROUTE_BUILDER_VERSION; a stale-version on-disk file won't match the
    # client's cached etag, falling through to a rebuild below.
    if not force and ap.exists():
        try:
            etag = f"{ap.stat().st_mtime:.3f}-{route_builder.ROUTE_BUILDER_VERSION}"
            if request.if_none_match.contains(etag):
                return Response(status=304)
        except OSError:
            pass
    artifact = route_builder.get_region_artifact(
        region,
        artifacts_dir=REGION_TRAILS_CACHE_DIR,
        osm_paths_dir=OSM_PATHS_CACHE_DIR,
        osm_roads_dir=OSM_ROADS_CACHE_DIR,
        force_rebuild=force,
    )
    try:
        etag = f"{ap.stat().st_mtime:.3f}-{artifact.get('version', 0)}"
    except OSError:
        etag = str(artifact.get("version", "0"))
    if request.if_none_match.contains(etag):
        return Response(status=304)
    resp = jsonify({"version": ROUTES_API_VERSION, **artifact})
    resp.set_etag(etag)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def _iter_region_edges(artifact: dict):
    """Yield every edge dict across the artifact's trails + roads collections."""
    for collection in (artifact.get("trails") or [], artifact.get("roads") or []):
        for entry in collection:
            yield from (entry.get("edges") or [])


@app.route("/api/regions/<region_id>/edges-summary")
def api_region_edges_summary(region_id: str):
    """Return just the edge id + length list for `region_id`.

    The full artifact is ~600 KB (polylines dominate). The /routes list
    page only needs edge_id -> length_m to compute per-route distance
    totals, so this slim endpoint cuts the payload ~40x. Still builds the
    artifact server-side (so the cache stays warm), then strips."""
    region = _region_by_id(region_id)
    if region is None:
        abort(404)
    artifact = route_builder.get_region_artifact(
        region,
        artifacts_dir=REGION_TRAILS_CACHE_DIR,
        osm_paths_dir=OSM_PATHS_CACHE_DIR,
        osm_roads_dir=OSM_ROADS_CACHE_DIR,
    )
    edges = [{"id": e["id"], "length_m": e["length_m"]}
             for e in _iter_region_edges(artifact)]
    return jsonify({"version": ROUTES_API_VERSION,
                     "region_id": region_id, "edges": edges})


def _downsample_points(items: list, n: int, get) -> list:
    """Downsample `items` to n+1 [lat,lon] pairs (n evenly-spaced plus the
    final item, so the true endpoint is always represented). `get(item)`
    yields the (lat, lon) pair. Inputs of <= n items are returned as-is."""
    if not items:
        return []
    if len(items) <= n:
        return [[*get(it)] for it in items]
    step = len(items) / n
    out = [[*get(items[int(i * step)])] for i in range(n)]
    out.append([*get(items[-1])])
    return out


def _downsample_latlon(coords: list, n: int = 50) -> list:
    """Downsample a [[lat,lon],...] list to n+1 points. Sibling of
    `_downsample_polyline`, but operates on coordinate pairs rather than
    GPX point dicts."""
    return _downsample_points(coords, n, lambda c: (c[0], c[1]))


def _region_edge_polylines(region_id: str) -> dict:
    """edge_id -> polyline ([[lat,lon],...]) for one region, read from the
    cached trails+roads artifact (in-memory after first build). Returns {}
    if the region or its artifact is unavailable."""
    region = _region_by_id(region_id)
    if region is None:
        return {}
    try:
        artifact = route_builder.get_region_artifact(
            region,
            artifacts_dir=REGION_TRAILS_CACHE_DIR,
            osm_paths_dir=OSM_PATHS_CACHE_DIR,
            osm_roads_dir=OSM_ROADS_CACHE_DIR,
        )
    except Exception:
        return {}
    out = {e["id"]: (e.get("polyline") or []) for e in _iter_region_edges(artifact)}
    return out


def _region_edge_lengths(region_id: str) -> dict:
    """edge_id -> length_m for one region, from the cached trails+roads artifact
    (in-memory after first build). Returns {} if the region or its artifact is
    unavailable. Lets the /routes list compute per-route distance server-side
    instead of each client re-fetching the edges-summary."""
    region = _region_by_id(region_id)
    if region is None:
        return {}
    try:
        artifact = route_builder.get_region_artifact(
            region,
            artifacts_dir=REGION_TRAILS_CACHE_DIR,
            osm_paths_dir=OSM_PATHS_CACHE_DIR,
            osm_roads_dir=OSM_ROADS_CACHE_DIR,
        )
    except Exception:
        return {}
    return {e["id"]: (e.get("length_m") or 0.0) for e in _iter_region_edges(artifact)}


def _route_preview_polyline(route: dict, edge_polys: dict, n: int = 50) -> list:
    """Concatenate the route's edge polylines in segment order and downsample
    to ~n points for a list-page mini-map. Segments whose edge_id is missing
    from the artifact (stale ids) are skipped — the preview shows whatever
    geometry is still resolvable."""
    coords = []
    for seg in route.get("segments") or []:
        pl = edge_polys.get(seg.get("edge_id"))
        if pl:
            coords.extend(pl)
    return _downsample_latlon(coords, n)


@app.route("/api/routes", methods=["GET"])
def api_routes_list():
    """List routes, enriched with attempt count + best time + a 50-point
    preview polyline per route.

    Stats come from `_get_route_attempts` which is cached on disk + in
    memory; a warm list page fires no trail_match recomputes. The preview
    polyline is built server-side from the cached region artifact (one edge
    map per region, shared across that region's routes) so the list page
    never has to pull the ~600 KB geometry artifact to draw thumbnails."""
    routes = _all_routes()
    edge_cache: dict[str, dict] = {}    # region_id -> {edge_id: polyline}, once per region
    length_cache: dict[str, dict] = {}  # region_id -> {edge_id: length_m}, once per region
    out = []
    for r in routes:
        rid = r["id"]
        stats = _get_route_attempts(rid, route=r) or {}
        region_id = r.get("region_id")
        if region_id not in edge_cache:
            edge_cache[region_id]   = _region_edge_polylines(region_id)
            length_cache[region_id] = _region_edge_lengths(region_id)
        lengths = length_cache[region_id]
        # Per-route distance, summed server-side from the edge map we already
        # built for the preview polyline — saves the client an edges-summary
        # round-trip per region on every render.
        distance_m = sum(lengths.get(seg.get("edge_id"), 0.0)
                         for seg in (r.get("segments") or []))
        out.append({
            **r,
            "polyline":          _route_preview_polyline(r, edge_cache[region_id]),
            "distance_m":        distance_m,
            "attempts":          stats.get("attempt_count", 0),
            "best_duration_sec": stats.get("best_duration_sec"),
            "best_filename":     stats.get("best_filename"),
            "best_date":         stats.get("best_date"),
        })
    return jsonify({"version": ROUTES_API_VERSION, "routes": out})


@app.route("/api/routes", methods=["POST"])
def api_routes_create():
    cleaned, err = _validate_route_payload(request.get_json(silent=True))
    if err:
        return jsonify({"error": err}), 400
    ROUTES_DIR.mkdir(parents=True, exist_ok=True)
    rid = secrets.token_hex(6)
    while (ROUTES_DIR / f"{rid}.json").exists():
        rid = secrets.token_hex(6)
    now = datetime.now(timezone.utc).isoformat()
    route = {"id": rid, "created": now, "modified": now, **cleaned}
    _atomic_write(ROUTES_DIR / f"{rid}.json", json.dumps(route, ensure_ascii=False))
    _invalidate_route_attempts(rid)
    return jsonify(route), 201


@app.route("/api/routes/<route_id>", methods=["GET"])
def api_routes_get(route_id: str):
    route = _load_route(route_id)
    if route is None:
        abort(404)
    return jsonify(route)


@app.route("/api/routes/<route_id>", methods=["PUT"])
def api_routes_update(route_id: str):
    existing = _load_route(route_id)
    if existing is None:
        abort(404)
    cleaned, err = _validate_route_payload(request.get_json(silent=True))
    if err:
        return jsonify({"error": err}), 400
    route = {
        "id":       route_id,
        "created":  existing.get("created"),
        "modified": datetime.now(timezone.utc).isoformat(),
        **cleaned,
    }
    _atomic_write(ROUTES_DIR / f"{route_id}.json", json.dumps(route, ensure_ascii=False))
    _invalidate_route_attempts(route_id)
    return jsonify(route)


@app.route("/api/routes/<route_id>", methods=["DELETE"])
def api_routes_delete(route_id: str):
    p = _route_path(route_id)
    if p is None or not p.exists():
        abort(404)
    try:
        p.unlink()
    except OSError as exc:
        logger.warning("Failed to delete route %s: %s", route_id, exc)
        return jsonify({"error": "delete failed"}), 500
    _invalidate_route_attempts(route_id)
    # Tidy: remove the orphan attempts file too, if it exists. The cache
    # key check would never re-hit it (route is gone), so it's just dead
    # bytes — but tidy is cheap.
    disk = ROUTE_ATTEMPTS_CACHE_DIR / f"{route_id}.json"
    if disk.exists():
        try: disk.unlink()
        except OSError: pass
    return jsonify({"ok": True})


@app.route("/api/routes/<route_id>/attempts", methods=["GET"])
def api_routes_attempts(route_id: str):
    """Full attempts list for one route — same data the list endpoint
    aggregates, but with the per-attempt rows kept intact for the
    detail page leaderboard."""
    route = _load_route(route_id)
    if route is None:
        abort(404)
    payload = _get_route_attempts(route_id, route=route) or {}
    return jsonify({"version": ROUTES_API_VERSION, "route_id": route_id, **payload})


# ─── Recurring-route suggestions ────────────────────────────────────────────
# Auto-discover loops the rider has done >= 2 times by clustering rides on
# GPS-footprint similarity (route_suggestions.py), then propose saving the
# cluster's most-typical ride as a route. Cached as one payload keyed on
# the ride set + saved routes + region polygons.

def _routes_dir_fingerprint() -> tuple[int, float]:
    """`(file_count, max_mtime)` of the saved-routes dir — moves whenever a
    route is created/edited/deleted so the suggestions exclusion stays
    fresh. Windows-safe (in-place replace doesn't tick dir mtime)."""
    if not ROUTES_DIR.exists():
        return (0, -1.0)
    count = 0
    max_mtime = -1.0
    for p in ROUTES_DIR.glob("*.json"):
        count += 1
        try:
            max_mtime = max(max_mtime, p.stat().st_mtime)
        except OSError:
            pass
    return (count, max_mtime)


def _suggestions_cache_key() -> tuple:
    """Anything here changing triggers a fresh clustering pass."""
    return (
        route_suggestions.SUGGESTIONS_VERSION,
        trail_match.TRAIL_MATCH_VERSION,
        _trail_match_dir_fingerprint(),   # ride set + per-ride edits
        _routes_dir_fingerprint(),        # saved-route exclusion
        _stat_mtime(METADATA_FILE),                # trims / regions_pinned
        _stat_mtime(REGIONS_FILE),                 # region polygons -> bucketing
    )


def _bbox_from_latlon(coords: list) -> list | None:
    """Coarse [min_lat,min_lon,max_lat,max_lon] from a [[lat,lon],...] list
    (the cached 50-pt preview polyline) — enough for the prefilter gate,
    and free since it's already in the sidebar entry."""
    if not coords:
        return None
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    return [min(lats), min(lons), max(lats), max(lons)]


def _saved_route_cellsets(region_id: str) -> list:
    """Cell sets of every saved route in `region_id`, for the
    'cluster already saved?' exclusion."""
    edge_polys = _region_edge_polylines(region_id)
    out = []
    for r in _all_routes():
        if r.get("region_id") != region_id:
            continue
        coords = []
        for seg in r.get("segments") or []:
            coords.extend(edge_polys.get(seg.get("edge_id")) or [])
        if coords:
            pts = [{"lat": c[0], "lon": c[1]} for c in coords]
            out.append(route_suggestions.ride_cell_set(pts, stride=1))
    return out


def _build_route_suggestions() -> dict:
    """Cluster the MTB library into recurring loops and build a savable
    proposal for each cluster not already covered by a saved route."""
    acts = all_activities()
    entries: dict[str, dict] = {}
    rides: list[dict] = []
    for a in acts:
        if a.get("effective_type") != "mtb" or a.get("excluded"):
            continue
        fn = a.get("filename")
        pl = a.get("polyline") or []
        dist = (a.get("stats") or {}).get("distance_km")
        bbox = _bbox_from_latlon(pl)
        if not fn or not bbox or not dist:
            continue
        entries[fn] = a
        rides.append({"filename": fn, "regions": a.get("regions") or [],
                      "bbox": bbox, "distance_km": dist,
                      "date": (a.get("date") or "")[:10]})

    cell_cache: dict[str, frozenset] = {}

    def loader(fn: str):
        if fn not in cell_cache:
            d = get_activity(fn)
            cell_cache[fn] = route_suggestions.ride_cell_set((d or {}).get("points") or [])
        return cell_cache[fn]

    clusters = route_suggestions.cluster_rides(rides, loader)

    region_names = {r["id"]: r.get("name") for r in load_regions()}
    route_cellsets_by_region: dict[str, list] = {}
    edge_polys_by_region: dict[str, dict] = {}
    suggestions: list[dict] = []
    skipped_no_region = skipped_no_artifact = skipped_unbuildable = 0

    for c in clusters:
        region_id = c["region_id"]
        # A cluster with no region can't be attributed to a route graph,
        # and building it would fan out across every region (live OSM
        # fetches). Skip.
        if region_id is None:
            skipped_no_region += 1
            continue
        # Only work from ALREADY-CACHED region artifacts — never trigger an
        # Overpass fetch during the suggestions pass.
        if not (REGION_TRAILS_CACHE_DIR / f"{region_id}.json").exists():
            skipped_no_artifact += 1
            continue

        # Exclude clusters already captured by a saved route.
        if region_id not in route_cellsets_by_region:
            route_cellsets_by_region[region_id] = _saved_route_cellsets(region_id)
        if route_suggestions.cluster_covered_by_route(
                c["representative_cells"], route_cellsets_by_region[region_id]):
            continue

        # Build the pre-selected route from the cluster's medoid ride.
        proposal, _err, _status = _route_proposal_for_ride(c["representative"])
        if proposal is None:
            skipped_unbuildable += 1
            continue
        prop_region = proposal.get("region_id") or region_id
        if prop_region not in edge_polys_by_region:
            edge_polys_by_region[prop_region] = _region_edge_polylines(prop_region)
        preview = _route_preview_polyline(
            {"segments": proposal["segments"]}, edge_polys_by_region[prop_region])

        members = []
        for fn in c["members"]:
            e = entries.get(fn) or {}
            members.append({
                "filename": fn,
                "date": (e.get("date") or "")[:10],
                "distance_km": (e.get("stats") or {}).get("distance_km"),
                "polyline": e.get("polyline") or [],
            })
        members.sort(key=lambda m: m["date"])

        sug_id = hashlib.md5(
            "|".join([prop_region or ""] + c["members"]).encode()
        ).hexdigest()[:12]
        suggestions.append({
            "id": sug_id,
            "region_id": prop_region,
            "region_name": region_names.get(prop_region),
            "ride_count": c["size"],
            "representative": c["representative"],
            "name": proposal["name"],
            "segments": proposal["segments"],
            "trail_chain": proposal.get("trail_chain") or [],
            "preview_polyline": preview,
            "rides": members,
        })

    suggestions.sort(key=lambda s: (-s["ride_count"], s["name"]))
    logger.info(
        "route suggestions: %d clusters -> %d suggestions "
        "(skipped: %d no-region, %d uncached-artifact, %d unbuildable)",
        len(clusters), len(suggestions),
        skipped_no_region, skipped_no_artifact, skipped_unbuildable)
    return {"computed_at": datetime.now(timezone.utc).isoformat(),
            "suggestions": suggestions}


def _suggestions_from_cache(key: tuple) -> dict | None:
    """Return the cached suggestions payload if mem or disk holds the
    current `key`, else None. Populates mem from disk on a hit."""
    mem = _route_suggestions_mem
    if mem["key"] == key and mem["payload"] is not None:
        return mem["payload"]
    disk = ROUTE_SUGGESTIONS_CACHE_DIR / "clusters.json"
    if disk.exists():
        try:
            entry = json.loads(disk.read_text(encoding="utf-8"))
            # Compare in JSON-normalized form: the key has nested tuples
            # (dir fingerprints) that serialize to lists, so a raw tuple
            # compare against the round-tripped key always misses.
            if entry.get("key") == json.loads(json.dumps(key)):
                payload = entry.get("payload") or {}
                _route_suggestions_mem.update(key=key, payload=payload)
                return payload
        except Exception:
            pass
    return None


def _compute_and_store_suggestions() -> dict:
    payload = _build_route_suggestions()
    # Key is read AFTER the build: building a proposal can lazily write a
    # trail_match file (via cached_match), which bumps the trail_match dir
    # fingerprint. Keying on the pre-build state would store under a key
    # that's already stale, forcing a recompute on every request. The
    # post-build key is stable because a second build finds those files
    # cached and writes nothing.
    key = _suggestions_cache_key()
    ROUTE_SUGGESTIONS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    disk = ROUTE_SUGGESTIONS_CACHE_DIR / "clusters.json"
    try:
        tmp = disk.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"key": list(key), "payload": payload},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(disk)
    except OSError as exc:
        logger.warning("Failed to persist route_suggestions: %s", exc)
    _route_suggestions_mem.update(key=key, payload=payload)
    return payload


def _maybe_start_suggestions_worker() -> None:
    """Kick off a background clustering pass unless one is already running."""
    global _SUGGESTIONS_COMPUTING
    with _route_suggestions_lock:
        if _SUGGESTIONS_COMPUTING:
            return
        _SUGGESTIONS_COMPUTING = True

    def run():
        global _SUGGESTIONS_COMPUTING
        try:
            _compute_and_store_suggestions()
        except Exception:
            logger.exception("route suggestions compute failed")
        finally:
            with _route_suggestions_lock:
                _SUGGESTIONS_COMPUTING = False

    threading.Thread(target=run, name="route-suggestions", daemon=True).start()


def _get_route_suggestions(force: bool = False, block: bool = False) -> dict:
    """Recurring-route suggestions, cached (mem + one disk payload).

    A warm key returns instantly with `computing: False`. On a cold/stale
    key the clustering runs in a BACKGROUND thread (it's multi-second) and
    this returns the last-known payload tagged `computing: True`; the page
    polls until it settles. `block=True` forces a synchronous compute (used
    by the offline oracle / tests)."""
    if not force:
        cached = _suggestions_from_cache(_suggestions_cache_key())
        if cached is not None:
            return {**cached, "computing": False}
    if block:
        return {**_compute_and_store_suggestions(), "computing": False}
    _maybe_start_suggestions_worker()
    last = _route_suggestions_mem["payload"] or {"suggestions": [], "computed_at": None}
    return {**last, "computing": True}


def _dismissals_path() -> Path:
    return ROUTE_SUGGESTIONS_CACHE_DIR / "dismissals.json"


def _load_dismissals() -> set:
    """Set of suggestion ids the rider has hidden. Filtered at serve time
    (not build time) so dismissing never invalidates the clustering cache."""
    try:
        return set(json.loads(_dismissals_path().read_text(encoding="utf-8")) or [])
    except Exception:
        return set()


def _save_dismissals(ids: set) -> None:
    ROUTE_SUGGESTIONS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(_dismissals_path(), json.dumps(sorted(ids), ensure_ascii=False))


@app.route("/api/routes/suggestions", methods=["GET"])
def api_routes_suggestions():
    """Recurring loops the rider has done >= 2 times, not already saved.

    Cached (disk + memory); a cold cache computes in the background and the
    response carries `computing: true` until ready (poll to refresh).
    Optional `?region_id=` filters the cached list; `?force=1` recomputes.
    Dismissed suggestions are filtered out here (serve-time)."""
    payload = _get_route_suggestions(force=request.args.get("force") == "1")
    suggestions = payload.get("suggestions") or []
    region_id = request.args.get("region_id")
    if region_id:
        suggestions = [s for s in suggestions if s.get("region_id") == region_id]
    dismissed = _load_dismissals()
    if dismissed:
        suggestions = [s for s in suggestions if s.get("id") not in dismissed]
    return jsonify({"version": ROUTES_API_VERSION,
                     "computing": payload.get("computing", False),
                     "computed_at": payload.get("computed_at"),
                     "suggestions": suggestions})


@app.route("/api/routes/suggestions/<sug_id>/dismiss", methods=["POST"])
def api_routes_suggestion_dismiss(sug_id: str):
    """Hide a suggestion permanently (won't resurface unless its cluster
    membership changes, which mints a new id)."""
    if not _ID_RE.fullmatch(sug_id or ""):
        abort(404)
    ids = _load_dismissals()
    ids.add(sug_id)
    _save_dismissals(ids)
    return jsonify({"ok": True, "dismissed": sug_id})


@app.route("/api/routes/suggestions/<sug_id>/dismiss", methods=["DELETE"])
def api_routes_suggestion_undismiss(sug_id: str):
    """Un-hide a previously dismissed suggestion."""
    if not _ID_RE.fullmatch(sug_id or ""):
        abort(404)
    ids = _load_dismissals()
    ids.discard(sug_id)
    _save_dismissals(ids)
    return jsonify({"ok": True})


@app.route("/api/activity/<path:filename>/routes-ridden")
def api_activity_routes_ridden(filename: str):
    """Which saved routes did the rider complete in this activity?

    Derived from the cached per-route attempts data — no fresh scan.
    Each match gets a `rank` and `rank_total` so the panel can show
    "best ever / 2 of 9" style stats inline. Multiple attempts of the
    same route in one ride each get their own row, sorted by start_time.
    """
    # Path-traversal guard — `<path:filename>` accepts slashes, so a
    # naive existence check would resolve `../app.py` against GPX_DIR
    # and pass.  _safe_gpx_path bounds the lookup to the tracks dir.
    if _safe_gpx_path(filename) is None:
        abort(404)
    routes = _all_routes()
    # Hoist the trail_match dir scan once per request — every route's
    # cache key would otherwise rescan ~500 files independently.
    trail_match_fp = _trail_match_dir_fingerprint()
    matches = []
    for r in routes:
        payload = _get_route_attempts(r["id"], route=r,
                                        trail_match_fp=trail_match_fp) or {}
        attempts = payload.get("attempts") or []
        total = len(attempts)
        for idx, att in enumerate(attempts):
            if att.get("filename") != filename:
                continue
            matches.append({
                "route_id":      r["id"],
                "route_name":    r["name"],
                "region_id":     r.get("region_id"),
                "rank":          idx + 1,
                "rank_total":    total,
                "duration_sec":  att.get("duration_sec"),
                "start_time":    att.get("start_time"),
                "end_time":      att.get("end_time"),
                "first_idx":     att.get("first_idx"),
                "last_idx":      att.get("last_idx"),
                "is_best":       idx == 0,
            })
    matches.sort(key=lambda m: (m.get("start_time") or "", m.get("rank") or 0))
    return jsonify({"version": ROUTES_API_VERSION,
                     "filename": filename,
                     "routes": matches})


@app.route("/trails")
def trails_page():
    """Region-grouped trail leaderboard view. Lists every region you've
    ridden, the trails in that region, attempts per trail, and your best
    time per trail (with a link to that ride)."""
    return render_template("trails.html",
        types_json=_safe_json(load_types()))


@app.route("/api/trails/leaderboard/<path:name>")
def api_trail_leaderboard(name):
    """Per-(trail, direction) leaderboard: every completed attempt across
    all cached MTB rides, sorted fastest first.

    Query params:
      direction: 'up' | 'down' | 'mixed' | 'all' (default = 'all', which
        returns every direction's rows interleaved-then-resorted).
    """
    boards = _get_leaderboards()
    direction = (request.args.get("direction") or "all").lower()
    if direction == "all":
        # Combine all directions for the legacy callers / overview view.
        all_rows = []
        for (n, d), rows in boards.items():
            if n != name:
                continue
            all_rows.extend(rows)
        all_rows.sort(key=lambda r: (r["duration_sec"], r["filename"]))
        rows = all_rows
    else:
        rows = boards.get((name, direction)) or []
    decorated = [{**r, "rank": i} for i, r in enumerate(rows, start=1)]
    return jsonify({
        "name": name, "direction": direction,
        "rows": decorated, "count": len(rows),
        # Also surface which directions are available for this trail so
        # the UI can let the user toggle.
        "available_directions": sorted({d for (n, d) in boards.keys() if n == name}),
    })


@app.route("/api/activities")
def api_activities():
    etag = _make_etag("activities", _activities_cache_key())
    if request.if_none_match.contains(etag):
        return Response(status=304)
    resp = jsonify(all_activities())
    resp.set_etag(etag)
    resp.headers["Cache-Control"] = "no-cache"  # must revalidate, but body can be skipped
    return resp


@app.route("/api/activities/filters")
def api_activities_filters():
    """Lightweight endpoint — just filename, date, type for building filter UI.
    Type falls through to effective_type so freshly-imported activities
    (no user-set metadata yet) still appear under their auto-detected
    chip rather than as 'Untagged'."""
    return jsonify([
        {
            "filename": a["filename"],
            "date":     a["date"],
            "type":     a["meta"].get("type", "") or a.get("effective_type") or "",
        }
        for a in all_activities()
    ])


# ─── Reverse geocoding (province + country) ──────────────────────────────────
# Cache key rounds to 1 decimal (~10 km) — granular enough for province/country
# and keeps the cache file small. Failures are cached as {} so we don't retry on
# every page load. Lookups never block HTTP handlers: cache hits return inline,
# misses enqueue a background fetch that respects Nominatim's 1 req/sec policy
# and backfills for the next view.
_GEOCODE_CACHE_FILE   = CACHE_DIR / "geocode.json"
_geocode_lock         = threading.Lock()
_geocode_queue_lock   = threading.Lock()
_geocode_pending: set = set()
# Successfully-fetched results the worker has buffered but not yet flushed to
# disk. `reverse_geocode` checks this before re-enqueueing so a burst of page
# loads during prewarm doesn't trigger duplicate network fetches.
_geocode_buffer: dict = {}
_geocode_worker_started = False


def _geocode_key(lat: float, lon: float) -> str:
    return f"{round(lat, 1)},{round(lon, 1)}"


# Process-local memoised copy of the geocode dict. Reloaded from disk only
# when the file mtime changes — avoids the per-activity reparse burden when
# /api/activities or /api/summary lists 500+ rows and each calls reverse_geocode().
_geocode_mem_cache: dict | None = None
_geocode_mem_mtime: float = -1.0
_geocode_mem_lock = threading.Lock()


def _load_geocode_cache() -> dict:
    global _geocode_mem_cache, _geocode_mem_mtime
    try:
        st = _GEOCODE_CACHE_FILE.stat()
    except FileNotFoundError:
        return {}
    with _geocode_mem_lock:
        if _geocode_mem_cache is not None and st.st_mtime == _geocode_mem_mtime:
            return _geocode_mem_cache
        try:
            data = json.loads(_GEOCODE_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        _geocode_mem_cache = data
        _geocode_mem_mtime = st.st_mtime
        return data


def _save_geocode_cache(cache: dict) -> None:
    """Persist + refresh the in-memory copy so the next read hits the new
    bytes without a stat-then-reparse round trip."""
    global _geocode_mem_cache, _geocode_mem_mtime
    try:
        _atomic_write(_GEOCODE_CACHE_FILE, json.dumps(cache))
        with _geocode_mem_lock:
            _geocode_mem_cache = dict(cache)
            try:
                _geocode_mem_mtime = _GEOCODE_CACHE_FILE.stat().st_mtime
            except FileNotFoundError:
                _geocode_mem_mtime = -1.0
    except Exception:
        pass


def _geocode_fetch(lat: float, lon: float) -> dict:
    url = (
        "https://nominatim.openstreetmap.org/reverse"
        f"?format=jsonv2&lat={lat}&lon={lon}&zoom=8&addressdetails=1"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AlForks/1.0 (personal GPX viewer)"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        addr = payload.get("address", {}) or {}
        province = addr.get("state") or addr.get("province") or addr.get("region") or ""
        country  = addr.get("country") or ""
        if province or country:
            return {"province": province, "country": country}
    except Exception:
        pass
    return {}


_GEOCODE_FLUSH_EVERY = 10


def _flush_geocode_buffer() -> None:
    """Write all buffered results to disk in a single pass."""
    with _geocode_queue_lock:
        if not _geocode_buffer:
            return
        to_write = dict(_geocode_buffer)
        _geocode_buffer.clear()
    with _geocode_lock:
        cache = _load_geocode_cache()
        cache.update(to_write)
        _save_geocode_cache(cache)


def _geocode_worker():
    """Drain the pending queue at ~1 req/sec (Nominatim's fair-use policy).
    Only successful lookups are cached so transient failures are retried later.
    Writes are batched: during a burst we accumulate results in memory and
    flush to disk only when the queue drains or every _GEOCODE_FLUSH_EVERY
    results. Prevents rewriting the entire cache file once per second during
    prewarm of many locations.
    """
    while True:
        with _geocode_queue_lock:
            if not _geocode_pending:
                queue_empty = True
                key = None
            else:
                queue_empty = False
                key = next(iter(_geocode_pending))
                _geocode_pending.discard(key)
        if queue_empty:
            _flush_geocode_buffer()
            time.sleep(1.0)
            continue
        try:
            lat_s, lon_s = key.split(",")
            result = _geocode_fetch(float(lat_s), float(lon_s))
        except Exception:
            result = {}
        if result:
            with _geocode_queue_lock:
                _geocode_buffer[key] = result
                buffered = len(_geocode_buffer)
            if buffered >= _GEOCODE_FLUSH_EVERY:
                _flush_geocode_buffer()
        time.sleep(1.1)


def _ensure_geocode_worker():
    global _geocode_worker_started
    if _geocode_worker_started:
        return
    with _geocode_queue_lock:
        if _geocode_worker_started:
            return
        threading.Thread(target=_geocode_worker, name="geocode-worker", daemon=True).start()
        _geocode_worker_started = True


def reverse_geocode(lat: float, lon: float) -> dict:
    """Return cached geocode for a coord, or {} if not yet fetched.
    Enqueues a background lookup on miss — safe to call from request handlers.
    Empty cached entries (from past failures) are treated as misses and retried.
    """
    key = _geocode_key(lat, lon)
    # Check the in-flight write buffer first — during a prewarm burst, results
    # may not have hit disk yet but are already resolved in memory.
    with _geocode_queue_lock:
        buffered = _geocode_buffer.get(key)
    if buffered:
        return buffered
    with _geocode_lock:
        cache = _load_geocode_cache()
        cached = cache.get(key)
        if cached:
            return cached
    _ensure_geocode_worker()
    with _geocode_queue_lock:
        _geocode_pending.add(key)
    return {}


def prewarm_geocode(activities: list[dict]) -> None:
    """Queue geocode lookups for any activity start points not yet cached."""
    with _geocode_lock:
        cache = _load_geocode_cache()
    with _geocode_queue_lock:
        buffered = set(_geocode_buffer.keys())
    to_add = []
    for a in activities:
        pts = a.get("_start_latlon")
        if not pts:
            continue
        key = _geocode_key(pts[0], pts[1])
        if key in buffered:
            continue
        if not cache.get(key):  # missing or previously-empty entries
            to_add.append(key)
    if not to_add:
        return
    _ensure_geocode_worker()
    with _geocode_queue_lock:
        _geocode_pending.update(to_add)


@app.route("/api/activity/<filename>")
def api_activity(filename):
    p = _safe_gpx_path(filename)
    if p is None or not p.exists():
        abort(404)
    date_str = ""
    try:
        # Cheap peek at the cached parsed data to learn the date
        cached = _read_disk_cache(filename, p.stat().st_mtime)
        if cached and cached.get("date"):
            date_str = cached["date"][:10]
    except Exception:
        pass
    hr_file = HR_CACHE_DIR / f"{date_str}.json" if date_str else None
    etag = _make_etag(
        "activity", _ACTIVITY_RESPONSE_VERSION, filename, _stat_mtime(p),
        _stat_mtime(METADATA_FILE), _stat_mtime(REGIONS_FILE), _stat_mtime(_GEOCODE_CACHE_FILE),
        _stat_mtime(CONFIG_FILE), _stat_mtime(TYPES_FILE),
        _stat_mtime(hr_file) if hr_file else 0,
        # Bumping TRAIL_MATCH_VERSION changes the trails payload shape;
        # baking it into the etag means the client revalidates without
        # needing a parallel _ACTIVITY_RESPONSE_VERSION bump.
        trail_match.TRAIL_MATCH_VERSION,
    )
    if request.if_none_match.contains(etag):
        return Response(status=304)
    data = get_activity(filename)
    if data is None:
        abort(404)
    meta      = load_metadata()
    file_meta = meta.get(filename, {})
    all_regions = load_regions()
    # Effective type drives algorithm choice + display, falling back from
    # explicit meta.type to the first matched region's `default_type`. Pins
    # count for this fallback so pinning "Whistler" onto a tagless activity
    # flips it to snowboard automatically.
    matched_regions = _effective_regions(get_activity(filename) or {}, file_meta, all_regions)
    eff_type = _effective_type_for(file_meta.get("type", ""), matched_regions, all_regions,
                                   data.get("date") or "")
    # Canonical trail-match input: transform the RAW data independently of the
    # ?notrim/?noshift/?norepair/?nosmoothing UI bypass flags below, so the snap
    # cached under meta_fp always matches — regardless of whether this request,
    # prewarm, or a rescan populated it first. (Only MTB activities are matched.)
    match_data = _effective_for_match(filename, data, file_meta, eff_type) if eff_type == "mtb" else None
    data      = _effective_data(filename, data, eff_type)
    # Time-shift runs first so trim windows and HR merge see the corrected
    # timeline. ?noshift=1 bypasses it for the trim-edit UI (which wants
    # to see the raw clock) — mirrors the ?notrim / ?norepair pattern.
    shift_h = (file_meta.get("time_shift_hours") or 0)
    if shift_h and not request.args.get("noshift"):
        data = _apply_time_shift(data, shift_h)
    # Apply user trim (start_km / end_km in original distances) before HR merge
    # so HR alignment respects the trimmed time window. ?notrim=1 in the URL
    # bypasses the trim — used by the trim-edit UI to see the full track.
    trim = file_meta.get("trim") or {}
    if trim and not request.args.get("notrim"):
        data = _apply_trim(data, trim)
    if file_meta.get("spike_repair") and not request.args.get("norepair"):
        data = _apply_spike_repair(data)
    smoothing = file_meta.get("smoothing") or {}
    sm_window = int(smoothing.get("window") or 0) if isinstance(smoothing, dict) else 0
    if sm_window > 1 and not request.args.get("nosmoothing"):
        data = _apply_smoothing(data, smoothing)
    data      = _merge_hr_into_data(data)
    regions   = _effective_regions(data, file_meta, all_regions)
    place: dict = {}
    pts = data.get("points") or []
    if pts:
        place = reverse_geocode(pts[0]["lat"], pts[0]["lon"])
    issues = [] if file_meta.get("issues_approved") else _detect_issues_cached(data)
    # Preview feature: per-trail attempt timeline for MTB activities in
    # gated regions (Moose Mountain only, for now). Cached per-file by
    # GPX mtime — the snap loop is too slow to run on every request.
    trails = None
    if eff_type == "mtb":
        try:
            # Per-file metadata that affects the snapped data (trim,
            # smoothing, time-shift, spike-repair) needs to invalidate
            # the trail_match cache. The raw GPX mtime alone isn't
            # enough — these edits live in metadata.json. Fingerprint
            # them into a short hash and pass through.
            meta_fp = _meta_fp(file_meta)
            disk_before = TRAIL_MATCH_CACHE_DIR / f"{filename}.json"
            existed_before = disk_before.exists()
            trails = _cached_match(filename, _stat_mtime(p), match_data, meta_fp)
            # If cached_match wrote a fresh file for this activity (first
            # request or invalidated cache), the leaderboard built before
            # this call doesn't include the current attempts — meaning
            # rank lookups below would return None. Force-invalidate so
            # the next _get_leaderboards rebuilds with this file present.
            if not existed_before:
                _invalidate_trail_leaderboards()
            if trails is not None:
                # Decorate completed timeline entries with their rank
                # against the cross-activity leaderboard. Partials get
                # rank=None so the UI can show a blank.
                boards = _get_leaderboards()
                for entry in (trails.get("timeline") or []):
                    if not entry.get("completed"):
                        entry["rank"] = None
                        entry["rank_total"] = None
                        continue
                    rt = trail_match.rank_for_attempt(
                        boards, entry["name"], entry.get("direction") or "mixed",
                        filename, entry.get("start_idx", 0),
                    )
                    if rt is None:
                        entry["rank"] = None
                        entry["rank_total"] = None
                    else:
                        entry["rank"], entry["rank_total"] = rt
        except Exception as exc:
            logger.warning("trail_match failed for %s: %s", filename, exc)
            trails = None
    resp = jsonify({**data, "meta": file_meta, "regions": regions, "place": place,
                    "effective_type": eff_type, "issues": issues,
                    "excluded": bool(file_meta.get("excluded_from_stats")),
                    "effective_assisted": _is_effectively_assisted(file_meta, regions, all_regions),
                    "trails": trails})
    resp.set_etag(etag)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ─── Request-body validators ──────────────────────────────────────────────
# Shape-check payloads for write endpoints so a malformed client can't
# persist state that later crashes a read path (e.g. trim.start_km = "foo"
# would raise ValueError inside _apply_trim on every request thereafter).

def _bad(msg: str):
    resp = jsonify({"error": msg})
    resp.status_code = 400
    abort(resp)


def _validate_trim(v) -> dict:
    if not isinstance(v, dict):
        _bad("trim must be an object")
    out: dict = {}
    for k in ("start_km", "end_km"):
        if k in v and v[k] is not None:
            try:
                n = float(v[k])
            except (TypeError, ValueError):
                _bad(f"trim.{k} must be a number")
            if n < 0:
                _bad(f"trim.{k} must be >= 0")
            out[k] = n
    return out


def _validate_smoothing(v) -> dict:
    if not isinstance(v, dict):
        _bad("smoothing must be an object")
    out: dict = {}
    if "window" in v and v["window"] is not None:
        try:
            w = int(v["window"])
        except (TypeError, ValueError):
            _bad("smoothing.window must be an integer")
        if w < 1:
            _bad("smoothing.window must be >= 1")
        out["window"] = w
    for k in ("start_km", "end_km"):
        if k in v and v[k] is not None:
            try:
                n = float(v[k])
            except (TypeError, ValueError):
                _bad(f"smoothing.{k} must be a number")
            if n < 0:
                _bad(f"smoothing.{k} must be >= 0")
            out[k] = n
    return out


_ALLOWED_SEG_TYPES = frozenset({"riding", "assisted"})


def _validate_segment_overrides(v) -> list:
    if not isinstance(v, list):
        _bad("segment_overrides must be a list")
    out: list = []
    for idx, seg in enumerate(v):
        if not isinstance(seg, dict):
            _bad(f"segment_overrides[{idx}] must be an object")
        t = seg.get("type")
        if t not in _ALLOWED_SEG_TYPES:
            _bad(f"segment_overrides[{idx}].type must be 'riding' or 'assisted'")
        try:
            start = int(seg["start"])
            end = int(seg["end"])
        except (KeyError, TypeError, ValueError):
            _bad(f"segment_overrides[{idx}].start/end must be integers")
        if start < 0 or end < start:
            _bad(f"segment_overrides[{idx}] must satisfy 0 <= start <= end")
        out.append({"type": t, "start": start, "end": end})
    return out


# ─── Activity deletion (archives the GPX rather than hard-deleting) ────────
ARCHIVE_DIR = GPX_DIR / "_archive_dedup"


def _archive_activity(filename: str) -> bool:
    """Move a GPX to the archive folder, drop its metadata.json entry, and
    invalidate the activities cache. Returns True on success."""
    src = _safe_gpx_path(filename)
    if src is None or not src.exists():
        return False
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVE_DIR / src.name
    if dest.exists():
        dest = ARCHIVE_DIR / f"{src.stem}_{int(time.time())}{src.suffix}"
    try:
        src.rename(dest)
    except OSError:
        return False
    # Evict the parsed-track LRU entry. Without this, get_activity() would
    # keep returning the cached parse for a file that no longer lives in
    # tracks/ — and _build_activity_entry would happily re-add the
    # archived activity to the sidebar cache as if it were still active.
    _mem_cache.evict(filename)
    # Hold the metadata lock across the read-mutate-write so a concurrent
    # PATCH on a different filename doesn't lose its update by racing
    # with our `del`. Same pattern in the other three callers below.
    with _metadata_lock:
        meta = load_metadata()
        if filename in meta:
            del meta[filename]
            save_metadata(meta, changed_filenames=[filename])
        else:
            # Metadata untouched — still drop the sidebar entry for the archived file
            _update_activity_entry(filename)
    return True


@app.route("/api/activity/<filename>", methods=["DELETE"])
def api_delete_activity(filename):
    if _archive_activity(filename):
        return jsonify({"ok": True})
    abort(404)


@app.route("/api/activity/<filename>/refresh-hr", methods=["POST"])
def api_refresh_hr(filename):
    """Re-fetch Garmin HR for this activity's date. Used by the per-track
    "↻ Refresh HR" button when an activity is showing no HR data and the
    user has just synced their watch. Synchronous — runs `garmin_sync.py
    --retry-date` for the activity's date and waits for the result."""
    if _safe_gpx_path(filename) is None:
        abort(404)
    activity = get_activity(filename)
    if not activity:
        abort(404)
    date_str = (activity.get("date") or "")[:10]
    if not date_str:
        return jsonify({"ok": False, "message": "activity has no date"}), 400

    try:
        proc = subprocess.run(
            [sys.executable, str(_ROOT / "sync" / "garmin_sync.py"),
             "--retry-date", date_str],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "message": "fetch timed out after 60s"}), 504
    except Exception as e:
        return jsonify({"ok": False, "message": f"error: {e}"}), 500

    if proc.returncode != 0:
        out = (proc.stderr or proc.stdout).strip()
        msg = out.splitlines()[-1] if out else "fetch failed"
        return jsonify({"ok": False, "message": msg}), 502

    # Drop the per-date HR-range cache so the next has_hr check re-stats the
    # cache file and picks up the new sample range.
    with _hr_range_cache_lock:
        _hr_range_cache.pop(date_str, None)
    # Refresh just this activity's sidebar entry so has_hr flips when the
    # fetch returned samples covering the activity window.
    _update_activity_entry(filename)

    last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return jsonify({"ok": True, "message": last, "date": date_str})


@app.route("/api/activities/bulk-delete", methods=["POST"])
def api_bulk_delete():
    body = request.get_json(force=True) or {}
    filenames = body.get("filenames", [])
    moved = sum(1 for fn in filenames if _archive_activity(fn))
    return jsonify({"ok": True, "archived": moved})


@app.route("/api/bulk-metadata", methods=["PATCH"])
def api_bulk_metadata():
    body      = request.get_json(force=True) or {}
    filenames = body.get("filenames", [])
    allowed   = {"type", "title", "location", "notes"}
    update    = {k: v for k, v in body.items() if k in allowed}
    if not update or not filenames:
        abort(400)
    with _metadata_lock:
        meta    = load_metadata()
        count   = 0
        touched = []
        for fn in filenames:
            if _safe_gpx_path(fn) is None or not (GPX_DIR / fn).exists():
                continue
            meta.setdefault(fn, {}).update(update)
            meta[fn] = {k: v for k, v in meta[fn].items() if v != ""}
            if not meta[fn]:
                del meta[fn]
            touched.append(fn)
            count += 1
        save_metadata(meta, changed_filenames=touched)
    return jsonify({"ok": True, "updated": count})


@app.route("/api/activity/<filename>/metadata", methods=["PATCH"])
def api_save_metadata(filename):
    if _safe_gpx_path(filename) is None or not (GPX_DIR / filename).exists():
        abort(404)
    body    = request.get_json(force=True) or {}
    allowed = {"type", "title", "location", "notes", "trim", "issues_approved",
               "smoothing", "excluded_from_stats", "regions_pinned", "assisted",
               "spike_repair", "time_shift_hours"}
    update  = {k: v for k, v in body.items() if k in allowed}
    # `assisted` is a tri-state override (True / False / null). Coerce to bool
    # when present so the JSON file holds a clean shape; null means "clear
    # the override" (handled by the empty-value strip below).
    if "assisted" in update and update["assisted"] is not None:
        update["assisted"] = bool(update["assisted"])
    if "spike_repair" in update and update["spike_repair"] is not None:
        update["spike_repair"] = bool(update["spike_repair"])
    # time_shift_hours: integer in [-23, 23]; null or 0 clears the field
    # (handled by the empty-value strip below since we replace 0 → None).
    if "time_shift_hours" in update:
        v = update["time_shift_hours"]
        if v is None or v == 0:
            update["time_shift_hours"] = None
        else:
            try:
                h = int(v)
            except (TypeError, ValueError):
                _bad("time_shift_hours must be an integer")
            if h < -23 or h > 23:
                _bad("time_shift_hours must be between -23 and 23")
            update["time_shift_hours"] = h
    # Shape-check structured fields so a bad payload doesn't corrupt metadata.
    if "trim" in update and update["trim"]:
        update["trim"] = _validate_trim(update["trim"])
    if "smoothing" in update and update["smoothing"]:
        update["smoothing"] = _validate_smoothing(update["smoothing"])
    # Normalize regions_pinned: accept a list of strings, dedupe, drop empties
    if "regions_pinned" in update:
        pins = update["regions_pinned"] or []
        if not isinstance(pins, list):
            _bad("regions_pinned must be a list of strings")
        seen: set[str] = set()
        cleaned: list[str] = []
        for rid in pins:
            if isinstance(rid, str) and rid and rid not in seen:
                cleaned.append(rid)
                seen.add(rid)
        update["regions_pinned"] = cleaned
    with _metadata_lock:
        meta    = load_metadata()
        meta.setdefault(filename, {}).update(update)
        # Strip empty/cleared values, but keep trim={} as a clear signal of "no trim"
        meta[filename] = {k: v for k, v in meta[filename].items()
                          if v != "" and v is not None and not (k == "trim" and not v)
                          and not (k == "regions_pinned" and not v)}
        if not meta[filename]:
            del meta[filename]
        save_metadata(meta, changed_filenames=[filename])
    return jsonify({"ok": True})


@app.route("/heatmap")
def heatmap():
    return render_template("heatmap.html",
        mapbox_token=load_config().get("mapbox_token", ""),
        types_json=_safe_json(load_types()))


@app.route("/api/activity/<filename>/segments", methods=["PATCH", "DELETE"])
def api_save_segments(filename):
    if _safe_gpx_path(filename) is None or not (GPX_DIR / filename).exists():
        abort(404)
    with _metadata_lock:
        meta = load_metadata()
        if request.method == "DELETE":
            if filename in meta:
                meta[filename].pop("segment_overrides", None)
                if not meta[filename]:
                    del meta[filename]
        else:
            overrides = (request.get_json(force=True) or {}).get("segment_overrides")
            if overrides is not None:
                meta.setdefault(filename, {})["segment_overrides"] = _validate_segment_overrides(overrides)
        save_metadata(meta, changed_filenames=[filename])
    return jsonify({"ok": True})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        body = request.get_json(force=True) or {}
        cfg  = load_config()
        if "mapbox_token" in body:
            cfg["mapbox_token"] = body["mapbox_token"].strip()
        save_config(cfg)
        return jsonify({"ok": True})
    return jsonify({"mapbox_token": bool(load_config().get("mapbox_token"))})


@app.route("/api/heatmap/stream")
def api_heatmap_stream():
    """SSE endpoint — streams one activity JSON object per event."""
    year     = request.args.get("year", "")
    act_type = request.args.get("type", "")
    sample_n = 5

    def generate():
        # Filter against the already-cached sidebar entries so we skip the
        # directory glob and avoid calling get_activity() — which loads the
        # full parsed point data — for activities that won't pass the
        # year/type filter anyway.
        candidates = []
        for act in all_activities():
            if year and (not act.get("date") or not act["date"].startswith(year)):
                continue
            if act_type and (act.get("meta") or {}).get("type", "") != act_type:
                continue
            candidates.append(act)
        total = len(candidates)
        yield f"data: {json.dumps({'total': total})}\n\n"

        for act in candidates:
            data = get_activity(act["filename"])
            if not data:
                yield f"data: {json.dumps({'skip': True})}\n\n"
                continue
            file_meta = act.get("meta") or {}
            polyline = [[p["lat"], p["lon"]] for p in data["points"][::sample_n]]
            payload = {
                "activity": {
                    "filename":    data["filename"],
                    "date":        data["date"],
                    "type":        file_meta.get("type", ""),
                    "title":       file_meta.get("title") or data["name"],
                    "distance_km": data["stats"]["distance_km"],
                    "polyline":    polyline,
                }
            }
            yield f"data: {json.dumps(payload)}\n\n"

        yield f"data: {json.dumps({'done': True})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/activity/<filename>/compare")
def api_compare_algorithms(filename):
    """Run all detection algorithms on a track and return segments + stats for each."""
    if _safe_gpx_path(filename) is None:
        abort(404)
    data = get_activity(filename)
    if data is None:
        abort(404)

    per_pt, latlons = _per_pt_from_points(data["points"])

    pts  = data["points"]
    bbox = (
        min(p["lat"] for p in pts), min(p["lon"] for p in pts),
        max(p["lat"] for p in pts), max(p["lon"] for p in pts),
    )
    osm_lifts = _fetch_osm_lifts(bbox)

    results = []
    for algo_id, label, description, fn in DETECTION_ALGORITHMS:
        is_assisted = fn(per_pt, latlons, osm_lifts)
        results.append({
            "id":          algo_id,
            "label":       label,
            "description": description,
            "segments":    _build_segments(is_assisted),
            "stats":       _compute_algo_stats(is_assisted, per_pt),
        })

    return jsonify({"osm_available": bool(osm_lifts), "algorithms": results})


# ─── Garmin HR integration ───────────────────────────────────────────────────
# Actual sync lives in garmin_sync.py (CLI, runs offline). This module just
# reads the on-disk HR cache and merges samples into activity data.

HR_CACHE_DIR = CACHE_DIR / "hr"
_TOKEN_DIR   = Path.home() / ".alforks"
_STATUS_FILE = _TOKEN_DIR / "garmin_status.json"

# In-memory LRU cache for HR-merged activity stats. Keyed by everything that
# would change the result: GPX filename, GPX file mtime, HR cache file mtime,
# and the user's effective max-HR (which gates zone bucketing). The merge
# function does timezone math + binary search per sample, so caching prevents
# repeating that for /api/fitness/weekly which iterates many activities.
_HR_MERGE_CACHE: OrderedDict = OrderedDict()
_HR_MERGE_CACHE_MAX = 500
_hr_merge_cache_lock = threading.Lock()


def _hr_merge_cache_get(key):
    with _hr_merge_cache_lock:
        v = _HR_MERGE_CACHE.get(key)
        if v is not None:
            _HR_MERGE_CACHE.move_to_end(key)
        return v


def _hr_merge_cache_put(key, value):
    with _hr_merge_cache_lock:
        _HR_MERGE_CACHE[key] = value
        _HR_MERGE_CACHE.move_to_end(key)
        while len(_HR_MERGE_CACHE) > _HR_MERGE_CACHE_MAX:
            _HR_MERGE_CACHE.popitem(last=False)


def _load_hr_samples(date_str: str) -> list[list]:
    """Return [[utc_ms, bpm], ...] for a given date, or [] if not cached."""
    fp = HR_CACHE_DIR / f"{date_str}.json"
    if not fp.exists():
        return []
    try:
        payload = json.loads(fp.read_text(encoding="utf-8"))
        return payload.get("samples") or []
    except Exception:
        return []


def _merge_hr_into_data(data: dict) -> dict:
    """Overlay Garmin HR samples onto a parsed activity.

    Adds, when HR is cached for the activity's date and we can align timestamps:
      data['hr_samples']       — list of [dist_km, bpm] pairs for chart plotting
      data['stats']['hr_avg']  — average bpm over the activity
      data['stats']['hr_max']  — peak bpm during the activity
    The activity dict is returned unchanged if HR is unavailable.
    """
    pts = data.get("points") or []
    start_iso = data.get("date")
    if not pts or not start_iso:
        return data

    date_str = start_iso[:10]

    # Fast path: in-memory cache keyed by inputs that affect the merge result
    cache_key = None
    fname = data.get("filename")
    if fname:
        try:
            gpx_mtime = (GPX_DIR / fname).stat().st_mtime
        except OSError:
            gpx_mtime = 0
        try:
            hr_mtime = (HR_CACHE_DIR / f"{date_str}.json").stat().st_mtime
        except OSError:
            hr_mtime = 0
        cache_key = (fname, gpx_mtime, hr_mtime, _effective_max_hr())
        cached = _hr_merge_cache_get(cache_key)
        if cached is not None:
            data = {**data, "hr_samples": cached["hr_samples"]}
            data["stats"] = {**data["stats"], **cached["stats_overlay"]}
            return data

    samples  = _load_hr_samples(date_str)
    if not samples:
        return data

    # Treat the GPX wall-clock time as LOCAL — TrailForks (and many other GPX
    # exporters) tag timestamps with `+00:00` even though the displayed HH:MM
    # is the rider's local time. We strip any stated tzinfo and re-anchor with
    # the location's IANA timezone. `parse_gpx` already resolved the zone from
    # the start point and stashed it in `data["tz_name"]`; prefer that to
    # avoid a second `_weather_timezone_name` call (which uses a different
    # cache key and can fire a duplicate Open-Meteo fetch for the same ride).
    # Centroid-based lookup remains as the fallback for legacy cache entries
    # that don't carry `tz_name`.
    tz_name = data.get("tz_name")
    if not tz_name:
        clat = sum(p["lat"] for p in pts) / len(pts)
        clon = sum(p["lon"] for p in pts) / len(pts)
        tz_name = _weather_timezone_name(clat, clon, date_str)
    if not tz_name:
        return data
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return data

    try:
        pt_utc_ms = []
        for p in pts:
            t = p.get("time")
            if not t:
                pt_utc_ms.append(None)
                continue
            # Strip tzinfo (see comment above) and re-anchor at `tz`. During the
            # fall-back DST hour the wall-clock is ambiguous; fold=0 resolves
            # to the pre-transition (DST) instance, which is what riders
            # usually intend. During the spring-forward gap the wall-clock
            # doesn't exist and either fold is an arbitrary pick.
            dt = datetime.fromisoformat(t).replace(tzinfo=None, fold=0).replace(tzinfo=tz)
            pt_utc_ms.append(int(dt.timestamp() * 1000))
    except Exception:
        return data

    # Valid time-aware points
    valid_idx = [i for i, ms in enumerate(pt_utc_ms) if ms is not None]
    if len(valid_idx) < 2:
        return data

    first_ms = pt_utc_ms[valid_idx[0]]
    last_ms  = pt_utc_ms[valid_idx[-1]]

    # Keep HR samples that overlap the activity window (with 2-min buffer)
    window = 2 * 60 * 1000
    in_window = [(ms, bpm) for ms, bpm in samples
                 if first_ms - window <= ms <= last_ms + window]
    if not in_window:
        return data

    # For each in-window sample, find nearest GPX point (by UTC ms) and
    # record (dist_km, bpm) for chart plotting
    pt_ms_array = [pt_utc_ms[i] for i in valid_idx]
    pt_dist     = [pts[i]["dist_km"] for i in valid_idx]

    def nearest_dist(ms):
        # Binary search for insertion position
        lo, hi = 0, len(pt_ms_array) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if pt_ms_array[mid] < ms: lo = mid + 1
            else:                      hi = mid
        # lo is the nearest-or-next — check the one before too
        best_i = lo
        if lo > 0 and abs(pt_ms_array[lo - 1] - ms) < abs(pt_ms_array[lo] - ms):
            best_i = lo - 1
        return pt_dist[best_i]

    hr_samples = []
    bpms       = []
    for ms, bpm in in_window:
        if bpm is None: continue
        hr_samples.append([nearest_dist(ms), bpm])
        bpms.append(bpm)

    if not bpms:
        return data

    # Time in HR zones based on the user's effective max HR. Walk consecutive
    # samples (skipping gaps over 5 min) and accumulate the average-bpm interval
    # into the matching zone. Restricted to the exact activity window — the
    # 2-min buffer used for hr_avg/hr_max would count trailhead resting HR as
    # zone time, which skews the distribution.
    max_hr = _effective_max_hr()
    zones = [0, 0, 0, 0, 0]
    if max_hr:
        ordered = [(ms, bpm) for ms, bpm in in_window
                   if bpm is not None and first_ms <= ms <= last_ms]
        ordered.sort(key=lambda x: x[0])
        for i in range(1, len(ordered)):
            ms_prev, bpm_prev = ordered[i-1]
            ms_cur,  bpm_cur  = ordered[i]
            dt = (ms_cur - ms_prev) / 1000
            if dt <= 0 or dt > 300:
                continue
            avg_bpm = (bpm_prev + bpm_cur) / 2
            pct = avg_bpm / max_hr
            if   pct < 0.6: z = 0
            elif pct < 0.7: z = 1
            elif pct < 0.8: z = 2
            elif pct < 0.9: z = 3
            else:           z = 4
            zones[z] += dt

    stats_overlay = {
        "hr_avg":   round(sum(bpms) / len(bpms)),
        "hr_max":   max(bpms),
        "hr_zones": [round(z) for z in zones],
        "hr_max_used": max_hr,
    }
    if cache_key is not None:
        _hr_merge_cache_put(cache_key, {"hr_samples": hr_samples, "stats_overlay": stats_overlay})
    data = {**data, "hr_samples": hr_samples}
    data["stats"] = {**data["stats"], **stats_overlay}
    return data


# ─── Fitness: max HR + zones ────────────────────────────────────────────────
# Cache: (result, timestamp, hr_file_count). Short-circuit when the file count
# hasn't changed since last compute — avoids re-reading hundreds of HR files
# on every 5-minute TTL expiry for an unchanged dataset.
_observed_max_hr_cache: tuple[int | None, float, int] = (None, 0.0, -1)
_OBSERVED_MAX_HR_TTL_SEC = 300


def _observed_max_hr() -> int | None:
    """99.5th percentile of every cached HR sample — filters sensor spikes
    while still tracking the user's real ceiling. Cached with TTL + a file-count
    shortcut so we only rescan when a new sync has dropped files.
    """
    global _observed_max_hr_cache
    cached_val, cached_at, cached_count = _observed_max_hr_cache
    try:
        cur_count = sum(1 for _ in HR_CACHE_DIR.glob("*.json")) if HR_CACHE_DIR.exists() else 0
    except OSError:
        cur_count = cached_count
    # Fast path: within TTL AND no new files → nothing could have changed
    if cached_at and cur_count == cached_count and time.time() - cached_at < _OBSERVED_MAX_HR_TTL_SEC:
        return cached_val
    bpms: list[int] = []
    if HR_CACHE_DIR.exists():
        for fp in HR_CACHE_DIR.glob("*.json"):
            try:
                d = json.loads(fp.read_text(encoding="utf-8"))
                for _ms, bpm in (d.get("samples") or []):
                    if bpm:
                        bpms.append(bpm)
            except Exception:
                continue
    if not bpms:
        result = None
    else:
        bpms.sort()
        idx = min(len(bpms) - 1, int(len(bpms) * 0.995))
        result = int(bpms[idx])
    _observed_max_hr_cache = (result, time.time(), cur_count)
    return result


def _effective_max_hr() -> int | None:
    """Returns the max HR to use for zone calculation: user override if set,
    otherwise the observed max from cached samples.
    """
    cfg = load_config()
    override = cfg.get("max_hr_override")
    if isinstance(override, (int, float)) and override > 0:
        return int(override)
    return _observed_max_hr()


def _downsample_polyline(points: list, n: int = 50) -> list:
    """Return n+1 [lat, lon] pairs from the GPX point list: n evenly-spaced
    samples plus the final point appended, so a track's true endpoint is
    always represented regardless of step alignment. Inputs of <= n points
    are returned as-is (no append)."""
    return _downsample_points(points, n, lambda p: (p["lat"], p["lon"]))


def _difficulty_score(distance_km: float | None, elev_gain_m: float | None,
                      elev_loss_m: float | None = None,
                      descent_sport: bool = False) -> int | None:
    """Terrain-difficulty heuristic. Two-step base × steepness multiplier.

    Climb-based path (MTB, hike — `descent_sport=False`):
      1. Base — distance plus climb-equivalent distance (every 55 m of climb
         counts as one km), softly compressed so the high end doesn't run
         away: `(dist + gain/55) ^ 0.83 / 3.2`.
      2. Steepness multiplier — kicks in past 30 m/km gradient (~3%); below
         that it's rolling terrain and the multiplier is 1.0. Climbs at
         ~48 m/km hit ~1.3×, ~100 m/km hit ~2.2×. Without this, moderate-
         climb MTB rides get under-rated vs flat rides of similar distance.

      Sample values:
        5 km / 50 m       → 1    (trivial)
        10 km / 100 m     → 2    (short, flat)
        19 km / 400 m     → 5    (medium, gentle)
        18 km / 868 m     → 8    (medium, moderate climbing)
        30 km / 1000 m    → 8    (solid)
        50 km / 2000 m    → 14   (epic)
        10 km / 1000 m    → 11   (short, brutal — heavy steepness boost)
        80 km / 3000 m    → 20   (monster)

    Descent-based path (ski, snowboard — `descent_sport=True`):
      A lift-assisted gravity sport: difficulty is steepness and volume, not
      climbing effort. The climb formula breaks here (6000 m of descent through
      `gain/55` would explode the base), so descent enters only as the
      steepness ratio:
      1. Base — riding distance alone: `dist ^ 0.83 / 3.2`. Descent is "cheap"
         per metre, so it doesn't add to the base directly.
      2. Steepness multiplier — descent gradient (loss per riding-km). Mellow
         days average ~90 m/km (cat-tracks and runouts dragging the figure
         down); past that each 90 m/km adds 1.0×. ~140 m/km → ~1.6×, ~190 m/km
         → ~2.1×. Riding-segment climb (elev_gain_m) is ignored — it's mostly
         GPS noise and the odd skate-uphill, not deliberate effort.

      Sample values (from real activities):
        16 km / 1450 m loss (90 m/km)   → 3    (short, mellow)
        37 km / 4600 m loss (126 m/km)  → 9    (full day, moderate)
        44 km / 5650 m loss (128 m/km)  → 10   (big day)
        35 km / 6750 m loss (191 m/km)  → 13   (long and steep)

    Scale is arbitrary — useful for relative comparison, not for matching
    any external rating system. Always returns at least 1.
    """
    if not distance_km or distance_km <= 0:
        return None
    if descent_sport:
        loss = elev_loss_m or 0
        loss_per_km = loss / distance_km
        base = distance_km ** 0.83 / 3.2
        steepness = 1 + max(0.0, loss_per_km - 90) / 90
        return max(1, round(base * steepness))
    gain = elev_gain_m or 0
    gain_per_km = gain / distance_km
    base = (distance_km + gain / 55) ** 0.83 / 3.2
    # Steepness multiplier kicks in past ~30 m/km (3% grade) — anything
    # more than rolling terrain. Original threshold of 50 m/km left
    # moderate-climb MTB rides (~5% grade) under-rated vs flat rides of
    # similar distance. Divisor 60 keeps the curve gentle: 1.3× at
    # ~48 m/km, ~2.2× at 100 m/km.
    steepness = 1 + max(0.0, gain_per_km - 30) / 60
    return max(1, round(base * steepness))


@app.route("/comparison")
def comparison_redirect():
    """Old route — the rich-list page is now `/logs` (the canonical
    "Logs" landing). 301 so historical bookmarks resolve cleanly."""
    return redirect("/logs", code=301)


@app.route("/compare")
def compare_overlay_page():
    """Pair two activities: overlay both polylines on a single map and show
    stats side-by-side. Filenames come in as ?a=<file>&b=<file>.
    """
    return render_template("compare.html",
                           types_json=_safe_json(load_types()),
                           mapbox_token=load_config().get("mapbox_token", ""))


@app.route("/api/comparison")
def api_comparison():
    type_arg = (request.args.get("type") or "").strip()
    type_filter = {t for t in type_arg.split(",") if t and t != "all"}
    start_str = (request.args.get("start") or "").strip()
    end_str   = (request.args.get("end")   or "").strip()
    # `issues_only`: legacy narrow scope (only _detect_issues flags) —
    # kept for old bookmarked URLs. `flagged_only`: union of every /review
    # tab's flagged activities (duplicates, missing types, spikes, odd
    # times, issues). The /logs UI uses the latter.
    issues_only  = request.args.get("issues_only")  in ("1", "true")
    flagged_only = request.args.get("flagged_only") in ("1", "true")
    flagged_set  = _review_flagged_filenames() if flagged_only else None
    # Region filter: "" / "all" = no filter; "unassigned" = activities
    # with an empty regions list; any other value = region id that must
    # appear in the activity's region list.
    region_arg = (request.args.get("region") or "").strip()
    max_hr    = _effective_max_hr()
    region_lookup = {r["id"]: r for r in load_regions()}

    all_acts = all_activities()
    items = []
    for act in all_acts:
        date = (act.get("date") or "")[:10]
        if not date: continue
        if start_str and date < start_str: continue
        if end_str   and date > end_str:   continue
        meta_type = (act.get("meta") or {}).get("type", "")
        # Match the filter against meta type OR the auto-detected
        # effective type, so an unedited fresh import still shows up
        # under the right type chip on /logs.
        match_type = meta_type or act.get("effective_type") or ""
        if type_filter and match_type not in type_filter: continue
        if issues_only  and not (act.get("issues") or []): continue
        if flagged_set is not None and act["filename"] not in flagged_set: continue
        if region_arg and region_arg != "all":
            act_regions = act.get("regions") or []
            if region_arg == "unassigned":
                if act_regions: continue
            elif region_arg not in act_regions:
                continue

        # Stats (including hr_avg/hr_max) are pre-baked into the sidebar
        # entry by `_activity_payload`, so we read them straight from
        # `act["stats"]` instead of re-merging HR per request. Cuts ~10–
        # 15 ms per has-HR row on a 200-row window.
        s = act.get("stats") or {}
        hr_avg = s.get("hr_avg")
        intensity = round((hr_avg / max_hr) * 100) if (hr_avg and max_hr) else None

        # Polyline is now pre-baked into the sidebar entry by
        # `_build_activity_entry` (50-point map thumbnail), so we no longer
        # load + re-downsample the full point list per request. This was the
        # last per-row full-track read in the loop — dropping it takes /logs
        # from ~8.7 s to sub-second on a 556-activity library.
        items.append({
            "filename":    act["filename"],
            "date":        act.get("date"),
            # Fall through to the auto-detected type so freshly-imported
            # activities (no user-set metadata yet) still render with the
            # correct icon on /logs — matches the precedence used by
            # /api/activity/<filename> and /api/summary.
            "type":        meta_type or act.get("effective_type") or "",
            "title":       (act.get("meta") or {}).get("title") or "",
            "regions":     [{"id": rid, "name": region_lookup.get(rid, {}).get("name", rid),
                             "color": region_lookup.get(rid, {}).get("color", "#888")}
                            for rid in (act.get("regions") or [])],
            "distance_km":  s.get("distance_km"),
            "elev_gain_m":  s.get("elev_gain_m"),
            "elev_loss_m":  s.get("elev_loss_m"),
            "duration_sec": s.get("duration_sec"),
            "avg_speed_kmh": s.get("avg_speed_kmh"),
            "max_speed_kmh": s.get("max_speed_kmh"),
            "hr_avg":       hr_avg,
            "hr_max":       s.get("hr_max"),
            "difficulty":   _difficulty_score(
                                s.get("distance_km"), s.get("elev_gain_m"),
                                s.get("elev_loss_m"),
                                descent_sport=match_type in ("ski", "snowboard")),
            "intensity":    intensity,
            "polyline":     act.get("polyline") or [],
            "issues":       act.get("issues") or [],
            "excluded":     bool(act.get("excluded")),
        })
    items.sort(key=lambda x: x["date"] or "", reverse=True)

    # Min/max date across the *entire* activities list (not just the
    # filtered subset) so the front-end can default the date-range inputs
    # to the actual span of stored logs. Skips activities without a parsed
    # date — those wouldn't render usefully anyway. Reuse `all_acts` from
    # above so we don't re-snapshot the activities cache and risk reading
    # min/max from a different version than `items`.
    all_dates = [(a.get("date") or "")[:10] for a in all_acts]
    all_dates = [d for d in all_dates if d]
    min_date = min(all_dates) if all_dates else None
    max_date = max(all_dates) if all_dates else None

    return jsonify({
        "items":    items,
        "max_hr":   max_hr,
        "min_date": min_date,
        "max_date": max_date,
    })


_TYPE_METS = {
    # MET values from the Compendium of Physical Activities (Ainsworth 2011).
    # Used as a fall-back when an activity has no HR-zone data.
    "mtb":         8.5,
    "snowboard":   5.3,
    "ski":         5.5,
    "hike":        6.0,
    "fat_biking":  8.0,
    "other":       6.0,
}
_ZONE_METS = [3.0, 5.0, 8.0, 11.0, 14.0]   # Z1..Z5
_DEFAULT_WEIGHT_KG = 75.0


def _weight_for_date(date_str: str, weights_log: list[dict]) -> float | None:
    """Most recent weight (kg) on or before the given date. weights_log
    is sorted oldest-first; we walk forward and remember the latest match."""
    chosen = None
    for w in weights_log:
        if w["date"] <= date_str:
            chosen = w["weight_kg"]
        else:
            break
    return chosen


def _activity_kcal(act_zones, duration_sec, type_id, weight_kg) -> float:
    """Estimate kcal for one activity. When HR-zone seconds are available
    we use intensity-weighted METs (more accurate — a hard ride scores
    higher than a cruise of the same duration); otherwise we fall back
    to a per-sport MET × duration."""
    if act_zones and any(act_zones):
        hours_per_zone = sum(
            (act_zones[i] / 3600.0) * _ZONE_METS[i]
            for i in range(min(5, len(act_zones)))
        )
        return hours_per_zone * weight_kg
    duration_h = (duration_sec or 0) / 3600.0
    met = _TYPE_METS.get(type_id, 6.0)
    return met * weight_kg * duration_h


def _compute_fitness_weeks(n_weeks: int, type_filter: set | None = None) -> list[dict]:
    """Return `n_weeks` of trailing weekly fitness rollups (Monday-anchored).
    Each week dict carries: start, hours, gain_m, zones_sec[5], rides,
    z2_hr_28d, kcal. Used by `/api/fitness/weekly` and the Training Load page."""
    type_filter = type_filter or set()
    cutoff = datetime.now().date() - timedelta(weeks=n_weeks)
    weights_log = _load_weights()

    per_day: dict[str, dict] = {}
    rolling_z2_input: list[tuple[str, int, int]] = []  # (date, avg_hr, z2_seconds)

    for act in all_activities():
        date = (act.get("date") or "")[:10]
        if not date:
            continue
        try:
            d = datetime.fromisoformat(date).date()
        except Exception:
            continue
        if d < cutoff:
            continue
        if type_filter:
            t = (act.get("meta") or {}).get("type") or act.get("effective_type") or ""
            if t not in type_filter:
                continue
        if act.get("excluded"):
            continue
        bucket = per_day.setdefault(date, {"sec": 0, "gain": 0, "zones": [0]*5, "n": 0, "kcal": 0.0, "dist_km": 0.0})
        s = act.get("stats") or {}
        bucket["sec"]     += int(s.get("duration_sec") or 0)
        bucket["gain"]    += int(s.get("elev_gain_m")  or 0)
        bucket["dist_km"] += float(s.get("distance_km") or 0)
        bucket["n"]       += 1

        act_zones = None
        if act.get("has_hr"):
            # Prefer the baked hr_zones/hr_avg on the sidebar entry (see
            # _build_activity_entry). Only fall back to a full GPX parse +
            # HR re-merge for entries that predate the baking (i.e. those
            # written before ENTRY_SCHEMA_VERSION 2) — once they recompute,
            # the per-ride read disappears entirely.
            z      = s.get("hr_zones")
            hr_avg = s.get("hr_avg")
            if z is None:
                data = get_activity(act["filename"])
                if data:
                    eff    = _effective_data(act["filename"], data, (act.get("meta") or {}).get("type", ""))
                    ms     = _merge_hr_into_data(eff).get("stats") or {}
                    z      = ms.get("hr_zones")
                    hr_avg = ms.get("hr_avg")
            if z:
                act_zones = z
                for i in range(min(5, len(z))):
                    bucket["zones"][i] += int(z[i])
                if hr_avg is not None and len(z) > 1 and z[1] > 0:
                    rolling_z2_input.append((date, int(hr_avg), int(z[1])))

        # Calories — intensity-weighted from HR zones when available,
        # MET-by-sport fallback otherwise. Weight comes from the most
        # recent weight-log entry on or before this activity's date,
        # falling back to a 75kg default if the user hasn't logged
        # anything yet.
        weight_kg = _weight_for_date(date, weights_log) or _DEFAULT_WEIGHT_KG
        type_id   = (act.get("meta") or {}).get("type") or act.get("effective_type") or ""
        bucket["kcal"] += _activity_kcal(act_zones, int(s.get("duration_sec") or 0), type_id, weight_kg)

    weeks: dict[str, dict] = {}
    for date_str, b in per_day.items():
        d = datetime.fromisoformat(date_str).date()
        wk_start = (d - timedelta(days=d.weekday())).isoformat()
        w = weeks.setdefault(wk_start, {"hours": 0.0, "gain_m": 0, "zones_sec": [0]*5, "rides": 0, "kcal": 0.0, "dist_km": 0.0})
        w["hours"]    += b["sec"] / 3600
        w["gain_m"]   += b["gain"]
        w["rides"]    += b["n"]
        w["kcal"]     += b.get("kcal", 0.0)
        w["dist_km"]  += b.get("dist_km", 0.0)
        for i in range(5):
            w["zones_sec"][i] += b["zones"][i]

    today_monday = datetime.now().date()
    today_monday = today_monday - timedelta(days=today_monday.weekday())
    output = []
    for w in range(n_weeks - 1, -1, -1):
        wk = (today_monday - timedelta(weeks=w)).isoformat()
        bucket = weeks.get(wk, {"hours": 0, "gain_m": 0, "zones_sec": [0]*5, "rides": 0, "kcal": 0.0, "dist_km": 0.0})
        output.append({
            "start":       wk,
            "hours":       round(bucket["hours"], 1),
            "gain_m":      int(bucket["gain_m"]),
            "zones_sec":   [int(s) for s in bucket["zones_sec"]],
            "rides":       int(bucket["rides"]),
            "kcal":        int(round(bucket.get("kcal", 0.0))),
            "distance_km": round(bucket.get("dist_km", 0.0), 1),
        })

    for week in output:
        wk_end = datetime.fromisoformat(week["start"]).date() + timedelta(days=6)
        wk_28_start = wk_end - timedelta(days=28)
        num = den = 0
        for date_str, avg_hr, z2_sec in rolling_z2_input:
            d = datetime.fromisoformat(date_str).date()
            if wk_28_start <= d <= wk_end:
                num += avg_hr * z2_sec
                den += z2_sec
        week["z2_hr_28d"] = round(num / den) if den > 0 else None

    return output


@app.route("/api/fitness/weekly")
def api_fitness_weekly():
    """Aggregate fitness training metrics by ISO week.
    Query params:
      weeks: number of trailing weeks to return (default 12)
      type:  optional comma-separated activity-type filter
    """
    n_weeks = max(1, min(52, int(request.args.get("weeks", 12))))
    type_arg = (request.args.get("type") or "").strip()
    type_filter = {t for t in type_arg.split(",") if t and t != "all"}
    weeks = _compute_fitness_weeks(n_weeks, type_filter)
    return jsonify({"weeks": weeks, "max_hr": _effective_max_hr()})


_ALLOWED_TRAINING_LOAD_WEEKS = (4, 8, 12, 26)


def _fmt_hours_h_mm(hours: float) -> str:
    """Format a fractional-hours value as 'H:MM' (matches the design's `dur` field)."""
    if hours is None or hours <= 0:
        return "0:00"
    total_min = int(round(hours * 60))
    return f"{total_min // 60}:{total_min % 60:02d}"


def _compute_training_load(n_weeks: int, type_filter: set | None = None) -> dict:
    """Build the Training Load page payload: N weeks of fitness rollups, the
    in-window activities for window-scoped PRs, and per-type totals rolled up
    over the same window. `type_filter` is an optional set of type ids; when
    given, only matching activities (by meta.type or effective_type) feed
    the weeks/recent/totals output."""
    weeks = _compute_fitness_weeks(n_weeks, type_filter)
    types_list = load_types()
    types_by_id = {t["id"]: t for t in types_list}

    # Window bounds — first week's Monday through last week's Sunday
    if weeks:
        win_start = datetime.fromisoformat(weeks[0]["start"]).date()
        win_end   = datetime.fromisoformat(weeks[-1]["start"]).date() + timedelta(days=6)
    else:
        today = datetime.now().date()
        win_start = today - timedelta(weeks=n_weeks)
        win_end   = today

    # In-window activities — used by both records and per-type totals.
    # Apply the same meta-or-effective type fall-through used elsewhere
    # so freshly-imported activities still match a type filter.
    in_window: list[dict] = []
    for a in all_activities():
        date = (a.get("date") or "")[:10]
        if not date or a.get("excluded"):
            continue
        try:
            d = datetime.fromisoformat(date).date()
        except ValueError:
            continue
        if d < win_start or d > win_end:
            continue
        if type_filter:
            t = (a.get("meta") or {}).get("type") or a.get("effective_type") or ""
            if t not in type_filter:
                continue
        in_window.append(a)

    # Recent activities — flat list with the design's RecentActivity shape.
    recent: list[dict] = []
    for a in in_window:
        s = a.get("stats") or {}
        recent.append({
            "filename": a["filename"],
            "type":     (a.get("meta") or {}).get("type", "") or a.get("effective_type") or "",
            "date":     (a.get("date") or "")[:10],
            "name":     a.get("name") or a["filename"],
            "dist":     round(s.get("distance_km") or 0, 2),
            "elev":     int(s.get("elev_gain_m") or 0),
            "elev_loss": int(s.get("elev_loss_m") or 0),
            "dur":      _fmt_hours_h_mm((s.get("duration_sec") or 0) / 3600),
            "duration_sec": int(s.get("duration_sec") or 0),
            "max":      round(s.get("max_speed_kmh") or 0, 1),
            "hr":       int(s.get("hr_avg")) if s.get("hr_avg") is not None else None,
        })
    recent.sort(key=lambda r: r["date"], reverse=True)

    # Per-type rollups across the same window.
    totals_by_type: dict[str, dict] = {}
    for a in in_window:
        meta_type = (a.get("meta") or {}).get("type") or a.get("effective_type") or ""
        if not meta_type:
            continue
        s = a.get("stats") or {}
        b = totals_by_type.setdefault(meta_type, {
            "type": meta_type,
            "days": set(),
            "moving_h": 0.0,
            "elev_gain_m": 0,
            "elev_loss_m": 0,
            "count": 0,
        })
        b["days"].add((a.get("date") or "")[:10])
        b["moving_h"]    += (s.get("duration_sec") or 0) / 3600
        b["elev_gain_m"] += int(s.get("elev_gain_m") or 0)
        b["elev_loss_m"] += int(s.get("elev_loss_m") or 0)
        b["count"]       += 1
    totals = []
    for tid, b in totals_by_type.items():
        td = types_by_id.get(tid, {})
        totals.append({
            "type":        tid,
            "label":       td.get("label", tid),
            "color":       td.get("color", "#9ca3af"),
            "color_bg":    td.get("bg", "#2a2a2a"),
            # Frontend prefers `t.glyph` (set on Setup) over the
            # id-derived fallback. Single-letter fallback retained for
            # historical activity types that pre-date the glyph field.
            "glyph":       td.get("glyph") or (td.get("label", tid)[:1] or "?").upper(),
            "days":        len(b["days"]),
            "moving_h":    round(b["moving_h"], 1),
            "elev_gain_m": b["elev_gain_m"],
            "elev_loss_m": b["elev_loss_m"],
            "count":       b["count"],
        })
    totals.sort(key=lambda t: t["moving_h"], reverse=True)

    return {
        "weeks":        weeks,
        "recent":       recent,
        "totals":       totals,
        "max_hr":       _effective_max_hr(),
        "today":        datetime.now().date().isoformat(),
        "window_start": win_start.isoformat(),
        "window_end":   win_end.isoformat(),
        "n_weeks":      n_weeks,
    }


# ─── Body weight log ──────────────────────────────────────────────────────
# Manually-entered weight history. Stored canonically in kg (UI converts
# to/from stones+lbs). One entry per date — adding for a date that already
# has an entry replaces it.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _load_weights() -> list[dict]:
    if not WEIGHTS_FILE.exists():
        return []
    try:
        data = json.loads(WEIGHTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    weights = data.get("weights") if isinstance(data, dict) else None
    if not isinstance(weights, list):
        return []
    out = []
    for w in weights:
        if not isinstance(w, dict):
            continue
        date = (w.get("date") or "").strip()
        kg = w.get("weight_kg")
        if _DATE_RE.match(date) and isinstance(kg, (int, float)):
            out.append({"date": date, "weight_kg": round(float(kg), 2)})
    out.sort(key=lambda w: w["date"])
    return out


def _save_weights(weights: list[dict]) -> None:
    weights = sorted(weights, key=lambda w: w["date"])
    _atomic_write(WEIGHTS_FILE, json.dumps({"weights": weights}, indent=2))


# Serialize the weights load->mutate->save so two concurrent POST/DELETE
# requests under threaded=True can't lose an update (mirrors the lock pattern
# used by _dup_dismissals / _regions stores).
_weights_lock = threading.Lock()


@app.route("/api/weights")
def api_weights_list():
    return jsonify(_load_weights())


@app.route("/api/weights", methods=["POST"])
def api_weights_add():
    body = request.get_json(force=True) or {}
    date = (body.get("date") or "").strip()
    if not _DATE_RE.match(date):
        abort(400)
    try:
        kg = float(body.get("weight_kg"))
    except (TypeError, ValueError):
        abort(400)
    if not (20.0 <= kg <= 300.0):
        abort(400)
    with _weights_lock:
        weights = [w for w in _load_weights() if w["date"] != date]
        weights.append({"date": date, "weight_kg": round(kg, 2)})
        _save_weights(weights)
    return jsonify({"ok": True})


@app.route("/api/weights/<date>", methods=["DELETE"])
def api_weights_delete(date):
    if not _DATE_RE.match(date):
        abort(400)
    with _weights_lock:
        weights = [w for w in _load_weights() if w["date"] != date]
        _save_weights(weights)
    return jsonify({"ok": True})


@app.route("/api/training")
def api_training_load():
    """JSON payload for the Training page. `weeks` query param accepts
    4, 8, 12, or 26 — anything else falls back to 12 (the design default).
    `type` is an optional comma-separated list of activity-type ids; when
    set, the rollups only include matching activities."""
    try:
        requested = int(request.args.get("weeks", 12))
    except ValueError:
        requested = 12
    n_weeks = requested if requested in _ALLOWED_TRAINING_LOAD_WEEKS else 12
    type_arg = (request.args.get("type") or "").strip()
    type_filter = {t for t in type_arg.split(",") if t and t != "all"}
    return jsonify(_compute_training_load(n_weeks, type_filter or None))


@app.route("/api/fitness/max-hr", methods=["GET", "POST"])
def api_max_hr():
    if request.method == "POST":
        body = request.get_json(force=True) or {}
        cfg = load_config()
        v = body.get("override")
        if v in (None, "", 0):
            cfg.pop("max_hr_override", None)
        else:
            try:
                cfg["max_hr_override"] = int(v)
            except (TypeError, ValueError):
                abort(400)
        save_config(cfg)
    cfg = load_config()
    return jsonify({
        "observed":  _observed_max_hr(),
        "override":  cfg.get("max_hr_override"),
        "effective": _effective_max_hr(),
    })


# In-process dedup for TZ resolution. Keyed on the same 2-decimal rounding
# that `_weather_cache_path` uses, so two activities at the "same" location
# share a single lookup regardless of date.
#
# Successful resolutions are cached indefinitely. Failures are cached with a
# short TTL via `_TZ_LRU_FAIL` so that a transient Open-Meteo outage during
# `_prewarm` doesn't poison the entire process — once the TTL expires, a
# fresh call retries. This balances cold-start dedup (the whole point) with
# self-healing after temporary network blips.
_TZ_LRU: dict[tuple[float, float], str] = {}
_TZ_LRU_FAIL: dict[tuple[float, float], float] = {}
_TZ_LRU_FAIL_TTL_SEC = 60.0


def _weather_timezone_name(lat: float, lon: float, date_str: str) -> str | None:
    """Return the IANA timezone name (e.g. 'America/Edmonton') for a location.

    Fetched via Open-Meteo (timezone=auto). Cached on disk per (lat, lon,
    date) and in-process per (round(lat,2), round(lon,2)). The latter is
    what keeps `_prewarm` fast: 6 threads parsing ~669 GPX files without
    the in-memory cache fire ~108 separate Open-Meteo calls; with it, the
    same coord cluster shares a single resolution.

    Note: `date_str` only affects which on-disk cache file is checked; the
    in-process dedup key is coordinates-only, since Open-Meteo's TZ result
    is location-only.

    Returning the name (rather than a single offset) lets us compute the
    historically-correct DST-aware offset for any datetime via zoneinfo,
    which matters for activities spanning the DST transition or recorded
    in winter (where Open-Meteo's `utc_offset_seconds` always reflects the
    *current* offset, not the date's).
    """
    key = (round(lat, 2), round(lon, 2))
    cached = _TZ_LRU.get(key)
    if cached:
        return cached
    fail_ts = _TZ_LRU_FAIL.get(key)
    if fail_ts is not None and time.time() - fail_ts < _TZ_LRU_FAIL_TTL_SEC:
        return None

    cp = _weather_cache_path(lat, lon, date_str)
    if cp.exists():
        try:
            entry = json.loads(cp.read_text(encoding="utf-8"))
            if entry.get("timezone_name"):
                _TZ_LRU[key] = entry["timezone_name"]
                _TZ_LRU_FAIL.pop(key, None)
                return entry["timezone_name"]
        except Exception:
            pass

    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat:.4f}&longitude={lon:.4f}"
        f"&start_date={date_str}&end_date={date_str}"
        "&daily=temperature_2m_max"
        "&timezone=auto"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AlanForks-GPX-Viewer/1.0"})
        # Tight timeout: Open-Meteo serves these in <500 ms when healthy;
        # longer waits are signalling either congestion or an outage, and
        # the 8 s default was the source of multi-minute prewarm stalls.
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tz_name = data.get("timezone")
        if not tz_name:
            _TZ_LRU_FAIL[key] = time.time()
            return None
        entry = {}
        if cp.exists():
            try: entry = json.loads(cp.read_text(encoding="utf-8"))
            except Exception: entry = {}
        entry["timezone_name"] = tz_name
        # Keep utc_offset_sec around for any legacy reader, but the truth lives
        # in timezone_name now.
        if data.get("utc_offset_seconds") is not None:
            entry["utc_offset_sec"] = int(data["utc_offset_seconds"])
        # Persist the rounded coords inside the entry so a startup prewarm
        # can repopulate `_TZ_LRU` from existing cache files without parsing
        # any GPX. Without this, the LRU stays empty after a restart and
        # every worker thread on a storm pays a disk read for the same
        # coord cluster.
        entry["tz_lru_key"] = [round(lat, 2), round(lon, 2)]
        entry.setdefault("fetched", int(time.time()))
        _atomic_write(cp, json.dumps(entry, ensure_ascii=False))
        _TZ_LRU[key] = tz_name
        _TZ_LRU_FAIL.pop(key, None)
        return tz_name
    except Exception:
        _TZ_LRU_FAIL[key] = time.time()
        return None


def _garmin_status() -> dict:
    """Read persistence dropped by garmin_sync.py."""
    status = {"configured": False, "last_sync": None, "user": None, "method": None}
    # Library-saved auth: older releases used oauth1/oauth2_token.json; newer
    # releases save a single garmin_tokens.json. Accept either.
    has_tokens = _TOKEN_DIR.exists() and (
        any(_TOKEN_DIR.glob("oauth*.json"))
        or (_TOKEN_DIR / "garmin_tokens.json").exists()
    )
    has_curl   = (_TOKEN_DIR / "garmin_curl.txt").exists()
    status["has_library_auth"] = has_tokens
    status["has_curl_auth"]    = has_curl
    status["configured"]       = has_tokens or has_curl
    if _STATUS_FILE.exists():
        try:
            s = json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
            status["user"]       = s.get("user")
            status["last_login"] = s.get("last_login")
            status["last_sync"]  = s.get("last_sync")
            status["last_synced"]= s.get("last_synced")
            status["total_dates"]= s.get("total_dates")
            status["method"]     = s.get("method")
        except Exception:
            pass
    try:
        status["cached_dates"] = len(list(HR_CACHE_DIR.glob("*.json")))
    except Exception:
        status["cached_dates"] = 0
    return status


@app.route("/debug/hr/<date_str>")
def debug_hr_day(date_str):
    """Render the raw cached Garmin daily HR for a date — no activity matching,
    just the full 24-hour sample stream so you can sanity-check the signal.
    """
    if not _DATE_RE.match(date_str):
        abort(400)
    fp = HR_CACHE_DIR / f"{date_str}.json"
    if not fp.exists():
        return f"No HR cache for {date_str}", 404
    try:
        payload = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("debug_hr_day: failed to parse %s: %s", fp.name, e)
        return f"HR cache for {date_str} is corrupt", 500
    samples = [[ms, bpm] for ms, bpm in (payload.get("samples") or []) if bpm is not None]
    return render_template("debug_hr.html", date_str=date_str, samples=json.dumps(samples),
                           resting=payload.get("resting"), max_day=payload.get("max_day"),
                           min_day=payload.get("min_day"), total=len(samples))


@app.route("/api/garmin/status")
def api_garmin_status():
    return jsonify(_garmin_status())


# ─── Historical weather (Open-Meteo) ─────────────────────────────────────────
# Free API, no key required. Hourly UTC data is cached by (rounded lat, lon, date)
# — the raw hourly day stays on disk forever; summaries are computed per request
# so the same day can be windowed differently for different activities.

_WEATHER_TTL_SEC = 365 * 24 * 3600
_WEATHER_CACHE_VERSION = 2   # bump when hourly schema or timezone convention changes


def _weather_cache_path(lat: float, lon: float, date_str: str) -> Path:
    key = f"{round(lat, 2)}_{round(lon, 2)}_{date_str}"
    h = hashlib.md5(key.encode()).hexdigest()[:12]
    return CACHE_DIR / "weather" / f"{h}.json"


def _fetch_hourly_day(lat: float, lon: float, date_str: str) -> dict | None:
    """Fetch a full day of hourly weather data in the location's LOCAL timezone.
    Trailforks (and many other tools) export GPX timestamps as 'wall clock' local
    time with a fabricated +00:00 offset, so comparing against local-tz hourly
    data matches the user's expectation of when the ride actually happened.
    """
    cp = _weather_cache_path(lat, lon, date_str)
    if cp.exists():
        try:
            entry = json.loads(cp.read_text(encoding="utf-8"))
            if (entry.get("version") == _WEATHER_CACHE_VERSION
                    and time.time() - entry.get("fetched", 0) < _WEATHER_TTL_SEC
                    and "hourly" in entry):
                return entry["hourly"]
        except Exception:
            pass

    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat:.4f}&longitude={lon:.4f}"
        f"&start_date={date_str}&end_date={date_str}"
        "&hourly=temperature_2m,precipitation,snowfall,windspeed_10m,weathercode"
        "&timezone=auto"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AlanForks-GPX-Viewer/1.0"})
        # Short timeout: this runs synchronously in the request thread (up to two
        # calls per ride when it crosses midnight), so a slow upstream must not
        # stall the worker — same rationale as the timeout=2 tz lookup.
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        hourly = data.get("hourly", {}) or {}
        if not hourly.get("time"):
            return None
        _atomic_write(cp, json.dumps({
            "version": _WEATHER_CACHE_VERSION,
            "fetched": time.time(),
            "hourly":  hourly,
        }, ensure_ascii=False))
        return hourly
    except Exception:
        return None


def _fetch_weather(lat: float, lon: float, start_iso: str, end_iso: str) -> dict | None:
    """Summarize weather over the actual activity window.

    GPX timestamps are treated as naive local times at the track's location
    (Trailforks-style). We fetch Open-Meteo hourly data in the same local
    timezone (timezone=auto) and compare wall-clock to wall-clock.
    """
    try:
        # Strip tz offset — the offset is unreliable for GPX exports
        start = datetime.fromisoformat(start_iso).replace(tzinfo=None)
        end   = datetime.fromisoformat(end_iso).replace(tzinfo=None)
    except Exception:
        return None
    if end <= start:
        end = start + timedelta(hours=1)

    # Pull start + end day (at most two) in case the activity crosses midnight
    dates_needed = {start.date().isoformat(), end.date().isoformat()}
    temps, precips, snows, winds, codes = [], [], [], [], []

    for date_str in sorted(dates_needed):
        hourly = _fetch_hourly_day(lat, lon, date_str)
        if not hourly:
            continue
        times = hourly.get("time", []) or []
        T = hourly.get("temperature_2m", []) or []
        P = hourly.get("precipitation",  []) or []
        S = hourly.get("snowfall",       []) or []
        W = hourly.get("windspeed_10m",  []) or []
        C = hourly.get("weathercode",    []) or []
        for i, t_str in enumerate(times):
            try:
                t = datetime.fromisoformat(t_str)   # naive local
            except Exception:
                continue
            hour_end = t + timedelta(hours=1)
            if hour_end > start and t < end:
                if i < len(T) and T[i] is not None: temps.append(T[i])
                if i < len(P) and P[i] is not None: precips.append(P[i])
                if i < len(S) and S[i] is not None: snows.append(S[i])
                if i < len(W) and W[i] is not None: winds.append(W[i])
                if i < len(C) and C[i] is not None: codes.append(C[i])

    if not temps:
        return None

    code = None
    if codes:
        freq = {}
        for c in codes: freq[c] = freq.get(c, 0) + 1
        code = max(freq, key=freq.get)

    return {
        "tmin":   min(temps),
        "tmax":   max(temps),
        "precip": round(sum(precips), 2) if precips else 0.0,
        "snow":   round(sum(snows),   2) if snows   else 0.0,
        "wind":   max(winds) if winds else 0.0,
        "code":   code,
    }


@app.route("/api/activity/<filename>/weather")
def api_weather(filename):
    if _safe_gpx_path(filename) is None:
        abort(404)
    data = get_activity(filename)
    if not data or not data.get("date"):
        return jsonify({})
    pts = data.get("points", [])
    if not pts:
        return jsonify({})
    lat = sum(p["lat"] for p in pts) / len(pts)
    lon = sum(p["lon"] for p in pts) / len(pts)
    start_iso = data["date"]
    duration  = (data.get("stats") or {}).get("duration_sec") or 3600
    try:
        start_naive = datetime.fromisoformat(start_iso).replace(tzinfo=None)
        end_naive   = start_naive + timedelta(seconds=duration)
        start_iso, end_iso = start_naive.isoformat(), end_naive.isoformat()
    except Exception:
        end_iso = start_iso
    return jsonify(_fetch_weather(lat, lon, start_iso, end_iso) or {})


# ─── Regions ──────────────────────────────────────────────────────────────────

_regions_cache: list | None = None
_regions_lock = threading.Lock()


def load_regions() -> list[dict]:
    global _regions_cache
    if _regions_cache is not None:
        return _regions_cache
    with _regions_lock:
        if _regions_cache is not None:
            return _regions_cache
        if REGIONS_FILE.exists():
            try:
                _regions_cache = json.loads(REGIONS_FILE.read_text(encoding="utf-8"))
                return _regions_cache
            except Exception:
                pass
        _regions_cache = []
        return _regions_cache


def _region_by_id(region_id: str) -> dict | None:
    """The region dict with this id, or None. `load_regions()` is in-memory
    cached, so the linear scan is over the already-loaded small list."""
    return next((r for r in load_regions() if r["id"] == region_id), None)


def save_regions(regions: list[dict]):
    global _regions_cache, _regions_geom_hash_cached
    _atomic_write(REGIONS_FILE, json.dumps(regions, indent=2, ensure_ascii=False))
    with _regions_lock:
        _regions_cache = regions
    # Activities cache holds each entry's matched-regions list; without this,
    # adding/editing/deleting a region wouldn't refresh the sidebar `regions`
    # field nor /api/regions/untagged on Windows where mtime resolution is
    # coarse enough to occasionally miss a same-second save.
    _invalidate_activities_cache()
    # The /api/regions[/<id>] handlers mutate the cached regions list in place
    # (regions.append(...) / regions.pop(...)), so id(regions) is unchanged
    # across saves. _regions_geom_hash has a fast-path keyed on id(regions) —
    # without busting it, the hash stays stale and _regions_match_cache keeps
    # serving the old per-track matches indefinitely.
    _regions_geom_hash_cached = None
    with _regions_match_cache_lock:
        _regions_match_cache.clear()


def _stats_from_trimmed(pts: list, segments: list, base_stats: dict) -> dict:
    """Recompute the stats overlay (distance, climbing, descent, duration,
    average / max speed, assisted gain) from a trimmed point list. Falls back
    to base_stats fields where it can't compute (e.g., max_speed when no
    point has speed data)."""
    n = len(pts)
    if n < 2:
        return base_stats
    duration = 0
    try:
        t0 = datetime.fromisoformat(pts[0]["time"])
        t1 = datetime.fromisoformat(pts[-1]["time"])
        duration = int((t1 - t0).total_seconds())
    except Exception:
        pass
    # _build_segments shares boundary indices between adjacent segments for
    # rendering continuity (assisted segment starts at i-1 when the transition
    # hits index i). For per-point stats, index i's delta-to-arrive belongs to
    # is_assisted[i]'s type — which is the NEXT segment's type, not this one's
    # boundary share. Skip each segment's start index so attribution matches
    # _compute_algo_stats.
    assisted = set()
    for s in segments or []:
        if s.get("type") == "assisted":
            for k in range(s["start"] + 1, s["end"] + 1):
                assisted.add(k)
    # Parse per-point times once so we can accumulate riding duration
    # (avg_speed uses riding time only — see convention in parse_gpx).
    parsed_times: list = []
    for p in pts:
        try:
            parsed_times.append(datetime.fromisoformat(p["time"]))
        except Exception:
            parsed_times.append(None)
    riding_dist = riding_gain = riding_loss = assisted_gain = 0.0
    riding_dur_sec = 0.0
    # Mirror parse_gpx: gain/loss accumulation runs on a ma20-smoothed
    # elevation series so trim and spike-repair produce numbers consistent
    # with the parse-time stat. The raw `ele` field on each point is
    # untouched. Write the re-smoothed series back onto each point's
    # `ele_sm` so JS recomputeStats stays consistent at trim/repair
    # boundaries (where parse-time `ele_sm` was smoothed against now-absent
    # neighbours).
    smooth_ele = _smooth_elevations([p.get("ele") for p in pts])
    for p, sm in zip(pts, smooth_ele):
        p["ele_sm"] = round(sm, 1) if sm is not None else None
    for i in range(1, n):
        dd = pts[i]["dist_km"] - pts[i-1]["dist_km"]
        e_prev, e_cur = smooth_ele[i-1], smooth_ele[i]
        de = (e_cur - e_prev) if (e_prev is not None and e_cur is not None) else 0
        if i in assisted:
            if de > 0: assisted_gain += de
        else:
            riding_dist += dd
            if   de > 0: riding_gain += de
            elif de < 0: riding_loss -= de
            tp, tc = parsed_times[i-1], parsed_times[i]
            if tp is not None and tc is not None:
                dt = (tc - tp).total_seconds()
                if dt > 0:
                    riding_dur_sec += dt
    speeds    = [p["speed"] for p in pts if p.get("speed") is not None]
    max_speed = max(speeds) if speeds else base_stats.get("max_speed_kmh")
    # Riding-only max: excludes any sample inside an assisted segment, INCLUDING
    # the segment's start index. (`assisted` above skips start to keep elevation
    # attribution matched to _compute_algo_stats; that boundary-share convention
    # doesn't apply to speed — the segment-start sample is still lift telemetry,
    # not a riding sample.)
    assisted_for_speed = set()
    for s in segments or []:
        if s.get("type") == "assisted":
            for k in range(s["start"], s["end"] + 1):
                assisted_for_speed.add(k)
    riding_speeds = [p["speed"] for i, p in enumerate(pts)
                     if i not in assisted_for_speed and p.get("speed") is not None]
    max_speed_riding = (max(riding_speeds) if riding_speeds
                        else base_stats.get("max_speed_kmh_riding"))
    avg_speed = (riding_dist / (riding_dur_sec / 3600)) if riding_dur_sec > 0 else None
    return {
        **base_stats,
        "distance_km":     round(riding_dist, 2),
        "duration_sec":    duration,
        "elev_gain_m":     round(riding_gain),
        "elev_loss_m":     round(riding_loss),
        "assisted_gain_m": round(assisted_gain),
        "avg_speed_kmh":   round(avg_speed, 1) if avg_speed is not None else None,
        "max_speed_kmh":   max_speed,
        "max_speed_kmh_riding": max_speed_riding,
    }


def _apply_smoothing(data: dict, smoothing) -> dict:
    """Replace each point's lat/lon with the moving average over a window of
    neighbouring points, then re-derive cumulative dist_km, bbox, and stats.
    Non-destructive — original GPX unchanged.

    `smoothing` accepts either an int (window, whole-track) or a dict of
    `{window, start_km?, end_km?}`. When start_km/end_km are set, only points
    whose original dist_km falls within the range are smoothed; points outside
    keep their raw lat/lon. The moving-average window still reads neighbours
    from the full track so the transition at the boundary is continuous.
    """
    pts = data.get("points") or []
    # Accept legacy int window signature for callers that pre-date range support
    if isinstance(smoothing, dict):
        window   = int(smoothing.get("window") or 1)
        start_km = smoothing.get("start_km")
        end_km   = smoothing.get("end_km")
    else:
        window, start_km, end_km = int(smoothing or 1), None, None
    window = max(1, min(15, window))
    if window <= 1 or len(pts) < window:
        return data

    n = len(pts)
    # Decide which indices to smooth. An unset range means "whole track".
    full_dist = pts[-1].get("dist_km") or 0
    lo_km = float(start_km) if start_km is not None else 0.0
    hi_km = float(end_km)   if end_km   is not None else full_dist
    range_active = (lo_km > 0.0) or (hi_km < full_dist)
    in_zone = [lo_km <= (p.get("dist_km") or 0) <= hi_km for p in pts] if range_active else [True] * n

    half = window // 2
    smoothed = []
    for i in range(n):
        np = dict(pts[i])
        if in_zone[i]:
            lo = max(0, i - half)
            hi = min(n, i + half + 1)
            np["lat"] = sum(p["lat"] for p in pts[lo:hi]) / (hi - lo)
            np["lon"] = sum(p["lon"] for p in pts[lo:hi]) / (hi - lo)
        smoothed.append(np)

    # Re-derive cumulative distance from the smoothed positions
    cum_km = 0.0
    smoothed[0]["dist_km"] = 0.0
    for i in range(1, n):
        p0 = (smoothed[i-1]["lat"], smoothed[i-1]["lon"])
        p1 = (smoothed[i]["lat"],   smoothed[i]["lon"])
        cum_km += haversine(p0, p1) / 1000
        smoothed[i]["dist_km"] = round(cum_km, 4)
    # Re-derive instantaneous speed from new dist + existing time
    for i in range(1, n):
        t_prev, t_cur = smoothed[i-1].get("time"), smoothed[i].get("time")
        if t_prev and t_cur:
            try:
                dt = (datetime.fromisoformat(t_cur) - datetime.fromisoformat(t_prev)).total_seconds()
                if dt > 0:
                    dd_km = smoothed[i]["dist_km"] - smoothed[i-1]["dist_km"]
                    smoothed[i]["speed"] = round(dd_km / (dt / 3600), 2)
            except Exception:
                pass
    if smoothed:
        smoothed[0]["speed"] = smoothed[1].get("speed", 0) if len(smoothed) > 1 else 0

    bbox = (
        min(p["lat"] for p in smoothed), min(p["lon"] for p in smoothed),
        max(p["lat"] for p in smoothed), max(p["lon"] for p in smoothed),
    )
    new_stats = _stats_from_trimmed(smoothed, data.get("segments") or [], data.get("stats") or {})
    applied = {"window": window}
    if range_active:
        applied["start_km"] = round(lo_km, 3)
        applied["end_km"]   = round(hi_km, 3)
    return {**data, "points": smoothed, "bbox": bbox, "stats": new_stats,
            "smoothing_applied": applied}


# ─── GPS phantom-warp detection + repair ───────────────────────────────────
# Some GPS exports (notably Strava's stitched re-uploads) emit clusters of
# 1-3 samples whose implied inter-point speed is wildly above the
# surrounding ride. The existing speed clamp at parse time (>150 km/h)
# misses them because they cluster around 100-130 km/h, and a length-5
# median filter passes a 3-sample plateau through unchanged. Detection
# here uses local-median context instead of an absolute threshold so it
# scales to whatever activity speed the rider is actually doing.

# A leg counts as a phantom if its implied km/h is > _SPIKE_RATIO × the
# median of nearby legs AND above _SPIKE_FLOOR_KMH absolute. The floor
# stops slow-walking GPS noise (where a 0.3→1.2 m/s jump is also a 4×
# multiple of the local median) from constantly flagging.
# A wider window helps the median resist contamination by adjacent
# phantoms — a 3-sample warp cluster is less than a quarter of 21 samples,
# so the median still reflects the true ride pace.
_SPIKE_WINDOW = 10           # ± samples for the local-median context
_SPIKE_RATIO = 4.0
# Floor stops slow-walking GPS jitter (where 0.3→1.5 m/s blips are 5× the
# local median) from constantly tripping. 40 km/h is well above any
# plausible MTB / hike / fat-bike pace but below typical phantom warps.
_SPIKE_FLOOR_KMH = 40.0
# Absolute upper bound — anything implying speeds beyond this is flagged
# regardless of local context. Necessary because warp clusters can be
# longer than the median window (e.g. 40+ consecutive samples), which
# contaminates the median itself and hides the cluster from the ratio
# test. 80 km/h is safely above any of the user's activity types
# (MTB / snowboard / hike / fat-bike) but below most phantom warps.
_SPIKE_HARD_CAP_KMH = 80.0

# Surface-threshold thresholds — when an activity is "spike-flagged" for
# /review and /logs purposes. Three or more flagged samples in total OR a
# single very-fast warp. Centralised here because four callers used to
# copy-paste the same predicate.
_SPIKE_FLAG_MIN_COUNT = 3
_SPIKE_FLAG_PEAK_KMH = 80.0


def _is_spike_flagged(n_spikes: int, max_implied_kmh: float) -> bool:
    """Whether this activity's spike scan should surface on /review and
    "flagged" filters. Single point of truth for the threshold."""
    return n_spikes >= _SPIKE_FLAG_MIN_COUNT or (
        n_spikes >= 1 and max_implied_kmh >= _SPIKE_FLAG_PEAK_KMH
    )


def _find_speed_spikes(pts: list) -> tuple:
    """Detect phantom-warp legs. Returns (mask, max_implied_kmh):
       - mask[i] is True when the leg from pts[i-1] to pts[i] implies an
         outlier speed. Index 0 is always False — there's no leg into the
         first point.
       - max_implied_kmh is the highest implied km/h across all flagged
         legs. 0.0 when nothing was flagged.
    The implied speed comes from raw haversine / dt, which is what made
    the warp anomalous in the first place — the per-point `speed` field
    has already been median-filtered and may hide it.

    Legs touching an assisted (lift/shuttle) sample are ignored entirely:
    real vehicle/lift speeds aren't phantom warps, and including them in
    the local-median window contaminates the threshold for riding samples
    near the boundary. Detection runs cleanly on the riding portions.
    """
    n = len(pts)
    if n < 2 * _SPIKE_WINDOW + 1:
        return [False] * n, 0.0
    speeds = [0.0] * n
    for i in range(1, n):
        # Skip legs where either endpoint is on a lift/shuttle. Leaving
        # speeds[i] = 0 makes the median-window filter (speeds[j] > 0) skip
        # this leg naturally, and the spike-flagging loop's floor check
        # ensures it can never be flagged.
        if pts[i].get("assisted") or pts[i-1].get("assisted"):
            continue
        t_prev, t_cur = pts[i-1].get("time"), pts[i].get("time")
        if not t_prev or not t_cur:
            continue
        try:
            dt = (datetime.fromisoformat(t_cur) - datetime.fromisoformat(t_prev)).total_seconds()
        except Exception:
            continue
        if dt <= 0:
            continue
        d_m = haversine((pts[i-1]["lat"], pts[i-1]["lon"]),
                        (pts[i]["lat"],   pts[i]["lon"]))
        speeds[i] = (d_m / dt) * 3.6
    phantom = [False] * n
    max_implied = 0.0
    for i in range(1, n):
        if speeds[i] <= _SPIKE_FLOOR_KMH:
            continue
        lo = max(1, i - _SPIKE_WINDOW)
        hi = min(n, i + _SPIKE_WINDOW + 1)
        nbr = sorted(speeds[j] for j in range(lo, hi) if j != i and speeds[j] > 0)
        if not nbr:
            continue
        med = nbr[len(nbr) // 2]
        if speeds[i] > max(med * _SPIKE_RATIO, _SPIKE_FLOOR_KMH):
            phantom[i] = True
            if speeds[i] > max_implied:
                max_implied = speeds[i]
    return phantom, max_implied


def _apply_spike_repair(data: dict, phantom_mask: list | None = None) -> dict:
    """Repair a track by clamping the distance contribution of each phantom
    leg to the local-median leg length, and snapping the phantom point's
    lat/lon to the nearest non-phantom anchor so the map renders the warp
    as a straight skip rather than a 100 km/h jag. Re-derives cumulative
    distance, per-point speed, bbox, and stats. Original GPX is untouched
    — this is an at-request transform driven by meta.spike_repair = True.

    We don't try to interpolate phantom positions back onto a "true" path
    because in practice the warps are time-compression artifacts (the GPX
    reports 5 s elapsed for 150 m of trail, with the underlying positions
    on a smooth track). Interpolating between the surrounding anchors just
    redistributes the same impossible speed across more samples.

    `phantom_mask`: optional precomputed result of `_find_speed_spikes(pts)`.
    Callers (like /api/speed-spikes/<f>/preview) that have just computed it
    pass it in to avoid the second redundant scan.
    """
    pts = data.get("points") or []
    n = len(pts)
    if n < 3:
        return data
    if phantom_mask is None:
        phantom, _ = _find_speed_spikes(pts)
    else:
        phantom = phantom_mask
    if not any(phantom):
        return data

    new_pts = [dict(p) for p in pts]

    # Raw haversine leg lengths from the original positions, used both for
    # accumulating clean legs and for finding the local median to clamp
    # phantom legs to.
    raw_legs = [0.0] * n
    for k in range(1, n):
        raw_legs[k] = haversine((pts[k-1]["lat"], pts[k-1]["lon"]),
                                (pts[k]["lat"],   pts[k]["lon"])) / 1000

    # Cumulative distance: for phantom legs, substitute the median of
    # non-phantom legs in the surrounding window. Falls back to 0 if every
    # neighbouring leg is itself a phantom (rare; only on a long run of
    # consecutive warps with no clean anchor inside the window).
    new_pts[0]["dist_km"] = 0.0
    cum = 0.0
    for k in range(1, n):
        if phantom[k]:
            lo = max(1, k - _SPIKE_WINDOW)
            hi = min(n, k + _SPIKE_WINDOW + 1)
            nbr = sorted(raw_legs[j] for j in range(lo, hi) if j != k and not phantom[j])
            cum += nbr[len(nbr) // 2] if nbr else 0.0
        else:
            cum += raw_legs[k]
        new_pts[k]["dist_km"] = round(cum, 4)

    # Snap phantom positions to the previous (or next, for early phantoms)
    # non-phantom anchor so the map draws a clean skip. Forward-fill is
    # enough for the common case; one backward pass handles phantoms that
    # appear before the first clean point.
    last_good = None
    for k in range(n):
        if phantom[k]:
            if last_good is not None:
                new_pts[k]["lat"] = pts[last_good]["lat"]
                new_pts[k]["lon"] = pts[last_good]["lon"]
        else:
            last_good = k
    if any(phantom[:last_good or 0]) or (last_good is None):
        next_good = None
        for k in range(n - 1, -1, -1):
            if phantom[k] and next_good is not None and \
               (new_pts[k]["lat"] == pts[k]["lat"] and new_pts[k]["lon"] == pts[k]["lon"]):
                new_pts[k]["lat"] = pts[next_good]["lat"]
                new_pts[k]["lon"] = pts[next_good]["lon"]
            elif not phantom[k]:
                next_good = k

    # Per-point speed re-derived from clamped cumulative distance, then
    # passed through the same k=5 median filter that parse_gpx applies so
    # max_speed stays consistent with how it's computed elsewhere.
    raw_speeds = [None] * n
    for k in range(1, n):
        t_prev, t_cur = new_pts[k-1].get("time"), new_pts[k].get("time")
        if not t_prev or not t_cur:
            continue
        try:
            dt = (datetime.fromisoformat(t_cur) - datetime.fromisoformat(t_prev)).total_seconds()
        except Exception:
            continue
        if dt > 0:
            dd_km = new_pts[k]["dist_km"] - new_pts[k-1]["dist_km"]
            raw_speeds[k] = dd_km / (dt / 3600)
    smoothed = _median_filter(raw_speeds, k=5)
    for k in range(n):
        if smoothed[k] is not None:
            new_pts[k]["speed"] = round(smoothed[k], 2)
    if n > 1 and new_pts[0].get("speed") is None:
        new_pts[0]["speed"] = new_pts[1].get("speed", 0)

    bbox = (
        min(p["lat"] for p in new_pts), min(p["lon"] for p in new_pts),
        max(p["lat"] for p in new_pts), max(p["lon"] for p in new_pts),
    )
    new_stats = _stats_from_trimmed(new_pts, data.get("segments") or [], data.get("stats") or {})
    return {**data, "points": new_pts, "bbox": bbox, "stats": new_stats,
            "spike_repair_applied": {"count": sum(phantom)}}


def _apply_time_shift(data: dict, hours) -> dict:
    """Shift every timestamp on a track by `hours` hours. Original GPX is
    untouched — at-request transform driven by `meta.time_shift_hours`.
    Used to correct activities whose timestamps were mis-localized at
    parse time (typically a 'fake UTC' producer not yet recognised in
    `_FAKE_UTC_CREATORS`).

    Per-point dt is preserved (every point shifts by the same amount),
    so distance / speed / spike detection / repair are unaffected. Only
    the displayed wall-clock changes. `date` may move across midnight
    if the shift crosses 00:00 — intended, since a mis-localized 02:00
    that should read 14:00 also belongs on the prior calendar day.
    """
    try:
        h = int(hours or 0)
    except (TypeError, ValueError):
        return data
    if not h:
        return data
    delta = timedelta(hours=h)

    def _shift(iso):
        if not iso:
            return iso
        try:
            return (datetime.fromisoformat(iso) + delta).isoformat()
        except Exception:
            return iso

    pts = data.get("points") or []
    new_pts = [{**p, "time": _shift(p.get("time"))} if p.get("time") else p
               for p in pts]
    out = {**data, "points": new_pts, "time_shift_applied_hours": h}
    for k in ("start_time", "end_time"):
        v = data.get(k)
        if v:
            out[k] = _shift(v)
    # Re-derive `date` from the (now shifted) start_time so sidebar
    # filtering and grouping land on the corrected day.
    new_start = out.get("start_time") or (new_pts[0].get("time") if new_pts else None)
    if new_start:
        try:
            out["date"] = datetime.fromisoformat(new_start).strftime("%Y-%m-%d")
        except Exception:
            pass
    return out


def _apply_trim(data: dict, trim: dict) -> dict:
    """Slice an activity to [start_km, end_km] (in original distances).
    Re-bases dist_km so the trimmed track starts at 0, adjusts segment
    indices, and recomputes stats. Returns a new data dict; non-destructive."""
    pts = data.get("points") or []
    if not pts or not trim:
        return data
    full_dist = pts[-1]["dist_km"]
    start_km = float(trim.get("start_km") or 0)
    end_km_raw = trim.get("end_km")
    end_km   = float(end_km_raw) if end_km_raw is not None else full_dist
    if start_km <= 0 and end_km >= full_dist:
        return data  # no-op trim
    start_idx = next((i for i, p in enumerate(pts) if p["dist_km"] >= start_km), 0)
    end_idx = len(pts) - 1
    for i in range(len(pts) - 1, -1, -1):
        if pts[i]["dist_km"] <= end_km:
            end_idx = i
            break
    if end_idx <= start_idx:
        return data
    base = pts[start_idx]["dist_km"]
    new_pts = []
    for p in pts[start_idx:end_idx + 1]:
        np = dict(p)
        np["dist_km"] = round(p["dist_km"] - base, 3)
        new_pts.append(np)
    new_segs = []
    for s in (data.get("segments") or []):
        ns = max(0, s["start"] - start_idx)
        ne = min(len(new_pts) - 1, s["end"] - start_idx)
        if ne > ns:
            new_segs.append({**s, "start": ns, "end": ne})
    bbox = (
        min(p["lat"] for p in new_pts), min(p["lon"] for p in new_pts),
        max(p["lat"] for p in new_pts), max(p["lon"] for p in new_pts),
    )
    new_stats = _stats_from_trimmed(new_pts, new_segs, data.get("stats") or {})
    return {**data,
            "points": new_pts, "segments": new_segs, "bbox": bbox, "stats": new_stats,
            "trim_full_distance_km": round(full_dist, 2)}


# ─── Issue-detection cache ────────────────────────────────────────────────────
# Keyed by (filename, gpx_mtime, distance_km, duration_sec) — the stats tuple
# fingerprints trim and smoothing (they change distance/duration) without
# hashing the whole point list. Pure function of the GPX + metadata, so long-
# lived entries are safe to keep.
_issues_cache: dict[tuple, list] = {}
_issues_cache_lock = threading.Lock()
_ISSUES_CACHE_MAX  = 1000


def _detect_issues_cached(eff: dict) -> list[dict]:
    filename = eff.get("filename")
    if not filename:
        return _detect_issues(eff)
    try:
        mtime = (GPX_DIR / filename).stat().st_mtime
    except OSError:
        return _detect_issues(eff)
    stats = eff.get("stats") or {}
    # CACHE_VERSION in the key so a code/threshold change that affects what
    # _detect_issues flags (e.g. the per-segment spike skip introduced with
    # ALGO_SIG v13) doesn't get masked by entries from a previous process.
    key = (CACHE_VERSION, filename, mtime, stats.get("distance_km"), stats.get("duration_sec"))
    with _issues_cache_lock:
        cached = _issues_cache.get(key)
        if cached is not None:
            return cached
    result = _detect_issues(eff)
    with _issues_cache_lock:
        _issues_cache[key] = result
        if len(_issues_cache) > _ISSUES_CACHE_MAX:
            # Drop the oldest 20% in insertion order (dict preserves it)
            drop = int(_ISSUES_CACHE_MAX * 0.2)
            for k in list(_issues_cache.keys())[:drop]:
                _issues_cache.pop(k, None)
    return result


def _detect_issues(eff: dict) -> list[dict]:
    """Detect likely-bogus recording patterns. Returns a list of
        {code, severity, msg}
    dicts; empty list means nothing flagged. Cheap enough to run for every
    activity in `all_activities()`."""
    issues = []
    stats = eff.get("stats") or {}
    pts   = eff.get("points") or []
    dist  = stats.get("distance_km") or 0
    dur   = stats.get("duration_sec") or 0

    # 1. Trivially short — likely a test recording or accidental start
    if dist < 0.2:
        issues.append({"code": "tiny_distance", "severity": "low",
                       "msg": f"Only {dist*1000:.0f} m of distance"})
    if 0 < dur < 60:
        issues.append({"code": "tiny_duration", "severity": "low",
                       "msg": f"Only {int(dur)} s long"})

    # 2. Impossible max speed
    max_spd = stats.get("max_speed_kmh") or 0
    if max_spd > 150:
        issues.append({"code": "max_speed", "severity": "high",
                       "msg": f"Max speed {max_spd:.0f} km/h"})

    # 3. Implausible climb rate (m gained per km of distance)
    gain = stats.get("elev_gain_m") or 0
    if dist > 0.5 and gain > 0 and (gain / dist) > 200:
        issues.append({"code": "climb_rate", "severity": "med",
                       "msg": f"{gain:.0f} m climb over {dist:.1f} km ({gain/dist:.0f} m/km)"})

    # 4. GPS teleport — large spatial jump in a short time. A jump with a
    # large time delta is just pause-and-resume (lunch, gondola), not a
    # GPS error, so we require dt < 5 min to flag.
    if len(pts) >= 2:
        max_jump = 0.0
        for i in range(1, len(pts)):
            d = pts[i]["dist_km"] - pts[i-1]["dist_km"]
            if d <= 1.0 or d <= max_jump:
                continue
            t_prev, t_cur = pts[i-1].get("time"), pts[i].get("time")
            if not t_prev or not t_cur:
                continue
            try:
                dt = (datetime.fromisoformat(t_cur) - datetime.fromisoformat(t_prev)).total_seconds()
            except Exception:
                continue
            if dt < 300:  # under 5 minutes — impossible velocity
                max_jump = d
        if max_jump > 1.0:
            issues.append({"code": "teleport", "severity": "high",
                           "msg": f"GPS jump of {max_jump:.1f} km in under 5 min"})

    # 5. Speed spikes — short clusters of points whose implied inter-point
    # speed is wildly out of line with the surrounding ride. Distinct from
    # rule 2 (absolute >150 km/h cap, which is rare and obvious) and rule 4
    # (>1 km position jump, which is teleport not phantom). Catches Strava-
    # export warps in the 60-130 km/h range that the absolute cap misses.
    # Only fires when there's enough evidence that stats are likely
    # impacted: ≥3 spikes, or a single high-magnitude spike (>80 km/h).
    # Lower-confidence single spikes get logged silently so the repair
    # button is still available, but don't create a noisy issue card.
    if pts and not eff.get("spike_repair_applied"):
        spikes, max_implied = _find_speed_spikes(pts)
        n_spikes = sum(spikes)
        if _is_spike_flagged(n_spikes, max_implied):
            issues.append({"code": "speed_spike", "severity": "med",
                           "msg": f"{n_spikes} GPS sample{'' if n_spikes == 1 else 's'} with implausible inter-point speed (peak {max_implied:.0f} km/h)"})

    # 6. GPS jitter — recorded path much longer than a smoothed path. Compute
    # a 5-point moving-average path and compare cumulative lengths.
    if len(pts) >= 30 and dist > 1.0:
        win = 5
        smoothed = []
        for i in range(len(pts)):
            lo = max(0, i - win // 2)
            hi = min(len(pts), i + win // 2 + 1)
            window = pts[lo:hi]
            slat = sum(p["lat"] for p in window) / len(window)
            slon = sum(p["lon"] for p in window) / len(window)
            smoothed.append((slat, slon))
        smoothed_km = 0.0
        for i in range(1, len(smoothed)):
            smoothed_km += haversine(smoothed[i-1], smoothed[i]) / 1000
        if smoothed_km > 0:
            ratio = dist / smoothed_km
            if ratio > 1.3:
                issues.append({"code": "jitter", "severity": "med",
                               "msg": f"Recorded distance is {ratio:.2f}× the smoothed path (GPS noise)"})
    return issues


def _effective_type_for(meta_type: str, region_ids: list[str], regions: list[dict],
                        activity_date: str = "") -> str:
    """Type that should drive UI / algorithm choice. Explicit meta.type wins;
    otherwise fall back to the first matched region's seasonal default
    (winter_default_type when activity_date is in winter_months) or its plain
    default_type. activity_date should be an ISO 'YYYY-MM-DD…' string."""
    if meta_type:
        return meta_type
    if not region_ids:
        return ""
    month = 0
    if activity_date and len(activity_date) >= 7:
        try: month = int(activity_date[5:7])
        except ValueError: month = 0
    by_id = {r["id"]: r for r in regions}
    for rid in region_ids:
        r = by_id.get(rid) or {}
        # Winter-specific default takes precedence when the activity falls in
        # the region's winter months and the region has a winter type set.
        winter_months = r.get("winter_months") or [11, 12, 1, 2, 3, 4]
        if month and month in winter_months and r.get("winter_default_type"):
            return r["winter_default_type"]
        if r.get("default_type"):
            return r["default_type"]
    return ""


def _is_effectively_assisted(meta: dict, region_ids: list, regions: list) -> bool:
    """True when an activity should be treated as lift- or shuttle-assisted.

    Activity-level `meta.assisted` overrides everything (set to True or False
    explicitly to force a per-ride answer). When unset, fall back to any
    matched region having `assisted: True` — typical use is flagging a ski
    hill or DH bike park so every ride there is auto-tagged.

    Used to exclude lapping rides from distance/duration record rankings
    where they'd otherwise dominate against point-to-point efforts.
    """
    if isinstance(meta, dict) and meta.get("assisted") is not None:
        return bool(meta["assisted"])
    if not region_ids:
        return False
    by_id = {r["id"]: r for r in regions}
    for rid in region_ids:
        if (by_id.get(rid) or {}).get("assisted"):
            return True
    return False


# Cache of (filename, gpx_mtime, regions_geom_hash) → [region_ids]. Region
# matching is driven by the track centroid and polygon point-in-polygon; for
# our dataset that's O(regions) per activity per rebuild, which adds up to
# ~25 s across 669 activities. Centroid drift from trim/smoothing is tiny
# relative to region polygons, so keying on the file mtime is safe — region
# matches won't flip when the user adjusts trim.
_regions_match_cache: dict[tuple, list] = {}
_regions_match_cache_lock = threading.Lock()
_REGIONS_MATCH_CACHE_MAX = 2000

_regions_geom_hash_cached: tuple[str, int] | None = None  # (hash, id(regions_list))


def _regions_geom_hash(regions: list[dict]) -> str:
    """Hash of region IDs + geometries. Stable across non-geometry edits
    (rename, default_type change) so those don't invalidate the match cache.
    Uses id(regions) as a fast-path key since `load_regions` returns the same
    cached list reference until `save_regions` runs.
    """
    global _regions_geom_hash_cached
    cached = _regions_geom_hash_cached
    if cached is not None and cached[1] == id(regions):
        return cached[0]
    h = hashlib.md5(repr([
        (r.get("id"), r.get("geometry") or {}) for r in regions
    ]).encode()).hexdigest()
    _regions_geom_hash_cached = (h, id(regions))
    return h


# Fraction of a track's points that must fall inside a region's polygon for
# the region to auto-tag. The previous implementation only matched on the
# centroid (one point) — that miscounted multi-region rides and missed
# tracks whose centroid landed in a polygon's hole or on a shared road.
_REGION_MATCH_THRESHOLD = 0.15
# Bumped whenever the matching algorithm changes so old cache entries
# (computed under different rules) are invalidated automatically.
_REGION_MATCH_VERSION = 2


def regions_for_track(data: dict, regions: list[dict]) -> list[str]:
    """Return ids of regions whose polygon contains ≥ _REGION_MATCH_THRESHOLD
    of the track's GPS points. Each region is evaluated independently, so
    a track that genuinely crosses multiple regions tags all of them.
    Per-region bbox prefilter rejects far-away points before the trig of
    the ray-cast. Memoized per (filename, gpx_mtime, regions_geom_hash,
    match_version)."""
    pts = data.get("points", [])
    if not pts:
        return []
    # Fast path: per-filename cache keyed on geometry hash + algo version
    filename = data.get("filename")
    cache_key = None
    if filename:
        try:
            mtime = (GPX_DIR / filename).stat().st_mtime
            cache_key = (filename, mtime, _regions_geom_hash(regions),
                         _REGION_MATCH_VERSION)
            with _regions_match_cache_lock:
                cached = _regions_match_cache.get(cache_key)
                if cached is not None:
                    return cached
        except OSError:
            cache_key = None
    matched: list[str] = []
    n_pts = len(pts)
    min_inside = max(1, int(n_pts * _REGION_MATCH_THRESHOLD))
    for r in regions:
        # GeoJSON polygon ring coordinates are [lon, lat]
        coords = r.get("geometry", {}).get("coordinates", [[]])[0]
        ring = [[c[1], c[0]] for c in coords]  # convert to [lat, lon]
        if not ring:
            continue
        # Bbox prefilter — points outside the polygon's bounding rectangle
        # can't be inside the polygon, and the four-comparison check is much
        # cheaper than the ray-cast.
        ring_lats = [p[0] for p in ring]
        ring_lons = [p[1] for p in ring]
        min_lat, max_lat = min(ring_lats), max(ring_lats)
        min_lon, max_lon = min(ring_lons), max(ring_lons)
        inside = 0
        # Early-out: as soon as we know inside count can't reach the
        # threshold, skip the rest of the points for this region.
        for i, p in enumerate(pts):
            lat = p["lat"]; lon = p["lon"]
            if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                if _point_in_polygon(lat, lon, ring):
                    inside += 1
                    if inside >= min_inside:
                        break
            # Optimistic upper bound: even if every remaining point is inside
            remaining = n_pts - 1 - i
            if inside + remaining < min_inside:
                break
        if inside >= min_inside:
            matched.append(r["id"])
    if cache_key is not None:
        with _regions_match_cache_lock:
            _regions_match_cache[cache_key] = matched
            if len(_regions_match_cache) > _REGIONS_MATCH_CACHE_MAX:
                drop = int(_REGIONS_MATCH_CACHE_MAX * 0.2)
                for k in list(_regions_match_cache.keys())[:drop]:
                    _regions_match_cache.pop(k, None)
    return matched


def _effective_regions(data: dict, file_meta: dict, regions: list[dict]) -> list[str]:
    """Geometry-matched region IDs plus any the user has explicitly pinned
    via `metadata[fn].regions_pinned`. Pins let the user force an activity to
    be associated with a region whose polygon doesn't cover the centroid —
    useful when GPS centroids land just outside a region you drew.

    Stale pin IDs (pointing at regions that have since been deleted) are
    silently dropped from the display but left in metadata; the next
    pin/unpin edit will naturally persist a clean list.
    """
    matched = regions_for_track(data, regions)
    pinned = file_meta.get("regions_pinned") or []
    if not pinned:
        return matched
    valid_ids = {r["id"] for r in regions}
    seen = set(matched)
    merged = list(matched)
    for rid in pinned:
        if rid in valid_ids and rid not in seen:
            merged.append(rid)
            seen.add(rid)
    return merged


# ─── Sync orchestration (Strava + Garmin) ────────────────────────────────────

_sync_state: dict = {
    "strava": {"running": False, "started_at": None, "finished_at": None,
               "ok": None, "message": "", "added": 0},
    "garmin": {"running": False, "started_at": None, "finished_at": None,
               "ok": None, "message": "", "added": 0},
}
_sync_state_lock = threading.Lock()


def _run_sync_subprocess(source: str, script: str) -> None:
    """Run a sync script as a subprocess and update _sync_state with results.
    Counts new files / dates added by diffing before/after."""
    with _sync_state_lock:
        _sync_state[source].update(running=True, started_at=int(time.time()),
                                   finished_at=None, ok=None, message="syncing…", added=0)
    try:
        if source == "strava":
            before_files = {p.name for p in (_ROOT / "tracks").glob("strava_*.gpx")}
        else:
            before = len(list((CACHE_DIR / "hr").glob("*.json"))) if (CACHE_DIR / "hr").exists() else 0

        proc = subprocess.run(
            [sys.executable, str(_ROOT / "sync" / script), "--sync"],
            capture_output=True, text=True, timeout=600,
        )

        new_files: list[str] = []
        if source == "strava":
            after_files = {p.name for p in (_ROOT / "tracks").glob("strava_*.gpx")}
            new_files = sorted(after_files - before_files)
            added = len(new_files)
            unit = "files"
        else:
            after = len(list((CACHE_DIR / "hr").glob("*.json"))) if (CACHE_DIR / "hr").exists() else 0
            added = max(0, after - before)
            unit = "dates"

        ok = proc.returncode == 0
        msg = f"{added} new {unit}" if ok else (proc.stderr or proc.stdout).strip().splitlines()[-1] if (proc.stderr or proc.stdout).strip() else "sync failed"
        with _sync_state_lock:
            _sync_state[source].update(running=False, finished_at=int(time.time()),
                                       ok=ok, message=msg, added=added)
        # Surgical refresh: build (and persist) only the newly-synced
        # entries instead of nuking the whole 554-entry sidebar cache.
        # `_update_activity_entry` also writes the per-file sidebar
        # cache, so the next request — or restart — sees the new files
        # without rebuilding anything else.
        if source == "strava" and new_files:
            for fn in new_files:
                try:
                    _update_activity_entry(fn)
                except Exception:
                    logger.exception("sidebar update failed for %s", fn)
            # New rides may include MTB+Moose-Mountain activities — kick
            # the trail prewarm so they land on the leaderboard without
            # waiting for the user to open them. The worker idempotently
            # scans for new uncached entries, so this is cheap.
            _prewarm_trail_matches_async()
    except subprocess.TimeoutExpired:
        with _sync_state_lock:
            _sync_state[source].update(running=False, finished_at=int(time.time()),
                                       ok=False, message="timed out after 10 min")
    except Exception as e:
        with _sync_state_lock:
            _sync_state[source].update(running=False, finished_at=int(time.time()),
                                       ok=False, message=f"error: {e}")


def _kick_sync(source: str) -> bool:
    """Start a background sync if not already running. Returns False if already in flight."""
    script = "strava_sync.py" if source == "strava" else "garmin_sync.py"
    with _sync_state_lock:
        if _sync_state[source]["running"]:
            return False
    threading.Thread(target=_run_sync_subprocess, args=(source, script),
                     name=f"{source}-sync", daemon=True).start()
    return True


@app.route("/api/sync/status")
def api_sync_status():
    with _sync_state_lock:
        return jsonify(dict(_sync_state))


@app.route("/api/sync/<source>", methods=["POST"])
def api_sync_trigger(source):
    if source not in ("strava", "garmin", "all"):
        abort(400)
    started = []
    for s in (("strava", "garmin") if source == "all" else (source,)):
        if _kick_sync(s):
            started.append(s)
    return jsonify({"started": started})


@app.route("/api/sync/settings", methods=["GET", "POST"])
def api_sync_settings():
    if request.method == "POST":
        body = request.get_json(force=True) or {}
        cfg = load_config()
        cfg["sync_settings"] = {
            "auto_strava": bool(body.get("auto_strava")),
            "auto_garmin": bool(body.get("auto_garmin")),
        }
        save_config(cfg)
    cfg = load_config()
    s = cfg.get("sync_settings") or {}
    return jsonify({
        "auto_strava": bool(s.get("auto_strava")),
        "auto_garmin": bool(s.get("auto_garmin")),
    })


def _maybe_autosync_on_startup():
    cfg = load_config()
    s = cfg.get("sync_settings") or {}
    if s.get("auto_strava"):
        _kick_sync("strava")
    if s.get("auto_garmin"):
        _kick_sync("garmin")


@app.route("/setup")
def setup_page():
    return render_template("setup.html", types_json=_safe_json(load_types()))


@app.route("/api/types", methods=["GET", "POST"])
def api_types():
    if request.method == "POST":
        import uuid, re
        body  = request.get_json(force=True) or {}
        label = body.get("label", "").strip() or "New Type"
        # Build an id from the label — lowercase, alphanumeric, max 16 chars
        base = re.sub(r"[^a-z0-9]", "_", label.lower()).strip("_")[:16] or str(uuid.uuid4())[:8]
        types = load_types()
        existing_ids = {t["id"] for t in types}
        tid = base
        n = 1
        while tid in existing_ids:
            n += 1
            tid = f"{base}_{n}"
        new_type = {
            "id":    tid,
            "label": label,
            # Glyph defaults to a 3-char uppercase slice of the label (or
            # the id, if the label is too short). User can override on
            # the Setup page.
            "glyph": _normalise_glyph(body.get("glyph") or label or tid),
            "color": body.get("color", "#9ca3af"),
            "bg":    body.get("bg",    "#2a2a2a"),
        }
        types.append(new_type)
        save_types(types)
        return jsonify(new_type)
    return jsonify(load_types())


def _normalise_glyph(raw: str) -> str:
    """3-char uppercase glyph; strips non-alphanumerics and pads/truncates."""
    if not raw:
        return "?"
    cleaned = "".join(c for c in str(raw) if c.isalnum()).upper()
    return (cleaned[:3] or "?")


@app.route("/api/types/<type_id>", methods=["PATCH", "DELETE"])
def api_type(type_id):
    types = load_types()
    idx = next((i for i, t in enumerate(types) if t["id"] == type_id), None)
    if idx is None:
        abort(404)
    if request.method == "DELETE":
        # Cascade: strip this type from any activity tagged with it
        meta = load_metadata()
        changed_fns: list[str] = []
        for fn, m in list(meta.items()):
            if m.get("type") == type_id:
                del m["type"]
                changed_fns.append(fn)
                if not m:
                    del meta[fn]
        if changed_fns:
            save_metadata(meta, changed_filenames=changed_fns)
        types.pop(idx)
    else:
        body = request.get_json(force=True) or {}
        for k in ("label", "color", "bg"):
            if k in body:
                types[idx][k] = body[k]
        if "glyph" in body:
            types[idx]["glyph"] = _normalise_glyph(body["glyph"])
    save_types(types)
    return jsonify({"ok": True})


@app.route("/api/types/<type_id>/usage")
def api_type_usage(type_id):
    """Count activities currently tagged with this type."""
    meta = load_metadata()
    n = sum(1 for m in meta.values() if m.get("type") == type_id)
    return jsonify({"count": n})


# Default deny-list for OSM region search. Persistable via /api/regions/search-filters.
_DEFAULT_OSM_DENY_CLASSES = [
    "shop", "craft", "office", "advertising", "building",
    "barrier", "emergency", "power", "man_made", "telecom",
]


def _get_osm_deny_classes() -> list[str]:
    cfg = load_config()
    val = cfg.get("osm_search_deny_classes")
    if isinstance(val, list):
        return [str(s).strip() for s in val if str(s).strip()]
    return list(_DEFAULT_OSM_DENY_CLASSES)


@app.route("/api/regions/search-filters", methods=["GET", "POST"])
def api_regions_search_filters():
    if request.method == "POST":
        body = request.get_json(force=True) or {}
        cfg = load_config()
        if "deny_classes" in body:
            cleaned = [str(s).strip() for s in (body["deny_classes"] or []) if str(s).strip()]
            cfg["osm_search_deny_classes"] = cleaned
            save_config(cfg)
    return jsonify({
        "deny_classes": _get_osm_deny_classes(),
        "defaults":     list(_DEFAULT_OSM_DENY_CLASSES),
    })


@app.route("/api/regions/search")
def api_regions_search():
    """Search OSM (Nominatim) for named places that have polygon boundaries.
    Used by /setup → Regions to import a polygon for a known park / area
    rather than free-drawing it. Results are returned as
        [{display_name, type, geometry}]
    where geometry is a GeoJSON Polygon (or null when OSM returns just a
    point / linestring for that result).
    """
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": []})
    # Higher limit so polygon-filtering still leaves enough useful candidates
    # — Nominatim ranks the most "famous" name first (the town beats the ski
    # resort for "Revelstoke"); polygon-only filtering drops most of the rest.
    url = (
        "https://nominatim.openstreetmap.org/search?"
        + urllib.parse.urlencode({
            "q": q, "format": "jsonv2", "polygon_geojson": "1",
            "addressdetails": "0", "limit": "40",
            # dedupe=0 keeps OSM features that share a name root (e.g.,
            # "Revelstoke" the city + "Revelstoke Mountain Resort" the ski hill)
            "dedupe": "0",
        })
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AlForks/1.0 (personal GPX viewer)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("regions/search: OSM lookup failed: %s", e)
        return jsonify({"results": [], "error": "region search failed"}), 502
    DENY_CLASSES = set(_get_osm_deny_classes())
    results = []
    for r in payload:
        geom = r.get("geojson") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        if r.get("class") in DENY_CLASSES:
            continue
        if geom["type"] == "MultiPolygon":
            try:
                geom = {"type": "Polygon", "coordinates": geom["coordinates"][0]}
            except Exception:
                continue
        results.append({
            "display_name": r.get("display_name") or "",
            "type":         f"{r.get('class','')}/{r.get('type','')}",
            "osm_id":       r.get("osm_id"),
            "geometry":     geom,
        })
    return jsonify({"results": results})


# Lightweight region-usage cache. Keyed on (GPX dir mtime, regions geometry
# hash) so non-geometry edits to a region (renaming, default_type, etc.) don't
# trigger an expensive full rebuild of the activities cache just to answer
# "how many activities are in this region".
_region_centroids: dict | None = None
_region_centroids_gpx_mtime: float | None = None
_region_usage_counts: dict[str, int] | None = None
_region_usage_cache_key: tuple | None = None
_region_usage_lock = threading.Lock()


def _rebuild_region_centroids():
    """(filename → (lat, lon)) using the cached parsed GPX data — much cheaper
    than rebuilding the full all_activities() result."""
    global _region_centroids, _region_centroids_gpx_mtime
    out: dict = {}
    for gpx_file in GPX_DIR.glob("*.gpx"):
        data = get_activity(gpx_file.name)
        if not data: continue
        pts = data.get("points") or []
        if not pts: continue
        clat = sum(p["lat"] for p in pts) / len(pts)
        clon = sum(p["lon"] for p in pts) / len(pts)
        out[gpx_file.name] = (clat, clon)
    _region_centroids = out
    try: _region_centroids_gpx_mtime = GPX_DIR.stat().st_mtime
    except OSError: _region_centroids_gpx_mtime = -1.0


def _region_usage_dict() -> dict[str, int]:
    """Return {region_id: activity_count} using a narrow cache that survives
    non-geometry region edits."""
    global _region_usage_counts, _region_usage_cache_key
    with _region_usage_lock:
        regions = load_regions()
        try: gpx_mtime = GPX_DIR.stat().st_mtime
        except OSError: gpx_mtime = -1.0
        # Hash of just region IDs + geometries (defaults / names don't matter)
        geom_hash = _regions_geom_hash(regions)
        key = (gpx_mtime, geom_hash)
        if _region_usage_counts is not None and _region_usage_cache_key == key:
            return _region_usage_counts
        # Refresh centroids if GPX dir changed
        if _region_centroids is None or _region_centroids_gpx_mtime != gpx_mtime:
            _rebuild_region_centroids()
        # Pre-build (region_id, ring) pairs
        rings = []
        for r in regions:
            coords = (r.get("geometry") or {}).get("coordinates") or [[]]
            ring = [[c[1], c[0]] for c in (coords[0] if coords else [])]
            if ring:
                rings.append((r["id"], ring))
        counts = {r["id"]: 0 for r in regions}
        for fn, (lat, lon) in (_region_centroids or {}).items():
            for rid, ring in rings:
                if _point_in_polygon(lat, lon, ring):
                    counts[rid] += 1
        _region_usage_counts = counts
        _region_usage_cache_key = key
        return counts


def _decimate_latlon(pts: list, max_points: int) -> list[list[float]]:
    """Evenly thin a GPX point list to ~max_points [lat,lon] pairs (5 dp),
    always keeping the true endpoint. Returns [] for <2 points."""
    if len(pts) < 2:
        return []
    step = max(1, len(pts) // max_points)
    out = [[round(p["lat"], 5), round(p["lon"], 5)]
           for p in pts[::step] if "lat" in p and "lon" in p]
    last = pts[-1]
    if out and (out[-1][0] != round(last["lat"], 5) or out[-1][1] != round(last["lon"], 5)):
        out.append([round(last["lat"], 5), round(last["lon"], 5)])
    return out


def _decimated_coords(filename: str, max_points: int = 120) -> list[list[float]]:
    """Lat/lon polyline for a track, evenly thinned to ~max_points samples.
    Used by lightweight UI overlays (regionless tracks map, duplicates map)
    where the full point density would balloon the payload for no gain."""
    data = get_activity(filename)
    return _decimate_latlon((data or {}).get("points") or [], max_points)


@app.route("/api/track-coords/<path:filename>")
def api_track_coords(filename):
    """Decimated polyline for a single track. Used by the duplicates panel
    to render small per-group comparison maps without paying for full point
    data. Returns 404 if the GPX can't be parsed."""
    if _safe_gpx_path(filename) is None:
        abort(404)
    coords = _decimated_coords(filename)
    if not coords:
        abort(404)
    return jsonify({"filename": filename, "coords": coords})


_DUPLICATE_STAT_TOL = 0.05  # ±5% on distance and duration


def _values_within_tol(a: float, b: float, tol: float) -> bool:
    if a is None or b is None:
        return False
    if a <= 0 and b <= 0:
        return True
    denom = max(abs(a), abs(b))
    if denom == 0:
        return True
    return abs(a - b) / denom <= tol


def _compute_duplicate_groups(detail: bool = True) -> list[dict]:
    """Return duplicate groups across all_activities(), already filtered for
    user-dismissed signatures. `detail=True` includes every per-track field
    the /review page needs; `detail=False` keeps only enough to count and
    identify each group (used by /api/review-counts to keep the badge
    snappy).
    """
    by_date: dict[str, list[dict]] = {}
    for a in all_activities():
        d = (a.get("date") or "")[:10]
        if not d:
            continue
        s = a.get("stats") or {}
        entry = {
            "filename":     a["filename"],
            "distance_km":  s.get("distance_km"),
            "duration_sec": s.get("duration_sec"),
        }
        if detail:
            entry.update({
                "name":         a.get("name") or a["filename"],
                "date":         d,
                "start_time":   a.get("start_time"),
                "end_time":     a.get("end_time"),
                "elev_gain_m":  s.get("elev_gain_m"),
                "type":         a.get("effective_type") or "",
                "excluded":     bool(a.get("excluded")),
                "regions":      [r.get("name") if isinstance(r, dict) else r
                                 for r in (a.get("regions") or [])],
            })
        by_date.setdefault(d, []).append(entry)

    groups: list[dict] = []
    for d, acts in by_date.items():
        if len(acts) < 2:
            continue
        parent = list(range(len(acts)))
        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        def union(i, j):
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj
        for i in range(len(acts)):
            for j in range(i + 1, len(acts)):
                if _values_within_tol(acts[i]["distance_km"],  acts[j]["distance_km"],  _DUPLICATE_STAT_TOL) \
                   and _values_within_tol(acts[i]["duration_sec"], acts[j]["duration_sec"], _DUPLICATE_STAT_TOL):
                    union(i, j)
        clusters: dict[int, list[dict]] = {}
        for i, a in enumerate(acts):
            clusters.setdefault(find(i), []).append(a)
        for members in clusters.values():
            if len(members) >= 2:
                members.sort(key=lambda a: a["filename"])
                groups.append({"date": d, "tracks": members})

    # Hold the lock across the read so we don't witness a partial-write
    # mid-flight from a concurrent /api/duplicates/dismiss POST. Path.write_text
    # in `_save_dup_dismissals` is not atomic on Windows.
    with _dup_dismissals_lock:
        dismissed = _load_dup_dismissals()
    groups = [g for g in groups
              if tuple(sorted(t["filename"] for t in g["tracks"])) not in dismissed]
    groups.sort(key=lambda g: g["date"], reverse=True)
    return groups


@app.route("/api/duplicates")
def api_duplicates():
    """Surface likely-duplicate activities so the user can clean up imports
    that came in from multiple sources (e.g. the same ride synced from both
    Strava and Garmin). Heuristic per pair: same calendar date AND
    distance_km within ±5% AND duration_sec within ±5%. Groups the user
    has dismissed via "Not duplicates" are filtered out — see
    `_compute_duplicate_groups`."""
    groups = _compute_duplicate_groups(detail=True)
    return jsonify({"groups": groups, "total_pairs": sum(len(g["tracks"]) for g in groups)})


_dup_dismissals_lock = threading.Lock()


def _load_dup_dismissals() -> set:
    """Persisted set of (sorted-filename) tuples the user has marked as
    not-actually-duplicates. JSON file holds a list of lists for portability;
    we keep it in memory as a set of tuples so membership checks are O(1)."""
    try:
        if not DUP_DISMISSALS_FILE.exists():
            return set()
        raw = json.loads(DUP_DISMISSALS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return set()
    out = set()
    for entry in raw:
        if isinstance(entry, list) and all(isinstance(x, str) for x in entry):
            out.add(tuple(sorted(entry)))
    return out


def _save_dup_dismissals(s: set) -> None:
    DUP_DISMISSALS_FILE.write_text(
        json.dumps([list(t) for t in sorted(s)], indent=2),
        encoding="utf-8",
    )


@app.route("/api/duplicates/dismiss", methods=["POST"])
def api_dup_dismiss():
    """Mark a duplicate group as 'not actually duplicates' so it stops
    appearing on /review. Body: {"filenames": [list of strings]} — the
    full set of activities currently in the group. We store the sorted
    tuple so future scans can match by exact composition."""
    body = request.get_json(force=True) or {}
    filenames = body.get("filenames")
    if not isinstance(filenames, list) or not all(isinstance(f, str) for f in filenames):
        _bad("filenames must be a list of strings")
    if len(filenames) < 2:
        _bad("a duplicate group needs at least two filenames")
    sig = tuple(sorted(filenames))
    with _dup_dismissals_lock:
        s = _load_dup_dismissals()
        s.add(sig)
        _save_dup_dismissals(s)
    # Bust the review-counts cache so the badge reflects the new state on
    # the next call (the activities cache key didn't change, so otherwise
    # we'd serve a stale total).
    with _review_counts_cache_lock:
        _review_counts_cache.clear()
    return jsonify({"ok": True, "dismissed": list(sig)})


# Activities whose start time falls inside this hour window get flagged on
# the Setup → Odd Times panel. Hours are interpreted in the activity's local
# time. Window is [21, 06]: hour 7 (07:00:xx) is daytime and not flagged.
_ODD_TIME_NIGHT_HOURS = {21, 22, 23, 0, 1, 2, 3, 4, 5, 6}

# Cap on lazy `_weather_timezone_name` lookups per request so a fresh install
# (every activity missing tz_name) doesn't fan out to ~669 sequential 2 s
# Open-Meteo calls. Subsequent tab opens make incremental progress as the
# weather cache fills in.
_ODD_TIMES_LAZY_LOOKUP_BUDGET = 20


def _iter_spike_flagged_activities():
    """Yield (activity, n_spikes, max_implied_kmh) for every non-excluded,
    non-repaired, non-approved activity whose GPS-speed scan trips the
    spike-flag threshold. Single source of truth for the three callers
    (/api/speed-spikes, the /review flagged set, the review-counts badge)
    so they can't drift on which rides count as spike-flagged."""
    for act in all_activities():
        if act.get("excluded"):
            continue
        file_meta = act.get("meta") or {}
        if file_meta.get("spike_repair") or file_meta.get("issues_approved"):
            continue
        data = get_activity(act["filename"])
        if not data:
            continue
        pts = data.get("points") or []
        if len(pts) < 2 * _SPIKE_WINDOW + 1:
            continue
        mask, max_implied = _find_speed_spikes(pts)
        n = sum(mask)
        if _is_spike_flagged(n, max_implied):
            yield act, n, max_implied


_spike_flagged_cache: dict = {}
_spike_flagged_cache_lock = threading.Lock()


def _spike_flagged_activities() -> list:
    """Materialized + cached form of `_iter_spike_flagged_activities`, keyed on
    `_activities_cache_key()`. The underlying scan reads every non-triaged
    activity's full point list (the expensive part of /review), and all three
    callers — /api/speed-spikes, the /review flagged set, and the review-counts
    badge — need it. Without this cache they each re-run the full scan, so a
    single metadata/region/type edit pays the spike scan up to three times.
    Returns a list of (activity, n_spikes, max_implied_kmh)."""
    cache_key = _activities_cache_key()
    with _spike_flagged_cache_lock:
        if _spike_flagged_cache.get("k") == cache_key:
            return _spike_flagged_cache["v"]
    result = list(_iter_spike_flagged_activities())
    with _spike_flagged_cache_lock:
        _spike_flagged_cache["k"] = cache_key
        _spike_flagged_cache["v"] = result
    return result


def _odd_time_local_dt(dt, tz_name):
    """Resolve a start datetime to location-local time for night-window
    classification. Returns the localized datetime, or None when the zone
    is unknown/unresolvable (callers skip rather than false-flag). The lazy
    tz_name resolution that /api/odd-times does lives in that caller; this
    only does the dt -> local conversion the three callers share."""
    if tz_name:
        try:
            return dt.astimezone(ZoneInfo(tz_name)) if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(tz_name))
        except ZoneInfoNotFoundError:
            return None
    if dt.tzinfo is None:
        return dt
    return None


@app.route("/api/odd-times")
def api_odd_times():
    """Flag rides whose start time, in the activity-location's local zone,
    falls in the night window. Defensive against the parse-time failure mode
    where `_weather_timezone_name` returned None and timestamps were cached
    with their original (often UTC) offset — we re-resolve TZ here from the
    sidebar entry's lat/lon, which lazily fills the weather cache for next
    time. Entries we can't classify are skipped rather than false-flagged.
    """
    flagged: list[dict] = []
    lazy_budget = _ODD_TIMES_LAZY_LOOKUP_BUDGET
    for a in all_activities():
        st = a.get("start_time")
        if not st:
            continue
        try:
            dt = datetime.fromisoformat(st)
        except ValueError:
            continue

        tz_name = a.get("tz_name")
        if not tz_name and a.get("start_latlon") and lazy_budget > 0:
            lat, lon = a["start_latlon"]
            # Open-Meteo's timezone result is location-only; the date param is
            # required by the archive endpoint but doesn't affect the zone we
            # get back. Use the activity's date when available so the cache
            # entry sits alongside any weather we'd fetch later anyway.
            date_str = (a.get("date") or "")[:10] or "2024-01-01"
            lazy_budget -= 1
            # `_weather_timezone_name` swallows its own exceptions and
            # returns None — no outer try/except needed here.
            tz_name = _weather_timezone_name(lat, lon, date_str)

        local_dt = _odd_time_local_dt(dt, tz_name)
        # Naive timestamp + no tz_name resolves to the wall-clock at face
        # value (correct for TrailForks-style files where naive == local
        # clock; an accepted limitation for any naive-UTC source). A zone we
        # can't resolve yields None — skip rather than false-flag.
        if local_dt is None or local_dt.hour not in _ODD_TIME_NIGHT_HOURS:
            continue
        s = a.get("stats") or {}
        date_str = (a.get("date") or "")[:10]
        flagged.append({
            "filename":     a["filename"],
            "name":         a.get("name") or a["filename"],
            "date":         date_str,
            "start_time":   local_dt.isoformat(),
            "_sort_key":    (date_str, local_dt.hour, local_dt.minute),
            "duration_sec": s.get("duration_sec"),
            "distance_km":  s.get("distance_km"),
            "type":         a.get("effective_type") or "",
            "regions":      [r.get("name") if isinstance(r, dict) else r
                             for r in (a.get("regions") or [])],
            "tz_name":      tz_name,
            "tz_offset":    local_dt.utcoffset().total_seconds() / 3600 if local_dt.utcoffset() is not None else None,
            # Current persisted shift, so the frontend can render the
            # actual offset and increment from it correctly.
            "time_shift_hours": (a.get("meta") or {}).get("time_shift_hours") or 0,
        })
    flagged.sort(key=lambda a: a["_sort_key"], reverse=True)
    for a in flagged:
        a.pop("_sort_key", None)
    return jsonify({"activities": flagged, "window": "21:00–07:00 local"})


@app.route("/api/speed-spikes")
def api_speed_spikes():
    """Scan every activity for unrepaired GPS phantom warps. The expensive
    bit is the per-leg haversine + local-median pass which has to read each
    activity's full point list — a few seconds across a multi-year archive
    on the first call, then served from the regular activity cache.
    """
    flagged: list[dict] = []
    for act, n, max_implied in _spike_flagged_activities():
        s = act.get("stats") or {}
        flagged.append({
            "filename":      act["filename"],
            "name":          act.get("name") or act["filename"],
            "date":          (act.get("date") or "")[:10],
            "type":          act.get("effective_type") or "",
            "regions":       [r for r in (act.get("regions") or [])],
            "n_spikes":      n,
            "max_implied":   round(max_implied, 1),
            "distance_km":   s.get("distance_km"),
            "duration_sec":  s.get("duration_sec"),
            "max_speed_kmh": s.get("max_speed_kmh"),
        })
    flagged.sort(key=lambda a: (-a["max_implied"], -(a["n_spikes"] or 0)))
    return jsonify({"activities": flagged})


@app.route("/api/speed-spikes/<path:filename>/preview")
def api_speed_spike_preview(filename):
    """Side-by-side preview of an activity's raw vs spike-repaired track,
    used by the /review GPS Spikes card. Returns coords, cumulative
    distances, elevations and the indices of phantom-warp samples so the
    UI can paint both polylines, plot two elevation charts, and dot the
    flagged points on the map.

    `get_activity()` returns parsed-but-untransformed data — `spike_repair`
    is applied at request time elsewhere — so we can synthesize both views
    here regardless of the meta flag's current state.
    """
    if _safe_gpx_path(filename) is None:
        abort(404)
    data = get_activity(filename)
    if not data:
        abort(404)
    pts = data.get("points") or []
    if len(pts) < 2 * _SPIKE_WINDOW + 1:
        return jsonify({"error": "track too short for spike detection",
                        "raw_coords": [], "repaired_coords": []})

    mask, max_implied = _find_speed_spikes(pts)
    spike_indices = [i for i, m in enumerate(mask) if m]

    # Reuse the mask we just computed — _apply_spike_repair would otherwise
    # call _find_speed_spikes again on the same point list.
    repaired = _apply_spike_repair(data, phantom_mask=mask)
    rpts = repaired.get("points") or pts

    # round() keeps the JSON small — the cards don't need nano-degree precision.
    def _coords(arr): return [[round(p["lat"], 6), round(p["lon"], 6)] for p in arr]
    def _dists(arr):  return [round(p.get("dist_km") or 0, 4) for p in arr]
    def _eles(arr):
        out = []
        for p in arr:
            e = p.get("ele")
            out.append(round(e, 1) if isinstance(e, (int, float)) else None)
        return out

    # Per-point speed series for the chart pair. The raw series uses the
    # same haversine / dt that powers `_find_speed_spikes` — the stored
    # `speed` field is already median-filtered and would hide the very
    # spikes we want the user to see. The repaired series uses the
    # median-filtered post-repair `speed` (that's what feeds stats.max_speed_kmh)
    # so the chart matches the projected reported max.
    raw_speeds = [0.0] * len(pts)
    for i in range(1, len(pts)):
        t_prev = pts[i-1].get("time"); t_cur = pts[i].get("time")
        if not t_prev or not t_cur:
            continue
        try:
            dt = (datetime.fromisoformat(t_cur) - datetime.fromisoformat(t_prev)).total_seconds()
        except Exception:
            continue
        if dt <= 0:
            continue
        d_m = haversine((pts[i-1]["lat"], pts[i-1]["lon"]),
                        (pts[i]["lat"],   pts[i]["lon"]))
        raw_speeds[i] = round((d_m / dt) * 3.6, 2)
    repaired_speeds = [round(p.get("speed"), 2) if isinstance(p.get("speed"), (int, float))
                       else None for p in rpts]

    # `raw_speeds[0]` is always 0 (no incoming leg) and gaps with missing
    # timestamps stay 0 too — guard on `> 0` so a track without any
    # computable leg speed reports None instead of a misleading 0.0.
    raw_max = round(max(raw_speeds), 1) if any(s > 0 for s in raw_speeds) else None
    rep_speeds_clean = [s for s in repaired_speeds if s is not None]
    repaired_max = round(max(rep_speeds_clean), 1) if rep_speeds_clean else None

    return jsonify({
        "raw_coords":         _coords(pts),
        "raw_dists":          _dists(pts),
        "raw_speeds":         raw_speeds,
        "repaired_coords":    _coords(rpts),
        "repaired_dists":     _dists(rpts),
        "repaired_speeds":    repaired_speeds,
        "spike_indices":      spike_indices,
        "n_spikes":           len(spike_indices),
        "raw_max_kmh":        raw_max,
        "repaired_max_kmh":   repaired_max,
    })


@app.route("/api/missing-types")
def api_missing_types():
    """Activities with no resolvable type — neither an explicit `meta.type`
    nor a matched-region `default_type` (or seasonal `winter_default_type`).
    Sorted newest-first so recent imports surface for tagging.
    """
    flagged: list[dict] = []
    for a in all_activities():
        if a.get("excluded"):
            continue
        if a.get("effective_type"):
            continue
        s = a.get("stats") or {}
        flagged.append({
            "filename":     a["filename"],
            "name":         a.get("name") or a["filename"],
            "date":         (a.get("date") or "")[:10],
            "regions":      [r.get("name") if isinstance(r, dict) else r
                             for r in (a.get("regions") or [])],
            "distance_km":  s.get("distance_km"),
            "duration_sec": s.get("duration_sec"),
        })
    flagged.sort(key=lambda x: x["date"], reverse=True)
    return jsonify({"activities": flagged})


@app.route("/api/issues")
def api_issues():
    """Activities flagged by `_detect_issues` for anything *other than* a
    speed_spike — spikes have their own dedicated /review tab with rich
    map + chart preview, so we exclude them here to keep the two
    surfaces non-redundant.

    Skips excluded activities and any ride the user has already marked
    `issues_approved = true`. Each row carries its per-issue message
    list inline so the /review tab can show what's wrong without a
    second round-trip.
    """
    flagged: list[dict] = []
    for a in all_activities():
        if a.get("excluded"):
            continue
        if (a.get("meta") or {}).get("issues_approved"):
            continue
        issues = a.get("issues") or []
        non_spike = [i for i in issues if i.get("code") != "speed_spike"]
        if not non_spike:
            continue
        s = a.get("stats") or {}
        flagged.append({
            "filename":     a["filename"],
            "name":         a.get("name") or a["filename"],
            "date":         (a.get("date") or "")[:10],
            "type":         a.get("effective_type") or "",
            "distance_km":  s.get("distance_km"),
            "duration_sec": s.get("duration_sec"),
            "issues":       non_spike,
        })
    # Newest-first overall; within a date, most-severe and most-issues
    # bubble up so the worst rides are at the top of each cluster.
    _sev_rank = {"high": 0, "med": 1, "low": 2}
    def _key(r):
        worst = min((_sev_rank.get(i.get("severity"), 3) for i in r["issues"]), default=3)
        # date desc → invert by negating the lex order via a reverse-sort
        # on the date field only.
        return (worst, -len(r["issues"]))
    # Stable sort: secondary key first (severity), then primary (date desc).
    flagged.sort(key=_key)
    flagged.sort(key=lambda x: x["date"], reverse=True)
    return jsonify({"activities": flagged})


_review_counts_cache: dict = {}
_review_counts_cache_lock = threading.Lock()

# Union of filenames currently flagged on *any* /review tab — duplicates,
# missing types, GPS spikes, odd times, or data issues. Used by the
# /logs "Show only flagged" filter and computed once per activities-cache
# key so the spike scan (the expensive part) doesn't run repeatedly.
_review_flagged_cache: dict = {}
_review_flagged_cache_lock = threading.Lock()


def _review_flagged_filenames() -> set:
    """Set of activity filenames currently surfaced on any /review tab.
    Cached against `_activities_cache_key()` — the same key that drives
    `_review_counts_cache`, so any metadata edit (Approve, Repair, type
    assignment) invalidates this naturally on the next call.
    """
    cache_key = _activities_cache_key()
    with _review_flagged_cache_lock:
        if _review_flagged_cache.get("k") == cache_key:
            return _review_flagged_cache["v"]

    flagged: set = set()

    # Duplicates — every track in any non-dismissed group is "flagged".
    for g in _compute_duplicate_groups(detail=False):
        for t in g["tracks"]:
            flagged.add(t["filename"])

    # Missing types / non-spike issues / odd times — all from the cached
    # activities list, so cheap.
    #
    # Odd-times consistency note: `/api/odd-times` does a lazy
    # `_weather_timezone_name` lookup for activities missing a cached
    # `tz_name`, capped at _ODD_TIMES_LAZY_LOOKUP_BUDGET. We deliberately
    # skip that lookup here (matching `api_review_counts` for symmetry) —
    # adding a network call to every cold cache miss would punish the
    # /logs filter and the nav-badge update. The flagged set may diverge
    # slightly from the Odd Times tab on a fresh install where tz_names
    # haven't been resolved yet. Once tz_name caches fill in, the two
    # converge.
    for a in all_activities():
        if a.get("excluded"):
            continue
        fn = a["filename"]
        if not a.get("effective_type"):
            flagged.add(fn)
        if any(i.get("code") != "speed_spike" for i in (a.get("issues") or [])):
            flagged.add(fn)
        st = a.get("start_time")
        if st:
            try:
                dt = datetime.fromisoformat(st)
            except ValueError:
                dt = None
            if dt is not None:
                tz_name = a.get("tz_name")
                local_dt = _odd_time_local_dt(dt, tz_name)
                if local_dt is not None and local_dt.hour in _ODD_TIME_NIGHT_HOURS:
                    flagged.add(fn)

    # GPS spikes — expensive (full point scan per non-repaired/approved
    # activity). Skip activities that the user has already triaged.
    for act, _n, _max in _spike_flagged_activities():
        flagged.add(act["filename"])

    # Cache a frozenset so a future caller can't mutate the shared state in
    # place. (Callers only iterate / membership-check, but the safety belt
    # is free.)
    frozen = frozenset(flagged)
    with _review_flagged_cache_lock:
        _review_flagged_cache["k"] = cache_key
        _review_flagged_cache["v"] = frozen
    return frozen


@app.route("/api/review-counts")
def api_review_counts():
    """Cheap summary used by the nav badge. Each count is computed from the
    same source the per-section endpoints use, so the badge stays in sync
    with what the user actually sees on /review. Result is cached against
    the activities-cache key so the spike scan (which reads every activity's
    points) doesn't re-run on every page load — invalidates automatically
    when metadata, regions, or types change.
    """
    cache_key = _activities_cache_key()
    with _review_counts_cache_lock:
        cached = _review_counts_cache.get("v")
        cached_key = _review_counts_cache.get("k")
        if cached and cached_key == cache_key:
            return jsonify(cached)
    # Duplicates — share the canonical grouping helper so the badge
    # tracks /api/duplicates exactly (including user-dismissed groups).
    dup_groups = len(_compute_duplicate_groups(detail=False))

    # Missing types — same rule as /api/missing-types
    missing_types = sum(
        1 for a in all_activities()
        if not a.get("excluded") and not a.get("effective_type")
    )

    # Speed spikes — match /api/speed-spikes' threshold (≥3 spikes or any
    # single ≥80 km/h). Skips repaired and approved rides.
    spikes = len(_spike_flagged_activities())

    # Odd times — re-derive count from the existing endpoint's heuristic.
    # Cheaper version: just count activities whose start hour falls in the
    # night window using the cached tz_name (skip lazy lookups so the
    # badge call stays fast).
    odd = 0
    for a in all_activities():
        st = a.get("start_time")
        if not st:
            continue
        try:
            dt = datetime.fromisoformat(st)
        except ValueError:
            continue
        tz_name = a.get("tz_name")
        local_dt = _odd_time_local_dt(dt, tz_name)
        if local_dt is None:
            continue
        if local_dt.hour in _ODD_TIME_NIGHT_HOURS:
            odd += 1

    # Non-spike issues — match /api/issues. The cached `issues` field on
    # each activity already excludes approved rides (it's set to [] when
    # meta.issues_approved is true at build time), so we just check the
    # non-spike subset directly.
    issue_count = 0
    for a in all_activities():
        if a.get("excluded"):
            continue
        if any(i.get("code") != "speed_spike" for i in (a.get("issues") or [])):
            issue_count += 1

    total = dup_groups + missing_types + spikes + odd + issue_count
    payload = {
        "duplicates":    dup_groups,
        "odd_times":     odd,
        "speed_spikes":  spikes,
        "missing_types": missing_types,
        "issues":        issue_count,
        "total":         total,
    }
    with _review_counts_cache_lock:
        _review_counts_cache["k"] = cache_key
        _review_counts_cache["v"] = payload
    return jsonify(payload)


@app.route("/api/regions/<region_id>/usage")
def api_region_usage(region_id):
    if _region_by_id(region_id) is None:
        abort(404)
    return jsonify({"count": _region_usage_dict().get(region_id, 0)})


@app.route("/api/regions/untagged")
def api_regions_untagged():
    """Activities that match no region and have no pinned regions, with
    decimated polylines for overlay on the regions setup map. Lets the user
    see coverage gaps and draw new polygons to fill them.

    Refresh always forces a rebuild: this endpoint is the user's explicit
    "re-evaluate region matches" trigger, and we don't want a stale cache
    (e.g. from a same-second mtime collision on Windows) to make Refresh
    look broken."""
    _invalidate_activities_cache()
    out = []
    for act in all_activities():
        if act.get("excluded"):
            continue
        if act.get("regions"):
            continue
        if (act.get("meta") or {}).get("regions_pinned"):
            continue
        data = get_activity(act["filename"])
        coords = _decimate_latlon((data or {}).get("points") or [], 80)
        if len(coords) < 2:
            continue
        out.append({
            "filename": act["filename"],
            "name":     act.get("name") or act["filename"],
            "date":     (act.get("date") or "")[:10],
            "type":     act.get("effective_type") or "",
            "coords":   coords,
        })
    out.sort(key=lambda a: a["date"], reverse=True)
    return jsonify(out)


@app.route("/api/regions", methods=["GET", "POST"])
def api_regions():
    if request.method == "POST":
        import uuid
        body    = request.get_json(force=True) or {}
        regions = load_regions()
        region  = {
            "id":                  str(uuid.uuid4())[:8],
            "name":                body.get("name", "New Region"),
            "color":                body.get("color", "#3b82f6"),
            "geometry":             body.get("geometry", {}),
            "default_type":         body.get("default_type", ""),
            "winter_default_type":  body.get("winter_default_type", ""),
            "winter_months":        body.get("winter_months") or [11, 12, 1, 2, 3, 4],
            "assisted":             bool(body.get("assisted", False)),
            "source":               body.get("source", ""),
        }
        regions.append(region)
        save_regions(regions)
        return jsonify(region)
    return jsonify(load_regions())


@app.route("/api/regions/<region_id>", methods=["PATCH", "DELETE"])
def api_region(region_id):
    regions = load_regions()
    idx = next((i for i, r in enumerate(regions) if r["id"] == region_id), None)
    if idx is None:
        abort(404)
    if request.method == "DELETE":
        regions.pop(idx)
    else:
        body = request.get_json(force=True) or {}
        for k in ("name", "color", "geometry", "default_type",
                  "winter_default_type", "winter_months", "assisted"):
            if k in body:
                regions[idx][k] = body[k]
        if "assisted" in regions[idx]:
            regions[idx]["assisted"] = bool(regions[idx]["assisted"])
    save_regions(regions)
    return jsonify({"ok": True})


_init_backup_tracking()
_maybe_autosync_on_startup()


if __name__ == "__main__":
    # Configure root + alforks logger when running as the Flask entrypoint.
    # Default level is INFO; ALFORKS_DEBUG=1 bumps to DEBUG.
    debug = os.environ.get("ALFORKS_DEBUG") == "1"
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.info("Starting GPX viewer at http://localhost:5000")
    # Trail-match prewarm: in a daemon thread so startup isn't blocked.
    # The worker iterates MTB+Moose-Mountain rides and computes any
    # missing trail-match results so the leaderboard is populated by the
    # time the user clicks a trail name. Idempotent + cheap when the
    # cache is already warm.
    _prewarm_trail_matches_async()
    # Debug mode leaks tracebacks — opt in via ALFORKS_DEBUG=1 for local dev.
    app.run(debug=debug, threaded=True)
