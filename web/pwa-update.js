// The page half of the update mechanism.
//
// The worker takes over as soon as it installs and then tells every open
// window. This decides when each window acts on that: immediately, or once the
// person has finished what they were doing.

const RELOADED_KEY = 'pwa-kit:updated';

/**
 * A message, for an app that has no way of showing one.
 *
 * Several of these apps have no toast, no banner, nothing -- and an update that
 * happens in silence is indistinguishable from the app restarting on its own,
 * which is unsettling rather than reassuring. So the kit brings its own.
 *
 * Styled inline and scoped to one element, so adopting the kit never means
 * adopting a stylesheet or a class name that might collide. It follows the
 * viewer's colour scheme and respects a preference for less motion.
 */
function defaultToast(message) {
  const el = document.createElement('div');
  el.setAttribute('role', 'status');
  el.textContent = message;
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
  Object.assign(el.style, {
    position: 'fixed',
    top: 'max(14px, calc(env(safe-area-inset-top) + 10px))',
    left: '50%',
    transform: 'translateX(-50%)',
    background: dark ? '#EFF0E6' : '#1B1D17',
    color: dark ? '#1B1D17' : '#F6F7F0',
    padding: '11px 18px',
    borderRadius: '99px',
    font: '500 15px/1.3 system-ui, -apple-system, sans-serif',
    boxShadow: '0 6px 24px rgba(0,0,0,.28)',
    maxWidth: '90vw',
    textAlign: 'center',
    zIndex: '2147483647',
    opacity: still ? '1' : '0',
    transition: still ? 'none' : 'opacity .2s ease'
  });
  document.body.appendChild(el);
  if (!still) requestAnimationFrame(() => { el.style.opacity = '1'; });
  setTimeout(() => {
    if (still) return el.remove();
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 250);
  }, 3200);
}

/**
 * Wires an app up to its service worker.
 *
 * @param {object} options
 * @param {string} options.appName      Used in the default message.
 * @param {string} [options.message]
 *        The whole message, for an app that is not in English. Several of
 *        these are Romanian, and "Plate updated to the latest version" in the
 *        middle of a Romanian interface reads like something went wrong.
 * @param {(msg: string) => void} [options.toast]
 *        How to show that message. Defaults to a self-contained one, so an app
 *        with no notification of its own still says something.
 * @param {() => boolean} [options.isBusy]
 *        Return true while reloading would lose the person's work. The update
 *        waits until this goes false, or until they leave the tab -- at which
 *        point nothing is on screen to lose.
 * @param {string} [options.scriptUrl]  Defaults to '/sw.js'.
 */
export function installUpdates({
  appName = 'The app',
  message = null,
  toast = defaultToast,
  isBusy = () => false,
  scriptUrl = '/sw.js'
} = {}) {
  if (!('serviceWorker' in navigator)) return;

  // Announce the last reload before anything else, so the message survives
  // however long registration takes.
  if (sessionStorage.getItem(RELOADED_KEY)) {
    sessionStorage.removeItem(RELOADED_KEY);
    toast(message || `${appName} updated to the latest version`);
  }

  // Registration is deferred until the page has settled, but only if it has
  // not settled already. Waiting on 'load' unconditionally is a trap: an app
  // that calls this from an async boot() can easily reach it after load has
  // fired, and a listener added then never runs -- so the app silently has no
  // service worker at all, with nothing in the console to say so.
  const start = async () => {
    let registration;
    try {
      // updateViaCache: 'none' so the worker script itself is never answered
      // from the HTTP cache. Without it a stale sw.js can keep an app on an
      // old version for as long as its cache headers allow, which is the
      // classic way this whole mechanism silently stops working.
      registration = await navigator.serviceWorker.register(scriptUrl, { updateViaCache: 'none' });
    } catch (err) {
      console.warn('service worker did not register:', err);
      return;
    }

    registration.update().catch(() => {});

    // An installed PWA is often never closed, so the reliable moment to look
    // for a new version is when it comes back to the foreground.
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') registration.update().catch(() => {});
    });

    let reloading = false;
    const reload = () => {
      if (reloading) return;
      reloading = true;
      // Read on the way back up, by the block at the top of this function.
      sessionStorage.setItem(RELOADED_KEY, '1');
      location.reload();
    };

    navigator.serviceWorker.addEventListener('message', (event) => {
      if (event.data?.type !== 'sw-updated') return;

      // Acknowledge first. The worker reloads any window that stays silent,
      // and a page that means to reload itself in a moment should not also be
      // navigated from underneath.
      event.source?.postMessage?.({ type: 'sw-update-ack' });
      navigator.serviceWorker.controller?.postMessage({ type: 'sw-update-ack' });

      if (!isBusy()) return reload();

      // Mid-something. Wait for a moment when there is nothing to lose: either
      // the work finishes, or they leave the tab and the screen stops
      // mattering.
      const settle = setInterval(() => {
        if (!isBusy()) { clearInterval(settle); reload(); }
      }, 2000);

      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') { clearInterval(settle); reload(); }
      }, { once: true });
    });
  };

  if (document.readyState === 'complete') start();
  else window.addEventListener('load', start, { once: true });
}
