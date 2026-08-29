// static/js/scroll-restore.js
//
// БАГ, КОТОРЫЙ ТУТ БЫЛ (жалоба пользователя, 2026-08-29): кнопка "назад"
// браузера возвращала со страницы матча (или любой другой детальной
// страницы) на список НЕ туда, где пользователь был проскроллен, а в
// самый верх. По спецификации браузер сам обязан помнить scroll каждой
// записи истории (history.scrollRestoration='auto' по умолчанию) — но
// это восстановление молча ломается, если контент страницы после
// перехода назад успевает измениться по высоте ПОСЛЕ того, как браузер
// уже попытался прокрутить (логотипы команд/иконки шрифта догружаются
// асинхронно, картинки без явного intrinsic-size немного "прыгают") —
// на момент попытки браузера документ ещё короче, чем сохранённая
// позиция, и итоговый scrollTop обрезается до текущей (меньшей) высоты.
// Здесь та же проблема не только на /matches/, а на любой странице со
// списком карточек (команды/игроки/лидерборды и т.д.) — общий баг, не
// специфичный для одного шаблона, поэтому фикс общий, не привязан к
// конкретному приложению.
//
// Решение: не полагаться на автоматическое восстановление браузера
// (history.scrollRestoration='manual' ставится ещё раньше — см. inline-
// скрипт в <head> templates/base.html, до первой отрисовки), а хранить
// последнюю позицию скролла в sessionStorage под ключом URL страницы и
// восстанавливать её самим на 'pageshow' — с несколькими повторными
// попытками в течение первых секунд загрузки, чтобы пережить именно тот
// сценарий "контент дорос уже после первой попытки восстановления".
(function () {
  var STORAGE_PREFIX = 'dopx:scroll:';

  function storageKey() {
    return STORAGE_PREFIX + location.pathname + location.search;
  }

  function saveScroll() {
    try {
      sessionStorage.setItem(storageKey(), String(window.scrollY || window.pageYOffset || 0));
    } catch (e) {
      // приватный режим / квота sessionStorage исчерпана — просто не
      // запоминаем позицию, не роняем страницу из-за этого.
    }
  }

  var saveTimer = null;
  window.addEventListener('scroll', function () {
    clearTimeout(saveTimer);
    // Дебаунс — иначе sessionStorage.setItem дёргался бы на каждый кадр
    // скролла (заметно на мобильных при инерционной прокрутке).
    saveTimer = setTimeout(saveScroll, 150);
  }, { passive: true });

  // pagehide, а НЕ beforeunload/unload: слушатель unload-событий в Chrome
  // сам по себе выключает bfcache для страницы (и делает переход "назад"
  // всегда полной перезагрузкой с сервера вместо мгновенного восстановления
  // из кэша) — то есть попытка подстраховаться через unload устроила бы
  // ровно ту деградацию, которую мы чиним. pagehide не имеет этого
  // побочного эффекта и срабатывает в обоих случаях (обычная навигация и
  // уход в bfcache).
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

    // Несколько попыток вместо одной: сразу, на следующем кадре и через
    // полсекунды — на случай, если шрифты/логотипы команд ещё меняют
    // высоту документа в момент первой попытки (см. докстринг файла).
    window.scrollTo(0, y);
    requestAnimationFrame(function () {
      window.scrollTo(0, y);
      setTimeout(function () { window.scrollTo(0, y); }, 400);
    });
  }

  // pageshow — не 'load'/'DOMContentLoaded': стреляет и на обычной
  // загрузке, и при восстановлении страницы из bfcache (когда
  // DOMContentLoaded вообще не сработает повторно).
  window.addEventListener('pageshow', restoreScroll);
})();
