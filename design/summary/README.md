# Handoff: GPX Activity Summary — Per-Activity Collapsible Rows

## Overview
A dense, dark-mode dashboard that summarizes a user's outdoor GPX activity (Fat Bike, Mountain Bike, Snowboard) for a rolling window (default 365 days). The page leads with cross-activity totals + an "active days" ribbon, then breaks down each activity into a collapsible row with year-over-year monthly chart, performance stats, recent log, and personal records.

The visual language is "engineering log / Bloomberg terminal" — monospace numerals, tight 1px gridlines, low-saturation fills on near-black surfaces, color used purely as activity identity (orange / green / blue).

## About the Design Files
The files in this bundle are **design references created in HTML** — a React-via-Babel prototype demonstrating intended look and behavior. They are **not production code to copy directly.**

Your task is to **recreate this design in the target codebase's existing environment** (React, Vue, SwiftUI, native, etc.) using its established component primitives, styling conventions, and data layer. If no environment is established yet, choose the most appropriate framework and implement the design there.

The mock data in `source/data.jsx` (the `ROLLUPS`, `HISTORY`, `RECENT`, etc. shapes) shows the data contract the UI expects — wire your real GPX/activity backend to produce equivalent shapes (or adapt the components to your existing shape).

## Fidelity
**High-fidelity.** Colors, type, spacing, chart geometry, and interactions are intentional — recreate pixel-perfectly. The only deliberate placeholder is the underlying activity data (mocked).

---

## Files in this bundle

| File | Purpose |
|---|---|
| `preview.html` | Open in any browser to see the design rendered. |
| `source/data.jsx` | Mock data + units/format helpers. **Read first** — defines the data contract. |
| `source/atoms.jsx` | Shared UI primitives (Stat, Card, MiniBars, etc). Most of these are unused by the combined view but show the broader design system. |
| `source/yoy-chart.jsx` | The `YearOverYearChart` component — grouped bars + overlay line modes. |
| `source/v-combined.jsx` | The page itself — header, totals strip, ribbon, and the per-activity collapsible rows. |

The page is rendered by `<VCombined units="metric" daysBack={365} />`.

---

## Design Tokens

### Colors

```
Surface
  Page background          #0a0c0c
  Card / panel background  #0f1313
  Border (hairline)        rgba(255, 255, 255, 0.06)
  Border (row divider)     rgba(255, 255, 255, 0.08)
  Border (faint dotted)    rgba(255, 255, 255, 0.05) (1px dotted)

Text
  Primary                  #ffffff
  Secondary                rgba(255, 255, 255, 0.6) – 0.7
  Tertiary / labels        rgba(255, 255, 255, 0.4) – 0.45
  Muted / units            rgba(255, 255, 255, 0.3) – 0.35
  Faint                    rgba(255, 255, 255, 0.04) – 0.05 (empty bars)

Activity accents (oklch — convert to your color space if needed)
  Fat Bike    accent    oklch(0.74 0.14 55)        ≈ #d68a4a (warm orange)
              soft      oklch(0.74 0.14 55 / 0.18)
  MTB         accent    oklch(0.78 0.15 145)       ≈ #4fc97a (green)
              soft      oklch(0.78 0.15 145 / 0.18)
  Snowboard   accent    oklch(0.78 0.13 230)       ≈ #5fb6e0 (blue)
              soft      oklch(0.78 0.13 230 / 0.18)

Semantic
  Positive / up           oklch(0.78 0.15 145)  (green — same as MTB)
  Negative / warm warn    oklch(0.74 0.14 55)   (orange — same as Fat Bike)
  Neutral last-activity   #ffffff               (used when 2–7d ago)
```

### Typography

