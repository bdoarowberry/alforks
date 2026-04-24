# AlForks Architecture

Flask backend + server-rendered templates with per-page JS. All state
lives on disk — no database. Runs locally against a `tracks/`
directory of GPX files. This doc is a tour of how a request is served.

---

## Directory layout

```
app.py                   — Flask routes, orchestration, GPX parsing
detection.py             — lift / assisted-segment algorithms (stdlib only)
cache_utils.py           — atomic writes, backup snapshots, LRU
tracks/                  — user GPX files (gitignored)
cache/                   — parsed activity cache + weather + HR + OSM lifts
backups/                 — daily snapshots of user-edited JSON files
metadata.json            — per-file overrides (type, title, trim, smoothing, etc)
regions.json             — user-drawn geographic regions for auto-type + grouping
types.json               — activity type palette (mtb, ski, snowboard, hike, other)
config.json              — Mapbox token, max-HR override (gitignored)
templates/               — Jinja templates (index, summary, heatmap, 3d, comparison)
static/                  — JS, CSS, SVG icons
sync/                    — CLI scripts for Garmin + Strava sync (run offline)
tests/                   — stdlib unittest, no pytest dependency
scripts/                 — profiling and other dev tooling
docs/                    — this doc and metrics.md
```

The project root is computed as `Path(__file__).parent` in app.py — no
relative-path assumptions.

---

## Backend module split

The three Python modules serve different concerns. Keep them that way.

### `app.py` — Flask + orchestration
- Route definitions and HTTP-facing code.
- Config, metadata, regions, types loaders (thread-safe, in-memory cached).
- `parse_gpx` — XML read, per-point loop, initial segmentation, disk cache write.
- `get_activity` — mem cache → disk cache → parse_gpx fallback chain.
- `_effective_data` — MTB re-segmentation, decides which algorithm runs.
- `_apply_trim` + `_apply_smoothing` — user-override replay on read.
- `_merge_hr_into_data` — timezone-aware HR overlay.
- External-fetch glue: Overpass (OSM lifts), Open-Meteo (weather +
  timezone), Nominatim (geocode + region search).

### `detection.py` — algorithms
- Lift / assisted-segment algorithms and their thresholds.
- Stdlib-only (`math`, `statistics`, `datetime`) — no Flask, no
  filesystem IO. Safe to import from tests.
- Core helpers: `haversine`, `_median_filter`, `_prefix_sum`,
  `_build_segments`, `_compute_algo_stats`, `_merge_stats`,
  `_per_pt_from_points`.
- Per-algorithm functions: `_algo_lift`, `_algo_mtb`,
  `_algo_smart_combined`, `_algo_speed_osm`, `_algo_osm`,
  `_algo_elevation_rate`, `_algo_heuristic`, `_algo_time_gap`.
- Registry: `DETECTION_ALGORITHMS` (driven by `/comparison` page) and
  `ALGO_SIG` (hashed into `CACHE_VERSION` for cache invalidation).

### `cache_utils.py` — filesystem utilities
- `_atomic_write` — temp-file + `os.replace`, with OneDrive-aware
  retry on Windows (OneDrive briefly opens newly-created files for
  upload, which causes `PermissionError`).
- `_ensure_daily_backup` — snapshot user-edited files to
  `backups/<stem>.YYYY-MM-DD<suffix>`, prune after 7 days.
- `LRUCache` — thread-safe bounded `OrderedDict`.

No circular imports — `app` imports from `detection` and `cache_utils`;
neither of those imports `app`.

---

## Request lifecycle: `GET /api/activity/<filename>`

The most-travelled path. All the machinery is in play.

```
  HTTP request
       │
       ▼
  api_activity(filename)  (app.py ~950)
       │  ─── _safe_gpx_path: confirm filename resolves inside GPX_DIR
       │        (reject "../" traversal attempts and non-.gpx suffixes)
       │
       ├── get_activity(filename)
       │    │
       │    ├── _mem_cache.get(filename)  → hit? return
       │    │                               → _UNPARSEABLE sentinel? return None
       │    │
       │    ├── file lock (per-filename)
       │    │
       │    ├── _read_disk_cache(filename, mtime)
       │    │    │  key: (CACHE_VERSION, mtime)
       │    │    │  hit? return {data} from cache/gpx/<filename>.json
       │    │
       │    └── parse_gpx(path)                 (on cache miss)
       │         │
       │         ├── gpxpy.parse (XML)          60% of total time
       │         ├── per-point loop:
       │         │    haversine, dt, speed, ele_delta
       │         ├── _median_filter(k=5)         (smooth spd spikes)
       │         ├── compute raw bbox
       │         ├── _fetch_osm_lifts(bbox)      (Overpass, cached per bbox)
       │         ├── _algo_lift(per_pt, latlons, osm_lifts)
       │         ├── _build_segments(is_assisted)
       │         ├── accumulate riding_dist/gain/loss/duration
       │         └── return dict; cache to disk
       │
       ├── _effective_data(filename, data, meta.type)
       │        MTB type → re-run _algo_mtb, cache under _mtb_seg_cache
       │        Other types → pass through unchanged
       │
       ├── _apply_trim(data, meta.trim)        (if trim set)
       ├── _apply_smoothing(data, meta.smoothing)  (if window > 1)
       │
       ├── _merge_hr_into_data(data)           (if HR cache file exists)
       │        Open-Meteo timezone → local-time → UTC-ms alignment
       │        → hr_samples, hr_avg, hr_max, hr_zones
       │
       ├── _effective_regions + _effective_type_for
       │        Match centroid against region polygons,
       │        resolve winter vs default type.
       │
       ├── _detect_issues_cached(eff)
       │        Flag GPS teleports, jitter, impossible max speed, etc.
       │
       └── jsonify({...})
              ETag-tagged so the browser can skip re-download on refresh
```

