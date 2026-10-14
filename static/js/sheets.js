// static/js/sheets.js
// Окна снизу (шторки меню, уведомления, выбор игрока) и модалки на телефоне:
// - страница под открытым окном не прокручивается и не сдвигается (body фиксируется, позиция запоминается);
// - окно закрывается свайпом вниз: следует за пальцем, уходит вниз или возвращается на место.
(() => {
  const narrow = () => window.matchMedia('(max-width: 1023px)').matches;
  const phone = () => window.matchMedia('(max-width: 639px)').matches;

  // ---------- Блокировка прокрутки страницы ----------
  let locked = false;
  let savedY = 0;

  function setLock(on) {
    if (on === locked) return;
    const root = document.documentElement;
    if (on) {
      savedY = window.scrollY;
      document.body.style.top = `-${savedY}px`;
      root.classList.add('dx-scroll-lock');
    } else {
      root.classList.remove('dx-scroll-lock');
      document.body.style.top = '';
      // Мгновенно, без плавной прокрутки: страница остаётся там, где была.
      window.scrollTo({ top: savedY, left: 0, behavior: 'instant' });
    }
    locked = on;
  }

  function anyOpen() {
    if (!narrow()) return false;
    if (document.querySelector('.dx-drawer.is-open, .modal.modal-open')) return true;
    const pop = document.querySelector('.nf-pop');
    return !!pop && phone() && getComputedStyle(pop).display !== 'none';
  }

  let queued = false;
  const sync = () => { queued = false; setLock(anyOpen()); };
  const schedule = () => { if (!queued) { queued = true; requestAnimationFrame(sync); } };
  new MutationObserver(schedule).observe(document.body, { subtree: true, attributes: true, attributeFilter: ['class', 'style'] });
  window.addEventListener('resize', schedule);

  // ---------- Свайп вниз ----------
  // panel — окно, scroll — его прокручиваемая часть, close — кнопка закрытия (иначе Escape, его слушают модалки).
  const TARGETS = [
    { panel: '.dx-drawer__panel', scroll: '.dx-drawer__scroll', close: '.dx-drawer__close', backdrop: '.dx-drawer__backdrop', when: narrow },
    { panel: '.nf-pop', scroll: '.nf-panel__list', close: '.nf-panel__close-m', backdrop: '.nf-backdrop', when: phone },
    { panel: '.modal-box', scroll: null, close: null, backdrop: null, when: narrow },
  ];
  const DISMISS_DISTANCE = 110;
  const DISMISS_SPEED = 0.6; // px/мс
  const START_SLOP = 8;

  let st = null;

  function findTarget(el) {
    for (const t of TARGETS) {
      const panel = el.closest(t.panel);
      if (panel && t.when()) return { t, panel };
    }
    return null;
  }

  function backdropOf(t, panel) {
    if (!t.backdrop) return null;
    const scope = panel.closest('.dx-drawer') || panel.parentElement;
    return scope ? scope.querySelector(t.backdrop) : null;
  }

  document.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) { st = null; return; }
    const hit = findTarget(e.target);
    if (!hit) { st = null; return; }
    // Ползунки, списки выбора и поля — свои жесты.
    if (e.target.closest('input[type="range"], select, textarea')) { st = null; return; }
    const scroller = hit.t.scroll ? hit.panel.querySelector(hit.t.scroll) : hit.panel;
    const inScroller = scroller && scroller !== hit.panel && scroller.contains(e.target);
    const atTop = !scroller || scroller.scrollTop <= 0;
    // Внутри прокрученного списка свайп вниз — это прокрутка вверх, а не закрытие.
    if (inScroller && !atTop) { st = null; return; }
    if (scroller === hit.panel && hit.panel.scrollTop > 0) { st = null; return; }
    const touch = e.touches[0];
    st = {
      ...hit, scroller, backdrop: backdropOf(hit.t, hit.panel),
      x: touch.clientX, y: touch.clientY, t0: performance.now(), dy: 0, dragging: false,
    };
  }, { passive: true });

  document.addEventListener('touchmove', (e) => {
    if (!st) return;
    const touch = e.touches[0];
    const dy = touch.clientY - st.y;
    const dx = touch.clientX - st.x;
    if (!st.dragging) {
      // Движение вверх или вбок — не наш жест.
      if (dy < -START_SLOP || Math.abs(dx) > START_SLOP * 2) { st = null; return; }
      if (dy < START_SLOP || Math.abs(dy) < Math.abs(dx) * 1.2) return;
      st.dragging = true;
      st.panel.style.transition = 'none';
      if (st.backdrop) st.backdrop.style.transition = 'none';
    }
    // Не даём странице или списку двигаться вместе с окном.
    if (e.cancelable) e.preventDefault();
    st.dy = Math.max(0, dy - START_SLOP);
    st.panel.style.transform = `translateY(${st.dy}px)`;
    if (st.backdrop) {
      const h = st.panel.getBoundingClientRect().height || 1;
      st.backdrop.style.opacity = String(Math.max(0, 1 - st.dy / h));
    }
  }, { passive: false });

  function release(cancelled) {
    if (!st) return;
    const s = st;
    st = null;
    if (!s.dragging) return;
    const speed = s.dy / Math.max(1, performance.now() - s.t0);
    if (!cancelled && (s.dy > DISMISS_DISTANCE || speed > DISMISS_SPEED)) {
      dismiss(s);
    } else {
      // Вернуть на место.
      s.panel.style.transition = 'transform .28s cubic-bezier(.16, 1, .3, 1)';
      s.panel.style.transform = 'translateY(0)';
      if (s.backdrop) { s.backdrop.style.transition = 'opacity .25s ease'; s.backdrop.style.opacity = ''; }
      setTimeout(() => { s.panel.style.transition = ''; s.panel.style.transform = ''; if (s.backdrop) s.backdrop.style.transition = ''; }, 320);
    }
  }
  document.addEventListener('touchend', () => release(false));
  document.addEventListener('touchcancel', () => release(true));

  function dismiss(s) {
    // Сначала окно уходит вниз до конца, потом обычное закрытие (кнопка или Escape) убирает его из разметки.
    s.panel.style.transition = 'transform .22s cubic-bezier(.4, 0, 1, 1)';
    s.panel.style.transform = 'translateY(110%)';
    if (s.backdrop) { s.backdrop.style.transition = 'opacity .22s ease'; s.backdrop.style.opacity = '0'; }
    setTimeout(() => {
      const btn = s.t.close ? s.panel.querySelector(s.t.close) : null;
      if (btn) btn.click();
      else window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      // Встроенные стили снимаем, когда окно уже скрыто, чтобы оно не мелькнуло.
      setTimeout(() => {
        s.panel.style.transition = ''; s.panel.style.transform = '';
        if (s.backdrop) { s.backdrop.style.transition = ''; s.backdrop.style.opacity = ''; }
      }, 520);
    }, 210);
  }
})();
