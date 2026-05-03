// Mock GPX activity data + shared helpers.
// Multi-year history (2022-2026) so YoY comparisons are meaningful.

const ACTIVITY_DEFS = {
  fatbike: {
    id: 'fatbike',
    label: 'Fat Bike',
    short: 'FAT',
    accent: 'oklch(0.74 0.14 55)',
    accentSoft: 'oklch(0.74 0.14 55 / 0.18)',
    glyph: 'F',
    season: 'Winter',
    // Metrics this activity actually cares about. Order = display priority.
    metrics: ['days', 'distance', 'ascent', 'moving'],
    // Charts the user can toggle between
    chartMetrics: [
      { key: 'count',    label: 'Activities' },
      { key: 'distance', label: 'Distance' },
      { key: 'duration', label: 'Duration' },
      { key: 'ascent',   label: 'Ascent' },
    ],
  },
  mtb: {
    id: 'mtb',
    label: 'Mountain Bike',
    short: 'MTB',
    accent: 'oklch(0.78 0.15 145)',
    accentSoft: 'oklch(0.78 0.15 145 / 0.18)',
    glyph: 'M',
    season: 'Summer',
    metrics: ['days', 'distance', 'ascent', 'moving'],
    chartMetrics: [
      { key: 'count',    label: 'Activities' },
      { key: 'distance', label: 'Distance' },
      { key: 'duration', label: 'Duration' },
      { key: 'ascent',   label: 'Ascent' },
    ],
  },
  snow: {
    id: 'snow',
    label: 'Snowboard',
    short: 'SNB',
    accent: 'oklch(0.78 0.13 230)',
    accentSoft: 'oklch(0.78 0.13 230 / 0.18)',
    glyph: 'S',
    season: 'Winter',
    // Snowboard: descent matters, ascent does not.
    metrics: ['days', 'descent', 'distance', 'moving'],
    chartMetrics: [
      { key: 'count',    label: 'Activities' },
      { key: 'descent',  label: 'Vertical' },
      { key: 'duration', label: 'Duration' },
      { key: 'distance', label: 'Distance' },
    ],
  },
};

const ACTIVITY_ORDER = ['fatbike', 'mtb', 'snow'];
const TODAY = '2026-04-27';
const YEARS = [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026];
const MONTH_LABELS = ['J','F','M','A','M','J','J','A','S','O','N','D'];

