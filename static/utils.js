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
