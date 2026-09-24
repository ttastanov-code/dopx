// static/js/smart-back-links.js
//
// Ссылки «Назад к …» с data-back-link: если пришли с нашего сайта — history.back()
// (сохраняются фильтры, страница и прокрутка), иначе обычный переход по href.
(function () {
  document.addEventListener('click', function (event) {
    // Клики с модификаторами и не левой кнопкой — не трогаем.
    if (event.defaultPrevented || event.button !== 0 ||
        event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
      return;
    }

    var link = event.target.closest('[data-back-link]');
    if (!link) return;

    // history.back() только если referrer — наш сайт.
    var sameOriginReferrer = false;
    try {
      sameOriginReferrer = !!document.referrer && new URL(document.referrer).origin === location.origin;
    } catch (e) {
      sameOriginReferrer = false;
    }

    if (sameOriginReferrer && window.history.length > 1) {
      event.preventDefault();
      window.history.back();
    }
    // иначе — обычный переход по href
  });
})();
