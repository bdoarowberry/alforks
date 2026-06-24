/* ─────────────────────────────────────────────────────────────────────────
   Shared mock data for the AlForks Dashboard redesign prototypes.
   Every prototype includes this verbatim (<script src="./mock-data.js">) so
   the numbers are identical across designs — only the visual/layout direction
   differs. NOT production code; this stands in for the /api/summary payload.

   Window framing: "Last 365 days" ending 2026-06-23. Current year 2026 has
   data through June (month index 5); later 2026 months are null (future).
   ───────────────────────────────────────────────────────────────────────── */
(function (root) {
  'use strict';

  const TODAY = '2026-06-23';
  const CUR_YEAR = 2026;
  const CUR_MONTH = 5;            // June, 0-indexed → 2026 has Jan..Jun
  const YEARS = [2023, 2024, 2025, 2026];

  const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const MONTH_LETTERS = ['J','F','M','A','M','J','J','A','S','O','N','D'];

  // Activity types. accent colours stand in for the per-type colours that the
  // real app stores in types.json. isSnow flips the lead metric to descent.
  const TYPES = [
    { id: 'mtb',  label: 'Mountain Bike', glyph: 'MTB', accent: '#f97316', isSnow: false },
    { id: 'road', label: 'Road',          glyph: 'RD',  accent: '#3b82f6', isSnow: false },
    { id: 'hike', label: 'Hike',          glyph: 'HK',  accent: '#22c55e', isSnow: false },
    { id: 'ski',  label: 'Ski',           glyph: 'SKI', accent: '#38bdf8', isSnow: true  },
  ];

  // Headline cross-activity totals: last 365 days + the immediately preceding
  // 365 days (for the year-over-year deltas the redesign leads with).
  const TOTALS = {
    distance_km: 1842,   distance_prev: 1644,
    moving_h:    128.67, moving_prev:   123.80,   // 128h 40m
    climb_m:     38400,  climb_prev:    39600,
    descent_m:   41900,  descent_prev:  40100,
    days_out:    142,    days_prev:     124,
    activities:  198,    activities_prev: 176,
    last_date:   '2026-06-21',
    days_since:  2,
    last_14d_active: 9,
    longest_streak: 11,
    current_streak: 3,
  };

  // ── Deterministic monthly generator ──────────────────────────────────────
  // Seeded so visuals are stable across reloads (no Math.random drift).
  function rng(seed) {
    let s = seed % 2147483647;
    if (s <= 0) s += 2147483646;
    return () => (s = (s * 16807) % 2147483647) / 2147483647;
  }
  function seasonalWeight(m, snow) {
    const summer = [0.35,0.45,0.62,0.80,0.95,1.00,0.98,0.92,0.78,0.60,0.45,0.35];
    const winter = [0.98,0.92,0.78,0.42,0.12,0.02,0.00,0.00,0.03,0.18,0.55,0.95];
    return (snow ? winter : summer)[m];
  }
  // annual = full-year total for the metric in the BASE year (2023).
  function genMonthly(seed, annual, year, snow) {
    const r = rng(seed + year * 31);
    const growth = 1 + (year - 2023) * 0.12;   // gentle year-over-year build
    let sumw = 0;
    for (let m = 0; m < 12; m++) sumw += seasonalWeight(m, snow);
    const out = [];
    for (let m = 0; m < 12; m++) {
      if (year === CUR_YEAR && m > CUR_MONTH) { out.push(null); continue; }
      const base = annual * growth * (seasonalWeight(m, snow) / sumw);
      const jitter = 0.82 + r() * 0.36;
      out.push(Math.round(base * jitter * 10) / 10);
    }
    return out;
  }

  // Per-type annual baselines (2023 full-year) used to synthesise monthly
  // series for every metric the charts can switch between.
  const BASE = {
    mtb:  { distance: 980,  duration: 76,  climb: 25800, descent: 27600, count: 92 },
    road: { distance: 680,  duration: 30,  climb: 7600,  descent: 7400,  count: 40 },
    hike: { distance: 128,  duration: 13.5,climb: 8800,  descent: 8700,  count: 24 },
    ski:  { distance: 92,   duration: 5.0, climb: 1100,  descent: 12200, count: 34 },
  };
  const METRICS = ['distance', 'duration', 'climb', 'descent', 'count'];

  // Per-type rolling-365 rollups (what each "By Activity" card summarises).
  const ROLLUPS = {
    mtb:  { days: 78, activities: 96, distance_km: 1020, climb_m: 26500, descent_m: 28800,
            moving_h: 78.5, avg_speed_kmh: 13.0, top_speed_kmh: 58.4, last_date: '2026-06-21', days_since: 2 },
    road: { days: 34, activities: 41, distance_km: 690,  climb_m: 7800,  descent_m: 7600,
            moving_h: 31.2, avg_speed_kmh: 22.1, top_speed_kmh: 64.2, last_date: '2026-06-18', days_since: 5 },
    hike: { days: 22, activities: 25, distance_km: 132,  climb_m: 9100,  descent_m: 9000,
            moving_h: 14.0, avg_speed_kmh: 4.5,  top_speed_kmh: 7.8,  last_date: '2026-06-09', days_since: 14 },
    ski:  { days: 18, activities: 36, distance_km: 95,   climb_m: 1200,  descent_m: 12500,
            moving_h: 5.0,  avg_speed_kmh: 31.5, top_speed_kmh: 71.0, last_date: '2026-03-22', days_since: 93 },
  };

  // Build per-type monthly history: HISTORY[typeId][metric][year] = [12].
  const HISTORY = {};
  TYPES.forEach((t, ti) => {
    HISTORY[t.id] = {};
    METRICS.forEach((metric, mi) => {
      HISTORY[t.id][metric] = {};
      YEARS.forEach(y => {
        HISTORY[t.id][metric][y] = genMonthly(ti * 97 + mi * 13 + 7, BASE[t.id][metric], y, t.isSnow);
      });
    });
  });

  // Cross-activity monthly history (sum across types), same shape.
  const HISTORY_ALL = {};
  METRICS.forEach(metric => {
    HISTORY_ALL[metric] = {};
    YEARS.forEach(y => {
      const acc = new Array(12).fill(null);
      TYPES.forEach(t => {
        const arr = HISTORY[t.id][metric][y];
        for (let m = 0; m < 12; m++) {
          if (arr[m] == null) continue;
          acc[m] = (acc[m] == null ? 0 : acc[m]) + arr[m];
        }
      });
      HISTORY_ALL[metric][y] = acc.map(v => v == null ? null : Math.round(v * 10) / 10);
    });
  });

  // Records set within the last 365 days, across all activities (the
  // "recent PRs" the redesign surfaces above the fold). kind drives unit.
  const RECENT_PRS = [
    { label: 'Longest ride',   type: 'mtb',  kind: 'dist',  value: 47.3,  date: '2025-09-14', ride: 'Moose Mountain Epic',     file: 'moose-epic.gpx' },
    { label: 'Most climbing',  type: 'mtb',  kind: 'elev',  value: 1840,  date: '2026-05-02', ride: 'Powerline Grind',         file: 'powerline.gpx' },
    { label: 'Top speed',      type: 'road', kind: 'speed', value: 64.2,  date: '2026-04-18', ride: 'Highwood Pass Descent',   file: 'highwood.gpx' },
    { label: 'Biggest vert',   type: 'ski',  kind: 'elev',  value: 1620,  date: '2026-02-11', ride: 'Lake Louise pow day',     file: 'louise-pow.gpx' },
    { label: 'Longest day',    type: 'hike', kind: 'dist',  value: 18.6,  date: '2025-08-03', ride: 'Tent Ridge Horseshoe',    file: 'tent-ridge.gpx' },
    { label: 'Longest moving', type: 'mtb',  kind: 'dur',   value: 15120, date: '2025-07-22', ride: 'Canmore Nordic all-day',  file: 'nordic-allday.gpx' },
  ];

  // Per-type record lists for the Records tab + By Activity cards.
  // scope 'window' = last 365 days; scope 'all' = all time.
  const RECORDS = {
    mtb: {
      window: [
        { label: 'Longest',  kind: 'dist',  value: 47.3, date: '2025-09-14', ride: 'Moose Mountain Epic',  file: 'moose-epic.gpx' },
        { label: 'Climbing', kind: 'elev',  value: 1840, date: '2026-05-02', ride: 'Powerline Grind',      file: 'powerline.gpx' },
        { label: 'Descent',  kind: 'elev',  value: 1910, date: '2026-05-02', ride: 'Powerline Grind',      file: 'powerline.gpx' },
        { label: 'Top speed',kind: 'speed', value: 58.4, date: '2026-05-30', ride: 'Mt 7 Psychosis',       file: 'mt7.gpx' },
        { label: 'Duration', kind: 'dur',   value: 15120,date: '2025-07-22', ride: 'Canmore Nordic all-day',file: 'nordic-allday.gpx' },
      ],
      all: [
        { label: 'Longest',  kind: 'dist',  value: 62.8, date: '2023-08-19', ride: 'TransRockies day 3',   file: 'trrockies3.gpx' },
        { label: 'Climbing', kind: 'elev',  value: 2240, date: '2024-07-06', ride: 'Three Sisters epic',   file: 'three-sisters.gpx' },
        { label: 'Descent',  kind: 'elev',  value: 2310, date: '2024-07-06', ride: 'Three Sisters epic',   file: 'three-sisters.gpx' },
        { label: 'Top speed',kind: 'speed', value: 61.9, date: '2024-09-01', ride: 'Mt 7 Psychosis',       file: 'mt7-2024.gpx' },
        { label: 'Duration', kind: 'dur',   value: 19980,date: '2023-08-19', ride: 'TransRockies day 3',   file: 'trrockies3.gpx' },
      ],
    },
    road: {
      window: [
        { label: 'Longest',  kind: 'dist',  value: 112.4,date: '2026-06-07', ride: 'Cochrane century',     file: 'cochrane.gpx' },
        { label: 'Climbing', kind: 'elev',  value: 1450, date: '2026-05-25', ride: 'Highwood loop',        file: 'highwood-loop.gpx' },
        { label: 'Top speed',kind: 'speed', value: 64.2, date: '2026-04-18', ride: 'Highwood Pass Descent', file: 'highwood.gpx' },
        { label: 'Duration', kind: 'dur',   value: 16500,date: '2026-06-07', ride: 'Cochrane century',     file: 'cochrane.gpx' },
      ],
      all: [
        { label: 'Longest',  kind: 'dist',  value: 164.0,date: '2024-06-22', ride: 'Banff gran fondo',     file: 'fondo.gpx' },
        { label: 'Climbing', kind: 'elev',  value: 2180, date: '2023-07-15', ride: 'Three passes',         file: 'three-passes.gpx' },
        { label: 'Top speed',kind: 'speed', value: 71.5, date: '2023-08-30', ride: 'Smith-Dorrien bomb',   file: 'smith.gpx' },
        { label: 'Duration', kind: 'dur',   value: 24300,date: '2024-06-22', ride: 'Banff gran fondo',     file: 'fondo.gpx' },
      ],
    },
    hike: {
      window: [
        { label: 'Longest',  kind: 'dist',  value: 18.6, date: '2025-08-03', ride: 'Tent Ridge Horseshoe', file: 'tent-ridge.gpx' },
        { label: 'Climbing', kind: 'elev',  value: 1320, date: '2025-09-20', ride: 'Ha Ling summit',       file: 'haling.gpx' },
        { label: 'Duration', kind: 'dur',   value: 21600,date: '2025-08-03', ride: 'Tent Ridge Horseshoe', file: 'tent-ridge.gpx' },
      ],
      all: [
        { label: 'Longest',  kind: 'dist',  value: 24.2, date: '2023-09-09', ride: 'Northover Ridge',      file: 'northover.gpx' },
        { label: 'Climbing', kind: 'elev',  value: 1680, date: '2023-09-09', ride: 'Northover Ridge',      file: 'northover.gpx' },
        { label: 'Duration', kind: 'dur',   value: 32400,date: '2023-09-09', ride: 'Northover Ridge',      file: 'northover.gpx' },
      ],
    },
    ski: {
      window: [
        { label: 'Vertical', kind: 'elev',  value: 1620, date: '2026-02-11', ride: 'Lake Louise pow day',  file: 'louise-pow.gpx' },
        { label: 'Longest',  kind: 'dist',  value: 8.9,  date: '2026-01-28', ride: 'Sunshine top-to-bottom',file: 'sunshine.gpx' },
        { label: 'Top speed',kind: 'speed', value: 71.0, date: '2026-02-11', ride: 'Lake Louise pow day',  file: 'louise-pow.gpx' },
        { label: 'Duration', kind: 'dur',   value: 9900, date: '2026-01-28', ride: 'Sunshine top-to-bottom',file: 'sunshine.gpx' },
      ],
      all: [
        { label: 'Vertical', kind: 'elev',  value: 1980, date: '2024-03-04', ride: 'Kicking Horse big day', file: 'kh.gpx' },
        { label: 'Longest',  kind: 'dist',  value: 11.2, date: '2024-03-04', ride: 'Kicking Horse big day', file: 'kh.gpx' },
        { label: 'Top speed',kind: 'speed', value: 78.3, date: '2023-02-18', ride: 'Norquay race day',      file: 'norquay.gpx' },
        { label: 'Duration', kind: 'dur',   value: 12600,date: '2024-03-04', ride: 'Kicking Horse big day', file: 'kh.gpx' },
      ],
    },
  };

  // Active-days calendar: dominant activity type per date over the last 365.
  // Deterministically generated so the ribbon/heatmap is stable.
  function buildActiveDates() {
    const r = rng(424242);
    const end = new Date('2026-06-23T00:00:00');
    const map = {};                 // iso → type id
    for (let i = 0; i < 365; i++) {
      const d = new Date(end);
      d.setDate(end.getDate() - i);
      const month = d.getMonth();   // 0-11
      // Pick a plausible dominant sport by season, then decide if active.
      let pool;
      if (month <= 2 || month === 11) pool = ['ski','ski','ski','mtb','road'];
      else if (month >= 5 && month <= 8) pool = ['mtb','mtb','road','road','hike'];
      else pool = ['mtb','road','hike','mtb','road'];
      const active = r() < 0.42;     // ~42% of days have an activity
      if (active) {
        const iso = d.toISOString().slice(0, 10);
        map[iso] = pool[Math.floor(r() * pool.length)];
      }
    }
    return map;
  }
  const DOMINANT_BY_DATE = buildActiveDates();

  root.MOCK = {
    TODAY, CUR_YEAR, CUR_MONTH, YEARS, MONTHS, MONTH_LETTERS,
    TYPES, TOTALS, ROLLUPS, HISTORY, HISTORY_ALL,
    RECENT_PRS, RECORDS, DOMINANT_BY_DATE, METRICS,
  };

  // ── Shared format helpers (so prototypes render numbers identically) ──────
  root.FMT = {
    dist(km)  { return km < 100 ? km.toFixed(1) : Math.round(km).toLocaleString(); },
    elev(m)   { return Math.round(m).toLocaleString(); },
    speed(k)  { return k.toFixed(1); },
    hours(h)  { const hr = Math.floor(h), mn = Math.round((h - hr) * 60); return `${hr}h ${String(mn).padStart(2,'0')}m`; },
    dur(sec)  { const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60); return h>0 ? `${h}h ${String(m).padStart(2,'0')}m` : `${m}m`; },
    dateShort(iso) { const d = new Date(iso + 'T00:00:00'); return d.toLocaleDateString('en-US',{month:'short',day:'numeric'}); },
    pct(cur, prev) { if (!prev) return null; return Math.round((cur - prev) / prev * 100); },
    // Format a record value by kind.
    record(rec) {
      switch (rec.kind) {
        case 'dist':  return this.dist(rec.value) + ' km';
        case 'elev':  return this.elev(rec.value) + ' m';
        case 'speed': return this.speed(rec.value) + ' km/h';
        case 'dur':   return this.dur(rec.value);
        default:      return String(rec.value);
      }
    },
  };
})(window);