// Per-activity rolling-365d rollup (from 2025-04-28 to 2026-04-27).
// Realistic for a weekend warrior. PRs span multiple seasons.
const ROLLUPS = {
  fatbike: {
    days: 32, distance_km: 348.2, elev_gain_m: 4120, elev_loss_m: 4080,
    moving_h: 35.4, avg_speed_kmh: 12.8, max_speed_kmh: 38.4,
    avg_hr: 142, max_hr: 178, avg_power_w: 168,
    peak_altitude_m: 1842,
    longest_streak: 7, current_streak: 0, last_date: '2026-03-22',
    activity_count: 32,
    prs: [
      { label: 'Longest',   value: '38.2 km',  loc: 'Birch Loop',     date: '2026-01-18' },
      { label: 'Climbing',  value: '612 m',    loc: 'Hidden Valley',  date: '2026-02-09' },
      { label: 'Descent',   value: '598 m',    loc: 'Hidden Valley',  date: '2026-02-09' },
      { label: 'Top speed', value: '38.4 km/h',loc: 'Powerline O&B',  date: '2026-03-15' },
      { label: 'Duration',  value: '3h 18m',   loc: 'Birch Loop x2',  date: '2026-01-18' },
    ],
    weather: {
      hottest: { temp_c: 4,  date: '2025-11-08', loc: 'Birch Loop' },
      coldest: { temp_c: -28,date: '2026-01-18', loc: 'Birch Loop' },
    },
  },
  mtb: {
    days: 48, distance_km: 1184.6, elev_gain_m: 16920, elev_loss_m: 17010,
    moving_h: 72.4, avg_speed_kmh: 16.4, max_speed_kmh: 54.7,
    avg_hr: 156, max_hr: 188, avg_power_w: 214,
    peak_altitude_m: 2732,
    longest_streak: 11, current_streak: 2, last_date: '2026-04-25',
    activity_count: 233,
    prs: [
      { label: 'Longest',   value: '68.95 km', loc: 'Kicking Horse Bike Park', date: '2025-08-12' },
      { label: 'Climbing',  value: '3,948 m',  loc: 'Powderedtoast Ride',      date: '2025-09-03' },
      { label: 'Descent',   value: '8,019 m',  loc: 'Kicking Horse Bike Park', date: '2025-08-12' },
      { label: 'Top speed', value: '54.7 km/h',loc: 'West Bragg Creek',        date: '2025-08-22' },
      { label: 'Duration',  value: '6h 31m',   loc: 'Ridelog',                 date: '2025-08-12' },
    ],
    weather: {
      hottest: { temp_c: 36, date: '2025-07-21', loc: 'West Bragg Creek' },
      coldest: { temp_c: -2, date: '2025-11-04', loc: 'Bradbury Mtn' },
    },
  },
  snow: {
    days: 22, distance_km: 168.4, elev_gain_m: 220, elev_loss_m: 26840,
    moving_h: 18.2, avg_speed_kmh: 24.6, max_speed_kmh: 71.2,
    avg_hr: 128, max_hr: 164, avg_power_w: null,
    peak_altitude_m: 2542,
    longest_streak: 4, current_streak: 0, last_date: '2026-03-29',
    activity_count: 60,
    // Snow-season specifics
    lift_rides: 180,
    avg_lifts_per_day: 10,
    biggest_day_m: 7045,
    biggest_day_date: '2026-03-12',
    biggest_day_loc: 'Sunshine Village',
    most_lifts: 20,
    most_lifts_date: '2023-03-18',
    biggest_run_m: 1180,
    biggest_run_date: '2026-02-14',
    biggest_run_loc: 'Sunshine Village',
    season_start: '2025-12-14',
    season_end:   '2026-03-29',
    prs: [
      { label: 'Longest',   value: '59.47 km', loc: 'Sunshine Village', date: '2026-02-14' },
      { label: 'Climbing',  value: '1,776 m',  loc: 'Lake Louise',      date: '2026-02-21' },
      { label: 'Descent',   value: '9,832 m',  loc: 'Sunshine Village', date: '2026-03-12' },
      { label: 'Top speed', value: '109.2 km/h',loc: 'Sunshine Village',date: '2026-01-28' },
      { label: 'Duration',  value: '6h 59m',   loc: 'Sunshine Village', date: '2026-02-14' },
    ],
    weather: {
      hottest: { temp_c: 8,  date: '2026-03-29', loc: 'Sunshine Village' },
      coldest: { temp_c: -32,date: '2026-01-12', loc: 'Lake Louise' },
    },
  },
};

// Lifetime totals (since first activity 2019-08-13). Mirrors the screenshot.
const LIFETIME = {
  since: '2019-08-13',
  activities: 669,
  active_days: 480,
  distance_km: 11621.5,
  climbing_m: 384918,
  descent_m: 701461,
  descent_self_powered_m: 462624,   // i.e. 701,461 - 238,837 (assisted)
  descent_assisted_m: 238837,
  moving_h: 1440 + 34/60,
  peak_altitude_m: 2732,
  longest_streak: 4,
  current_streak: 0,
  untagged_count: 608,
  untagged_km: 10321,
  untagged_share: 0.91,
};

// Records · Overall (across all activity types, lifetime)
const OVERALL_PRS = [
  { label: 'Longest ride',    value: '71.62 km',     loc: 'Ridelog',          date: '2025-08-12', type: 'mtb'  },
  { label: 'Most climbing',   value: '4,625 m',      loc: 'Ridelog',          date: '2025-09-03', type: 'mtb'  },
  { label: 'Most descent',    value: '9,832 m',      loc: 'Sunshine Village', date: '2026-03-12', type: 'snow' },
  { label: 'Fastest max speed',value: '109.2 km/h',  loc: 'Sunshine Village', date: '2026-01-28', type: 'snow' },
  { label: 'Longest duration',value: '8h 57m',       loc: 'Ridelog',          date: '2025-08-12', type: 'mtb'  },
  { label: 'Hottest ride',    value: '36 °C',        loc: 'West Bragg Creek', date: '2025-07-21', type: 'mtb'  },
  { label: 'Coldest ride',    value: '-32 °C',       loc: 'Lake Louise',      date: '2026-01-12', type: 'snow' },
  { label: 'Highest point',   value: '2,732 m',      loc: 'Kicking Horse',    date: '2025-08-12', type: 'mtb'  },
];

