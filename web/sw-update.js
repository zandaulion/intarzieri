// The update mechanism, as a drop-in for a service worker that already exists.
//
// This deliberately does not touch caching. Each app's fetch strategy is its
// own business -- some precache a shell, some keep an API cache for offline
// reading -- and replacing that wholesale to gain an update flow would trade a
// real feature for a nicer deploy.
//
// In the app's sw.js:
//
//     importScripts('/sw-update.js');
//
//     self.addEventListener('activate', (e) => {
//       e.waitUntil((async () => {
//         ...whatever this app already does...
//         await self.clients.claim();
//         await announceUpdate();          // <- last, after claiming
//       })());
//     });

// How long a window gets to answer before it is reloaded out from under
// itself. Long enough for a page to reply, short enough that a wedged one is
// not left on old code.
const PWA_ACK_GRACE_MS = 4000;

const pwaAcked = new Set();

self.addEventListener('message', (event) => {
  if (event.data?.type === 'sw-update-ack' && event.source) pwaAcked.add(event.source.id);
  if (event.data?.type === 'sw-skip-waiting') self.skipWaiting();
});

/**
 * Tell every open window that a new version is now in charge.
 *
 * Announcing rather than acting is the point. The obvious implementation calls
 * client.navigate() the moment the worker activates, which is instant and also
 * discards whatever the person was in the middle of -- a half-written entry, a
 * sheet they had not saved. The page knows whether that is true; the worker
 * does not. So the page decides, and says so by acknowledging.
 *
 * A window that stays silent is reloaded anyway. That covers the client still
 * running the code from before this existed, and the one that has wedged --
 * neither should sit on stale code indefinitely.
 */
async function announceUpdate() {
  const windows = await self.clients.matchAll({ type: 'window' });
  if (!windows.length) return;

  pwaAcked.clear();
  for (const client of windows) {
    client.postMessage({ type: 'sw-updated' });
  }

  await new Promise((resolve) => setTimeout(resolve, PWA_ACK_GRACE_MS));

  // Re-read: a page that reloaded itself in the meantime is a different client
  // now, and must not be navigated a second time.
  const remaining = await self.clients.matchAll({ type: 'window' });
  for (const client of remaining) {
    if (pwaAcked.has(client.id)) continue;
    if (typeof client.navigate !== 'function') continue;
    try {
      // Back to where they were, not to the root: an app with real routes
      // would otherwise lose the person's place on every deploy.
      await client.navigate(client.url);
    } catch {
      // navigate() rejects for a client that is no longer navigable. Nothing
      // to be done, and it must not take the activation down with it.
    }
  }
}
