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