```
Sans (UI)            'Inter', system-ui, -apple-system, sans-serif
                     weights: 300, 400, 500, 600
Mono (numerals)      'JetBrains Mono', ui-monospace, 'SF Mono', monospace
                     weights: 400, 500, 600
                     ALWAYS combined with: font-variant-numeric: tabular-nums

Type scale (px / weight / letter-spacing)
  H1 hero number          32 / 500 / -0.02em      (e.g. "102")
  H1 hero suffix          32 / 300 / -0.02em      ("days outside" — at 0.45 alpha)
  Stat number (KPI)       30 / 500 / -0.02em      mono, tabular
  Stat unit suffix        13 / 400                @ 0.4 alpha
  Body                    14 / 500                row labels ("Mountain Bike")
  Body small              12 / 400                detail rows
  Number inline (header)  11 / 400                mono, tabular
  Date / mono small       10–11 / 400             mono
  Eyebrow / label         10 / 500 / 0.14em uppercase   @ 0.4 alpha
  Hero eyebrow            11 / 500 / 0.18em uppercase   @ 0.4 alpha
  Table column header     9  / 400 / 0.08em uppercase   mono @ 0.3 alpha
  Glyph chip              13 / 700                mono ('F' / 'M' / 'S')
  Chart tooltip           10 / 400                mono on #15191a
```

### Spacing

The page uses a loose 4-px subgrid; commit these specific values:

```
Page padding                   32px top/bottom · 36px sides
Section gap (vertical)         22px
KPI cell padding               20px 22px
Card padding                   20px 22px
Row header padding             14px 18px
Row body padding               4px 18px 22px
Glyph ↔ label gap              18px
Detail-stats row padding       6px 0
Recent-log row padding         6px 0
Records row padding            8px 0
KPI strip cell gap             1px (achieved by 1px gap on a darker background)
Chart bar group gap            3px (between months)
Chart bar gap (within group)   1px (between year bars)
Ribbon cell gap                1px
```

### Radius / shadows

```
Card / panel radius     4px
Glyph chip radius       4px
Toggle group radius     4px
Pill (legend swatch)    2px
Bar element radius      1–2px
Tooltip radius          3–4px
No drop shadows anywhere — separation comes from 1px borders on near-black.
```

### Layout

```
Outer max-width    1320px (centered)
Background         #0a0c0c full-bleed; designed for desktop. Below ~900px, the
                   per-row 3-col grid (Performance / Recent / Records) should
                   collapse to single-column.
```

---

## Page Structure (top to bottom)

The page is one column, ~36px side padding, 22px vertical gap between sections.

### 1. Page header (`<VCombined>` top row)

Two-column flex, `align-items: flex-end`, `justify-content: space-between`.

**Left:**
- Eyebrow: `Rolling 365 days` (10px, uppercase, 0.18em tracking, alpha 0.4).
  - Text changes with `daysBack` prop: 365 → "Rolling 365 days", 90 → "Last 90 days", 30 → "Last 30 days", else `{N}d`.
- H1: `<big-number>` + ` <suffix>` where suffix is `"days outside"`.
  - Number is `totalRollup().days` (sum of all-activity days in window).
  - Suffix at 0.45 alpha, weight 300; number at full alpha, weight 500.
  - 32px, letter-spacing -0.02em.

**Right (text-align: right):**
- Eyebrow: `Last activity` (10px, uppercase, 0.14em).
- Line 1: formatted date of most recent activity (`Mar 22`), 16px, mono.
- Line 1 (cont): ` · {N}d ago` — color rule:
  - ≤1 day: green `oklch(0.78 0.15 145)`
  - 2–7 days: white
  - >7 days: warm `oklch(0.74 0.14 55)`

### 2. All-activities KPI strip

A 5-column grid, separated by 1px gaps over a faint `rgba(255,255,255,0.06)` background so the gaps render as gridlines. Each cell:

- Background `#0f1313`, padding `20px 22px`.
- Eyebrow label (10px uppercase 0.14em, alpha 0.4, margin-bottom 10px).
- Big mono number (30px / 500 / -0.02em / lh 1).
- Optional small suffix at 13px / alpha 0.4 (e.g. "max 27d", "m").

The 5 cells, in order:

| # | Label | Value source | Suffix |
|---|---|---|---|
| 1 | `Total days` | `totals.days` | — |
| 2 | `Current streak` | `${current}d` | `max ${longest}d` |
| 3 | `Total ascent` | `fmtElev(totals.elev_gain_m, units)` | `m` / `ft` |
| 4 | `Total descent` | `fmtElev(totals.elev_loss_m, units)` | `m` / `ft` |
| 5 | `Moving time` | `fmtHours(totals.moving_h)` (e.g. `126h 00m`) | — |