// Per-month averages (1..12) of activity DAYS — for "Activity days per month" chart in screenshot.
// Computed across ALL years of HISTORY at runtime, but pre-compute pattern here.
// Will be filled after HISTORY is built.
const ACTIVITY_DAYS_PER_MONTH = new Array(12).fill(0);

// Fitness training — last 12 weeks (2026-02-09 .. 2026-04-27)
// Each row: { wk: 'MM-DD', volume_h, climbing_m, hr_zones: [z1..z5] (h), z2_avg_28d_bpm }
const FITNESS_WEEKS = [
  { wk: '02-09', volume_h: 2.4, climbing_m: 680, hr_zones: [0.5, 1.4, 0.4, 0.1, 0.0], z2_avg: 134 },
  { wk: '02-16', volume_h: 7.8, climbing_m: 580, hr_zones: [1.0, 5.6, 1.0, 0.2, 0.0], z2_avg: 112 },
  { wk: '02-23', volume_h: 5.6, climbing_m: 240, hr_zones: [0.7, 4.2, 0.6, 0.1, 0.0], z2_avg: 110 },
  { wk: '03-02', volume_h: 9.6, climbing_m: 600, hr_zones: [1.2, 7.2, 1.0, 0.2, 0.0], z2_avg: 109 },
  { wk: '03-09', volume_h: 4.8, climbing_m: 580, hr_zones: [0.6, 3.6, 0.5, 0.1, 0.0], z2_avg: 110 },
  { wk: '03-16', volume_h: 0.0, climbing_m:   0, hr_zones: [0.0, 0.0, 0.0, 0.0, 0.0], z2_avg: 110 },
  { wk: '03-23', volume_h: 8.0, climbing_m: 580, hr_zones: [1.0, 6.0, 0.8, 0.2, 0.0], z2_avg: 112 },
  { wk: '03-30', volume_h: 7.4, climbing_m: 580, hr_zones: [0.9, 5.6, 0.7, 0.2, 0.0], z2_avg: 116 },
  { wk: '04-06', volume_h: 5.2, climbing_m: 240, hr_zones: [0.7, 3.8, 0.6, 0.1, 0.0], z2_avg: 118 },
  { wk: '04-13', volume_h: 3.4, climbing_m: 580, hr_zones: [0.4, 2.6, 0.4, 0.0, 0.0], z2_avg: 116 },
  { wk: '04-20', volume_h: 2.6, climbing_m: 600, hr_zones: [0.3, 2.0, 0.3, 0.0, 0.0], z2_avg: 114 },
  { wk: '04-27', volume_h: 0.6, climbing_m:  60, hr_zones: [0.1, 0.4, 0.1, 0.0, 0.0], z2_avg: 109 },
];

const HR_ZONE_COLORS = [
  'oklch(0.6 0.02 240)',   // Z1 grey
  'oklch(0.78 0.13 230)',  // Z2 blue
  'oklch(0.78 0.15 145)',  // Z3 green
  'oklch(0.82 0.16 80)',   // Z4 yellow
  'oklch(0.7 0.2 25)',     // Z5 red
];
const HR_ZONE_LABELS = ['Z1 Recovery','Z2 Endurance','Z3 Tempo','Z4 Threshold','Z5 VO2max'];

// Multi-year monthly history. Shape: HISTORY[activity][year] = { count[], dist_km[], dur_h[], asc_m[], desc_m[] } (12 months)
// Patterns reflect: MTB peaks summer, fatbike+snow peak winter, with realistic variation across years.
function makeYearSeries(seed, base, seasonality) {
  // base = max monthly value, seasonality = array of 12 multipliers 0..1
  const out = [];
  let s = seed;
  for (let i = 0; i < 12; i++) {
    s = (s * 9301 + 49297) % 233280;
    const noise = 0.7 + (s / 233280) * 0.6;
    out.push(Math.max(0, Math.round(base * seasonality[i] * noise * 10) / 10));
  }
  return out;
}

