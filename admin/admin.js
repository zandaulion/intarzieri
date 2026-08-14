/* Train Watch admin. Served only on the tailnet listener, which is what
   injects X-Admin on the way to the API -- this page carries no credential
   of its own and would get 404s from the public URL. */
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const api = (path, body, method) =>
  fetch(path, {
    method: method || (body ? 'POST' : 'GET'),
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  }).then(async (r) => {
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
    return data;
  });

/* This page is served over plain HTTP on the tailnet, which is not a secure
   context, so navigator.clipboard does not exist here. The deprecated
   execCommand path is the only one available. */
async function copy(text, btn) {
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch { ok = false; }
  if (!ok) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);   // iOS needs the explicit range
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    document.body.removeChild(ta);
  }
  if (btn) {
    const was = btn.textContent;
    btn.textContent = ok ? 'Copied' : 'Select it manually';
    setTimeout(() => { btn.textContent = was; }, 1600);
  }
}

const when = (iso) => {
  if (!iso) return 'never';
  const d = new Date(iso);
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)} h ago`;
  return d.toLocaleDateString([], { day: 'numeric', month: 'short' });
};

/* ------------------------------------------------------------- invites --- */
$('form-invite').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('invite-err');
  err.hidden = true;
  const btn = e.target.querySelector('button');
  btn.disabled = true;
  btn.textContent = 'Creating…';
  try {
    const label = $('invite-label').value.trim();
    const inv = await api('/api/admin/invites', { label });
    $('invite-label').value = '';
    showInvite(inv);
    await load();
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Create invite';
  }
});

function showInvite(inv) {
  const box = $('invite-result');
  box.hidden = false;
  box.innerHTML = `
    ${inv.url ? `<div class="field">
      <label for="v-link">Link</label>
      <div class="val" id="v-link">${esc(inv.url)}</div>
      <button class="btn small" data-copy="${esc(inv.url)}">Copy</button>
    </div>` : ''}
    <div class="field">
      <label for="v-code">Code</label>
      <div class="val code-big" id="v-code">${esc(inv.code)}</div>
      <button class="btn small" data-copy="${esc(inv.code)}">Copy</button>
    </div>
    <p class="note">
      Single use, expires in ${esc(inv.expires_in_days)} days. Sharing the link
      is safe: opening it does not spend the invite — the recipient has to tap
      <strong>Activate</strong>, so chat-app link previews cannot burn it.<br>
      <strong>On iPhone</strong> send the <em>code</em>: they must add the page
      to the Home Screen first and enter it there, because the installed app
      has its own storage and is the only place notifications work.
    </p>`;
  box.querySelectorAll('[data-copy]').forEach((b) =>
    b.addEventListener('click', () => copy(b.dataset.copy, b)));
}

/* ------------------------------------------------------------- listings --- */
async function load() {
  try {
    const [d, i] = await Promise.all([
      api('/api/admin/devices'),
      api('/api/admin/invites'),
    ]);
    renderDevices(d.devices);
    renderInvites(i.invites);
  } catch (ex) {
    $('devices').innerHTML = `<p class="err">${esc(ex.message)}</p>`;
  }
}

function renderDevices(devices) {
  if (!devices.length) {
    $('devices').innerHTML = '<p class="empty">No devices registered yet.</p>';
    return;
  }
  $('devices').innerHTML = devices.map((d) => `
    <div class="item${d.revoked ? ' off' : ''}">
      <div class="grow">
        <div class="name">${esc(d.label || `Device ${d.id}`)}
          <span class="pill ${d.revoked ? 'bad' : 'ok'}">${d.revoked ? 'revoked' : 'active'}</span>
          ${d.has_push ? '' : '<span class="pill warn">no notifications</span>'}
        </div>
        <div class="meta">
          #${d.id} · ${d.active_trips} train${d.active_trips === 1 ? '' : 's'} watched
          · last seen ${esc(when(d.last_seen))}
        </div>
      </div>
      <button class="btn small ghost" data-rename="${d.id}">Rename</button>
      <button class="btn small ghost" data-revoke="${d.id}" data-to="${d.revoked ? 0 : 1}">
        ${d.revoked ? 'Restore' : 'Revoke'}
      </button>
    </div>`).join('');

  $('devices').querySelectorAll('[data-revoke]').forEach((b) =>
    b.addEventListener('click', async () => {
      const on = b.dataset.to === '1';
      // Revoking cuts someone off mid-journey, so it asks first.
      if (on && !confirm('Revoke this device? It loses access immediately and '
                       + 'stops receiving notifications.')) return;
      await api(`/api/admin/devices/${b.dataset.revoke}/revoke`, { revoked: on });
      load();
    }));

  $('devices').querySelectorAll('[data-rename]').forEach((b) =>
    b.addEventListener('click', async () => {
      const label = prompt('Label for this device (e.g. "Ana - iPhone")');
      if (label === null) return;
      await api(`/api/admin/devices/${b.dataset.rename}/label`, { label });
      load();
    }));
}

function renderInvites(invites) {
  const open = invites.filter((i) => !i.used_at);
  const used = invites.filter((i) => i.used_at);
  if (!invites.length) {
    $('invites').innerHTML = '<p class="empty">No invites yet.</p>';
    return;
  }
  const now = Date.now();
  const row = (i) => {
    const expired = !i.used_at && new Date(i.expires_at).getTime() < now;
    const state = i.used_at ? ['ok', `used ${when(i.used_at)}`]
      : expired ? ['bad', 'expired']
      : ['warn', `expires ${when(i.expires_at).replace(' ago', ' from now')}`];
    // The code exists only while the invite can still register something;
    // it is wiped from the database on redemption.
    const live = i.code && !i.used_at && !expired;
    return `
      <div class="item${i.used_at || expired ? ' off' : ''}">
        <div class="grow">
          <div class="name">${esc(i.label || 'unlabelled')}
            <span class="pill ${state[0]}">${esc(state[1])}</span>
            ${i.adopt_id ? '<span class="pill">adoption</span>' : ''}
          </div>
          <div class="meta">
            #${i.id} · created ${esc(when(i.created_at))}
            ${i.device_id ? ` · became device #${i.device_id}` : ''}
          </div>
          ${live ? `<div class="code-row">
              <code>${esc(i.code)}</code>
              <button class="btn small ghost" data-copy="${esc(i.code)}">Code</button>
              ${i.url ? `<button class="btn small ghost" data-copy="${esc(i.url)}">Link</button>` : ''}
            </div>` : ''}
        </div>
        ${i.used_at ? '' :
          `<button class="btn small ghost" data-unvite="${i.id}">Revoke</button>`}
      </div>`;
  };
  $('invites').innerHTML = [...open, ...used].map(row).join('');
  $('invites').querySelectorAll('[data-copy]').forEach((b) =>
    b.addEventListener('click', () => copy(b.dataset.copy, b)));
  $('invites').querySelectorAll('[data-unvite]').forEach((b) =>
    b.addEventListener('click', async () => {
      await api(`/api/admin/invites/${b.dataset.unvite}/revoke`, {});
      load();
    }));
}

$('prune').addEventListener('click', async () => {
  if (!confirm('Delete every used and expired invite? Pending ones are kept.')) return;
  const r = await api('/api/admin/invites/prune', {});
  await load();
  alert(`Removed ${r.deleted} invite${r.deleted === 1 ? '' : 's'}.`);
});

$('refresh').addEventListener('click', load);
load();
