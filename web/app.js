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

const state = {
  route: null, branch: 0, from: null, to: null, sub: null,
  open: null,        // id of the expanded trip card
  routes: new Map(), // number|run_date -> route, so reopening is instant
};

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
function stopRows(stops, { from = null, to = null, interactive = false } = {}) {
  const now = Date.now();
  return stops.map((sp, i) => {
    const time = sp.dep_scheduled || sp.arr_scheduled;
    const delay = sp.dep_delay ?? sp.arr_delay;
    const est = sp.dep_scheduled ? sp.dep_estimated : sp.arr_estimated;
    const expected = sp.dep_expected || sp.arr_expected;
    let note = '';
    if (delay === null || delay === undefined) note = '';
    else if (delay === 0) note = '<span class="s-on_time">on time</span>';
    else {
      const cls = delay < 15 ? 'slight' : delay < 60 ? 'delayed' : 'severe';
      note = `<span class="s-${cls}">${delay > 0 ? '+' : ''}${delay} min</span>
              <span class="exp">→ ${esc(hhmm(expected))}</span>`;
    }
    const sel = i === from ? ' is-from' : i === to ? ' is-to' : '';
    const mid = from !== null && to !== null && i > from && i < to ? ' is-mid' : '';
    // Everything whose expected time has passed is dimmed, so how far the
    // train has actually got is readable at a glance.
    const past = expected && new Date(expected).getTime() < now ? ' is-past' : '';
    const tag = interactive ? 'button' : 'div';
    return `<${tag} class="stop${sel}${mid}${past}"${interactive ? ` data-i="${i}"` : ''}>
        <span class="stop-time">${esc(time || '--:--')}</span>
        <span class="stop-dot"></span>
        <span class="stop-name">${esc(sp.name)}</span>
        <span class="stop-note">${note}${est ? '<em class="tag">est</em>' : ''}</span>
      </${tag}>`;
  }).join('');
}

function renderRoute() {
  const r = state.route;
  const br = r.branches[state.branch];
  const rows = stopRows(br.stops,
    { from: state.from, to: state.to, interactive: true });

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

/* Back to a clean step 1: used after subscribing, so the next train can be
   looked up without clearing a stale route by hand. */
function resetPicker() {
  state.route = null;
  state.branch = 0;
  state.from = state.to = null;
  $('number').value = '';
  $('route-card').innerHTML = '';
  $('step-leg').hidden = true;
  $('step-notify').hidden = true;
  $('train-err').hidden = true;
  $('notify-err').hidden = true;
  $('ios-hint').hidden = true;
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
    await refreshTrips();
    // The new card in the watching list is the confirmation, so the picker
    // clears itself rather than leaving a spent form behind.
    resetPicker();
    btn.textContent = 'Notify me';
    btn.disabled = false;
    $('watching').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
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
        const open = state.open === t.id;
        return `<div class="trip">
            <div class="row${t.active ? '' : ' done'}${open ? ' open' : ''}"
                 data-trip="${t.id}" role="button" tabindex="0"
                 aria-expanded="${open}" title="Show this train's stations">
              <span class="rn">${esc(t.number)}</span>
              <span class="rs">${esc(t.from_name)} → ${esc(t.to_name)}<br>
                <em>${esc(status)}${esc(delay ? ' · ' + delay : '')}</em></span>
              <span class="rd">${esc(whenLabel(eta))}</span>
              <span class="chev" aria-hidden="true">${open ? '▴' : '▾'}</span>
              <button class="link del" data-id="${t.id}" aria-label="Stop watching">✕</button>
            </div>
            <div class="detail" id="detail-${t.id}"${open ? '' : ' hidden'}></div>
          </div>`;
      }).join('');

  $('trip-list').querySelectorAll('.del').forEach((el) => {
    el.addEventListener('click', async (e) => {
      // Sits inside the row, which is itself a toggle.
      e.stopPropagation();
      await api(`/api/trips/${el.dataset.id}/delete`, { subscription: state.sub });
      if (state.open === Number(el.dataset.id)) state.open = null;
      refreshTrips();
    });
  });

  $('trip-list').querySelectorAll('.row[data-trip]').forEach((el) => {
    const id = Number(el.dataset.trip);
    // Toggling touches the DOM directly instead of calling refreshTrips():
    // expanding a card should not wait on a round trip to the server.
    const toggle = () => {
      state.open = state.open === id ? null : id;
      applyOpenState(trips);
    };
    el.addEventListener('click', toggle);
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
  });

  if (!trips.some((t) => t.id === state.open)) state.open = null;
  applyOpenState(trips);
}

function applyOpenState(trips) {
  $('trip-list').querySelectorAll('.trip').forEach((el) => {
    const row = el.querySelector('.row[data-trip]');
    if (!row) return;
    const open = state.open === Number(row.dataset.trip);
    row.classList.toggle('open', open);
    row.setAttribute('aria-expanded', String(open));
    const chev = row.querySelector('.chev');
    if (chev) chev.textContent = open ? '▴' : '▾';
    const det = el.querySelector('.detail');
    det.hidden = !open;
    if (!open) det.innerHTML = '';
  });
  const trip = trips.find((t) => t.id === state.open);
  if (trip) showTripDetail(trip);
}

function renderDetail(host, trip, route) {
  const br = route.branches.find((b) => b.code === trip.branch_code)
    || route.branches.find((b) => {
         const slugs = b.stops.map((x) => x.slug);
         return slugs.includes(trip.from_slug) && slugs.includes(trip.to_slug);
       })
    || route.branches[0];

  const idx = (slug) => br.stops.findIndex((x) => x.slug === slug);
  host.innerHTML = `
    ${br.position_note ? `<p class="note">${esc(br.position_note)}</p>` : ''}
    ${br.summary_delay !== null && br.summary_delay !== undefined
        ? `<p class="hint">Reported ${esc(br.summary_delay)} min late${
             br.reported_at ? ` at ${esc(br.reported_at)}` : ''}</p>`
        : ''}
    <div class="stops">${stopRows(br.stops,
        { from: idx(trip.from_slug), to: idx(trip.to_slug) })}</div>`;
}

/* Stations + live delays for an already-watched trip, under its card. */
async function showTripDetail(trip) {
  const host = $(`detail-${trip.id}`);
  if (!host) return;
  const key = `${trip.number}|${trip.run_date}`;

  // Paint the cached route first so the 60s refresh never blanks an open
  // panel back to a loading message.
  const cached = state.routes.get(key);
  if (cached) renderDetail(host, trip, cached);
  else host.innerHTML = '<p class="hint">Loading stations…</p>';

  let route;
  try {
    route = await api(`/api/route/${encodeURIComponent(trip.number)}?date=${trip.run_date}`);
  } catch (ex) {
    if (!cached) host.innerHTML = `<p class="err">${esc(ex.message)}</p>`;
    return;
  }
  state.routes.set(key, route);
  if (state.open === trip.id) renderDetail(host, trip, route);
}


/* ------------------------------------------------------------------ boot */
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').then(refreshTrips).catch(() => {});
}
setInterval(refreshTrips, 60000);
