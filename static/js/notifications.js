// static/js/notifications.js
//
// Обновление заголовка вкладки браузера числом непрочитанных уведомлений —
// ПЕРЕСОБРАНО 2026-08-21 (по прямому запросу пользователя на редизайн UI
// уведомлений). Раньше этот же функционал жил ДВУМЯ независимыми копиями
// инлайн-<script> — в components/_notification_badge.html и в
// templates/notifications/list.html — и обе слушали htmx:afterSwap на
// элементе с id="notification-badge-container", которого не существовало
// НИГДЕ в DOM (колокольчик подключался в _navbar.html обычным
// {% include %}, без единого hx-get/hx-trigger вообще). Итог: "живого"
// обновления заголовка вкладки не было вообще, только на полной
// перезагрузке страницы.
//
// Теперь единственный источник правды — компонент #notif-unread-badge
// (components/_notification_unread_badge.html), который РЕАЛЬНО свапается:
// сам поллит себя каждые 30с (`hx-trigger="every 30s"`) и обновляется
// мгновенно out-of-band сразу после отметки уведомления прочитанным
// (notifications/views.py::_oob_counters_html). Слушатель здесь ОДИН,
// навешан один раз при загрузке страницы (не при каждом swap — если бы
// этот код жил в самом partial'е, который свапается, слушатель дублировался
// бы при каждом поллинге).
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

    // Значение при первой загрузке страницы (до первого swap/поллинга) —
    // сервер уже отрендерил актуальное число в самом badge.
    document.addEventListener('DOMContentLoaded', function () {
        updateTitle(readCountFromBadge(document.getElementById('notif-unread-badge')));
    });
})();
