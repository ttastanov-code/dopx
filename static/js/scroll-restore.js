// static/js/scroll-restore.js
//
// Восстановление прокрутки при «Назад»: браузерное не срабатывает, если высота страницы
// дорастает после загрузки. Храним позицию в sessionStorage по URL и восстанавливаем
// на pageshow несколькими попытками. scrollRestoration='manual' ставится в base.html.
(function () {
  var STORAGE_PREFIX = 'dopx:scroll:';

  function storageKey() {
    return STORAGE_PREFIX + location.pathname + location.search;
  }

  function saveScroll() {
    try {
      sessionStorage.setItem(storageKey(), String(window.scrollY || window.pageYOffset || 0));
    } catch (e) {
      // Приватный режим / квота — просто не сохраняем.
    }
  }

  var saveTimer = null;
  window.addEventListener('scroll', function () {
    clearTimeout(saveTimer);
    // Дебаунс записи при прокрутке.
    saveTimer = setTimeout(saveScroll, 150);
  }, { passive: true });

  // pagehide, а не unload — unload отключает bfcache.
  window.addEventListener('pagehide', saveScroll);

  function restoreScroll() {
    var y = 0;
    try {
      var raw = sessionStorage.getItem(storageKey());
      if (raw !== null) {
        y = parseInt(raw, 10) || 0;
      }
    } catch (e) {
      return;
    }
    if (y <= 0) return;

    // Несколько попыток — высота может ещё меняться.
    window.scrollTo(0, y);
    requestAnimationFrame(function () {
      window.scrollTo(0, y);
      setTimeout(function () { window.scrollTo(0, y); }, 400);
    });
  }

  // pageshow срабатывает и при восстановлении из bfcache.
  window.addEventListener('pageshow', restoreScroll);
})();
