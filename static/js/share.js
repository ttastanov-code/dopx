// static/js/share.js
//
// «Поделиться» в один тап: [data-share] — системное меню (на телефоне — с картинкой-карточкой,
// оттуда Instagram Stories / WhatsApp / Telegram); без Web Share — копируем ссылку.
// [data-copy="текст"] — копировать в буфер с подтверждением на кнопке.
(function () {
    function flash(btn, text) {
        if (btn._flashTimer) return;
        btn.classList.add('is-copied');
        const label = btn.querySelector('[data-share-label]');
        const icon = btn.querySelector('.ti');
        const oldText = label && label.textContent;
        const oldIcon = icon && icon.className;
        if (label) label.textContent = text;
        // Кнопка-иконка без подписи — меняем только иконку на галочку.
        if (icon) icon.className = 'ti ti-check';
        if (!label && !icon) btn.setAttribute('aria-label', text);
        btn._flashTimer = setTimeout(() => {
            if (label) label.textContent = oldText;
            if (icon) icon.className = oldIcon;
            btn.classList.remove('is-copied');
            btn._flashTimer = null;
        }, 1800);
    }

    async function copy(text, btn) {
        try { await navigator.clipboard.writeText(text); flash(btn, 'Скопировано'); }
        catch (e) { window.prompt('Скопируйте ссылку', text); }
    }

    // Картинки скачиваются заранее: Safari на iPhone открывает меню «Поделиться» только сразу после нажатия,
    // а ожидание загрузки (генерация карточки — несколько секунд) это разрешение сжигает.
    const touch = window.matchMedia('(pointer: coarse)').matches;
    const files = new Map();  // url → File или Promise
    function prefetch(url) {
        if (!url || files.has(url)) return files.get(url);
        const job = fetch(url, { credentials: 'same-origin' })
            .then((r) => { if (!r.ok) throw new Error(r.status); return r.blob(); })
            .then((b) => { const f = new File([b], 'dopx.png', { type: b.type || 'image/png' }); files.set(url, f); return f; })
            .catch(() => { files.delete(url); return null; });
        files.set(url, job);
        return job;
    }
    const ready = (url) => (files.get(url) instanceof File ? files.get(url) : null);
    const imageOf = (el) => el.dataset.story || el.dataset.shareImage;

    if (touch) {
        // По очереди после загрузки страницы и сразу при касании кнопки.
        const warm = async () => {
            for (const el of document.querySelectorAll('[data-story], [data-share-image]')) await prefetch(imageOf(el));
        };
        window.addEventListener('load', () => setTimeout(warm, 600));
        document.addEventListener('pointerdown', (e) => {
            const el = e.target.closest('[data-story], [data-share-image]');
            if (el) prefetch(imageOf(el));
        }, { passive: true });
    }

    // Картинка ещё готовится: на кнопке крутится загрузка, по готовности — обычная иконка, нажать снова.
    function waitFor(btn, url) {
        const icon = btn.querySelector('.ti');
        if (!icon || btn.classList.contains('is-loading')) return;
        const old = icon.className;
        btn.classList.add('is-loading');
        btn.setAttribute('aria-busy', 'true');
        icon.className = 'ti ti-loader-2 dx-spin';
        Promise.resolve(prefetch(url)).then(() => {
            icon.className = old;
            btn.classList.remove('is-loading');
            btn.removeAttribute('aria-busy');
        });
    }

    // Сторис: только картинка, без текста — так Instagram сразу открывает её как историю.
    function shareStory(btn) {
        const url = btn.dataset.story;
        if (touch && navigator.share && navigator.canShare) {
            const file = ready(url);
            if (!file) { waitFor(btn, url); return; }
            if (navigator.canShare({ files: [file] })) { navigator.share({ files: [file] }).catch(() => {}); return; }
        }
        window.open(url, '_blank', 'noopener');
    }

    document.addEventListener('click', (event) => {
        const copyBtn = event.target.closest('[data-copy]');
        if (copyBtn) { event.preventDefault(); copy(copyBtn.dataset.copy, copyBtn); return; }

        const storyBtn = event.target.closest('[data-story]');
        if (storyBtn) { event.preventDefault(); shareStory(storyBtn); return; }

        const btn = event.target.closest('[data-share]');
        if (!btn) return;
        event.preventDefault();
        const url = btn.dataset.shareUrl || location.href;
        const text = btn.dataset.shareText || '';
        if (navigator.share) {
            // Ссылка — в тексте отдельной строкой: часть мессенджеров склеивает url и text без пробела.
            const data = { text: text ? text + '\n' + url : url };
            // Файл — только на телефонах и только если уже скачан (без ожидания после нажатия).
            const file = touch ? ready(btn.dataset.shareImage) : null;
            if (file && navigator.canShare && navigator.canShare({ files: [file] })) data.files = [file];
            navigator.share(data).catch((e) => { if (e.name !== 'AbortError') copy(url, btn); });
            return;
        }
        copy(url, btn);
    });
})();
