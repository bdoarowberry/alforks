# Handoff: Training Load page

## Overview
The **Training Load** page is a fitness-coach view for the AlForks GPX activity tracker.
It surfaces a single 12-week training window so the user can answer:

- **Am I getting fitter?** (28-day Z2 heart-rate trend)
- **Am I overdoing it?** (acute / chronic load + form/freshness)
- **Where am I spending my minutes?** (HR-zone composition)
- **What's my output?** (weekly volume, weekly climbing)
- **What were my best efforts in this window?** (personal records — scoped to the visible window only)
- **How do my activities split?** (per-activity totals for the season)

Everything on the page is computed from a 12-week slice of weekly fitness data
plus a recent activity log. There is no "lifetime" framing here — by design.

## About the design files
The files in `source/` are **HTML/JSX design references**. They were built as a
prototype to show the intended layout, color, typography, and information
hierarchy. They are **not** production code to copy directly.

The implementation task is to **recreate this page in the target codebase's
existing environment** (React, Vue, SwiftUI, native, etc.) using its established
component patterns, design tokens, and chart library — or, if no environment
exists yet, choose the most appropriate framework for the project and implement
the design there.

## Fidelity
**High-fidelity.** Colors, typography, spacing, and chart geometry are all
final. Recreate pixel-perfectly using the codebase's existing libraries. The
chart implementations in the source are inline SVG / flexbox-bar — feel free to
swap to your charting library (Recharts, Visx, Chart.js, native canvas, etc.)
as long as the visual result matches.

---

## Page structure (top → bottom)

The page is a single scrolling column at design width **1320 px**, padding
**20 px**, gap **12 px** between sections, on background `#080a0c`.

### 1. Header
- Eyebrow: `COACH VIEW` — uppercase, letter-spacing 0.3em, color `oklch(0.7 0.2 25)` (V4_RED), 9 px, weight 700.
- Title: `Training load · {today's date}` — 22 px, weight 500, letter-spacing -0.02em, white.
- Right-aligned meta: `12 weeks of data · ramp into MTB season` — 11 px, monospace, dim.

### 2. Z2 Hero (`V4Z2Hero`)
The flagship chart. Filled area + line + dots.
- Panel: `#0d1115` bg, `1px solid rgba(255,255,255,0.07)` border, **left border `3px solid` V4_RED**, radius 4, padding 22/28 px.
- Layout: header row (title left, latest reading right), then chart.
- Header left:
  - Eyebrow: `AEROBIC FITNESS` (V4_DIMMER, 9px, 0.22em letter-spacing).
  - Title: `28-day Z2 heart-rate average` + dim suffix `lower = fitter`.
- Header right:
  - Eyebrow: `LATEST`.
  - Big number: latest Z2 in bpm, 36 px, monospace, tabular-nums.
  - Delta line: `↓ Xbpm vs 12wk ago (Y%)` — green (V4_GREEN) if delta < 0 (fitter), else red.
- Chart:
  - SVG, 720 × 220 viewport, `preserveAspectRatio="none"` so it stretches.
  - Y-axis: 3 dashed ticks at min, mid, max — values in monospace, dim.
  - X-axis: week labels (`MM-DD`) at the bottom.
  - Stroke: V4_RED, 2 px. Fill: linearGradient V4_RED 0.28 → 0.
  - Dots: 3 px radius, V4_RED, with a 2 px stroke matching panel bg (creates a halo).

### 3. Load cards row (`V4LoadCards`)
Five equal columns, gap 8 px. Each card:
- Panel `#0d1115`, 1px border, radius 4, padding 16/18 px.
- Eyebrow label (uppercase 9 px, dim).
- Big value (24 px monospace, accent color) + small unit (12 px, dim).
- Sub-line (10 px monospace, dim).

