# AlForks Metrics Reference

This doc pins down how every number the app displays is computed, so a
future reader (or a future bug report) can trace any value back to the
code that produced it.

All line numbers are approximate — use a grep to find the current
definition if the file has moved.

---

## Where stats live

Every activity has a single `stats` dict that flows through three layers:

1. **`parse_gpx`** (app.py ~527) computes the raw stats from the GPX file
   during initial parse. Written to disk cache at `cache/gpx/<filename>.json`.
2. **`_effective_data`** (app.py ~494) wraps the cached stats. For MTB
   activities it re-runs `_algo_mtb` on the cached points and replaces
   `segments` / `stats` via `_merge_stats` (detection.py ~258).
3. **`_apply_trim`** (app.py ~2208) + **`_apply_smoothing`** (app.py
   ~2131) recompute stats via **`_stats_from_trimmed`** (app.py ~2065)
   when the user has set a trim range or smoothing window.

`hr_*` fields (`hr_avg`, `hr_max`, `hr_zones`, `hr_max_used`) are added
later by **`_merge_hr_into_data`** (app.py ~1355) when a Garmin HR cache
file exists for the activity's date.

---

## Activity-level metrics

### `distance_km`
- **Where:** `parse_gpx` (riding distance accumulator, app.py ~575)
- **Formula:** Sum of `haversine(point_i, point_{i-1})` for every
  consecutive pair of track points whose `is_assisted[i]` is `False`.
  Points in assisted (lift) segments do NOT contribute. GPS samples
  with out-of-order timestamps contribute 0 (guarded in the per-point
  loop at app.py ~540).
- **Units:** kilometres, rounded to 2 decimal places.
- **Trimmed override:** `_stats_from_trimmed` recomputes from the
  cached `pts[i]["dist_km"]` delta between each pair, excluding
  assisted indices (boundary index belongs to the previous segment —
  see the `[start + 1, end + 1)` convention at app.py ~1999).

### `duration_sec`
- **Where:** `parse_gpx` (app.py ~599)
- **Formula:** Wall-clock difference between the first and last point's
  `<time>` tag. Includes lift waiting, trailhead idling, lunch — every
  second between start and end of the recording.
- **Units:** seconds, rounded to integer.
- **Trimmed override:** `_stats_from_trimmed` uses the first and last
  point's time after trim, so a trim from 0.5 km to 8.0 km produces
  the wall-clock between those two samples, not total recording time.

### `elev_gain_m` / `elev_loss_m`
- **Where:** `parse_gpx` per-point loop (app.py ~582–585)
- **Formula:** For each non-assisted transition, add positive
  `ele_delta` to `elev_gain`, subtract negative `ele_delta` from
  `elev_loss`. No threshold — every positive step is counted, including
  GPS barometric noise. Assisted segments do NOT contribute.
- **Units:** metres, rounded to integer.
- **Trimmed override:** Same logic on the trimmed point list, using
  `pts[i].get("ele") - pts[i-1].get("ele")` for each delta.

### `assisted_gain_m`
- **Where:** `parse_gpx` per-point loop (app.py ~579)
- **Formula:** Sum of positive `ele_delta` values across points where
  `is_assisted[i]` is True. Net *climb* while on lifts / shuttles /
  gondolas. Negative deltas during a ride-down don't cancel it.
- **Reason:** Lets the Summary view attribute "cable metres" vs
  "pedal metres".

### `avg_speed_kmh`
- **Where:** `parse_gpx` (app.py ~605) / `_merge_stats` (detection.py
  ~258) / `_stats_from_trimmed` (app.py ~2065).
- **Formula:** `riding_dist_km / (riding_dur_sec / 3600)`. The
  denominator is **riding time only** — seconds spent in assisted
  (lift) segments are excluded. This is a deliberate convention set
  2026-04-24; a ski day with 50 % lift time otherwise reports an
  artificially-low "average" that conflates pace with lift wait.
- **Cache invalidation:** `ALGO_SIG` prefix bumped to `v9-riding-avg`
  so any disk cache built with the old (wall-clock) denominator is
  invalidated on next load.
- **Note:** `duration_sec` remains wall-clock for display. Only the
  denominator of `avg_speed` excludes lifts.
- **Edge case:** `None` if there are no non-assisted transitions
  (e.g., a pure-lift import).

### `max_speed_kmh`
- **Where:** `parse_gpx` per-point loop (app.py ~586)
- **Formula:** Max of the smoothed `per_pt[i]['speed']` across ALL
  points (including assisted). Smoothing is the same `_median_filter(k=5)`
  used for display speed. Raw spikes over 150 km/h are dropped to
  `None` before smoothing (app.py ~544) so a GPS glitch can't spike
  this field.
- **Units:** km/h, rounded to 1 decimal place.

### `lift_count`
- **Where:** `parse_gpx` return dict (app.py ~625)
- **Formula:** `sum(1 for s in segments if s["type"] == "assisted")`.
  Counts distinct assisted runs — the segment boundaries come from
  `_build_segments(is_assisted)` (detection.py ~208), which collapses
  consecutive True flags into one segment. MTB activities reach the
  same count via `_compute_algo_stats`, which increments on every
  False → True transition — mathematically equivalent.

