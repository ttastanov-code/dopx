// static/js/live-time.js
//
// Живое время без перезагрузки страницы:
//  - [data-countdown="ISO"] — текст «5 ч 10 мин» обновляется раз в 30 с; по истечении —
//    data-countdown-done (или «сейчас») и пробуждение фоновых блоков (dopx:wake);
//  - [data-wake-at="ISO"] — блок, который пока не опрашивается (матч далеко), получает
//    dopx:wake в указанный момент и перерисовывается сам — уже с нужным опросом.
(function () {
    const TICK_MS = 30000;
    // setTimeout не принимает задержку больше ~24.8 дня.
    const MAX_TIMEOUT_MS = 2147483647;

    function plural(n, forms) {
        const mod10 = n % 10, mod100 = n % 100;
        if (mod10 === 1 && mod100 !== 11) return forms[0];
        if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return forms[1];
        return forms[2];
    }

    function formatLeft(ms) {
        if (ms < 60000) return `${Math.max(1, Math.ceil(ms / 1000))} с`;
        const minutes = Math.round(ms / 60000);
        const days = Math.floor(minutes / 1440);
        const hours = Math.floor((minutes % 1440) / 60);
        const mins = minutes % 60;
        if (days >= 1) {
            const d = `${days} ${plural(days, ['день', 'дня', 'дней'])}`;
            return hours ? `${d} ${hours} ч` : d;
        }
        if (hours >= 1) return mins ? `${hours} ч ${mins} мин` : `${hours} ч`;
        return `${mins} мин`;
    }

    function wakeAll() {
        if (!window.htmx) return;
        const now = Date.now();
        document.querySelectorAll('[data-background-poll][hx-trigger*="dopx:wake"]').forEach((el) => {
            const wakeAt = el.dataset.wakeAt && Date.parse(el.dataset.wakeAt);
            if (wakeAt && wakeAt > now) return; // ещё рано — у него свой таймер
            htmx.trigger(el, 'dopx:wake');
        });
    }

    function tickCountdowns() {
        let expired = false;
        document.querySelectorAll('[data-countdown]').forEach((el) => {
            const deadline = Date.parse(el.dataset.countdown);
            if (Number.isNaN(deadline)) return;
            const left = deadline - Date.now();
            if (left > 0) {
                // Приставка («Закроется через ») — внутри элемента, чтобы по истечении заменялась вся фраза.
                el.textContent = (el.dataset.countdownPrefix || '') + formatLeft(left);
                delete el.dataset.countdownExpired;
                return;
            }
            if (!el.dataset.countdownExpired) {
                el.dataset.countdownExpired = '1';
                el.textContent = el.dataset.countdownDone || 'сейчас';
                expired = true;
            }
        });
        if (expired) setTimeout(wakeAll, 1500); // сервер успевает обновить статус
    }

    function scheduleWakes(root) {
        (root || document).querySelectorAll('[data-wake-at]:not([data-wake-scheduled])').forEach((el) => {
            const at = Date.parse(el.dataset.wakeAt);
            if (Number.isNaN(at)) return;
            el.dataset.wakeScheduled = '1';
            const delay = Math.max(0, at - Date.now()) + Math.random() * 5000; // разнос запросов
            if (delay > MAX_TIMEOUT_MS) return;
            setTimeout(() => {
                if (el.isConnected && window.htmx) htmx.trigger(el, 'dopx:wake');
            }, delay);
        });
    }

    function refresh() {
        tickCountdowns();
        scheduleWakes();
    }

    document.addEventListener('DOMContentLoaded', refresh);
    document.addEventListener('htmx:afterSettle', refresh);
    document.addEventListener('dopx:live-updated', refresh);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') refresh();
    });
    setInterval(tickCountdowns, TICK_MS);
    setInterval(() => {
        const soon = Array.from(document.querySelectorAll('[data-countdown]:not([data-countdown-expired])'))
            .some((el) => Date.parse(el.dataset.countdown) - Date.now() < 90000);
        if (soon) tickCountdowns();
    }, 1000);
    window.dopxWakeAll = wakeAll;
})();
