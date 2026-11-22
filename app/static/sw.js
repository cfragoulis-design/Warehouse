/* Kiosk cache. Scope is the whole origin because this file is served at /sw.js. */
const CACHE = 'wh-kiosk-v1';

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(['/static/sr-theme.css'])));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

function cacheable(url, request) {
  if (request.method !== 'GET') return false;
  return url.pathname === '/consumables/take'
    || url.pathname === '/consumables/take/stock'
    || url.pathname.endsWith('/image')
    || url.pathname.indexOf('/consumables/category-image/') === 0
    || url.pathname.indexOf('/static/') === 0;
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (!cacheable(url, event.request)) return;
  event.respondWith(
    fetch(event.request).then((response) => {
      const copy = response.clone();
      caches.open(CACHE).then((cache) => cache.put(event.request, copy));
      return response;
    }).catch(() => caches.match(event.request))
  );
});
