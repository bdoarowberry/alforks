// What's New — loaded on every page via _head.html. When start.bat pulls a new
// version, surface a single dismissible "updated to vX" notice in the shared
// notices strip (see notices.js) that links to /whatsnew. Detected client-side
// by comparing the running version to a localStorage "last seen" value.
// (The old always-visible version chip + full-width banner were retired; the
// version now lives in the gear menu, and What's New is a gear-menu link.)
(function () {
  var KEY = 'alforks_seen_version';

  var meta = document.querySelector('meta[name="alforks-version"]');
  var ver = meta && meta.getAttribute('content');

  function markSeen() { try { localStorage.setItem(KEY, ver); } catch (e) {} }

  function run() {
    if (!ver || ver === 'dev') return;

    var path = location.pathname.replace(/\/+$/, '') || '/';
    if (path === '/whatsnew') { markSeen(); return; }   // viewing notes marks read

    var seen;
    try { seen = localStorage.getItem(KEY); } catch (e) { return; }
    if (seen === null) { markSeen(); return; }            // fresh install — not an update
    if (seen === ver) return;                             // already current

    // Version moved since last seen → post one dismissible update notice.
    if (window.AlNotices) {
      window.AlNotices.set('update', {
        icon: '✨',
        text: 'Updated to v' + ver,
        href: '/whatsnew',
        linkText: "See what's new",
        onDismiss: markSeen
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
})();
