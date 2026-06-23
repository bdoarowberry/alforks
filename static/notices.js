// notices.js — one dismissible "notices" strip under the header, plus the gear
// dot. Loaded FIRST (before whatsnew.js / review_nav.js) so window.AlNotices
// exists when they call it. This is the single source of truth for "something
// needs your attention": each notice is a row with its own × dismiss, and the
// gear wears a dot whenever any notice is showing (dismiss = quiet).
(function () {
  var rows = {};       // id -> row element
  var strip = null;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function ensureStrip() {
    if (strip) return strip;
    var header = document.querySelector('header');
    if (!header) return null;
    strip = document.createElement('div');
    strip.className = 'notice-strip';
    strip.style.display = 'none';
    header.insertAdjacentElement('afterend', strip);
    return strip;
  }

  function syncChrome() {
    var any = Object.keys(rows).length > 0;
    if (strip) strip.style.display = any ? '' : 'none';
    var dot = document.querySelector('.nav-gear-dot');   // gear dot mirrors notices
    if (dot) dot.hidden = !any;
  }

  function removeRow(id, quiet) {
    var el = rows[id];
    if (el && el.parentNode) el.parentNode.removeChild(el);
    delete rows[id];
    if (!quiet) syncChrome();
  }

  // set(id, {icon, text, href, linkText, onDismiss}) — add or replace a notice.
  function set(id, opt) {
    var s = ensureStrip();
    if (!s) return;
    removeRow(id, true);
    var row = document.createElement('div');
    row.className = 'notice-row';
    row.dataset.id = id;
    var link = opt.href
      ? ' — <a class="nl" href="' + esc(opt.href) + '">' + esc(opt.linkText || 'Open') + ' →</a>'
      : '';
    row.innerHTML =
      '<span class="ndot"></span>' +
      '<span class="ntext">' + (opt.icon ? esc(opt.icon) + ' ' : '') + esc(opt.text) + link + '</span>' +
      '<button class="nx" type="button" title="Dismiss" aria-label="Dismiss">×</button>';
    row.querySelector('.nx').addEventListener('click', function () {
      if (typeof opt.onDismiss === 'function') { try { opt.onDismiss(); } catch (e) {} }
      removeRow(id);
    });
    s.appendChild(row);
    rows[id] = row;
    syncChrome();
  }

  window.AlNotices = { set: set, remove: function (id) { removeRow(id); } };
})();
