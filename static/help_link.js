// Help / Guide link — loaded on every page via _head.html. Adds a persistent
// "Guide" entry to the header nav so the onboarding & help walkthrough is
// reachable from anywhere, not only buried on the Setup page. Mirrors the
// upload.js / whatsnew.js header-injection pattern so no per-page template
// needs editing (the nav is currently copy-pasted across templates).
(function () {
  function inject() {
    var nav = document.querySelector('header nav');
    if (!nav || nav.querySelector('.nav-guide-link')) return;
    var a = document.createElement('a');
    a.className = 'nav-guide-link';
    a.href = '/guide';
    a.textContent = 'Guide';
    a.title = 'Setup & help guide';
    var path = location.pathname.replace(/\/+$/, '') || '/';
    if (path === '/guide') a.classList.add('active');
    nav.appendChild(a);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }
})();
