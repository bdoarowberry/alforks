// Loaded by every page's nav. Fetches the lightweight review-counts summary
// and stamps the count onto the Review nav link's pill. Intentionally
// non-blocking — the pill stays hidden until the response arrives, so an
// unreachable backend doesn't leave a stale "0" on screen.
(function () {
  function update() {
    const pill = document.getElementById('nav-review-badge');
    if (!pill) return;
    fetch('/api/review-counts', { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (!d) return;
        const total = d.total || 0;
        if (total <= 0) { pill.style.display = 'none'; return; }
        pill.textContent = total > 99 ? '99+' : String(total);
        pill.style.display = '';
      })
      .catch(() => {});
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', update);
  } else {
    update();
  }
})();