### `peak_ele_m`
- **Where:** `parse_gpx` return dict (app.py ~626)
- **Formula:** Max of `point["ele"]` across all points where `ele` is
  not None, rounded to integer. `None` if every point lacks elevation
  (or the max is 0).

### `difficulty` (sidebar only)
- **Where:** `_difficulty_score` (app.py ~1569)
- **Formula:** `round((distance_km * (1 + elev_gain_m/distance_km/50)) ** 0.5)`,
  clamped to ≥ 1.
- **Calibration:** 10 km flat ride ≈ 3; 20 km flat ≈ 4; 10 km with
  1000 m gain ≈ 6; 20 km with 1500 m gain ≈ 8.
- **Scope:** Arbitrary scale for relative comparison between rides;
  does not match any external standard (Strava difficulty, route
  difficulty grades, etc.).

---

## HR metrics (optional, Garmin-sourced)

Appears only when a Garmin daily HR cache file
(`cache/hr/<YYYY-MM-DD>.json`) exists for the activity's date and the
activity has a resolvable local timezone. Layered onto the stats dict
by `_merge_hr_into_data`.

### `hr_avg`
- **Formula:** `round(sum(bpm for all in-window samples) / count)`.
- **Window:** `[first_activity_ms - 2 min, last_activity_ms + 2 min]`.
  The 2-minute buffer forgives small watch-vs-GPX clock skew.

### `hr_max`
- **Formula:** `max(bpm)` over the same window. GPS buffer samples
  usually have lower HR (resting), so they don't pull this down.

### `hr_zones`
- **Shape:** 5-element list, one entry per zone, seconds each.
  Index 0 is Z1 (lowest), index 4 is Z5 (highest).
- **Bins:** based on `_effective_max_hr()`. Boundaries:
  - Z1 (index 0): bpm / max_hr < 60 %
  - Z2 (index 1): 60–70 %
  - Z3 (index 2): 70–80 %
  - Z4 (index 3): 80–90 %
  - Z5 (index 4): ≥ 90 %
- **Window:** **Strictly `[first_activity_ms, last_activity_ms]`** —
  no buffer. The 2-min buffer used by `hr_avg`/`hr_max` would count
  trailhead resting HR as Z1 time, which skews distributions.
- **Accumulation:** For each consecutive sample pair with dt ≤ 5 min,
  add `dt` seconds to the zone matching the pair's average bpm. Gaps
  over 5 min are skipped (the rider stopped recording).

### `hr_max_used`
- The max-HR value used for the zone bins. Comes from
  `_effective_max_hr()` — either the user's `max_hr_override` in
  `config.json`, or the observed 99.5th percentile of cached HR
  samples if no override is set. Recorded so the UI can show which
  figure was used.

---

## Detection: what is "assisted"?

`is_assisted` is a parallel boolean list the same length as `points`.
`True` means "this point's delta-to-arrive was travelled via a lift,
shuttle, or other mechanized assistance." False means "rider was moving
under their own power (pedalling, skinning, walking, skiing)."

Which algorithm produces `is_assisted` depends on the activity's
**effective type**:

### Type resolution (`_effective_type_for`, app.py ~2362)
1. Explicit `meta.type` on the activity (set via the UI) wins.
2. Otherwise, walk the matched regions (point-in-polygon against user
   region geometry in `regions.json`):
   - If activity month is in the region's `winter_months` (default
     Nov–Apr) and the region has `winter_default_type`, use that.
   - Otherwise use the region's plain `default_type`.
3. Empty string if nothing matches.

### Algorithm selection
| Effective type | Algorithm | Runs when |
|---|---|---|
| `mtb` | `_algo_mtb` | `_effective_data` recomputes on every request (cached separately) |
| any non-MTB (ski, snowboard, hike, other, "") | `_algo_lift` | baked into disk cache at parse time |

Reason for MTB re-computation: the MTB detector (`_algo_mtb`) uses
higher elevation-rate thresholds to reject pedal effort vs cable
speed, and includes a high-speed shuttle detector. Running it lazily
means non-MTB activities don't pay its cost.

### Algorithm registry
All eight algorithms are listed at `DETECTION_ALGORITHMS` (detection.py
~628) and shown in the comparison view (`/comparison` route). In
summary:

- **lift (default):** elev-rate ≥ 5 m/min ∪ time-gap → OSM snap → speed-min trim
- **mtb:** elev-rate ≥ 15 m/min ∪ time-gap ∪ high-speed-shuttle ∪ OSM → OSM snap
- **smart:** all three primitives (elev-rate, speed+sinuosity, time-gap) → OSM snap → boundary trim
- **speed_osm:** speed+sinuosity ∪ time-gap → OSM snap → boundary trim
- **osm:** station-proximity only (match track points to mapped aerialway endpoints within 100 m)
- **elev_rate:** sustained uphill (5 m/min for 60 s)
- **heuristic:** time-gap ∪ speed+sinuosity
- **time_gap:** GPS dropout > 200 s with ≥ 100 m gain

