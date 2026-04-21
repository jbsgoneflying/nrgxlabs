/* =============================================================
   Engine 14 v2 — IC Scenario Command Deck (additive layer)

   Loads on top of ic-scenario.js (the legacy scenario form
   stays wired; nothing it does is disrupted). Adds:

   - A ranked Wing Decision Console card above the form, calling
     POST /api/ic-scenario/wing-console and painting a 12-row
     placements table + EM/wing slider tuner.
   - "Simulate This Pick" handoff that pre-fills the scenario
     form and triggers its submit so the existing drilldown
     flow runs against the clicked placement.
   - MI v2 Regime, MC Reading, and MAE Pool drilldown cards
     populated from the /wing-console response.
   - An always-on E14-native advisor button post-scenario that
     posts to /api/ic-scenario/advisor and paints the narrative.

   This file is loaded AFTER ic-scenario.js so it can piggyback
   on the existing escapeHtml helpers via a minimal inline copy.
   ============================================================= */

(function () {
  "use strict";

  const State = {
    entryDate:   "",
    expiryDate:  "",
    asOfDate:    "",
    lastDeck:    null,
    selectedIndex: 0,
  };

  const SOURCE_LABELS = {
    desk_default:  "Desk default",
    user_override: "Override",
    unknown:       "",
  };

  // ---------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtPct(x, digits) {
    if (x === null || x === undefined) return "—";
    var n = Number(x);
    if (!Number.isFinite(n)) return "—";
    return n.toFixed(digits == null ? 1 : digits) + "%";
  }

  function todayIso() {
    var d = new Date();
    return d.toISOString().slice(0, 10);
  }

  function addBusinessDays(iso, n) {
    var d = new Date(iso + "T00:00:00");
    var added = 0;
    while (added < n) {
      d.setDate(d.getDate() + 1);
      var day = d.getDay();
      if (day !== 0 && day !== 6) added++;
    }
    return d.toISOString().slice(0, 10);
  }

  function setSourceChip(source) {
    var chip = $("e14EventSourceChip");
    if (!chip) return;
    var s = String(source || "unknown").toLowerCase();
    var cls = "e1SourceChip--unknown";
    if (s === "user_override") cls = "e1SourceChip--user_override";
    else if (s === "desk_default") cls = "e1SourceChip--orats_cores";
    chip.className = "e1SourceChip " + cls;
    var label = SOURCE_LABELS[s] || "";
    chip.textContent = label;
    chip.title = label ? ("Scenario source: " + label.toLowerCase()) : "";
  }

  function scoreColor(score) {
    if (score >= 75) return "e14MetricGood";
    if (score >= 55) return "e14MetricMed";
    return "e14MetricRisky";
  }

  // ---------------------------------------------------------------
  // Wing Console fetcher + painter
  // ---------------------------------------------------------------

  async function fetchAndPaintWingConsole() {
    var host = $("e14WingConsole");
    if (!host) return;
    host.innerHTML = '<div class="e14ConsoleWarnings">Scoring wing placements…</div>';

    var entryEl  = $("entryDate");
    var expiryEl = $("expiry");
    var entryDate = (entryEl && entryEl.value) || todayIso();
    var expiryDate = (expiryEl && expiryEl.value) || addBusinessDays(entryDate, 4);

    State.entryDate  = entryDate;
    State.expiryDate = expiryDate;
    State.asOfDate   = todayIso();

    setSourceChip("desk_default");

    try {
      var resp = await fetch("/api/ic-scenario/wing-console", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          underlying:  "SPX",
          entry_date:  entryDate,
          expiry_date: expiryDate,
        }),
      });
      if (!resp.ok) {
        var txt = await resp.text();
        throw new Error("wing-console " + resp.status + ": " + txt.slice(0, 300));
      }
      var deck = await resp.json();
      State.lastDeck = deck;
      paintWingConsole(host, deck);
      paintMiV2(deck);
      paintMcReading(deck);
      paintMaeDistribution(deck);
    } catch (err) {
      host.innerHTML = '<div class="e14ConsoleWarnings">Wing Console unavailable: ' +
        esc(err && err.message ? err.message : String(err)) + "</div>";
    }
  }

  function paintWingConsole(host, deck) {
    var wc = deck && deck.wingConsole;
    if (!wc || !Array.isArray(wc.placements) || !wc.placements.length) {
      host.innerHTML = '<div class="e14ConsoleWarnings">' +
        esc(((wc && wc.warnings) || ["No placements returned."]).join(" · ")) +
        "</div>";
      return;
    }
    var top = wc.placements.slice(0, 8);
    var subtitle = [
      "Entry <strong>" + esc(wc.entry_date) + "</strong>",
      "Expiry <strong>" + esc(wc.expiry_date) + "</strong>",
      "Spot <strong>" + (wc.spot != null ? Number(wc.spot).toFixed(2) : "—") + "</strong>",
      "1σ EM <strong>" + fmtPct(wc.em_pct, 2) + "</strong>",
      "Regime <strong>" + esc(wc.regime_label || "—") + "</strong>" +
        (wc.regime_mi_v2 && wc.regime_mi_v2.label
          ? " · HMM <strong>" + esc(wc.regime_mi_v2.label) + "</strong>" : ""),
      "MC <strong>" + ((deck.mcResults && deck.mcResults.n_sims) || 0) + "</strong> sims" +
        (deck.mcResults && deck.mcResults.conditioning_used
          ? " · " + esc(deck.mcResults.conditioning_used) : ""),
    ].map(function (s) { return "<span>" + s + "</span>"; }).join("");

    var warningsHtml = Array.isArray(wc.warnings) && wc.warnings.length
      ? '<div class="e14ConsoleWarnings">' + wc.warnings.map(esc).join(" · ") + "</div>"
      : "";

    var rows = top.map(function (p, i) {
      var scoreClass = scoreColor(Number(p.composite_score || 0));
      var maeClass   = (p.mae_p95_vs_wing || 0) >= 0.9 ? "e14MetricRisky"
                     : (p.mae_p95_vs_wing || 0) >= 0.5 ? "e14MetricMed" : "e14MetricGood";
      var breachClass = (p.breach_close_prob || 0) >= 0.2 ? "e14MetricRisky"
                      : (p.breach_close_prob || 0) >= 0.1 ? "e14MetricMed" : "e14MetricGood";
      var touchClass  = (p.touch_intraweek_prob || 0) >= 0.3 ? "e14MetricRisky"
                      : (p.touch_intraweek_prob || 0) >= 0.15 ? "e14MetricMed" : "e14MetricGood";
      return '' +
        '<tr class="e14PlacementRow ' + (i === 0 ? "e14PlacementRow--top" : "") + '" data-index="' + i + '">' +
          '<td class="e14RankCell">' + (i + 1) + (i === 0 ? '<span class="e14StarTop">★</span>' : "") + "</td>" +
          "<td>" + Number(p.em_mult).toFixed(2) + "</td>" +
          "<td>" + Number(p.wing_pts).toFixed(0) + "</td>" +
          "<td>" + Number(p.short_put_strike).toFixed(0) + " / " + Number(p.short_call_strike).toFixed(0) + "</td>" +
          "<td>$" + Number(p.credit_dollars).toFixed(0) + "</td>" +
          '<td class="' + breachClass + '">' + fmtPct((p.breach_close_prob || 0) * 100) + "</td>" +
          '<td class="' + touchClass + '">' + fmtPct((p.touch_intraweek_prob || 0) * 100) + "</td>" +
          '<td class="' + maeClass + '">' + fmtPct((p.mae_p95_vs_wing || 0) * 100) + "</td>" +
          "<td>" + fmtPct(p.theta_capture_pct) + "</td>" +
          '<td class="e14ScoreCell ' + scoreClass + '">' + Number(p.composite_score).toFixed(1) + "</td>" +
          "<td>" + esc(p.confidence || "—") + "</td>" +
        "</tr>";
    }).join("");

    var topP = top[0] || { em_mult: 1.25, wing_pts: 10 };

    host.innerHTML = '' +
      '<div class="e14Console">' +
        '<div class="e14ConsoleHeader">' +
          "<div>" +
            '<h3 class="e14ConsoleTitle">Ranked weekly-IC placements</h3>' +
            '<div class="e14ConsoleSubtitle">' + subtitle + "</div>" +
          "</div>" +
        "</div>" +
        warningsHtml +
        '<table class="e14PlacementTable"><thead><tr>' +
          "<th>#</th><th>EM×</th><th>Wings (pts)</th>" +
          "<th>P short / C short</th><th>Credit ($)</th>" +
          "<th>Brch close</th><th>Touch intraweek</th>" +
          "<th>MAE p95 (% wing)</th><th>Theta cap</th>" +
          "<th>Score</th><th>Conf.</th>" +
        "</tr></thead><tbody>" + rows + "</tbody></table>" +

        '<div class="e14Tuner">' +
          '<div class="e14TunerField">' +
            '<label for="e14TunerEm">EM multiple <span id="e14TunerEmValue" class="e14TunerValue">' + Number(topP.em_mult).toFixed(2) + "</span></label>" +
            '<input id="e14TunerEm" type="range" min="0.75" max="2.5" step="0.05" value="' + Number(topP.em_mult).toFixed(2) + '" />' +
          "</div>" +
          '<div class="e14TunerField">' +
            '<label for="e14TunerWp">Wing width (pts) <span id="e14TunerWpValue" class="e14TunerValue">' + Number(topP.wing_pts).toFixed(0) + "</span></label>" +
            '<input id="e14TunerWp" type="range" min="2" max="30" step="1" value="' + Number(topP.wing_pts).toFixed(0) + '" />' +
          "</div>" +
          '<div class="e14TunerScoreBox">' +
            '<div>Custom placement score: <strong id="e14TunerScore">—</strong></div>' +
            '<div style="font-size:11px;color:var(--muted,#9aa0a6)" id="e14TunerScoreNote">snap to nearest grid</div>' +
          "</div>" +
        "</div>" +

        '<div class="e14ConsoleActions">' +
          '<button type="button" id="e14SimPickBtn" class="e14ConsoleActions--primary">Simulate This Pick</button>' +
          '<button type="button" id="e14RunAdvisorBtn">Run Advisor</button>' +
          '<button type="button" id="e14ExportBtn">Export JSON</button>' +
        "</div>" +
      "</div>";

    wireTuner(deck);
    wireDeckActions(deck);
  }

  function wireTuner(deck) {
    var emEl = $("e14TunerEm");
    var wpEl = $("e14TunerWp");
    var emV = $("e14TunerEmValue");
    var wpV = $("e14TunerWpValue");
    var scoreEl = $("e14TunerScore");
    var noteEl  = $("e14TunerScoreNote");
    if (!emEl || !wpEl) return;

    var placements = (deck && deck.wingConsole && deck.wingConsole.placements) || [];
    var seq = 0;
    var debounceTimer = null;
    var DEBOUNCE_MS = 220;

    function nearest(em, wp) {
      var best = null, bestDist = Infinity;
      for (var i = 0; i < placements.length; i++) {
        var p = placements[i];
        var d = Math.pow(p.em_mult - em, 2) + Math.pow((p.wing_pts - wp) / 5, 2);
        if (d < bestDist) { best = p; bestDist = d; }
      }
      return best;
    }

    function paint(p, tag) {
      if (!p || !scoreEl) return;
      scoreEl.textContent = Number(p.composite_score).toFixed(1);
      scoreEl.className = scoreColor(Number(p.composite_score));
      if (noteEl) {
        noteEl.textContent = tag +
          " · brch " + fmtPct((p.breach_close_prob || 0) * 100) +
          " · touch " + fmtPct((p.touch_intraweek_prob || 0) * 100) +
          " · credit $" + Number(p.credit_dollars || 0).toFixed(0);
      }
    }

    async function fetchExact(em, wp) {
      var mySeq = ++seq;
      try {
        var resp = await fetch("/api/ic-scenario/wing-console/score-placement", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            underlying:  "SPX",
            entry_date:  State.entryDate,
            expiry_date: State.expiryDate,
            as_of_date:  State.asOfDate,
            em_mult:     em,
            wing_pts:    wp,
          }),
        });
        if (!resp.ok) return;
        var body = await resp.json();
        if (mySeq !== seq) return;
        paint(body.placement, "exact");
      } catch (err) { /* ignore */ }
    }

    function recompute() {
      var em = Number(emEl.value);
      var wp = Number(wpEl.value);
      if (emV) emV.textContent = em.toFixed(2);
      if (wpV) wpV.textContent = wp.toFixed(0);
      var near = nearest(em, wp);
      if (near) paint(near, "grid ~ EM " + Number(near.em_mult).toFixed(2) + " / " +
        Number(near.wing_pts).toFixed(0) + "pt");
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () { fetchExact(em, wp); }, DEBOUNCE_MS);
    }
    emEl.addEventListener("input", recompute);
    wpEl.addEventListener("input", recompute);
    recompute();
  }

  function wireDeckActions(deck) {
    var exportBtn = $("e14ExportBtn");
    if (exportBtn) {
      exportBtn.addEventListener("click", function () {
        var blob = new Blob([JSON.stringify(deck, null, 2)], { type: "application/json" });
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = "e14-wing-console-" +
          ((deck.wingConsole || {}).entry_date || "now") + ".json";
        a.click();
        setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      });
    }
    var simBtn = $("e14SimPickBtn");
    if (simBtn) simBtn.addEventListener("click", simulateTopPick);

    var advBtn = $("e14RunAdvisorBtn");
    if (advBtn) advBtn.addEventListener("click", runAdvisor);
  }

  function simulateTopPick() {
    var deck = State.lastDeck;
    if (!deck || !deck.wingConsole) return;
    var p = (deck.wingConsole.placements || [])[State.selectedIndex || 0];
    if (!p) return;

    var sp  = $("shortPut"),  lp = $("longPut"),
        sc  = $("shortCall"), lc = $("longCall"),
        cr  = $("creditReceived"),
        ed  = $("entryDate"),
        xp  = $("expiry");
    if (sp) sp.value = Number(p.short_put_strike).toFixed(0);
    if (lp) lp.value = Number(p.long_put_strike).toFixed(0);
    if (sc) sc.value = Number(p.short_call_strike).toFixed(0);
    if (lc) lc.value = Number(p.long_call_strike).toFixed(0);
    if (cr) cr.value = Number(p.credit_dollars / 100.0).toFixed(2);
    if (ed) ed.value = State.entryDate;
    if (xp) xp.value = State.expiryDate;

    // Trigger the existing scenario form handler so the drilldown renders.
    var form = $("icForm");
    if (form) {
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else {
        var evt = new Event("submit", { bubbles: true, cancelable: true });
        form.dispatchEvent(evt);
      }
    }
    setSourceChip("user_override");
  }

  // ---------------------------------------------------------------
  // MI v2 / MC / MAE card painters
  // ---------------------------------------------------------------

  function paintMiV2(deck) {
    var sec  = $("e14RegimeMiV2Section");
    var body = $("e14RegimeMiV2Body");
    if (!sec || !body) return;
    var r = (deck && deck.regime && deck.regime.mi_v2) || null;
    if (!r) { sec.style.display = "none"; return; }
    sec.style.display = "";
    var probs = r.probabilities || {};
    var bars = Object.keys(probs).map(function (label) {
      var pct = Math.max(0, Math.min(1, Number(probs[label]) || 0));
      return '' +
        '<div class="e14RegimeMiV2">' +
          '<span style="min-width:110px">' + esc(label) + "</span>" +
          '<span class="e14RegimeMiV2Bar" style="width:' + Math.max(24, Math.round(pct * 180)) + 'px">' +
            '<span class="e14RegimeMiV2Fill" style="width:' + (pct * 100).toFixed(0) + '%"></span>' +
          "</span>" +
          "<span>" + (pct * 100).toFixed(1) + "%</span>" +
        "</div>";
    }).join("");
    body.innerHTML = '' +
      '<div style="padding:12px 16px">' +
        '<div class="muted" style="font-size:12px;margin-bottom:6px">' +
          "label: <strong>" + esc(r.label || "—") + "</strong> · vol_state: <strong>" +
          esc(typeof r.vol_state === "string" ? r.vol_state : JSON.stringify(r.vol_state || "—")) +
          "</strong> · source: <strong>" + esc(r.source || "—") + "</strong>" +
        "</div>" +
        bars +
      "</div>";
  }

  function paintMcReading(deck) {
    var sec  = $("e14McReadingSection");
    var body = $("e14McReadingBody");
    if (!sec || !body) return;
    var mc = deck && deck.mcResults;
    if (!mc || !mc.n_sims) { sec.style.display = "none"; return; }
    sec.style.display = "";
    body.innerHTML = '' +
      '<div style="padding:12px 16px">' +
        '<div class="muted" style="font-size:12px;margin-bottom:6px">' +
          "n_sims: <strong>" + mc.n_sims + "</strong> · mode: <strong>" + esc(mc.mode) +
          "</strong> · conditioning: <strong>" + esc(mc.conditioning_used) +
          "</strong> · pool used/total: <strong>" + mc.pool_size_used + "/" + mc.pool_size_total + "</strong>" +
        "</div>" +
        (Array.isArray(mc.notes) && mc.notes.length
          ? '<div class="muted" style="font-size:11px">' + mc.notes.map(esc).join(" · ") + "</div>"
          : "") +
      "</div>";
  }

  function paintMaeDistribution(deck) {
    var sec  = $("e14MaeDistributionSection");
    var body = $("e14MaeDistributionBody");
    if (!sec || !body) return;
    var m = deck && deck.maeDistribution;
    if (!m || !m.n) { sec.style.display = "none"; return; }
    sec.style.display = "";
    body.innerHTML = '' +
      '<div style="padding:12px 16px">' +
        '<div class="muted" style="font-size:12px;margin-bottom:8px">' +
          "n=<strong>" + m.n + "</strong> weeks · source: <strong>" + esc(m.source) + "</strong>" +
        "</div>" +
        '<div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(120px, 1fr));gap:8px">' +
          '<div class="e14Card"><div class="e14CardLabel">p50</div><div class="e14CardValue">' + fmtPct(m.p50, 2) + "</div></div>" +
          '<div class="e14Card"><div class="e14CardLabel">p75</div><div class="e14CardValue">' + fmtPct(m.p75, 2) + "</div></div>" +
          '<div class="e14Card"><div class="e14CardLabel">p90</div><div class="e14CardValue">' + fmtPct(m.p90, 2) + "</div></div>" +
          '<div class="e14Card"><div class="e14CardLabel">p95</div><div class="e14CardValue">' + fmtPct(m.p95, 2) + "</div></div>" +
          '<div class="e14Card"><div class="e14CardLabel">max</div><div class="e14CardValue">' + fmtPct(m.max, 2) + "</div></div>" +
        "</div>" +
      "</div>";
  }

  // ---------------------------------------------------------------
  // Advisor
  // ---------------------------------------------------------------

  async function runAdvisor() {
    var sec = $("e14AdvisorSection");
    var body = $("e14AdvisorBody");
    if (!sec || !body) return;
    sec.style.display = "";
    body.innerHTML = '<div class="muted" style="padding:12px">Calling advisor…</div>';

    // Prefer a scenario that has been run from the form; fall back to
    // running the advisor from a fresh /request using the form values.
    var formBody = readFormBody();
    try {
      var resp = await fetch("/api/ic-scenario/advisor", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request: formBody }),
      });
      if (!resp.ok) {
        var txt = await resp.text();
        throw new Error("advisor " + resp.status + ": " + txt.slice(0, 300));
      }
      var out = await resp.json();
      paintAdvisor(body, out);
    } catch (err) {
      body.innerHTML = '<div class="e14ConsoleWarnings">Advisor unavailable: ' +
        esc(err && err.message ? err.message : String(err)) + "</div>";
    }
  }

  function readFormBody() {
    function num(id) {
      var v = $(id);
      return v && v.value ? parseFloat(v.value) : null;
    }
    function s(id) {
      var v = $(id);
      return v && v.value ? String(v.value) : "";
    }
    return {
      underlying:      "SPX",
      entryDate:       s("entryDate"),
      expiry:          s("expiry"),
      shortPut:        num("shortPut"),
      longPut:         num("longPut"),
      shortCall:       num("shortCall"),
      longCall:        num("longCall"),
      creditReceived:  num("creditReceived"),
      profitTargetPct: num("profitTargetPct"),
      stopLossPct:     num("stopLossPct"),
      seasonMode:      s("seasonMode") || "none",
    };
  }

  function paintAdvisor(host, out) {
    var adv = out && out.advisor;
    if (!adv) {
      host.innerHTML = '<div class="e14ConsoleWarnings">Advisor returned empty response.</div>';
      return;
    }
    var verdictClass = "e14ScoreCell";
    if (adv.verdict === "PASS") verdictClass = "e14ScoreCell--low";
    else if (adv.verdict === "HOLD") verdictClass = "e14ScoreCell--med";

    var risks = Array.isArray(adv.risks) ? adv.risks.map(function (r) {
      return "<li>" + esc(r) + "</li>";
    }).join("") : "";
    var keyPts = Array.isArray(adv.keyPoints) ? adv.keyPoints.map(function (r) {
      return "<li>" + esc(r) + "</li>";
    }).join("") : "";
    var adjs = Array.isArray(adv.suggestedAdjustments) ? adv.suggestedAdjustments.map(function (r) {
      return "<li>" + esc(r) + "</li>";
    }).join("") : "";

    host.innerHTML = '' +
      '<div style="padding:14px 16px">' +
        '<div style="display:flex; gap:12px; align-items:baseline; flex-wrap:wrap">' +
          '<span style="font-size:22px; font-weight:800; letter-spacing:0.04em" class="' + verdictClass + '">' + esc(adv.verdict) + "</span>" +
          '<span class="muted" style="font-size:12px">confidence: <strong>' + Number(adv.confidence * 100).toFixed(0) + "%</strong> · stance: <strong>" + esc(adv.stance) + "</strong> · source: <strong>" + esc(adv._source) + "</strong></span>" +
        "</div>" +
        '<div class="e14AdvisorNarrative" style="margin-top:8px">' + esc(adv.narrative || "") + "</div>" +
        (keyPts ? '<div style="margin-top:8px"><strong>Key points</strong><ul style="margin:4px 0 0 20px">' + keyPts + "</ul></div>" : "") +
        (risks ? '<div style="margin-top:8px"><strong>Risks</strong><ul style="margin:4px 0 0 20px">' + risks + "</ul></div>" : "") +
        (adjs ? '<div style="margin-top:8px"><strong>Suggested adjustments</strong><ul style="margin:4px 0 0 20px">' + adjs + "</ul></div>" : "") +
        (adv.deskNote ? '<div class="muted" style="margin-top:8px;font-size:12px"><strong>Desk note:</strong> ' + esc(adv.deskNote) + "</div>" : "") +
        (adv.plannedExitNote ? '<div class="muted" style="margin-top:4px;font-size:12px"><strong>Exit:</strong> ' + esc(adv.plannedExitNote) + "</div>" : "") +
      "</div>";
  }

  // ---------------------------------------------------------------
  // Boot: run Wing Console on page load + when form fields change.
  // ---------------------------------------------------------------

  function boot() {
    // Initial render.
    fetchAndPaintWingConsole();

    // Re-run when entry / expiry change (desk tweaks the window).
    var debounceTimer = null;
    function schedule() {
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(fetchAndPaintWingConsole, 400);
    }
    ["entryDate", "expiry"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("change", schedule);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
