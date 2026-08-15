// static/js/analytics.js
// Клиентский трекер продуктовой аналитики DOPX. Отправляет события на
// /analytics/track/ (analytics/views.py::TrackClientEventView) через
// sendBeacon — не блокирует навигацию/выгрузку страницы, в отличие от
// обычного fetch с ожиданием ответа.
(function () {
  const STORAGE_KEY = "dopx_anon_id";

  function getAnonymousId() {
    let id = localStorage.getItem(STORAGE_KEY);
    if (!id) {
      id = crypto.randomUUID();
      localStorage.setItem(STORAGE_KEY, id);
    }
    return id;
  }

  window.dopxTrack = function (eventName, properties) {
    const payload = JSON.stringify({
      event_name: eventName,
      anonymous_id: getAnonymousId(),
      properties: properties || {},
    });
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/analytics/track/", new Blob([payload], { type: "application/json" }));
    } else {
      fetch("/analytics/track/", {
        method: "POST",
        body: payload,
        headers: { "Content-Type": "application/json" },
        keepalive: true,
      });
    }
  };

  document.addEventListener("DOMContentLoaded", () => window.dopxTrack("page_view", { path: location.pathname }));
})();