### Segment boundaries
`_build_segments` (detection.py ~146) collapses the `is_assisted` list
into `[{type, start, end}]` segments. **Adjacent segments share a
boundary index** for rendering continuity: if a riding→assisted
transition occurs at index `i`, the outgoing riding segment ends at
`i − 1` and the incoming assisted segment starts at `i − 1`. This
lets the frontend draw a continuous coloured polyline at the join.

For stats, the shared index belongs to the *previous* segment's type —
`_stats_from_trimmed` iterates each assisted range as `[start + 1,
end + 1)` so index `start` is attributed to the preceding segment. See
the commit of 2026-04-24 (`0e85465`) for the regression that required
this fix.

---

## Cache invalidation

### `cache/gpx/<filename>.json` — parsed activity cache
- **Key:** `(CACHE_VERSION, file_mtime)` — first 8 hex chars of
  `MD5(ALGO_SIG)` plus the GPX file's mtime.
- **Invalidated by:** any change to a detection threshold that is
  hashed into `ALGO_SIG` (detection.py ~570). That includes every lift
  / assisted threshold, station proximity, MTB elevation rate, shuttle
  speed / duration / gain, and the algorithm version tag
  (`v9-riding-avg` at time of writing). Tweaking an MTB threshold
  without adding it to `ALGO_SIG` leaves the disk cache stale — hence
  the regression test at `TestAlgoSig.test_sig_changes_when_mtb_threshold_changes`.

### `_mem_cache` — process-local LRU of parsed activities
- **Size:** 400 entries (LRU eviction).
- **Key:** `filename`.
- **Sentinel:** `_UNPARSEABLE` marker is cached when `parse_gpx`
  returns `None` (e.g., a 1-point GPX), so the same unparseable file
  isn't re-attempted on every request.

### `_mtb_seg_cache` — in-memory MTB segmentation cache
- **Key:** `filename`. Each entry is `{mtime, segments, stats}`; a
  stored mtime that drifts from the file's current mtime by more than
  1 second is treated as stale and re-computed.
- **Scope:** not keyed on `segment_overrides` — overrides are stored
  server-side but applied client-side, so they don't invalidate the
  server's MTB segmentation.

### `_HR_MERGE_CACHE` — in-memory HR-merge result cache
- **Key:** `(filename, gpx_mtime, hr_mtime, effective_max_hr)`.
- **Scope:** process-local, resets at each Flask restart. Changes to
  the max-HR in `config.json` produce new keys automatically.

### OSM lifts cache (`cache/lifts/<bbox_hash>.json`)
- **Key:** bbox rounded to 2 decimal places (~1 km resolution) — see
  `_lift_cache_path`. Two tracks in the same resort area share a
  single Overpass fetch.

### Weather + geocode caches
- **Weather** (`cache/weather/…`): per (lat, lon, date_str). Kept
  forever — historical weather doesn't change.
- **Geocode** (`cache/geocode.json`): reverse lookup per rounded
  (lat, lon) to 1 decimal. Uses Nominatim.

---

## Trim & smoothing

These are user overrides stored in `metadata.json`, applied on read —
they do NOT modify the cached GPX or the disk cache entry.

### Trim (`meta.trim = {start_km?, end_km?}`)
- Shape-validated on write (`_validate_trim`).
- Applied by `_apply_trim` (app.py ~2198).
- Slices `points` to the subset whose `dist_km` falls in
  `[start_km, end_km]`, re-bases the first point's `dist_km` to 0,
  adjusts segment indices accordingly, and recomputes stats via
  `_stats_from_trimmed`.
- The returned dict carries a `trim_full_distance_km` field so the
  UI can show "trimmed from 14.3 km".

### Smoothing (`meta.smoothing = {window, start_km?, end_km?}`)
- Applied by `_apply_smoothing` (app.py ~2011).
- Replaces each point's lat/lon with a moving-average over `window`
  neighbours. If `start_km`/`end_km` are set, only points inside that
  range are smoothed; outside points keep their raw coordinates.
- Smoothing runs *after* trim when both are set.
- Recomputes stats after smoothing (distances change slightly).

### Segment overrides (`meta.segment_overrides`)
- Shape-validated: list of `{type, start, end}` dicts.
- **Backend stores but does NOT apply them** — the client (index.html)
  reads `meta.segment_overrides` and substitutes them for the
  server-computed segments at render time, also recomputing displayed
  stats.

---

## GPX time convention

TrailForks (and many other GPX exporters) stamp track points with
`+00:00` offsets even though the HH:MM values are the rider's local
time. AlForks treats GPX `<time>` values as **naive local** and
re-anchors them to the activity's IANA timezone (resolved via
Open-Meteo based on the track centroid). See `_merge_hr_into_data`
(app.py ~1394) for the detailed explanation.

During the fall-back DST hour (once a year) the wall-clock is
ambiguous; fold=0 is chosen explicitly (pre-transition / DST). This
is a deliberate and documented choice — the commit comment at
`7fe37a9` explains why.
