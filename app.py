"""Flask backend for GPX activity viewer."""

import hashlib
import json
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
from flask import Flask, Response, abort, jsonify, render_template, request

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
        print(f"Cache layout migrated: {moved} file(s) moved to subdirectories")


_migrate_cache_layout()

app = Flask(__name__)


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
                print(f"[metadata] failed to parse {METADATA_FILE.name}: {e} — "
                      f"reads will use empty dict; writes will be refused until repaired",
                      file=sys.stderr)
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
    try:
        hr_count = sum(1 for _ in hr_dir.glob("*.json"))
    except OSError:
        hr_count = 0
    return (_m(GPX_DIR), _m(METADATA_FILE), _m(REGIONS_FILE), _m(TYPES_FILE), hr_count)


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
    # has_hr = HR cache file exists and is large enough to plausibly contain
    # samples (empty payloads from Garmin's retention cutoff serialize to
    # ~80 bytes; real samples push it well past 200).
    date_str = (eff.get("date") or "")[:10]
    has_hr = False
    if date_str:
        try:
            has_hr = (CACHE_DIR / "hr" / f"{date_str}.json").stat().st_size > 200
        except OSError:
            has_hr = False
    matched_regions = _effective_regions(eff, file_meta, regions)
    entry = {
        "filename": eff["filename"],
        "name":     eff["name"],
        "date":     eff["date"],
        "stats":    eff["stats"],
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

    total_dist = riding_dist = 0.0
    elev_gain = elev_loss = assisted_gain = max_speed = 0.0
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
            "time":    pt.time.isoformat() if pt.time else None,
            "dist_km": round(total_dist / 1000, 3),
            "speed":   round(per_pt[i]['speed'], 1) if per_pt[i]['speed'] is not None else None,
        })

    start, end = raw[0].time, raw[-1].time
    dur       = (end - start).total_seconds() if start and end else None
    avg_speed = (riding_dist / 1000) / (dur / 3600) if dur else None

    lats = [p["lat"] for p in points]
    lons = [p["lon"] for p in points]

    return {
        "filename": path.name,
        "name":     (gpx.tracks[0].name or "").strip() or path.stem,
        "date":     start.isoformat() if start else None,
        "bbox":     [min(lats), min(lons), max(lats), max(lons)],
        "points":   points,
        "segments": segments,
        "stats": {
            "distance_km":     round(riding_dist / 1000, 2),
            "duration_sec":    round(dur) if dur else None,
            "elev_gain_m":     round(elev_gain),
            "elev_loss_m":     round(elev_loss),
            "assisted_gain_m": round(assisted_gain),
            "avg_speed_kmh":   round(avg_speed, 1) if avg_speed else None,
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
    if cached is not None:
        return cached

    with _lock_for(filename):
        cached = _mem_cache.get(filename)
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
        return data


def _prewarm():
    """Parse every GPX file in parallel at startup, newest first so the
    activity list populates with recent rides quickly."""
    files = sorted(GPX_DIR.glob("*.gpx"), key=lambda f: f.stat().st_mtime, reverse=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda f: get_activity(f.name), files))
    all_activities()


# Skip prewarm when the module is imported by tooling (tests, scripts). The
# real Flask entrypoint sets ALFORKS_PREWARM=1 below before serving.
if os.environ.get("ALFORKS_PREWARM") == "1":
    threading.Thread(target=_prewarm, daemon=True, name="cache-prewarm").start()


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html",
        mapbox_token=load_config().get("mapbox_token", ""),
        types_json=json.dumps(load_types()))


@app.route("/summary")
def summary():
    return render_template("summary.html", types_json=json.dumps(load_types()))


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
    # Normalize regions_pinned: accept a list of strings, dedupe, drop empties
    if "regions_pinned" in update:
        pins = update["regions_pinned"] or []
        if not isinstance(pins, list):
            abort(400)
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
            meta.setdefault(filename, {})["segment_overrides"] = overrides
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
        meta  = load_metadata()
        files = sorted(GPX_DIR.glob("*.gpx"))
        total = len(files)
        yield f"data: {json.dumps({'total': total})}\n\n"

        for gpx_file in files:
            data = get_activity(gpx_file.name)
            if not data:
                yield f"data: {json.dumps({'skip': True})}\n\n"
                continue

            file_meta = meta.get(gpx_file.name, {})
            if year     and (not data["date"] or not data["date"].startswith(year)):
                yield f"data: {json.dumps({'skip': True})}\n\n"
                continue
            if act_type and file_meta.get("type", "") != act_type:
                yield f"data: {json.dumps({'skip': True})}\n\n"
                continue

            polyline = [
                [p["lat"], p["lon"]]
                for i, p in enumerate(data["points"])
                if i % sample_n == 0
            ]
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
            dt = datetime.fromisoformat(t).replace(tzinfo=None).replace(tzinfo=tz)
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
    # into the matching zone.
    max_hr = _effective_max_hr()
    zones = [0, 0, 0, 0, 0]
    if max_hr:
        ordered = [(ms, bpm) for ms, bpm in in_window if bpm is not None]
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


