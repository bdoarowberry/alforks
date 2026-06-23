// notices.js — fills the header notifications bell (see _nav.html). Loaded FIRST
// (before whatsnew.js / review_nav.js) so window.AlNotices exists when they post
// to it. The bell is the single indicator for "something needs your attention":
// a count badge, and a dropdown panel of dismissible rows (each with its own ×).
(function () {
  var rows = {};   // id -> row element

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function container() { return document.querySelector('[data-bell-rows]'); }

  function syncChrome() {
    var n = Object.keys(rows).length;
    var badge = document.querySelector('.nav-bell-badge');
    if (badge) {
      badge.textContent = n > 9 ? '9+' : String(n);
      badge.hidden = n === 0;
    }
    var empty = document.querySelector('.nav-bell-empty');
    if (empty) empty.style.display = n === 0 ? '' : 'none';
  }

  function removeRow(id, quiet) {
    var el = rows[id];
    if (el && el.parentNode) el.parentNode.removeChild(el);
    delete rows[id];
    if (!quiet) syncChrome();
  }

  // set(id, {icon, text, href, linkText, onDismiss}) — add or replace a notice.
  function set(id, opt) {
    var box = container();
    if (!box) return;
    removeRow(id, true);
    var row = document.createElement('div');
    row.className = 'notice-row';
    row.dataset.id = id;
    var link = opt.href
      ? '<a class="nl" href="' + esc(opt.href) + '">' + esc(opt.linkText || 'Open') + ' →</a>'
      : '';
    row.innerHTML =
      '<span class="nicon">' + esc(opt.icon || '•') + '</span>' +
      '<span class="ntext">' + esc(opt.text) + (link ? '<br>' + link : '') + '</span>' +
      '<button class="nx" type="button" title="Dismiss" aria-label="Dismiss">×</button>';
    row.querySelector('.nx').addEventListener('click', function (e) {
      e.stopPropagation();
      if (typeof opt.onDismiss === 'function') { try { opt.onDismiss(); } catch (err) {} }
      removeRow(id);
    });
    box.appendChild(row);
    rows[id] = row;
    syncChrome();
  }

  window.AlNotices = { set: set, remove: function (id) { removeRow(id); } };

  // Set the initial empty state once the DOM is ready.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', syncChrome);
  } else {
    syncChrome();
  }
})();
