/* Train Watch -- pick a train, pick a leg, get push notifications. */
'use strict';

const $ = (id) => document.getElementById(id);
const api = (path, body) =>
  fetch(path, body ? {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  } : undefined).then(async (r) => {
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
    return data;
  });

const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const hhmm = (iso) => (iso ? new Date(iso).toLocaleTimeString([],
  { hour: '2-digit', minute: '2-digit' }) : '--:--');

// A bare HH:MM is ambiguous once a trip is not from today -- overnight trains
// routinely arrive on the following date.
const whenLabel = (iso) => {
  if (!iso) return '--:--';
  const d = new Date(iso);
  if (d.toDateString() === new Date().toDateString()) return hhmm(iso);
  return `${d.toLocaleDateString([], { day: 'numeric', month: 'short' })} ${hhmm(iso)}`;
};

const state = { route: null, branch: 0, from: null, to: null, sub: null };

/* ---------------------------------------------------------------- step 1 */
$('form-train').addEventListener('submit', async (e) => {
  e.preventDefault();
  const num = $('number').value.trim().replace(/\D/g, '');
  if (!num) return;
  const err = $('train-err');
  err.hidden = true;
  const btn = e.target.querySelector('button');
  btn.disabled = true;
  btn.textContent = 'Finding…';
  try {
    state.route = await api(`/api/route/${encodeURIComponent(num)}`);
    // A train can be published as several variants of the same run; start on
    // the one InfoFer shows by default.
    const def = state.route.branches.findIndex((b) => b.is_default);
    state.branch = def === -1 ? 0 : def;
    state.from = state.to = null;
    renderRoute();
    $('step-leg').hidden = false;
    $('step-notify').hidden = true;
    $('step-leg').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Find';
  }
});

/* ---------------------------------------------------------------- step 2 */
function renderRoute() {
  const r = state.route;
  const br = r.branches[state.branch];
  const rows = br.stops.map((s, i) => {
    const time = s.dep_scheduled || s.arr_scheduled;
    const delay = s.dep_delay ?? s.arr_delay;
    const est = s.dep_scheduled ? s.dep_estimated : s.arr_estimated;
    const expected = s.dep_expected || s.arr_expected;
    let note = '';
    if (delay === null || delay === undefined) note = '';
    else if (delay === 0) note = `<span class="s-on_time">on time</span>`;
    else {
      const cls = delay < 15 ? 'slight' : delay < 60 ? 'delayed' : 'severe';
      note = `<span class="s-${cls}">${delay > 0 ? '+' : ''}${delay} min</span>
              <span class="exp">→ ${esc(hhmm(expected))}</span>`;
    }
    const pick = state.from === null ? 'from' : (i > state.from ? 'to' : 'from');
    const sel = i === state.from ? ' is-from' : i === state.to ? ' is-to' : '';
    const between = state.from !== null && state.to !== null
      && i > state.from && i < state.to ? ' is-mid' : '';
    return `<button class="stop${sel}${between}" data-i="${i}" data-pick="${pick}">
        <span class="stop-time">${esc(time || '--:--')}</span>
        <span class="stop-dot"></span>
        <span class="stop-name">${esc(s.name)}</span>
        <span class="stop-note">${note}${est ? '<em class="tag">est</em>' : ''}</span>
      </button>`;
  }).join('');

  const hint = state.from === null
    ? 'Tap the station you board at.'
    : state.to === null
      ? 'Now tap the station you get off at.'
      : 'Tap any station to start over.';

  const picker = r.branches.length > 1
    ? `<div class="branches">${r.branches.map((b, i) =>
        `<button class="chip${i === state.branch ? ' on' : ''}" data-b="${i}">
           ${esc(b.name)}</button>`).join('')}</div>`
    : '';

  $('route-card').innerHTML = `
    <div class="tnum">
      <span class="cat">${esc(r.category || 'TR')}</span>
      <span class="num">${esc(r.number)}</span>
      <span class="tag">${esc(r.run_date)}</span>
    </div>
    ${picker}
    ${br.position_note ? `<p class="note">${esc(br.position_note)}</p>` : ''}
    <p class="hint">${esc(hint)}</p>
    <div class="stops">${rows}</div>`;

  $('route-card').querySelectorAll('.stop').forEach((el) => {
    el.addEventListener('click', () => selectStop(Number(el.dataset.i)));
  });
  $('route-card').querySelectorAll('.chip').forEach((el) => {
    el.addEventListener('click', () => {
      state.branch = Number(el.dataset.b);
      state.from = state.to = null;
      renderRoute();
      $('step-notify').hidden = true;
    });
  });
}

