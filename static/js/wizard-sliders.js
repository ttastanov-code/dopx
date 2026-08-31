// static/js/wizard-sliders.js
//
// Кастомный трек-заливка для .wizard-slider (см. static/css/wizard.css).
// Слайдеры в вайзарде оценки живут в разных механизмах обновления —
// часть через inline oninput="...", часть через Alpine x-model
// (evaluations/_player_card.html) — переписывать каждый под единый JS-
// компонент означало бы трогать все 6+ шаблонов заново. Вместо этого
// один делегированный слушатель на document: не заменяет существующие
// обработчики (они как обновляли текст значения, так и обновляют),
// а просто ДОПОЛНИТЕЛЬНО выставляет CSS-переменную --wz-fill в процентах,
// которую использует градиент трека в wizard.css. Работает для любого
// range с классом wizard-slider независимо от того, как ещё им управляют.
(function () {
    function updateFill(el) {
        const min = parseFloat(el.min || '0');
        const max = parseFloat(el.max || '100');
        const val = parseFloat(el.value);
        const pct = max > min ? ((val - min) / (max - min)) * 100 : 0;
        el.style.setProperty('--wz-fill', pct + '%');
    }

    function initAll(root) {
        (root || document).querySelectorAll('.wizard-slider').forEach(updateFill);
    }

    document.addEventListener('input', function (e) {
        if (e.target && e.target.classList && e.target.classList.contains('wizard-slider')) {
            updateFill(e.target);
        }
    });

    document.addEventListener('DOMContentLoaded', function () { initAll(); });
    // x-collapse в _player_card.html разворачивает слайдеры не сразу при
    // загрузке страницы (evaluate=false по умолчанию) — довешиваем
    // повторную инициализацию на 'alpine:init' и через небольшую задержку
    // на случай динамического разворачивания карточки.
    document.addEventListener('alpine:init', function () { setTimeout(initAll, 0); });
})();
