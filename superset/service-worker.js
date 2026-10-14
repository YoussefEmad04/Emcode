// Superset 6.1.0 references this file but omits it from the release image.
// Keep the local PoC worker intentionally inert: no caching and no external calls.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
