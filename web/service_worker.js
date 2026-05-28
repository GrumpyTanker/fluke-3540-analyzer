// Service worker — cache-first for static assets. Versioned by the
// CACHE name so a bump invalidates the old cache automatically.

const CACHE = 'fluke3540-v0.4.1';  // bump on every release to invalidate
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
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  // Network-first for HTML navigations and the spec JSON so a fresh deploy
  // is picked up without users having to hard-refresh. Cache-first for
  // everything else (vendor JS/CSS/SVG/PNG never change without a hash bump).
  const isHtml = req.mode === 'navigate'
    || req.destination === 'document'
    || url.pathname.endsWith('.html')
    || url.pathname.endsWith('/');
  const isFreshNeeded = isHtml || url.pathname.endsWith('field_map.json');

  if (isFreshNeeded) {
    event.respondWith(
      fetch(req).then((resp) => {
        if (resp.ok && resp.type === 'basic') {
          const copy = resp.clone();
          caches.open(CACHE).then((cache) => cache.put(req, copy));
        }
        return resp;
      }).catch(() => caches.match(req))   // offline fallback
    );
    return;
  }

  // Cache-first for static assets.
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((resp) => {
        if (resp.ok && resp.type === 'basic') {
          const copy = resp.clone();
          caches.open(CACHE).then((cache) => cache.put(req, copy));
        }
        return resp;
      }).catch(() => cached);
    })
  );
});