function selectStop(i) {
  if (state.from === null) state.from = i;
  else if (state.to === null && i > state.from) state.to = i;
  else if (state.to === null && i <= state.from) state.from = i;
  else { state.from = i; state.to = null; }
  renderRoute();

  if (state.from !== null && state.to !== null) {
    const stops = state.route.branches[state.branch].stops;
    const a = stops[state.from];
    const b = stops[state.to];
    $('leg-summary').innerHTML =
      `<strong>${esc(state.route.category || '')} ${esc(state.route.number)}</strong>
       from <strong>${esc(a.name)}</strong> (${esc(a.dep_scheduled || '--:--')})
       to <strong>${esc(b.name)}</strong> (${esc(b.arr_scheduled || '--:--')})`;
    $('step-notify').hidden = false;
    $('step-notify').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } else {
    $('step-notify').hidden = true;
  }
}

$('btn-back').addEventListener('click', () => {
  state.from = state.to = null;
  renderRoute();
  $('step-notify').hidden = true;
});

/* ------------------------------------------------------------ push setup */
const b64ToBytes = (b64) => {
  const pad = '='.repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
};

const standalone = () =>
  window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;

async function getSubscription() {
  if (state.sub) return state.sub;
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    throw new Error('This browser does not support push notifications.');
  }
  const reg = await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  if (!sub) {
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') {
      throw new Error('Notifications are blocked. Allow them for this site and try again.');
    }
    const { publicKey } = await api('/api/vapid');
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: b64ToBytes(publicKey),
    });
  }
  state.sub = sub.toJSON();
  return state.sub;
}

$('btn-watch').addEventListener('click', async () => {
  const err = $('notify-err');
  err.hidden = true;
  const btn = $('btn-watch');
  btn.disabled = true;
  btn.textContent = 'Setting up…';
  try {
    const sub = await getSubscription();
    const stops = state.route.branches[state.branch].stops;
    const a = stops[state.from];
    const b = stops[state.to];
    await api('/api/trips', {
      subscription: sub,
      number: state.route.number,
      run_date: state.route.run_date,
      from_slug: a.slug,
      to_slug: b.slug,
    });
    btn.textContent = 'Watching ✓';
    await refreshTrips();
    $('watching').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    setTimeout(() => { btn.textContent = 'Notify me'; btn.disabled = false; }, 1800);
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
    // iOS only exposes push to installed PWAs, so this is the usual cause.
    if (/iPhone|iPad|iPod/.test(navigator.userAgent) && !standalone()) {
      $('ios-hint').hidden = false;
    }
    btn.textContent = 'Notify me';
    btn.disabled = false;
  }
});

$('btn-test').addEventListener('click', async () => {
  try {
    const sub = await getSubscription();
    const res = await api('/api/push/test', { subscription: sub });
    if (!res.delivered) throw new Error(`Push service refused it (${res.status}).`);
  } catch (ex) {
    alert(ex.message);
  }
});

/* -------------------------------------------------------------- watching */
async function refreshTrips() {
  let sub;
  try {
    if (!('serviceWorker' in navigator)) return;
    const reg = await navigator.serviceWorker.ready;
    const existing = await reg.pushManager.getSubscription();
    if (!existing) return;
    sub = existing.toJSON();
    state.sub = sub;
  } catch { return; }

  let trips = [];
  try {
    ({ trips } = await api('/api/trips/list', { subscription: sub }));
  } catch { return; }

  // Keep the panel up once a subscription exists, so the test button stays
  // reachable even before anything is being watched.
  $('watching').hidden = trips.length === 0 && !state.sub;
  $('trip-list').innerHTML = trips.length === 0
    ? '<p class="hint">Nothing yet. Pick a train above.</p>'
    : trips.map((t) => {
        // active=0 with arrived=0 means the 6h fallback retired it: the train
        // stopped being published before an arrival was ever seen.
        const status = t.arrived ? 'arrived'
          : !t.active ? 'no longer tracked'
          : t.departed ? 'en route' : 'not yet departed';
        const d = t.last_delay;
        const delay = (d === null || d === undefined)
          ? '' : (d === 0 ? 'on time' : `${d > 0 ? '+' : ''}${d} min`);
        // Show when it is actually expected, not the timetable time.
        const eta = t.arr_planned
          ? new Date(new Date(t.arr_planned).getTime() + (d || 0) * 60000).toISOString()
          : null;
        return `<div class="row${t.active ? '' : ' done'}">
            <span class="rn">${esc(t.number)}</span>
            <span class="rs">${esc(t.from_name)} → ${esc(t.to_name)}<br>
              <em>${esc(status)}${esc(delay ? ' · ' + delay : '')}</em></span>
            <span class="rd">${esc(whenLabel(eta))}</span>
            <button class="link del" data-id="${t.id}" aria-label="Stop watching">✕</button>
          </div>`;
      }).join('');

  $('trip-list').querySelectorAll('.del').forEach((el) => {
    el.addEventListener('click', async () => {
      await api(`/api/trips/${el.dataset.id}/delete`, { subscription: state.sub });
      refreshTrips();
    });
  });
}

/* ------------------------------------------------------------------ boot */
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').then(refreshTrips).catch(() => {});
}
setInterval(refreshTrips, 60000);
