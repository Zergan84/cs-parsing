// Couchsurfing PWA Cache Buster Service Worker
self.addEventListener('install', (evt) => {
  self.skipWaiting();
});

self.addEventListener('activate', (evt) => {
  evt.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (evt) => {
  // Always fetch fresh from network with no-store
  evt.respondWith(
    fetch(evt.request, { cache: 'no-store' }).catch(() => fetch(evt.request))
  );
});
