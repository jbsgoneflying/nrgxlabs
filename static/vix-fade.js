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
    var h = "";
    for (var i = 0; i < edges.length; i++) {
      var e = edges[i];
      h += card(e.label,
        '<span class="e12MonoVal">' + fmt1(e.score) + "</span> <span style='font-size:13px;color:var(--muted);font-weight:600'>/ 100</span>",
        esc(e.interpretation), e.score);
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

  /* ── Scenario Analysis ── */
  function renderScenarios(d) {
    var sc = d.scenarios || {};
    var g = "";
    g += '<div class="e12ScenarioCard"><div class="e12ScenarioLabel">Contained</div><div class="e12ScenarioProb" style="color:var(--green)">' + fmtPct(sc.pContained) + '</div><div class="e12ScenarioCaption">Spike fades within days</div></div>';
    g += '<div class="e12ScenarioCard"><div class="e12ScenarioLabel">Disruption</div><div class="e12ScenarioProb" style="color:var(--amber)">' + fmtPct(sc.pDisruption) + '</div><div class="e12ScenarioCaption">Secondary expansion, then decay</div></div>';
    g += '<div class="e12ScenarioCard"><div class="e12ScenarioLabel">Escalation</div><div class="e12ScenarioProb" style="color:var(--red)">' + fmtPct(sc.pEscalation) + '</div><div class="e12ScenarioCaption">Multi-day vol expansion</div></div>';
    $("scenarioGrid").innerHTML = g;

    var adj = "";
    if (sc.adjustments && sc.adjustments.length) {
      for (var i = 0; i < sc.adjustments.length; i++) {
        adj += '<div style="font-size:11px;color:var(--muted);margin-bottom:3px;">\u2022 ' + esc(sc.adjustments[i]) + "</div>";
      }
    }
    $("scenarioAdjustments").innerHTML = adj;

    $("slContained").value = Math.round((sc.pContained || 0.55) * 100);
    $("slDisruption").value = Math.round((sc.pDisruption || 0.28) * 100);
    $("slEscalation").value = Math.round((sc.pEscalation || 0.17) * 100);
    updateSliderLabels();
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

  /* ── Slider helpers ── */
  function updateSliderLabels() {
    $("valContained").textContent = $("slContained").value + "%";
    $("valDisruption").textContent = $("slDisruption").value + "%";
    $("valEscalation").textContent = $("slEscalation").value + "%";
  }
  $("slContained").addEventListener("input", updateSliderLabels);
  $("slDisruption").addEventListener("input", updateSliderLabels);
  $("slEscalation").addEventListener("input", updateSliderLabels);

  /* ── Re-simulate ── */
  $("reSimBtn").addEventListener("click", function () {
    var c = parseInt($("slContained").value, 10);
    var d = parseInt($("slDisruption").value, 10);
    var e = parseInt($("slEscalation").value, 10);
    var total = c + d + e;
    if (total <= 0) return;

    var url = "/api/engine12/simulate?p_contained=" + (c / total).toFixed(3) +
      "&p_disruption=" + (d / total).toFixed(3) + "&p_escalation=" + (e / total).toFixed(3);
    if (lastPayload && lastPayload.spike && lastPayload.spike.vixCurrent)
      url += "&vix_current=" + lastPayload.spike.vixCurrent;

    $("status").textContent = "Re-simulating\u2026";
    if (typeof RavenLoading !== "undefined") RavenLoading.show();

    fetch(url).then(function (r) { return r.json(); }).then(function (data) {
      if (data.monteCarlo) renderMC({ monteCarlo: data.monteCarlo });
      if (data.recommendation) renderRecommendation({ recommendation: data.recommendation });
      $("status").textContent = "Re-simulation complete";
    }).catch(function (err) {
      $("status").textContent = "Re-simulation failed: " + err.message;
    }).finally(function () {
      if (typeof RavenLoading !== "undefined") RavenLoading.hide();
    });
  });

  /* ── Main scan ── */
  function runScan() {
    $("status").textContent = "Running Engine 12 analysis\u2026";
    $("results").style.display = "none";
    $("runBtn").disabled = true;
    if (typeof RavenLoading !== "undefined") RavenLoading.show();

    fetch("/api/engine12/scan").then(function (r) { return r.json(); }).then(function (data) {
      if (data.status === "error") {
        $("status").textContent = "Error: " + (data.message || "Unknown");
        return;
      }
      lastPayload = data;
      renderRegime(data);
      renderEdges(data);
      renderOU(data);
      renderScenarios(data);
      renderRecommendation(data);
      renderMC(data);
      renderHistorical(data);
      $("results").style.display = "block";
      $("status").textContent = "Analysis complete \u2014 " + (data.asOfDate || "");
    }).catch(function (err) {
      $("status").textContent = "Failed: " + err.message;
    }).finally(function () {
      $("runBtn").disabled = false;
      if (typeof RavenLoading !== "undefined") RavenLoading.hide();
    });
  }

  $("runBtn").addEventListener("click", runScan);
})();
