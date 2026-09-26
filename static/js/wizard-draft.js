// static/js/wizard-draft.js
//
// Черновик шага вайзарда оценки: форма с data-wizard-draft сохраняется в localStorage
// при каждом изменении и восстанавливается при возврате на шаг. Ползунки — только тронутые
// (сервер не засчитывает нетронутые). После отправки шага черновик удаляется.
(function () {
    const MAX_AGE_MS = 3 * 24 * 60 * 60 * 1000;
    const SAVE_DELAY_MS = 300;

    function storageKey(form) {
        return 'dopx:wizard-draft:' + (form.getAttribute('action') || location.pathname);
    }

    function read(key) {
        try {
            const draft = JSON.parse(localStorage.getItem(key) || 'null');
            if (!draft || Date.now() - draft.savedAt > MAX_AGE_MS) return null;
            return draft;
        } catch (e) {
            return null;
        }
    }

    function write(key, fields) {
        try {
            if (Object.keys(fields).length) {
                localStorage.setItem(key, JSON.stringify({ savedAt: Date.now(), fields: fields }));
            } else {
                localStorage.removeItem(key);
            }
        } catch (e) { /* приватный режим или переполнение — без черновика */ }
    }

    function remove(key) {
        try { localStorage.removeItem(key); } catch (e) { /* без черновика */ }
    }

    function isTouchedRange(form, el) {
        const hidden = form.querySelector('input[type="hidden"][name="' + CSS.escape(el.name + '__touched') + '"]');
        return hidden && hidden.value === '1';
    }

    function collect(form) {
        const fields = {};
        form.querySelectorAll('input[name], select[name], textarea[name]').forEach(function (el) {
            if (el.name === 'csrfmiddlewaretoken' || el.name.endsWith('__touched') || el.type === 'hidden' || el.type === 'submit') return;
            if (el.type === 'range') {
                if (isTouchedRange(form, el)) fields[el.name] = el.value;
            } else if (el.type === 'checkbox') {
                if (el.checked) fields[el.name] = true;
            } else if (el.type === 'radio') {
                if (el.checked) fields[el.name] = el.value;
            } else if (el.value) {
                fields[el.name] = el.value;
            }
        });
        return fields;
    }

    // Порядок: сначала переключатели (раскрывают карточки), потом значения.
    function restore(form, fields) {
        let restored = 0;
        form.querySelectorAll('input[type="checkbox"][name], input[type="radio"][name], select[name]').forEach(function (el) {
            if (!(el.name in fields)) return;
            if (el.type === 'checkbox') {
                if (!el.checked) { el.checked = true; el.dispatchEvent(new Event('change', { bubbles: true })); restored++; }
            } else if (el.type === 'radio') {
                if (el.value === String(fields[el.name]) && !el.checked) {
                    el.checked = true; el.dispatchEvent(new Event('change', { bubbles: true })); restored++;
                }
            } else if (el.value !== String(fields[el.name])) {
                el.value = fields[el.name]; el.dispatchEvent(new Event('change', { bubbles: true })); restored++;
            }
        });
        form.querySelectorAll('input[type="range"][name], input[type="text"][name], input[type="number"][name], textarea[name]').forEach(function (el) {
            if (!(el.name in fields)) return;
            el.value = fields[el.name];
            el.dispatchEvent(new Event('input', { bubbles: true }));
            restored++;
        });
        return restored;
    }

    function showNotice(form, key) {
        const notice = document.createElement('div');
        notice.className = 'dx-draft-notice';
        notice.setAttribute('role', 'status');
        notice.innerHTML = '<i class="ti ti-device-floppy"></i><span>Восстановлен черновик этого шага</span>';
        const reset = document.createElement('button');
        reset.type = 'button';
        reset.textContent = 'Начать заново';
        reset.addEventListener('click', function () { remove(key); location.reload(); });
        notice.appendChild(reset);
        form.prepend(notice);
    }

    function init(form) {
        if (form.dataset.wizardDraftInit) return;
        form.dataset.wizardDraftInit = '1';
        const key = storageKey(form);

        const draft = read(key);
        if (draft && restore(form, draft.fields) > 0) showNotice(form, key);

        let timer = null;
        const save = function () {
            clearTimeout(timer);
            timer = setTimeout(function () { write(key, collect(form)); }, SAVE_DELAY_MS);
        };
        form.addEventListener('input', save);
        form.addEventListener('change', save);
        form.addEventListener('submit', function () { remove(key); });
    }

    // После wizard-sliders.js (скрытые __touched) и инициализации Alpine.
    function initAll() {
        document.querySelectorAll('form[data-wizard-draft]').forEach(init);
    }
    document.addEventListener('alpine:initialized', function () { setTimeout(initAll, 60); });
    window.addEventListener('load', function () { setTimeout(initAll, 120); });
})();
