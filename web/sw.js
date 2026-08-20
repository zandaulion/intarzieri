/* Shell cache-first; API network-first so a stale delay is better than none. */
'use strict';
const VERSION = '__BUILD_VERSION__';
const SHELL_CACHE = 'trains-shell-' + VERSION;
const API_CACHE = 'trains-api';

const SHELL = ['/', '/index.html', '/app.css', '/app.js', '/manifest.webmanifest',
               '/icons/icon.svg', '/icons/icon-192.png', '/icons/icon-512.png',
               // Precached: a push can arrive offline, and a badge that 404s
               // leaves Android drawing the Chrome logo instead.
               '/icons/badge-96.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL_CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k.startsWith('trains-shell-') && k !== SHELL_CACHE)
                          .map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

async function apiNetworkFirst(req) {
  const cache = await caches.open(API_CACHE);
  try {
    const fresh = await fetch(req);
    if (fresh.ok) cache.put(req, fresh.clone());
    return fresh;
  } catch (err) {
    const hit = await cache.match(req);
    if (hit) return hit;
    return new Response(JSON.stringify({ detail: 'Offline and no cached data yet.' }),
      { status: 503, headers: { 'Content-Type': 'application/json' } });
  }
}

async function shellCacheFirst(req) {
  const cache = await caches.open(SHELL_CACHE);
  const hit = await cache.match(req);
  if (hit) return hit;
  try {
    const res = await fetch(req);
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch (err) {
    if (req.mode === 'navigate') return cache.match('/index.html');
    throw err;
  }
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  // Trip subscribe/list/delete are POSTs and must never be cached or replayed.
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  e.respondWith(url.pathname.startsWith('/api/') ? apiNetworkFirst(req) : shellCacheFirst(req));
});


/* ------------------------------------------------------------ push ----- */
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = {}; }
  const title = d.title || 'Train update';
  e.waitUntil(self.registration.showNotification(title, {
    body: d.body || '',
    // Tag per trip+kind so a re-sent update replaces the old bubble instead
    // of stacking another one on the lock screen.
    tag: d.tag || 'train',
    renotify: true,
    icon: '/icons/icon-192.png',
    badge: '/icons/badge-96.png',
    data: d,
    vibrate: d.kind === 'delay' ? [90, 60, 90] : [140],
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) {
      if (new URL(c.url).origin === self.location.origin) return c.focus();
    }
    return self.clients.openWindow('/');
  })());
});

/* The browser may replace a push subscription at any time. Until this was
   handled, a rotation stranded the device's trips behind a dead endpoint and
   notifications simply stopped, silently. The device cookie rides along with
   this fetch, so the server knows whose subscription to update. */
self.addEventListener('pushsubscriptionchange', (e) => {
  e.waitUntil((async () => {
    try {
      const old = e.oldSubscription || await self.registration.pushManager.getSubscription();
      let fresh = e.newSubscription;
      if (!fresh) {
        const key = old && old.options && old.options.applicationServerKey;
        if (!key) return;
        fresh = await self.registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: key,
        });
      }
      await fetch('/api/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subscription: fresh.toJSON() }),
      });
    } catch (err) {
      // Nothing useful to do here; the next app open re-registers.
    }
  })());
});
