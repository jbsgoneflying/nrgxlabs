/* Equity Repricing Lab — shadow scout page */
(async function () {
  const $ = (id) => document.getElementById(id);

  async function get(url) {
    const r = await fetch(url, { credentials: "same-origin" });
    if (!r.ok) {
      const t = await r.text();
      throw new Error(url + " " + r.status + " " + t.slice(0, 120));
    }
    return r.json();
  }

  async function post(url) {
    const r = await fetch(url, { method: "POST", credentials: "same-origin" });
    if (!r.ok) {
      const t = await r.text();
      throw new Error(url + " " + r.status + " " + t.slice(0, 160));
    }
    return r.json();
  }

  function fmtPx(v) {
    if (v == null || v === "") return "—";
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(2) : "—";
  }

  function renderCandidates(cands) {
    const el = $("scout-list");
    $("erl-count").textContent = String(cands.length);
    if (!cands.length) {
      el.className = "erlEmpty";
      el.textContent = "No scout candidates yet. Click Refresh scout to pull recent earnings beats.";
      return;
    }
    el.className = "";
    el.innerHTML =
      '<table class="erlTable"><thead><tr>' +
      "<th>Ticker</th><th>Decision</th><th>Entry / Stop</th><th>Reasons</th>" +
      "</tr></thead><tbody>" +
      cands
        .map(function (c) {
          const entry = (c.entry && c.entry.entry_price) || null;
          const stop = (c.stop && c.stop.stop_price) || null;
          return (
            "<tr>" +
            '<td class="erlTicker">' +
            (c.ticker || c.instrumentId || "") +
            "</td>" +
            "<td>" +
            (c.decisionSession || "") +
            '<div class="erlReasons">' +
            (c.archetype || "") +
            "</div></td>" +
            "<td>" +
            fmtPx(entry) +
            " / " +
            fmtPx(stop) +
            "</td>" +
            '<td class="erlReasons">' +
            (c.reasonCodes || []).join(" · ") +
            "</td>" +
            "</tr>"
          );
        })
        .join("") +
      "</tbody></table>";
  }

  async function loadHealth() {
    try {
      const h = await get("/api/equity-repricing/health");
      $("erl-status").textContent = h.enabled ? "On" : "Off";
      const pill = $("erl-shadow-pill");
      if (h.shadowOnly) {
        pill.textContent = "Shadow only";
        pill.className = "erlPill";
      } else {
        pill.textContent = "Live risk path";
        pill.className = "erlPill erlPill--on";
      }
      return h;
    } catch (e) {
      $("erl-status").textContent = "Error";
      $("erl-msg").textContent = String(e.message || e);
      return null;
    }
  }

  async function loadScout() {
    const scout = await get("/api/equity-repricing/scout");
    renderCandidates(scout.candidates || []);
    return scout;
  }

  async function loadValidation() {
    try {
      const v = await get("/api/equity-repricing/validation");
      $("validation-body").textContent = JSON.stringify(v, null, 2);
    } catch (e) {
      $("validation-body").textContent = String(e.message || e);
    }
  }

  async function refresh() {
    const btn = $("erl-refresh");
    btn.disabled = true;
    $("erl-msg").textContent = "Refreshing scout from EODHD earnings calendar…";
    try {
      const res = await post("/api/equity-repricing/refresh?lookback_days=7");
      $("erl-msg").textContent =
        "Wrote " +
        (res.written || 0) +
        " candidates (" +
        (res.skipped || 0) +
        " skipped) in window " +
        ((res.window || []).join(" → ") || "—");
      await loadScout();
      await loadValidation();
    } catch (e) {
      $("erl-msg").textContent = "Refresh failed: " + (e.message || e);
    } finally {
      btn.disabled = false;
    }
  }

  $("erl-refresh").addEventListener("click", refresh);

  const health = await loadHealth();
  if (!health || !health.enabled) {
    $("scout-list").textContent = "Lab is disabled.";
    return;
  }
  try {
    const scout = await loadScout();
    if (!(scout.candidates || []).length) {
      // Auto-seed on first visit so the desk isn't staring at an empty table.
      await refresh();
    } else {
      await loadValidation();
    }
  } catch (e) {
    $("scout-list").textContent = "Scout unavailable: " + (e.message || e);
  }
})();
