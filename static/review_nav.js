// Loaded on every page via _head.html. Fetches the lightweight review-counts
// summary and (1) stamps the count onto the Clean-up item's pill in the gear
// menu, and (2) posts a dismissible "N items to clean up" notice in the shared
// strip (see notices.js). Non-blocking — nothing shows until the response
// arrives, so an unreachable backend leaves no stale state.
(function () {
  // Clean-up is real pending work, so the notice's × only dismisses it for the
  // current session (keyed by the count) — it returns next launch, or sooner if
  // the count changes. This avoids permanently hiding genuine to-dos.
  var SNOOZE = 'alforks_cleanup_snooze';

  function update() {
    fetch('/api/review-counts', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        var total = d.total || 0;

        var pill = document.getElementById('nav-review-badge');
        if (pill) {
          if (total <= 0) { pill.style.display = 'none'; }
          else { pill.textContent = total > 99 ? '99+' : String(total); pill.style.display = ''; }
        }

        if (!window.AlNotices) return;
        var snoozed;
        try { snoozed = sessionStorage.getItem(SNOOZE); } catch (e) { snoozed = null; }
        if (total > 0 && String(total) !== snoozed) {
          window.AlNotices.set('cleanup', {
            icon: '🧹',
            text: total + ' item' + (total === 1 ? '' : 's') + ' to clean up',
            href: '/review',
            linkText: 'Review',
            onDismiss: function () { try { sessionStorage.setItem(SNOOZE, String(total)); } catch (e) {} }
          });
        } else {
          window.AlNotices.remove('cleanup');
        }
      })
      .catch(function () {});
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', update);
  } else {
    update();
  }
})();
