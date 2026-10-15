// static/js/wizard-sliders.js
//
// Для всех .wizard-slider:
// - выставляет --wz-fill (заливка трека в wizard.css);
// - создаёт скрытое поле <name>__touched (0/1): сервер не засчитывает нетронутые критерии;
// - у нетронутого критерия вместо числа прочерк, на шаг — одна заметка.
(function () {
    function updateFill(el) {
        const min = parseFloat(el.min || '0');
        const max = parseFloat(el.max || '100');
        const val = parseFloat(el.value);
        const pct = max > min ? ((val - min) / (max - min)) * 100 : 0;
        // Поправка на ширину ручки (6px): заливка кончается ровно под ней.
        el.style.setProperty('--wz-fill', `calc(3px + (100% - 6px) * ${pct / 100})`);
        const ticks = el._wzTicks;
        if (ticks) ticks.forEach((t) => t.classList.toggle('is-on', parseFloat(t.dataset.v) <= val));
    }

    // Деления шкалы: не больше 11 точек (0–100 с шагом 5 — каждые 10).
    function addTicks(el) {
        const card = el.closest('.wz-slider-card');
        if (!card || el._wzTicks) return;
        const min = parseFloat(el.min || '0'), max = parseFloat(el.max || '100'), step = parseFloat(el.step || '1');
        let every = step;
        while ((max - min) / every > 10) every += step;
        const box = document.createElement('div');
        box.className = 'wz-ticks';
        box.setAttribute('aria-hidden', 'true');
        const list = [];
        for (let v = min; v <= max + 1e-9; v += every) {
            const i = document.createElement('i');
            i.dataset.v = v;
            i.style.setProperty('--k', (v - min) / (max - min));
            box.appendChild(i);
            list.push(i);
        }
        el.insertAdjacentElement('beforebegin', box);
        el._wzTicks = list;
    }

    // Заметка шага по умолчанию; шаблоны переопределяют её через data-wz-untouched-hint.
    const DEFAULT_HINT = 'Засчитывается только то, что вы оценили кнопкой или ползунком.'

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
        // Нетронутый критерий: прочерк вместо «5», значение появится при первом движении.
        card.classList.add('wz-slider-card--untouched');
        const value = card.querySelector('.wz-slider-card__value');
        if (value) {
            value.textContent = '—';
            value.dataset.max = el.max || '10';  // «8/10» — масштаб шкалы рядом со значением
        }
        addFormNote(el);

        el.addEventListener('input', function () {
            hidden.value = '1';
            card.classList.remove('wz-slider-card--untouched');
            if (value) {
                if (value.textContent !== el.value) {
                    value.classList.add('is-bump');
                    clearTimeout(value._bump);
                    value._bump = setTimeout(() => value.classList.remove('is-bump'), 140);
                }
                value.textContent = el.value;
            }
        });
    }

    // Одна заметка на шаг вместо подсказки под каждым ползунком.
    function addFormNote(el) {
        const scope = el.closest('[data-wz-untouched-hint]');
        if (!scope || scope.dataset.wzNoteAdded) {
            return;
        }
        scope.dataset.wzNoteAdded = '1';
        const note = document.createElement('p');
        note.className = 'wz-note';
        note.textContent = scope.dataset.wzUntouchedHint || DEFAULT_HINT;
        scope.insertAdjacentElement('afterbegin', note);
    }

    function initAll(root) {
        (root || document).querySelectorAll('.wizard-slider').forEach(function (el) {
            addTicks(el);
            updateFill(el);
            ensureTouchedTracking(el);
        });
    }

    // Лёгкий отклик на смену деления (Android; на iPhone вибрация из браузера недоступна).
    const canBuzz = 'vibrate' in navigator && window.matchMedia('(pointer: coarse)').matches;
    document.addEventListener('input', function (e) {
        if (e.target && e.target.classList && e.target.classList.contains('wizard-slider')) {
            updateFill(e.target);
            if (canBuzz && e.target.dataset.wzLast !== e.target.value) {
                e.target.dataset.wzLast = e.target.value;
                navigator.vibrate(6);
            }
        }
    });

    // Пресеты режима «Быстро»: data-wz-preset-value — процент (0-100) шкалы каждого слайдера
    // в [data-wz-preset-scope]. data-wz-invert="true" — слайдер двигается в обратную сторону.
    // Шлём настоящее 'input', чтобы сработали все обработчики.
    // Ручное движение ползунка после быстрой кнопки — это уже своя оценка: подсветку кнопки снимаем.
    let applyingPreset = false;
    document.addEventListener('input', function (e) {
        if (applyingPreset || !e.target.classList || !e.target.classList.contains('wizard-slider')) {
            return;
        }
        const scope = e.target.closest('[data-wz-preset-scope]');
        if (!scope) {
            return;
        }
        scope.querySelectorAll('[data-wz-preset-value].btn-primary').forEach(function (b) {
            b.classList.remove('btn-primary');
            b.classList.add('btn-outline');
        });
    });

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
        applyingPreset = true;
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
        applyingPreset = false;
        scope.querySelectorAll('[data-wz-preset-value]').forEach(function (b) {
            b.classList.toggle('btn-primary', b === btn);
            b.classList.toggle('btn-outline', b !== btn);
        });
    });

    // Пальцем: тап по капсуле ставит значение, горизонтальный жест тянет; вертикальный — прокрутка страницы (touch-action: pan-y).
    // Нужно для iPhone: Safari двигает ползунок только за саму ручку, а она тонкая.
    function setFromX(el, clientX) {
        const r = el.getBoundingClientRect();
        const min = parseFloat(el.min || '0'), max = parseFloat(el.max || '100'), step = parseFloat(el.step || '1');
        const pct = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
        const v = Math.min(max, Math.max(min, Math.round((min + pct * (max - min)) / step) * step));
        if (String(v) !== el.value || el.closest('.wz-slider-card--untouched')) {
            el.value = v;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }
    let drag = null;
    document.addEventListener('pointerdown', function (e) {
        if (e.pointerType !== 'touch' || !e.target.classList || !e.target.classList.contains('wizard-slider')) return;
        drag = { el: e.target, x: e.clientX, y: e.clientY, moved: false };
        const card = e.target.closest('.wz-slider-card');
        if (card) card.classList.add('is-dragging');
    }, { passive: true });
    document.addEventListener('pointermove', function (e) {
        if (!drag) return;
        if (!drag.moved && Math.abs(e.clientX - drag.x) > 6 && Math.abs(e.clientX - drag.x) > Math.abs(e.clientY - drag.y)) drag.moved = true;
        if (drag.moved) setFromX(drag.el, e.clientX);
    }, { passive: true });
    document.addEventListener('pointerup', function (e) {
        if (!drag) return;
        if (!drag.moved && Math.abs(e.clientY - drag.y) < 8) setFromX(drag.el, e.clientX);
        endDrag();
    });
    document.addEventListener('pointercancel', endDrag);
    function endDrag() {
        if (drag) { const card = drag.el.closest('.wz-slider-card'); if (card) card.classList.remove('is-dragging'); }
        drag = null;
    }

    document.addEventListener('DOMContentLoaded', function () { initAll(); });
    // Слайдеры в свёрнутых карточках появляются позже — инициализируем повторно.
    document.addEventListener('alpine:init', function () { setTimeout(initAll, 0); });
})();
