/* ── Engine 12 — VIX Spike Fade ────────────────────────────────────── */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var esc = typeof escapeHtml === "function" ? escapeHtml : function (s) { return String(s || ""); };
  var fmt2 = function (v) { return v == null ? "\u2014" : Number(v).toFixed(2); };
  var fmt1 = function (v) { return v == null ? "\u2014" : Number(v).toFixed(1); };
  var fmt0 = function (v) { return v == null ? "\u2014" : Math.round(Number(v)).toString(); };
  var fmtPct = function (v) { return v == null ? "\u2014" : (Number(v) * 100).toFixed(1) + "%"; };

  var lastPayload = null;
  var _insightCache = {};
  var _insightRegistry = [];

  /* ════════════════════════════════════════════════════════════════════
     GPT-5.3 Contextual Desk Insight System
     ════════════════════════════════════════════════════════════════════ */

  function requestInsight(type, key, data, title) {
    var cacheKey = type + ":" + key;
    var popup = $("e12Popup");
    var body = $("e12PopupBody");
    $("e12PopupTitle").textContent = title || "Desk Insight \u2014 GPT-5.3";
    popup.classList.add("visible");

    if (_insightCache[cacheKey]) {
      renderInsight(_insightCache[cacheKey]);
      return;
    }

    body.innerHTML = '<div class="e12PopupLoading"><div class="e12PopupSpinner"></div><div>Analyzing ' + esc(key) + '\u2026</div></div>';

    var summary = {};
    if (lastPayload) {
      summary = {
        spike: lastPayload.spike,
        severity: lastPayload.severity,
        scenarios: lastPayload.scenarios,
        edgeComposite: lastPayload.edgeComposite,
        ouModel: lastPayload.ouModel,
        dealerGamma: lastPayload.dealerGamma,
        crossAssetStress: lastPayload.crossAssetStress,
      };
    }

    fetch("/api/engine12/explain", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: type, key: key, data: data, scan_summary: summary }),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || "Failed"); });
        return r.json();
      })
      .then(function (result) {
        _insightCache[cacheKey] = result;
        renderInsight(result);
      })
      .catch(function (err) {
        body.innerHTML = '<div style="color:rgba(255,59,48,.8);padding:20px;">Error: ' + esc(err.message) + '</div>';
      });
  }

  function renderInsight(data) {
    var body = $("e12PopupBody");
    var h = "";

    if (data.headline) {
      h += '<div style="font-size:15px;font-weight:800;margin-bottom:14px;color:#fff;line-height:1.3;">' + esc(data.headline) + '</div>';
    }

    var fields = [
      ["what_it_is", "What This Measures"],
      ["current_read", "Current Read"],
      ["how_to_trade", "How to Trade This"],
      ["what_to_watch", "What to Watch"],
      ["re_simulate_hint", "Re-Simulate Guide"],
      ["desk_note", "Desk Note"],
    ];

    for (var i = 0; i < fields.length; i++) {
      var val = data[fields[i][0]];
      if (!val) continue;
      h += '<div class="e12Field"><div class="e12FieldLabel">' + fields[i][1] + '</div><div class="e12FieldValue">' + esc(val) + '</div></div>';
    }

    if (data.raw_text) {
      h += '<pre style="white-space:pre-wrap;font-size:11px;color:rgba(255,255,255,.7);">' + esc(data.raw_text) + '</pre>';
    }

    body.innerHTML = h || "<div>No insight generated.</div>";
  }

  function inlineInsightBtn(type, key, data, title) {
    var idx = _insightRegistry.length;
    _insightRegistry.push({ type: type, key: key, data: data, title: title });
    return '<button class="e12InsightBtn" data-insight-idx="' + idx + '" style="margin-top:8px;"><span class="ico">&#9432;</span> Explain</button>';
  }

  /* Section-level insight buttons (data-section attr in HTML) */
  document.addEventListener("click", function (e) {
    var sectionBtn = e.target.closest(".e12InsightBtn[data-section]");
    if (sectionBtn && lastPayload) {
      var section = sectionBtn.getAttribute("data-section");
      var sectionData = {};
      var title = "Desk Insight";
      if (section === "regime") {
        sectionData = { spike: lastPayload.spike, severity: lastPayload.severity, dealerGamma: lastPayload.dealerGamma, crossAssetStress: lastPayload.crossAssetStress };
        title = "Regime Dashboard \u2014 GPT-5.3";
      } else if (section === "edge") {
        sectionData = lastPayload.edgeComposite;
        title = "Edge Decomposition \u2014 GPT-5.3";
      } else if (section === "ou_model") {
        sectionData = { ouModel: lastPayload.ouModel, forwardCurve: lastPayload.forwardCurve, termStructure: lastPayload.termStructure };
        title = "OU Model \u2014 GPT-5.3";
      } else if (section === "scenarios") {
        sectionData = { scenarios: lastPayload.scenarios, dealerGamma: lastPayload.dealerGamma, crossAssetStress: lastPayload.crossAssetStress };
        title = "Scenario Analysis \u2014 GPT-5.3";
      } else if (section === "recommendation") {
        sectionData = { recommendation: lastPayload.recommendation, edgeComposite: lastPayload.edgeComposite, scenarios: lastPayload.scenarios };
        title = "Structure Recommendation \u2014 GPT-5.3";
      } else if (section === "mc_results") {
        sectionData = lastPayload.monteCarlo;
        title = "Monte Carlo Results \u2014 GPT-5.3";
      } else if (section === "historical") {
        sectionData = { historicalComparisons: lastPayload.historicalComparisons, spike: lastPayload.spike };
        title = "Historical Shocks \u2014 GPT-5.3";
      }
      requestInsight(section, section, sectionData, title);
      return;
    }

    /* Inline card-level insight buttons */
    var cardBtn = e.target.closest(".e12InsightBtn[data-insight-idx]");
    if (cardBtn) {
      var idx = parseInt(cardBtn.getAttribute("data-insight-idx"), 10);
      if (!isNaN(idx) && _insightRegistry[idx]) {
        var entry = _insightRegistry[idx];
        requestInsight(entry.type, entry.key, entry.data, entry.title);
      }
    }
  });

  /* Popup drag + close */
  (function initDrag() {
    var popup = $("e12Popup");
    var header = $("e12PopupHeader");
    var closeBtn = $("e12PopupClose");
    var dragging = false, startX = 0, startY = 0, origX = 0, origY = 0;

    closeBtn.addEventListener("click", function () { popup.classList.remove("visible"); });

    header.addEventListener("mousedown", function (ev) {
      if (ev.target === closeBtn) return;
      dragging = true;
      popup.classList.add("isDragging");
      startX = ev.clientX; startY = ev.clientY;
      var rect = popup.getBoundingClientRect();
      origX = rect.left; origY = rect.top;
      ev.preventDefault();
    });
    document.addEventListener("mousemove", function (ev) {
      if (!dragging) return;
      popup.style.left = (origX + ev.clientX - startX) + "px";
      popup.style.top = (origY + ev.clientY - startY) + "px";
      popup.style.right = "auto";
    });
    document.addEventListener("mouseup", function () {
      if (dragging) { dragging = false; popup.classList.remove("isDragging"); }
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") popup.classList.remove("visible");
    });
  })();

  /* ── Badge helpers ── */
  function scoreBadge(score, label) {
    if (score == null) return "";
    var cls = score >= 70 ? "e12Badge--red" : score >= 50 ? "e12Badge--amber" : score >= 30 ? "e12Badge--blue" : "e12Badge--green";
    return '<span class="e12Badge ' + cls + '">' + (label || fmt0(score)) + "</span>";
  }

  function scoreBarHtml(score) {
    var cls = score >= 70 ? "high" : score >= 40 ? "med" : "low";
    return '<div class="e12ScoreBar"><div class="e12ScoreBarFill ' + cls + '" style="width:' + Math.max(0, Math.min(100, score)) + '%"></div></div>';
  }

  function gammaLabel(sign) {
    if (sign === "negative") return '<span style="color:var(--red);font-weight:900">NEGATIVE</span>';
    if (sign === "positive") return '<span style="color:var(--green);font-weight:900">POSITIVE</span>';
    return '<span style="color:var(--muted)">UNKNOWN</span>';
  }

  function card(label, valueHtml, captionHtml, score) {
    var badge = score != null ? " " + scoreBadge(score) : "";
    var bar = score != null ? scoreBarHtml(score) : "";
    return '<div class="e12Card"><div class="e12CardLabel">' + esc(label) + "</div>" +
      '<div class="e12CardValue">' + valueHtml + badge + "</div>" +
      '<div class="e12CardCaption">' + (captionHtml || "") + "</div>" + bar + "</div>";
  }

  /* ── Regime Dashboard ── */
  function renderRegime(d) {
    var sp = d.spike || {};
    var sev = d.severity || {};
    var dg = d.dealerGamma || {};
    var stress = d.crossAssetStress || {};

    var h = "";
    h += card("VIX Current",
      '<span class="e12MonoVal">' + fmt2(sp.vixCurrent) + "</span>",
      "20d MA: " + fmt2(sp.vix20dMA) + " &nbsp;|&nbsp; \u03C3: " + fmt2(sp.vix20dStd) + " &nbsp;|&nbsp; z: " + fmt2(sp.zScore));

    h += card("Spike Detected",
      sp.detected ? '<span style="color:var(--red);font-weight:900">YES</span>' : '<span style="color:var(--green)">NO</span>',
      "+" + fmt1(sp.spikePctAboveMA) + "% above MA &nbsp;|&nbsp; Pre-regime: <strong>" + esc(sp.preEventRegime) + "</strong>");

    h += card("Severity Score",
      '<span class="e12MonoVal">' + fmt0(sev.score) + "</span>",
      "VIX " + fmt1(sev.vixSpikePct) + "% &nbsp;|&nbsp; SPX " + fmt2(sev.spxGapPct) + "% &nbsp;|&nbsp; Oil " + fmt2(sev.oilGapPct) + "%",
      sev.score);

    h += card("Dealer Gamma", gammaLabel(dg.netGammaSign),
      "Magnitude: <strong>" + esc(dg.magnitudeBucket) + "</strong>");

    h += card("Cross-Asset Stress",
      '<span class="e12MonoVal">' + fmt0(stress.score) + "</span>",
      esc(stress.label), stress.score);

    $("regimeGrid").innerHTML = h;
  }

  /* ── Edge Decomposition ── */
  function renderEdges(d) {
    var ec = d.edgeComposite || {};
    var edges = ec.edges || [];
    _insightRegistry = [];
    var h = "";
    for (var i = 0; i < edges.length; i++) {
      var e = edges[i];
      var btn = inlineInsightBtn("edge", e.edgeId || e.label, e, e.label + " \u2014 GPT-5.3");
      h += card(e.label,
        '<span class="e12MonoVal">' + fmt1(e.score) + "</span> <span style='font-size:13px;color:var(--muted);font-weight:600'>/ 100</span>",
        esc(e.interpretation) + "<br>" + btn, e.score);
    }
    h += card("Composite Edge",
      '<span class="e12MonoVal">' + fmt1(ec.score) + "</span> <span style='font-size:13px;color:var(--muted);font-weight:600'>/ 100</span>",
      esc(ec.label), ec.score);
    $("edgeGrid").innerHTML = h;
  }

  /* ── OU Model ── */
  function renderOU(d) {
    var ou = d.ouModel || {};
    var ts = d.termStructure || {};
    var h = "";
    h += card("Modeled Half-Life",
      '<span class="e12MonoVal">' + fmt1(ou.modeledHalfLifeDays) + "d</span>",
      "\u03BA: " + fmt2(ou.kappa) + " &nbsp;|&nbsp; n: " + fmt0(ou.nObs));
    h += card("Long-Run Mean (\u03B8)",
      '<span class="e12MonoVal">' + fmt2(ou.theta) + "</span>",
      "\u03C3: " + fmt2(ou.sigma) + " &nbsp;|&nbsp; R\u00B2: " + fmt2(ou.rSquared));
    h += card("IV 30d / 60d / 90d",
      '<span class="e12MonoVal">' + fmt1(ts.iv_30d) + " / " + fmt1(ts.iv_60d) + " / " + fmt1(ts.iv_90d) + "</span>",
      "SPX ATM implied vol by DTE");
    $("ouGrid").innerHTML = h;
    renderForwardCurve(d.forwardCurve || []);
  }

  /* ── SVG Forward Curve ── */
  function renderForwardCurve(curve) {
    var wrap = $("forwardCurveChart");
    if (!curve.length) { wrap.innerHTML = '<div style="text-align:center;padding:40px;color:var(--muted);font-size:12px;">No forward curve data.</div>'; return; }

    var W = 640, H = 180, pad = { l: 52, r: 24, t: 16, b: 34 };
    var xMax = curve[curve.length - 1].horizon_days;
    var yVals = curve.map(function (p) { return p.expected_vix; });
    var yMin = Math.floor(Math.min.apply(null, yVals) - 1);
    var yMax = Math.ceil(Math.max.apply(null, yVals) + 1);

    function sx(d) { return pad.l + (d / xMax) * (W - pad.l - pad.r); }
    function sy(v) { return pad.t + (1 - (v - yMin) / (yMax - yMin)) * (H - pad.t - pad.b); }

    var pts = curve.map(function (p) { return sx(p.horizon_days) + "," + sy(p.expected_vix); }).join(" ");

    var svg = '<svg viewBox="0 0 ' + W + " " + H + '" xmlns="http://www.w3.org/2000/svg" style="font-family:-apple-system,system-ui,sans-serif">';
    // Grid lines
    var yStep = Math.max(1, Math.round((yMax - yMin) / 4));
    for (var yy = yMin; yy <= yMax; yy += yStep) {
      svg += '<line x1="' + pad.l + '" y1="' + sy(yy) + '" x2="' + (W - pad.r) + '" y2="' + sy(yy) + '" stroke="rgba(15,23,42,0.06)" />';
      svg += '<text x="' + (pad.l - 8) + '" y="' + (sy(yy) + 3) + '" fill="rgba(11,11,15,0.4)" font-size="10" text-anchor="end" font-weight="600">' + yy + "</text>";
    }
    // X labels
    for (var i = 0; i < curve.length; i++) {
      var cd = curve[i].horizon_days;
      svg += '<text x="' + sx(cd) + '" y="' + (H - 10) + '" fill="rgba(11,11,15,0.4)" font-size="10" text-anchor="middle" font-weight="600">' + cd + "d</text>";
    }
    // Axes
    svg += '<line x1="' + pad.l + '" y1="' + (H - pad.b) + '" x2="' + (W - pad.r) + '" y2="' + (H - pad.b) + '" stroke="rgba(15,23,42,0.10)" />';
    svg += '<line x1="' + pad.l + '" y1="' + pad.t + '" x2="' + pad.l + '" y2="' + (H - pad.b) + '" stroke="rgba(15,23,42,0.10)" />';
    // Area fill
    var areaPath = "M" + sx(curve[0].horizon_days) + "," + sy(yMin) + " ";
    for (var j = 0; j < curve.length; j++) areaPath += "L" + sx(curve[j].horizon_days) + "," + sy(curve[j].expected_vix) + " ";
    areaPath += "L" + sx(curve[curve.length - 1].horizon_days) + "," + sy(yMin) + " Z";
    svg += '<path d="' + areaPath + '" fill="rgba(0,122,255,0.06)" />';
    // Line
    svg += '<polyline points="' + pts + '" fill="none" stroke="rgba(0,122,255,0.85)" stroke-width="2.5" stroke-linejoin="round" />';
    // Dots + labels
    for (var k = 0; k < curve.length; k++) {
      var p = curve[k];
      svg += '<circle cx="' + sx(p.horizon_days) + '" cy="' + sy(p.expected_vix) + '" r="4" fill="rgba(0,122,255,0.9)" stroke="#fff" stroke-width="1.5" />';
      svg += '<text x="' + sx(p.horizon_days) + '" y="' + (sy(p.expected_vix) - 10) + '" fill="rgba(11,11,15,0.7)" font-size="10" text-anchor="middle" font-weight="700">' + fmt1(p.expected_vix) + "</text>";
    }
    svg += "</svg>";
    wrap.innerHTML = svg;
  }

  /* ── Scenario Probabilities (algorithmically derived) ── */
  function renderScenarios(d) {
    var sc = d.scenarios || {};
    var g = "";
    g += '<div class="e12ScenarioCard"><div class="e12ScenarioLabel">Contained</div><div class="e12ScenarioProb" style="color:var(--green)">' + fmtPct(sc.pContained) + '</div><div class="e12ScenarioCaption">Spike fades within days</div></div>';
    g += '<div class="e12ScenarioCard"><div class="e12ScenarioLabel">Disruption</div><div class="e12ScenarioProb" style="color:var(--amber)">' + fmtPct(sc.pDisruption) + '</div><div class="e12ScenarioCaption">Secondary expansion, then decay</div></div>';
    g += '<div class="e12ScenarioCard"><div class="e12ScenarioLabel">Escalation</div><div class="e12ScenarioProb" style="color:var(--red)">' + fmtPct(sc.pEscalation) + '</div><div class="e12ScenarioCaption">Multi-day vol expansion</div></div>';
    $("scenarioGrid").innerHTML = g;

    var adj = "";
    if (sc.adjustments && sc.adjustments.length) {
      adj += '<div style="padding:12px 16px;border-radius:var(--radius-card);border:1px solid var(--border);background:var(--surfaceSolid);">';
      adj += '<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;color:var(--muted);margin-bottom:8px;">Model Reasoning (' + sc.adjustments.length + ' factors)</div>';
      for (var i = 0; i < sc.adjustments.length; i++) {
        adj += '<div style="font-size:11px;color:var(--text);margin-bottom:4px;line-height:1.4;padding-left:12px;text-indent:-12px;">\u2022 ' + esc(sc.adjustments[i]) + "</div>";
      }
      adj += "</div>";
    }
    $("scenarioAdjustments").innerHTML = adj;
  }

  /* ── Recommendation ── */
  function renderRecommendation(d) {
    var rec = d.recommendation;
    if (!rec) { $("recContent").innerHTML = '<div style="padding:20px;color:var(--muted);font-size:12px;">No recommendation available.</div>'; return; }

    var h = '<div class="e12Rec">';
    h += '<div class="e12RecTitle">' + esc(rec.primary) + "</div>";
    h += '<div class="e12RecBody">' + esc(rec.primaryRationale) + "</div>";
    h += "</div>";

    if (rec.guardrails && rec.guardrails.length) {
      for (var i = 0; i < rec.guardrails.length; i++) {
        h += '<div class="e12Guardrail">\u26A0 ' + esc(rec.guardrails[i]) + "</div>";
      }
    }

    if (rec.positionSize) {
      var ps = rec.positionSize;
      h += '<div class="e12Grid" style="margin-top:12px;">';
      h += card("Contracts", '<span class="e12MonoVal">' + (ps.contracts || 0) + "</span>", esc(ps.note));
      h += card("Risk Budget", '<span class="e12MonoVal">$' + fmt0(ps.adjustedBudget) + "</span>",
        "Severity " + fmtPct(ps.severityScale) + " &nbsp;|&nbsp; Esc " + fmtPct(ps.escalationScale));
      h += card("Total Max Loss", '<span class="e12MonoVal e12Negative">$' + fmt0(ps.totalMaxLoss) + "</span>",
        "Per contract: $" + fmt0(ps.maxLossPerContract));
      h += "</div>";
    }
    $("recContent").innerHTML = h;
  }

  /* ── MC Table ── */
  function renderMC(d) {
    var mc = d.monteCarlo;
    if (!mc || !mc.structures) { $("mcBody").innerHTML = ""; return; }
    var h = "";
    var structs = mc.structures.slice().sort(function (a, b) { return b.sharpe - a.sharpe; });
    for (var i = 0; i < structs.length; i++) {
      var s = structs[i];
      var pnlClass = s.expectedPnL >= 0 ? "e12Positive" : "e12Negative";
      h += "<tr>";
      h += "<td><strong>" + esc(s.name) + "</strong></td>";
      h += '<td class="e12MonoVal ' + pnlClass + '">$' + fmt2(s.expectedPnL) + "</td>";
      h += '<td class="e12MonoVal">' + fmtPct(s.pProfit) + "</td>";
      h += '<td class="e12MonoVal">' + fmt2(s.sharpe) + "</td>";
      h += '<td class="e12MonoVal e12Negative">$' + fmt2(s.cvar95) + "</td>";
      h += '<td class="e12MonoVal e12Negative">$' + fmt2(s.maxLoss) + "</td>";
      h += '<td class="e12MonoVal e12Positive">$' + fmt2(s.maxGain) + "</td>";
      h += "</tr>";
    }
    $("mcBody").innerHTML = h;
  }

  /* ── Historical Table ── */
  function renderHistorical(d) {
    var events = d.historicalComparisons || [];
    var h = "";
    for (var i = 0; i < events.length; i++) {
      var e = events[i];
      var pre = e.vix_pre_close || 0;
      var opn = e.vix_event_open || 0;
      var peak = e.peak_vix || opn;
      var jumpR = opn > 0 ? (peak / opn).toFixed(2) + "x" : "\u2014";
      var decay5 = opn > 0 && e.vix_5d_after ? (((e.vix_5d_after - opn) / opn) * 100).toFixed(1) + "%" : "\u2014";

      var oBadge = "e12Badge--green";
      if (e.outcome_class === "disruption") oBadge = "e12Badge--amber";
      else if (e.outcome_class === "escalation") oBadge = "e12Badge--red";

      h += "<tr>";
      h += "<td>" + esc(e.description || e.event_id) + "</td>";
      h += "<td>" + esc(e.event_date) + "</td>";
      h += '<td class="e12MonoVal">' + fmt1(pre) + "</td>";
      h += '<td class="e12MonoVal">' + fmt1(opn) + "</td>";
      h += '<td class="e12MonoVal">' + fmt1(peak) + "</td>";
      h += '<td class="e12MonoVal">' + jumpR + "</td>";
      h += '<td class="e12MonoVal">' + fmt2(e.spx_gap_pct) + "%</td>";
      h += '<td class="e12MonoVal">' + fmt2(e.oil_gap_pct) + "%</td>";
      h += '<td><span class="e12Badge ' + oBadge + '">' + esc(e.outcome_class) + "</span></td>";
      h += '<td class="e12MonoVal">' + decay5 + "</td>";
      h += "</tr>";
    }
    $("histBody").innerHTML = h;
  }

  /* ── Main scan ── */
  function runScan() {
    $("status").textContent = "Running Engine 12 analysis\u2026";
    $("results").classList.add("hidden");
    $("runBtn").disabled = true;
    if (typeof RavenLoading !== "undefined") RavenLoading.show();

    fetch("/api/engine12/scan")
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        console.log("[Engine 12] scan response:", data);
        if (data.status === "error") {
          $("status").textContent = "Error: " + (data.message || "Unknown");
          return;
        }
        lastPayload = data;
        try { renderRegime(data); } catch (e) { console.error("[E12] renderRegime:", e); }
        try { renderEdges(data); } catch (e) { console.error("[E12] renderEdges:", e); }
        try { renderOU(data); } catch (e) { console.error("[E12] renderOU:", e); }
        try { renderScenarios(data); } catch (e) { console.error("[E12] renderScenarios:", e); }
        try { renderRecommendation(data); } catch (e) { console.error("[E12] renderRecommendation:", e); }
        try { renderMC(data); } catch (e) { console.error("[E12] renderMC:", e); }
        try { renderHistorical(data); } catch (e) { console.error("[E12] renderHistorical:", e); }
        $("results").classList.remove("hidden");
        $("status").textContent = "Analysis complete \u2014 " + (data.asOfDate || "");
      })
      .catch(function (err) {
        console.error("[Engine 12] scan failed:", err);
        $("status").textContent = "Failed: " + err.message;
      })
      .finally(function () {
        $("runBtn").disabled = false;
        if (typeof RavenLoading !== "undefined") RavenLoading.hide();
      });
  }

  $("runBtn").addEventListener("click", runScan);
})();
