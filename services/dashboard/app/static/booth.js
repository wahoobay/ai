// Booth mode: a conference booth's browser, where visitors try the site on their own. Opening any page
// with ?booth=1 turns it on in this browser (remembered), ?booth=0 turns it off. Loaded in <head>,
// before the page's own scripts, so nothing flashes and the review page boots in the view the tour
// is written for. What booth mode changes is in tour.js and tour.css.
(function () {
  "use strict";
  const KEY = "wb.booth";
  try {
    const params = new URLSearchParams(window.location.search);
    const asked = params.get("booth");
    if (asked === "1") window.localStorage.setItem(KEY, "1");
    else if (asked === "0") window.localStorage.removeItem(KEY);
    if (asked !== null) {
      params.delete("booth");
      const query = params.toString();
      window.history.replaceState(null, "", window.location.pathname + (query ? "?" + query : "") + window.location.hash);
    }
    if (window.localStorage.getItem(KEY) === "1") {
      document.documentElement.dataset.booth = "1";
      window.localStorage.setItem("rv.mode", "frame");   // review-core.js RV.MODE_KEY: the whole-frame view
    }
  } catch (err) {
    /* storage blocked: no booth mode */
  }
})();
