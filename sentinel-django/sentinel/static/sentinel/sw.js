/* FireVision AI service worker — shows Web Push incident alerts even when the dashboard tab is closed. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : '' };
  }
  event.waitUntil(self.registration.showNotification(data.title || 'FireVision AI alert', {
    body: data.body || 'A new incident requires attention.',
    tag: data.tag || 'sentinel-alert',
    renotify: true,
    requireInteraction: Boolean(data.requireInteraction),
    vibrate: data.requireInteraction ? [300, 120, 300, 120, 300] : [200],
    data: { url: data.url || '/' },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || '/', self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const existing = windows.find((w) => w.url.startsWith(self.location.origin));
    return existing ? existing.focus() : self.clients.openWindow(target);
  })());
});
