/* Equity Repricing Lab — shadow scout page (flag-gated server-side). */
(async function () {
  async function get(url) {
    const r = await fetch(url, { credentials: "same-origin" });
    if (!r.ok) throw new Error(url + " " + r.status);
    return r.json();
  }
  try {
    const scout = await get("/api/equity-repricing/scout");
    const el = document.getElementById("scout-list");
    if (!el) return;
    if (!scout.candidates || !scout.candidates.length) {
      el.textContent = "No shadow candidates yet.";
      return;
    }
    el.innerHTML = "<ul>" + scout.candidates.map(function (c) {
      return "<li><strong>" + (c.instrumentId || "") + "</strong> "
        + (c.archetype || "") + " — " + (c.decisionSession || "")
        + " [" + (c.reasonCodes || []).join(", ") + "]</li>";
    }).join("") + "</ul>";
  } catch (e) {
    const el = document.getElementById("scout-list");
    if (el) el.textContent = "Scout unavailable: " + e.message;
  }
  try {
    const v = await get("/api/equity-repricing/validation");
    const pre = document.getElementById("validation-body");
    if (pre) pre.textContent = JSON.stringify(v, null, 2);
  } catch (_) { /* ignore */ }
})();
