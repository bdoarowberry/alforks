// Thin top progress strip for "what's processing on app open". Surfaces, in
// priority order: the sidebar-cache build (the slow part of a cold first page
// load) and the trail-match prewarm (re-snapping rides against OSM, which runs
// at startup after a version bump). Polls the two status endpoints, fills
// done/total, then fades. Self-contained: drop the <script defer> on any page.
// Warm/idle pages stop polling within a few seconds.
(function () {
  const POLL_MS = 700;
  const MAX_IDLE_POLLS = 20;   // ~14s with no activity before giving up (warm cache)

  let el, bar, label, seenActive = false, idle = 0, timer = null, stopped = false;

  function ensureEl() {
    if (el) return;
    const css = document.createElement('style');
    css.textContent =
      '#proc-strip{position:fixed;top:0;left:0;right:0;height:3px;z-index:4000;' +
      'opacity:0;transition:opacity .3s;pointer-events:none;}' +
      '#proc-strip.on{opacity:1;}' +
      '#proc-strip .pf{height:100%;width:0;background:var(--accent,#3b82f6);' +
      'transition:width .3s ease;box-shadow:0 0 6px var(--accent,#3b82f6);}' +
      '#proc-label{position:fixed;top:8px;right:12px;z-index:4001;' +
      'font-family:var(--font-mono,monospace);font-size:.68rem;' +
      'color:var(--text-secondary,#9aa);background:rgba(10,12,12,.85);' +
      'border:1px solid rgba(255,255,255,.12);border-radius:4px;padding:3px 9px;' +
      'opacity:0;transition:opacity .3s;pointer-events:none;}' +
      '#proc-label.on{opacity:1;}';
    document.head.appendChild(css);
    el = document.createElement('div'); el.id = 'proc-strip';
    bar = document.createElement('div'); bar.className = 'pf';
    el.appendChild(bar);
    label = document.createElement('div'); label.id = 'proc-label';
    document.body.appendChild(el);
    document.body.appendChild(label);
  }

  function show(text, done, total) {
    ensureEl();
    el.classList.add('on'); label.classList.add('on');
    bar.style.width = (total > 0 ? Math.round((done / total) * 100) : 0) + '%';
    label.textContent = text;
  }
  function finish() {
    if (!el) return;
    bar.style.width = '100%';
    setTimeout(() => { el.classList.remove('on'); label.classList.remove('on'); }, 450);
  }
  function hide() { if (el) { el.classList.remove('on'); label.classList.remove('on'); } }

  function schedule(ms) { clearTimeout(timer); timer = setTimeout(tick, ms); }

  async function status(url) {
    try { return await (await fetch(url, { cache: 'no-store' })).json(); }
    catch (_e) { return null; }
  }

  async function tick() {
    if (stopped) return;
    // 1) Sidebar build — highest priority (it blocks the page's own data).
    const b = await status('/api/activities/build-status');
    if (b && b.running && b.total > 0) {
      seenActive = true; idle = 0;
      show(`Preparing ${b.done}/${b.total} rides…`, b.done, b.total);
      return schedule(POLL_MS);
    }
    // 2) Trail-match prewarm — re-snapping rides against OSM at startup.
    const t = await status('/api/trail-match/status');
    if (t && t.running && t.total > 0) {
      seenActive = true; idle = 0;
      show(`Matching trails ${t.done}/${t.total}…`, t.done, t.total);
      return schedule(POLL_MS);
    }
    // Nothing active.
    if (seenActive) { stopped = true; return finish(); }   // finished → fill + fade
    if (++idle <= MAX_IDLE_POLLS) return schedule(POLL_MS); // may start once the page fetch fires
    stopped = true; hide();                                 // warm, nothing to show → stop
  }

  // Loaded with `defer`, so the DOM is parsed by the time this runs.
  tick();
})();
