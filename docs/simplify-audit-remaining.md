# Simplify Audit — Remaining Work

Status as of **2026-06-03**. This tracks the leftovers from a whole-repo
simplification audit (reuse / duplication / dead-code / altitude). It is a
to-do list, not a spec — re-verify before acting.

> ⚠️ **Line numbers drifted.** Many `app.py` references below come from the
> original audit report, which ran *before* this session's edits (which added
> `_meta_fp`, `_stat_mtime`, the `geo` import, `_iter_spike_flagged_activities`,
> `_odd_time_local_dt`, `_active_day_streaks` and removed several dup blocks).
> Prefer the **symbol names**; grep to re-locate before editing.

## Already done (committed + pushed to origin/main)
- `fa1732f` route-suggestions tabbed My Routes / Suggested Routes UI
- `4c739ef` drop abandoned union-find path from `route_suggestions.py`
- `2d0a968` dead code + small dedups (15 files; ~570 lines of dead JS/CSS out of
  `setup.html`; deleted superseded `scripts/trail_match_probe.py`)
- `150527f` 4 "stay-in-sync" extractions in `app.py`: `_meta_fp` (×5),
  `_iter_spike_flagged_activities` (×3), `_odd_time_local_dt` (×3),
  `_active_day_streaks` (×2)
- `584affa` `geo.point_in_polygon` (shared by app + route_builder) + `_stat_mtime`
  (collapsed 5 `_m` closures); **haversine deliberately NOT unified** — see below
- `5bfa630` `sync/_common.py` holding the byte-identical `_secure_chmod`
- `3666b50` route 4 local HTML escapers through `utils.js` `escapeHtml`

## Key decisions / landmines (don't relitigate without reading these)
- **Haversine stays duplicated on purpose.** `detection.haversine` and
  `trail_match._haversine_m` are mathematically equal but round differently at the
  ULP level (`radians(b)-radians(a)` vs `radians(b-a)`) — measured 194k/200k random
  pairs differ. Both feed version-cached subsystems (`ALGO_SIG`, `TRAIL_MATCH_VERSION`),
  so unifying would perturb GPS outputs / invalidate caches for ~zero gain.
- **JSON-writers → `_atomic_write` is now low value.** The original robustness pitch
  was the OneDrive `PermissionError` retry — **the user is no longer on OneDrive**, so
  this drops to tidiness only. (`geo`/`_stat_mtime` already done; this one was downgraded.)
- **Frontend map work needs a browser.** No node/browser in the agent env, so stateful
  Leaflet/Mapbox refactors can't be JS-runtime-verified — an HTTP-200 render check won't
  catch a blank map. Do those with the app open and eyeball each page.
- **The two sliding-median impls genuinely differ** (`detection._median_filter` even-window
  averaging + k=5 fast-path vs `trail_match._median_smooth` upper-middle, no averaging).
  Reconcile against `tests/test_detection.py` or leave alone.

## Remaining — Python (mostly verifiable with the offline test suite)

### Bigger refactors
- **Unify the 3 OSM-cache + Overpass-fetch stacks.** Lift stack in `app.py`
  (`_fetch_osm_lifts` area) + trail stack + road stack in `trail_match.py`. The two
  `trail_match` Overpass parsers are identical except one tag field (`mtb_scale` vs
  `oneway`). *Stage it:* collapse the two `trail_match` trail/road triples first
  (they share `_osm_lock_for`, the round+md5 cache-path scheme, cache/stale readers,
  and the urlopen+breaker+atomic-write body). Highest-dup item in the repo; moderate risk.
- **`build_leaderboards` / `build_region_trail_index` shared row extraction** in
  `trail_match.py`. Both walk `scan_cached_results` → timeline → skip incomplete/unnamed →
  compute direction/date/idx/title, then bucket differently. Extract
  `_iter_completed_attempts(...)` yielding a normalized attempt dict (must yield the
  superset of fields — one builder filters on regions). Moderate risk.
- **`cached_match` wrapper** for the 3 constant cache-dir kwargs (`cache_dir_osm/results/roads`).
  ~4 call sites in `app.py` (pairs with the already-extracted `_meta_fp`). Note: sites
  vary in how mtime is sourced (`p.stat().st_mtime` vs the old `_m(p)` → now `_stat_mtime`),
  so the wrapper must take mtime as a param. Low effort, moderate risk.
