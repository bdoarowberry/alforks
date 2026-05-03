// Shared formatting utilities used across all pages.

function fmtDuration(sec, { showSeconds = false } = {}) {
  if (sec == null) return '--';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (showSeconds) return m > 0 ? `${m}m ${String(s).padStart(2, '0')}s` : `${s}s`;
  return `${m}m`;
}

function fmtDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-CA', { year: 'numeric', month: 'short', day: 'numeric' });
}

function fmtNum(n, d = 0) {
  return n != null ? n.toLocaleString('en-CA', { maximumFractionDigits: d, minimumFractionDigits: d }) : '--';
}

// Round-and-localize for elevation values. Null-safe so callers can pipe
// raw stats through without pre-checking.
function fmtElev(m) {
  if (m == null) return '—';
  return Math.round(m).toLocaleString();
}

// Escape user-supplied strings before interpolating into innerHTML
// templates. The five chars handled are sufficient for HTML body /
// attribute contexts; anything stricter (script context, etc.) needs a
// different escape policy.
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

// Activity-type abbreviation — exactly 3 characters, used as the badge
// text wherever activities are listed (Logs row, Compare picker, Setup
// swatch, Training totals, Summary, etc.). Accepts either a type object
// or a type id; if the type object carries an explicit `glyph` field
// (set on the Setup page), that wins. Otherwise falls back to a hand-
// tuned override map for built-in types, then to a 3-char uppercase
// slice of the id.
const TYPE_GLYPH_OVERRIDES = {
  mtb:        'MTB',
  snowboard:  'SNO',
  ski:        'SKI',
  hike:       'HIK',
  fat_biking: 'FAT',
  other:      'UNK',
};
function typeGlyph(input) {
  if (!input) return '?';
  if (typeof input === 'object') {
    if (input.glyph) return String(input.glyph).toUpperCase().slice(0, 3);
    return typeGlyph(input.id);
  }
  if (TYPE_GLYPH_OVERRIDES[input]) return TYPE_GLYPH_OVERRIDES[input];
  return String(input).replace(/_/g, '').slice(0, 3).toUpperCase();
}

// Open-Meteo weather code → emoji glyph. Used by the activity header
// weather strip and the per-row weather summary on /logs.
function weatherIcon(code) {
  if (code == null) return '';
  if (code === 0) return '☀';
  if (code <= 3)  return '⛅';
  if (code <= 48) return '🌫';
  if (code <= 67) return '🌧';
  if (code <= 77) return '❄';
  if (code <= 82) return '🌧';
  if (code <= 86) return '❄';
  return '⛈';
}
