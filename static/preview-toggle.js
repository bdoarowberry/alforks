// Style-cleanup preview toggle. Adds/removes `body.preview` so the
// preview.css override block applies. Persists across navigations via
// localStorage so the toggle stays sticky across page loads.

(function () {
  const KEY = 'alforks_preview_styles';

  function apply() {
    document.body.classList.toggle('preview', localStorage.getItem(KEY) === '1');
    const btn = document.getElementById('preview-toggle-btn');
    if (btn) btn.classList.toggle('on', localStorage.getItem(KEY) === '1');
  }

  window.togglePreviewStyles = function () {
    const next = localStorage.getItem(KEY) === '1' ? null : '1';
    if (next) localStorage.setItem(KEY, next);
    else      localStorage.removeItem(KEY);
    apply();
  };

  // Apply ASAP — DOMContentLoaded so body exists.
  if (document.body) apply();
  else document.addEventListener('DOMContentLoaded', apply);
})();