- **JSON writers → `cache_utils._atomic_write`** (~7 sites: `trail_match.py`,
  `route_builder.py`, `app.py`). **Downgraded** (OneDrive rationale gone). If done, give the
  sync CLIs a small `atomic_write_text` in `sync/_common.py` rather than importing cache_utils.

### In-file / cross-file dedup (lower risk)
- **`detection.py` local dups:** snap-and-trim segment loop ×4 (extract
  `_finalize_lift_segments(..., trim_fn=None)`); two-end trim scans; two manual cumsum
  loops where `_prefix_sum` exists; cable-speed filter comprehension ×3; raw/smoothed
  ele-delta blocks.
- **`trail_match.py` local dups:** byte-identical `_read_trail_cache_stale` /
  `_read_road_cache_stale`; endpoint-touch completion override copy-pasted between the
  summary and timeline passes.
- **`route_attempts.py` / `route_builder.py`:** identical 6-line `node_xy` build
  (route_attempts, two spots); double-checked-locking read block written twice in route_builder.
- **`sidebar_cache.py`:** `(entry.get("date") or "")[:10]` in both read and write paths.
- **`sync/strava_sync.py`:** identical ISO→epoch block in the skip and write branches.
- **`app.py` remaining:** region-artifact edge iteration dup (→ `_iter_region_edges`);
  near-identical `_downsample_latlon` / `_downsample_polyline` (one core + key accessor);
  inline decimation reimplementing `_decimated_coords`; `re.fullmatch(r"[a-f0-9]{12}", …)`
  id guard ×3 (→ precompiled `_ID_RE`); repeated `load_regions()` linear-scan lookup
  (→ `_region_by_id`), incl. `_route_proposal_for_ride` calling `load_regions()` per loop iter.
- **`point_gap_seconds(a, b, start_field, end_field)`** to collapse the "parse two point
  timestamps, diff seconds, guard KeyError/ValueError" pattern in `trail_match` + `route_attempts`.

### Deliberately skipped (don't redo)
- Stale `554` / `669` hard-coded counts in comments — appear in **8 places**; editing the
  2 the report flagged would *worsen* the inconsistency for ~zero value.
- `sync` credential parsers + status I/O — differ in required keys / merge semantics,
  network path not covered by offline tests.

## Remaining — Frontend (needs a browser to verify)
- **`LazyMapManager`** factory — `routes.html` lazy mini-map LRU ↔ `logs.html` are
  near-identical (routes even comments "mirrors /logs").
- **Shared Leaflet basemap helpers** — `makeStreetLayer()` / `makeSatelliteLayer()` /
  `makeStaticMiniMap(el)` across `compare.html` (×4), `heatmap.html`, `logs.html`,
  `routes.html` (note: routes uses CartoDB tiles — only the locked-map options are shared).
- **Mapbox 3D init boilerplate** duplicated `heatmap.html` ↔ `compare.html`.
- **View-toggle / fullscreen + basemap swap** duplicated `compare.html` ↔ `heatmap.html`.
- **Mapbox chrome CSS** duplicated verbatim → move to `static/base.css`.
- **`validLatLngs(points)`** track-coord sanitizing filter (compare.html, two spots).
- **`index.html` JS:** `haversineKm` vs `_haversineKm` (make one a wrapper);
  `renderRunStats` / `renderTrailStats` shared close-and-exit choreography;
  `typeShortLabel` is now a pure pass-through to `typeGlyph` (one call site);
  `build3DHeatGeoJSON` / `buildHeatPoints` share the stride/downsample loop (heatmap.html).
- **Escape variants left on purpose:** `routes_edit.html` `escapeHtml` (uses `String(s)`,
  not null-coalesced); `training_load.html` `_attrEsc` and `index`/`trails` `_escText`
  (escape fewer chars); `summary.html` `fmtSec` (null handling vs `fmtDuration`);
  `review.html` `_fmtTime` (no `utils.js` equivalent — would need a new shared helper).

## How to resume
The full verified report lived in the workflow run `woz17u6wd` (transcript may be gone after
`/clear`). This file is the durable summary. Pick a bucket, grep to re-locate the symbols
(line numbers drifted), and verify with `python -m pytest -q` plus — for behavior-sensitive
`app.py` endpoints — the before/after live-server SHA diff pattern used this session
(capture `/api/...` responses, restart Flask, re-capture, compare).
