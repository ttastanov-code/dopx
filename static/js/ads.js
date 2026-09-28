// static/js/ads.js
//
// Учёт показов рекламы: показ засчитывается, когда баннер виден хотя бы наполовину
// не меньше секунды (стандарт видимости IAB). Один баннер на странице — один показ.
(function () {
    if (!('IntersectionObserver' in window)) return;
    const timers = new WeakMap();

    function send(el) {
        if (el.dataset.adSent) return;
        el.dataset.adSent = '1';
        const url = el.dataset.adView;
        if (navigator.sendBeacon) navigator.sendBeacon(url);
        else fetch(url, { method: 'POST', keepalive: true, credentials: 'same-origin' }).catch(() => {});
        io.unobserve(el);
    }

    const io = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
            const el = entry.target;
            if (entry.isIntersecting && entry.intersectionRatio >= 0.5 && document.visibilityState === 'visible') {
                if (!timers.has(el)) timers.set(el, setTimeout(() => send(el), 1000));
            } else if (timers.has(el)) {
                clearTimeout(timers.get(el));
                timers.delete(el);
            }
        });
    }, { threshold: [0, 0.5, 1] });

    function scan(root) {
        (root || document).querySelectorAll('[data-ad-view]:not([data-ad-sent])').forEach((el) => {
            if (!el.dataset.adWatched) { el.dataset.adWatched = '1'; io.observe(el); }
        });
    }

    document.addEventListener('DOMContentLoaded', () => scan());
    document.addEventListener('htmx:afterSettle', (e) => scan(e.target));
    if (document.readyState !== 'loading') scan();
})();