| Card | Value | Sub | Accent |
| --- | --- | --- | --- |
| **Acute load** | last-4-week avg of `volume_h`, 1 dp, `h/wk` | `last 4 weeks` | V4_AMBER |
| **Chronic load** | last-8-week avg of `volume_h`, 1 dp, `h/wk` | `last 8 weeks` | V4_PURPLE |
| **Form** | `chronic - acute`, signed, `h` | `Fresh` if > 1, `Fatigued` if < -1, else `Steady` | green / red / blue accordingly |
| **Volume** | sum of `volume_h` across the window, 0 dp, `h` | `12 weeks total` | V4_BLUE |
| **Climbing** | sum of `climbing_m`, locale string, `m` | `12 weeks total` | V4_AMBER |

Note: this is a *simplified* CTL/ATL/TSB. Acute = 4-week avg, Chronic = 8-week
avg, Form = chronic - acute. If your real backend uses proper rolling-EMA TSS
math, plug it in here — the card chrome stays the same.

### 4. Weekly HR-zone time (`V4HRStack`)
Big stacked bar chart — one stack per week, zones stacked floor → ceiling Z1 → Z5.
- Panel: standard.
- Header: title `Weekly HR-zone time` + sub-title `Where you spend your minutes` (14 px, white).
- Right side of header: legend with all 5 zones — color swatch + label, 10 px monospace, wraps if needed.
- Chart area: 200 px tall, 36 px left padding for the y-axis (in hours, dim monospace), 22 px bottom padding for week labels.
- Bars: flex row, `gap: 6 px`. Each bar is a `flex-direction: column-reverse` stack with `gap: 1 px` between zones (creates thin dark separators).
- Zone colors:
  - Z1 Recovery: `oklch(0.6 0.02 240)` (grey)
  - Z2 Endurance: `oklch(0.78 0.13 230)` (blue)
  - Z3 Tempo: `oklch(0.78 0.15 145)` (green)
  - Z4 Threshold: `oklch(0.78 0.16 60)` (amber)
  - Z5 VO2max: `oklch(0.7 0.2 25)` (red)
- X-axis: every other week label shown to avoid crowding.

### 5. Volume + Climbing + Zone composition (3-column row)
Layout: `1.6fr 1fr` grid → left side has its own internal `1fr 1fr` grid for the two bar charts; right side is the composition card.

#### 5a. Weekly volume (`V4VolumeAndClimb` left)
- Standard panel.
- Eyebrow `WEEKLY VOLUME`.
- 140 px tall flexbox bar chart, gap 6 px, left padding 32 px for y-axis (`Xh`).
- Bars: V4_PURPLE, opacity 0.9, min height 1 px.
- Bottom labels: every other week.

#### 5b. Weekly climbing (`V4VolumeAndClimb` right)
Same as 5a but bars are V4_AMBER and y-axis shows `Xm`.

#### 5c. Last-4-weeks zone composition (`V4ZoneDonut`)
- Standard panel.
- Eyebrow `LAST 4 WEEKS · ZONE COMPOSITION`.
- 16 px tall stacked horizontal bar (rounded radius 2, 1px gaps between segments) showing percentage share per zone for the last 4 weeks combined.
- 2-column legend grid below with `Z1`/`Z2`/`Z3`/`Z4`/`Z5` (just the prefix), each with the percentage right-aligned.
- Footer (12 px top padding, top border): copy `Z2 share is X% — aim for 60–80% in base season.` — Z2 share value rendered in white monospace.

### 6. Records + Activity totals (2-column row)
Layout: `1.4fr 1fr` grid, gap 8 px.

#### 6a. Personal records (`V4Records`) — **WINDOW-SCOPED**
This is the **important behavioral spec**: records here are NOT lifetime PRs.
They are computed from activities **whose date falls inside the visible
FITNESS_WEEKS window** (start = first week, end = last week).

The implementation in `v4-coach.jsx` exposes a `deriveWindowPRs(units)` helper
that reduces the in-window subset of `RECENT` activities to:
- **Longest ride** — by `dist`
- **Most climbing** — by `elev`
- **Longest duration** — by `dur` (parse `H:MM`)
- **Top speed** — by `max` (km/h)
- **Highest avg HR** — by `hr` (bpm)

