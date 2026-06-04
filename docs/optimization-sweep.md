# Optimization Sweep — Whole-Repo Review

_Generated 2026-06-03 by a parallel multi-agent review (21 finder agents across 4 dimensions → per-bucket adversarial verification). **82 findings** survived verification; every one was independently re-confirmed against the cited code. The initial run covered 17 buckets; a top-up run covered 4 buckets the first run missed (routing core, app.py mid-third, training/logs, routes templates), so coverage is now complete._

**Severity tally:** 10× P1, 22× P2, 50× P3. No P0 (data-loss/security cliff).

---

## ✅ Fixed this session — all 10 P1s + 13 P2s (tests green, 180 passed)

Applied to the working tree across three commits, each verified with `pytest` (180 passed) + `py_compile`. Detailed entries below are struck through / marked **[done]** where fixed.

**P1 — correctness (6):**
1. `index.html` segment-override silent save → check `res.ok`, keep `editDirty`, surface "Save failed".
2. `index.html:3865` stale list on region-only filter → `region !== 'all'` in the empty-state predicate.
3. `sync_toast.js` compounding poll loops → single tracked `pollTimer` + `schedule()`.
4. `app.py` trail-match cache poisoning → `_effective_for_match()` unifying all four cache-writing sites; detail path feeds canonical (URL-flag-independent) data.
5. `strava_sync.py:364` incremental-sync boundary dropping UTC → tz-aware parse.
6. `app.py` double/triple spike scan → `_spike_flagged_activities()` cached per `_activities_cache_key()`.

**P1 — performance (4):**
7. `trail_match.py` `_snap_points` O(points×ways) → uniform lat/lon grid; verified byte-identical vs brute-force over 1101 points.
8. `route_builder.py` 600 KB artifact re-parsed every call → `(path, mtime)` parse memo; `/trails-geometry` serves 304 from on-disk mtime before building.
9. `app.py` fitness re-parses GPX per HR ride → bake `hr_zones` into the sidebar entry (gated by `sidebar_cache.ENTRY_SCHEMA_VERSION=2`), read it in `_compute_fitness_weeks`.
10. `routes.html` redundant per-region `edges-summary` fetch → `distance_m` computed server-side in `api_routes_list`.

**P2 (13):** weights lost-update lock · `sidebar_cache` corruption-tolerance widen · `sync_toast`/`compare.html`/`summary_archived.html` HTML-escaping · `setup.html` `kickSync` interval stacking · `trails.html` search debounce · `routes_edit.html` dead `roadLayerGroup` removed · `review.html` dead Odd-Times fallback · `review.html` triple-dup summary block extracted · `index.html` `saveMeta` `res.ok` · `index.html` segment-build early-break (×2) · weather fetch timeout 10s→4s.

**P2 — deferred (9, with rationale):**
- `/api/activity` etag trail-fingerprint — folding a per-request dir scan into every (incl. non-MTB / 304) request; needs a design decision (gate on MTB or cache the fingerprint).
- `setup.html renderRegionList` sort-once — small list; a safe cache needs mutation-tracking across call sites. Low value/risk ratio.
- `route_attempts.py` coalesce-noise misread **and** `trail_match.py` priming-loop min/max — both change attempt/traversal **output**, so they need a `TRAIL_MATCH_VERSION`/route-attempts version bump + equivalence validation. Not a quick win.
- `detection.py` speed-sinuosity prefix sums — `ALGO_SIG` + float-summation-order sensitivity.
- `app.py:838` `has_hr` naive timestamp — subtle tz semantics; wants its own focused change + test.
- `logs.html` full re-render after toggle, `index.html` smoothing-drag rAF, `_saved_route_cellsets` hoist — moderate refactors, deferred from the quick-wins pass.

---

## Health summary

The codebase is generally clean — the simplify audit did its job, and most findings are P3 latent footguns. The signal clusters into recurring themes:

