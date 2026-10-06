// static/js/responsive-tables.js
//
// Таблицы на телефоне — карточками, без прокрутки вбок (стили .rt в css/ui.css):
// колонка «#» — значок места, следующая (или первая) — заголовок карточки, длинный текст — блоком,
// остальные ячейки — компактные плашки «подпись значение». Таблица с data-table-keep остаётся таблицей.
(function () {
    const RANK_HEADS = ['#', '№', ''];
    const WIDE_TEXT = 32;

    function prepare(table) {
        if (table.dataset.rtReady || table.hasAttribute('data-table-keep')) return;
        const heads = [...table.querySelectorAll('thead th')].map((th) => th.innerText.trim());
        if (!heads.length) return;
        const rows = [...table.querySelectorAll('tbody tr')];
        // Первая колонка — номер места, если заголовок «#»/пусто и значения короткие.
        const rankFirst = heads.length > 2 && RANK_HEADS.includes(heads[0]) &&
            rows.every((tr) => !tr.children[0] || tr.children[0].innerText.trim().length <= 4);
        const titleCol = rankFirst ? 1 : 0;
        // Колонка с длинным текстом хоть в одной строке — блоком во всех строках (без разнобоя).
        const wideCols = new Set();
        rows.forEach((tr) => [...tr.children].forEach((td, i) => {
            if (td.colSpan === 1 && td.innerText.trim().length > WIDE_TEXT) wideCols.add(i);
        }));
        rows.forEach((tr) => {
            let col = 0;
            [...tr.children].forEach((td) => {
                if (td.colSpan > 1) { td.classList.add('rt-full'); col += td.colSpan; return; }
                if (!td.hasAttribute('data-label')) td.setAttribute('data-label', heads[col] || '');
                if (rankFirst && col === 0) td.classList.add('rt-rank');
                else if (col === titleCol) td.classList.add('rt-title');
                else if (wideCols.has(col)) td.classList.add('rt-wide');
                else if (!td.innerText.trim() && td.querySelector('a, button, form')) td.classList.add('rt-action');
                col += 1;
            });
        });
        table.classList.add('rt');
        if (rankFirst) listMode(table, heads, rows, titleCol);
        table.dataset.rtReady = '1';
    }

    // Элемент с именем (последний, у кого есть собственный текст) — детали встают под ником, а не под аватаром.
    function nameHolder(cell) {
        let found = cell;
        cell.querySelectorAll('*').forEach((el) => {
            if ([...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) found = el;
        });
        if (found !== cell && getComputedStyle(found).display === 'inline') found.style.display = 'block';
        if (found !== cell) found.style.minWidth = '0';
        return found;
    }

    // Рейтинги — компактными строками: место, имя, под ним одна строка мелких данных, справа главная цифра.
    const VALUE_HEAD = /рейтинг|trust|очк|score|точност|средн/i;
    function listMode(table, heads, rows, titleCol) {
        let valueCol = heads.findIndex((h, i) => i > titleCol && VALUE_HEAD.test(h));
        if (valueCol < 0) valueCol = heads.length - 1;
        table.classList.add('rt--list');
        rows.forEach((tr) => {
            const cells = [...tr.children];
            const title = cells[titleCol];
            if (!title || tr.querySelector('.rt-sub')) return;
            const parts = [];
            cells.forEach((td, i) => {
                if (i === 0 || i === titleCol || td.colSpan > 1) return;
                if (i === valueCol) { td.classList.add('rt-value'); return; }
                td.classList.add('rt-hide');
                const text = td.innerText.trim().replace(/\s+/g, ' ');
                if (!text || text === '—' || text === '-' || text.length > 24) return;
                // Число — с подписью («Матчей 5»), текст — как есть («Каспий»), он понятен сам.
                const numeric = /^[\d\s.,%+−-]+$/.test(text);
                // Неразрывный пробел: «Уровень 4» не разъезжается по строкам.
                parts.push((numeric && heads[i] ? `${heads[i]} ${text}` : text).replace(/ /g, '\u00a0'));
            });
            if (parts.length) {
                const sub = document.createElement('span');
                sub.className = 'rt-sub';
                sub.textContent = parts.join(' · ');
                nameHolder(title).appendChild(sub);
            }
        });
    }
    function scan(root) { (root || document).querySelectorAll('table.table').forEach(prepare); }
    document.addEventListener('DOMContentLoaded', () => scan());
    // Фрагменты, подгруженные HTMX, и мягкое обновление страницы.
    document.addEventListener('htmx:afterSettle', (e) => scan(e.target));
    document.addEventListener('dopx:refreshed', () => scan());
})();
