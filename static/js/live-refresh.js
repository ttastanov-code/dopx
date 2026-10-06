// static/js/live-refresh.js
//
// Незаметное обновление контента, как табло на бирже: меняются только отличающиеся узлы,
// новое значение мягко проявляется (без вспышек и перерисовки блоков).
//
// 1. Страница целиком: раз в N секунд фоном запрашиваем её же и сливаем <main> через Idiomorph.
//    Настройка: <body data-live-refresh="30"> (секунды) или "off"; в админке — window.DOPX_LIVE.
// 2. Фоновые HTMX-опросы и ленивые блоки: вместо замены outerHTML — то же слияние.
// 3. Пинг версии данных (core/live.py) раз в 3 с: изменились данные — обновляем сразу.
//
// Не трогаются: поля форм, [data-live-ignore], состояние <details>, самообновляемые
// HTMX-блоки (у них свой опрос), Alpine-компоненты (своё состояние на клиенте).
// Пропуск тика: вкладка скрыта, форма изменена, поле в фокусе, выделен текст,
// открыто модальное окно, пользователь только что взаимодействовал.
(function () {
    if (!window.fetch || !window.DOMParser) return;

    const QUIET_AFTER_INTERACTION_MS = 4000;
    const MIN_INTERVAL_S = 10;
    const TICK_MS = 450;
    const FORM_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT', 'OPTION']);

    const override = window.DOPX_LIVE || {};
    let lastInteraction = 0;
    let dirtyForms = false;
    let inFlight = false;
    let timer = null;

    function intervalSeconds() {
        const raw = String((document.body && document.body.dataset.liveRefresh) || override.interval || '');
        if (raw === 'off') return null;
        if (/\/(add|change|password|delete)\/?$/.test(location.pathname)) return null; // формы админки
        const value = parseInt(raw, 10);
        return Number.isFinite(value) ? Math.max(MIN_INTERVAL_S, value) : 60;
    }

    function targetSelector() {
        return (document.body && document.body.dataset.liveTarget) || override.target || 'main';
    }

    function shouldSkip() {
        if (document.visibilityState !== 'visible') return true;
        if (dirtyForms) return true;
        if (Date.now() - lastInteraction < QUIET_AFTER_INTERACTION_MS) return true;
        const active = document.activeElement;
        if (active && (FORM_TAGS.has(active.tagName) || active.isContentEditable)) return true;
        const selection = window.getSelection && window.getSelection();
        if (selection && String(selection).trim()) return true;
        if (document.querySelector('dialog[open], .modal-open, .modal.modal-open')) return true;
        return false;
    }

    function isSelfUpdating(el) {
        return el.hasAttribute('hx-get') && el.hasAttribute('hx-trigger');
    }

    // Слияние с «проявлением» изменившихся значений. root — узел, который разрешено
    // сливать, даже если он сам самообновляемый или Alpine (фоновый опрос этого узла).
    function softMorph(current, fresh, root) {
        const changed = [];
        const added = [];
        const scrollX = window.scrollX, scrollY = window.scrollY;
        Idiomorph.morph(current, fresh, {
            morphStyle: 'outerHTML',
            ignoreActiveValue: true,
            callbacks: {
                beforeNodeMorphed(oldNode, newNode) {
                    if (oldNode.nodeType !== 1) return true;
                    if (oldNode.hasAttribute('data-live-ignore') || oldNode.id === 'djDebug') return false;
                    if (FORM_TAGS.has(oldNode.tagName)) return false; // ввод пользователя не трогаем
                    // Отсчёт форматирует клиент (live-time.js); сливаем, только если сдвинулся срок.
                    if (oldNode.hasAttribute('data-countdown')
                        && oldNode.getAttribute('data-countdown') === newNode.getAttribute('data-countdown')) return false;
                    // Текст/классы, которые рисует Alpine, — клиентские; серверный вариант не подставляем.
                    if (oldNode.hasAttribute('x-text') || oldNode.hasAttribute('x-html')) return false;
                    if (oldNode !== root && oldNode.hasAttribute('data-live-src')) return false;
                    if (oldNode !== root) {
                        // Самообновляемый блок или ленивая заглушка вместо уже загруженного — обновится сам.
                        if (isSelfUpdating(oldNode) || isSelfUpdating(newNode)) return false;
                        if (oldNode.hasAttribute('x-data')) return false; // состояние Alpine на клиенте
                    } else if (oldNode.hasAttribute('x-data') && oldNode.outerHTML !== newNode.outerHTML
                               && oldNode.getAttribute('x-data') !== newNode.getAttribute('x-data')) {
                        // Опрашиваемый Alpine-блок сменил тип (например, матч начался) — заменяем целиком.
                        oldNode.replaceWith(newNode.cloneNode(true));
                        return false;
                    }
                    // Листовой элемент с новым текстом — мягко проявим после слияния.
                    if (!oldNode.firstElementChild && !newNode.firstElementChild
                        && oldNode.textContent !== newNode.textContent && newNode.textContent.trim()) {
                        changed.push(oldNode);
                    }
                    return true;
                },
                afterNodeAdded(node) {
                    // Новый элемент (событие в ленте, строка таблицы) плавно появляется.
                    if (node.nodeType === 1 && !['SCRIPT', 'STYLE', 'TEMPLATE'].includes(node.tagName)) added.push(node);
                },
                beforeNodeRemoved(node) {
                    return !(node.nodeType === 1 && (node.hasAttribute('data-live-ignore') || node.id === 'djDebug'));
                },
                beforeAttributeUpdated(attributeName, node) {
                    if (node.tagName === 'DETAILS' && attributeName === 'open') return false;
                    if (attributeName === 'data-wake-scheduled' || attributeName === 'data-countdown-expired') return false;
                    if (attributeName === 'class' && node.classList
                        && (node.classList.contains('dx-live-tick') || node.classList.contains('dx-live-pop') || node.classList.contains('dx-live-enter'))) return false;
                    if (attributeName === 'class' && (node.hasAttribute(':class') || node.hasAttribute('x-bind:class'))) return false;
                    if (attributeName === 'style' && (node.hasAttribute(':style') || node.hasAttribute('x-show'))) return false;
                    return true;
                },
            },
        });
        if (window.scrollX !== scrollX || window.scrollY !== scrollY) window.scrollTo(scrollX, scrollY);
        changed.forEach(tick);
        added.forEach((node) => animate(node, 'dx-live-enter', 500));
        return changed.length + added.length;
    }

    // Новое значение проявляется мягко — как сегмент электронного табло.
    function tick(el) {
        if (!el.isConnected || el.closest('[data-live-ignore]')) return;
        // Счёт и другие [data-live-pop] — «пружинят», как табло стадиона; остальное проявляется.
        const pop = el.closest('[data-live-pop]');
        if (pop) animate(pop, 'dx-live-pop', 900);
        else animate(el, 'dx-live-tick', TICK_MS + 50);
    }

    function animate(el, className, duration) {
        if (!el.isConnected) return;
        el.classList.remove(className);
        void el.offsetWidth; // перезапуск анимации
        el.classList.add(className);
        setTimeout(() => el.classList.remove(className), duration);
    }

    function afterUpdate(root) {
        if (window.htmx) htmx.process(root);
        document.dispatchEvent(new CustomEvent('dopx:live-updated', { detail: { root: root } }));
    }

    // ---------- 1. Страница целиком ----------
    async function refresh() {
        if (inFlight || shouldSkip() || !window.Idiomorph) return false;
        const selector = targetSelector();
        const current = document.querySelector(selector);
        if (!current) return true;
        inFlight = true;
        try {
            const response = await fetch(location.href, {
                credentials: 'same-origin',
                cache: 'no-store',
                headers: { 'X-Live-Refresh': '1', 'X-Requested-With': 'live-refresh' },
            });
            // Редирект (сессия истекла, страница переехала) или ошибка — оставляем как есть.
            if (!response.ok || response.redirected) return true;
            const html = await response.text();
            if (shouldSkip()) return false; // пока ждали ответ, пользователь начал что-то делать
            const doc = new DOMParser().parseFromString(html, 'text/html');
            if (assetsChanged(doc)) { location.reload(); return true; }
            const fresh = doc.querySelector(selector);
            if (!fresh) return true;
            const dataBefore = jsonSignature();
            softMorph(current, fresh, current);
            if (jsonSignature() !== dataBefore) rerunScripts();
            if (doc.title && doc.title !== document.title) document.title = doc.title;
            // Режим траура: атрибуты на <html> вне <main> — переносим сами.
            syncMourning(doc);
            afterUpdate(current);
            return true;
        } catch (e) {
            return false; // сеть пропала — попробуем на следующем тике
        } finally {
            inFlight = false;
        }
    }

    // ---------- 2. Фоновые HTMX-опросы: слияние вместо замены ----------
    document.addEventListener('htmx:beforeSwap', (event) => {
        const detail = event.detail;
        const target = detail.target;
        if (!window.Idiomorph || !target) return;
        const trigger = (detail.elt && detail.elt.getAttribute('hx-trigger')) || '';
        const lazy = /(^|[\s,])load([\s,]|$)/.test(trigger);
        if (!target.hasAttribute('data-background-poll') && !lazy) return;
        const swap = ((detail.elt && detail.elt.getAttribute('hx-swap')) || '').trim();
        if (detail.elt !== target) return;
        // Ленивый контейнер (innerHTML): htmx вставит содержимое сам; запомним адрес для перезапроса.
        if (lazy && swap.startsWith('innerHTML') && detail.pathInfo && detail.pathInfo.requestPath) {
            target.setAttribute('data-live-src', detail.pathInfo.requestPath);
            return;
        }
        // Только блоки, заменяющие сами себя (outerHTML): иначе фрагмент встал бы на место контейнера.
        if (!swap.startsWith('outerHTML')) return;
        if (!detail.shouldSwap || !detail.xhr || detail.xhr.status !== 200) return;
        const template = document.createElement('template');
        template.innerHTML = (detail.serverResponse || '').trim();
        const fresh = template.content.firstElementChild;
        if (!fresh || template.content.childElementCount !== 1) return; // не наш формат — обычная замена
        detail.shouldSwap = false;
        // Загруженный контент без своего опроса запоминает адрес — перезапросим при смене версии данных.
        if (!fresh.hasAttribute('hx-get') && detail.pathInfo && detail.pathInfo.requestPath) {
            fresh.setAttribute('data-live-src', detail.pathInfo.requestPath);
        }
        softMorph(target, fresh, target);
        const node = fresh.id ? document.getElementById(fresh.id) || target : target;
        afterUpdate(node);
    });

    // ---------- Режим траура: переключение ровно в момент начала/конца периода ----------
    let mourningTimer = null;

    function syncMourning(doc) {
        const root = document.documentElement, fresh = doc.documentElement;
        root.toggleAttribute('data-mourning', fresh.hasAttribute('data-mourning'));
        const next = fresh.getAttribute('data-mourning-next');
        if (next) root.setAttribute('data-mourning-next', next); else root.removeAttribute('data-mourning-next');
        scheduleMourning();
    }

    function scheduleMourning() {
        clearTimeout(mourningTimer);
        const at = Date.parse(document.documentElement.getAttribute('data-mourning-next') || '');
        if (Number.isNaN(at)) return;
        const delay = at - Date.now() + 1000; // секунда запаса: сервер уже считает новый режим
        if (delay > 2147483647) return;
        mourningTimer = setTimeout(async function retry() {
            // Переключение важно не пропустить: если пользователь сейчас занят — повторяем.
            if (!(await refresh())) mourningTimer = setTimeout(retry, 3000);
        }, Math.max(0, delay));
    }

    // ---------- 3. Версия данных: пинг раз в VERSION_POLL_MS, при смене — мягкое обновление ----------
    const VERSION_POLL_MS = 3000;
    let knownVersion = null;
    let pendingVersion = false;

    async function refreshLiveSources() {
        for (const el of Array.from(document.querySelectorAll('[data-live-src]'))) {
            const url = el.getAttribute('data-live-src');
            try {
                const response = await fetch(url, {
                    credentials: 'same-origin', cache: 'no-store',
                    headers: { 'HX-Request': 'true', 'X-Live-Refresh': '1' },
                });
                if (!response.ok || response.redirected || !el.isConnected) continue;
                const html = (await response.text()).trim();
                let fresh;
                if ((el.getAttribute('hx-swap') || '').startsWith('innerHTML')) {
                    // Контейнер остаётся тем же — сливаем только содержимое.
                    fresh = el.cloneNode(false);
                    fresh.innerHTML = html;
                } else {
                    const template = document.createElement('template');
                    template.innerHTML = html;
                    fresh = template.content.firstElementChild;
                    if (!fresh || template.content.childElementCount !== 1) continue;
                    fresh.setAttribute('data-live-src', url);
                }
                softMorph(el, fresh, el);
                afterUpdate(el);
            } catch (e) { /* следующая смена версии */ }
        }
    }

    async function pollVersion() {
        if (!intervalSeconds() || document.visibilityState !== 'visible') return;
        try {
            const response = await fetch('/api/live-version/', { credentials: 'same-origin', cache: 'no-store', headers: { 'X-Live-Refresh': '1' } });
            if (!response.ok || response.redirected) return;
            const version = String((await response.json()).v);
            if (knownVersion === null) knownVersion = version;
            else if (version !== knownVersion) {
                knownVersion = version;
                pendingVersion = true;
                // Live-блоки (счёт, события) — сразу и даже во время скролла: их опрос сливает узлы мягко.
                if (window.dopxWakeAll) window.dopxWakeAll(true);
            }
        } catch (e) { return; }
        if (!pendingVersion || shouldSkip()) return;
        const applied = await refresh();
        await refreshLiveSources();
        if (applied) pendingVersion = false;
    }

    // Версия нашего кода в <script src="...?v=">: изменилась после деплоя — нужен новый JS.
    function assetVersions(root) {
        return Array.from(root.querySelectorAll('script[src*="/static/js/"]'), (el) => el.getAttribute('src')).sort().join('|');
    }
    const initialAssets = assetVersions(document);
    function assetsChanged(doc) {
        const fresh = assetVersions(doc);
        return fresh && initialAssets && fresh !== initialAssets && !dirtyForms;
    }

    // Данные графиков (json_script) — если изменились, перезапускаем скрипты с data-live-rerun.
    function jsonSignature() {
        return Array.from(document.querySelectorAll('script[type="application/json"]'), (el) => el.id + el.textContent).join('|');
    }

    function rerunScripts() {
        document.querySelectorAll('script[data-live-rerun]').forEach((old) => {
            const script = document.createElement('script');
            script.textContent = old.textContent;
            script.setAttribute('data-live-rerun', '');
            old.replaceWith(script);
        });
    }

    function schedule() {
        clearInterval(timer);
        const seconds = intervalSeconds();
        if (!seconds) return;
        timer = setInterval(refresh, seconds * 1000);
    }

    function markInteraction() { lastInteraction = Date.now(); }

    document.addEventListener('DOMContentLoaded', () => {
        knownVersion = (document.body && document.body.dataset.liveVersion) || null;
        schedule();
        scheduleMourning();
        setInterval(pollVersion, VERSION_POLL_MS);
    });
    ['pointerdown', 'keydown', 'wheel', 'touchstart'].forEach((type) => {
        document.addEventListener(type, markInteraction, { passive: true, capture: true });
    });
    document.addEventListener('input', (event) => {
        if (event.target && event.target.form) dirtyForms = true;
    }, true);
    document.addEventListener('submit', () => { dirtyForms = false; }, true);
    // Вернулись на вкладку — обновляем сразу, не дожидаясь тика.
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') setTimeout(refresh, 800);
    });
    window.dopxLiveRefresh = refresh;
})();
