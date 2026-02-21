/* ── Engine 8: Post-Event Trade Extension ───────────────────────────────
   Frontend controller for the post-event evaluator page.
   ──────────────────────────────────────────────────────────────────────── */
(function () {
  "use strict";

  var form       = document.getElementById("e8Form");
  var runBtn     = document.getElementById("runBtn");
  var statusEl   = document.getElementById("status");
  var resultsEl  = document.getElementById("results");

  var _dummyEl = document.createElement("span");
  function qs(id) { return document.getElementById(id) || _dummyEl; }
  function fmt(v, d) { return v == null ? "—" : Number(v).toFixed(d == null ? 2 : d); }
  function pct(v) { return v == null ? "—" : (Number(v) * 100).toFixed(1) + "%"; }

  /* ── Render results ────────────────────────────────────────────────── */
  function render(data) {
    var dec = data.decision || {};
    var decisionStr = (typeof dec === "string" ? dec : dec.decision || "PASS").toUpperCase();
    var badge = qs("decisionBadge");
    badge.textContent = decisionStr;
    badge.className = "decisionBadge " + decisionStr.toLowerCase();

    var dir = dec.direction || data.direction;
    qs("decisionDirection").textContent = dir ? (dir.toLowerCase() === "long" ? "Long" : "Short") : "—";
    var conf = dec.confidence_score != null ? dec.confidence_score : data.confidence;
    qs("decisionConfidence").textContent = conf != null ? "Confidence: " + Math.round(conf) + " / 100" : "";
    qs("decisionRationale").textContent = dec.pass_reason || data.rationale || data.summary || "";

    /* Snapshot */
    var snap = data.snapshot || {};
    qs("snapActualMove").textContent = snap.actual_move_pct != null ? fmt(snap.actual_move_pct) + "%" : snap.actual_move != null ? fmt(snap.actual_move) + "%" : "—";
    qs("snapEmMultiple").textContent = fmt(snap.move_vs_em || snap.em_multiple) + "x";
    qs("snapAtrMultiple").textContent = fmt(snap.atr_multiple) + "x";
    qs("snapGapStructure").textContent = snap.gap_structure || "—";
    qs("snapIvCrush").textContent = snap.iv_crush_pct != null ? fmt(snap.iv_crush_pct) + "%" : snap.iv_crush != null ? fmt(snap.iv_crush) + "%" : "—";
    qs("snapSentiment").textContent = snap.sentiment || "—";

    /* Displacement */
    var prof = data.profile || data.displacement || {};
    qs("displaceMagnitude").textContent = prof.magnitude_em_label || prof.magnitude || "—";
    qs("displaceStructure").textContent = prof.structure_label || prof.structure || prof.gap_structure || "—";
    qs("displaceContext").textContent = prof.context_label || prof.context || "—";

    /* Historical — use 5-day horizon as the primary display */
    var hist = data.historical || {};
    var contProb = hist.continuation_prob_5d != null ? hist.continuation_prob_5d : hist.continuation_prob_3d;
    var revProb  = hist.reversion_prob_5d != null ? hist.reversion_prob_5d : hist.reversion_prob_3d;
    qs("histContinuation").textContent = contProb != null ? pct(contProb) : "—";
    qs("histReversion").textContent = revProb != null ? pct(revProb) : "—";
    qs("histMagnitude").textContent = hist.avg_continuation_magnitude != null ? fmt(hist.avg_continuation_magnitude) + "%" : "—";
    qs("histSample").textContent = hist.sample_size || "—";

    /* Trade profile */
    if (decisionStr !== "PASS") {
      qs("tradeDirection").textContent = dir ? (dir.toLowerCase() === "long" ? "Long" : "Short") : "—";
      qs("tradeRiskUnits").textContent = fmt(dec.risk_units, 1);
      qs("tradeHolding").textContent = (dec.holding_period_days || "1–5") + "d";
      qs("tradeEntry").textContent = dec.entry_preference || "—";
      qs("tradeSection").style.display = "";
    } else {
      qs("tradeSection").style.display = "none";
    }
  }

  /* ── Submit handler ────────────────────────────────────────────────── */
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var ticker = qs("ticker").value.trim().toUpperCase();
    if (!ticker) { statusEl.textContent = "Please enter a ticker."; return; }

    var earningsDate = qs("earningsDate").value || "";

    var params = new URLSearchParams();
    params.set("ticker", ticker);
    if (earningsDate) params.set("earnings_date", earningsDate);

    runBtn.disabled = true;
    runBtn.querySelector(".btnSpinner").style.display = "inline-block";
    statusEl.textContent = "Evaluating " + ticker + " for post-event extension…";
    resultsEl.classList.add("hidden");

    if (window.RavenLoading) window.RavenLoading.show("Evaluating " + ticker + "…");

    fetch("/api/engine8/evaluate?" + params.toString())
      .then(function (r) {
        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || r.statusText); });
        return r.json();
      })
      .then(function (data) {
        render(data);
        resultsEl.classList.remove("hidden");
        statusEl.textContent = "Evaluation complete for " + ticker + ".";
      })
      .catch(function (err) {
        statusEl.textContent = "Error: " + err.message;
      })
      .finally(function () {
        runBtn.disabled = false;
        runBtn.querySelector(".btnSpinner").style.display = "none";
        if (window.RavenLoading) window.RavenLoading.hide();
      });
  });

  /* ── Tooltip behaviour ─────────────────────────────────────────────── */
  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".tipBtn");
    if (btn) {
      e.stopPropagation();
      var panel = btn.nextElementSibling;
      var open = panel.classList.contains("open");
      document.querySelectorAll(".tipPanel.open").forEach(function (p) { p.classList.remove("open"); });
      if (!open) panel.classList.add("open");
      return;
    }
    if (!e.target.closest(".tipPanel")) {
      document.querySelectorAll(".tipPanel.open").forEach(function (p) { p.classList.remove("open"); });
    }
  });
})();
