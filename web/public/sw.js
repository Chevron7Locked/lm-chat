/**
 * lm-chat service worker.
 *
 * Strategy:
 *   - Cache-first for /assets/* (immutable hashed Vite build output).
 *   - Network-only for /api/* and /healthz/* (SSE + API must be live).
 *   - Network-first with cache fallback for the shell (/).
 *
 * No Workbox: hand-written to keep the bundle size minimal and avoid the
 * Workbox generator peer-dep chain. The cache is keyed by CACHE_VERSION
 * so stale caches from old deployments are evicted on activation.
 */

const CACHE_VERSION = "v1";
const SHELL_CACHE = `lmchat-shell-${CACHE_VERSION}`;
const ASSET_CACHE = `lmchat-assets-${CACHE_VERSION}`;
const SHELL_URLS = ["/", "/index.html", "/globals.css"];

// ─── Install: pre-cache the shell ────────────────────────────────────────────

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) =>
      cache.addAll(SHELL_URLS).catch(() => {
        // Shell pre-cache failure is non-fatal (first install offline).
      })
    )
  );
  // Take control immediately — don't wait for old sw to unload.
  self.skipWaiting();
});

// ─── Activate: evict stale caches ────────────────────────────────────────────

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== SHELL_CACHE && k !== ASSET_CACHE)
          .map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// ─── Fetch: route-aware strategy ─────────────────────────────────────────────

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Skip non-GET and cross-origin requests.
  if (request.method !== "GET" || url.origin !== self.location.origin) {
    return;
  }

  // Network-only: API and health endpoints (includes SSE streams).
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/healthz")) {
    return; // Let the browser handle it; don't intercept.
  }

  // Cache-first: hashed Vite assets under /assets/.
  if (url.pathname.startsWith("/assets/")) {
    event.respondWith(
      caches.open(ASSET_CACHE).then(async (cache) => {
        const cached = await cache.match(request);
        if (cached !== undefined) return cached;
        const response = await fetch(request);
        if (response.ok) {
          cache.put(request, response.clone());
        }
        return response;
      })
    );
    return;
  }

  // Network-first with shell fallback for navigation requests.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() =>
        caches.match("/index.html").then((r) => r ?? Response.error())
      )
    );
    return;
  }

  // Default: network-first, cache on success.
  event.respondWith(
    caches.open(SHELL_CACHE).then(async (cache) => {
      try {
        const response = await fetch(request);
        if (response.ok) cache.put(request, response.clone());
        return response;
      } catch {
        const cached = await cache.match(request);
        return cached ?? Response.error();
      }
    })
  );
});
