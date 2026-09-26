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
        setTimeout(autoResync, 3000);
    });

    // Раз в сутки тихо сверяем подписку с сервером: браузер мог сменить endpoint,
    // а сервер — удалить запись как мёртвую. Разрешение не запрашиваем.
    const RESYNC_KEY = 'dopx:push-resync';
    const RESYNC_EVERY_MS = 24 * 60 * 60 * 1000;
    function autoResync() {
        if (document.body.dataset.auth !== '1' || !('Notification' in window) || Notification.permission !== 'granted') return;
        try {
            if (Date.now() - Number(localStorage.getItem(RESYNC_KEY) || 0) < RESYNC_EVERY_MS) return;
            localStorage.setItem(RESYNC_KEY, String(Date.now()));
        } catch (e) { /* без localStorage — сверяем каждый раз, это дёшево */ }
        let csrf = '';
        try { csrf = JSON.parse(document.body.getAttribute('hx-headers') || '{}')['X-CSRFToken'] || ''; } catch (e) { /* нет токена */ }
        if (csrf && window.dopxPushStatus) window.dopxPushStatus(csrf);
    }

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
            window.dopxPushEndpoint = subscription.endpoint;
            const ok = await sendSubscription(subscription, csrfToken);
            notifyDevicesChanged();
            return { ok };
        } catch (err) {
            console.warn('DOPX: push subscribe failed', err);
            return { ok: false, reason: 'error' };
        }
    };

    // Сообщает странице, что список устройств на сервере изменился.
    function notifyDevicesChanged() {
        document.dispatchEvent(new CustomEvent('dopx:push-devices-changed'));
    }

    async function sendSubscription(subscription, csrfToken) {
        const res = await fetch('/users/push/subscribe/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify(subscription.toJSON()),
        });
        const data = res.ok ? await res.json() : {};
        if (data.created) notifyDevicesChanged();
        return res.ok;
    }

    // Подписка сделана под другим VAPID-ключом — push на неё не доходят.
    function keyMismatch(subscription) {
        const vapidKey = document.body.dataset.vapidPublicKey;
        const current = subscription.options && subscription.options.applicationServerKey;
        if (!vapidKey || !current) return false;
        const expected = urlBase64ToUint8Array(vapidKey);
        const actual = new Uint8Array(current);
        return expected.length !== actual.length || expected.some((b, i) => b !== actual[i]);
    }

    // Статус подписки именно этого браузера — PushManager.getSubscription().
    // Endpoint текущей подписки — в window.dopxPushEndpoint.
    window.dopxPushStatus = async function (csrfToken) {
        window.dopxPushEndpoint = null;
        if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
            return 'unsupported';
        }
        // serviceWorker.ready может висеть вечно — ограничиваем таймаутом (статус 'timeout').
        const timeout = new Promise((resolve) => setTimeout(() => resolve('timeout'), 4000));
        try {
            const result = await Promise.race([
                (async () => {
                    const registration = await navigator.serviceWorker.ready;
                    let subscription = await registration.pushManager.getSubscription();
                    if (!subscription) return 'idle';
                    if (keyMismatch(subscription) && Notification.permission === 'granted') {
                        await subscription.unsubscribe();
                        subscription = await registration.pushManager.subscribe({
                            userVisibleOnly: true,
                            applicationServerKey: urlBase64ToUint8Array(document.body.dataset.vapidPublicKey),
                        });
                    }
                    window.dopxPushEndpoint = subscription.endpoint;
                    // Записи в БД может не быть (удалена как мёртвая) — переотправляем, subscribe идемпотентен.
                    if (csrfToken) {
                        try {
                            await sendSubscription(subscription, csrfToken);
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
            window.dopxPushEndpoint = null;
            notifyDevicesChanged();
            return { ok: true };
        } catch (err) {
            console.warn('DOPX: push unsubscribe failed', err);
            return { ok: false };
        }
    };
})();
