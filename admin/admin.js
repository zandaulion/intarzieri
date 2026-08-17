/* Întârzieri admin. Served only on the tailnet listener, which is what
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
    if (!r.ok) throw new Error(data.detail || `Cererea a eșuat (${r.status})`);
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
    btn.textContent = ok ? 'Copiat' : 'Selectează manual';
    setTimeout(() => { btn.textContent = was; }, 1600);
  }
}

const when = (iso) => {
  if (!iso) return 'niciodată';
  const d = new Date(iso);
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return 'chiar acum';
  if (mins < 60) return `acum ${mins} min`;
  if (mins < 60 * 24) return `acum ${Math.round(mins / 60)} h`;
  return d.toLocaleDateString('ro-RO', { day: 'numeric', month: 'short' });
};

/* ------------------------------------------------------------- invites --- */
$('form-invite').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('invite-err');
  err.hidden = true;
  const btn = e.target.querySelector('button');
  btn.disabled = true;
  btn.textContent = 'Se creează…';
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
    btn.textContent = 'Creează invitație';
  }
});

/* The whole message to send, so the sender is not left assembling one.

   The link is safe in a chat: opening it only fills the code in, and
   redemption needs a real tap, so a preview fetch cannot spend it. The steps
   still put installing first, because an invite redeemed in a chat's own
   browser leaves the installed app unregistered -- which is what the in-app
   warning on the gate exists to catch. */
function inviteMessage(inv) {
  return [
    'Salut! Îți trimit acces la Întârzieri — urmărește trenurile CFR și te',
    'anunță când întârzie.',
    '',
    '1) Deschide linkul:',
    inv.url,
    '',
    '2) Adaugă pagina pe ecranul principal:',
    '• Android (Chrome): butonul „Instalează” din banner, sau meniul ⋮ →',
    '  „Adaugă la ecranul principal”',
    '• iPhone (Safari): butonul de partajare → „Add to Home Screen”',
    '',
    '3) Deschide aplicația de pe ecranul principal și apasă linkul de mai sus',
    'din nou — codul se completează singur. Apasă „Activează”.',
    '',
    `Codul e valabil ${inv.expires_in_days ?? 7} zile și înregistrează un singur telefon.`,
    'Notificările merg doar din aplicația instalată.',
  ].join('\n');
}

function showInvite(inv) {
  const box = $('invite-result');
  box.hidden = false;
  box.innerHTML = `
    ${inv.url ? `<div class="field">
      <label for="v-link">Link</label>
      <div class="val" id="v-link">${esc(inv.url)}</div>
      <button class="btn small" data-copy="${esc(inv.url)}">Copiază</button>
    </div>` : ''}
    <div class="field">
      <label for="v-code">Cod</label>
      <div class="val code-big" id="v-code">${esc(inv.code)}</div>
      <button class="btn small" data-copy="${esc(inv.code)}">Copiază</button>
    </div>
    ${inv.url ? `<div class="field">
      <label for="v-msg">Mesaj</label>
      <div class="val" id="v-msg">Mesaj complet, cu link și pași</div>
      <button class="btn small" data-copy-msg="1">Copiază mesajul</button>
      <a class="btn small ghost" target="_blank" rel="noopener"
         href="https://wa.me/?text=${encodeURIComponent(inviteMessage(inv))}">WhatsApp</a>
    </div>` : ''}
    <p class="note">
      Un singur dispozitiv, expiră în ${esc(inv.expires_in_days)} zile.
      Linkul poate fi trimis liniștit: deschiderea lui nu consumă invitația —
      destinatarul trebuie să apese <strong>Activează</strong>, deci
      previzualizările din aplicațiile de chat nu o pot arde.<br>
      <strong>Pe iPhone</strong> trimite <em>codul</em>: trebuie să adauge
      întâi pagina pe ecranul principal și să îl introducă acolo, fiindcă
      aplicația instalată are stocare proprie și e singurul loc unde
      funcționează notificările.
    </p>`;
  box.querySelectorAll('[data-copy]').forEach((b) =>
    b.addEventListener('click', () => copy(b.dataset.copy, b)));
  box.querySelectorAll('[data-copy-msg]').forEach((b) =>
    b.addEventListener('click', () => copy(inviteMessage(inv), b)));
}

/* ------------------------------------------------------------- listings --- */
/* Carried on the listing rather than on each row, so the message builder is
   given it explicitly. */
let ttlDays = 7;

async function load() {
  try {
    const [d, i] = await Promise.all([
      api('/api/admin/devices'),
      api('/api/admin/invites'),
    ]);
    renderDevices(d.devices);
    ttlDays = i.ttl_days ?? ttlDays;
    renderInvites(i.invites);
  } catch (ex) {
    $('devices').innerHTML = `<p class="err">${esc(ex.message)}</p>`;
  }
}