For the sidebar list (`GET /api/activities`) the same pipeline runs
for every activity but the result is a trimmed "entry" dict
(filename, date, stats, meta, spark, regions, has_hr, effective_type)
rather than the full points array. Cached at `_activities_cache` and
keyed on `GPX_DIR.stat().st_mtime` so adding a file invalidates it.

---

## Prewarm

Flask's entrypoint sets `ALFORKS_PREWARM=1` and launches a daemon
thread that walks every `*.gpx` file in `tracks/` newest-first and
calls `get_activity(filename)` on each with a 6-worker `ThreadPoolExecutor`
(app.py ~718). The effect:

1. All disk-cache reads that are still valid serve instantly.
2. All disk-cache misses reparse in parallel during startup.
3. `all_activities()` is populated so the sidebar renders immediately
   when the first request arrives.

The thread is gated behind the env var so importing `app` from tests
doesn't kick off a background parse of the user's whole track library.

---

## Heatmap SSE stream

`/api/heatmap/stream` is a Server-Sent Events endpoint. The frontend
(`heatmap.html`) opens an `EventSource`, receives one activity payload
per event, adds the polyline to the map, then moves on.

Flow (`api_heatmap_stream`, app.py ~1224):

1. Iterate `all_activities()` — the already-cached sidebar entries.
2. Apply `year` and `type` filters *using the entry's cached metadata
   before loading full point data* (this is the key perf win from
   Phase 3b: we don't read the disk cache for activities that will be
   filtered out).
3. Emit `{total: N}` where N is the post-filter count.
4. For each surviving candidate:
   - `get_activity(filename)` — mem cache hit if prewarm has run.
   - Downsample polyline to every 5th point via `data["points"][::5]`.
   - Emit `{activity: {filename, date, type, title, distance_km, polyline}}`.
5. Emit `{done: True}`.

`{skip: True}` events only fire when `get_activity` returns `None` for
a candidate (unparseable file that somehow made it into the sidebar
cache) — they don't fire for filtered-out activities.

---

## External integrations

### Mapbox GL JS — 3D terrain + heatmap + comparison views
- Token in `config.json`. `/api/config` serves it to the frontend.
- Never sent anywhere except Mapbox CDN from the browser.

### OpenStreetMap Overpass — aerialway (lift) geometry
- Fetched by `_fetch_osm_lifts(bbox)` (app.py ~170ish).
- Per-bbox lock in `_fetch_osm_lifts` prevents the prewarm thread pool
  from making duplicate Overpass requests for tracks that share a
  resort bbox.
- Cached at `cache/lifts/<bbox_hash>.json`. Bbox rounded to 2 decimal
  places (~1 km) so nearby tracks share one fetch.
- User-Agent `AlForks/1.0 (personal GPX viewer)` — courtesy for the
  public Overpass instance.

### Open-Meteo — historical weather + IANA timezone resolution
- `_weather_timezone_name(lat, lon, date_str)` hits the `timezone=auto`
  endpoint. Critical for HR alignment: zoneinfo needs an IANA name,
  and Open-Meteo's `utc_offset_seconds` alone would be wrong for a
  winter activity queried in summer.
- `_fetch_weather` gets hourly temp / wind / precipitation over the
  activity's local-time window.
- Free tier, no auth, no rate limit in practice for personal use.
- Cached forever under `cache/weather/…` (historical weather doesn't
  change).

### Nominatim — reverse geocode + region search
- `_geocode_fetch(lat, lon)` reverse-geocodes the first track point
  to a human name (city / park / trail). Cached at
  `cache/geocode.json`.
- Region search (`/api/regions/search`) proxies Nominatim forward-search
  to draw polygons around known locations.
- User-Agent identifies AlForks; courtesy for the public Nominatim
  instance.

### Garmin Connect (offline sync)
- `sync/garmin_sync.py` — CLI, not imported by Flask. Runs on demand:
  `--login` stores OAuth tokens in `~/.alforks/`, `--sync` pulls daily
  HR timeseries.
