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
//
// ДОБАВЛЕНО (2026-09-04, анти-шум оценок — см.
// docs/adr/0005-anti-noise-touched-tracking.md): нативный <input type="range">
// физически не может быть "пустым" — при отправке формы всегда уходит
// какое-то значение, даже если пользователь ни разу его не тронул. До этой
// правки часть базы данных, вероятно, состояла из "5 из 10" не потому что
// кто-то так решил, а потому что кто-то нажал "Далее", не разглядывая
// ползунки. Здесь — сторона клиента этого фикса: на каждый .wizard-slider
// заводится скрытое поле-спутник "<name>__touched" (изначально "0", "1"
// после первого реального input-события), плюс визуальная подсказка,
// пока ползунок не тронут. Сервер (evaluations/views.py) читает эти поля
// и решает, что делать с нетронутыми критериями — сам по себе этот файл
// ничего не знает про бизнес-правила, только собирает и показывает факт
// "тронуто/не тронуто".
(function () {
    function updateFill(el) {
        const min = parseFloat(el.min || '0');
        const max = parseFloat(el.max || '100');
        const val = parseFloat(el.value);
        const pct = max > min ? ((val - min) / (max - min)) * 100 : 0;
        el.style.setProperty('--wz-fill', pct + '%');
    }

    // Дефолтная подсказка — нейтральная, ничего не утверждает про то, что
    // будет с оценкой. Конкретные шаблоны (teams/coaches/referee/players),
    // где нетронутый критерий реально не будет засчитан, переопределяют
    // текст через data-wz-untouched-hint на ближайшем предке (обычно
    // <form>) — см. docs/adr/0005-anti-noise-touched-tracking.md.
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

    // Быстрые пресеты режима "Быстро" (см.
    // docs/adr/0006-quick-full-evaluation-mode.md) — кнопка "Слабо/
    // Нормально/Сильно" выставляет ВСЕ .wizard-slider внутри ближайшего
    // [data-wz-preset-scope] одним кликом. data-wz-preset-value — ПРОЦЕНТ
    // (0-100) позиции на шкале КАЖДОГО слайдера, а не абсолютное число:
    // на шаге "Судья" одна кнопка одновременно управляет слайдером 0-100
    // (влияние) и слайдером 1-10 (качество решений) — общий процент
    // переводится в значение под конкретный min/max каждого слайдера,
    // округляясь до ближайшего шага (step).
    //
    // Диспатчим настоящее 'input'-событие на каждый слайдер, а не пишем
    // в них напрямую — так автоматически срабатывают И существующие
    // inline oninput (обновление текста значения), И updateFill выше, И
    // markTouched из ensureTouchedTracking, ни один из которых не нужно
    // дублировать здесь вручную.
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
            let raw = min + ((max - min) * pct) / 100;
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
    // x-collapse в _player_card.html разворачивает слайдеры не сразу при
    // загрузке страницы (evaluate=false по умолчанию) — довешиваем
    // повторную инициализацию на 'alpine:init' и через небольшую задержку
    // на случай динамического разворачивания карточки.
    document.addEventListener('alpine:init', function () { setTimeout(initAll, 0); });
})();
