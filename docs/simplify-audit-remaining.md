# Simplify Audit — Remaining Work

Status as of **2026-06-03**. This tracks the leftovers from a whole-repo
simplification audit (reuse / duplication / dead-code / altitude). It is a
to-do list, not a spec — re-verify before acting.

> ✅ **Essentially complete.** Both the offline-verifiable Python bucket and the
> browser-verified frontend bucket are swept (sessions 1–5). What remains below is
> only the **deliberately-skipped** set (decisions recorded inline: haversine,
> detection-`ALGO_SIG` local dups, JSON-writers downgrade, OSM lift-fold declined,
> escape-variant differences). Nothing here is "todo" — it's "decided not to."

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

### Session 2 (2026-06-03, low-risk in-file/cross-file dedup)
- `_ID_RE` precompiled — the 3 `re.fullmatch(r"[a-f0-9]{12}", …)` id guards in `app.py`
- `_region_by_id(region_id)` in `app.py` — 6 `next((r for r in load_regions() …))`
  lookups + 2 `any(…)` membership checks (load_regions is already mem-cached)
- `_downsample_points(items, n, get)` core — `_downsample_latlon`/`_downsample_polyline`
  now one-line wrappers (verified output-identical over 2000 random trials)
- `_iter_region_edges(artifact)` generator — the (trails+roads → entry → edges) walk
  in `api_region_edges_summary` + `_region_edge_polylines`
- `route_attempts._build_node_xy` — the inlined 6-line junction+endpoint index (×2)
- `sidebar_cache._entry_date` — `(entry.get("date") or "")[:10]` in read + write
- `trail_match._read_osm_cache_stale` — unified byte-identical `_read_{trail,road}_cache_stale`
- `route_builder._read_valid_artifact` — the double-checked cache read (before + inside lock)
- `sync/strava_sync._advance_newest_epoch` — the ISO→epoch block in skip + write branches

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
- **Unify the 3 OSM-cache + Overpass-fetch stacks.** **Step 1 DONE (session 2,
  `75ba972`):** the two `trail_match` trail/road stacks now share
  `_fetch_osm_ways_cached` + `_build_ways_query` + `_read_ways_mem_or_disk` +
  `_parse_overpass_ways(extra_tags=…)`; behavior pinned by `tests/test_osm_fetch.py`
  (10 chars tests: query strings, key order, cache bytes, breaker/network fallbacks,
  mem-cache non-cross-contamination). Mem caches + lock registries kept **separate per
  set** on purpose (trail+road same-bbox share a `cp.stem`). **Step 2 STILL OPEN:** fold
  the `app.py` lift stack (`_fetch_osm_lifts`) into the same template. Lower value +
  higher risk — it crosses the module boundary (app.py has its own same-named
  `_try_read_osm_cache`/`_read_osm_cache_stale`/`_osm_lock_for` copies), the lift query
  differs structurally (`out geom;` not `(._;>;);out body;`, hardcoded `pad=0.01` +
  `timeout=20`), uses `_atomic_write` vs hand-rolled tmp+replace, and feeds `ALGO_SIG`
  not `TRAIL_MATCH_VERSION`. If done, pin it with lift golden tests the same way first,
  and decide where the template lives (neutral `osm_fetch.py`, not the trail-matcher).
  **Session 3 call: NOT DOING IT.** The cost/benefit is upside-down — one ~40-line body
  deduped vs. a new module + re-pointing the two just-unified stacks + the lift stack +
  ALGO_SIG golden tests. Revisit only if `osm_fetch.py` is being created for another reason.
- ~~`build_leaderboards` / `build_region_trail_index` shared row extraction~~ —
  **DONE (session 3, `1cbdb10`):** `_iter_completed_attempts(...)` generator; both
  builders are thin consumers. Covered by new `tests/test_leaderboards.py` (5 tests).
