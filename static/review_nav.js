// Loaded by every page's nav. Fetches the lightweight review-counts summary
// and stamps the count onto the Review nav link's pill. Intentionally
// non-blocking — the pill stays hidden until the response arrives, so an
// unreachable backend doesn't leave a stale "0" on screen.
(function () {
  function update() {
    const pill = document.getElementById('nav-review-badge');
    if (!pill) return;
    // Clean-up lives inside the collapsed gear menu now, so also flag the gear
    // button with a dot when there are items to resolve.
    const dot = document.querySelector('.nav-gear-dot');
    const gearBtn = document.querySelector('.nav-gear-btn');
    fetch('/api/review-counts', { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (!d) return;
        const total = d.total || 0;
        if (total <= 0) {
          pill.style.display = 'none';
          if (dot) dot.hidden = true;
          return;
        }
        pill.textContent = total > 99 ? '99+' : String(total);
        pill.style.display = '';
        if (dot) dot.hidden = false;
        if (gearBtn) gearBtn.title = total + ' item' + (total === 1 ? '' : 's') + ' to clean up — Settings, help & more';
      })
      .catch(() => {});
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', update);
  } else {
    update();
  }
})();
