/* ── Engine 9: Credit Stress Drift — Frontend Logic ────────────────── */
(function () {
  "use strict";

  var _scanData = null;
  var _spreadChart = null;
  var _refreshTimer = null;
  var _abortCtrl = null;

  /* ── helpers ── */
  function $(id) { return document.getElementById(id); }
  function fmt(v, d) { return v != null ? Number(v).toFixed(d == null ? 1 : d) : "—"; }
  function fmtDollar(v) {
    if (v == null) return "—";
    var abs = Math.abs(v);
    if (abs >= 1e9) return "$" + (v / 1e9).toFixed(1) + "B";
    if (abs >= 1e6) return "$" + (v / 1e6).toFixed(1) + "M";
    if (abs >= 1e3) return "$" + (v / 1e3).toFixed(0) + "K";
    return "$" + v.toFixed(0);
  }
  function scoreClass(s) { return s >= 60 ? "high" : s >= 30 ? "med" : "low"; }
  function phaseColor(p) { return p >= 4 ? "var(--e9-red)" : p >= 3 ? "var(--e9-orange)" : p >= 2 ? "var(--e9-amber)" : "var(--e9-green)"; }

  /* ── Scan ── */
  function runScan() {
    var btn = $("e9ScanBtn");
    var loading = $("e9Loading");
    btn.disabled = true;
    btn.textContent = "Scanning...";
    loading.style.display = "block";

    if (_abortCtrl) _abortCtrl.abort();
    _abortCtrl = new AbortController();

    fetch("/api/engine9/scan", { signal: _abortCtrl.signal })
      .then(function (r) {
        if (!r.ok) throw new Error("Scan failed: " + r.status);
        return r.json();
      })
      .then(function (data) {
        _scanData = data;
        renderAll(data);
        fetchSpreads();
        $("e9DeskNotesBtn").disabled = false;
        $("e9Updated").textContent = "Last updated: " + new Date().toLocaleTimeString();
      })
      .catch(function (err) {
        if (err.name === "AbortError") return;
        console.error("Engine 9 scan error:", err);
        loading.innerHTML = '<div style="color:var(--e9-red)">Scan failed: ' + err.message + '</div>';
      })
      .finally(function () {
        btn.disabled = false;
        btn.textContent = "Run Full Scan";
      });
  }

  /* ── Render All ── */
  function renderAll(data) {
    $("e9Loading").style.display = "none";
    renderPhase(data);
    renderSignals(data.signals || []);
    renderForcedSellerMap(data.forced_seller_map || []);
    renderWatchlist(data.watchlist || {});
  }

  /* ── Section A: Phase + Triggers + Thesis ── */
  function renderPhase(data) {
    var comp = data.composite || {};
    var phase = comp.phase || 1;
    var badge = $("e9PhaseBadge");
    badge.setAttribute("data-phase", phase);
    badge.style.display = "";
    $("e9PhaseNum").textContent = phase;
    $("e9PhaseLabel").textContent = comp.phase_label || "";
    $("e9Composite").textContent = "Composite: " + fmt(comp.composite, 1);
    $("e9PhaseRow").style.display = "";

    var action = $("e9ActionBanner");
    if (comp.phase_action) {
      action.textContent = "Phase " + phase + " Action: " + comp.phase_action;
      action.style.display = "";
      action.style.borderColor = phaseColor(phase);
    }

    /* Triggers */
    var triggers = data.triggers || [];
    var tList = $("e9TriggersList");
    tList.innerHTML = "";
    triggers.forEach(function (t) {
      var row = document.createElement("div");
      row.className = "e9TriggerRow";
      var badgeCls = "e9TriggerBadge" + (t.active ? " active" : "");
      row.innerHTML =
        '<div class="' + badgeCls + '" data-level="' + t.level + '">' + t.level + '</div>' +
        '<div class="e9TriggerInfo">' +
          '<div class="e9TriggerName">' + t.name + (t.active ? " — ACTIVE" : "") + '</div>' +
          '<div class="e9TriggerCondition">' + t.condition + '</div>' +
        '</div>';
      tList.appendChild(row);
    });

    /* Thesis Health */
    var thesis = data.thesis_health || [];
    var tHealth = $("e9ThesisHealth");
    tHealth.innerHTML = "";
    thesis.forEach(function (ind) {
      var item = document.createElement("div");
      item.className = "e9ThesisItem";
      item.innerHTML =
        '<div class="e9ThesisDot ' + (ind.healthy ? "healthy" : "warning") + '"></div>' +
        '<span>' + ind.name + ': ' + (ind.detail || "") + '</span>';
      tHealth.appendChild(item);
    });

    /* Time Compression Banner */
    var tc = (data.signals || []).find(function (s) { return s.key === "time_compression"; });
    var banner = $("e9CompressionBanner");
    if (tc && tc.triggered) {
      banner.classList.add("visible");
    } else {
      banner.classList.remove("visible");
    }
  }

  /* ── Section B: Signal Grid ── */
  function renderSignals(signals) {
    $("e9SigTitle").style.display = "";
    var grid = $("e9SignalGrid");
    grid.innerHTML = "";
    signals.forEach(function (sig) {
      var sc = scoreClass(sig.score);
      var weightLabel = sig.weight > 0 ? (sig.weight * 100).toFixed(0) + "%" : (sig.key === "time_compression" ? "META" : "OVERLAY");
      var card = document.createElement("div");
      card.className = "e9SigCard" + (sig.triggered ? " triggered" : "");
      card.innerHTML =
        '<div class="e9SigHeader">' +
          '<span class="e9SigName">' + sig.label + '</span>' +
          '<span class="e9SigWeight">' + weightLabel + '</span>' +
        '</div>' +
        '<div class="e9SigScore ' + sc + '">' + fmt(sig.score, 0) + '</div>' +
        '<div class="e9SigBar"><div class="e9SigBarFill ' + sc + '" style="width:' + Math.min(sig.score, 100) + '%"></div></div>' +
        '<div class="e9SigDetail">' + (sig.detail || "") + '</div>';
      grid.appendChild(card);
    });
  }

  /* ── Section C: Forced Seller Map ── */
  function renderForcedSellerMap(entries) {
    $("e9ForcedTitle").style.display = "";
    var wrap = $("e9ForcedWrap");
    if (!entries.length) {
      wrap.innerHTML = '<div style="padding:12px;color:var(--e9-muted);font-size:13px;">No forced seller data available</div>';
      return;
    }
    var html = '<table class="e9Table"><thead><tr>' +
      '<th>Ticker</th><th>Fragility</th><th>Leverage</th><th>Liq. Mismatch</th>' +
      '<th>Retail Exp.</th><th>Put Skew 25d</th><th>Price 20d %</th><th>Insider 30d</th>' +
      '</tr></thead><tbody>';
    entries.forEach(function (e) {
      var fc = e.fragility_score >= 60 ? "e9FragHigh" : e.fragility_score >= 30 ? "e9FragMed" : "e9FragLow";
      var priceCls = (e.price_20d_pct != null && e.price_20d_pct < 0) ? "e9Negative" : "";
      html +=
        '<tr>' +
        '<td class="e9Ticker">' + e.ticker + '</td>' +
        '<td class="' + fc + '">' + fmt(e.fragility_score, 0) + '</td>' +
        '<td>' + fmt(e.leverage, 2) + '</td>' +
        '<td>' + fmt(e.liquidity_mismatch, 2) + '</td>' +
        '<td>' + fmt(e.retail_exposure, 0) + '%</td>' +
        '<td>' + fmt(e.put_skew_25d, 3) + '</td>' +
        '<td class="' + priceCls + '">' + fmt(e.price_20d_pct, 2) + '%</td>' +
        '<td>' + fmtDollar(e.insider_net_30d) + '</td>' +
        '</tr>';
    });
    html += '</tbody></table>';
    wrap.innerHTML = html;
  }

  /* ── Section D: Tiered Watchlist ── */
  function renderWatchlist(watchlist) {
    $("e9WatchTitle").style.display = "";
    var wrap = $("e9WatchWrap");
    wrap.innerHTML = "";

    var tierOrder = ["tier1", "tier2", "tier3", "tier4"];
    var tierLabels = {
      tier1: "Tier 1: BDCs (Direct Stress)",
      tier2: "Tier 2: Alt Managers (Sentiment + AUM)",
      tier3: "Tier 3: Credit ETFs (Confirmation)",
      tier4: "Tier 4: Vol / Tail Hedges",
    };

    tierOrder.forEach(function (tierKey) {
      var tickers = watchlist[tierKey] || [];
      if (!tickers.length) return;

      var group = document.createElement("div");
      group.className = "e9TierGroup";

      var header = document.createElement("div");
      header.className = "e9TierHeader";
      header.innerHTML =
        '<span class="e9TierChevron">&#9654;</span>' +
        '<span class="e9TierBadge ' + tierKey + '">' + tierKey.toUpperCase().replace("TIER", "T") + '</span>' +
        '<span>' + (tierLabels[tierKey] || tierKey) + ' (' + tickers.length + ')</span>';

      var body = document.createElement("div");
      body.className = "e9TierBody";

      var table = '<table class="e9Table"><thead><tr>' +
        '<th>Ticker</th><th>Price</th><th>5d %</th><th>20d %</th><th>IV Rank</th>' +
        '<th>Put Skew</th><th>Insider 30d</th><th>Score</th><th>Conviction</th>' +
        '</tr></thead><tbody>';

      tickers.forEach(function (t) {
        var chg5Cls = (t.change_5d_pct != null && t.change_5d_pct < 0) ? "e9Negative" : (t.change_5d_pct > 0 ? "e9Positive" : "");
        var chg20Cls = (t.change_20d_pct != null && t.change_20d_pct < 0) ? "e9Negative" : (t.change_20d_pct > 0 ? "e9Positive" : "");
        table +=
          '<tr>' +
          '<td class="e9Ticker">' + t.ticker + '</td>' +
          '<td>' + fmt(t.price, 2) + '</td>' +
          '<td class="' + chg5Cls + '">' + fmt(t.change_5d_pct, 2) + '%</td>' +
          '<td class="' + chg20Cls + '">' + fmt(t.change_20d_pct, 2) + '%</td>' +
          '<td>' + fmt(t.iv_rank, 0) + '</td>' +
          '<td>' + fmt(t.put_skew_25d, 3) + '</td>' +
          '<td>' + fmtDollar(t.insider_net_30d) + '</td>' +
          '<td>' + fmt(t.signal_score, 0) + '</td>' +
          '<td><span class="e9ConvBadge ' + (t.conviction || "neutral") + '">' + (t.conviction || "—") + '</span></td>' +
          '</tr>';
      });
      table += '</tbody></table>';
      body.innerHTML = table;

      header.addEventListener("click", function () {
        header.classList.toggle("open");
        body.classList.toggle("open");
      });

      group.appendChild(header);
      group.appendChild(body);
      wrap.appendChild(group);
    });

    /* Auto-open tier1 */
    var first = wrap.querySelector(".e9TierHeader");
    if (first) { first.classList.add("open"); first.nextElementSibling.classList.add("open"); }
  }

  /* ── Section E: Spread Chart ── */
  function fetchSpreads() {
    fetch("/api/engine9/spreads")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data) renderSpreadChart(data); })
      .catch(function (err) { console.warn("Spread chart fetch error:", err); });
  }

  function renderSpreadChart(data) {
    $("e9ChartTitle").style.display = "";
    $("e9ChartWrap").style.display = "";
    var canvas = $("e9SpreadChart");

    var hy = data.hy_oas || {};
    var ig = data.ig_oas || {};
    var curve = data.curve_2s10s || {};

    var dates = hy.dates || ig.dates || [];
    var hyVals = hy.values || [];
    var igVals = ig.values || [];
    var curveVals = curve.values || [];

    if (_spreadChart) _spreadChart.destroy();

    var datasets = [];
    if (hyVals.length) {
      datasets.push({
        label: "HY OAS (bps)",
        data: hyVals,
        borderColor: "rgba(255,59,48,0.9)",
        backgroundColor: "rgba(255,59,48,0.08)",
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 2,
        yAxisID: "y",
      });
    }
    if (igVals.length) {
      datasets.push({
        label: "IG OAS (bps)",
        data: igVals,
        borderColor: "rgba(255,159,10,0.7)",
        backgroundColor: "transparent",
        fill: false,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 1.5,
        yAxisID: "y",
      });
    }
    if (curveVals.length) {
      datasets.push({
        label: "2s10s Curve (%)",
        data: curveVals,
        borderColor: "rgba(52,199,89,0.7)",
        backgroundColor: "transparent",
        fill: false,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 1.5,
        yAxisID: "y2",
        borderDash: [6, 3],
      });
    }

    _spreadChart = new Chart(canvas, {
      type: "line",
      data: { labels: dates, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "top", labels: { usePointStyle: true, font: { size: 11 } } },
          tooltip: { mode: "index", intersect: false },
        },
        scales: {
          x: {
            ticks: { maxTicksLimit: 12, font: { size: 10 } },
            grid: { display: false },
          },
          y: {
            position: "left",
            title: { display: true, text: "OAS (bps)", font: { size: 11 } },
            grid: { color: "rgba(0,0,0,0.05)" },
          },
          y2: {
            position: "right",
            title: { display: true, text: "Curve (%)", font: { size: 11 } },
            grid: { display: false },
          },
        },
      },
    });
  }

  /* ── Desk Notes ── */
  function openDeskNotes() {
    var popup = $("e9DeskPopup");
    var content = $("e9DeskContent");
    popup.classList.add("visible");
    content.innerHTML = '<div class="e9Loading"><div class="e9Spinner"></div><div>Generating desk brief...</div></div>';

    fetch("/api/engine9/desk-notes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scan_data: _scanData }),
    })
      .then(function (r) { return r.ok ? r.json() : r.json().then(function (e) { throw new Error(e.detail || "Failed"); }); })
      .then(function (data) { renderDeskNotes(data); })
      .catch(function (err) {
        content.innerHTML = '<div style="color:var(--e9-red)">Error: ' + err.message + '</div>';
      });
  }

  function renderDeskNotes(data) {
    var content = $("e9DeskContent");
    var html = "";

    if (data.phase_assessment) {
      html += "<h3>Phase Assessment</h3><p>" + data.phase_assessment + "</p>";
    }
    if (data.active_triggers_commentary) {
      html += "<h3>Active Triggers</h3><p>" + data.active_triggers_commentary + "</p>";
    }
    if (data.top_trades && data.top_trades.length) {
      html += "<h3>Top Trades</h3>";
      data.top_trades.forEach(function (t, i) {
        html += "<p><strong>" + (i + 1) + ". " + (t.instrument || t.ticker || "") + "</strong> — " + (t.action || "") + "<br>";
        if (t.sizing) html += "Size: " + t.sizing + "<br>";
        if (t.rationale) html += t.rationale;
        html += "</p>";
      });
    }
    if (data.forced_seller_spotlight) {
      html += "<h3>Forced Seller Spotlight</h3><p>" + data.forced_seller_spotlight + "</p>";
    }
    if (data.risk_flags) {
      html += "<h3>Risk Flags</h3><p>" + data.risk_flags + "</p>";
    }
    if (data.invalidation_triggers) {
      html += "<h3>Invalidation</h3><p>" + data.invalidation_triggers + "</p>";
    }
    if (data.position_sizing_guidance) {
      html += "<h3>Position Sizing</h3><p>" + data.position_sizing_guidance + "</p>";
    }
    if (data.raw_text) {
      html += "<h3>Full Brief</h3><p>" + data.raw_text + "</p>";
    }

    content.innerHTML = html || "<p>No desk notes generated.</p>";
  }

  /* ── Auto-refresh during market hours ── */
  function startAutoRefresh() {
    if (_refreshTimer) clearInterval(_refreshTimer);
    _refreshTimer = setInterval(function () {
      var now = new Date();
      var h = now.getUTCHours();
      if (h >= 13 && h < 21) {
        runScan();
      }
    }, 5 * 60 * 1000);
  }

  /* ── Init ── */
  function init() {
    $("e9ScanBtn").addEventListener("click", runScan);
    $("e9DeskNotesBtn").addEventListener("click", openDeskNotes);
    $("e9DeskPopupClose").addEventListener("click", function () {
      $("e9DeskPopup").classList.remove("visible");
    });
    startAutoRefresh();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
