// static/js/wizard-sliders.js
//
// Для всех .wizard-slider:
// - выставляет --wz-fill (заливка трека в wizard.css);
// - создаёт скрытое поле <name>__touched (0/1) и подсказку, пока ползунок не тронут —
//   сервер не засчитывает нетронутые критерии.
(function () {
    function updateFill(el) {
        const min = parseFloat(el.min || '0');
        const max = parseFloat(el.max || '100');
        const val = parseFloat(el.value);
        const pct = max > min ? ((val - min) / (max - min)) * 100 : 0;
        el.style.setProperty('--wz-fill', pct + '%');
    }

    // Нейтральная подсказка; шаблоны переопределяют её через data-wz-untouched-hint.
    const DEFAULT_HINT = 'Потяните, чтобы указать своё мнение';

    function ensureTouchedTracking(el) {
        if (el.dataset.wzTouchedInit || !el.name) {
            return;
        }
        el.dataset.wzTouchedInit = '1';

        const hidden = document.createElement('input');
        hidden.type = 'hidden';
        hidden.name = el.name + '__touched';
        hidden.value = '0';
        el.insertAdjacentElement('afterend', hidden);

        const card = el.closest('.wz-slider-card');
        if (!card) {
            return;
        }
        card.classList.add('wz-slider-card--untouched');

        const hintSource = el.closest('[data-wz-untouched-hint]');
        const hint = document.createElement('div');
        hint.className = 'wz-slider-card__untouched-hint';
        hint.textContent = hintSource ? hintSource.dataset.wzUntouchedHint : DEFAULT_HINT;
        card.appendChild(hint);

        el.addEventListener('input', function markTouched() {
            hidden.value = '1';
            card.classList.remove('wz-slider-card--untouched');
            hint.remove();
            el.removeEventListener('input', markTouched);
        });
    }

    function initAll(root) {
        (root || document).querySelectorAll('.wizard-slider').forEach(function (el) {
            updateFill(el);
            ensureTouchedTracking(el);
        });
    }

    document.addEventListener('input', function (e) {
        if (e.target && e.target.classList && e.target.classList.contains('wizard-slider')) {
            updateFill(e.target);
        }
    });

    // Пресеты режима «Быстро»: data-wz-preset-value — процент (0-100) шкалы каждого слайдера
    // в [data-wz-preset-scope]. data-wz-invert="true" — слайдер двигается в обратную сторону.
    // Шлём настоящее 'input', чтобы сработали все обработчики.
    document.addEventListener('click', function (e) {
        const btn = e.target.closest('[data-wz-preset-value]');
        if (!btn) {
            return;
        }
        const scope = btn.closest('[data-wz-preset-scope]');
        if (!scope) {
            return;
        }
        const pct = parseFloat(btn.dataset.wzPresetValue);
        scope.querySelectorAll('.wizard-slider').forEach(function (slider) {
            const min = parseFloat(slider.min || '0');
            const max = parseFloat(slider.max || '100');
            const step = parseFloat(slider.step || '1');
            const effectivePct = slider.dataset.wzInvert === 'true' ? (100 - pct) : pct;
            let raw = min + ((max - min) * effectivePct) / 100;
            raw = Math.round(raw / step) * step;
            slider.value = Math.min(max, Math.max(min, raw));
            slider.dispatchEvent(new Event('input', { bubbles: true }));
        });
        scope.querySelectorAll('[data-wz-preset-value]').forEach(function (b) {
            b.classList.toggle('btn-primary', b === btn);
            b.classList.toggle('btn-outline', b !== btn);
        });
    });

    document.addEventListener('DOMContentLoaded', function () { initAll(); });
    // Слайдеры в свёрнутых карточках появляются позже — инициализируем повторно.
    document.addEventListener('alpine:init', function () { setTimeout(initAll, 0); });
})();
