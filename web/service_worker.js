// Service worker — cache-first for static assets. Versioned by the
// CACHE name so a bump invalidates the old cache automatically.

const CACHE = 'fluke3540-v0.4.0';
const ASSETS = [
  './',
  './index.html',
  './app.js',
  './parser.js',
  './parser_worker.js',
  './events.js',
  './snapshots.js',
  './plots.js',
  './insights.js',
  './insights_compare.js',
  './range_select.js',
  './html_report.js',
  './pdf_export.js',
  './xlsx_export.js',
  './bundle_export.js',
  './fel.js',
  './csv_input.js',
  './cache.js',
  './multi_session.js',
  './tariff.js',
  './notes.js',
  './style.css',
  './manifest.json',
  './vendor/pico.min.css',
  './vendor/uPlot.iife.min.js',
  './vendor/uPlot.min.css',
  './vendor/xlsx.full.min.js',
  './vendor/jszip.min.js',
  './vendor/pdf-lib.min.js',
  './spec/field_map.json',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      // Install MUST succeed even if a few assets are 404 in some deploy
      // shapes — addAll() is all-or-nothing, so add individually with try.
      Promise.all(ASSETS.map((url) =>
        cache.add(url).catch(() => null)
      ))
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  // Only handle same-origin GETs — never intercept user file uploads / external requests.
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((resp) => {
        // Populate cache for runtime-fetched same-origin assets so first-load
        // works fully offline.
        if (resp.ok && resp.type === 'basic') {
          const copy = resp.clone();
          caches.open(CACHE).then((cache) => cache.put(req, copy));
        }
        return resp;
      }).catch(() => cached);  // offline, no cache → undefined
    })
  );
});
