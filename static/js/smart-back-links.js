// static/js/smart-back-links.js
//
// Ссылки «Назад к …» с data-back-link: если пришли со страницы, куда ведёт ссылка, — history.back()
// (сохраняются фильтры, страница и прокрутка), иначе обычный переход по href (например, после мастера оценки).
(function () {
  document.addEventListener('click', function (event) {
    // Клики с модификаторами и не левой кнопкой — не трогаем.
    if (event.defaultPrevented || event.button !== 0 ||
        event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
      return;
    }

    var link = event.target.closest('[data-back-link]');
    if (!link) return;

    // history.back() только если предыдущая страница — та самая, куда ведёт ссылка (фильтры в query не важны).
    var cameFromTarget = false;
    try {
      var ref = document.referrer && new URL(document.referrer);
      var target = new URL(link.href, location.href);
      cameFromTarget = !!ref && ref.origin === location.origin && ref.pathname === target.pathname;
    } catch (e) {
      cameFromTarget = false;
    }

    if (cameFromTarget && window.history.length > 1) {
      event.preventDefault();
      window.history.back();
    }
    // иначе — обычный переход по href
  });
})();