If the window has zero activities, render an empty state ("No activities in
this window yet").

UI:
- Panel: standard.
- Header row: eyebrow `PERSONAL RECORDS · THIS WINDOW` left; on the right, dim monospace shows the window range `MM-DD → MM-DD`. **This window framing is required so the user understands these are scoped, not lifetime.**
- Each row: `1fr auto` grid, padding 10/0 px, divider between rows.
  - Left: label (12 px white) + sub `{activity-color dot} {activity name} · {date}` in 10 px monospace dim.
  - Right: value, 18 px monospace tabular-nums, color V4_AMBER, letter-spacing -0.01em.

#### 6b. Activity totals (`V4ActivityTotals`)
- Panel: standard.
- Eyebrow `ACTIVITY TOTALS · THIS SEASON`.
- One row per activity, columns: glyph badge | name | days | moving time | climbing-or-descent.
- Glyph badge: 22 × 22, radius 3, `accentSoft` background, `accent` foreground, monospace 11 px weight 700.
- Name: 13 px white.
- Days: 13 px monospace, dim, right-aligned, min-width 70.
- Moving time: 13 px monospace, white, right-aligned, min-width 90.
- Elev: 13 px monospace, V4_AMBER, right-aligned, min-width 90. For snowboard, show `elev_loss_m` (descent); otherwise `elev_gain_m` (ascent).
- Bottom border on every row (so the last row also has a divider — intentional).

---

## Design tokens

```css
--bg:        #080a0c;   /* page bg                              */
--panel:     #0d1115;   /* every card / chart bg                */
--panel-hi:  #11161a;   /* (reserved, not currently used)       */
--line:      rgba(255, 255, 255, 0.07);
--dim:       rgba(255, 255, 255, 0.5);
--dimmer:    rgba(255, 255, 255, 0.32);
--ink:       oklch(0.96 0 0);

--green:     oklch(0.78 0.15 145);  /* good / fitter / fresh    */
--red:       oklch(0.7  0.20  25);  /* hot accent / Z5 / fatigued */
--blue:      oklch(0.78 0.13 230);  /* Z2 / steady              */
--amber:     oklch(0.78 0.16  60);  /* climbing / records value */
--purple:    oklch(0.72 0.13 305);  /* volume                   */
```

Activity colors (used only in records & activity totals):

```css
--act-fatbike:  oklch(0.74 0.14  55);  /* warm orange   */
--act-mtb:      oklch(0.78 0.15 145);  /* green         */
--act-snow:     oklch(0.78 0.13 230);  /* blue          */
```

HR-zone palette (Z1 → Z5):

```
Z1 Recovery   oklch(0.6  0.02 240)  grey
Z2 Endurance  oklch(0.78 0.13 230)  blue
Z3 Tempo      oklch(0.78 0.15 145)  green
Z4 Threshold  oklch(0.78 0.16  60)  amber
Z5 VO2max    oklch(0.7  0.20  25)  red
```

### Spacing

- Page padding: 20 px.
- Section gap: 12 px.
- Card grid gap: 8 px.
- Panel padding: typically `16-22 px` vertical / `18-28 px` horizontal.

### Radius / borders

- Card / panel radius: **4 px** (deliberately low — this UI is technical, not playful).
- Stack-bar segment radius: 2 px.
- Glyph badge radius: 3 px.
- Border: always 1 px `--line`. Heroes have a 3 px colored left border accent.

### Typography

- Sans family: **Inter** (300, 400, 500, 600).
- Mono family: **JetBrains Mono** (400, 500, 600). Use mono everywhere a value is shown — values, axis ticks, dates, deltas.
- Tabular nums: ON for every numeric value (`font-variant-numeric: tabular-nums`).
- Eyebrow labels: 8–9 px, `letter-spacing: 0.18em–0.30em`, `text-transform: uppercase`, weight 600.
- Body / row label: 11–13 px.
- Section title: 14 px white.
- Hero number: 36 px.
- Card big number: 22–24 px.
- Record value: 18 px.

---

## Data shapes (use these as the contract)

The implementation expects three inputs:

```ts
type FitnessWeek = {
  wk: string;          // 'MM-DD' label of the week's Monday
  volume_h: number;    // training hours that week
  climbing_m: number;  // self-powered climbing in meters that week
  hr_zones: [number, number, number, number, number]; // hours in Z1..Z5
  z2_avg: number;      // 28-day rolling Z2 HR average in bpm, as of end of week
};

type RecentActivity = {
  id: string;
  type: 'fatbike' | 'mtb' | 'snow';
  date: string;        // 'YYYY-MM-DD'
  name: string;
  dist: number;        // km
  elev: number;        // meters of climbing
  dur: string;         // 'H:MM'
  avg: number;         // km/h average
  max: number;         // km/h top
  hr:  number;         // average HR bpm
};

type ActivityTotal = {
  type: 'fatbike' | 'mtb' | 'snow';
  days: number;        // distinct days with at least one activity in the window
  moving_h: number;    // total moving time hours
  elev_gain_m: number; // sum (use elev_loss_m for snow)
  elev_loss_m: number;
};
```

Number of weeks is variable — the page renders whatever length array it gets,
but tested for **12**.

---

## Behavior notes

### Records must be window-scoped
This is a non-trivial product decision — confirmed by the user. The Records
section must reflect the visible 12-week window, not lifetime. If the user
later toggles a different window length, recompute. Do not mix in lifetime PRs
under any circumstance — that would re-introduce the bug we just removed.

### Form / freshness coloring
- Form > 1h → `Fresh` (green)
- Form < -1h → `Fatigued` (red)
- otherwise → `Steady` (blue)

### Z2 trend color
- If latest Z2 < first Z2 → green ↓ (fitter)
- Else → red ↑

### No interactivity required
The prototype renders static. Tooltips on bar hover (showing exact weekly
values) are nice-to-have but not required for v1.

### Responsive
The prototype is built for **1320 px** design width. For narrower viewports,
the cleanest move is to:
- Stack the 5 load cards into a 2-column grid below ~900 px, then 1-column below ~600 px.
- Stack the 3-column row (volume / climbing / composition) at ~1000 px.
- Stack records + totals at ~900 px.

---

## Files

In `source/`:

- **`Training Load.html`** — the entry point; loads React + Babel and mounts `<V4Coach />`.
- **`v4-coach.jsx`** — the page component. All sub-components (`V4Z2Hero`, `V4LoadCards`, `V4HRStack`, `V4VolumeAndClimb`, `V4ZoneDonut`, `V4Records`, `V4ActivityTotals`) live here. The window-scoped PR helper is `deriveWindowPRs(units)`.
- **`data.jsx`** — mock data + helpers (`fmtDist`, `fmtElev`, `fmtSpeed`, `fmtHours`, `fmtDate`, `daysBetween`). The relevant exports for this page are `FITNESS_WEEKS`, `RECENT`, `ROLLUPS`, `ACTIVITY_DEFS`, `ACTIVITY_ORDER`, `HR_ZONE_COLORS`, `HR_ZONE_LABELS`, `TODAY`. **Drop the `OVERALL_PRS` export** when porting — it is intentionally NOT used by this page anymore.
- **`atoms.jsx`** — shared atoms (loaded by the bundle but not strictly needed by this page; included for vocabulary reference only — feel free to skip).

To preview the bundle as-is: open `source/Training Load.html` in a browser
served over http (it uses ES module-style script tags which require an http
origin — `python -m http.server` from inside `source/` works).

---

## Open questions for the implementer

1. **Real CTL / ATL math** — wire to your backend's TSS-based EMA if one exists, or keep the simple 4-/8-week mean shown here.
2. **Window length** — currently fixed at 12 weeks. Should the user be able to change it (4 / 8 / 12 / 26 weeks)? If yes, expose as a header dropdown and recompute everything (records included).
3. **Empty / loading states** — not designed yet. The prototype assumes data is always present.
4. **Activity totals "this season"** — currently always shows the full `ROLLUPS` (rolling 365d). If the page is genuinely window-scoped, should this also re-derive from the in-window subset of activities? (Recommend: yes, for internal consistency.)