Outer wrapper has 1px border `rgba(255,255,255,0.06)` and `border-radius: 4px overflow: hidden`.

### 3. Active-days ribbon

A single panel (background `#0f1313`, 1px border, radius 4, padding `20 22`).

**Top of panel (flex row, space-between, margin-bottom 14):**
- Left: eyebrow `Active days · rolling 365 days` (lowercased to match the dynamic range).
- Right: legend — for each activity, a 10×10 px swatch (radius 2) in its accent color + 3-letter `short` label (`FAT`, `MTB`, `SNB`) at 10px mono, alpha 0.6. 14px gap between legend items.

**The ribbon itself:**
- One thin vertical cell per day in the window (default 365). All cells share `flex: 1` with `gap: 1px` and `height: 36px`.
- Cell background:
  - Active day: that activity's `accent` (whichever activity is "dominant" for that date — `RECENT` overrides `HISTORY`).
  - Inactive: `rgba(255,255,255,0.04)`.
- Hover state: 1px white outline on the cell, cursor `pointer` only on active days.
- Below the ribbon (margin-top 8px, 10px mono, alpha 0.4):
  - When NOT hovering: left `"365 days back"`, right `"today →"`.
  - When hovering an active cell: left `2026-03-22` (ISO date), right activity label colored in that activity's accent.

### 4. Section heading "By activity"

Eyebrow line on its own (margin-top 4): `By activity · all-time` (10px uppercase 0.18em, alpha 0.4).

### 5. Per-activity stack (the centerpiece)

A vertical stack of 3 collapsible rows, one per activity, in `ACTIVITY_ORDER` (`fatbike`, `mtb`, `snow`). The whole stack is wrapped in a 1px border with `border-top: none`, radius 4 — so the first row's `border-top` provides the top edge.

**Each row:**

Container:
- `border-top: 1px solid rgba(255,255,255,0.08)`
- `border-left: 2px solid {activity.accent}` ← left rail in the activity color
- `background: #0a0c0c`

#### 5a. Collapsed header (the `<button>`)

Single-line flex row, padding `14 18`, gap `18`, full-width clickable.

