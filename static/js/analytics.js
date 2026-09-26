// static/js/analytics.js
// Трекер продуктовой аналитики: события на /analytics/track/ через sendBeacon.
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

  // Служебные разделы — не трафик аудитории.
  const isStaffPath = /^\/(staff|admin)\//.test(location.pathname);
  document.addEventListener("DOMContentLoaded", () => {
    if (isStaffPath) return;
    window.dopxTrack("page_view", { path: location.pathname, referrer: document.referrer });
  });
})();
