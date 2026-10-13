// core/layout_check.js
// Проверка вёрстки в странице (manage.py check_mobile_layout): наложения текстов, текст за пределами
// блока и за краем экрана, обрезка многоточием, горизонтальная прокрутка страницы.
(() => {
  const vw = window.innerWidth;
  const out = { hscroll: document.documentElement.scrollWidth > vw + 1 ? document.documentElement.scrollWidth : 0, overlaps: [], spills: [], offscreen: [], truncated: [] };
  const fixedCache = new Map();
  const inFixed = (el) => {
    for (let e = el; e && e !== document.body; e = e.parentElement) {
      if (fixedCache.has(e)) return fixedCache.get(e);
      const p = getComputedStyle(e).position;
      if (p === 'fixed' || p === 'sticky') { fixedCache.set(el, true); return true; }
    }
    return false;
  };
  const visible = (el) => {
    const closed = el.closest('details:not([open])');
    if (closed && !el.closest('summary')) return false;
    for (let e = el; e && e !== document.documentElement; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) return false;
    }
    return true;
  };
  const sel = (el) => {
    const parts = [];
    for (let e = el; e && e !== document.body && parts.length < 4; e = e.parentElement) {
      let s = e.tagName.toLowerCase();
      const cls = [...e.classList].filter(c => !c.includes(':') && !c.includes('[')).slice(0, 2).join('.');
      if (cls) s += '.' + cls;
      parts.unshift(s);
    }
    return parts.join(' > ');
  };
  // Прямоугольники текстовых узлов по элементам.
  const boxes = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const byEl = new Map();
  let n;
  while ((n = walker.nextNode())) {
    if (!n.textContent.trim()) continue;
    const el = n.parentElement;
    if (!el || ['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'OPTION'].includes(el.tagName)) continue;
    if (el.closest('#djDebug, [role=tooltip], .tooltip-content, [data-tip], svg, .sr-only, dialog:not([open])')) continue;
    const r = document.createRange(); r.selectNodeContents(n);
    const rects = [...r.getClientRects()].filter(q => q.width > 1 && q.height > 1);
    if (!rects.length) continue;
    if (!byEl.has(el)) byEl.set(el, []);
    byEl.get(el).push(...rects);
  }
  for (const [el, rects] of byEl) {
    if (!visible(el) || inFixed(el)) continue;
    const sy = window.scrollY, sx = window.scrollX;
    // Обрезка предками с overflow != visible: текст за многоточием не виден.
    let clip = { l: -1e9, r: 1e9, t: -1e9, b: 1e9 };
    for (let e = el; e && e !== document.body; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') {
        const q = e.getBoundingClientRect();
        if (cs.overflowX !== 'visible') { clip.l = Math.max(clip.l, q.left); clip.r = Math.min(clip.r, q.right); }
        if (cs.overflowY !== 'visible') { clip.t = Math.max(clip.t, q.top); clip.b = Math.min(clip.b, q.bottom); }
      }
    }
    const rs = rects.map(q => ({ l: Math.max(q.left, clip.l) + sx, r: Math.min(q.right, clip.r) + sx, t: Math.max(q.top, clip.t) + sy, b: Math.min(q.bottom, clip.b) + sy }))
                    .filter(q => q.r - q.l > 1 && q.b - q.t > 1);
    if (!rs.length) continue;
    boxes.push({ el, rs, text: el.textContent.trim().slice(0, 40) });
    // Текст за правым краем экрана.
    const scroller = (() => { for (let e = el.parentElement; e && e !== document.body; e = e.parentElement) { const ox = getComputedStyle(e).overflowX; if (ox !== 'visible') return true; } return false; })();
    if (!scroller) for (const q of rs) if ((q.r > vw + 2 || q.l < -2) && q.l > -500) { out.offscreen.push({ sel: sel(el), text: el.textContent.trim().slice(0, 40), right: Math.round(q.r) }); break; }
  }
  // Наложение текстов разных элементов (не предок/потомок).
  boxes.sort((a, b) => a.rs[0].t - b.rs[0].t);
  const seen = new Set();
  for (let i = 0; i < boxes.length; i++) {
    const A = boxes[i];
    const aBottom = Math.max(...A.rs.map(q => q.b));
    for (let j = i + 1; j < boxes.length; j++) {
      const B = boxes[j];
      if (B.rs[0].t > aBottom + 40) break;
      if (A.el.contains(B.el) || B.el.contains(A.el)) continue;
      let hit = null;
      for (const p of A.rs) for (const q of B.rs) {
        const w = Math.min(p.r, q.r) - Math.max(p.l, q.l), h = Math.min(p.b, q.b) - Math.max(p.t, q.t);
        if (w > 2 && h > 3) hit = { w: Math.round(w), h: Math.round(h) };
      }
      if (hit) {
        const key = sel(A.el) + '|' + sel(B.el);
        if (seen.has(key)) continue; seen.add(key);
        out.overlaps.push({ a: sel(A.el), at: A.text, b: sel(B.el), bt: B.text, ...hit });
      }
    }
  }
  // Текст вылезает из своего блока (переполнение без обрезки).
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('#djDebug') || !el.childElementCount && !el.textContent.trim()) continue;
    const cs = getComputedStyle(el);
    if (cs.overflowX !== 'visible' || cs.display === 'inline' || cs.display === 'contents') continue;
    if (el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0 && visible(el) && !inFixed(el)) {
      const hasDirectText = [...el.childNodes].some(c => c.nodeType === 3 && c.textContent.trim());
      if (hasDirectText) out.spills.push({ sel: sel(el), text: el.textContent.trim().slice(0, 40), over: el.scrollWidth - el.clientWidth });
    }
  }
  // Текст, обрезанный многоточием или line-clamp: запрещено, текст должен быть виден целиком.
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('#djDebug') || !el.textContent.trim() || !visible(el)) continue;
    const cs = getComputedStyle(el);
    const clamp = cs.webkitLineClamp && cs.webkitLineClamp !== 'none';
    if ((cs.textOverflow === 'ellipsis' && el.scrollWidth > el.clientWidth + 1) || (clamp && el.scrollHeight > el.clientHeight + 1)) {
      out.truncated.push({ sel: sel(el), text: el.textContent.trim().slice(0, 50) });
    }
  }
  out.spills = out.spills.slice(0, 15); out.offscreen = out.offscreen.slice(0, 15); out.truncated = out.truncated.slice(0, 15);
  return out;
})()
