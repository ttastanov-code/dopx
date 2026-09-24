// static/sw.js
// Service worker: установка на экран «Домой» и Web Push.
// Кешируется только статика, HTML — никогда (устаревшие формы/CSRF).
const CACHE_NAME = 'dopx-shell-v1';
const APP_SHELL = [
    '/static/pwa/icon-192.png',
    '/static/pwa/icon-512.png',
    '/static/manifest.json',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)).catch(() => {})
    );
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
        )
    );
    self.clients.claim();
});

// Cache-first только для APP_SHELL, остальное — напрямую в сеть.
self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);
    if (event.request.method !== 'GET' || !APP_SHELL.some((path) => url.pathname === path)) {
        return;
    }
    event.respondWith(
        caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
});

// === Web Push ===
self.addEventListener('push', (event) => {
    let payload = {};
    try {
        payload = event.data ? event.data.json() : {};
    } catch (e) {
        payload = { title: 'DOPX', body: event.data ? event.data.text() : '' };
    }

    const title = payload.title || 'DOPX';
    const options = {
        body: payload.body || '',
        icon: '/static/pwa/icon-192.png',
        badge: '/static/pwa/icon-192.png',
        data: { url: payload.url || '/' },
        timestamp: Date.now(),
    };
    // Одинаковый tag заменяет прошлое уведомление (например, новый гол — прошлый счёт).
    if (payload.tag) {
        options.tag = payload.tag;
        options.renotify = true;
    }

    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const targetUrl = (event.notification.data && event.notification.data.url) || '/';
    event.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
            for (const client of clientList) {
                if (client.url === targetUrl && 'focus' in client) {
                    return client.focus();
                }
            }
            if (self.clients.openWindow) {
                return self.clients.openWindow(targetUrl);
            }
        })
    );
});