function renderDevices(devices) {
  if (!devices.length) {
    $('devices').innerHTML = '<p class="empty">Niciun dispozitiv înregistrat.</p>';
    return;
  }
  $('devices').innerHTML = devices.map((d) => `
    <div class="item${d.revoked ? ' off' : ''}">
      <div class="grow">
        <div class="name">${esc(d.label || `Device ${d.id}`)}
          <span class="pill ${d.revoked ? 'bad' : 'ok'}">${d.revoked ? 'revocat' : 'activ'}</span>
          ${d.has_push ? '' : '<span class="pill warn">fără notificări</span>'}
        </div>
        <div class="meta">
          #${d.id} · ${d.active_trips} ${d.active_trips === 1 ? 'tren' : 'trenuri'} urmărite
          · văzut ultima dată ${esc(when(d.last_seen))}
        </div>
      </div>
      <button class="btn small ghost" data-rename="${d.id}">Redenumește</button>
      <button class="btn small ghost" data-revoke="${d.id}" data-to="${d.revoked ? 0 : 1}">
        ${d.revoked ? 'Restaurează' : 'Revocă'}
      </button>
      <button class="btn small danger" data-forget="${d.id}"
              data-trips="${d.active_trips}">Șterge</button>
    </div>`).join('');

  $('devices').querySelectorAll('[data-revoke]').forEach((b) =>
    b.addEventListener('click', async () => {
      const on = b.dataset.to === '1';
      // Revoking cuts someone off mid-journey, so it asks first.
      if (on && !confirm('Revoci acest dispozitiv? Pierde accesul imediat și '
                       + 'nu mai primește notificări.')) return;
      await api(`/api/admin/devices/${b.dataset.revoke}/revoke`, { revoked: on });
      load();
    }));

  $('devices').querySelectorAll('[data-forget]').forEach((b) =>
    b.addEventListener('click', async () => {
      const n = Number(b.dataset.trips);
      // Deleting cascades to the device's trips and push subscription, so the
      // count is worth stating rather than discovering afterwards.
      const extra = n ? ` Cele ${n} curse urmărite se pierd.` : '';
      if (!confirm(`Ștergi definitiv acest dispozitiv?${extra} `
                 + 'Va avea nevoie de o invitație nouă.')) return;
      await api(`/api/admin/devices/${b.dataset.forget}`, null, 'DELETE');
      load();
    }));

  $('devices').querySelectorAll('[data-rename]').forEach((b) =>
    b.addEventListener('click', async () => {
      const label = prompt('Eticheta dispozitivului (ex. „Ana — iPhone”)');
      if (label === null) return;
      await api(`/api/admin/devices/${b.dataset.rename}/label`, { label });
      load();
    }));
}

function renderInvites(invites) {
  const open = invites.filter((i) => !i.used_at);
  const used = invites.filter((i) => i.used_at);
  if (!invites.length) {
    $('invites').innerHTML = '<p class="empty">Nicio invitație.</p>';
    return;
  }
  const now = Date.now();
  const row = (i) => {
    const expired = !i.used_at && new Date(i.expires_at).getTime() < now;
    const state = i.used_at ? ['ok', `folosită ${when(i.used_at)}`]
      : expired ? ['bad', 'expirată']
      : ['warn', 'în așteptare'];
    // The code exists only while the invite can still register something;
    // it is wiped from the database on redemption.
    const live = i.code && !i.used_at && !expired;
    return `
      <div class="item${i.used_at || expired ? ' off' : ''}">
        <div class="grow">
          <div class="name">${esc(i.label || 'fără etichetă')}
            <span class="pill ${state[0]}">${esc(state[1])}</span>
            ${i.adopt_id ? '<span class="pill">adopție</span>' : ''}
          </div>
          <div class="meta">
            #${i.id} · creată ${esc(when(i.created_at))}
            ${i.device_id ? ` · a devenit dispozitivul #${i.device_id}` : ''}
          </div>
          ${live ? `<div class="code-row">
              <code>${esc(i.code)}</code>
              <button class="btn small ghost" data-copy="${esc(i.code)}">Cod</button>
              ${i.url ? `<button class="btn small ghost" data-copy="${esc(i.url)}">Link</button>
              <button class="btn small ghost" data-msg="${i.id}">Mesaj</button>` : ''}
            </div>` : ''}
        </div>
        ${i.used_at ? '' :
          `<button class="btn small ghost" data-unvite="${i.id}">Revocă</button>`}
      </div>`;
  };
  $('invites').innerHTML = [...open, ...used].map(row).join('');
  $('invites').querySelectorAll('[data-copy]').forEach((b) =>
    b.addEventListener('click', () => copy(b.dataset.copy, b)));

  $('invites').querySelectorAll('[data-msg]').forEach((b) =>
    b.addEventListener('click', () => {
      const i = invites.find((x) => String(x.id) === b.dataset.msg);
      copy(inviteMessage({ ...i, expires_in_days: ttlDays }), b);
    }));
  $('invites').querySelectorAll('[data-unvite]').forEach((b) =>
    b.addEventListener('click', async () => {
      await api(`/api/admin/invites/${b.dataset.unvite}/revoke`, {});
      load();
    }));
}

$('prune-devices').addEventListener('click', async () => {
  if (!confirm('Ștergi toate dispozitivele revocate? Cursele lor se pierd.')) return;
  const r = await api('/api/admin/devices/prune', {});
  await load();
  alert(`Am șters ${r.deleted} ${r.deleted === 1 ? 'dispozitiv' : 'dispozitive'}.`);
});

$('prune').addEventListener('click', async () => {
  if (!confirm('Ștergi toate invitațiile folosite și expirate? Cele în așteptare rămân.')) return;
  const r = await api('/api/admin/invites/prune', {});
  await load();
  alert(`Am șters ${r.deleted} ${r.deleted === 1 ? 'invitație' : 'invitații'}.`);
});

$('refresh').addEventListener('click', load);
load();
