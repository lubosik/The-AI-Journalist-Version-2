/* HERALD Intelligence service worker */
const CACHE_NAME = 'herald-v2-cache-v2';
const STATIC_ASSETS = ['/', '/public/herald.css', '/public/manifest.json'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS).catch(() => {}))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('push', (event) => {
  const data = event.data ? event.data.json() : {};
  const title = data.title || 'HERALD Intelligence';
  const body = data.body || 'New content available';
  const tag = data.tag || 'herald-notification';
  const notificationData = data.data || {};

  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: '/public/herald-logo.svg',
      badge: '/public/herald-logo.svg',
      tag,
      requireInteraction: data.important || false,
      data: { url: notificationData.url || data.url || '/' },
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window' }).then((clientList) => {
      for (const client of clientList) {
        if (client.url === url && 'focus' in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});

self.addEventListener('fetch', (event) => {
  // Never intercept WebSocket or API requests
  if (
    event.request.url.includes('/api/') ||
    event.request.url.includes('socket.io') ||
    event.request.url.includes('ws://')
  ) {
    return;
  }
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
