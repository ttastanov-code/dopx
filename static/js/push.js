// static/js/push.js
//
// Регистрирует /sw.js на каждой странице. Подписка на push — только по кнопке
// (window.dopxSubscribePush), без автозапроса разрешения.
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

    // Статус подписки именно этого браузера — PushManager.getSubscription().
    window.dopxPushStatus = async function (csrfToken) {
        if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
            return 'unsupported';
        }
        // serviceWorker.ready может висеть вечно — ограничиваем таймаутом (статус 'timeout').
        const timeout = new Promise((resolve) => setTimeout(() => resolve('timeout'), 4000));
        try {
            const result = await Promise.race([
                (async () => {
                    const registration = await navigator.serviceWorker.ready;
                    const subscription = await registration.pushManager.getSubscription();
                    if (!subscription) return 'idle';
                    // Подписка браузера есть, а записи в БД может не быть — переотправляем её на сервер
                    // (push_subscribe идемпотентен по endpoint).
                    if (csrfToken) {
                        try {
                            await fetch('/users/push/subscribe/', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                                body: JSON.stringify(subscription.toJSON()),
                            });
                        } catch (err) {
                            console.warn('DOPX: push self-heal re-register failed', err);
                        }
                    }
                    return 'subscribed';
                })(),
                timeout,
            ]);
            if (result === 'timeout') {
                console.warn('DOPX: service worker не стал active за 4с — проверьте вкладку Application > Service Workers и консоль на ошибки регистрации /sw.js');
            }
            return result;
        } catch (err) {
            console.warn('DOPX: push status check failed', err);
            return 'idle';
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
