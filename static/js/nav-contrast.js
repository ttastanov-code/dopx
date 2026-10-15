// static/js/nav-contrast.js
// Стеклянное верхнее меню: над тёмным фоном всё меню разом — тёмное стекло и светлый текст.
(() => {
  const nav = document.querySelector('[data-glass-nav]');
  if (!nav) return;

  const parse = (c) => {
    const m = c && c.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const [r, g, b, a = 1] = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return { r, g, b, a };
  };
  const lum = ({ r, g, b }) => (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;

  // Тон под точкой: первый непрозрачный фон; у картинок/градиентов — по цвету текста секции.
  const toneAt = (x, y) => {
    const el = document.elementsFromPoint(x, y).find((e) => !nav.contains(e));
    for (let e = el; e && e !== document.documentElement; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (e.dataset && e.dataset.navTone) return e.dataset.navTone;
      const bg = parse(cs.backgroundColor);
      if (bg && bg.a > 0.6) return lum(bg) < 0.45 ? 'dark' : 'light';
      if (cs.backgroundImage !== 'none') {
        const fg = parse(cs.color);
        if (fg) return lum(fg) > 0.6 ? 'dark' : 'light';
      }
    }
    const body = parse(getComputedStyle(document.body).backgroundColor);
    return body && lum(body) < 0.45 ? 'dark' : 'light';
  };

  let queued = false;
  // Меню меняется целиком: тёмный фон под заметной частью ширины — тёмное стекло и светлый текст.
  const update = () => {
    queued = false;
    // В тёмной теме текст меню и так светлый.
    if (document.documentElement.getAttribute('data-theme') === 'dark') { nav.classList.remove('dx-nav--on-dark'); return; }
    const y = nav.getBoundingClientRect().bottom + 2;
    const w = window.innerWidth;
    const xs = Array.from({ length: 11 }, (_, i) => w * (0.04 + i * 0.092));
    const darkShare = xs.map((x) => toneAt(x, y)).filter((t) => t === 'dark').length / xs.length;
    const dark = darkShare >= 0.4;
    nav.classList.toggle('dx-nav--on-dark', dark);
  };
  const schedule = () => { if (!queued) { queued = true; requestAnimationFrame(update); } };

  window.addEventListener('scroll', schedule, { passive: true });
  window.addEventListener('resize', schedule);
  document.addEventListener('htmx:afterSettle', schedule);
  new MutationObserver(schedule).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  schedule();
})();
