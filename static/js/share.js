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

    // Засчитать задание дня «Поделитесь» (гостю сервер ответит 204 без действий).
    function reportShare() {
        const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        fetch('/api/share-done/', {
            method: 'POST', credentials: 'same-origin', keepalive: true,
            headers: { 'X-CSRFToken': m ? decodeURIComponent(m[1]) : '' },
        }).catch(() => {});
    }

    // Сторис: только картинка, без текста — так Instagram сразу открывает её как историю.
    async function shareStory(btn) {
        const url = btn.dataset.story;
        const touch = window.matchMedia('(pointer: coarse)').matches;
        if (touch && navigator.share && navigator.canShare) {
            try {
                const blob = await (await fetch(url, { credentials: 'same-origin' })).blob();
                const file = new File([blob], 'dopx-story.png', { type: blob.type || 'image/png' });
                if (navigator.canShare({ files: [file] })) { await navigator.share({ files: [file] }); return; }
            } catch (e) { if (e.name === 'AbortError') return; }
        }
        window.open(url, '_blank', 'noopener');
    }

    document.addEventListener('click', async (event) => {
        if (event.target.closest('.dx-share')) reportShare();
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
            const image = btn.dataset.shareImage;
            // Файл — только на телефонах: десктопный Chrome отдаёт получателю удалённый временный файл.
            const touch = window.matchMedia('(pointer: coarse)').matches;
            if (image && touch && navigator.canShare) {
                try {
                    const blob = await (await fetch(image, { credentials: 'same-origin' })).blob();
                    const file = new File([blob], 'dopx.png', { type: blob.type || 'image/png' });
                    if (navigator.canShare({ files: [file] })) data.files = [file];
                } catch (e) { /* без картинки — только ссылка */ }
            }
            try { await navigator.share(data); return; } catch (e) { if (e.name === 'AbortError') return; }
        }
        copy(url, btn);
    });
})();
