const CACHE = "crm-moura-v5";
const ASSETS = ["/", "/static/appearance.js", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png"];
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});
self.addEventListener("fetch", (event) => {
  // Não intercepta chamadas de API — sempre busca a rede para dados atualizados
  if (event.request.url.includes("/api/")) return;
  // Só gerencia GET (POST/PUT/DELETE não são cacheáveis)
  if (event.request.method !== "GET") return;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        // Rede OK: atualiza a cópia em cache para o fallback offline ficar sempre atual
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy)).catch(() => {});
        }
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
/* ── Push Notifications ── */
self.addEventListener("push", (event) => {
  let data = { title: "CRM Moura", body: "Você tem uma notificação.", url: "/" };
  try {
    if (event.data) data = event.data.json();
  } catch (e) {}
  const options = {
    body: data.body,
    icon: "/icon-192.png",
    badge: "/icon-192.png",
    vibrate: [200, 100, 200],
    data: { url: data.url || "/" },
    tag: data.tag || "crm-moura-notif",
  };
  event.waitUntil(self.registration.showNotification(data.title, options));
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (client.url.includes(self.location.origin) && "focus" in client) {
          client.navigate(url);
          return client.focus();
        }
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});
