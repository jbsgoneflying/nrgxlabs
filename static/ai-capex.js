/* AI Capex Reality Engine (Engine 17) — dashboard.
   Fetches /api/ai-capex and renders the verdict table (Reality Score,
   Consensus Gap, label, conviction), a label filter, expandable per-ticker
   evidence drill-down + trade ideas, and thematic baskets. */
(function () {
  "use strict";

  var LABEL_ORDER = [
    "consensus_not_updated", "real_beneficiary", "second_order_winner",
    "delayed_beneficiary", "overhyped_beneficiary", "second_order_loser", "neutral",
  ];

  var state = { payload: null, filter: "all", open: {} };

  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]; }); }
  function num(v, d) { d = (d == null ? 1 : d); return (v == null ? "—" : (Math.round(v * Math.pow(10, d)) / Math.pow(10, d)).toFixed(d)); }

  function setStatus(msg, isError) {
    var el = $("acStatus");
    el.textContent = msg || "";
    el.style.color = isError ? "#b02018" : "rgba(11,11,15,0.5)";
  }

  function labelChip(label, display) {
    return '<span class="acLabel acLabel--' + esc(label) + '">' + esc(display) + "</span>";
  }

  function dirCell(dir) {
    var cls = dir === "long" ? "acDir--long" : (dir === "short" ? "acDir--short" : "acDir--neutral");
    return '<span class="' + cls + '">' + esc(dir) + "</span>";
  }

  function gapCell(gap) {
    var cls = gap > 0 ? "acGap--pos" : (gap < 0 ? "acGap--neg" : "");
    return '<span class="' + cls + '">' + (gap > 0 ? "+" : "") + num(gap, 0) + "</span>";
  }

  function realityCell(r) {
    var pct = Math.max(0, Math.min(100, r));
    return '<div style="display:flex;align-items:center;gap:8px;justify-content:flex-end">' +
      "<span>" + num(r, 0) + "</span>" +
      '<div class="acBar"><div class="acBarFill" style="width:' + pct.toFixed(0) + '%"></div></div></div>';
  }

  function renderBanner(p) {
    var s = p.summary || {};
    $("acTotal").textContent = s.total != null ? s.total : 0;
    $("acActionable").textContent = s.actionable != null ? s.actionable : 0;
    $("acEvidence").textContent = p.evidenceTotal != null ? p.evidenceTotal : 0;
    $("acWeb").textContent = p.webEvidence != null ? p.webEvidence : 0;
  }

  function renderChips(p) {
    var wrap = $("acChips");
    var counts = (p.summary && p.summary.byLabel) || {};
    var labels = p.labels || {};
    var chips = [];
    var total = (p.verdicts || []).length;
    chips.push(chip("all", "All", total));
    LABEL_ORDER.forEach(function (lbl) {
      if (counts[lbl]) chips.push(chip(lbl, labels[lbl] || lbl, counts[lbl]));
    });
    wrap.innerHTML = chips.join("");
    Array.prototype.forEach.call(wrap.querySelectorAll(".acChip"), function (el) {
      el.addEventListener("click", function () {
        state.filter = el.getAttribute("data-k");
        renderChips(state.payload);
        renderTable(state.payload);
      });
    });
  }

  function chip(key, label, count) {
    var active = state.filter === key ? " active" : "";
    return '<span class="acChip' + active + '" data-k="' + esc(key) + '">' +
      esc(label) + ' <span class="acCount">' + count + "</span></span>";
  }

  function evidenceRow(ev) {
    var pol = ev.polarity > 0 ? "+" : (ev.polarity < 0 ? "−" : "0");
    var src = ev.sourceUrl
      ? '<a href="' + esc(ev.sourceUrl) + '" target="_blank" rel="noopener">' + esc(ev.sourceTitle || ev.sourceType) + "</a>"
      : esc(ev.sourceTitle || ev.sourceType);
    return '<div class="acEv">' +
      '<div class="acEvTop">' +
        '<span class="acTag">' + esc(ev.signalType) + "</span>" +
        '<span class="acTag">' + esc(ev.timing) + "</span>" +
        '<span class="acTag">mag ' + num(ev.magnitude, 2) + "</span>" +
        '<span class="acTag">conf ' + num(ev.confidence, 2) + "</span>" +
        '<span class="acTag">pol ' + pol + "</span>" +
      "</div>" +
      '<div class="acEvClaim">' + esc(ev.claim) + "</div>" +
      '<div class="acEvMeta">' + esc(ev.sourceType) + (ev.date ? " · " + esc(ev.date) : "") + " · " + src + "</div>" +
    "</div>";
  }

  function ideaRow(idea) {
    return '<div class="acIdea">• ' + esc(idea.expression) + "</div>";
  }

  function detailRow(v) {
    var ev = (v.topEvidence || []).map(evidenceRow).join("") || '<span class="acMuted">No evidence captured.</span>';
    var ideas = (v.tradeIdeas || []).map(ideaRow).join("") || '<span class="acMuted">No trade expression.</span>';
    var mc = v.marketContext || {};
    var ctx = [
      mc.momentum3mPct != null ? "3m mom " + num(mc.momentum3mPct, 0) + "%" : null,
      mc.momentum6mPct != null ? "6m mom " + num(mc.momentum6mPct, 0) + "%" : null,
      mc.pe != null ? "P/E " + num(mc.pe, 0) : null,
      mc.ratingDrift != null ? "rating drift " + (mc.ratingDrift > 0 ? "+" : "") + mc.ratingDrift : null,
      mc.marketPositioning != null ? "positioning " + num(mc.marketPositioning, 0) : null,
    ].filter(Boolean).join(" · ");
    return '<tr class="acDetail"><td colspan="8">' +
      '<div class="acRationale">' + esc(v.rationale || "") + (ctx ? '<div class="acEvMeta" style="margin-top:6px">' + esc(ctx) + "</div>" : "") + "</div>" +
      '<div class="acDetailGrid">' +
        "<div><div class=\"acStatLabel\" style=\"margin-bottom:6px\">Evidence (top)</div>" + ev + "</div>" +
        "<div><div class=\"acStatLabel\" style=\"margin-bottom:6px\">Trade ideas</div>" + ideas + "</div>" +
      "</div></td></tr>";
  }

  function renderTable(p) {
    var body = $("acBody");
    var empty = $("acEmpty");
    body.innerHTML = "";
    var verdicts = (p.verdicts || []).filter(function (v) {
      return state.filter === "all" || v.label === state.filter;
    });
    $("acVerdictSub").textContent = verdicts.length + " name(s)" + (state.filter !== "all" ? " · filtered" : "");
    if (!verdicts.length) {
      empty.style.display = "block";
      empty.textContent = p.note || "No verdicts to show. Rebuild the scan or relax the filter.";
      return;
    }
    empty.style.display = "none";
    verdicts.forEach(function (v) {
      var tr = document.createElement("tr");
      tr.className = "acRow";
      tr.innerHTML =
        "<td><b>" + esc(v.ticker) + "</b></td>" +
        "<td>" + esc((p.categories && p.categories[v.category] && p.categories[v.category].name) || v.category || "—") + "</td>" +
        "<td>" + labelChip(v.label, v.labelDisplay) + "</td>" +
        "<td>" + dirCell(v.direction) + "</td>" +
        '<td class="acNum">' + realityCell(v.realityScore) + "</td>" +
        '<td class="acNum">' + gapCell(v.consensusGap) + "</td>" +
        '<td class="acNum">' + num(v.conviction, 0) + "</td>" +
        '<td class="acNum">' + (v.evidenceCount || 0) + "</td>";
      tr.addEventListener("click", function () { toggle(v.ticker, tr); });
      body.appendChild(tr);
      if (state.open[v.ticker]) {
        body.insertAdjacentHTML("beforeend", detailRow(v));
      }
    });
  }

  function toggle(ticker, tr) {
    state.open[ticker] = !state.open[ticker];
    renderTable(state.payload);
  }

  function renderBaskets(p) {
    var el = $("acBaskets");
    var baskets = p.baskets || [];
    if (!baskets.length) {
      el.innerHTML = '<span class="acMuted">No multi-name baskets — needs ≥2 names agreeing on direction within a category.</span>';
      return;
    }
    el.innerHTML = baskets.map(function (b) {
      return '<div class="acBasket">' +
        '<div class="acBasketHead">' + dirCell(b.direction) + " " + esc(b.categoryName) +
          ' <span class="acMuted">· avg conviction ' + num(b.avgConviction, 0) + "</span></div>" +
        '<div class="acBasketBody">' + esc((b.tickers || []).join(", ")) + "</div>" +
      "</div>";
    }).join("");
  }

  function render(p) {
    state.payload = p;
    renderBanner(p);
    renderChips(p);
    renderTable(p);
    renderBaskets(p);
    var when = p.asOf ? new Date(p.asOf).toLocaleString() : "";
    var srcTxt = p.source === "scan" ? "Fresh scan" : (p.source === "rescore" ? "Re-scored from stored evidence" : (p.cached ? "Cached scan" : "Scan"));
    setStatus(srcTxt + (when ? " · " + when : "") + (p.note ? " · " + p.note : ""));
  }

  function load() {
    var btn = $("acRefresh");
    btn.disabled = true;
    setStatus("Loading scan…");
    fetch("/api/ai-capex")
      .then(function (res) {
        if (res.status === 404) throw new Error("AI Capex Reality Engine is disabled on this deployment.");
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (p) { render(p); })
      .catch(function (err) { setStatus(err.message || "Failed to load AI Capex scan.", true); })
      .then(function () { btn.disabled = false; });
  }

  // Full rebuild is the heavy ~70-ticker LLM pass — far longer than a request
  // timeout — so kick it as a detached background job and poll /status instead
  // of blocking the request (which would hang the button and eventually error).
  function rebuild() {
    var btn = $("acRefresh");
    btn.disabled = true;
    setStatus("Starting background rebuild (ingest + LLM extract + Tier-2 web)…");
    fetch("/api/ai-capex/refresh?background=true", { method: "POST" })
      .then(function (res) {
        if (res.status === 404) throw new Error("AI Capex Reality Engine is disabled on this deployment.");
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function () { pollRebuild(0); })
      .catch(function (err) { setStatus(err.message || "Failed to start rebuild.", true); btn.disabled = false; });
  }

  function pollRebuild(tries) {
    if (tries > 260) {  // ~35 min safety cap
      setStatus("Rebuild is still running in the background — reload in a few minutes to see the fresh scan.");
      $("acRefresh").disabled = false;
      return;
    }
    fetch("/api/ai-capex/status")
      .then(function (r) { return r.json(); })
      .then(function (s) {
        var lr = s.lastRun || {};
        if (s.backgroundRunning || lr.status === "running") {
          var mins = Math.round((tries * 8 / 60) * 10) / 10;
          setStatus("Rebuilding scan… ~" + mins + " min elapsed (ingest + LLM extract + Tier-2 web). You can leave this page.");
          setTimeout(function () { pollRebuild(tries + 1); }, 8000);
          return;
        }
        if (lr.status === "error") {
          setStatus("Rebuild failed: " + (lr.error || "unknown error"), true);
          $("acRefresh").disabled = false;
          return;
        }
        setStatus("Rebuild complete — loading fresh scan…");
        load();
      })
      .catch(function () { setTimeout(function () { pollRebuild(tries + 1); }, 8000); });
  }

  function init() {
    $("acRefresh").addEventListener("click", rebuild);
    load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
