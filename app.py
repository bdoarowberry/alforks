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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

import gpxpy
from flask import Flask, Response, abort, jsonify, redirect, render_template, request

from cache_utils import LRUCache, _atomic_write, init_backup_tracking
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

_DEFAULT_TYPES = [
    {"id": "mtb",       "label": "Mountain Bike", "color": "#4ade80", "bg": "#1a3a2a"},
    {"id": "snowboard", "label": "Snowboard",     "color": "#60a5fa", "bg": "#1a2a4a"},
    {"id": "ski",       "label": "Ski",           "color": "#a78bfa", "bg": "#2a1a4a"},
    {"id": "hike",      "label": "Hike",          "color": "#fb923c", "bg": "#3a2a1a"},
    {"id": "other",     "label": "Other",         "color": "#9ca3af", "bg": "#2a2a2a"},
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
    if not cp.exists():
        return None
    try:
        entry = json.loads(cp.read_text(encoding="utf-8"))
        if time.time() - entry.get("fetched", 0) < _LIFT_CACHE_TTL_SEC:
            return entry["lifts"]
    except Exception:
        pass
    return None


def _fetch_osm_lifts(bbox) -> list[dict]:
    """Return aerialway segments for bbox, cached to disk for 30 days.
    Per-bbox locking prevents duplicate Overpass requests during parallel prewarm.
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
            return lifts

        except Exception:
            return []


# ─── Metadata ─────────────────────────────────────────────────────────────────

_metadata_cache: dict | None = None
_metadata_lock  = threading.Lock()
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


# ─── Activities list cache ─────────────────────────────────────────────────────
# Cached result of all_activities() — invalidated when the GPX directory changes
# (new file added/removed) or when metadata is saved.

_activities_cache:            list | None  = None
_activities_cache_dir_mtime: tuple | None  = None
_activities_cache_lock                     = threading.Lock()


def _invalidate_activities_cache():
    global _activities_cache
    with _activities_cache_lock:
        _activities_cache = None


def _activities_cache_key() -> tuple:
    """Cache key combining GPX dir + metadata/regions/types mtimes, plus a
    count of HR cache files. Previously used the HR dir's mtime, but on Windows
    that bumps every time any HR file is added/removed — so every Garmin sync
    invalidated the entire sidebar cache. The file count only changes when a
    new date appears, and the per-activity `has_hr` flag is re-stat'd anyway.
    """
    def _m(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return -1.0
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
    return (_m(GPX_DIR), _m(METADATA_FILE), _m(REGIONS_FILE), _m(TYPES_FILE),
            hr_count, hr_max_mtime, _REGION_MATCH_VERSION)


# ─── HR coverage check ────────────────────────────────────────────────────────
# Tolerance applied at both ends of the activity window. Garmin's daily-wellness
# HR is sampled every ~2 minutes; activities recorded right at the edge of a
# sample window would otherwise look uncovered even though HR is effectively
# present.
_HR_COVERAGE_TOL_MS = 5 * 60 * 1000

# date_str -> (file mtime, (first_ms, last_ms, count) | None)
_hr_range_cache: dict[str, tuple[float, tuple[int, int, int] | None]] = {}


def _hr_sample_range(date_str: str) -> tuple[int, int, int] | None:
    """First / last sample timestamp (utc ms) and count for a cached HR date,
    or None when the cache is absent / unreadable / empty. Memoized by mtime."""
    if not date_str:
        return None
    p = CACHE_DIR / "hr" / f"{date_str}.json"
    try:
        mtime = p.stat().st_mtime
    except OSError:
        _hr_range_cache.pop(date_str, None)
        return None
    cached = _hr_range_cache.get(date_str)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        samples = payload.get("samples") or []
    except (OSError, ValueError):
        _hr_range_cache[date_str] = (mtime, None)
        return None
    rng = (samples[0][0], samples[-1][0], len(samples)) if samples else None
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
    trim = file_meta.get("trim") or {}
    if trim:
        eff = _apply_trim(eff, trim)
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
    # Bake hr_avg / hr_max into the sidebar entry's stats once at build time
    # so the Summary V2 / Training Load aggregations don't have to re-merge
    # HR per request. The cache invalidates correctly already — `has_hr`
    # tracks the HR cache's mtime via the activities-cache key.
    stats = dict(eff["stats"])
    if has_hr:
        try:
            merged_stats = (_merge_hr_into_data(eff).get("stats") or {})
            if merged_stats.get("hr_avg") is not None:
                stats["hr_avg"] = int(merged_stats["hr_avg"])
            if merged_stats.get("hr_max") is not None:
                stats["hr_max"] = int(merged_stats["hr_max"])
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
        "regions":  matched_regions,
        "has_hr":   has_hr,
        "effective_type": _effective_type_for(file_meta.get("type", ""),
                                              matched_regions, regions,
                                              eff.get("date") or ""),
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
        result  = []
        start_coords = []
        for gpx_file in sorted(GPX_DIR.glob("*.gpx")):
            built = _build_activity_entry(gpx_file.name, meta, regions)
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
    """Surgical cache update: recompute just one filename's entry in the
    cached activities list. Called from metadata/segment save paths that
    know exactly which file changed, avoiding a full 40 s rebuild.

    No-op when the cache hasn't been built yet — the next full load picks up
    the on-disk change via its mtime key.
    """
    global _activities_cache, _activities_cache_dir_mtime
    with _activities_cache_lock:
        if _activities_cache is None:
            return
        meta    = load_metadata()
        regions = load_regions()
        built   = _build_activity_entry(filename, meta, regions)
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
    elev_gain = elev_loss = assisted_gain = max_speed = 0.0
    riding_dur_sec = 0.0
    points = []

    for i, pt in enumerate(raw):
        if i > 0:
            p = per_pt[i]
            total_dist += p['dist']
            if is_assisted[i]:
                if p['ele_delta'] > 0:
                    assisted_gain += p['ele_delta']
            else:
                riding_dist += p['dist']
                riding_dur_sec += p['dt']
                if p['ele_delta'] > 0:
                    elev_gain += p['ele_delta']
                elif p['ele_delta'] < 0:
                    elev_loss += abs(p['ele_delta'])
            if p['speed'] is not None and p['speed'] > max_speed:
                max_speed = p['speed']

        points.append({
            "lat":     pt.latitude,
            "lon":     pt.longitude,
            "ele":     round(pt.elevation, 1) if pt.elevation is not None else None,
            "time":    _iso_localized(pt.time),
            "dist_km": round(total_dist / 1000, 3),
            "speed":   round(per_pt[i]['speed'], 1) if per_pt[i]['speed'] is not None else None,
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
    """Parse every GPX file in parallel at startup, newest first so the
    activity list populates with recent rides quickly."""
    def _mtime_or_zero(f):
        try:
            return f.stat().st_mtime
        except OSError:
            return 0
    files = sorted(GPX_DIR.glob("*.gpx"), key=_mtime_or_zero, reverse=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda f: get_activity(f.name), files))
    all_activities()


# Skip prewarm when the module is imported by tooling (tests, scripts). The
# real Flask entrypoint sets ALFORKS_PREWARM=1 below before serving.
if os.environ.get("ALFORKS_PREWARM") == "1":
    threading.Thread(target=_prewarm, daemon=True, name="cache-prewarm").start()


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    """Home page — formerly Summary V2, now the canonical summary view.
    Old bookmarks like `/?file=foo.gpx` (which used to point to Ride Logs)
    redirect to `/rides?file=foo.gpx` so external links keep working."""
    if "file" in request.args:
        return redirect(f"/rides?{request.query_string.decode()}", code=301)
    return render_template("summary_v2.html", types_json=json.dumps(load_types()))


@app.route("/rides")
def ride_logs():
    """Ride Logs view — the activity-detail map + sidebar list. Used to live
    at `/`; moved here when Summary V2 became the home page."""
    return render_template("index.html",
        mapbox_token=load_config().get("mapbox_token", ""),
        types_json=json.dumps(load_types()))


@app.route("/summary")
def summary():
    """Archived. Linked from Setup → Archived; not in the main nav."""
    return render_template("summary.html", types_json=json.dumps(load_types()))


@app.route("/summary/v2")
def summary_v2():
    """Engineering-log style summary dashboard (per design/summary-v2). Data
    is fetched separately from /api/summary/v2 to keep the template thin."""
    return render_template("summary_v2.html", types_json=json.dumps(load_types()))


@app.route("/training-load")
def training_load():
    """Coach-view fitness page (per design/training-load). Data fetched from
    /api/training-load; template stays thin."""
    return render_template("training_load.html", types_json=json.dumps(load_types()))


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


def _summary_v2_data(days_back: int, units: str) -> dict:
    """Build the data contract documented in design/summary-v2/README.md from
    the user's real activities. Window is rolling-`days_back`-days back from
    today, inclusive of today.
    """
    today_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_iso = today_dt.strftime("%Y-%m-%d")
    earliest_dt = today_dt - timedelta(days=days_back - 1)

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
            "short":      _three_letter_short(tid, label),
            "glyph":      label[:1].upper() or "?",
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
        # Rolling-window subset
        in_window = []
        for a in type_acts:
            d = (a.get("date") or "")[:10]
            if not d:
                continue
            try:
                dt = datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                continue
            if earliest_dt <= dt <= today_dt:
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
        longest_streak = current_streak = 0
        if sorted_dates:
            run = 1
            longest_streak = 1
            for i in range(1, len(sorted_dates)):
                prev = datetime.strptime(sorted_dates[i - 1], "%Y-%m-%d")
                cur  = datetime.strptime(sorted_dates[i],     "%Y-%m-%d")
                if (cur - prev).days == 1:
                    run += 1
                    longest_streak = max(longest_streak, run)
                else:
                    run = 1
            # Current streak is consecutive days ending at today (or yesterday)
            last_dt = datetime.strptime(sorted_dates[-1], "%Y-%m-%d")
            if (today_dt - last_dt).days <= 1:
                run = 1
                for i in range(len(sorted_dates) - 1, 0, -1):
                    a_dt = datetime.strptime(sorted_dates[i], "%Y-%m-%d")
                    p_dt = datetime.strptime(sorted_dates[i - 1], "%Y-%m-%d")
                    if (a_dt - p_dt).days == 1:
                        run += 1
                    else:
                        break
                current_streak = run

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

        # Records — best-of across the user's full history (not just window).
        # Top distance, top climbing, top descent, top max-speed, top duration.
        def _rank(key, top=1):
            ranked = sorted(
                (a for a in type_acts if (a.get("stats") or {}).get(key) is not None),
                key=lambda a: a["stats"][key], reverse=True,
            )
            return ranked[:top]

        prs = []
        # Snow types lead with descent ("vertical"), others with distance.
        if tid in ("snowboard", "ski"):
            for top in _rank("elev_loss_m"):
                prs.append({"label": "Vertical",   "value_m": top["stats"]["elev_loss_m"], "kind": "elev",
                            "name": (top.get("meta") or {}).get("title") or top.get("name") or top["filename"],
                            "date": (top.get("date") or "")[:10],
                            "filename": top["filename"]})
            for top in _rank("distance_km"):
                prs.append({"label": "Longest",    "value_km": top["stats"]["distance_km"], "kind": "dist",
                            "name": (top.get("meta") or {}).get("title") or top.get("name") or top["filename"],
                            "date": (top.get("date") or "")[:10],
                            "filename": top["filename"]})
            for top in _rank("max_speed_kmh"):
                prs.append({"label": "Top speed",  "value_kmh": top["stats"]["max_speed_kmh"], "kind": "speed",
                            "name": (top.get("meta") or {}).get("title") or top.get("name") or top["filename"],
                            "date": (top.get("date") or "")[:10],
                            "filename": top["filename"]})
            for top in _rank("duration_sec"):
                prs.append({"label": "Duration",   "value_sec": top["stats"]["duration_sec"], "kind": "dur",
                            "name": (top.get("meta") or {}).get("title") or top.get("name") or top["filename"],
                            "date": (top.get("date") or "")[:10],
                            "filename": top["filename"]})
        else:
            for top in _rank("distance_km"):
                prs.append({"label": "Longest",    "value_km": top["stats"]["distance_km"], "kind": "dist",
                            "name": (top.get("meta") or {}).get("title") or top.get("name") or top["filename"],
                            "date": (top.get("date") or "")[:10],
                            "filename": top["filename"]})
            for top in _rank("elev_gain_m"):
                prs.append({"label": "Climbing",   "value_m": top["stats"]["elev_gain_m"], "kind": "elev",
                            "name": (top.get("meta") or {}).get("title") or top.get("name") or top["filename"],
                            "date": (top.get("date") or "")[:10],
                            "filename": top["filename"]})
            for top in _rank("elev_loss_m"):
                prs.append({"label": "Descent",    "value_m": top["stats"]["elev_loss_m"], "kind": "elev",
                            "name": (top.get("meta") or {}).get("title") or top.get("name") or top["filename"],
                            "date": (top.get("date") or "")[:10],
                            "filename": top["filename"]})
            for top in _rank("max_speed_kmh"):
                prs.append({"label": "Top speed",  "value_kmh": top["stats"]["max_speed_kmh"], "kind": "speed",
                            "name": (top.get("meta") or {}).get("title") or top.get("name") or top["filename"],
                            "date": (top.get("date") or "")[:10],
                            "filename": top["filename"]})
            for top in _rank("duration_sec"):
                prs.append({"label": "Duration",   "value_sec": top["stats"]["duration_sec"], "kind": "dur",
                            "name": (top.get("meta") or {}).get("title") or top.get("name") or top["filename"],
                            "date": (top.get("date") or "")[:10],
                            "filename": top["filename"]})

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
        # Aggregate
        for a in type_acts:
            d = (a.get("date") or "")[:10]
            if len(d) != 10:
                continue
            try:
                dt = datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                continue
            yr_str = str(dt.year)
            mi = dt.month - 1
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
                dt = datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                continue
            ym = (dt.year, dt.month)
            if first_year is None or ym < (first_year, first_month):
                first_year, first_month = ym
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
    all_in_window = [a for tid in ordered_ids for a in by_type[tid]
                     if (a.get("date") or "")[:10]
                     and earliest_dt <= datetime.strptime(a["date"][:10], "%Y-%m-%d") <= today_dt]
    unique_active = {a["date"][:10] for a in all_in_window if a.get("date")}
    sorted_dates_all = sorted(unique_active)
    longest_streak_all = 0
    current_streak_all = 0
    if sorted_dates_all:
        run = 1
        longest_streak_all = 1
        for i in range(1, len(sorted_dates_all)):
            prev = datetime.strptime(sorted_dates_all[i - 1], "%Y-%m-%d")
            cur  = datetime.strptime(sorted_dates_all[i],     "%Y-%m-%d")
            if (cur - prev).days == 1:
                run += 1
                longest_streak_all = max(longest_streak_all, run)
            else:
                run = 1
        last_dt = datetime.strptime(sorted_dates_all[-1], "%Y-%m-%d")
        if (today_dt - last_dt).days <= 1:
            run = 1
            for i in range(len(sorted_dates_all) - 1, 0, -1):
                a_dt = datetime.strptime(sorted_dates_all[i], "%Y-%m-%d")
                p_dt = datetime.strptime(sorted_dates_all[i - 1], "%Y-%m-%d")
                if (a_dt - p_dt).days == 1:
                    run += 1
                else:
                    break
            current_streak_all = run

    last_date_all = max(unique_active) if unique_active else None
    days_since = None
    if last_date_all:
        days_since = (today_dt - datetime.strptime(last_date_all, "%Y-%m-%d")).days

    # Active days in the last 14 days (rolling fortnight ending today).
    fortnight_start = (today_dt - timedelta(days=13)).strftime("%Y-%m-%d")
    last_14d_active_days = sum(1 for d in unique_active if d >= fortnight_start)

    totals = {
        "days":            len(unique_active),
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


@app.route("/api/summary/v2")
def api_summary_v2():
    try:
        days_back = max(1, min(3650, int(request.args.get("days", 365))))
    except (TypeError, ValueError):
        days_back = 365
    units = request.args.get("units", "metric")
    if units not in ("metric", "imperial"):
        units = "metric"
    return jsonify(_summary_v2_data(days_back, units))


def _make_etag(*parts) -> str:
    return hashlib.md5(repr(parts).encode()).hexdigest()


# Bump when changing the shape/contents of /api/activity responses so clients
# refetch even if all input files are unchanged.
_ACTIVITY_RESPONSE_VERSION = 9


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
    """Lightweight endpoint — just filename, date, type for building filter UI."""
    return jsonify([
        {
            "filename": a["filename"],
            "date":     a["date"],
            "type":     a["meta"].get("type", ""),
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


def _load_geocode_cache() -> dict:
    if not _GEOCODE_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(_GEOCODE_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_geocode_cache(cache: dict) -> None:
    try:
        _atomic_write(_GEOCODE_CACHE_FILE, json.dumps(cache))
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
    def _m(path):
        try: return path.stat().st_mtime
        except OSError: return -1.0
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
        "activity", _ACTIVITY_RESPONSE_VERSION, filename, _m(p),
        _m(METADATA_FILE), _m(REGIONS_FILE), _m(_GEOCODE_CACHE_FILE),
        _m(CONFIG_FILE), _m(TYPES_FILE),
        _m(hr_file) if hr_file else 0,
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
    data      = _effective_data(filename, data, eff_type)
    # Apply user trim (start_km / end_km in original distances) before HR merge
    # so HR alignment respects the trimmed time window. ?notrim=1 in the URL
    # bypasses the trim — used by the trim-edit UI to see the full track.
    trim = file_meta.get("trim") or {}
    if trim and not request.args.get("notrim"):
        data = _apply_trim(data, trim)
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
    resp = jsonify({**data, "meta": file_meta, "regions": regions, "place": place,
                    "effective_type": eff_type, "issues": issues,
                    "excluded": bool(file_meta.get("excluded_from_stats"))})
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
               "smoothing", "excluded_from_stats", "regions_pinned"}
    update  = {k: v for k, v in body.items() if k in allowed}
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
        types_json=json.dumps(load_types()))


@app.route("/api/activity/<filename>/segments", methods=["PATCH", "DELETE"])
def api_save_segments(filename):
    if _safe_gpx_path(filename) is None or not (GPX_DIR / filename).exists():
        abort(404)
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
    # the location's IANA timezone (via Open-Meteo). zoneinfo computes the
    # historically-correct DST-aware offset for the activity's actual datetime,
    # which matters for winter activities (Open-Meteo's `utc_offset_seconds`
    # always reports the *current* offset, not the date's).
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
    """Return ~n evenly-spaced (lat, lon) pairs from the GPX point list."""
    if not points:
        return []
    if len(points) <= n:
        return [[p["lat"], p["lon"]] for p in points]
    step = len(points) / n
    out = [[points[int(i * step)]["lat"], points[int(i * step)]["lon"]] for i in range(n)]
    out.append([points[-1]["lat"], points[-1]["lon"]])
    return out


def _difficulty_score(distance_km: float | None, elev_gain_m: float | None) -> int | None:
    """Cheap terrain-difficulty heuristic. Combines distance and elevation per km
    so a flat ride scores by length and a hilly ride scores higher per km.

    Rough calibration: a 10 km flat ride ≈ 3; 20 km flat ≈ 4; 10 km with 1000 m
    gain ≈ 6; 20 km with 1500 m gain ≈ 8. The scale is arbitrary — good for
    relative comparison between rides, not for reproducing any external
    standard. Always rounds up to at least 1.
    """
    if not distance_km or distance_km <= 0:
        return None
    gain = elev_gain_m or 0
    gain_per_km = gain / distance_km
    score = (distance_km * (1 + gain_per_km / 50)) ** 0.5
    return max(1, round(score))


@app.route("/comparison")
def comparison_page():
    return render_template("comparison.html", types_json=json.dumps(load_types()),
                           mapbox_token=load_config().get("mapbox_token", ""))


@app.route("/compare")
def compare_overlay_page():
    """Pair two activities: overlay both polylines on a single map and show
    stats side-by-side. Filenames come in as ?a=<file>&b=<file>.
    """
    return render_template("compare.html", types_json=json.dumps(load_types()))


@app.route("/api/comparison")
def api_comparison():
    type_arg = (request.args.get("type") or "").strip()
    type_filter = {t for t in type_arg.split(",") if t and t != "all"}
    start_str = (request.args.get("start") or "").strip()
    end_str   = (request.args.get("end")   or "").strip()
    issues_only = request.args.get("issues_only") in ("1", "true")
    max_hr    = _effective_max_hr()
    region_lookup = {r["id"]: r for r in load_regions()}

    items = []
    for act in all_activities():
        date = (act.get("date") or "")[:10]
        if not date: continue
        if start_str and date < start_str: continue
        if end_str   and date > end_str:   continue
        meta_type = (act.get("meta") or {}).get("type", "")
        if type_filter and meta_type not in type_filter: continue
        if issues_only and not (act.get("issues") or []): continue

        data = get_activity(act["filename"])
        if not data: continue
        eff    = _effective_data(act["filename"], data, meta_type)
        merged = _merge_hr_into_data(eff)
        s = merged.get("stats") or {}
        hr_avg = s.get("hr_avg")
        intensity = round((hr_avg / max_hr) * 100) if (hr_avg and max_hr) else None

        items.append({
            "filename":    act["filename"],
            "date":        act.get("date"),
            "type":        meta_type,
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
            "difficulty":   _difficulty_score(s.get("distance_km"), s.get("elev_gain_m")),
            "intensity":    intensity,
            "polyline":     _downsample_polyline(merged.get("points") or [], 50),
            "issues":       act.get("issues") or [],
            "excluded":     bool(act.get("excluded")),
        })
    items.sort(key=lambda x: x["date"] or "", reverse=True)
    return jsonify({"items": items, "max_hr": max_hr})


def _compute_fitness_weeks(n_weeks: int, type_filter: set | None = None) -> list[dict]:
    """Return `n_weeks` of trailing weekly fitness rollups (Monday-anchored).
    Each week dict carries: start, hours, gain_m, zones_sec[5], rides,
    z2_hr_28d. Used by `/api/fitness/weekly` and the Training Load page."""
    type_filter = type_filter or set()
    cutoff = datetime.now().date() - timedelta(weeks=n_weeks)

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
        if type_filter and (act.get("meta") or {}).get("type", "") not in type_filter:
            continue
        if act.get("excluded"):
            continue
        bucket = per_day.setdefault(date, {"sec": 0, "gain": 0, "zones": [0]*5, "n": 0})
        s = act.get("stats") or {}
        bucket["sec"]  += int(s.get("duration_sec") or 0)
        bucket["gain"] += int(s.get("elev_gain_m")  or 0)
        bucket["n"]    += 1

        if act.get("has_hr"):
            data = get_activity(act["filename"])
            if data:
                eff    = _effective_data(act["filename"], data, (act.get("meta") or {}).get("type", ""))
                merged = _merge_hr_into_data(eff)
                ms     = merged.get("stats") or {}
                z      = ms.get("hr_zones") or [0]*5
                for i in range(5):
                    bucket["zones"][i] += int(z[i])
                if ms.get("hr_avg") is not None and z[1] > 0:
                    rolling_z2_input.append((date, int(ms["hr_avg"]), int(z[1])))

    weeks: dict[str, dict] = {}
    for date_str, b in per_day.items():
        d = datetime.fromisoformat(date_str).date()
        wk_start = (d - timedelta(days=d.weekday())).isoformat()
        w = weeks.setdefault(wk_start, {"hours": 0.0, "gain_m": 0, "zones_sec": [0]*5, "rides": 0})
        w["hours"]   += b["sec"] / 3600
        w["gain_m"]  += b["gain"]
        w["rides"]   += b["n"]
        for i in range(5):
            w["zones_sec"][i] += b["zones"][i]

    today_monday = datetime.now().date()
    today_monday = today_monday - timedelta(days=today_monday.weekday())
    output = []
    for w in range(n_weeks - 1, -1, -1):
        wk = (today_monday - timedelta(weeks=w)).isoformat()
        bucket = weeks.get(wk, {"hours": 0, "gain_m": 0, "zones_sec": [0]*5, "rides": 0})
        output.append({
            "start":     wk,
            "hours":     round(bucket["hours"], 1),
            "gain_m":    int(bucket["gain_m"]),
            "zones_sec": [int(s) for s in bucket["zones_sec"]],
            "rides":     int(bucket["rides"]),
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


def _compute_training_load(n_weeks: int) -> dict:
    """Build the Training Load page payload: 12 weeks of fitness rollups, the
    in-window activities for window-scoped PRs, and per-type totals rolled up
    over the same window. Returns a dict matching the design contract."""
    weeks = _compute_fitness_weeks(n_weeks)
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
            "glyph":       (td.get("label", tid)[:1] or "?").upper(),
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


@app.route("/api/training-load")
def api_training_load():
    """JSON payload for the Training Load page. `weeks` query param accepts
    4, 8, 12, or 26 — anything else falls back to 12 (the design default)."""
    try:
        requested = int(request.args.get("weeks", 12))
    except ValueError:
        requested = 12
    n_weeks = requested if requested in _ALLOWED_TRAINING_LOAD_WEEKS else 12
    return jsonify(_compute_training_load(n_weeks))


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


def _weather_timezone_name(lat: float, lon: float, date_str: str) -> str | None:
    """Return the IANA timezone name (e.g. 'America/Edmonton') for a location.

    Fetched via Open-Meteo (timezone=auto). Cached per location. Returning the
    name (rather than a single offset) lets us compute the historically-correct
    DST-aware offset for any datetime via zoneinfo, which matters for activities
    spanning the DST transition or recorded in winter (where Open-Meteo's
    `utc_offset_seconds` always reflects the *current* offset, not the date's).
    """
    cp = _weather_cache_path(lat, lon, date_str)
    if cp.exists():
        try:
            entry = json.loads(cp.read_text(encoding="utf-8"))
            if entry.get("timezone_name"):
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
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tz_name = data.get("timezone")
        if not tz_name:
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
        entry.setdefault("fetched", int(time.time()))
        _atomic_write(cp, json.dumps(entry, ensure_ascii=False))
        return tz_name
    except Exception:
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


_HR_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@app.route("/debug/hr/<date_str>")
def debug_hr_day(date_str):
    """Render the raw cached Garmin daily HR for a date — no activity matching,
    just the full 24-hour sample stream so you can sanity-check the signal.
    """
    if not _HR_DATE_RE.match(date_str):
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
        with urllib.request.urlopen(req, timeout=10) as resp:
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


def _point_in_polygon(lat: float, lon: float, ring: list) -> bool:
    """Ray-casting point-in-polygon. ring is [[lat, lon], ...] pairs."""
    n = len(ring)
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


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
    for i in range(1, n):
        dd = pts[i]["dist_km"] - pts[i-1]["dist_km"]
        e_prev, e_cur = pts[i-1].get("ele"), pts[i].get("ele")
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
    key = (filename, mtime, stats.get("distance_km"), stats.get("duration_sec"))
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

    # 5. GPS jitter — recorded path much longer than a smoothed path. Compute
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
            before = len(list((_ROOT / "tracks").glob("strava_*.gpx")))
        else:
            before = len(list((CACHE_DIR / "hr").glob("*.json"))) if (CACHE_DIR / "hr").exists() else 0

        proc = subprocess.run(
            [sys.executable, str(_ROOT / "sync" / script), "--sync"],
            capture_output=True, text=True, timeout=600,
        )

        if source == "strava":
            after = len(list((_ROOT / "tracks").glob("strava_*.gpx")))
            unit = "files"
        else:
            after = len(list((CACHE_DIR / "hr").glob("*.json"))) if (CACHE_DIR / "hr").exists() else 0
            unit = "dates"
        added = max(0, after - before)

        ok = proc.returncode == 0
        msg = f"{added} new {unit}" if ok else (proc.stderr or proc.stdout).strip().splitlines()[-1] if (proc.stderr or proc.stdout).strip() else "sync failed"
        with _sync_state_lock:
            _sync_state[source].update(running=False, finished_at=int(time.time()),
                                       ok=ok, message=msg, added=added)
        if source == "strava" and added > 0:
            _invalidate_activities_cache()
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
    return render_template("setup.html", types_json=json.dumps(load_types()))


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
            "color": body.get("color", "#9ca3af"),
            "bg":    body.get("bg",    "#2a2a2a"),
        }
        types.append(new_type)
        save_types(types)
        return jsonify(new_type)
    return jsonify(load_types())


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
        geom_hash = hashlib.md5(repr([
            (r.get("id"), r.get("geometry") or {}) for r in regions
        ]).encode()).hexdigest()
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


def _decimated_coords(filename: str, max_points: int = 120) -> list[list[float]]:
    """Lat/lon polyline for a track, evenly thinned to ~max_points samples.
    Used by lightweight UI overlays (regionless tracks map, duplicates map)
    where the full point density would balloon the payload for no gain."""
    data = get_activity(filename)
    pts = (data or {}).get("points") or []
    if len(pts) < 2:
        return []
    step = max(1, len(pts) // max_points)
    out = [[round(p["lat"], 5), round(p["lon"], 5)]
           for p in pts[::step] if "lat" in p and "lon" in p]
    last = pts[-1]
    if out and (out[-1][0] != round(last["lat"], 5) or out[-1][1] != round(last["lon"], 5)):
        out.append([round(last["lat"], 5), round(last["lon"], 5)])
    return out


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


@app.route("/api/duplicates")
def api_duplicates():
    """Surface likely-duplicate activities so the user can clean up imports
    that came in from multiple sources (e.g. the same ride synced from both
    Strava and Garmin).

    Heuristic per pair: same calendar date AND distance_km within
    ±5% AND duration_sec within ±5%. Within a date, we form transitive
    groups (if A~B and B~C then A,B,C are one group)."""
    by_date: dict[str, list[dict]] = {}
    for a in all_activities():
        d = (a.get("date") or "")[:10]
        if not d:
            continue
        s = a.get("stats") or {}
        by_date.setdefault(d, []).append({
            "filename":     a["filename"],
            "name":         a.get("name") or a["filename"],
            "date":         d,
            "start_time":   a.get("start_time"),
            "end_time":     a.get("end_time"),
            "distance_km":  s.get("distance_km"),
            "duration_sec": s.get("duration_sec"),
            "elev_gain_m":  s.get("elev_gain_m"),
            "type":         a.get("effective_type") or "",
            "excluded":     bool(a.get("excluded")),
            "regions":      [r.get("name") if isinstance(r, dict) else r
                             for r in (a.get("regions") or [])],
        })

    groups: list[dict] = []
    for d, acts in by_date.items():
        if len(acts) < 2:
            continue
        # Union-find by similarity
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

    groups.sort(key=lambda g: g["date"], reverse=True)
    return jsonify({"groups": groups, "total_pairs": sum(len(g["tracks"]) for g in groups)})


# Activities whose start time falls inside this hour window get flagged on
# the Setup → Odd Times panel. Hours are interpreted in the activity's local
# time. Window is [21, 06]: hour 7 (07:00:xx) is daytime and not flagged.
_ODD_TIME_NIGHT_HOURS = {21, 22, 23, 0, 1, 2, 3, 4, 5, 6}

# Cap on lazy `_weather_timezone_name` lookups per request so a fresh install
# (every activity missing tz_name) doesn't fan out to ~669 sequential 8 s
# Open-Meteo calls. Subsequent tab opens make incremental progress as the
# weather cache fills in.
_ODD_TIMES_LAZY_LOOKUP_BUDGET = 20


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
            try:
                tz_name = _weather_timezone_name(lat, lon, date_str)
            except Exception:
                tz_name = None

        if tz_name:
            try:
                local_dt = dt.astimezone(ZoneInfo(tz_name)) if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(tz_name))
            except ZoneInfoNotFoundError:
                continue
        elif dt.tzinfo is None:
            # Naive timestamp + no tz_name: trust the wall-clock at face value.
            # Correct for TrailForks-style files (naive == local clock) but
            # would re-introduce the original false-flag bug for any naive-UTC
            # source. Accepted limitation — gpxpy emits naive only when the
            # source GPX had no timezone marker, and TrailForks is the only
            # known producer of that shape we ingest.
            local_dt = dt
        else:
            continue

        if local_dt.hour not in _ODD_TIME_NIGHT_HOURS:
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
        })
    flagged.sort(key=lambda a: a["_sort_key"], reverse=True)
    for a in flagged:
        a.pop("_sort_key", None)
    return jsonify({"activities": flagged, "window": "21:00–07:00 local"})


@app.route("/api/regions/<region_id>/usage")
def api_region_usage(region_id):
    if not any(r["id"] == region_id for r in load_regions()):
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
        pts  = (data or {}).get("points") or []
        if len(pts) < 2:
            continue
        step = max(1, len(pts) // 80)
        coords = [[round(p["lat"], 5), round(p["lon"], 5)]
                  for p in pts[::step] if "lat" in p and "lon" in p]
        if pts[-1] is not pts[::step][-1]:
            coords.append([round(pts[-1]["lat"], 5), round(pts[-1]["lon"], 5)])
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
                  "winter_default_type", "winter_months"):
            if k in body:
                regions[idx][k] = body[k]
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
    # Enable background prewarm only for the Flask entrypoint — not when
    # app.py is imported by tests or tooling.
    os.environ.setdefault("ALFORKS_PREWARM", "1")
    threading.Thread(target=_prewarm, daemon=True, name="cache-prewarm").start()
    logger.info("Starting GPX viewer at http://localhost:5000")
    # Debug mode leaks tracebacks — opt in via ALFORKS_DEBUG=1 for local dev.
    app.run(debug=debug, threaded=True)