1. **Silent-failure UI (correctness).** Many `fetch()` mutators never check `res.ok` before showing success and mutating local state → a failed save shows "Saved" and is lost on reload. *(The two worst, in index.html, are now fixed; more remain at P2.)*
2. **Repeated full re-parse on read paths (performance).** A ~600 KB region artifact, full GPX tracks, and the whole routes directory are re-read + `json.loads`'d on hot request paths with no parsed-result memo — the dominant cold-cache cost on `/routes`, `/training`, and the suggestions build.
3. **Cache key vs. data / etag coherence.** Trail-match cached raw points under a trim-encoding key *(fixed)*; the `/api/activity` etag omits the cross-ride trail-rank fingerprint so a 304 body can show stale ranks.
4. **Timezone / naive-datetime handling.** A naive `.timestamp()` shifted the Strava sync boundary *(fixed)*; the same pattern recurs in `has_hr` and the summary ribbon.
5. **Re-render / re-sort on every keystroke or toggle.** Three template search boxes rebuild full DOM per keystroke; `/logs` re-fetches and tears down all Leaflet maps after a single boolean toggle.

---

## P1 — remaining (clear win, real impact)

### Performance

**`trail_match.py:1095-1130` — `_snap_points` is O(points × ways) with no spatial index (documented 5-20s for a ~5k-point ride).**
For every GPS point the inner loop linearly scans every way; `_project_ways` builds numpy segment arrays but no spatial index. Warm reads are cached; first-compute and prewarm pay it in full.
→ Build a uniform grid of way bboxes (~`SNAP_THRESHOLD`-sized cells) in `_project_ways`; in `_snap_points` look up only ways in the point's cell + 8 neighbours.

**`route_builder.py:528-539` — the ~600 KB region artifact is re-read + `json.loads`'d on *every* `get_region_artifact` call (no in-memory parsed cache, despite app.py comments claiming "in-memory after first build").**
`_read_valid_artifact` re-parses the full file every call. Hot paths: `api_region_trails_geometry` (app.py:2743) calls it **before** the `if_none_match` 304 check, so even a revalidation re-parses; `_region_edge_polylines` runs once per region in `api_routes_list` and `_saved_route_cellsets`. On `/routes` and the suggestions pass that's multiple full 600 KB parses per request.
→ Add a process-local memo in `route_builder` keyed on `(artifact_path, st_mtime)` (mirror the `_GEOCODE`/`_hr_merge` pattern); move the `get_region_artifact` call in `api_region_trails_geometry` to **after** the 304 check; fix the misleading comments.

**`app.py:4596-4608` — `_compute_fitness_weeks` re-parses the full GPX + re-merges HR per HR activity because `hr_zones` was never baked into the sidebar entry.**
`_build_activity_entry` bakes `hr_avg`/`hr_max` (854-863) but not `hr_zones`, so the fitness loop calls `get_activity → _effective_data → _merge_hr_into_data` per HR ride just to read zone seconds. The `_HR_MERGE_CACHE` LRU hides it after warmup, but the first post-restart `/api/training` / `/api/fitness/weekly` (and any window beyond the LRU) pays the full per-ride track read the baking was meant to avoid.
→ Bake `hr_zones` into the sidebar entry stats alongside `hr_avg`/`hr_max`; read `act['stats']['hr_zones']` in `_compute_fitness_weeks`.

**`templates/routes.html:325-346, 430-435` — redundant per-region `edges-summary` fetch to compute a distance the server already has.**
`render()` awaits `getEdgeLengths(rid)` for every distinct region (N extra `GET /api/regions/<id>/edges-summary` round-trips) only to sum `length_m`. But `api_routes_list` already calls `_region_edge_polylines` per region and `_iter_region_edges` already yields `length_m` — the data is in hand at list-build time. The N blocking round-trips delay first paint and recur on filter/layout toggles.
→ Sum `distance_m` server-side in `api_routes_list`, add it to each route object, and drop `getEdgeLengths`/`edgeLengthCache`/`routeDistance_m` (keep the endpoint — other callers use it).

---

## P2 — worthwhile

### Correctness

