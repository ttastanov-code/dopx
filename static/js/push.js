// static/js/push.js
// Продуктовый аудит, раздел 5c ("PWA + Web Push").
//
// Регистрирует /sw.js на КАЖДОЙ странице (нужен и для installability
// манифеста, и для получения push вне зависимости от того, где конкретно
// пользователь нажал "включить уведомления"). Подписка на push (запрос
// разрешения браузера + PushManager.subscribe) вызывается ТОЛЬКО явным
// действием пользователя — window.dopxSubscribePush(), вызывается из
// кнопки на странице настроек уведомлений, НЕ автоматически при загрузке
// страницы. Автозапрос разрешения на уведомления при заходе на сайт —
// один из самых раздражающих анти-паттернов веба, конверсия в согласие
// у него в разы ниже, чем у запроса "по требованию" в осознанный момент.
(function () {
    if (!('serviceWorker' in navigator)) return;

    window.addEventListener('load', () => {
        navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch((err) => {
            console.warn('DOPX: service worker registration failed', err);
        });
    });

    function urlBase64ToUint8Array(base64String) {
        const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
        const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
        const rawData = window.atob(base64);
        return Uint8Array.from([...rawData].map((c) => c.charCodeAt(0)));
    }

    window.dopxSubscribePush = async function (vapidPublicKey, csrfToken) {
        if (!('PushManager' in window)) {
            return { ok: false, reason: 'unsupported' };
        }
        try {
            const registration = await navigator.serviceWorker.ready;
            const permission = await Notification.requestPermission();
            if (permission !== 'granted') {
                return { ok: false, reason: 'denied' };
            }
            const subscription = await registration.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
            });
            const res = await fetch('/users/push/subscribe/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                body: JSON.stringify(subscription.toJSON()),
            });
            return { ok: res.ok };
        } catch (err) {
            console.warn('DOPX: push subscribe failed', err);
            return { ok: false, reason: 'error' };
        }
    };

    window.dopxUnsubscribePush = async function (csrfToken) {
        try {
            const registration = await navigator.serviceWorker.ready;
            const subscription = await registration.pushManager.getSubscription();
            if (subscription) {
                await fetch('/users/push/unsubscribe/', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                    body: JSON.stringify({ endpoint: subscription.endpoint }),
                });
                await subscription.unsubscribe();
            }
            return { ok: true };
        } catch (err) {
            console.warn('DOPX: push unsubscribe failed', err);
            return { ok: false };
        }
    };
})();
