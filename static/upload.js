// Manual track upload — loaded on every page via _head.html. Injects an
// "Upload" action button just before the header's Sync button (mirroring its
// treatment) plus a hidden multi-file <input>. Posts the chosen .gpx file(s)
// to /api/upload-track, surfaces a small top-right result toast, then either
// opens the new track (single file) or reloads so the list picks it up
// (multiple). The user explicitly wanted a button, NOT drag-and-drop.
(function () {
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // Minimal self-contained toast (the sync toast's renderer is module-private,
  // so we don't try to share it). Mirrors its position/style.
  let toastEl = null, hideTimer = null;
  function toast(html, holdMs) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.id = 'al-upload-toast';
      toastEl.style.cssText = `
        position: fixed; right: 18px; top: 10px; z-index: 99999;
        background: #12151a; border: 1px solid #2a2d35; border-radius: 8px;
        padding: 12px 16px; min-width: 240px; max-width: 360px;
        box-shadow: 0 6px 24px rgba(0,0,0,0.5);
        font-family: system-ui, -apple-system, sans-serif;
        font-size: 0.78rem; color: #e0e0e0;
        transition: opacity 0.2s;`;
      document.body.appendChild(toastEl);
    }
    toastEl.innerHTML = html;
    toastEl.style.display = '';
    toastEl.style.opacity = '1';
    clearTimeout(hideTimer);
    if (holdMs) {
      hideTimer = setTimeout(() => {
        toastEl.style.opacity = '0';
        setTimeout(() => { if (toastEl) toastEl.style.display = 'none'; }, 250);
      }, holdMs);
    }
  }

  async function doUpload(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const fd = new FormData();
    files.forEach(f => fd.append('file', f));
    const label = files.length === 1 ? esc(files[0].name) : (files.length + ' files');
    toast('<div style="color:#f59e0b">⟳ Uploading ' + label + '…</div>');
    try {
      const resp = await fetch('/api/upload-track', { method: 'POST', body: fd });
      const data = await resp.json().catch(() => null);
      if (!data) throw new Error(resp.status >= 500
        ? 'Something went wrong on the server — please try again.'
        : 'That upload couldn’t be processed — check the file and try again.');

      const added = data.added || [];
      const errors = data.errors || [];
      let rows = '';
      if (added.length) {
        rows += '<div style="color:#22c55e">✓ Added ' + added.length +
                ' track' + (added.length === 1 ? '' : 's') + '</div>';
      }
      errors.forEach(e => {
        rows += '<div style="color:#ef4444;margin-top:2px">✗ ' +
                esc(e.name) + ': ' + esc(e.error) + '</div>';
      });
      if (data.ready_url && added.length === 1 && !errors.length) {
        rows += '<div style="color:#888;margin-top:4px">Opening…</div>';
        toast(rows);
        setTimeout(() => { window.location = data.ready_url; }, 700);
        return;
      }
      const holdMs = errors.length ? 9000 : 5000;
      toast(rows || '<div style="color:#888">Nothing uploaded.</div>', holdMs);
      // New tracks landed but we're not navigating (multi-file, or some
      // failed) — reload so the sidebar/list reflects the additions. When some
      // files errored, wait out the toast so the user can read the failures
      // before the page reloads; otherwise reload promptly.
      if (added.length) {
        setTimeout(() => window.location.reload(), errors.length ? holdMs : 1200);
      }
    } catch (e) {
      // Self-thrown messages end in punctuation; a raw fetch failure (offline /
      // server down) does not — give those a plain "is it running?" hint.
      let msg = (e && e.message) || '';
      if (!/[.!?]$/.test(msg)) msg = 'Upload failed — is AlForks still running?';
      toast('<div style="color:#ef4444">' + esc(msg) + '</div>', 9000);
    }
  }

  function inject() {
    const header = document.querySelector('header');
    if (!header || document.querySelector('.nav-upload-btn')) return;

    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.gpx';
    input.multiple = true;
    input.style.display = 'none';
    input.addEventListener('change', () => {
      doUpload(input.files);
      input.value = '';  // allow re-selecting the same file later
    });

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'nav-upload-btn';
    btn.title = 'Upload one or more .gpx tracks';
    btn.textContent = 'Upload';
    btn.addEventListener('click', () => input.click());

    const sync = header.querySelector('.nav-sync-btn');
    if (sync) sync.insertAdjacentElement('afterend', btn);
    else header.appendChild(btn);
    header.appendChild(input);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }
})();
