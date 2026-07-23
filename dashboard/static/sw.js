// Cache-first for static assets only - listing/match data is always live,
// so there's no meaningful "offline" experience for it to fake. Nothing is
// precached on install (e.g. icon files may not exist yet) - assets are
// cached lazily as they're actually requested, in the fetch handler below.
const CACHE_NAME = "huisjagers-static-v1";

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        if (cached) return cached;
        return fetch(event.request).then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          }
          return response;
        });
      })
    );
  }
  // Everything else (HTML pages, data) passes straight through to the
  // network - no offline fallback, since stale listing data would be
  // actively misleading.
});
