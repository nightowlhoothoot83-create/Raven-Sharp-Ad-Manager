// Runtime config for the Ad Manager frontend. index.html already loads
// this via <script src="/config.js"></script> — it was just never created,
// which is why config.apiBaseUrl was always undefined and every request
// failed with "Failed to fetch" before even leaving the browser.
window.RAVEN_SHARP_CONFIG = {
  apiBaseUrl: "https://ads.raven-sharp.com",
};
