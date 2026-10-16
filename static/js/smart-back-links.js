// static/js/smart-back-links.js
//
// Умная кнопка «Назад» ([data-back-link]): ведёт на страницу, с которой пришли, и сама называет её
// («Назад · Кайрат», «Назад к матчам»). Мастер оценки, вход, внешние сайты и ссылки-приглашения в путь не попадают —
// туда кнопка не вернёт. Пришли извне — ссылка по умолчанию из шаблона.
(function () {
  var KEY = 'dx-trail';
  var SKIP = /^\/(evaluations|users\/(login|register|logout|password|complete)|accounts|tg|r|share|admin|staff|api)\//;
  var LISTS = [
    [/^\/$/, 'На главную'], [/^\/matches\/$/, 'Назад к матчам'], [/^\/teams\/$/, 'Назад к командам'],
    [/^\/players\/$/, 'Назад к игрокам'], [/^\/coaches\/$/, 'Назад к тренерам'], [/^\/referees\/$/, 'Назад к судьям'],
    [/^\/leagues\/$/, 'Назад к лигам'], [/^\/users\/leaderboard\//, 'Назад к рейтингу'], [/^\/users\/profile\/$/, 'Назад к профилю'],
    [/^\/users\/achievements\/$/, 'Назад к достижениям'], [/^\/season\/(.*\/)?round\//, 'Назад к лучшим тура'], [/^\/season\/(.*\/)?best-xi\//, 'Назад к сборной сезона'],
    [/^\/notifications\//, 'Назад к уведомлениям'],
  ];

  function read() { try { return JSON.parse(sessionStorage.getItem(KEY)) || []; } catch (e) { return []; } }
  function write(t) { try { sessionStorage.setItem(KEY, JSON.stringify(t.slice(-30))); } catch (e) { /* без памяти — шаблонная ссылка */ } }

  // Имя страницы для подписи: meta dx-page-name из шаблона («Кайсар — Ордабасы»), иначе h1, иначе title.
  function pageName() {
    var meta = document.querySelector('meta[name="dx-page-name"]');
    if (meta && meta.content.trim()) return meta.content.trim();
    var h = document.querySelector('main h1, h1');
    var t = (h && h.textContent || document.title.split(/ [—|-] /)[0] || '').replace(/\s+/g, ' ').trim();
    return t.length > 48 ? '' : t;
  }
  function labelFor(entry) {
    for (var i = 0; i < LISTS.length; i++) if (LISTS[i][0].test(entry.path)) return LISTS[i][1];
    return entry.name ? 'Назад · ' + entry.name : 'Назад';
  }

  var here = location.pathname;
  var trail = read();
  var refPath = '';
  try {
    var ref = document.referrer && new URL(document.referrer);
    if (ref && ref.origin === location.origin) refPath = ref.pathname;
  } catch (e) { refPath = ''; }

  // Вернулись назад — срезаем путь до этой страницы; новая страница — добавляем.
  var at = -1;
  for (var i = trail.length - 1; i >= 0; i--) if (trail[i].path === here) { at = i; break; }
  if (at >= 0) trail = trail.slice(0, at + 1);
  else if (!SKIP.test(here)) trail.push({ path: here, search: location.search, name: '' });
  if (!SKIP.test(here)) {
    trail[trail.length - 1].search = location.search;
    document.addEventListener('DOMContentLoaded', function () { trail[trail.length - 1].name = pageName(); write(trail); });
  }
  write(trail);

  // Предыдущая «настоящая» страница: только если пришли с нашего сайта (не по внешней ссылке).
  var prev = null;
  if (refPath) for (var j = trail.length - 2; j >= 0; j--) if (trail[j].path !== here && !SKIP.test(trail[j].path)) { prev = trail[j]; break; }

  function apply() {
    if (!prev) return;
    document.querySelectorAll('[data-back-link]').forEach(function (link) {
      link.href = prev.path + (prev.search || '');
      var label = link.querySelector('span:last-child');
      if (label) label.textContent = labelFor(prev);
    });
  }
  document.addEventListener('DOMContentLoaded', apply);

  document.addEventListener('click', function (event) {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    var link = event.target.closest('[data-back-link]');
    if (!link || !prev) return;
    // Предыдущая в истории браузера — та же страница: back() сохраняет фильтры и прокрутку.
    if (refPath === prev.path && window.history.length > 1) { event.preventDefault(); window.history.back(); }
  });
})();