- ~~`cached_match` wrapper~~ — **DONE (session 3, `518050d`):** `_cached_match(filename,
  mtime, data, meta_fp)` binds the 3 constant cache dirs; mtime stays a param (4 sites).
- **JSON writers → `cache_utils._atomic_write`** (~7 sites: `trail_match.py`,
  `route_builder.py`, `app.py`). **Downgraded** (OneDrive rationale gone). If done, give the
  sync CLIs a small `atomic_write_text` in `sync/_common.py` rather than importing cache_utils.

### In-file / cross-file dedup (lower risk)
- **`detection.py` local dups (STILL OPEN, but low priority):** snap-and-trim segment
  loop ×4 (extract `_finalize_lift_segments(..., trim_fn=None)`); two-end trim scans;
  cable-speed filter comprehension; raw/smoothed ele-delta blocks. **Held off in
  session 2** — detection feeds `ALGO_SIG`, and the "two manual cumsum loops where
  `_prefix_sum` exists" couldn't be relocated (the prefix sites already use `_prefix_sum`);
  reconcile against `tests/test_detection.py` and only touch if output stays bit-identical.
- ~~`trail_match.py` endpoint-touch completion override~~ — **DONE (session 3,
  `1cbdb10`):** `_endpoint_completion(...)`, predicate injected per pass; verified
  identical across all 28 branch combos. *(stale-cache readers DONE in session 2.)*
- ~~`route_attempts` node_xy~~ / ~~`route_builder` DCL read~~ — **DONE (session 2).**
- ~~`sidebar_cache` date prefix~~ / ~~`strava_sync` ISO→epoch~~ — **DONE (session 2).**
- ~~`app.py` inline decimation reimplementing `_decimated_coords`~~ — **DONE (session 3,
  `518050d`):** `_decimate_latlon(pts, max_points)` core, used by `_decimated_coords` + the
  regions-setup overlay. *(edge-iteration, downsample pair, `_ID_RE`, `_region_by_id` DONE
  in session 2; `_cached_match` DONE session 3.)* The ele-sparkline `//40` stride at the
  sidebar-entry builder is **left alone** — it emits `ele` values, not lat/lon, so it's not
  a `_decimated_coords` reimpl despite the similar stride.
- **`point_gap_seconds` — SKIPPED (session 2).** The three sites diverge too much to share
  cleanly: `_fragment_gap_ok` returns a bool and guards `ValueError` only; the leaderboard
  duration site returns `int` seconds and guards `(KeyError, ValueError)`; `trail_match`'s
  `_gap_sec`/`_dur_sec` are index-based closures over `points` with different fallbacks
  (`inf` vs `0.0`). The shared core is one expression — not worth the semantic-merge risk.

### Deliberately skipped (don't redo)
- Stale `554` / `669` hard-coded counts in comments — appear in **8 places**; editing the
  2 the report flagged would *worsen* the inconsistency for ~zero value.
- `sync` credential parsers + status I/O — differ in required keys / merge semantics,
  network path not covered by offline tests.

## Remaining — Frontend (needs a browser to verify)

### Session 4 (2026-06-03) — done + browser-verified, pushed
- ~~`validLatLngs(points)`~~ — **DONE (`3c2db58`):** extracted in compare.html (2 spots).
- ~~`index.html` `haversineKm`/`_haversineKm` + `typeShortLabel`~~ — **DONE (`a8778d1`):**
  `_haversineKm` wraps `haversineKm`; `typeShortLabel` inlined to `typeGlyph`.
- **Bonus fix (`5ded9d8`):** dark-themed the Leaflet 2D zoom + rotate-compass controls on
  compare/heatmap (they rendered default-white; only index themed `.leaflet-bar`). Compass
  arrow is a two-tone inline-SVG → `invert(1)`. **Follow-up:** consolidate the three
  `.leaflet-bar` copies (index + compare + heatmap) into `base.css` — now known safe.

