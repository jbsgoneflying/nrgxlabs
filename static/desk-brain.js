/* Desk Brain — command deck.
   Fetches /api/desk-brain/book and renders the regime banner, sleeve
   allocation, target book, desk-head LLM read, conflicts, edge table and
   opportunity set. */
(function () {
  "use strict";

  var SLEEVE_TAG = { volatility: "dbTag--vol", directional: "dbTag--dir", overlay: "dbTag--ovl" };
  var SLEEVE_SHORT = { volatility: "Vol/Income", directional: "Directional", overlay: "Overlay/Reserve" };

  function $(id) { return document.getElementById(id); }
  function pct(v) { return (v == null ? "—" : (Math.round(v * 100) / 100).toFixed(2) + "%"); }
  function money(v) { return (v == null ? "—" : "$" + Math.round(v).toLocaleString()); }
  function num(v, d) { d = (d == null ? 2 : d); return (v == null ? "—" : (Math.round(v * Math.pow(10, d)) / Math.pow(10, d)).toFixed(d)); }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]; }); }

  function regimeClass(label) {
    var k = String(label || "").toLowerCase();
    if (k === "risk-on") return "dbRegime--riskon";
    if (k === "risk-off") return "dbRegime--riskoff";
    if (k === "stressed") return "dbRegime--stressed";
    return "dbRegime--transitional";
  }

  function sleeveTag(sleeve) {
    var cls = SLEEVE_TAG[sleeve] || "dbTag--ovl";
    return '<span class="dbTag ' + cls + '">' + esc(SLEEVE_SHORT[sleeve] || sleeve) + "</span>";
  }

  function setStatus(msg, isError) {
    var el = $("dbStatus");
    el.textContent = msg || "";
    el.style.color = isError ? "#b02018" : "rgba(11,11,15,0.5)";
  }

  function renderBanner(book) {
    var r = $("dbRegime");
    r.innerHTML = '<span class="dbRegimePill ' + regimeClass(book.regimeLabel) + '">' + esc(book.regimeLabel) + "</span>";
    $("dbDeployed").textContent = pct(book.totalDeployedPct);
    $("dbReserve").textContent = pct(book.reservePct);
    $("dbPosCount").textContent = (book.positions || []).length;
    var budget = book.totalHeatBudgetPct || 6;
    var fillPct = Math.max(0, Math.min(100, (book.totalDeployedPct / budget) * 100));
    $("dbHeatFill").style.width = fillPct.toFixed(0) + "%";
    $("dbHeatCaption").textContent =
      pct(book.totalDeployedPct) + " of " + pct(budget) + " heat budget deployed · " + pct(book.reservePct) + " reserve";
  }

  function renderSleeves(book) {
    var wrap = $("dbSleeves");
    wrap.innerHTML = "";
    (book.sleeves || []).forEach(function (s) {
      var budget = book.totalHeatBudgetPct || 6;
      var fill = Math.max(0, Math.min(100, (s.deployedPct / (s.heatBudgetPct || budget)) * 100));
      var tiltTxt = (s.tiltedWeight !== s.baseWeight)
        ? ' · tilt ' + (s.tiltedWeight >= s.baseWeight ? "+" : "") + Math.round((s.tiltedWeight / (s.baseWeight || 1) - 1) * 100) + "%"
        : "";
      var card = document.createElement("div");
      card.className = "dbCard";
      card.innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:baseline">' +
          '<span class="dbSleeveName">' + esc(s.name) + "</span>" +
          '<span class="dbSleeveKind">' + esc(s.deployable ? "tradable" : "reserve") + "</span>" +
        "</div>" +
        '<div class="dbSleeveBar"><div class="dbSleeveBarFill" style="width:' + fill.toFixed(0) + '%"></div></div>' +
        '<div class="dbSleeveStat"><span>Budget ' + pct(s.heatBudgetPct) + tiltTxt + "</span>" +
          "<span>" + s.positionCount + " pos · " + pct(s.deployedPct) + " used</span></div>";
      wrap.appendChild(card);
    });
  }

  function renderBook(book) {
    var body = $("dbBookBody");
    var empty = $("dbBookEmpty");
    body.innerHTML = "";
    var positions = book.positions || [];
    $("dbBookSub").textContent = positions.length + " position(s) · " + pct(book.totalDeployedPct) + " deployed";
    if (!positions.length) {
      empty.style.display = "block";
      empty.textContent = "Book is flat — no actionable opportunities cleared the gate. Full reserve held.";
      return;
    }
    empty.style.display = "none";
    positions.forEach(function (p) {
      var tr = document.createElement("tr");
      var state = p.deskStatus ? esc(p.deskStatus) : '<span class="dbStatus">candidate</span>';
      tr.innerHTML =
        "<td>" + p.rank + "</td>" +
        "<td><b>" + esc(p.ticker) + "</b></td>" +
        "<td>" + esc(p.engineName) + "</td>" +
        "<td>" + sleeveTag(p.sleeve) + "</td>" +
        "<td>" + esc(p.direction) + "</td>" +
        '<td class="dbNum">' + num(p.conviction, 0) + "</td>" +
        '<td class="dbNum">' + num(p.edgeScore, 2) + "</td>" +
        '<td class="dbNum"><b>' + pct(p.riskPct) + "</b></td>" +
        '<td class="dbNum">' + money(p.riskDollars) + "</td>" +
        "<td>" + state + (p.haircut > 0 ? ' <span class="dbStatus">(haircut)</span>' : "") + "</td>";
      body.appendChild(tr);
    });
  }

  function renderLlm(llm) {
    var el = $("dbLlm");
    var badge = $("dbLlmSource");
    if (!llm || Object.keys(llm).length === 0) {
      el.innerHTML = '<span class="dbMuted">No desk-head synthesis available.</span>';
      badge.textContent = "";
      return;
    }
    badge.textContent = llm._source === "llm" ? "LLM" : "fallback";
    var rows = [
      ["Lean today", llm.lean_today],
      ["Conflicts", llm.conflicts],
      ["What would change my mind", llm.what_would_change_my_mind],
      ["Desk takeaway", llm.desk_takeaway],
    ];
    var html = rows.map(function (r) {
      if (!r[1]) return "";
      return '<div class="dbLlmRow"><b>' + esc(r[0]) + "</b><span>" + esc(r[1]) + "</span></div>";
    }).join("");
    if (llm.sleeve_tilt) {
      var t = llm.sleeve_tilt;
      html += '<div class="dbLlmRow"><b>Sleeve tilt (clamped ±20%)</b><span>' +
        ["volatility", "directional", "overlay"].map(function (s) {
          return SLEEVE_SHORT[s] + " ×" + num(t[s], 2);
        }).join(" · ") + "</span></div>";
    }
    el.innerHTML = html || '<span class="dbMuted">No desk-head synthesis available.</span>';
  }

  function renderConflicts(book) {
    var el = $("dbConflicts");
    var items = (book.conflicts || []).concat(book.notes || []);
    if (!items.length) {
      el.innerHTML = '<span class="dbMuted">No correlation clusters or conflicts flagged. Book is clean.</span>';
      return;
    }
    el.innerHTML = items.map(function (c) {
      var isConflict = (book.conflicts || []).indexOf(c) !== -1;
      return '<div class="' + (isConflict ? "dbConflict" : "dbMuted") + '">• ' + esc(c) + "</div>";
    }).join("");
  }

  function renderEdges(edges) {
    var body = $("dbEdgeBody");
    body.innerHTML = "";
    (edges || []).sort(function (a, b) { return b.edgeScore - a.edgeScore; }).forEach(function (e) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + esc(e.engineName) + "</td>" +
        "<td>" + sleeveTag(e.sleeve) + "</td>" +
        '<td class="dbNum">' + num(e.expectancyR, 2) + "</td>" +
        '<td class="dbNum">' + num(e.winRate * 100, 0) + "</td>" +
        '<td class="dbNum">' + num(e.sharpe, 2) + "</td>" +
        '<td class="dbNum"><b>' + num(e.edgeScore, 2) + "</b></td>" +
        '<td><span class="dbStatus">' + esc(e.source) + "</span></td>";
      body.appendChild(tr);
    });
  }

  function renderOpps(payload) {
    var body = $("dbOppBody");
    var empty = $("dbOppEmpty");
    body.innerHTML = "";
    var opps = payload.opportunities || [];
    var sum = payload.opportunitySummary || {};
    $("dbOppSub").textContent = (sum.total || 0) + " surfaced · " + (sum.actionable || 0) + " actionable";
    if (!opps.length) {
      empty.style.display = "block";
      empty.textContent = "No opportunities surfaced from engine trackers. Add signals in E4/E5 or wait for the next regime read.";
      return;
    }
    empty.style.display = "none";
    opps.forEach(function (o) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td><b>" + esc(o.ticker) + "</b></td>" +
        "<td>" + esc(o.engineName) + "</td>" +
        "<td>" + sleeveTag(o.sleeve) + "</td>" +
        "<td>" + esc(o.direction) + "</td>" +
        '<td class="dbNum">' + num(o.conviction, 0) + "</td>" +
        "<td>" + esc(o.verdict) + "</td>" +
        "<td>" + (o.deskStatus ? esc(o.deskStatus) : '<span class="dbStatus">—</span>') + "</td>" +
        '<td><span class="dbStatus">' + esc(o.source) + "</span></td>";
      body.appendChild(tr);
    });
  }

  function renderPaper(perf) {
    var el = $("dbPaper");
    if (!perf || !perf.byEngine || !perf.byEngine.length) {
      el.innerHTML = '<span class="dbMuted">' + esc((perf && perf.note) || "No closed paper trades yet — record the book and let positions resolve.") + "</span>";
      return;
    }
    var edge = perf.edge || 0;
    var edgeColor = edge >= 0 ? "#1f7a3d" : "#b02018";
    var html =
      '<div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:12px">' +
        '<div class="dbBannerItem"><span class="dbBannerLabel">Blended (edge-weighted)</span><span class="dbBannerValue">' + num(perf.blendedExpectancy, 2) + "</span></div>" +
        '<div class="dbBannerItem"><span class="dbBannerLabel">Equal-weight baseline</span><span class="dbBannerValue">' + num(perf.baselineExpectancy, 2) + "</span></div>" +
        '<div class="dbBannerItem"><span class="dbBannerLabel">Allocator edge</span><span class="dbBannerValue" style="color:' + edgeColor + '">' + (edge >= 0 ? "+" : "") + num(edge, 2) + "</span></div>" +
        '<div class="dbBannerItem"><span class="dbBannerLabel">Closed trades</span><span class="dbBannerValue">' + (perf.totalClosed || 0) + "</span></div>" +
      "</div>";
    html += '<table class="dbTable"><thead><tr><th>Engine</th><th class="dbNum">Closed</th><th class="dbNum">Win %</th><th class="dbNum">Avg P&amp;L</th><th class="dbNum">Total P&amp;L</th><th class="dbNum">Edge wt</th></tr></thead><tbody>';
    perf.byEngine.forEach(function (r) {
      html +=
        "<tr><td>" + esc(r.engineName) + "</td>" +
        '<td class="dbNum">' + r.closedTrades + "</td>" +
        '<td class="dbNum">' + num(r.winRate, 0) + "</td>" +
        '<td class="dbNum">' + num(r.avgPnl, 2) + "</td>" +
        '<td class="dbNum">' + num(r.totalPnl, 2) + "</td>" +
        '<td class="dbNum">' + num(r.edgeScore, 2) + "</td></tr>";
    });
    html += "</tbody></table>";
    el.innerHTML = html;
  }

  function loadPaper() {
    fetch("/api/desk-brain/paper/performance")
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (perf) { if (perf) renderPaper(perf); })
      .catch(function () {});
  }

  function recordPaper() {
    var btn = $("dbPaperRecord");
    btn.disabled = true;
    fetch("/api/desk-brain/paper/record", { method: "POST" })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (r) {
        if (r) setStatus("Logged " + r.logged + " position(s) to paper (" + r.skipped + " already open).");
        loadPaper();
      })
      .catch(function () { setStatus("Failed to log paper book.", true); })
      .then(function () { btn.disabled = false; });
  }

  function render(payload) {
    var book = payload.book || {};
    renderBanner(book);
    renderSleeves(book);
    renderBook(book);
    renderLlm(payload.llm);
    renderConflicts(book);
    renderEdges(payload.edges);
    renderOpps(payload);
    loadPaper();
    var when = payload.asOf ? new Date(payload.asOf).toLocaleString() : "";
    setStatus((payload.cached ? "Cached book" : "Fresh book") + (when ? " · built " + when : ""));
  }

  function load(forceRefresh) {
    var btn = $("dbRefresh");
    btn.disabled = true;
    setStatus(forceRefresh ? "Rebuilding book…" : "Loading book…");
    var url = forceRefresh ? "/api/desk-brain/refresh" : "/api/desk-brain/book";
    var opts = forceRefresh ? { method: "POST" } : { method: "GET" };
    fetch(url, opts)
      .then(function (res) {
        if (res.status === 404) throw new Error("Desk Brain is disabled on this deployment.");
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (payload) { render(payload); })
      .catch(function (err) { setStatus(err.message || "Failed to load Desk Brain book.", true); })
      .then(function () { btn.disabled = false; });
  }

  function init() {
    $("dbRefresh").addEventListener("click", function () { load(true); });
    $("dbPaperRecord").addEventListener("click", recordPaper);
    load(false);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
