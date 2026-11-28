/* KHP Panel v6.1.11 — service worker.
   Cache STATIC ASSETS ONLY (app shell: CSS / JS / logo / icons).
   NEVER cache API responses: every /api/ request is network-only,
   so key / server / online data is always live. Pages (HTML) are
   network-only too, so a new deploy is picked up on the next load.
   Static files are stale-while-revalidate: serve the cached copy
   at once, refresh it in the background, so a deploy that changes
   only style.css/app.js still reaches the user without waiting for
   this file to change. Bump CACHE on every version. */
const CACHE = 'khp-panel-v6.1.11';
const SHELL = [
  '/static/style.css',
  '/static/app.js',
  '/static/logo.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/manifest.json',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return; // network-only: live data
  if (!url.pathname.startsWith('/static/')) return; // pages: network
  e.respondWith(
    caches.match(e.request).then((hit) => {
      const net = fetch(e.request).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return res;
      }).catch(() => hit);
      return hit || net;
    })
  );
});