- **`app.py:4836-4860` — `weights` add/delete is a lost-update race.** Unlocked read-modify-write with `threaded=True`; every sibling store uses a lock. → Add `_weights_lock`.
- **`sidebar_cache.py:73-87` — corruption tolerance only wraps `json.loads`, not field access.** A structurally-valid entry with a scalar `start_latlon` makes `tuple(sl)` raise, unguarded; caller (`app.py:929`) isn't wrapped, so one bad entry 500s the whole sidebar. → Widen the try/except through `return`.
- **`app.py:2627` (index.html metadata PATCH mutators) ignore `res.ok`.** `saveMeta`→"✓ Saved", `approveIssues`, `repairSpikes`, `toggleRegionPin` all proceed unconditionally; contrast the checked `.ok` calls at 2224/2389/3452/3459/3552. → Gate success on `res.ok`. *(Same class as the fixed segment-save bug.)*
- **`templates/setup.html:1447-1456` — `kickSync` stacks overlapping `setInterval` polls.** Handle is local-only; three buttons call it. → Module-scoped `_syncPoll` + `clearInterval` before reassign.
- **`templates/review.html:897` — Odd Times date fallback to `a.date` is dead** (`_fmtYmd` returns truthy `'—'`). → Return `null` on bad input or restructure.
- **`app.py:838-846` — `has_hr` window check uses `.timestamp()` on possibly-naive ISO times** → can flip `has_hr` when server tz ≠ activity tz. → Attach tz before `.timestamp()`.
- **`app.py:3586-3595 vs 3663-3677` — `/api/activity` etag omits the trail-match dir fingerprint.** The body embeds cross-ride trail ranks from `_get_leaderboards()` (keyed on `_trail_match_dir_fingerprint()`), but the etag doesn't include it, so after another ride is rescanned a client holding the 304 shows stale ranks. → Fold `_trail_match_dir_fingerprint()` into the etag (the same fingerprint already used at app.py:3285).
- **`route_attempts.py:531-545` — a coalesced multi-fragment entry can be misread as noise and skipped instead of resetting the attempt.** `_coalesce_timeline` keeps the first fragment's `distance_km`/`coverage_pct` (un-summed), and `_is_noise_entry` reads exactly those, so a fragmented real traversal can read as tiny and stitch a non-attempt into a phantom attempt. → Accumulate `distance_km`/max `coverage_pct` in coalesce, or key `_is_noise_entry` off span length.
- **`trail_match.py:826-836` — first-direction priming loop tracks only a running min** → misses an early up-then-down swing. → Track running min *and* max.
- **`templates/compare.html:515-521` & `summary_archived.html:625,670` — user-controlled activity titles interpolated into markup unescaped** (broken rendering / XSS class; low real-XSS on single-user). → Escape via the shared helper.
- **`static/sync_toast.js:45,48,51` — sync message into `innerHTML` unescaped** (broken toast on markup). → Inline a tiny escape.

### Performance