Left → right:
1. **Glyph chip** (28×28, radius 4, mono 13/700). Background = `accentSoft`, color = `accent`. Glyph = `'F' | 'M' | 'S'`.
2. **Activity name** (14px / 500 / white) — e.g. `Mountain Bike`.
3. **Spacer** (`flex: 1`).
4. **Headline stats** (1–4 of them, depending on the activity's `metrics` array):
   - `metrics: ['days', 'distance', 'ascent', 'moving']` for fat / mtb
   - `metrics: ['days', 'descent', 'distance', 'moving']` for snow (descent matters; ascent does not)
   - Each stat is a single `<span>`, `min-width: 80, text-align: right`, mono 11px, tabular:
     - `<value>` in white
     - ` <unit>` at alpha 0.4 (only if applicable: km/mi, m/ft)
     - `  <LABEL>` at alpha 0.3, fontSize 9, letter-spacing 0.1em uppercase, margin-left 6
5. **"{N}d ago"** (mono 10px, alpha 0.45, min-width 60, text-align right). Hidden when `last_date` is null.
6. **Chevron** (`›`, mono 16px, alpha 0.4, 16px wide) — rotates 90° when expanded, 120ms transition.

#### 5b. Expanded body

Padded `4 18 22`. Three regions, top to bottom:

##### (i) Chart toolbar — flex row, space-between, margin-bottom 14, flex-wrap.

Left:
- Eyebrow with the chart subject: `{metric label} · year over year` (e.g. `Distance · year over year`).
- Then a YoY badge (computed from `HISTORY[k][2026][metric]` summed for elapsed months vs 2025 same-month-window):
  - `↑ 14% vs '25 (4mo)` colored green when ≥ 0
  - `↓ 22% vs '25 (4mo)` colored warm-orange when < 0
  - Mono, no letter-spacing.

Right (gap 10):
- **`MetricToggle`** — segmented buttons, one per `chartMetrics` entry for the activity. The active button's background = activity `accent`, text `#0b0d0c` weight 600. Inactive buttons: transparent, alpha-0.55 white. 5×12 padding, fontSize 10, uppercase, 0.06em tracking, mono. Container has 1px alpha-0.06 border, radius 4, divided by 1px alpha-0.06 between buttons.
  - Activity → metric options:
    - **Fat / MTB:** Activities, Distance, Duration, Ascent
    - **Snowboard:** Activities, Vertical (= descent), Duration, Distance
- **`ChartModeToggle`** — two icon buttons:
  - `▮▮` → grouped (default for fat / mtb / snow except as noted)
  - `⌇` → overlay (line-per-year)
  Active button: background `rgba(255,255,255,0.1)`, text white. Inactive: transparent, alpha 0.4.

##### (ii) `YearOverYearChart` — height 140, yearsToShow 4

Most recent 4 years of monthly data for the selected metric.

**Grouped mode (default):**
- 12 month-columns, each a flex row of N year-bars (4 bars per column), flex 1, gap 1px between bars, gap 3px between months.
- Year shading: opacity ramps from 0.32 (oldest) to 1.0 (latest) of the activity's `accent`. The latest year additionally gets a 1px top stroke (its own accent) to crisp it.
- Null months render as a 1px tall `rgba(255,255,255,0.04)` stub.
- Hover: bar goes to full opacity; tooltip appears 28px above the column on `#15191a` w/ 1px alpha-0.1 border, 3px radius, mono 10px, 4×8 padding: `{Month abbr} {Year} · {formatted value}`.

**Overlay mode:**
- One SVG line per year (path through 12 monthly points), same opacity ramp.
- Latest year: stroke-width 2; older: 1.2. Latest also gets 1.6r dots at each point.
- `vector-effect: non-scaling-stroke` so widths don't distort.
- Null values are skipped (segment ends).

**Below the chart:**
- **Month axis** — 12-col grid, gap 3px, 9px mono, alpha 0.3, single-letter labels `J F M A M J J A S O N D` centered in each column.
- **Year legend** — flex-end row, gap 12, 9px mono. For each year: 10×2 px bar swatch in accent (with the same opacity ramp) + year number. Latest year's number is white; others alpha 0.5.

##### (iii) Three-column body grid — `1fr 1.4fr 1fr`, gap 28, margin-top 24

**Performance** (left, 1fr):
- Eyebrow `performance`.
- Rows of `label / value` with thin 1px alpha-0.04 dividers (no divider on last row), 6px vertical padding.
- Label: 12px alpha 0.55. Value: 12px white, mono, tabular.
- Order:
  - **Fat / MTB:** avg speed, top speed, avg HR, max HR, avg power, streak (`current/longest`).
  - **Snowboard:** avg speed, top speed, avg HR, max HR, streak. (No power row.)

**Recent** (middle, 1.4fr):
- Eyebrow row: left `recent`, right `{count}` (alpha 0.3 mono).
- Column header row, 9px mono uppercase 0.08em alpha 0.3, with bottom 1px alpha-0.06 border, 6px bottom padding:
  - For non-snow: `date  name  dist  ↑  dur` (template `60px 1fr 60px 50px 50px`)
  - For snow:     `date  name  dist  ↓  dur` (template `60px 1fr 60px 60px 50px`)
- Up to 5 rows from `RECENT` filtered by activity. Each row uses the same grid:
  - `date` (mono 11, alpha 0.5) — formatted as `Mar 22`.
  - `name` (sans 12, white, ellipsis on overflow).
  - `dist`, `↑/↓ elevation`, `dur` (mono 11, tabular). Elevation + duration cols rendered at alpha 0.65.
  - 1px dotted alpha-0.05 bottom divider; cursor `pointer`.

**Records** (right, 1fr):
- Eyebrow `records`.
- Up to 5 rows from `ROLLUPS[activity].prs`. Grid: `14px 1fr auto`, gap 10, padding `8 0`, dividers 1px alpha-0.04 (none on last).
- Star `★` in activity accent.
- Middle: label (alpha 0.7, 11px) + tiny date below (alpha 0.35, 9px mono, margin-top 2).
- Right: value (white, mono, tabular).

---

## Interactions & Behavior

| Interaction | Behavior |
|---|---|
| Click activity row header | Toggle that row's expanded state. Initial state = all expanded. |
| Hover ribbon cell | If active: 1px white outline; bottom caption swaps to `{ISO date}  {activity label in accent}`. |
| Hover chart bar (grouped) | Bar goes to full opacity; tooltip appears above. |
| Hover overlay line | No hover (in this version) — overlay is read-only. |
| Click metric toggle | Re-renders the chart in that metric. Each row tracks its own metric independently. Default is `count` (or `descent` for snowboard). |
| Click chart-mode toggle (▮▮ / ⌇) | Switches between grouped bars and overlay lines for that row. Default `grouped`. |
| Click recent-log row | Cursor: pointer (the prototype doesn't navigate, but production should open the activity detail). |
| Units (`metric` / `imperial`) | Page-level prop. All distance/elevation/speed values format-flip via the helpers in `data.jsx`. |
| `daysBack` (window) | Page-level prop. 365 / 90 / 30 supported, plus arbitrary day count. Drives both header label and ribbon length. |

### Animations / transitions

- Chevron rotation: `transform: rotate(90deg)`, transition 120ms.
- Bar hover: opacity 80ms.
- Toggle hover/active: instantaneous (visual feedback through accent fill).
- No expand/collapse height animation in the prototype — the body simply mounts/unmounts. If you want a smooth transition, animate `max-height` 200ms ease.

---

## State Management

Per-page (or per-activity-row) state needed:

```
VCombined
  openSet:    Set<activityKey>      which rows are expanded
              (default: all activities open)

ActivityRow (one per activity)
  metric:     'count' | 'distance' | 'duration' | 'ascent' | 'descent'
              (default: 'count', or 'descent' for snowboard)
  mode:       'grouped' | 'overlay'         (default: 'grouped')

ActiveDaysRibbon
  hover:      number | null         index of hovered cell

YearOverYearChart
  hover:      [yearIdx, monthIdx] | null
```

No external state library required.

### Derived data needed

- **`totalRollup()`** — aggregate across activities for the KPI strip and header H1. Sums `days`, `elev_gain_m`, `elev_loss_m`, `moving_h`; max of `longest_streak` and `current_streak`; latest of `last_date`; computes `days_since`.
- **`buildActiveDays(daysBack)`** — for the ribbon. Returns `{ set: Set<iso>, dominant: Map<iso, activityKey> }`. RECENT entries override HISTORY.
- **YoY % per row** — `sum(thisYear[0..monthsElapsed]) vs sum(lastYear[0..monthsElapsed])` for the selected metric.

---

## Data Contract

These are the shapes the UI consumes. Replicate them on your backend (or adapt the components — the shapes are intentionally simple).

### `ACTIVITY_DEFS[id]`

```
{
  id: 'mtb',
  label: 'Mountain Bike',
  short: 'MTB',                    // 3-letter legend label
  accent: 'oklch(0.78 0.15 145)',
  accentSoft: 'oklch(0.78 0.15 145 / 0.18)',
  glyph: 'M',                      // single-character chip
  season: 'Summer',                // unused in this view; kept for other variants
  metrics: ['days','distance','ascent','moving'],     // headline stats, in order
  chartMetrics: [                                      // metrics the user can toggle
    { key: 'count',    label: 'Activities' },
    { key: 'distance', label: 'Distance' },
    { key: 'duration', label: 'Duration' },
    { key: 'ascent',   label: 'Ascent' },
  ],
}
```

### `ROLLUPS[id]` — per-activity rolling-window summary

```
{
  days: 48,
  distance_km: 1184.6,
  elev_gain_m: 16920,
  elev_loss_m: 17010,
  moving_h: 72.4,
  avg_speed_kmh: 16.4,
  max_speed_kmh: 54.7,
  avg_hr: 156, max_hr: 188,
  avg_power_w: 214,                // null for snowboard
  longest_streak: 11,
  current_streak: 2,
  last_date: '2026-04-25',         // ISO date
  prs: [
    { label: 'Longest',  value: '68.95 km', loc: 'Kicking Horse Bike Park', date: '2025-08-12' },
    // ... up to 5
  ],
}
```

### `HISTORY[id][year]` — multi-year monthly series

```
{
  count:    [12 numbers or nulls],   // activity count per month
  distance: [12 numbers or nulls],   // km
  duration: [12 numbers or nulls],   // hours
  ascent:   [12 numbers or nulls],   // m
  descent:  [12 numbers or nulls],   // m
}
```
Nulls represent months without data (pre-first-activity, or future months in the current year). Use `null`, not `0` — empty bars vs zero bars render differently.

### `RECENT[]` — recent activity log

```
{ id, type, date, name, dist, elev, dur, avg, max, hr }
//        type ∈ keyof ACTIVITY_DEFS
//        dist in km, elev in m, dur as 'h:mm' string
```

### Format helpers (replicate or adapt)

```
fmtDist(km, units)       1184.6 km  →  '1185'   |   '736' (mi)
distUnit(units)          'km' | 'mi'
fmtElev(m, units)        16920  →  '16,920'  |  '55,512' (ft)
elevUnit(units)          'm' | 'ft'
fmtSpeed(kmh, units)     16.4  →  '16.4'  |  '10.2' (mph)
speedUnit(units)         'km/h' | 'mph'
fmtHours(h)              72.4  →  '72h 24m'
fmtDate(iso)             '2026-04-25'  →  'Apr 25'
fmtMetric(value, key, units)         dispatcher used by chart tooltips
```

---

## Assets

No raster assets, icons, or fonts beyond:

- **Inter** — Google Fonts, weights 300/400/500/600.
- **JetBrains Mono** — Google Fonts, weights 400/500/600.

The activity glyphs (`F`, `M`, `S`) and chevron (`›`) are plain text. Chart bars and the ribbon are rendered with `<div>` flex/grid; the overlay-mode chart uses inline SVG. No icon library required.

If your codebase has a different mono pairing (e.g. SF Mono, IBM Plex Mono), substitute it — the dial-tone is "compact, clearly numeric, tabular-figures-on" and any quality monospace works.

---

## Implementation notes

- **`font-variant-numeric: tabular-nums` everywhere a number lives.** This is what makes the columns of mono numbers line up. If you skip this, the design will look subtly broken.
- **Color is identity, not decoration.** Each activity's accent appears in the row's left rail, glyph chip, ribbon cells, chart bars, PR stars, the active metric toggle, and the legend swatch. Don't reuse accents semantically (e.g. don't use the green for "good"), or you'll fight the activity-color rule. The one exception that's already baked in: the YoY positive/negative badge reuses MTB green / Fat-Bike orange — that's intentional and acceptable because the badge shows magnitude, not activity.
- **Borders, not shadows.** Every separator is a 1px alpha-low-white line on near-black. Avoid drop shadows; they'll wreck the engineering-log feel.
- **Dense, not cramped.** Whitespace is in the larger gaps (22 between sections, 28 in the body grid) — inside cells stays tight (6px row padding). Don't loosen the inner density.
- **Empty / loading / error states.** The prototype doesn't model these. Suggested treatment:
  - No activities for a metric in a year: render the year's bars at the empty fallback (`rgba(255,255,255,0.04)`, 1px tall) — same as nulls.
  - No PRs / no recent: show the eyebrow with a single dim row `— no records yet —` (alpha 0.3, italic, mono).
  - Loading: skeleton shimmer at alpha 0.04 → 0.08 over the same shapes.
- **Responsive.** Below ~960px, drop the body 3-column grid to a single column (Performance → Recent → Records vertically). Below ~720px, stack the KPI strip 2 wide (or scroll-x).

---

## Quick start for the implementer

1. Open `preview.html` in a browser. Click row headers to collapse/expand. Hover the ribbon and the chart bars to see tooltips. Try the metric toggles and switch ▮▮ ⇄ ⌇.
2. Skim `source/data.jsx` to lock in the data contract — that's what your backend needs to produce (or what you need to adapt the components to).
3. Recreate the page in your codebase's idioms. The component breakdown in `source/v-combined.jsx` (`VCombined` → `ActivityRow` → `DetailStats` / `RecentLog` / `PRList`, plus `ActiveDaysRibbon` and the toggles) is a good outline; reuse names if it helps.
4. Wire real data; remove the units/window props or expose them via your settings UI.
