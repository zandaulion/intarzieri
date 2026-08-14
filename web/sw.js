/* Shell cache-first; API network-first so a stale delay is better than none. */
'use strict';
const VERSION = '__BUILD_VERSION__';
const SHELL_CACHE = 'trains-shell-' + VERSION;
const API_CACHE = 'trains-api';

const SHELL = ['/', '/index.html', '/app.css', '/app.js', '/manifest.webmanifest',
               '/icons/icon.svg', '/icons/icon-192.png', '/icons/icon-512.png'];

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
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  e.respondWith(url.pathname.startsWith('/api/') ? apiNetworkFirst(req) : shellCacheFirst(req));
});