- **`templates/logs.html:950,965` — full `/api/comparison` re-fetch + `maps.destroyAll()` + full re-render after every exclude/approve toggle** (a boolean that doesn't change ordering). → Mutate the in-memory item + toggle the row in place; skip `load()`.
- **`templates/trails.html:185-273,377` — full region/trail DOM rebuilt on every search keystroke (no debounce).** (Also re-adds the delegated click listener each render — stacking duplicates.) → Debounce ~120-150ms; fix the listener re-add.
- **`app.py:2999-3013` — `_saved_route_cellsets` re-globs + JSON-parses the entire routes dir once per distinct region in the suggestions build** (R full directory re-parses + R artifact parses). → Hoist `_all_routes()` once; pass a `region_id → [routes]` index; reuse `edge_polys_by_region`.
- **`templates/index.html:1303-1311,1517-1524` — segment-build inner loops scan to end of track despite monotonic `dist_km`.** → `break` early or reuse `nearestPointIdxByDistKm`.
- **`templates/index.html:2470` — smoothing drag re-smooths the whole track + rebuilds Leaflet layers + updates chart on every mousemove.** → rAF-coalesce/debounce.
- **`templates/setup.html:280-281,1101-1154` — `renderRegionList` re-sorts + full-rebuilds DOM on every keystroke** (filter-invariant sort). → Sort once on load; only re-filter on input.
- **`detection.py:362-371` — `_algo_speed_sinuosity` rebuilds a 20-pt slice + comprehension each step, then re-sums the segment four times.** → Use `_prefix_sum` for gain/dur/dist (bit-identical). **Caution:** the speed-sum is float-order-sensitive under `ALGO_SIG`.
- **`app.py:5171-5191` — weather endpoint makes up to two blocking 10s Open-Meteo calls in the request thread** (~20s on a full miss). → Lower `_fetch_hourly_day` timeout to ~3-4s (matches the tz-lookup rationale).

### Simplification

- **`templates/routes_edit.html:215,280,287` — dead `roadLayerGroup`** (created, cleared, re-added, never drawn into; roads go into `trailLayerGroup`). → Delete all three references.
- **`templates/review.html:447-453,757-763,793-799` — the summary/empty-state recompute block is copy-pasted three times verbatim.** → Extract `_dupUpdateSummaryAndEmpty()`.

---

## P3 — minor / latent (50 findings, grouped)

**Redundant compute (perf):**
- `templates/index.html:3524` — `computeRuns` runs twice per ski/snowboard load. Memoize on `(filename, activeSegments)`.
- `detection.py:543-562` — composite algos rebuild ele/dt `_prefix_sum` already built in `_detect_elev_rate_param`. Compute once (bit-identical).
- `templates/index.html:2143` — region popover `renderList` re-sorts the full list on every keystroke. Sort once on open.
- `templates/index.html:1343-1344` — `recomputeStats` parses each timestamp twice via `Date.parse`. Carry the previous epoch-ms.
- `trail_match.py:1610` — `build_leaderboards` + `build_region_trail_index` each re-scan + re-parse the whole cache dir on the same invalidation. Memoize `scan_cached_results` on a dir fingerprint.
- `route_suggestions.py:196-208` — `_complete_linkage` rescans every cluster pair on every merge (latent O(k³)). Cache pair scores.
- `route_suggestions.py:250-258` — stage-1 prefilter re-fetches per-ride attrs in the O(n²) loop. Precompute once.
- `route_attempts.py:91-120,404-405,323` — `_run_covers` is O(verts × ride-span) haversine, no spatial index (bounded by caching + early-exits). Window the inner scan or add a per-span bbox prefilter.
- `route_builder.py:421,432` — per-name linear scan of all ways to fetch one sample is O(names × ways) at build time. Build a `first_way_by_name` dict once.
- `route_builder.py:214-234` — pass-2 interior near-miss junction detection is O(V × N × S) at build time. Bucket segments into a coarse grid.
- `static/utils.js:83,89` — `evictIfOver` re-sorts all entries + calls `getBoundingClientRect` per candidate (forced reflow during scroll; bounded by cap). Iterate the touch-ordered Map; batch rect reads.
- `templates/summary.html:509` — all activity-row chart bodies built eagerly despite a "built lazily" comment. Build on first expand.
- `templates/heatmap.html:744-757` — synchronous `JSON.stringify` of the full set blocks first paint on SSE completion. `applyFilter()` first, then `requestIdleCallback`.
- `app.py:4045-4063` — `api_compare_algorithms` builds bbox in 4 passes, runs every algo uncached, and crashes on a zero-point track (dev tool). Single-pass bbox; guard empty points.
- `sync/garmin_sync.py:393` — `cmd_status` reads+parses each HR cache file twice per date (one-shot CLI). Parse once.
- `scripts/suggestions_oracle.py:22-33` — same GPX JSON parsed twice + leaked handles (throwaway). Parse once; use `with`.
- `scripts/elev_smoothing.py:86-95` — `_moving_avg` is O(n·k) (only matters if ported live). Running-sum window.

**Timezone / display correctness:**
- `templates/summary.html:403-408,487-488` — ribbon date keys mix local-midnight `Date` with UTC `toISOString().slice` → off-by-one for positive UTC offsets (masked by MST). Derive key from local components.
- `templates/summary.html:471` — headline `days` renders literal `'undefined'` when the rollup is missing (no `|| 0`). Use `r.days ?? 0`.
- `templates/index.html:1009-1011` — `buildSparkline` emits `M NaN` on a single-point array. Guard `eles.length < 2`.
- `templates/review.html:499-505` — duplicate-outlier median uses the upper element for even counts. Average the two central values.
- `templates/training_load.html:556,572-579` — hero Z2 "N wk ago" label uses `weeks.length` but the baseline is the first **non-null** week. Track the first non-null index and label the real elapsed span.

**Cache / data-coherence (latent, no current runtime effect):**
- `sidebar_cache.py:46-83` — sidebar fingerprint uses exact rounded-ms mtime vs the parse cache's 1s tolerance → needless recompute on OneDrive jitter. Coarsen to whole seconds.
- `cache_utils.py:81` — backup-prune date slice breaks for a suffixless tracked file (`-0 == 0`); error swallowed → backups accumulate. Strip suffix safely.
- `trail_match.py:1502-1504` — `_RESULT_MEM_CACHE` annotation/comment say a 3-tuple key but the real key is a 4-tuple (`meta_fp`). Fix the annotation (meta_fp staleness footgun).
- `cache_utils.py:105-117` — `LRUCache` annotates value `dict` but stores the `_UNPARSEABLE = object()` sentinel. Broaden to `object`.
- `app.py:3124-3126` — unlocked read of `_route_suggestions_mem` races the unlocked two-key `.update()` (benign, self-corrects). Atomic rebind `{'key':…, 'payload':…}`.

**Correctness — narrow/defensive edges:**
- `sync/strava_sync.py:468` — `cmd_dedup` queues a file matching two activities for move twice (spurious "failed", double-counted report). Dedup move targets.
- `sync/strava_sync.py:338` — a present-but-short time stream collapses trailing timestamps onto the start anchor (`else 0`). Validate stream length up front.
- `app.py:6526-6530` — `_save_dup_dismissals` uses non-atomic `write_text` (comment acknowledges it) while siblings use `_atomic_write`; crash mid-write → corrupt JSON silently swallowed → dismissals lost. Use `_atomic_write`.
- `trail_match.py:1603-1663` — endpoint-completed (≥60% coverage) attempts rank by duration alongside full descents (no coverage tiebreak) → a partial can outrank a full descent. Exclude from ranking or add a coverage-aware sort + test.
- `scripts/dump_region_trails.py:85-86` — prints "Artifact written" even when the write was skipped on a partial Overpass fetch. Stat the path or return a signal.
- `templates/routes_edit.html:578,597` — loaded/suggested segments assigned by reference then mutated in place (currently harmless; fetched fresh). Optional defensive copy.
- `templates/route_detail.html:218-219` — map left on hardcoded Calgary center if region geometry fails to load (masked by the Alberta-only dataset). Center from `REGION.geometry` or show an error state.

**Maintainability / dedup (genuinely new, not on the simplify-audit skip list):**
- `app.py:1822` — loop var `acts` shadows the function-wide activities list in `_summary_data_compute` (latent footgun). Rename.
- `app.py:6620-6648/6906-6916/6966-6980` — the outer night-window odd-time predicate is open-coded three times with subtly different tz handling. Extract `_is_odd_time(activity)`.
- `templates/index.html:3650 vs 2649` — map-legend visibility logic duplicated (already drifted: `?.` in one). Extract `updateMapLegend()`.
- `templates/index.html:2208 & 2711` — two separate `change` listeners on `#header-type` ~500 lines apart. Consolidate or cross-reference.
- `templates/setup.html:1163-1202,1371-1384` — two deep-link IIFEs both `showTab('regions')` + drive the map with racing magic timers. Merge into one ordered routine.
- `templates/compare.html:340-352 vs 585-602` — `drawTrack` / `_drawTrackOnMap` near-duplicate renderers. Factor shared styling into a helper that does **not** clear overlays.
- `templates/summary.html:772-778 vs 807-813` — `prCompareValue` and `renderRecords.prValue` walk the identical PR field chain. Centralize in one `[field, formatter]` array.
- `templates/routes.html:283-291` — `fmtDurHms` is byte-identical in routes.html and route_detail.html (and trails.html has a variant); the attempts-table builder is near-duplicated. Hoist into `static/utils.js`.
- `route_attempts.py:486,180-181,518` — `_scan_one_ride` type hint declares a 5-tuple but the code returns/unpacks 6 (missing `polyline`). Fix the annotation or use a `NamedTuple`.
- `route_builder.py:176-177,440,414-418` — `_build_junctions` docstring says "roads excluded" but it's called with trails+roads on purpose; param named `trails`. Fix the docstring; rename to `ways`.
- `route_suggestions.py:173-209` — `_complete_linkage` lacks tests for 4+ cliques, weak-bridge separation, the early-return, and `_medoid` tie-breaks. Add targeted tests.
- `route_suggestions.py:154` — `cell_similarity` docstring uses mangled union notation `|A|B|`. Fix to `|A ∪ B|`.
- `static/utils.js:23,24` — `fmtElev` uses the default locale (siblings pin `en-CA`) and a different null glyph. Standardize.
- `trail_match.py:824` — dead local `start = 0` in `_split_traversals_linear`. Delete.
- `templates/logs.html:417` — `_naturalSortDir(field)` always returns `'desc'`; the param + comment imply a per-field policy that doesn't exist. Inline the constant or implement the policy.
- `templates/training_load.html:949,967,1045` — `zones_sec` accessed unguarded in three chart renderers while metric/trimp code guards with `|| []`. Make consistent (guarantee the contract or guard everywhere).
