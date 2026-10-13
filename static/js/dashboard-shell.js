// static/js/dashboard-shell.js — меню и палитра команд staff-дашборда (templates/dashboard/_shell.html).
(function () {
    const body = document.body;
    const palette = document.getElementById('dsh-palette');
    const dataEl = document.getElementById('dsh-palette-data');
    if (!palette || !dataEl) return;
    const data = JSON.parse(dataEl.textContent);
    const input = palette.querySelector('input');
    const list = palette.querySelector('.dsh-palette__list');
    let rows = [];
    let sel = 0;

    // Выезжающее меню на телефоне.
    document.addEventListener('click', (e) => {
        if (e.target.closest('[data-dsh-open]')) body.classList.add('dsh-menu-open');
        else if (e.target.closest('[data-dsh-close]')) body.classList.remove('dsh-menu-open');
        else if (e.target.closest('[data-dsh-palette]')) open();
        else if (e.target === palette) close();
    });

    const norm = (s) => (s || '').toLowerCase().replace(/ё/g, 'е');
    const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

    function build(q) {
        const nq = norm(q.trim());
        const pages = data.items.filter((i) => !nq || norm(i.label + ' ' + i.hint + ' ' + i.group).includes(nq));
        rows = pages.map((i) => ({ icon: i.icon, title: i.label, sub: i.group + (i.hint ? ' · ' + i.hint : ''), url: i.url, sect: 'Разделы' }));
        // Таблицы админки — только при поиске, чтобы не забивать список.
        if (nq) (data.admin || []).filter((a) => norm(a.label + ' ' + a.hint).includes(nq)).slice(0, 8)
            .forEach((a) => rows.push({ icon: 'ti-table', title: a.label, sub: 'Админка · ' + a.hint, url: a.url, sect: 'Данные в админке' }));
        // Поиск матча и пользователя — по введённому тексту.
        if (nq) {
            const term = encodeURIComponent(q.trim());
            if (data.search.matches) rows.push({ icon: 'ti-ball-football', title: 'Найти матч: «' + q.trim() + '»', sub: 'Команда, тур или id', url: data.search.matches + '?q=' + term, sect: 'Поиск' });
            if (data.search.users) rows.push({ icon: 'ti-user-search', title: 'Найти пользователя: «' + q.trim() + '»', sub: 'Логин или email', url: data.search.users + '?q=' + term, sect: 'Поиск' });
        }
        sel = 0;
        render();
    }

    function render() {
        if (!rows.length) { list.innerHTML = '<div class="dsh-palette__empty">Ничего не найдено</div>'; return; }
        let html = '', sect = '';
        rows.forEach((r, i) => {
            if (r.sect !== sect) { sect = r.sect; html += '<div class="dsh-palette__sect">' + esc(sect) + '</div>'; }
            html += '<a class="dsh-palette__item' + (i === sel ? ' is-sel' : '') + '" role="option" data-i="' + i + '" href="' + esc(r.url) + '">' +
                '<i class="ti ' + esc(r.icon) + '"></i><span><b>' + esc(r.title) + '</b><small>' + esc(r.sub) + '</small></span></a>';
        });
        list.innerHTML = html;
        const cur = list.querySelector('.is-sel');
        if (cur) cur.scrollIntoView({ block: 'nearest' });
    }

    function open() {
        body.classList.remove('dsh-menu-open');
        palette.hidden = false;
        input.value = '';
        build('');
        setTimeout(() => input.focus(), 0);
    }
    function close() { palette.hidden = true; }

    input.addEventListener('input', () => build(input.value));
    list.addEventListener('mousemove', (e) => {
        const a = e.target.closest('[data-i]');
        if (a && +a.dataset.i !== sel) { sel = +a.dataset.i; render(); }
    });
    document.addEventListener('keydown', (e) => {
        const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) || document.activeElement.isContentEditable;
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); palette.hidden ? open() : close(); return; }
        if (e.key === '/' && !typing && palette.hidden) { e.preventDefault(); open(); return; }
        if (palette.hidden) { if (e.key === 'Escape') body.classList.remove('dsh-menu-open'); return; }
        if (e.key === 'Escape') close();
        else if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(sel + 1, rows.length - 1); render(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(sel - 1, 0); render(); }
        else if (e.key === 'Enter' && rows[sel]) { e.preventDefault(); window.location.href = rows[sel].url; }
    });

    // Индикатор связи: зелёный — сервер отвечает на опрос версии данных, красный — два промаха подряд.
    const live = document.getElementById('dsh-live');
    if (live) {
        const text = live.querySelector('.dsh-live__text');
        let lastOk = 0, lastChange = 0, fails = 0;
        const fmt = (t) => new Date(t).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const paint = () => {
            const down = fails >= 2;
            live.classList.toggle('is-wait', !lastOk && !down);
            live.classList.toggle('is-down', down);
            if (text) text.textContent = down ? 'Нет связи' : lastOk ? 'Живые данные' : 'Подключение…';
            live.title = down ? 'Сервер не отвечает — данные на странице могут быть устаревшими. Проверка продолжается.'
                : lastOk ? 'Связь есть: страница сама подтягивает изменения.\nПоследняя проверка: ' + fmt(lastOk) +
                  (lastChange ? '\nПоследнее изменение данных: ' + fmt(lastChange) : '') : 'Проверяем связь с сервером…';
        };
        document.addEventListener('dopx:live-status', (e) => {
            if (e.detail.ok) { fails = 0; lastOk = Date.now(); if (e.detail.changed) lastChange = lastOk; } else { fails += 1; }
            paint();
        });
        window.addEventListener('offline', () => { fails = 2; paint(); });
        // Страница только что пришла с сервера — связь есть с момента загрузки.
        lastOk = Date.now();
        paint();
    }

    // ⌘ на Mac, Ctrl — на остальных.
    if (!/Mac|iPhone|iPad/.test(navigator.platform)) document.querySelectorAll('.dsh-search kbd').forEach((k) => { k.textContent = 'Ctrl K'; });
})();
