// Adds an always-visible link to the public ETL stats page (/etl-stats): a
// "the data is freshly updated" teaser on the logged-out login screen that also
// stays available as a nav link once signed in.
//
// Deliberately low-coupling so a Chainlit upgrade is unlikely to break it: it
// appends a single fixed-position element to <body> and does NOT reach into
// Chainlit's markup, routes, or React internals. Anchored bottom-right to stay
// clear of the centered message composer and the top-right header controls.
// Worst-case failure is the link silently not appearing — it can never block the
// app or the login form.
(function () {
  "use strict";

  var STATS_PATH = "/etl-stats";
  var LINK_ID = "etl-stats-link";

  function ensureLink() {
    if (!document.body || document.getElementById(LINK_ID)) return;
    var a = document.createElement("a");
    a.id = LINK_ID;
    a.href = STATS_PATH;
    a.textContent = "📊 Live data-refresh stats";
    a.style.cssText = [
      "position:fixed",
      "right:1rem",
      "bottom:1rem",
      "z-index:9999",
      "font:500 0.85rem system-ui,-apple-system,Segoe UI,Roboto,sans-serif",
      "padding:0.4rem 0.9rem",
      "border-radius:999px",
      "text-decoration:none",
      "color:inherit",
      "background:rgba(128,128,128,0.12)",
      "border:1px solid rgba(128,128,128,0.28)",
      "backdrop-filter:blur(4px)"
    ].join(";");
    document.body.appendChild(a);
  }

  // React renders into its own root div, not <body>, so our node persists across
  // navigation. The interval is just a cheap safety net (e.g. if body is replaced)
  // and to cover the link being added before <body> exists at script load.
  ensureLink();
  setInterval(ensureLink, 1000);
})();