### Landmine learned this session
- **Mapbox chrome CSS → base.css was ATTEMPTED then REVERTED.** Not because base.css is
  wrong — because it was a **misdiagnosis**: the white zoom/compass controls a tester sees
  on compare/heatmap are **Leaflet** 2D controls, NOT Mapbox. The `.mapboxgl-ctrl` rules
  only style the 3D view (toggle to 3D to see them). The move was actually fine; re-doing it
  is safe but low value. **Lesson: these pages stack Leaflet (default 2D) + Mapbox GL (3D) —
  identify which library owns a control before touching its CSS.**

### Still open (bigger, cross-file — need a shared `static/maps.js` + per-page verify)
- ~~`LazyMapManager` factory~~ — **DONE (`5a38aea`), browser-verified.** Extracted
  `createLazyMapManager({ maxActive })` into `utils.js` (has/get/touch/register/remove/
  evictIfOver/destroyAll/invalidateAll + exposed `instances`); logs (cap 20) + routes
  (cap 24, `sug:<id>` keys) both consume it. Per-page init stays page-specific.
  **Pattern for the rest:** shared map helpers live in `utils.js` (already loaded
  everywhere), per-page init/tiles/colours stay inline; restart Flask (utils.js asset_v +
  cached templates) and verify each page in-browser before committing.
- ~~Shared Leaflet basemap helpers (`makeStreetLayer`/`makeSatelliteLayer`)~~ — **DONE
  (`37262ce`):** extracted to utils.js; compare (main + dual A/B) + heatmap consume them.
  *(`makeStaticMiniMap` not done — see "still open" below.)*
- ~~Mapbox 3D init boilerplate~~ — **DONE (`028a5b6`):** `createTerrain3DMap(containerId,
  token, onLoad)` in utils.js; compare + heatmap `init3D()` are thin wrappers (page adds its
  own sources/layers in the onLoad callback).
- ~~View-toggle / fullscreen + basemap swap~~ — **DONE (`1ff502e`):** `toggleMapFullscreen`
  + `swapLeafletBasemap` in utils.js. The divergent rest of `setMapView` stays per-page.
- ~~`index.html` `renderRunStats`/`renderTrailStats` + heatmap `build3DHeatGeoJSON`/
  `buildHeatPoints`~~ — **DONE (`a6f9f9c`):** `makeDrawerCloser(drawerId, mapWrapClass)`
  (returns `{drawer, mapWrap, closeAndExit}`) in index.html; `eachHeatPoint(fn)` in
  heatmap.html. Surfaced + fixed a pre-existing bug (`f11e613`): leaderboard popups now
  close on track switch (loadActivity didn't reset their `.open`).

### Done (`7f91883`, browser-verified) — frontend bucket complete
- ~~`.leaflet-bar` → base.css~~ — one copy in base.css (loaded after leaflet.css ⇒ wins);
  removed the index + compare + heatmap inline copies. Every Leaflet map themes consistently.
- ~~`makeStaticMiniMap(el)`~~ — utils.js helper wrapping the 8-flag locked-map options; logs
  `initMap` + routes `initRouteMap` consume it.

### Escape variants left on purpose (do not "dedup") `routes_edit.html` `escapeHtml` (uses `String(s)`,
  not null-coalesced); `training_load.html` `_attrEsc` and `index`/`trails` `_escText`
  (escape fewer chars); `summary.html` `fmtSec` (null handling vs `fmtDuration`);
  `review.html` `_fmtTime` (no `utils.js` equivalent — would need a new shared helper).

## How to resume
The full verified report lived in the workflow run `woz17u6wd` (transcript may be gone after
`/clear`). This file is the durable summary. Pick a bucket, grep to re-locate the symbols
(line numbers drifted), and verify with `python -m pytest -q` plus — for behavior-sensitive
`app.py` endpoints — the before/after live-server SHA diff pattern used this session
(capture `/api/...` responses, restart Flask, re-capture, compare).
