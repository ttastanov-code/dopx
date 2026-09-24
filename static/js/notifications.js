// static/js/notifications.js
//
// Число непрочитанных в заголовке вкладки. Источник — #notif-unread-badge
// (поллинг каждые 30 с + OOB-обновление). Слушатель вешается один раз.
(function () {
    const ORIGINAL_TITLE = document.querySelector('meta[name="original-title"]')?.content || 'DOPX';

    function readCountFromBadge(el) {
        if (!el || el.classList.contains('hidden')) return 0;
        const n = parseInt((el.textContent || '').trim(), 10);
        return Number.isNaN(n) ? 0 : n;
    }

    function updateTitle(count) {
        document.title = count > 0 ? `(${count}) ${ORIGINAL_TITLE}` : ORIGINAL_TITLE;
    }

    document.body.addEventListener('htmx:afterSwap', function (event) {
        const target = event.detail.target;
        if (!target || target.id !== 'notif-unread-badge') return;
        updateTitle(readCountFromBadge(target));
    });

    // Начальное значение — из отрендеренного бейджа.
    document.addEventListener('DOMContentLoaded', function () {
        updateTitle(readCountFromBadge(document.getElementById('notif-unread-badge')));
    });
})();
