/* ── Engine 14 — IC Scenario Simulator frontend ─────────────────────── */

(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var mtmChart = null;
  var lastPayload = null;
  var lastRequestBody = null;
  var exitGrid = [];

  /* Default: entry = today, expiry = next Friday */
  function initDefaults() {
    var now = new Date();
    var entry = now.toISOString().slice(0, 10);
    var dow = now.getDay();                         // 0=Sun..6=Sat
    var daysToFri = (5 - dow + 7) % 7 || 7;
    var fri = new Date(now.getTime() + daysToFri * 86400000);
    var expiry = fri.toISOString().slice(0, 10);
    if (!$("entryDate").value) $("entryDate").value = entry;
    if (!$("expiry").value) $("expiry").value = expiry;
  }

  function setStatus(msg, kind) {
    var el = $("status");
    if (!el) return;
    el.textContent = msg || "";
    el.className = "status" + (kind ? " " + kind : "");
  }

  function showBanner(msg, kind) {
    var el = $("banner");
    if (!el) return;
    if (!msg) { el.style.display = "none"; return; }
    el.textContent = msg;
    el.className = "e14Banner" + (kind ? " " + kind : "");
    el.style.display = "block";
  }

  function fmtNum(x, digits) {
    if (x === null || x === undefined || isNaN(Number(x))) return "—";
    return Number(x).toFixed(digits === undefined ? 1 : digits);
  }

  function fmtPct(x, digits) {
    if (x === null || x === undefined || isNaN(Number(x))) return "—";
    var v = Number(x);
    return (v >= 0 ? "+" : "") + v.toFixed(digits === undefined ? 1 : digits) + "%";
  }

  function pnlColor(x) {
    if (x === null || x === undefined || isNaN(Number(x))) return "muted";
    var v = Number(x);
    if (v >= 50) return "green";
    if (v >= 0) return "blue";
    if (v >= -100) return "amber";
    return "red";
  }

  /* ── Outcome panel ─────────────────────────────────────────────── */

  var OUTCOME_META = {
    earlyTarget:  { label: "Early Target", color: "green" },
    fullCollect:  { label: "Full Collect", color: "blue" },
    whiteKnuckle: { label: "White Knuckle", color: "amber" },
    stopOut:      { label: "Stop Out",     color: "red" },
    breach:       { label: "Breach",       color: "red" },
  };

  function renderOutcomes(dist, targetId, ci) {
    var panel = $(targetId || "outcomePanel");
    if (!panel) return;
    panel.innerHTML = "";
    var keys = ["earlyTarget", "fullCollect", "whiteKnuckle", "stopOut", "breach"];
    var best = null;
    keys.forEach(function (k) {
      var v = (dist && dist[k]) || { pct: 0, n: 0, avgPnlPct: 0, avgDays: 0, maxAdverseExcursionPct: 0 };
      if (!best || v.pct > best.pct) best = Object.assign({ key: k }, v);
    });

    keys.forEach(function (k) {
      var v = (dist && dist[k]) || { pct: 0, n: 0, avgPnlPct: 0, avgDays: 0, maxAdverseExcursionPct: 0 };
      var band = ci && ci[k] ? ci[k] : null;
      var meta = OUTCOME_META[k];
      var pct = Math.max(0, Math.min(100, v.pct));
      var card = document.createElement("div");
      card.className = "e14OutcomeCard" + (best && best.key === k ? " dominant" : "");

      var barInner = '<div class="e14OutcomeBarFill bg' + cap(meta.color) + '" style="width:' + Math.max(2, pct) + '%"></div>';
      if (band && typeof band.pctLow === "number" && typeof band.pctHigh === "number" && band.pctHigh > band.pctLow) {
        var lo = Math.max(0, Math.min(100, band.pctLow));
        var hi = Math.max(0, Math.min(100, band.pctHigh));
        // Overlay: error-bar whisker from lo → hi, drawn on top of the bar.
        barInner +=
          '<div class="e14OutcomeBarCI" style="position:absolute;left:' + lo + '%;width:' + Math.max(0.5, hi - lo) + '%;top:0;bottom:0;' +
          'background:rgba(255,255,255,0.12);border-left:2px solid var(--muted);border-right:2px solid var(--muted);pointer-events:none;"></div>';
      }

      var ciLine = "";
      if (band && typeof band.pctLow === "number" && typeof band.pctHigh === "number") {
        ciLine = '<div class="e14OutcomeMeta" style="opacity:0.85;">90% CI ' +
          fmtNum(band.pctLow) + '–' + fmtNum(band.pctHigh) + '% · P&L ' +
          fmtPct(band.pnlLow) + ' → ' + fmtPct(band.pnlHigh) + '</div>';
      }

      card.innerHTML =
        '<div class="e14OutcomeName">' + meta.label + '</div>' +
        '<div class="e14OutcomePct ' + meta.color + '">' + fmtNum(v.pct) + '%</div>' +
        '<div class="e14OutcomeBar" style="position:relative;">' + barInner + '</div>' +
        '<div class="e14OutcomeMeta">' +
          'n=' + (v.n || 0) + ' · avg ' + fmtPct(v.avgPnlPct) +
          (v.avgDays ? ' · ~' + fmtNum(v.avgDays, 1) + 'd' : '') +
        '</div>' +
        ciLine;
      panel.appendChild(card);
    });
  }

  /* ── Phase E2: Position sizing card ──────────────────────────── */

  function renderSizing(s) {
    var panel = $("sizingPanel");
    var label = $("sizingDivider");
    if (!panel || !label) return;
    if (!s || !s.n) { panel.style.display = "none"; label.style.display = "none"; return; }

    function pct(x) { return (typeof x === "number") ? (x * 100).toFixed(1) + "%" : "—"; }
    function num(x, d) { return (typeof x === "number") ? x.toFixed(d || 2) : "—"; }

    var cards = [];

    // Consensus (most conservative) card — dominant.
    cards.push(
      '<div class="e14Card dominant"><div class="e14CardLabel">Consensus (min of three)</div>' +
      '<div class="e14CardValue blue" style="font-size:18px;">' + pct(s.consensusFraction) + '</div>' +
      '<div class="muted" style="font-size:10px;">fraction of equity · most conservative cap</div></div>'
    );

    // Kelly.
    var k = s.kelly || {};
    cards.push(
      '<div class="e14Card"><div class="e14CardLabel">Kelly (½-Kelly)</div>' +
      '<div class="e14CardValue">' + pct(k.fraction) + '</div>' +
      '<div class="muted" style="font-size:10px;">win prob ' + (typeof k.winProb === "number" ? (k.winProb*100).toFixed(0) + '%' : '—') +
      ' · payoff ' + num(k.payoffRatio, 2) + (k.clamp ? ' · clamped' : '') + '</div></div>'
    );

    // Fixed-fractional.
    var ff = s.fixedFractional || {};
    cards.push(
      '<div class="e14Card"><div class="e14CardLabel">Fixed-Fractional</div>' +
      '<div class="e14CardValue">' + pct(ff.fraction) + '</div>' +
      '<div class="muted" style="font-size:10px;">risk ' + num(ff.riskPerTradePct, 1) + '% / worst loss ' +
      num(ff.worstLossPctCredit, 0) + '% of credit</div></div>'
    );

    // Empirical max-DD.
    var dd = s.empiricalMaxDd || {};
    cards.push(
      '<div class="e14Card"><div class="e14CardLabel">Empirical Max-DD</div>' +
      '<div class="e14CardValue">' + pct(dd.fraction) + '</div>' +
      '<div class="muted" style="font-size:10px;">cap ' + num(dd.maxDrawdownPct, 1) + '% / empirical ' +
      num(dd.empiricalDdPctCredit, 0) + '% of credit</div></div>'
    );

    panel.innerHTML = cards.join("");
    panel.style.display = "grid";
    label.style.display = "";
  }

  /* ── Phase E3: greeks P&L attribution card ───────────────────── */

  function renderGreeksAttribution(g) {
    var panel = $("greeksPanel");
    var label = $("greeksDivider");
    if (!panel || !label) return;
    if (!g || !g.n) { panel.style.display = "none"; label.style.display = "none"; return; }

    var comps = [
      { key: "delta",    label: "Delta",    color: "var(--blue)",   val: g.deltaPct },
      { key: "gamma",    label: "Gamma",    color: "var(--amber)",  val: g.gammaPct },
      { key: "theta",    label: "Theta",    color: "var(--green)",  val: g.thetaPct },
      { key: "vega",     label: "Vega",     color: "var(--red)",    val: g.vegaPct  },
      { key: "residual", label: "Residual", color: "var(--muted)",  val: g.residualPct },
    ];

    var shares = g.shareOfAbsPnl || {};
    function cellVal(v) { return (typeof v === "number") ? (v >= 0 ? "+" : "") + v.toFixed(1) + "%" : "—"; }
    function share(k) { return (typeof shares[k] === "number") ? shares[k].toFixed(0) + "%" : "—"; }

    // Stacked horizontal bar — shares normalized to 100.
    var bar = '<div style="display:flex;height:16px;border-radius:4px;overflow:hidden;margin:10px 0;background:var(--bg2);">';
    comps.forEach(function (c) {
      var w = Math.max(0, (typeof shares[c.key] === "number") ? shares[c.key] : 0);
      if (w <= 0) return;
      bar += '<div title="' + c.label + ': ' + share(c.key) + '" style="background:' + c.color + ';width:' + w + '%;"></div>';
    });
    bar += '</div>';

    // Per-greek stats cards.
    var cards = '<div class="e14Grid" style="gap:10px;">';
    comps.forEach(function (c) {
      cards +=
        '<div class="e14Card"><div class="e14CardLabel" style="color:' + c.color + ';">' + c.label + '</div>' +
        '<div class="e14CardValue">' + cellVal(c.val) + '</div>' +
        '<div class="muted" style="font-size:10px;">' + share(c.key) + ' of |P&amp;L|</div></div>';
    });
    cards += '</div>';

    panel.innerHTML =
      '<div class="e14ExitCard">' +
      '<div class="muted" style="font-size:11px;margin-bottom:4px;">Average decomposition across ' + g.n + ' analogue paths · entry-Taylor approximation · residual absorbs unmodeled IV path and fill slippage.</div>' +
      bar +
      cards +
      '</div>';
    panel.style.display = "";
    label.style.display = "";
  }

  /* ── Phase D: thin-sample banner ─────────────────────────────── */

  function renderThinSampleBanner(ci, analoguesUsed) {
    var id = "thinSampleBanner";
    var existing = document.getElementById(id);
    if (existing) existing.parentNode.removeChild(existing);
    var meta = ci && ci._meta;
    if (!meta || !meta.thinSample) return;
    var parent = $("outcomePanel");
    if (!parent || !parent.parentNode) return;
    var banner = document.createElement("div");
    banner.id = id;
    banner.style.cssText =
      "margin:8px 0 10px;padding:10px 14px;border-radius:8px;border:1px solid var(--amber);" +
      "background:rgba(255,176,32,0.08);color:var(--amber);font-size:12px;line-height:1.5;";
    banner.innerHTML =
      '<strong style="letter-spacing:0.04em;text-transform:uppercase;">Thin sample</strong> · ' +
      'Only ' + (meta.n || analoguesUsed || 0) + ' analogues survived the filter — ' +
      'confidence intervals below are wide. Loosen match criteria (DTE tolerance, regime bucket, EM-multiple) ' +
      'or extend the backfill horizon before leaning on this distribution.';
    parent.parentNode.insertBefore(banner, parent);
  }

  /* ── Phase A: Fill model badge + mid-distribution side-by-side ── */

  function renderFillModelBadge(fm) {
    var badge = $("fillModelBadge");
    if (!badge) return;
    if (!fm || !fm.mode) { badge.style.display = "none"; return; }
    var labels = {
      nbbo:        { text: "NBBO close",     color: "var(--blue)" },
      mid:         { text: "Mid-only",       color: "var(--muted)" },
      mid_penalty: { text: "Mid + penalty",  color: "var(--amber)" },
    };
    var L = labels[fm.mode] || { text: fm.mode, color: "var(--muted)" };
    var mae = fm.maeProxyEnabled ? " · OHLC MAE proxy" : "";
    badge.textContent = L.text + mae;
    badge.style.color = L.color;
    badge.style.borderColor = L.color;
    badge.style.display = "inline-block";
  }

  function renderOutcomesMid(dist) {
    var panel = $("outcomePanelMid");
    var label = $("midDividerLabel");
    if (!panel || !label) return;
    if (!dist || Object.keys(dist).length === 0) {
      panel.style.display = "none"; label.style.display = "none"; return;
    }
    // Reuse the main renderer but strip the unavailable fields gracefully.
    renderOutcomes(dist, "outcomePanelMid");
    panel.style.display = "grid";
    label.style.display = "";
  }

  /* ── Phase C3: Regime match quality card ─────────────────────── */

  function renderRegimeMatchQuality(q, analoguesUsed) {
    var panel = $("regimeMatchPanel");
    var label = $("regimeMatchDivider");
    if (!panel || !label) return;
    if (!q) { panel.style.display = "none"; label.style.display = "none"; return; }

    var cards = [];
    var source = (q.source || "bucket").toString().toLowerCase();
    var sourceBadge;
    var badgeStyleBase = "display:inline-block;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700;letter-spacing:0.04em;text-transform:uppercase;";
    if (source === "knn") {
      sourceBadge = '<span style="' + badgeStyleBase + 'background:var(--blue);color:#fff;">KNN multi-factor</span>';
    } else {
      sourceBadge = '<span style="' + badgeStyleBase + 'background:var(--bg2);color:var(--muted);border:1px solid var(--border);">RV20 bucket</span>';
    }
    var headerRhs = typeof q.n === "number" ? (q.n + " analogues") : "—";
    cards.push(
      '<div class="e14Card"><div class="e14CardLabel">Match Source</div>' +
      '<div class="e14CardValue" style="display:flex;align-items:center;gap:8px;">' + sourceBadge +
      '<span class="muted" style="font-size:11px;">' + headerRhs + '</span></div></div>'
    );

    if (source === "knn") {
      var dMean = (typeof q.meanDistance === "number") ? q.meanDistance.toFixed(2) : "—";
      var dMin  = (typeof q.minDistance  === "number") ? q.minDistance.toFixed(2)  : "—";
      var dMax  = (typeof q.maxDistance  === "number") ? q.maxDistance.toFixed(2)  : "—";
      cards.push(
        '<div class="e14Card"><div class="e14CardLabel">Distance (weighted L2)</div>' +
        '<div class="e14CardValue">' + dMin + ' → ' + dMean + ' → ' + dMax + '</div>' +
        '<div class="muted" style="font-size:10px;">min · mean · max — lower is closer</div></div>'
      );

      var impPct = (typeof q.meanImputationFraction === "number")
        ? (q.meanImputationFraction * 100).toFixed(0) + "%" : "—";
      cards.push(
        '<div class="e14Card"><div class="e14CardLabel">Feature Imputation</div>' +
        '<div class="e14CardValue">' + impPct + '</div>' +
        '<div class="muted" style="font-size:10px;">share of feature cells filled from median</div></div>'
      );

      var kKnn = (typeof q.kKnn === "number") ? q.kKnn : "—";
      var kFb  = (typeof q.kBucketFallback === "number") ? q.kBucketFallback : 0;
      cards.push(
        '<div class="e14Card"><div class="e14CardLabel">Admitted</div>' +
        '<div class="e14CardValue">' + kKnn + ' KNN + ' + kFb + ' fallback</div>' +
        '<div class="muted" style="font-size:10px;">KNN-scored vs. legacy bucket fallback</div></div>'
      );
    } else if (q.bucket) {
      cards.push(
        '<div class="e14Card"><div class="e14CardLabel">RV20 Bucket</div>' +
        '<div class="e14CardValue">' + String(q.bucket) + '</div>' +
        '<div class="muted" style="font-size:10px;">feature store unavailable — using legacy bucket gate</div></div>'
      );
    }

    panel.innerHTML = cards.join("");
    panel.style.display = "grid";
    label.style.display = "";
  }

  /* ── Phase 2: Conditioning modifiers ─────────────────────────── */

  function renderModifiers(cond) {
    var panel = $("modifiersPanel");
    var label = $("modifiersDividerLabel");
    if (!panel || !label) return;
    if (!cond || Object.keys(cond).length === 0) {
      panel.style.display = "none"; label.style.display = "none";
      return;
    }
    var mods = [
      { key: "calendar",     title: "Macro Calendar" },
      { key: "dealerGamma",  title: "Dealer Gamma" },
      { key: "creditStress", title: "Cross-Asset Stress" },
      { key: "gapRegime",    title: "Gap Regime (E13)" },
    ];
    panel.innerHTML = "";
    mods.forEach(function (m) {
      var v = cond[m.key];
      if (!v) return;
      var colorMap = {
        extreme:  "red",
        elevated: "red",
        moderate: "amber",
        low:      "blue",
        none:     "muted",
      };
      var color = colorMap[v.severity] || "muted";
      var statusLine = v.status === "ok" ? "" : ' <span class="muted">[' + v.status + ']</span>';
      var tailBadge = v.tailMultiplier && v.tailMultiplier !== 1
        ? ' · tail ×' + Number(v.tailMultiplier).toFixed(2)
        : "";
      var wrBadge = v.winRateShiftPct && v.winRateShiftPct !== 0
        ? ' · WR ' + fmtPct(v.winRateShiftPct, 1)
        : "";
      var card = document.createElement("div");
      card.className = "e14Card";
      card.innerHTML =
        '<div class="e14CardLabel">' + m.title + statusLine + '</div>' +
        '<div class="e14CardValue ' + color + '" style="font-size:14px;">' +
          (v.severity || "none").toUpperCase() + tailBadge + wrBadge +
        '</div>' +
        '<div class="e14CardCaption">' + (v.note || "") + '</div>';
      panel.appendChild(card);
    });
    // Net summary card
    if ("netTailMultiplier" in cond || "netWinRateShiftPct" in cond) {
      var net = document.createElement("div");
      net.className = "e14Card";
      net.style.borderColor = "var(--blue)";
      net.innerHTML =
        '<div class="e14CardLabel">Net Adjustment</div>' +
        '<div class="e14CardValue blue" style="font-size:16px;">' +
          '×' + Number(cond.netTailMultiplier || 1).toFixed(2) +
          ' · ' + fmtPct(cond.netWinRateShiftPct || 0, 1) +
        '</div>' +
        '<div class="e14CardCaption">Combined tail multiplier × win-rate shift applied to the adjusted distribution.</div>';
      panel.appendChild(net);
    }
    panel.style.display = "grid";
    label.style.display = "";
  }

  function renderAdjusted(adj) {
    var panel = $("adjustedOutcomePanel");
    var label = $("adjustedDividerLabel");
    if (!panel || !label) return;
    if (!adj || Object.keys(adj).length === 0) {
      panel.style.display = "none"; label.style.display = "none";
      return;
    }
    renderOutcomes(adj, "adjustedOutcomePanel");
    panel.style.display = "grid";
    label.style.display = "";
  }

  function cap(s) { return s ? s[0].toUpperCase() + s.slice(1) : s; }

  /* ── Entry state cards ─────────────────────────────────────────── */

  // Distance (% of spot) from entry spot to a short-wing strike, plus that
  // distance expressed in multiples of the 1σ expected move. A multiple >1
  // means the short strike sits *outside* the 1σ cone (safer wing); <1 means
  // it's inside the cone (higher breach probability).
  function shortWingStats(spot, strike, emPct, side) {
    if (!Number.isFinite(spot) || !Number.isFinite(strike) || spot <= 0) return null;
    var pts     = (side === "put") ? (spot - strike) : (strike - spot);  // always positive for a well-formed IC
    var distPct = (pts / spot) * 100;
    var emMult  = (Number.isFinite(emPct) && emPct > 0) ? (distPct / emPct) : null;
    return { distPct: distPct, emMult: emMult, strike: strike, pts: pts };
  }

  // Green = comfortably outside 1σ, blue = just outside, amber = just inside,
  // red = deep inside the cone. Keeps the visual consistent with the rest of
  // the UI (short-put delta, sizing caps, etc.).
  function shortWingColor(emMult) {
    if (emMult === null || emMult === undefined || !Number.isFinite(emMult)) return "";
    if (emMult >= 1.25) return "green";
    if (emMult >= 1.00) return "blue";
    if (emMult >= 0.75) return "amber";
    return "red";
  }

  function renderEntryCards(data, req) {
    var cards = $("entryCards");
    cards.innerHTML = "";
    var es = data.entryState;
    if (!es) {
      cards.innerHTML = '<div class="e14Card"><div class="e14CardLabel">Entry state</div><div class="e14CardValue">—</div><div class="e14CardCaption">Simulator returned no entry state.</div></div>';
      return;
    }

    var spot  = Number(es.userSpot);
    var emPct = Number(es.userEmPct);
    var sp    = req ? Number(req.shortPut)  : NaN;
    var sc    = req ? Number(req.shortCall) : NaN;
    var putWing  = shortWingStats(spot, sp, emPct, "put");
    var callWing = shortWingStats(spot, sc, emPct, "call");

    function wingValue(w) {
      return w ? w.distPct.toFixed(2) + "%" : "—";
    }
    function wingCaption(w) {
      if (!w) return "strike not set";
      var mult = (w.emMult !== null) ? w.emMult.toFixed(2) + "× EM · " : "";
      var pts  = (Number.isFinite(w.pts)) ? " · " + fmtNum(w.pts, (w.pts === Math.round(w.pts) ? 0 : 1)) + " pts" : "";
      return mult + "K=" + w.strike + pts;
    }

    // Order is deliberate: row 1 shows the vol-cone view (EM + both short
    // wings + where they sit vs spot and wing width) so the desk can see at
    // a glance whether the structure is inside or outside the 1σ cone. Row 2
    // holds replay metadata + outcome stats.
    var items = [
      { label: "1σ EM %",         value: fmtNum(es.userEmPct, 2) + "%" },
      { label: "Short PUT Dist",  value: wingValue(putWing),              caption: wingCaption(putWing),  colorClass: shortWingColor(putWing  && putWing.emMult) },
      { label: "Short CALL Dist", value: wingValue(callWing),             caption: wingCaption(callWing), colorClass: shortWingColor(callWing && callWing.emMult) },
      { label: "Spot (Entry)",    value: fmtNum(es.userSpot, 2), caption: (es.userSpotIsLive === false && es.userSpotAsOf) ? ("as of " + es.userSpotAsOf + " (market closed)") : undefined, colorClass: (es.userSpotIsLive === false ? "amber" : "") },
      { label: "Wing Width",      value: es.wingWidth,                    caption: "smaller of put/call wings" },
      { label: "Analogues Used",  value: data.analoguesUsed,              caption: "of " + (data.analoguesConsidered || 0) + " candidates" },
      { label: "Regime Bucket",   value: es.regimeBucket,                 caption: "proxy: RV20 percentile" },
      { label: "Mean P&L",        value: fmtPct(data.expectedValue.meanPnlPct, 1), caption: "across all analogues" },
      { label: "Median P&L",      value: fmtPct(data.expectedValue.medianPnlPct, 1) },
      { label: "Sharpe (proxy)",  value: fmtNum(data.expectedValue.sharpeProxy, 2) },
    ];
    items.forEach(function (it) {
      var card = document.createElement("div");
      card.className = "e14Card";
      var valCls = "e14CardValue" + (it.colorClass ? " " + it.colorClass : "");
      card.innerHTML =
        '<div class="e14CardLabel">' + it.label + '</div>' +
        '<div class="' + valCls + '">' + (it.value === undefined || it.value === null ? "—" : it.value) + '</div>' +
        (it.caption ? '<div class="e14CardCaption">' + it.caption + '</div>' : '');
      cards.appendChild(card);
    });
  }

  /* ── MTM timeline chart ────────────────────────────────────────── */

  function renderMtmChart(timeline) {
    var canvas = $("mtmChart");
    if (!canvas || !window.Chart) return;
    var labels = timeline.map(function (r) { return "DTE " + r.dte; });
    var p10 = timeline.map(function (r) { return r.p10; });
    var p50 = timeline.map(function (r) { return r.p50; });
    var p90 = timeline.map(function (r) { return r.p90; });

    if (mtmChart) { mtmChart.destroy(); mtmChart = null; }

    mtmChart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          { label: "P90", data: p90, borderColor: "#34c759", backgroundColor: "rgba(52,199,89,0.12)", fill: "+1", tension: 0.25, pointRadius: 2 },
          { label: "P50", data: p50, borderColor: "#0a84ff", backgroundColor: "rgba(10,132,255,0.10)", fill: false, borderWidth: 2, tension: 0.25, pointRadius: 3 },
          { label: "P10", data: p10, borderColor: "#ff3b30", backgroundColor: "rgba(255,59,48,0.08)", fill: false, tension: 0.25, pointRadius: 2 },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          y: { title: { display: true, text: "P&L (% of credit)" }, grid: { color: "rgba(0,0,0,0.05)" } },
          x: { title: { display: true, text: "Days to Expiry" }, grid: { color: "rgba(0,0,0,0.03)" } },
        },
        plugins: {
          legend: { position: "bottom" },
          tooltip: { callbacks: {
            label: function (ctx) { return ctx.dataset.label + ": " + ctx.parsed.y.toFixed(1) + "%"; }
          }},
        },
      },
    });
  }

  /* ── Exit-rule panel ───────────────────────────────────────────── */

  function renderExitPanel(opt, req) {
    var el = $("exitPanel");
    if (!opt) { el.innerHTML = ""; return; }
    var changed = (opt.recommendedProfitTarget !== req.profitTargetPct)
                || (opt.recommendedStopLoss !== req.stopLossPct);
    var delta = opt.deltaFromDefault || {};
    el.innerHTML =
      '<div class="e14ExitCard">' +
        '<div class="e14ExitRow">' +
          '<div><div class="e14ExitLabel">Profit Target</div>' +
            '<div class="e14ExitValue ' + (changed ? "blue" : "") + '">' +
              fmtNum(opt.recommendedProfitTarget, 0) + '%' +
            '</div></div>' +
          '<div><div class="e14ExitLabel">Stop Loss</div>' +
            '<div class="e14ExitValue ' + (changed ? "blue" : "") + '">' +
              fmtNum(opt.recommendedStopLoss, 0) + '%' +
            '</div></div>' +
          '<div><div class="e14ExitLabel">Δ Win Rate</div>' +
            '<div class="e14ExitValue ' + (delta.winRatePct > 0 ? "green" : delta.winRatePct < 0 ? "red" : "") + '">' +
              fmtPct(delta.winRatePct, 1) +
            '</div></div>' +
          '<div><div class="e14ExitLabel">Δ Avg P&L</div>' +
            '<div class="e14ExitValue ' + (delta.avgPnlPct > 0 ? "green" : delta.avgPnlPct < 0 ? "red" : "") + '">' +
              fmtPct(delta.avgPnlPct, 1) +
            '</div></div>' +
        '</div>' +
        '<div class="e14ExitDelta" style="margin-top:10px">' +
          (changed
            ? "Historical grid search suggests these bands improve both win-rate and average P&L vs your defaults."
            : "Your current exit rules are already near-optimal on this analogue pool.") +
        '</div>' +
      '</div>';
  }

  /* ── Notes + analogue table ────────────────────────────────────── */

  function renderNotes(notes) {
    var el = $("notesList");
    el.innerHTML = "";
    (notes || []).forEach(function (n) {
      var d = document.createElement("div");
      d.className = "e14Note";
      d.textContent = n;
      el.appendChild(d);
    });
  }

  function renderAnaloguesTable(rows) {
    var el = $("analoguesTableWrap");
    if (!rows || !rows.length) { el.innerHTML = '<div class="muted">No analogues to display.</div>'; return; }
    var html = ['<table class="e14Table"><thead><tr>',
      '<th>Entry</th><th>Expiry</th><th>Outcome</th><th>Exit Day</th><th>P&L %</th><th>MAE %</th>',
      '<th>SP</th><th>LP</th><th>SC</th><th>LC</th><th>Breached</th>',
      '</tr></thead><tbody>'];
    rows.slice(0, 100).forEach(function (r) {
      var c = pnlColor(r.pnlPct);
      html.push('<tr>');
      html.push('<td>' + r.entryDate + '</td>');
      html.push('<td>' + r.expiryDate + '</td>');
      html.push('<td>' + (OUTCOME_META[r.outcome] ? OUTCOME_META[r.outcome].label : r.outcome) + '</td>');
      html.push('<td>' + r.exitDay + '</td>');
      html.push('<td class="' + c + '">' + fmtPct(r.pnlPct) + '</td>');
      html.push('<td class="amber">' + fmtPct(r.mae) + '</td>');
      html.push('<td>' + fmtNum(r.mappedStrikes.shortPut, 1) + '</td>');
      html.push('<td>' + fmtNum(r.mappedStrikes.longPut, 1) + '</td>');
      html.push('<td>' + fmtNum(r.mappedStrikes.shortCall, 1) + '</td>');
      html.push('<td>' + fmtNum(r.mappedStrikes.longCall, 1) + '</td>');
      html.push('<td>' + (r.breached ? '<span class="red">yes</span>' : '<span class="muted">no</span>') + '</td>');
      html.push('</tr>');
    });
    html.push('</tbody></table>');
    el.innerHTML = html.join("");
  }

  /* ── Reconciliation + pre-check (Stages 1.5 / 2 / 3 / 4) ─────── */

  var lastReconcile = null;

  var RECON_PRIMARY_KEYS = [
    "regimeBucket", "expectedMovePct", "emMultipleLabel",
    "policyConstraints", "creditQuad", "llmVerdict",
  ];

  function reconcileClass(status) {
    if (status === "agree") return "rAgree";
    if (status === "drift") return "rDrift";
    if (status === "mismatch") return "rMismatch";
    return "rNa";
  }

  function reconcileShortLabel(c) {
    var short = {
      regimeBucket: "Regime",
      spotPrice: "Spot",
      expectedMovePct: "EM%",
      emMultipleLabel: "Box",
      deskEmFloor: "Floor",
      policyConstraints: "Policy",
      breachRate: "Breach",
      conditioningNetEffect: "Cond",
      creditQuad: "Credit",
      llmVerdict: "Advisor",
      llmStrikesMatchUser: "Strikes",
      llmWingMatchUser: "Wing",
    };
    return short[c.key] || c.label || c.key;
  }

  function renderReconciliation(reconcile) {
    var wrap = $("reconcileWrap");
    if (!wrap || !reconcile || !reconcile.overall) return;
    lastReconcile = reconcile;
    wrap.style.display = "";

    var overallEl = $("reconcileOverall");
    var ov = reconcile.overall || {};
    var status = ov.status || "na";
    overallEl.className = "e14ReconcileOverall " + reconcileClass(status);
    var counts = ov.counts || {};
    overallEl.textContent =
      status.toUpperCase() +
      " · " + (counts.agree || 0) + "✓ " +
      (counts.drift || 0) + "~ " +
      (counts.mismatch || 0) + "✗" +
      ((counts.na || 0) ? " " + counts.na + " n/a" : "");

    var chipsEl = $("reconcileChips");
    chipsEl.innerHTML = "";
    var checks = reconcile.checks || [];
    var primary = checks.filter(function (c) { return RECON_PRIMARY_KEYS.indexOf(c.key) >= 0; });
    primary.forEach(function (c) {
      var chip = document.createElement("span");
      chip.className = "e14ReconcileChip " + reconcileClass(c.status);
      chip.title = (c.rule ? c.rule + "\n\n" : "") + (c.note || "");
      chip.innerHTML = '<span class="dot"></span><span>' +
        reconcileShortLabel(c) + " · " + (c.status || "na").toUpperCase() + "</span>";
      chipsEl.appendChild(chip);
    });

    var findings = $("reconcileFindings");
    var list = ov.topFindings || [];
    if (list.length) {
      findings.style.display = "";
      var lis = list.map(function (s) { return "<li>" + escapeHtml(s) + "</li>"; }).join("");
      findings.innerHTML = "<strong>Top findings:</strong><ul>" + lis + "</ul>";
    } else {
      findings.style.display = "none";
    }

    var drawer = $("reconcileDrawer");
    drawer.innerHTML = checks.map(function (c) {
      var e2v = c.e2 == null ? "—" : (typeof c.e2 === "object" ? JSON.stringify(c.e2) : String(c.e2));
      var e14v = c.e14 == null ? "—" : (typeof c.e14 === "object" ? JSON.stringify(c.e14) : String(c.e14));
      return [
        '<div class="e14ReconcileRow">',
          '<div class="reconLabel">', escapeHtml(c.label || c.key), '</div>',
          '<div><div class="reconSrc">Engine 2</div><div class="reconVal">', escapeHtml(e2v), '</div></div>',
          '<div><div class="reconSrc">Engine 14</div><div class="reconVal">', escapeHtml(e14v), '</div></div>',
          '<div class="reconStatus ', reconcileClass(c.status), '">',
            (c.status || "na").toUpperCase(),
            c.note ? '<div class="reconNote">' + escapeHtml(c.note) + '</div>' : '',
          '</div>',
        '</div>',
      ].join("");
    }).join("");
  }

  function escapeHtml(s) {
    s = String(s == null ? "" : s);
    return s.replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function renderConditioningSummary(summary) {
    var el = $("conditioningSummary");
    if (!el) return;
    if (!summary || !summary.humanSummary) { el.style.display = "none"; return; }
    el.style.display = "";
    el.className = "e14ConditioningSummary " + (summary.direction || "flat");
    el.textContent = summary.humanSummary;
  }

  /* ── Pre-check banner (Stage 3) ───────────────────────────────── */

  function renderPreCheck(result) {
    var el = $("preCheckBanner");
    if (!el) return;
    el.innerHTML = "";
    if (!result) return;
    var blocks = result.blocks || [];
    var warnings = result.warnings || [];
    if (!blocks.length && !warnings.length) return;

    if (blocks.length) {
      var div = document.createElement("div");
      div.className = "e14PreCheckBanner block";
      var btnHtml = "";
      if (result.suggestion && result.suggestion.strikes) {
        btnHtml = ' <button type="button" class="applyFix" id="applyFixBtn">Apply nearest strikes</button>';
      }
      var bullets = blocks.map(function (b) {
        var missingList = "";
        if (b.missing && b.missing.length) {
          missingList = "<ul>" + b.missing.map(function (m) {
            return "<li>" + escapeHtml(m.leg) + " = " + m.strike +
              " · nearest live = <strong>" + m.nearest + "</strong></li>";
          }).join("") + "</ul>";
        }
        return "<li>" + escapeHtml(b.message) + missingList + "</li>";
      }).join("");
      div.innerHTML = "<strong>Blocked — fix before running:</strong><ul>" + bullets + "</ul>" + btnHtml;
      el.appendChild(div);

      var btn = document.getElementById("applyFixBtn");
      if (btn && result.suggestion && result.suggestion.strikes) {
        btn.onclick = function () {
          var s = result.suggestion.strikes;
          if (s.shortPut != null)  $("shortPut").value  = s.shortPut;
          if (s.longPut != null)   $("longPut").value   = s.longPut;
          if (s.shortCall != null) $("shortCall").value = s.shortCall;
          if (s.longCall != null)  $("longCall").value  = s.longCall;
          el.innerHTML = "";
          setStatus("Strikes updated to nearest live quotes. Re-run pre-check.", "info");
        };
      }
    }

    if (warnings.length) {
      var wdiv = document.createElement("div");
      wdiv.className = "e14PreCheckBanner warn";
      wdiv.innerHTML = "<strong>Warnings:</strong><ul>" +
        warnings.map(function (w) { return "<li>" + escapeHtml(w.message) + "</li>"; }).join("") +
        "</ul>";
      el.appendChild(wdiv);
    }
  }

  async function runPreCheck() {
    var body;
    try { body = buildRequestBody(); } catch (e) { return null; }
    if (!body.shortPut || !body.longPut || !body.shortCall || !body.longCall
        || !body.creditReceived || !body.expiry) {
      return null;  // user hasn't filled the form yet; skip silently
    }
    try {
      var resp = await fetch("/api/ic-scenario/pre-check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      var data = await resp.json();
      renderPreCheck(data);
      return data;
    } catch (e) {
      return null;
    }
  }

  async function runReconcile(scenario, requestBody) {
    try {
      var resp = await fetch("/api/ic-scenario/reconcile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scenario: scenario,
          runAdvisor: true,
          checkLiveChain: true,
        }),
      });
      // Match the robustness pattern from runScenario: tolerate HTML
      // gateway-timeout pages instead of letting resp.json() explode.
      var ct = (resp.headers.get("content-type") || "");
      if (!resp.ok || ct.indexOf("application/json") === -1) {
        if (window.console) console.warn("[E14] reconcile non-JSON or !ok:", resp.status, ct);
        return;
      }
      var data = await resp.json();
      if (data && data.reconcile) {
        lastReconcile = data.reconcile;
        renderReconciliation(data.reconcile);
      }
    } catch (e) {
      if (window.console) console.warn("[E14] reconcile failed:", e);
    }
  }

  /* ── Submit handler ────────────────────────────────────────────── */

  function buildRequestBody() {
    return {
      underlying:       "SPX",
      entryDate:        $("entryDate").value,
      expiry:           $("expiry").value,
      shortPut:         Number($("shortPut").value),
      longPut:          Number($("longPut").value),
      shortCall:        Number($("shortCall").value),
      longCall:         Number($("longCall").value),
      creditReceived:   Number($("creditReceived").value),
      profitTargetPct:  Number($("profitTargetPct").value || 50),
      stopLossPct:      Number($("stopLossPct").value || 200),
      seasonMode:       $("seasonMode").value || "none",
    };
  }

  async function runScenario(evt) {
    if (evt) evt.preventDefault();
    showBanner("");
    setStatus("Running replay…");

    var body;
    try { body = buildRequestBody(); }
    catch (e) { setStatus("Invalid input: " + e.message, "error"); return; }

    // Front-end sanity
    if (!(body.longPut < body.shortPut && body.shortPut < body.shortCall && body.shortCall < body.longCall)) {
      setStatus("Strikes must satisfy: longPut < shortPut < shortCall < longCall", "error");
      return;
    }

    // Stage 3: pre-check blocks submission on missing strikes.
    var pre = await runPreCheck();
    if (pre && pre.ok === false) {
      setStatus("Pre-check blocked submission. Fix the issues above and retry.", "error");
      return;
    }

    var t0 = Date.now();
    try {
      var resp = await fetch("/api/ic-scenario", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      // Gateway timeouts (504) and Cloudflare origin timeouts (524) on a
      // cold path come back as HTML error pages, not JSON. Read as text
      // first so resp.json() can't throw mid-flow, then branch on status
      // and content-type for a desk-readable message instead of
      // "Unexpected token '<'…".
      var raw = await resp.text();
      var data = null;
      var looksLikeJson = (resp.headers.get("content-type") || "").indexOf("application/json") !== -1;
      if (looksLikeJson) {
        try { data = JSON.parse(raw); } catch (_) { data = null; }
      }
      if (!resp.ok || data === null) {
        var msg;
        if (data && data.detail) {
          msg = data.detail;
        } else if (resp.status === 504 || resp.status === 524 || resp.status === 522) {
          msg = "Server took too long to respond (cold cache). Give it a few seconds and retry — the next call will be fast.";
        } else if (!looksLikeJson) {
          msg = "Gateway returned an HTML error page (HTTP " + resp.status + "). The engine is likely warming up; retry in a few seconds.";
        } else {
          msg = "HTTP " + resp.status;
        }
        setStatus("Failed: " + msg, "error");
        showBanner(msg, "red");
        return;
      }
      render(data, body);
      var ms = Date.now() - t0;
      setStatus("Done in " + ms + "ms · " + (data.analoguesUsed || 0) + " analogues", "success");
      // Stage 1.5/2: reconcile against Engine 2 (async, doesn't block UI).
      runReconcile(data, body);
    } catch (e) {
      setStatus("Network error: " + e.message, "error");
    }
  }

  // Build a compact, chat-friendly slice of the scenario payload.
  // Full payload includes matchedAnalogues (50-70 rows) and a per-day mtm
  // grid which blow through the chat context budget without adding signal.
  function buildChatContext(data, req) {
    if (!data) return null;
    var ctx = {};
    var keep = [
      "asOf",
      "analoguesUsed",
      "analoguesConsidered",
      "entryState",
      "fillModel",
      "regimeMatchQuality",
      "outcomeDistribution",
      "outcomeDistributionMid",
      "outcomeDistributionCI",
      "adjustedOutcomeDistribution",
      "conditioningModifiers",
      "conditioningNotes",
      "expectedValue",
      "sizing",
      "exitRulesOptimization",
      "greeksAttribution",
    ];
    for (var i = 0; i < keep.length; i++) {
      var k = keep[i];
      if (data[k] !== undefined && data[k] !== null) ctx[k] = data[k];
    }
    // mtmTimeline: keep the p10/p50/p90 shape but thin to ≤20 points.
    var tl = data.mtmTimeline;
    if (Array.isArray(tl) && tl.length) {
      var step = Math.max(1, Math.ceil(tl.length / 20));
      var thin = [];
      for (var j = 0; j < tl.length; j += step) thin.push(tl[j]);
      if (thin[thin.length - 1] !== tl[tl.length - 1]) thin.push(tl[tl.length - 1]);
      ctx.mtmTimeline = thin;
    }
    // Matched analogues: keep a top-5 preview so the chat can cite examples
    // without drowning in the full table.
    var rows = Array.isArray(data.matchedAnalogues) ? data.matchedAnalogues : [];
    if (rows.length) {
      ctx.matchedAnaloguesPreview = rows.slice(0, 5);
      ctx.matchedAnaloguesCount = rows.length;
    }
    if (req) {
      ctx.request = {
        underlying: req.underlying,
        entryDate: req.entryDate,
        expiry: req.expiry,
        shortPut: req.shortPut,
        longPut: req.longPut,
        shortCall: req.shortCall,
        longCall: req.longCall,
        creditReceived: req.creditReceived,
        profitTargetPct: req.profitTargetPct,
        stopLossPct: req.stopLossPct,
        seasonMode: req.seasonMode,
      };
    }
    return ctx;
  }

  function pushRavenChatContext(data, req) {
    if (!window.RavenChat || typeof window.RavenChat.setEngineContext !== "function") return;
    try {
      window.RavenChat.setEngineContext("engine14", buildChatContext(data, req));
    } catch (e) {
      // Never let chat wiring break the engine UI.
      if (window.console) console.warn("[E14] RavenChat context push failed:", e);
    }
  }

  function render(data, req) {
    lastPayload = data;
    lastRequestBody = req;
    pushRavenChatContext(data, req);
    $("results").classList.remove("hidden");
    if ((data.analoguesUsed || 0) === 0) {
      showBanner(
        (data.conditioningNotes && data.conditioningNotes[0])
          || "No analogues available. Run the backfill to populate the chain cache.",
        "red"
      );
    }
    renderEntryCards(data, req);
    renderRegimeMatchQuality(data.regimeMatchQuality, data.analoguesUsed);
    renderFillModelBadge(data.fillModel);
    renderThinSampleBanner(data.outcomeDistributionCI, data.analoguesUsed);
    renderOutcomes(data.outcomeDistribution, "outcomePanel", data.outcomeDistributionCI);
    renderOutcomesMid(data.outcomeDistributionMid);
    renderAdjusted(data.adjustedOutcomeDistribution);
    renderConditioningSummary(data.conditioningSummary);
    renderModifiers(data.conditioningModifiers);
    renderMtmChart(data.mtmTimeline || []);
    renderSizing(data.sizing);
    renderGreeksAttribution(data.greeksAttribution);
    renderExitPanel(data.exitRulesOptimization, req);
    renderSlider(data.exitRulesOptimization, req);
    renderNotes(data.conditioningNotes || []);
    renderAnaloguesTable(data.matchedAnalogues || []);
    setActionStatus("", "");
  }

  /* ── Phase 3: Exit-rule sensitivity slider ───────────────────── */

  function renderSlider(opt, req) {
    var label = $("sliderDividerLabel");
    var panel = $("sliderPanel");
    if (!label || !panel) return;
    exitGrid = (opt && opt.grid) || [];
    if (!exitGrid.length) {
      label.style.display = "none"; panel.style.display = "none";
      return;
    }
    label.style.display = ""; panel.style.display = "";

    var pts = Array.from(new Set(exitGrid.map(function (c) { return c.profitTarget; }))).sort(function (a, b) { return a - b; });
    var sls = Array.from(new Set(exitGrid.map(function (c) { return c.stopLoss; }))).sort(function (a, b) { return a - b; });
    var ptInput = $("slidePt"), slInput = $("slideSl");
    if (!ptInput || !slInput) return;
    ptInput.min = pts[0]; ptInput.max = pts[pts.length - 1];
    slInput.min = sls[0]; slInput.max = sls[sls.length - 1];
    ptInput.value = (req && req.profitTargetPct) || 50;
    slInput.value = (req && req.stopLossPct) || 200;
    updateSlider();
    ptInput.oninput = updateSlider;
    slInput.oninput = updateSlider;
  }

  function nearestCell(pt, sl) {
    if (!exitGrid.length) return null;
    var best = null, bestD = Infinity;
    exitGrid.forEach(function (c) {
      var d = Math.pow(c.profitTarget - pt, 2) + Math.pow(c.stopLoss - sl, 2) / 25;
      if (d < bestD) { bestD = d; best = c; }
    });
    return best;
  }

  function updateSlider() {
    var pt = Number($("slidePt").value), sl = Number($("slideSl").value);
    $("slidePtLabel").textContent = pt + "%";
    $("slideSlLabel").textContent = sl + "%";
    var cell = nearestCell(pt, sl);
    if (!cell) return;
    $("slideWr").textContent = fmtNum(cell.winRatePct, 1) + "%";
    $("slideAvg").textContent = fmtPct(cell.avgPnlPct, 1);
    $("slideWr").className = "e14ExitValue " + (cell.winRatePct >= 65 ? "green" : cell.winRatePct >= 50 ? "blue" : "amber");
    $("slideAvg").className = "e14ExitValue " + pnlColor(cell.avgPnlPct);
    $("slideDelta").textContent =
      "Nearest grid cell: pt=" + cell.profitTarget + "%, sl=" + cell.stopLoss +
      "% · " + (cell.winRatePct).toFixed(1) + "% wins · avg " + fmtPct(cell.avgPnlPct);
  }

  /* ── Phase 3: Save to journal ─────────────────────────────────── */

  function setActionStatus(msg, kind) {
    var el = $("actionStatus");
    if (!el) return;
    el.textContent = msg || "";
    el.className = "e14ExitDelta" + (kind ? " " + kind : "");
  }

  async function saveToJournal() {
    if (!lastPayload || !lastRequestBody) {
      setActionStatus("Run a scenario first.", "red"); return;
    }
    setActionStatus("Saving trade…");
    try {
      var journalBody = { scenario: lastPayload, request: lastRequestBody };
      if (lastReconcile && lastReconcile.overall) {
        journalBody.reconcile = lastReconcile;
      }
      var resp = await fetch("/api/ic-scenario/journal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(journalBody),
      });
      var data = await resp.json();
      if (!resp.ok) {
        setActionStatus("Failed: " + (data.detail || resp.status), "red");
        return;
      }
      var id = data.tradeId;
      setActionStatus("Saved as " + id + " — view at " + (data.viewUrl || "/spx"), "green");
    } catch (e) {
      setActionStatus("Network error: " + e.message, "red");
    }
  }

  /* ── Phase 3: Chat summary ───────────────────────────────────── */

  function copyChatSummary() {
    if (!lastPayload || !lastRequestBody) {
      setActionStatus("Run a scenario first.", "red"); return;
    }
    var d = lastPayload, r = lastRequestBody;
    var dist = d.outcomeDistribution || {};
    var adj = d.adjustedOutcomeDistribution || {};
    var mods = d.conditioningModifiers || {};
    var lines = [
      "Please review this iron condor scenario:",
      "- Underlying: SPX",
      "- Entry " + r.entryDate + " → Expiry " + r.expiry,
      "- Strikes: LP " + r.longPut + " / SP " + r.shortPut + " / SC " + r.shortCall + " / LC " + r.longCall,
      "- Credit " + r.creditReceived + " · Profit target " + r.profitTargetPct + "% · Stop " + r.stopLossPct + "%",
      "",
      "Simulator (" + (d.analoguesUsed || 0) + " analogues):",
      "- fullCollect " + fmtNum((dist.fullCollect || {}).pct) + "% · earlyTarget " + fmtNum((dist.earlyTarget || {}).pct) + "%",
      "- whiteKnuckle " + fmtNum((dist.whiteKnuckle || {}).pct) + "% · stopOut " + fmtNum((dist.stopOut || {}).pct) + "% · breach " + fmtNum((dist.breach || {}).pct) + "%",
      "- Mean P&L " + fmtPct((d.expectedValue || {}).meanPnlPct) + " · Median " + fmtPct((d.expectedValue || {}).medianPnlPct),
    ];
    if (Object.keys(adj).length) {
      lines.push(
        "",
        "Adjusted (after Phase 2 conditioning):",
        "- fullCollect " + fmtNum((adj.fullCollect || {}).pct) + "% · earlyTarget " + fmtNum((adj.earlyTarget || {}).pct) + "% · breach " + fmtNum((adj.breach || {}).pct) + "%"
      );
    }
    if (Object.keys(mods).length) {
      lines.push("", "Modifiers:");
      ["calendar", "dealerGamma", "creditStress", "gapRegime"].forEach(function (k) {
        if (mods[k] && mods[k].note) lines.push("- " + k + ": " + mods[k].note);
      });
    }
    lines.push("", "Thoughts?");
    var text = lines.join("\n");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        setActionStatus("Chat summary copied to clipboard. Paste it into NRGX Chat.", "green");
      }, function () {
        setActionStatus("Clipboard write failed — falling back to alert.", "amber");
        window.prompt("Copy this summary:", text);
      });
    } else {
      window.prompt("Copy this summary:", text);
    }
  }

  /* ── Phase 3: Post-trade review ──────────────────────────────── */

  async function loadReview() {
    var tid = ($("reviewTradeId").value || "").trim();
    var wrap = $("reviewPanel");
    if (!tid) { wrap.innerHTML = '<div class="muted">Enter a trade ID.</div>'; return; }
    wrap.innerHTML = '<div class="muted">Loading…</div>';
    try {
      var resp = await fetch("/api/ic-scenario/review?tradeId=" + encodeURIComponent(tid));
      var data = await resp.json();
      if (!resp.ok) {
        wrap.innerHTML = '<div class="red">' + (data.detail || resp.status) + '</div>';
        return;
      }
      var pred = data.predicted || {};
      var actual = data.actual || {};
      var status = actual.status || "active";
      var pnl = actual.pnlPct;
      var rows = [
        ['Status', status],
        ['Closed At', actual.closedAt || '—'],
        ['Close Reason', actual.closeReason || '—'],
        ['Actual P&L', pnl === undefined || pnl === null ? '—' : fmtPct(pnl, 1)],
        ['Actual Days Held', actual.daysHeld !== undefined ? actual.daysHeld : '—'],
        ['Predicted Mean P&L', fmtPct(pred.meanPnlPct, 1)],
        ['Predicted Median P&L', fmtPct(pred.medianPnlPct, 1)],
        ['Predicted FullCollect %', fmtNum(pred.fullCollectPct) + '%'],
        ['Predicted Breach %', fmtNum(pred.breachPct) + '%'],
      ];
      var html = '<table class="e14Table"><tbody>';
      rows.forEach(function (r) {
        html += '<tr><th>' + r[0] + '</th><td>' + r[1] + '</td></tr>';
      });
      html += '</tbody></table>';
      if (data.verdict) {
        html += '<div class="e14Banner blue">' + data.verdict + '</div>';
      }
      wrap.innerHTML = html;
    } catch (e) {
      wrap.innerHTML = '<div class="red">Network error: ' + e.message + '</div>';
    }
  }

  /* ── LLM "What is this card?" desk tooltips ──────────────────── */

  // Pull the slice of lastPayload that corresponds to a given divider's
  // data-explain slug. Kept small deliberately: the LLM only needs the
  // numbers visible on the card, not the full replay payload.
  function extractCardData(slug, payload) {
    if (!payload) return {};
    switch (slug) {
      case "entry_state": {
        var es2   = payload.entryState || {};
        var req2  = lastRequestBody || {};
        var spot2 = Number(es2.userSpot);
        var em2   = Number(es2.userEmPct);
        var putW  = shortWingStats(spot2, Number(req2.shortPut),  em2, "put");
        var callW = shortWingStats(spot2, Number(req2.shortCall), em2, "call");
        return {
          entryState:          es2,
          analoguesUsed:       payload.analoguesUsed,
          analoguesConsidered: payload.analoguesConsidered,
          expectedValue:       payload.expectedValue || null,
          shortWings: {
            put:  putW  ? { strike: putW.strike,  distPct: putW.distPct,  emMult: putW.emMult  } : null,
            call: callW ? { strike: callW.strike, distPct: callW.distPct, emMult: callW.emMult } : null,
          },
        };
      }
      case "regime_match":
        return payload.regimeMatchQuality || {};
      case "outcome_distribution":
        return {
          distribution: payload.outcomeDistribution || {},
          ci:           payload.outcomeDistributionCI || null,
          fillModel:    payload.fillModel || null,
        };
      case "outcome_mid":
        return { distribution: payload.outcomeDistributionMid || {} };
      case "outcome_adjusted":
        return { distribution: payload.adjustedOutcomeDistribution || {} };
      case "modifiers":
        return payload.conditioningModifiers || {};
      case "mtm_timeline": {
        // Keep the payload compact: send first / mid / last percentile rows only.
        var tl = payload.mtmTimeline || [];
        if (tl.length <= 5) return { timeline: tl };
        var mid = Math.floor(tl.length / 2);
        return { timeline: [tl[0], tl[mid], tl[tl.length - 1]], nSteps: tl.length };
      }
      case "position_sizing":
        return payload.sizing || {};
      case "greeks_attribution":
        return payload.greeksAttribution || {};
      case "exit_optimization": {
        var opt = payload.exitRulesOptimization || {};
        return {
          recommendedProfitTarget: opt.recommendedProfitTarget,
          recommendedStopLoss:     opt.recommendedStopLoss,
          deltaFromDefault:        opt.deltaFromDefault,
          gridSize:                (opt.grid || []).length,
        };
      }
      case "exit_sensitivity": {
        var grid = ((payload.exitRulesOptimization || {}).grid) || [];
        var sample = grid.slice(0, 6);
        var cur = null;
        if (lastRequestBody) cur = nearestCell(Number(lastRequestBody.profitTargetPct), Number(lastRequestBody.stopLossPct));
        return { gridSize: grid.length, gridSample: sample, currentRuleCell: cur };
      }
      case "conditioning_notes":
        return { notes: payload.conditioningNotes || [] };
      case "matched_analogues": {
        var rows = payload.matchedAnalogues || [];
        return {
          total: rows.length,
          sample: rows.slice(0, 12).map(function (r) {
            return {
              entryDate: r.entryDate, expiryDate: r.expiryDate,
              outcome: r.outcome, exitDay: r.exitDay,
              pnlPct: r.pnlPct, mae: r.mae, breached: r.breached,
            };
          }),
        };
      }
      case "actions":
        return {};
      case "post_trade_review":
        return { note: "Load a trade ID above to populate this panel." };
      default:
        return {};
    }
  }

  function buildScenarioContext() {
    if (!lastRequestBody && !lastPayload) {
      return { note: "No scenario has been run yet — explain generically." };
    }
    var ctx = { request: lastRequestBody || null };
    if (lastPayload) {
      ctx.analoguesUsed = lastPayload.analoguesUsed || 0;
      var es = lastPayload.entryState || {};
      ctx.regimeBucket = es.regimeBucket || null;
      ctx.userEmPct    = es.userEmPct;
      ctx.userSpot     = es.userSpot;
      ctx.fillModelMode = (lastPayload.fillModel || {}).mode || null;
    }
    return ctx;
  }

  /* ── Desk Insight wire-up (shared; popup + fetcher live in desk-insight.js) ── */
  function wireDeskInsight() {
    if (!window.DeskInsight) return;
    window.DeskInsight.bind({
      engineId:           "e14",
      dividerSelector:    ".deskDivider[data-insight]",
      slugTitles: {
        entry_state:          "Entry State",
        regime_match:         "Regime Match Quality",
        outcome_distribution: "Outcome Distribution (NBBO)",
        outcome_mid:          "Legacy Mid-Fill Distribution",
        outcome_adjusted:     "Adjusted Distribution",
        modifiers:            "Conditioning Modifiers",
        mtm_timeline:         "MTM Timeline",
        position_sizing:      "Position Sizing",
        greeks_attribution:   "P&L Attribution (Greeks)",
        exit_optimization:    "Exit-Rule Optimization",
        exit_sensitivity:     "Exit-Rule Sensitivity",
        conditioning_notes:   "Conditioning Notes",
        matched_analogues:    "Matched Analogues",
        actions:              "Actions",
        post_trade_review:    "Post-Trade Review",
      },
      getCardData:        function (slug) { return extractCardData(slug, lastPayload); },
      getScenarioContext: buildScenarioContext,
    });
    // A new scenario run replaces every card's data — burn the cache.
    var results = $("results");
    if (results && typeof MutationObserver === "function") {
      var mo = new MutationObserver(function () {
        if (window.DeskInsight) {
          window.DeskInsight.clearCache();
          window.DeskInsight.refresh();
        }
      });
      mo.observe(results, { childList: true, subtree: false });
    }
  }

  /* ── Boot ─────────────────────────────────────────────────────── */

  function boot() {
    initDefaults();
    var form = $("icForm");
    if (form) form.addEventListener("submit", runScenario);
    var j = $("journalBtn"); if (j) j.addEventListener("click", saveToJournal);
    var c = $("chatBtn"); if (c) c.addEventListener("click", copyChatSummary);
    var rv = $("reviewBtn"); if (rv) rv.addEventListener("click", loadReview);

    wireDeskInsight();

    // Stage 2: reconciliation drawer toggle.
    var reconToggle = $("reconcileToggle");
    if (reconToggle) {
      reconToggle.addEventListener("click", function () {
        var drawer = $("reconcileDrawer");
        if (!drawer) return;
        var open = drawer.classList.toggle("open");
        reconToggle.textContent = open ? "Hide details" : "Show details";
        reconToggle.setAttribute("aria-expanded", open ? "true" : "false");
      });
    }

    // Stage 3: debounced pre-check on strike/credit/expiry edits.
    var preCheckDebounce = null;
    function schedulePreCheck() {
      if (preCheckDebounce) clearTimeout(preCheckDebounce);
      preCheckDebounce = setTimeout(runPreCheck, 600);
    }
    ["shortPut", "longPut", "shortCall", "longCall", "creditReceived", "expiry"].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener("change", schedulePreCheck);
    });

    // Health probe — if backend disabled or cache empty, surface a banner early.
    fetch("/api/ic-scenario/health")
      .then(function (r) { return r.json(); })
      .then(function (h) {
        if (!h || !h.enabled) {
          showBanner("Engine 14 is disabled. Set ENABLE_ENGINE14_IC_SCENARIO=1 to enable.", "amber");
          return;
        }
        if (h.chainCache && h.chainCache.daysCovered < 30) {
          showBanner(
            "Chain cache is sparse (" + (h.chainCache.daysCovered || 0) + " days cached). " +
            "Run scripts/engine14_backfill_chains.py on the droplet to populate 2 years of SPX weeklies.",
            "amber"
          );
        }
      })
      .catch(function () { /* ignore — endpoint gated in prod */ });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
