/*
 * Service worker de vortexPOS (PWA).
 * Estrategia: la app se sirve SIEMPRE desde caché (arranque instantáneo y 100%
 * offline) y en segundo plano se descarga la versión nueva si la hay — el
 * siguiente arranque ya la usa (stale-while-revalidate).
 * Las llamadas a /api/ NUNCA se interceptan: van directas a la red y la propia
 * app gestiona el modo sin conexión.
 */
const CACHE = "vortexpos-app-v2.1.0";
const ASSETS = [
  "/app/",
  "/app/manifest.webmanifest",
  "/app/icon-192.png",
  "/app/icon-512.png",
  "/app/icon-512-maskable.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;            // POST/PUT: siempre red
  if (url.pathname.startsWith("/api/")) return;      // la API nunca se cachea
  if (!url.pathname.startsWith("/app")) return;      // fuera de la app: red normal

  // navegaciones dentro del scope → shell de la app
  const key = e.request.mode === "navigate" ? "/app/" : url.pathname;

  e.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(key);
      const refresh = fetch(e.request)
        .then((res) => {
          if (res && res.ok) cache.put(key, res.clone());
          return res;
        })
        .catch(() => null);                          // sin red: nos vale la caché
      return cached || refresh.then((res) => res || new Response(
        "Sin conexión y sin copia local todavía. Abre la app una vez con internet.",
        { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } }
      ));
    })
  );
});
