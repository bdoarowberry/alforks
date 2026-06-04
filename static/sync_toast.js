// Bottom-right toast that surfaces background sync progress on every page.
// Polls /api/sync/status — only paints when a sync is active or recently
// finished (so quiet pages stay visually quiet).

(function () {
  const TOAST_ID = 'al-sync-toast';
  const POLL_MS_ACTIVE = 2000;
  const POLL_MS_IDLE   = 15000;
  const HIDE_AFTER_MS  = 6000;
  // Persist across page navigations so a finished sync only surfaces once.
  let lastSeenFinish;
  try {
    lastSeenFinish = JSON.parse(sessionStorage.getItem('alSyncSeen')) || { strava: 0, garmin: 0 };
  } catch (e) { lastSeenFinish = { strava: 0, garmin: 0 }; }
  function persistSeen() {
    try { sessionStorage.setItem('alSyncSeen', JSON.stringify(lastSeenFinish)); } catch (e) {}
  }
  // Don't surface stale finishes when landing on a page hours after the sync.
  const FRESH_WINDOW_SEC = 60;
  let hideTimer = null;
  // Single tracked handle for the self-rescheduling poll chain, so triggerSyncAll()
  // reschedules the one loop instead of forking a second (compounding) chain.
  let pollTimer = null;
  function schedule(delay) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(tick, delay);
  }

  function ensureContainer() {
    let el = document.getElementById(TOAST_ID);
    if (el) return el;
    el = document.createElement('div');
    el.id = TOAST_ID;
    el.style.cssText = `
      position: fixed; right: 18px; bottom: 18px; z-index: 99999;
      background: #12151a; border: 1px solid #2a2d35; border-radius: 8px;
      padding: 12px 16px; min-width: 240px; max-width: 360px;
      box-shadow: 0 6px 24px rgba(0,0,0,0.5);
      font-family: system-ui, -apple-system, sans-serif;
      font-size: 0.78rem; color: #e0e0e0; display: none;
      transition: opacity 0.2s; opacity: 0;
    `;
    document.body.appendChild(el);
    return el;
  }

  // sync messages come from subprocess output (app.py reads the last stderr/stdout
  // line); escape before interpolating into innerHTML so markup can't break the
  // toast. This IIFE doesn't import utils.js, so inline a tiny escaper.
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function rowHtml(name, st) {
    let badge = '';
    let extra = '';
    if (st.running) {
      badge = '<span style="color:#f59e0b">⟳ syncing</span>';
      extra = st.message ? `<div style="color:#888;margin-top:2px">${esc(st.message)}</div>` : '';
    } else if (st.ok === true) {
      badge = '<span style="color:#22c55e">✓</span>';
      extra = `<div style="color:#888;margin-top:2px">${esc(st.message || 'done')}</div>`;
    } else if (st.ok === false) {
      badge = '<span style="color:#ef4444">✗</span>';
      extra = `<div style="color:#ef4444;margin-top:2px">${esc(st.message || 'error')}</div>`;
    } else {
      return '';
    }
    return `<div style="margin-bottom:8px"><strong style="color:#fff">${esc(name)}</strong> ${badge}${extra}</div>`;
  }

  function show(html) {
    const el = ensureContainer();
    el.innerHTML = html;
    el.style.display = 'block';
    requestAnimationFrame(() => { el.style.opacity = '1'; });
    clearTimeout(hideTimer);
  }

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      const el = document.getElementById(TOAST_ID);
      if (!el) return;
      el.style.opacity = '0';
      setTimeout(() => { el.style.display = 'none'; }, 250);
    }, HIDE_AFTER_MS);
  }

  async function tick() {
    let nextDelay = POLL_MS_IDLE;
    try {
      const r = await fetch('/api/sync/status');
      const s = await r.json();
      const nowSec = Date.now() / 1000;
      const anyRunning = s.strava.running || s.garmin.running;
      // Show a finished sync only if (a) we haven't surfaced this run yet AND
      // (b) it actually finished recently — prevents the toast from popping up
      // on every page navigation hours after the sync completed.
      const showStrava = s.strava.running || (s.strava.finished_at && s.strava.finished_at > lastSeenFinish.strava && (nowSec - s.strava.finished_at) < FRESH_WINDOW_SEC);
      const showGarmin = s.garmin.running || (s.garmin.finished_at && s.garmin.finished_at > lastSeenFinish.garmin && (nowSec - s.garmin.finished_at) < FRESH_WINDOW_SEC);
      if (showStrava || showGarmin) {
        let html = '';
        if (showStrava) html += rowHtml('Strava', s.strava);
        if (showGarmin) html += rowHtml('Garmin', s.garmin);
        if (html) show(html);
        if (!anyRunning) {
          // Finished — note the timestamps so we don't re-show the same run
          if (s.strava.finished_at) lastSeenFinish.strava = s.strava.finished_at;
          if (s.garmin.finished_at) lastSeenFinish.garmin = s.garmin.finished_at;
          persistSeen();
          scheduleHide();
        }
      }
      nextDelay = anyRunning ? POLL_MS_ACTIVE : POLL_MS_IDLE;
    } catch (e) {
      // server probably restarting — fail quiet and try again later
    }
    schedule(nextDelay);
  }

  // First check kicks off shortly after page load
  schedule(1000);

  // Global helper: any page can call window.triggerSyncAll() to kick off a
  // Strava+Garmin sync without leaving its current view. The toast then
  // surfaces progress and result. Returns the parsed response from
  // /api/sync/all so callers can see which sources actually started.
  window.triggerSyncAll = async function () {
    try {
      const resp = await fetch('/api/sync/all', { method: 'POST' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      // Show an immediate "syncing" hint even before the next poll fires.
      const started = (data.started || []);
      if (started.length) {
        show(started.map(s => rowHtml(
          s === 'strava' ? 'Strava' : 'Garmin',
          { running: true, message: 'starting…' }
        )).join(''));
        // Reset hide timer; the regular poll will take over.
        clearTimeout(hideTimer);
        // Poll faster while we expect activity to start (reschedules the single
        // loop — does not fork a new chain).
        schedule(500);
      } else {
        show('<div style="color:#888">Sync already running.</div>');
        scheduleHide();
      }
      return data;
    } catch (e) {
      show('<div style="color:#ef4444">Sync trigger failed: ' + esc(e.message) + '</div>');
      scheduleHide();
      return null;
    }
  };
})();
