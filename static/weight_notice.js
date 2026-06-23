// Reminds the user to update their body weight when the latest log entry has
// gone stale. Posts a dismissible notice into the shared bell (see notices.js).
// Loaded on every page via _head.html (after notices.js so AlNotices exists).
//
// Only nags when there's at least one entry that's now old — a user who never
// logs weight isn't pestered to start; this is a "keep it current" reminder.
(function () {
  var SNOOZE = 'alforks_weight_snooze';   // ISO date string — suppress until then
  var STALE_DAYS = 14;
  var SNOOZE_DAYS = 7;

  function daysSince(dateStr) {
    var d = new Date(dateStr + 'T00:00:00');
    if (isNaN(d.getTime())) return Infinity;
    return Math.floor((Date.now() - d.getTime()) / 86400000);
  }
  function snoozed() {
    try {
      var until = localStorage.getItem(SNOOZE);
      return !!until && new Date(until + 'T23:59:59').getTime() > Date.now();
    } catch (e) { return false; }
  }
  function snooze() {
    try {
      var until = new Date(Date.now() + SNOOZE_DAYS * 86400000).toISOString().slice(0, 10);
      localStorage.setItem(SNOOZE, until);
    } catch (e) {}
  }

  function run() {
    if (!window.AlNotices) return;
    fetch('/api/weights', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (list) {
        if (!Array.isArray(list) || !list.length) {   // never logged -> don't nag
          window.AlNotices.remove('weight');
          return;
        }
        var age = daysSince(list[list.length - 1].date);   // list is oldest-first
        if (age >= STALE_DAYS && !snoozed()) {
          window.AlNotices.set('weight', {
            icon: '⚖️',                          // ⚖️
            text: 'Weight not logged in ' + age + ' days',
            href: '/training',
            linkText: 'Update it',
            onDismiss: snooze
          });
        } else {
          window.AlNotices.remove('weight');
        }
      })
      .catch(function () {});
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run);
  else run();
})();