const SEASONS = {
  // Mar..Feb in MONTH_LABELS terms (Jan..Dec) — index 0 = Jan
  fatbike: [1.0, 0.85, 0.4, 0.05, 0,    0,    0,    0,    0,    0.2,  0.7,  1.0],
  mtb:     [0,   0,    0.3, 0.7,  0.95, 1.0,  0.95, 0.85, 0.6,  0.25, 0.05, 0],
  snow:    [0.9, 1.0,  0.55,0.05, 0,    0,    0,    0,    0,    0,    0.3,  0.7],
};

// Per-activity baselines (count / dist / dur / asc / desc per peak month)
const BASELINES = {
  fatbike: { count: 12, dist: 95, dur: 9,  asc: 1100, desc: 1100 },
  mtb:     { count: 11, dist: 200,dur: 13, asc: 2800, desc: 2800 },
  snow:    { count: 8,  dist: 28, dur: 3,  asc: 80,   desc: 4400 },
};

// Per-year activity drift, mirroring the YoY pattern in the screenshot:
// 2019 ramp-up, peak 2020-22, dip mid-decade, partial recovery.
const YEAR_FACTOR = {
  2019: 0.30, 2020: 1.10, 2021: 1.20, 2022: 1.05,
  2023: 0.75, 2024: 0.95, 2025: 0.55, 2026: 0.30,
};

const HISTORY = {};
ACTIVITY_ORDER.forEach((k, ai) => {
  HISTORY[k] = {};
  YEARS.forEach((y, yi) => {
    const f = YEAR_FACTOR[y];
    const seedBase = (ai + 1) * 100 + yi * 7;
    const b = BASELINES[k];
    const s = SEASONS[k];
    HISTORY[k][y] = {
      count:    makeYearSeries(seedBase + 1, b.count * f, s).map(v => Math.round(v)),
      distance: makeYearSeries(seedBase + 2, b.dist * f, s),
      duration: makeYearSeries(seedBase + 3, b.dur * f, s),
      ascent:   makeYearSeries(seedBase + 4, b.asc * f, s).map(v => Math.round(v)),
      descent:  makeYearSeries(seedBase + 5, b.desc * f, s).map(v => Math.round(v)),
    };
  });
});

// 2019 only goes from Aug onward (first activity 2019-08-13).
// Future months in 2026 = null.
YEARS.forEach(y => {
  ACTIVITY_ORDER.forEach(k => {
    ['count','distance','duration','ascent','descent'].forEach(m => {
      if (y === 2019) for (let i = 0; i < 7;  i++) HISTORY[k][y][m][i] = null;
      if (y === 2026) for (let i = 4; i < 12; i++) HISTORY[k][y][m][i] = null;
    });
  });
});

// Per-month average activity days, across all years of HISTORY.
// (Sum counts across years/activities → average per month.)
for (let mi = 0; mi < 12; mi++) {
  let total = 0, n = 0;
  YEARS.forEach(y => {
    let monthSum = 0;
    let any = false;
    ACTIVITY_ORDER.forEach(k => {
      const v = HISTORY[k][y].count[mi];
      if (v != null) { monthSum += v; any = true; }
    });
    if (any) { total += monthSum; n++; }
  });
  ACTIVITY_DAYS_PER_MONTH[mi] = n ? total / n : 0;
}

// Year-over-year aggregates (for the YoY chart strip in the screenshot)
const YOY_AGG = {
  days:     YEARS.map(y => 0),
  distance: YEARS.map(y => 0),  // km, excl. lifts (i.e. not snowboard descent helpers)
  climbing: YEARS.map(y => 0),  // m, self-powered
  duration: YEARS.map(y => 0),  // hours
};
YEARS.forEach((y, yi) => {
  ACTIVITY_ORDER.forEach(k => {
    const h = HISTORY[k][y];
    h.count.forEach(v => { if (v) YOY_AGG.days[yi] += v; });
    h.distance.forEach(v => { if (v) YOY_AGG.distance[yi] += v; });
    // For climbing: include MTB + fatbike ascent; exclude snowboard ascent (lifts)
    if (k !== 'snow') h.ascent.forEach(v => { if (v) YOY_AGG.climbing[yi] += v; });
    h.duration.forEach(v => { if (v) YOY_AGG.duration[yi] += v; });
  });
});

