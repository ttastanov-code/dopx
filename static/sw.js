// static/sw.js
// Продуктовый аудит, раздел 5c ("PWA + Web Push").
//
// Сервис-воркер минимальный НАМЕРЕННО: цель этой итерации — установка
// сайта на домашний экран (installability) и доставка Web Push, а не
// полноценный офлайн-режим SPA. DOPX — по сути CRUD-сайт с формами
// (вайзард оценки матча, вход, регистрация); агрессивное офлайн-кеширование
// HTML этих страниц через service worker рискует показать пользователю
// устаревшую версию формы с CSRF-токеном от предыдущей сессии — хуже, чем
// отсутствие офлайн-режима вообще. Поэтому кешируется только статика
// (иконки/манифест), а НЕ HTML-страницы.
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

// Кешируем по принципу "cache falls back to network" ТОЛЬКО для файлов из
// APP_SHELL (статика) — любой другой запрос (HTML-страницы, HTMX-партиалы,
// POST-формы) идёт напрямую в сеть без вмешательства воркера.
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
    };

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
