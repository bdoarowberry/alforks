# AlForks design system

The cross-page reference for tokens, frame, and components — derived from
**what's actually in the code**, not from the historical per-page specs.

The sibling folders (`summary/`, `training-load/`) contain the original
design specs from when those pages were built. The shipped pages have
since drifted from those specs intentionally, and that's fine — this file
is the source of truth for current canonical patterns. Treat the per-page
specs as historical drafts, not as authority.

When building a new page, read this first. When updating a legacy page,
check the drift tracker at the bottom for what's known to be off.

---

## Tokens

The canonical tokens live in `static/base.css :root` and are inherited by
every template. Any page that redefines `:root` is **drift** unless it's
adding net-new variables (e.g., `--tl-z1..--tl-z5` HR-zone colours on
Training are fine — those don't exist globally).

### Surfaces

| Token | Value | Used for |
|---|---|---|
| `--bg-page` | `#0a0c0c` | Body background |
| `--bg-card` | `#0f1313` | Panels, header, filter bars |
| `--bg-elev` | `#11161a` | Rare; slight raise above bg-card |

### Borders

| Token | Value | Used for |
|---|---|---|
| `--border-hair` | `rgba(255,255,255,0.11)` | Default hairline (panels, dividers) |
| `--border-row` | `rgba(255,255,255,0.14)` | Inputs, list rows, subtle controls |

### Text

| Token | Value | Used for |
|---|---|---|
| `--text-primary` | `#fff` | Headings, primary numerics |
| `--text-secondary` | `rgba(255,255,255,0.82)` | Body text, default labels |
| `--text-tertiary` | `rgba(255,255,255,0.62)` | Eyebrow labels, secondary metadata |
| `--text-muted` | `rgba(255,255,255,0.5)` | De-emphasized labels, axis ticks |
| `--text-faint` | `rgba(255,255,255,0.38)` | Disabled state, placeholder, separator dots |

### Accent

| Token | Value | Used for |
|---|---|---|
| `--accent` | `#3b82f6` | Active state, focus ring, primary action |

For semantic positive/negative deltas, use OKLCH so they look right on the
dark background. Summary defines them locally; promote if a third page
needs them:

```css
--pos: oklch(0.78 0.15 145);  /* up-and-to-the-right */
--neg: oklch(0.74 0.14  55);  /* down */
```

### Type

```css
--font-sans: 'Inter', system-ui, -apple-system, sans-serif;
--font-mono: 'JetBrains Mono', ui-monospace, 'SF Mono', monospace;
```

Both loaded once via `@import` at the top of `base.css`. Never `<link>`
the Google Fonts URL again from a template.

---

## Page frame

Every full-bleed page wraps content in `<main>` (or `<main class="...">`):

| Spec | Value |
|---|---|
| `max-width` | **1680px** for the data-heavy pages (Summary, Logs, Compare, Training, Heatmap-aware containers) |
| `margin` | `0 auto` |
| `padding` | `18px 22px 32px` (compact) or `32px 36px` (airy — Summary only) |
| `display` | `flex; flex-direction: column` |
| `gap` | 12–22px depending on density |

The Heatmap page is intentionally full-bleed (no `<main>` max-width) — map
real estate dominates. That's allowed; everything else is 1680.

**Don't** introduce a third max-width value. If you need narrower for
focus (e.g. setup forms), apply a per-section `max-width` rather than
shrinking the page frame.

---

## Typography rules

### Page H1

```css
font-size: 1.4rem;        /* ~22.4px — equivalent to literal 22px is OK */
font-weight: 500;
letter-spacing: -0.02em;
color: #fff;              /* or var(--text-primary) */
margin: 0;                /* let the page-frame gap do the spacing */
```

Sits inside `<main>` immediately after the global `<header>`. May be
followed by a toolbar/eyebrow row. Don't put the title inside an
element with its own bg-card surface unless the page is map-dominant
(like Heatmap, where the title strip sits above the filter bar).

### Eyebrow

The "small uppercase label above a value or section" pattern. Three
canonical sizes:

| Use | Spec |
|---|---|
| Inline (panel-internal label) | `font-size: 10px; letter-spacing: 0.14em; color: var(--text-tertiary); text-transform: uppercase` |
| Section header | `font-size: 10px; letter-spacing: 0.18em; color: var(--text-tertiary); text-transform: uppercase` |
| Hero / page-level | `font-size: 11px; letter-spacing: 0.18em; color: var(--text-tertiary); text-transform: uppercase` |

All use `font-family: var(--font-mono)` when adjacent to numerics, sans
otherwise — pick one and stick with it within a panel.

### Numerics

Anywhere a column of numbers appears, set:

```css
font-family: var(--font-mono);
font-variant-numeric: tabular-nums;
```

Or scope it via a `.num` class on the section: `.num, .num * { font-variant-numeric: tabular-nums }`.

---

## Components

### Panel / card

```css
background: var(--bg-card);
border: 1px solid var(--border-hair);
border-radius: 4px;
padding: 16–22px;          /* depends on density */
```

For map overlays specifically, use a translucent variant:

```css
background: rgba(15,19,19,0.88);
border: 1px solid var(--border-hair);
border-radius: 4px;
backdrop-filter: blur(6px);
```

Heatmap exposes this as a `.map-panel` class. Reuse the class if you
build another map page.

### Filter chip (canonical)

The Summary / Heatmap pattern. Use this for any year/type/preset
filter row going forward.

```css
/* Resting */
padding: 4px 10px;
font-size: 10px;
font-family: var(--font-mono);
letter-spacing: 0.04em;
color: var(--text-tertiary);          /* or text-muted */
background: rgba(255,255,255,0.04);   /* var(--text-faint) on summary v2 */
border: 1px solid var(--border-hair);
border-radius: 3px;

/* Active */
color: var(--text-primary);
background: rgba(255,255,255,0.10);
border-color: rgba(255,255,255,0.18);

/* Hover (resting only) */
color: var(--text-secondary);
```

**Don't** use the older blue-accent active state (`background: rgba(59,130,246,0.10); color: var(--accent)`) — that's the Logs `.chip` pattern that's now drift.

### Segmented control

Pattern lives in `summary.html` as `.s2-segctrl`.

```css
/* Track */
display: inline-flex;
background: rgba(255,255,255,0.04);
border: 1px solid var(--border-hair);
border-radius: 4px;
overflow: hidden;

/* Buttons */
padding: 5px 12px;
font-size: 11px;
font-family: var(--font-mono);
letter-spacing: 0.06em;
text-transform: uppercase;
color: rgba(255,255,255,0.55);
background: transparent;
border: none;
border-left: 1px solid var(--border-hair);  /* between buttons */

/* Active */
background: rgba(255,255,255,0.10);
color: var(--text-primary);

/* Disabled */
opacity: 0.35; cursor: default;
```

Used for Heatmap's mode/view toggles, Training's window length, Heatmap's
basemap toggle. All consistent.

### Input (text/select/date)

```css
background: transparent;
border: 1px solid var(--border-row);
border-radius: 3px;
color: var(--text-secondary);
font-size: 0.78rem;
padding: 5px 8px;
font-family: var(--font-mono);
color-scheme: dark;        /* date inputs only */
```

Focus uses `border-color: var(--accent)` and optionally a faint blue tint
on the background (`rgba(59,130,246,0.05)`).

### Sync button

`.nav-sync-btn` in `base.css` — already standardised. Don't duplicate.

### Loading state

Two patterns coexist:

- **Block centered text** (Summary `.s2-loading`, Training): `padding: 40px; text-align: center; color: var(--text-tertiary); font-family: var(--font-mono); font-size: 12px`
- **Overlay with progress bar** (Heatmap `#loading`): translucent backdrop + 3px progress bar tinted with `var(--accent)`

Pick by use: block-centered for whole-page fetch, overlay for streaming progress.

---

## Drift tracker

What's known to be off the system, in roughly priority order.

### High-impact drift (worth fixing)

**`training_load.html` defines parallel surface tokens**
- `--tl-bg: #080a0c` (vs `--bg-page: #0a0c0c`), `--tl-panel: #0d1115` (vs `--bg-card: #0f1313`), `--tl-line: rgba(255,255,255,0.07)` (vs `--border-hair: 0.06`).
- Differences are 1–2% lightness; not a deliberate identity, just drift from when this page was built independently.
- Action: replace `--tl-*` references with the global tokens; keep `--tl-z1..z5` (HR-zone colours — those are legitimately Training-specific).

### Medium-impact drift

**Page-frame padding split: `18px 22px 32px` vs `32px 36px`**
- Summary is the only page using the airier `32px 36px`. Either bring it back to the compact value (matches Logs/Compare/Training) or document why dashboards get more breathing room than data tables.

**Border-radius mix: 3px / 4px / 6px / 8px**
- Canonical: 3px for chips/inputs, 4px for panels and segmented controls.
- 6px and 8px appear in legacy pages (Compare picker, Setup type-card, exag-ctrl original). Replace as those pages get touched.

**`.filter-btn` in `base.css` is a vestigial style**
- Currently used only by the archived `summary.html` and overridden locally in `heatmap.html`. The class lives in `base.css` partly out of historical inertia.
- Either: (a) update its definition in `base.css` to match the canonical chip and drop the local override in `heatmap.html`, or (b) delete it from `base.css` and let pages style their own chips.

### Low-impact drift

**Eyebrow letter-spacing variations: 0.14em / 0.18em / 0.22em**
- Mostly explained by the size hierarchy (panel-internal vs section vs hero).
- Training's `.tl-eyebrow` at 0.22em is slightly out of band. Bring to 0.18em next time the page is touched.

**H1 page-title styling is inlined on most pages**
- Could be promoted to a `.page-title` class in `base.css`. Low priority — the inline is fine and it's the same spec everywhere.

**Setup uses many literal hex colours and old-style buttons**
- Whole page is older. Worth a focused pass at some point but not blocking.

### Out of scope (deliberate differences)

- **Heatmap is full-bleed** — map-dominant page; intentionally not constrained to 1680px.
- **Training's red "Coach view" eyebrow** — deliberate visual identity for the technical/coach view. Keep.
- **Compare's blue/amber A/B accent borders** — semantic encoding (A vs B), not chrome. Keep.
- **Activity detail page (`index.html`) `#header-title`** — the title is an editable `<input>`, not a static H1; its 1.05rem sizing is for input affordance, not page title.

---

## Checklist for new pages

When you build a new page, copy this checklist:

- [ ] `<link rel="stylesheet" href="/static/base.css">` (no manual font import)
- [ ] No `:root` redefinition (just use the global tokens)
- [ ] `<main>` with `max-width: 1680px`, `margin: 0 auto`, padding from the canonical pair
- [ ] Page H1 uses the canonical spec (1.4rem / 500 / -0.02em)
- [ ] Filter chips use the canonical chip pattern (neutral-white active)
- [ ] Segmented controls use the canonical `s2-segctrl` pattern
- [ ] Panels use `var(--bg-card) / var(--border-hair) / 4px`
- [ ] Numeric columns use `var(--font-mono)` with `font-variant-numeric: tabular-nums`
- [ ] No literal text hex colours — all text uses `var(--text-*)` tokens
- [ ] No literal border hex colours — all borders use `var(--border-*)` tokens

If you need to deviate, add an entry to "Out of scope" above explaining why.