// Build a year-grid (week × weekday) of activity days for one year.
// Returns { weeks: [{ weekIdx, days: [{ iso, type, on }] }], monthBoundaries: [{ weekIdx, label }] }
function buildCalendarYear(year, daysSet, dominant) {
  const start = new Date(`${year}-01-01T00:00:00`);
  const end = new Date(`${year}-12-31T00:00:00`);
  // Find Sunday on/before Jan 1
  const cursor = new Date(start);
  cursor.setDate(cursor.getDate() - cursor.getDay());

  const weeks = [];
  const monthBoundaries = [];
  let lastMonth = -1;

  while (cursor <= end) {
    const days = [];
    for (let d = 0; d < 7; d++) {
      const iso = cursor.toISOString().slice(0, 10);
      const inYear = cursor.getFullYear() === year;
      const on = inYear && daysSet && daysSet.has(iso);
      const type = on ? dominant.get(iso) : null;
      days.push({ iso, inYear, on, type, dow: d });
      if (inYear && cursor.getMonth() !== lastMonth) {
        lastMonth = cursor.getMonth();
        monthBoundaries.push({ weekIdx: weeks.length, label: MONTH_LABELS[lastMonth] });
      }
      cursor.setDate(cursor.getDate() + 1);
    }
    weeks.push({ weekIdx: weeks.length, days });
  }
  return { weeks, monthBoundaries };
}

// Recent activity log (last ~year, 12 entries across activities)
const RECENT = [
  { id: 'a1',  type: 'mtb',     date: '2026-04-25', name: 'Pleasant Mtn Loop',   dist: 22.4, elev: 412, dur: '2:08', avg: 10.5, max: 38.2, hr: 152 },
  { id: 'a2',  type: 'mtb',     date: '2026-04-19', name: 'Bradbury Mtn',        dist: 18.2, elev: 320, dur: '1:48', avg: 10.1, max: 36.4, hr: 148 },
  { id: 'a3',  type: 'mtb',     date: '2026-04-12', name: 'Carter Hill Out',     dist: 24.6, elev: 488, dur: '2:24', avg: 10.3, max: 41.0, hr: 154 },
  { id: 'a4',  type: 'snow',    date: '2026-03-29', name: 'Sugarloaf Closing',   dist: 12.8, elev:  20, dur: '1:02', avg: 22.4, max: 64.1, hr: 124 },
  { id: 'a5',  type: 'fatbike', date: '2026-03-22', name: 'Birch Loop',          dist: 18.4, elev: 220, dur: '1:42', avg: 10.8, max: 28.2, hr: 138 },
  { id: 'a6',  type: 'fatbike', date: '2026-03-15', name: 'Powerline O&B',       dist: 24.6, elev: 312, dur: '2:18', avg: 11.2, max: 32.0, hr: 144 },
  { id: 'a7',  type: 'snow',    date: '2026-03-08', name: 'Saddleback East',     dist:  9.2, elev:  22, dur: '0:48', avg: 21.8, max: 58.6, hr: 122 },
  { id: 'a8',  type: 'snow',    date: '2026-02-21', name: 'Sunday River AM',     dist:  9.2, elev:  20, dur: '0:48', avg: 22.4, max: 64.1, hr: 124 },
  { id: 'a9',  type: 'snow',    date: '2026-02-14', name: 'Sugarloaf Bell-Bell', dist: 14.8, elev:  18, dur: '1:12', avg: 26.1, max: 71.2, hr: 132 },
  { id: 'a10', type: 'fatbike', date: '2026-02-09', name: 'Hidden Valley',       dist: 14.2, elev: 198, dur: '1:24', avg: 11.0, max: 26.4, hr: 140 },
  { id: 'a11', type: 'fatbike', date: '2026-02-01', name: 'Trestle Trail',       dist: 22.1, elev: 268, dur: '2:04', avg: 11.4, max: 30.8, hr: 142 },
  { id: 'a12', type: 'fatbike', date: '2026-01-18', name: 'Birch Loop x2',       dist: 38.2, elev: 440, dur: '3:18', avg: 11.2, max: 28.6, hr: 146 },
];

// Helpers
function daysBetween(a, b) {
  const d1 = new Date(a + 'T00:00:00');
  const d2 = new Date(b + 'T00:00:00');
  return Math.round((d2 - d1) / (1000 * 60 * 60 * 24));
}