@app.route("/api/fitness/weekly")
def api_fitness_weekly():
    """Aggregate fitness training metrics by ISO week.
    Query params:
      weeks: number of trailing weeks to return (default 12)
    Returns per-week: hours, gain_m, zones_sec[5], rides_count, plus a
    rolling 28-day average HR for endurance-effort (mostly-Z2) rides.
    """
    n_weeks = max(1, min(52, int(request.args.get("weeks", 12))))
    cutoff = datetime.now().date() - timedelta(weeks=n_weeks)
    # Optional type filter — comma-separated ids, or 'all' / empty for no filter
    type_arg = (request.args.get("type") or "").strip()
    type_filter = {t for t in type_arg.split(",") if t and t != "all"}

    # Aggregate per-day, then bucket by Monday-anchored week
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

        # Pull zones from the per-activity merge — only when HR exists
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

    # Bucket per-day into weeks (Monday start)
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

    # Fill missing weeks for a continuous timeline
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

    # 28-day rolling Z2-weighted average HR per week-end
    rolling = []
    for week in output:
        wk_end = datetime.fromisoformat(week["start"]).date() + timedelta(days=6)
        wk_28_start = wk_end - timedelta(days=28)
        num = den = 0
        for date_str, avg_hr, z2_sec in rolling_z2_input:
            d = datetime.fromisoformat(date_str).date()
            if wk_28_start <= d <= wk_end:
                num += avg_hr * z2_sec
                den += z2_sec
        rolling.append(round(num / den) if den > 0 else None)
    for i, v in enumerate(rolling):
        output[i]["z2_hr_28d"] = v

    return jsonify({"weeks": output, "max_hr": _effective_max_hr()})


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
        print(f"[debug_hr_day] failed to parse {fp.name}: {e}", file=sys.stderr)
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
    global _regions_cache
    _atomic_write(REGIONS_FILE, json.dumps(regions, indent=2, ensure_ascii=False))
    with _regions_lock:
        _regions_cache = regions


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
    assisted = set()
    for s in segments or []:
        if s.get("type") == "assisted":
            for k in range(s["start"], s["end"] + 1):
                assisted.add(k)
    riding_dist = riding_gain = riding_loss = assisted_gain = 0.0
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
    speeds    = [p["speed"] for p in pts if p.get("speed") is not None]
    max_speed = max(speeds) if speeds else base_stats.get("max_speed_kmh")
    avg_speed = (riding_dist / (duration / 3600)) if duration > 0 else None
    return {
        **base_stats,
        "distance_km":     round(riding_dist, 2),
        "duration_sec":    duration,
        "elev_gain_m":     round(riding_gain),
        "elev_loss_m":     round(riding_loss),
        "assisted_gain_m": round(assisted_gain),
        "avg_speed_kmh":   round(avg_speed, 1) if avg_speed else None,
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


def regions_for_track(data: dict, regions: list[dict]) -> list[str]:
    """Return ids of regions whose polygon contains the track centroid.
    Memoized per (filename, gpx_mtime, regions_geom_hash)."""
    pts = data.get("points", [])
    if not pts:
        return []
    # Fast path: per-filename cache keyed on geometry hash
    filename = data.get("filename")
    cache_key = None
    if filename:
        try:
            mtime = (GPX_DIR / filename).stat().st_mtime
            cache_key = (filename, mtime, _regions_geom_hash(regions))
            with _regions_match_cache_lock:
                cached = _regions_match_cache.get(cache_key)
                if cached is not None:
                    return cached
        except OSError:
            cache_key = None
    clat = sum(p["lat"] for p in pts) / len(pts)
    clon = sum(p["lon"] for p in pts) / len(pts)
    matched = []
    for r in regions:
        # GeoJSON polygon ring coordinates are [lon, lat]
        coords = r.get("geometry", {}).get("coordinates", [[]])[0]
        ring   = [[c[1], c[0]] for c in coords]  # convert to [lat, lon]
        if ring and _point_in_polygon(clat, clon, ring):
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
        print(f"[regions/search] OSM lookup failed: {e}", file=sys.stderr)
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


@app.route("/api/regions/<region_id>/usage")
def api_region_usage(region_id):
    if not any(r["id"] == region_id for r in load_regions()):
        abort(404)
    return jsonify({"count": _region_usage_dict().get(region_id, 0)})


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
    # Enable background prewarm only for the Flask entrypoint — not when
    # app.py is imported by tests or tooling.
    os.environ.setdefault("ALFORKS_PREWARM", "1")
    threading.Thread(target=_prewarm, daemon=True, name="cache-prewarm").start()
    print("Starting GPX viewer at http://localhost:5000")
    # Debug mode leaks tracebacks — opt in via ALFORKS_DEBUG=1 for local dev.
    debug = os.environ.get("ALFORKS_DEBUG") == "1"
    app.run(debug=debug, threaded=True)