- HR data cached at `cache/hr/<YYYY-MM-DD>.json`. One file per date;
  shared across every activity on that date.
- Flask only *reads* these files (via `_load_hr_samples` and
  `_merge_hr_into_data`); it never contacts Garmin itself.

### Strava (offline sync)
- `sync/strava_sync.py` — CLI. OAuth via local callback port 8765,
  tokens under `~/.alforks/`.
- Pulls activity GPX files into `tracks/`. Flask treats them
  identically to any other `*.gpx` once they're on disk.
- `--dedup` identifies duplicates between Strava-sourced and
  TrailForks-sourced GPX files and moves the losers to
  `tracks/_archive_dedup/`.

---

## Concurrency model

- Flask's default threading server (`threaded=True`).
- Per-filename locks via `_lock_for` guard the parse-and-cache window
  so two parallel requests for the same file don't duplicate work.
- `_activities_cache` uses double-checked locking keyed on
  `GPX_DIR.stat().st_mtime`. Readers see a consistent snapshot; the
  rebuild thread holds `_activities_cache_lock` while walking the
  directory.
- `_mtb_seg_lock`, `_hr_merge_cache_lock`, `_metadata_lock`,
  `_types_lock`, `_regions_lock`, `_regions_match_cache_lock` — each
  cache has its own lock.
- Per-bbox lock for OSM lift fetches avoids duplicate Overpass
  requests during prewarm.

No asyncio. No multiprocessing. Thread pools only for the prewarm
parallelism and for Overpass concurrent safety.

---

## Error-handling posture

Personal app running against trusted local files — aggressive
`try/except Exception` is common and appropriate. But:

- `metadata.json` parse failure **does not silently wipe the file**.
  A corruption flag prevents `save_metadata` from writing until the
  file is manually repaired (app.py ~253). Without this, the first
  write after a corrupt load would overwrite with `{}`.
- Every disk-cache write goes through `_atomic_write` — no partial
  files survive a crash or OneDrive lock.
- User-edited JSON files (`metadata`, `regions`, `types`) get daily
  backup snapshots in `backups/` automatically on write.
- External-fetch failures (Overpass / Nominatim / Open-Meteo) degrade
  to empty results rather than crashing the request. The activity
  still renders without OSM lift snapping or without HR overlay.
- Write endpoints validate payload shape (`_validate_trim`,
  `_validate_smoothing`, `_validate_segment_overrides`) so a bad
  client can't persist a structure that later crashes reads.

---

## Testing

- Stdlib `unittest`. No pytest dependency.
- `python -m unittest discover -s tests`.
- Tests under `tests/`:
  - `test_detection.py` — algorithm behaviour, helpers, cache-sig invariants.
  - `test_smoothing.py` — app.py regressions (smoothing, trim, HR
    merge edges, segment boundary attribution, validators, out-of-order
    timestamps, avg_speed convention, parse_gpx edge cases).
  - `test_cache_utils.py` — `_atomic_write` retry behaviour, LRU eviction.
- `fixtures.py` generates synthetic per_pt / latlons for algorithm tests.

Total ~59 tests at the time of writing. Run fast (< 1 second) because
there's no on-disk fixture data — synthetic arrays only.

---

## Profiling

`scripts/profile_parse.py` times each phase of `parse_gpx` against a
real GPX file (default: largest file in `tracks/`). Useful to decide
whether a proposed optimisation is worth implementing.

Baseline on a 16 k-point track (Apr 2026):
- gpxpy XML parse — 60 % of time (the biggest opportunity for future
  perf work would be an `xml.etree.ElementTree` replacement).
- Detection loops — 12 %.
- Stats accumulation — 10 %.
- Per-point loop (haversine / speed / ele_delta) — 9 %.
- JSON serialise — 5 % (not worth the `orjson` dep).

---

## Gotchas

- **OneDrive.** The project lives in a OneDrive-synced folder, so
  every write has to survive the OneDrive sync engine holding a file
  handle briefly. `_atomic_write` handles this; don't short-circuit
  it.
- **Windows `chmod` is lipstick.** POSIX mode bits are mostly a
  no-op on Windows (only toggles read-only). The sync scripts still
  call `chmod 0o600` on token files so a WSL / Linux user gets
  actual protection.
- **Caches outlive threshold changes — unless you bump `ALGO_SIG`.**
  If a detection threshold change affects the result, add it to
  `ALGO_SIG` or bump the version prefix. The regression test
  `TestAlgoSig` catches the easy misses.
- **GPX times are treated as naive local.** TrailForks tags them
  `+00:00` even though HH:MM is local. Don't assume UTC.
- **The frontend applies `segment_overrides`, not the backend.** If
  you change server-side segmentation logic, the user-facing values
  shown on screen may still be override-adjusted client-side.
