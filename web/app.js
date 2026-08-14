(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const LABEL = { on_time: 'la timp', slight: 'întârziere mică', delayed: 'întârziat', severe: 'întârziere mare' };

  // ---- rendering -------------------------------------------------------
  function renderTrain(t) {
    const m = t.measured || {};
    const st = m.near_station;
    const delay = t.delay_min > 0
      ? `${t.delay_min}<small>min întârziere</small>`
      : `La timp`;

    const kindTag = m.kind === 'reported'
      ? '<span class="tag">raportat</span>'
      : m.kind === 'estimated' ? '<span class="tag">estimat</span>' : '';
    const gpsTag = t.position?.source === 'gps' ? '<span class="tag">GPS</span>' : '';

    const whereLine = st
      ? `${esc(st.name)}${st.is_halt ? ' (haltă)' : ''} <span class="hint">— ${st.distance_km} km away</span>`
      : 'unknown';

    const when = m.at
      ? `${esc(m.at)}${m.minutes_ago != null ? ` <span class="hint">— ${m.minutes_ago} min ago</span>` : ''}`
      : 'unknown';

    $('result').innerHTML = `
      <div class="card">
        <div class="tnum">
          <span class="cat">${esc(t.category)}</span>
          <span class="num">${esc(t.number)}</span>
          ${kindTag}${gpsTag}
        </div>
        <p class="big s-${esc(t.status)}">${delay}</p>
        <dl class="where">
          <dt>Measured near</dt><dd>${whereLine}</dd>
          <dt>Measured at</dt><dd>${when}</dd>
          <dt>Position</dt>
          <dd><a href="https://www.openstreetmap.org/?mlat=${t.position.lat}&mlon=${t.position.lon}#map=13/${t.position.lat}/${t.position.lon}"
                 target="_blank" rel="noopener">${t.position.lat.toFixed(4)}, ${t.position.lon.toFixed(4)}</a>
              <span class="hint">${t.position.source === 'gps' ? 'live GPS fix' : 'computed from timetable'}</span></dd>
        </dl>
      </div>`;
    $('result').hidden = false;
    $('err').hidden = true;
  }

  function renderList(trains) {
    if (!trains.length) { $('list').innerHTML = '<p class="hint">No delays reported.</p>'; return; }
    $('list').innerHTML = trains.map((t) => {
      const st = t.measured?.near_station;
      return `<button class="row" data-n="${esc(t.number)}">
        <span class="rn">${esc(t.category)} ${esc(t.number)}</span>
        <span class="rs">${esc(st ? st.name : '—')}</span>
        <span class="rd s-${esc(t.status)}">${t.delay_min} min</span>
      </button>`;
    }).join('');
    $('list').querySelectorAll('.row').forEach((b) =>
      b.addEventListener('click', () => { $('q').value = b.dataset.n; lookup(b.dataset.n); }));
  }

  function renderMeta(meta) {
    if (!meta) return;
    const age = meta.age_seconds == null ? '?' : `${meta.age_seconds}s`;
    $('meta').textContent = `${meta.train_count} trains tracked · data ${age} old · polled every ${meta.poll_seconds}s${meta.stale ? ' · STALE' : ''}`;
  }

  // ---- data ------------------------------------------------------------
  async function lookup(num) {
    $('err').hidden = true;
    try {
      const r = await fetch(`/api/train/${encodeURIComponent(num)}`);
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      renderTrain(d.train); renderMeta(d.meta);
    } catch (e) {
      $('result').hidden = true;
      $('err').textContent = e.message;
      $('err').hidden = false;
    }
  }

  async function loadBoard() {
    try {
      const r = await fetch('/api/trains?delayed_only=1&limit=12');
      const d = await r.json();
      renderList(d.trains); renderMeta(d.meta);
    } catch {
      $('list').innerHTML = '<p class="hint">Unavailable offline — showing nothing new.</p>';
    }
  }

  // ---- wiring ----------------------------------------------------------
  $('search').addEventListener('submit', (e) => {
    e.preventDefault();
    const v = $('q').value.trim();
    if (v) lookup(v);
  });
  $('refresh').addEventListener('click', loadBoard);

  const netPill = () => { $('net-pill').hidden = navigator.onLine; };
  addEventListener('online', () => { netPill(); loadBoard(); });
  addEventListener('offline', netPill);
  netPill();

  loadBoard();
  setInterval(loadBoard, 60000);

  if ('serviceWorker' in navigator && isSecureContext) {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
  }
})();
