// static/sw.js
// Service worker: установка на экран «Домой» и Web Push.
// Кешируется только статика, HTML — никогда (устаревшие формы/CSRF).
const CACHE_NAME = 'dopx-shell-v2';
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
        // Значок в статус-баре Android — монохромный, иначе белый квадрат.
        badge: '/static/pwa/badge-96.png',
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
    const target = new URL((event.notification.data && event.notification.data.url) || '/', self.location.origin).href;
    event.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
            // Уже открыта эта страница — фокус; открыт сайт — переходим в той же вкладке.
            const exact = clientList.find((c) => c.url === target);
            if (exact && 'focus' in exact) return exact.focus();
            const sameSite = clientList.find((c) => new URL(c.url).origin === self.location.origin && 'navigate' in c);
            if (sameSite) return sameSite.navigate(target).then((c) => (c || sameSite).focus());
            if (self.clients.openWindow) return self.clients.openWindow(target);
        })
    );
});

// Браузер сменил подписку — переподписываемся тем же ключом; на сервер её
// отправит push.js при следующем открытии сайта.
self.addEventListener('pushsubscriptionchange', (event) => {
    const options = event.oldSubscription && event.oldSubscription.options;
    if (!options || !options.applicationServerKey) return;
    event.waitUntil(self.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: options.applicationServerKey,
    }));
});