function totalRollup() {
  const acts = ACTIVITY_ORDER.map(k => ROLLUPS[k]);
  const lastDate = acts.map(a => a.last_date).sort().pop();
  return {
    days: acts.reduce((s, a) => s + a.days, 0),
    elev_gain_m: acts.reduce((s, a) => s + a.elev_gain_m, 0),
    elev_loss_m: acts.reduce((s, a) => s + a.elev_loss_m, 0),
    moving_h: acts.reduce((s, a) => s + a.moving_h, 0),
    longest_streak: Math.max(...acts.map(a => a.longest_streak)),
    current_streak: Math.max(...acts.map(a => a.current_streak)),
    last_date: lastDate,
    days_since: daysBetween(lastDate, TODAY),
  };
}

// Build a Set of dates with activity, restricted to a window (rolling N days back from TODAY).
// Synthesizes from HISTORY[k][year].count distributed across each month.
function buildActiveDays(daysBack = 365) {
  const set = new Set(RECENT.map(r => r.date));
  const dominant = new Map();
  RECENT.forEach(r => dominant.set(r.date, r.type));

  const today = new Date(TODAY + 'T00:00:00');
  const earliest = new Date(today); earliest.setDate(today.getDate() - daysBack + 1);

  ACTIVITY_ORDER.forEach((k, ai) => {
    YEARS.forEach(y => {
      HISTORY[k][y].count.forEach((cnt, mi) => {
        if (!cnt) return;
        for (let i = 0; i < cnt; i++) {
          // Spread across the month, deterministically
          const day = ((i * 7 + ai * 3 + mi * 5 + y) % 27) + 2;
          const iso = `${y}-${String(mi+1).padStart(2,'0')}-${String(day).padStart(2,'0')}`;
          const d = new Date(iso + 'T00:00:00');
          if (d >= earliest && d <= today) {
            set.add(iso);
            if (!dominant.has(iso)) dominant.set(iso, k);
          }
        }
      });
    });
  });
  return { set, dominant };
}

// Unit helpers
function fmtDist(km, units) {
  if (units === 'imperial') return (km * 0.621371).toFixed(km < 100 ? 1 : 0);
  return km < 100 ? km.toFixed(1) : Math.round(km).toLocaleString();
}
function distUnit(units) { return units === 'imperial' ? 'mi' : 'km'; }
function fmtElev(m, units) {
  if (units === 'imperial') return Math.round(m * 3.28084).toLocaleString();
  return Math.round(m).toLocaleString();
}
function elevUnit(units) { return units === 'imperial' ? 'ft' : 'm'; }
function fmtSpeed(kmh, units) {
  if (units === 'imperial') return (kmh * 0.621371).toFixed(1);
  return kmh.toFixed(1);
}
function speedUnit(units) { return units === 'imperial' ? 'mph' : 'km/h'; }
function fmtHours(h) {
  const hr = Math.floor(h);
  const mn = Math.round((h - hr) * 60);
  return `${hr}h ${String(mn).padStart(2, '0')}m`;
}
function fmtDate(iso) {
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}
function fmtMetric(value, metricKey, units) {
  if (value == null) return '—';
  switch (metricKey) {
    case 'count':    return value.toString();
    case 'distance': return `${fmtDist(value, units)} ${distUnit(units)}`;
    case 'duration': return `${value.toFixed(1)} h`;
    case 'ascent':
    case 'descent':  return `${fmtElev(value, units)} ${elevUnit(units)}`;
    default:         return String(value);
  }
}

Object.assign(window, {
  ACTIVITY_DEFS, ACTIVITY_ORDER, ROLLUPS, RECENT, HISTORY, YEARS, MONTH_LABELS,
  TODAY, totalRollup, buildActiveDays, buildCalendarYear, daysBetween,
  LIFETIME, OVERALL_PRS, ACTIVITY_DAYS_PER_MONTH, YOY_AGG,
  FITNESS_WEEKS, HR_ZONE_COLORS, HR_ZONE_LABELS,
  fmtDist, distUnit, fmtElev, elevUnit, fmtSpeed, speedUnit, fmtHours, fmtDate, fmtMetric,
});
