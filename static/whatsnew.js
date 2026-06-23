// What's New — loaded on every page via _head.html. Two jobs:
//   1. Inject a small "v<version>" chip into the header (links to /whatsnew),
//      mirroring upload.js's header-injection pattern so no template needs editing.
//   2. Show a one-time, dismissible banner after start.bat pulls a new version —
//      detected client-side by comparing the running version to a localStorage
//      "last seen" value (start.bat writes no sentinel the app can read).
(function () {
  var KEY = 'alforks_seen_version';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  var meta = document.querySelector('meta[name="alforks-version"]');
  var ver  = meta && meta.getAttribute('content');

  function injectChip() {
    var header = document.querySelector('header');
    if (!header || header.querySelector('.app-version')) return;
    var a = document.createElement('a');
    a.className = 'app-version';
    a.href = '/whatsnew';
    a.textContent = 'v' + ver;
    a.title = "What's new in AlForks";
    header.appendChild(a);   // .nav-sync-btn's margin-left:auto keeps this far-right
  }

  function showBanner() {
    if (document.querySelector('.whatsnew-banner')) return;
    var bar = document.createElement('div');
    bar.className = 'whatsnew-banner';
    bar.innerHTML =
      '<span>AlForks updated to <strong>v' + esc(ver) + '</strong>.</span>' +
      '<a href="/whatsnew">See what’s new →</a>' +
      '<button class="wn-dismiss" type="button" title="Dismiss" aria-label="Dismiss">×</button>';
    bar.querySelector('.wn-dismiss').addEventListener('click', function () {
      try { localStorage.setItem(KEY, ver); } catch (e) {}
      bar.remove();
    });
    document.body.insertBefore(bar, document.body.firstChild);
  }

  function run() {
    if (!ver || ver === 'dev') return;
    injectChip();

    var path = location.pathname.replace(/\/+$/, '') || '/';
    if (path === '/whatsnew') {                 // viewing the notes marks them read
      try { localStorage.setItem(KEY, ver); } catch (e) {}
      return;
    }
    var seen;
    try { seen = localStorage.getItem(KEY); } catch (e) { return; }
    if (seen === null) {                         // fresh install — not an "update"
      try { localStorage.setItem(KEY, ver); } catch (e) {}
      return;
    }
    if (seen !== ver) showBanner();              // version moved since last seen
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
})();
