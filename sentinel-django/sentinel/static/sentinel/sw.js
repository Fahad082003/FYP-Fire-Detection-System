/* FireVision AI service worker — shows Web Push incident alerts even when the dashboard tab is closed. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

/** The shown notification from a push: Firebase Cloud Messaging ({ notification, data }) or the VAPID Web Push payload. */
function fromPush(payload) {
  const n = payload.notification ?? {}, d = payload.data ?? {};
  const fcm = Boolean(payload.notification || payload.data?.incidentId);
  const title = (fcm ? n.title : payload.title) || 'FireVision AI alert';
  return [title, {
    body: (fcm ? n.body : payload.body) || 'A new incident requires attention.',
    tag: fcm ? `incident-${d.incidentId}` : payload.tag || 'sentinel-alert',
    renotify: true,
    requireInteraction: fcm ? ['High', 'Critical'].includes(d.severity) : Boolean(payload.requireInteraction),
    vibrate: [300, 120, 300],
    ...(fcm && n.image ? { image: n.image } : {}),
    data: { url: (fcm ? d.url || payload.fcmOptions?.link : payload.url) || '/' },
  }];
}

self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = { body: event.data ? event.data.text() : '' };
  }
  event.waitUntil(self.registration.showNotification(...fromPush(payload)));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || '/', self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const existing = windows.find((w) => w.url.startsWith(self.location.origin));
    if (!existing) return self.clients.openWindow(target);
    await existing.focus();
    return existing.url === target ? existing : existing.navigate(target); // the incident the alert is about, not just any tab
  })());
});
