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

  // Independent sources corroborating the read: 0 = propagated/second-order,
  // 1 = single-source (treat with caution), 2+ = corroborated.
  function srcCell(n) {
    n = n || 0;
    if (n <= 0) return '<span class="acMuted" title="No own-evidence sources — second-order/propagated read">—</span>';
    if (n === 1) return '<span style="color:#8a5a00;font-weight:700" title="Single source — not yet independently corroborated">1</span>';
    return '<span class="acGap--pos" title="' + n + ' independent sources agree">' + n + "</span>";
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

  // Human-readable signal label + accent class (the colored left rail). Lets a
  // desk agent scan the *kind* of evidence at a glance instead of parsing enum
  // strings.
  var SIG_META = {
    capex_up:          { label: "Capex \u2191",   cls: "pos" },
    capex_down:        { label: "Capex \u2193",   cls: "neg" },
    supply_constraint: { label: "Supply tight",   cls: "pos" },
    demand_pull:       { label: "Demand pull",    cls: "pos" },
    delay:             { label: "Delay",          cls: "delay" },
    hype_language:     { label: "Hype language",  cls: "hype" },
    second_order_link: { label: "Linkage",        cls: "link" },
  };
  var IDEA_LABEL = { directional: "Directional", options: "Options", watch: "Watch", basket: "Basket" };

  function evidenceRow(ev) {
    var meta = SIG_META[ev.signalType] || { label: ev.signalType, cls: "" };
    var cls = meta.cls;
    if ((ev.polarity || 0) < 0 && cls === "pos") cls = "neg";  // hard-positive signal but bearish reading
    var s = (ev.magnitude || 0) * (ev.confidence || 0);
    var st = s >= 0.6 ? ["hi", "strong"] : (s >= 0.35 ? ["md", "medium"] : ["lo", "light"]);
    var srcLine = esc(ev.sourceType || "") + (ev.date ? " \u00b7 " + esc(ev.date) : "");
    var link = ev.sourceUrl
      ? '<div class="acEvLink"><a href="' + esc(ev.sourceUrl) + '" target="_blank" rel="noopener">' + esc(ev.sourceTitle || ev.sourceUrl) + "</a></div>"
      : (ev.sourceTitle ? '<div class="acEvLink acMuted">' + esc(ev.sourceTitle) + "</div>" : "");
    return '<div class="acEv acEv--' + cls + '">' +
      '<div class="acEvHead">' +
        '<span class="acEvSig">' + esc(meta.label) + "</span>" +
        '<span class="acEvChip">' + esc(ev.timing) + "</span>" +
        '<span class="acEvStrength acEvStrength--' + st[0] + '" title="magnitude ' + num(ev.magnitude, 2) + " \u00d7 confidence " + num(ev.confidence, 2) + '">' + st[1] + "</span>" +
        '<span class="acEvSrc">' + srcLine + "</span>" +
      "</div>" +
      '<div class="acEvClaim">' + esc(ev.claim) + "</div>" + link +
    "</div>";
  }

  function ideaRow(idea) {
    var t = IDEA_LABEL[idea.type] || (idea.type || "Idea");
    var dir = idea.direction;
    var dirCls = dir === "long" ? "acDir--long" : (dir === "short" ? "acDir--short" : "acDir--neutral");
    var dirEl = (dir && dir !== "neutral") ? '<span class="' + dirCls + '" style="font-size:11px;font-weight:800;text-transform:uppercase">' + esc(dir) + "</span>" : "";
    var structEl = idea.structure ? '<span class="acMuted" style="font-weight:600">' + esc(idea.structure) + "</span>" : "";
    return '<div class="acIdea">' +
      '<div class="acIdeaHead"><span class="acEvChip">' + esc(t) + "</span>" + dirEl + structEl + "</div>" +
      '<div class="acIdeaBody">' + esc(idea.expression || "") + "</div>" +
    "</div>";
  }

  function ctxChips(v) {
    var mc = v.marketContext || {};
    var n = v.independentSources || 0;
    var chips = [];
    var corrCls = n >= 2 ? "acCtxChip--good" : (n === 1 ? "acCtxChip--warn" : "");
    var corrTxt = n === 0 ? "propagated / second-order read" : ("<b>" + n + "</b> independent source" + (n === 1 ? " (verify)" : "s"));
    chips.push('<span class="acCtxChip ' + corrCls + '">' + corrTxt + "</span>");
    if (mc.marketPositioning != null) chips.push('<span class="acCtxChip">positioning <b>' + num(mc.marketPositioning, 0) + "</b></span>");
    if (mc.momentum3mPct != null) chips.push('<span class="acCtxChip">3m mom <b>' + num(mc.momentum3mPct, 0) + "%</b></span>");
    if (mc.momentum6mPct != null) chips.push('<span class="acCtxChip">6m mom <b>' + num(mc.momentum6mPct, 0) + "%</b></span>");
    if (mc.pe != null) chips.push('<span class="acCtxChip">P/E <b>' + num(mc.pe, 0) + "</b></span>");
    if (mc.ratingDrift != null && mc.ratingDrift !== 0) chips.push('<span class="acCtxChip">rating drift <b>' + (mc.ratingDrift > 0 ? "+" : "") + mc.ratingDrift + "</b></span>");
    return chips.join("");
  }

  function horizonRow(v) {
    var h = v.horizon || {};
    if (!h.band) return "";
    var parts = ['<span class="acHorizonLabel">Horizon</span>', '<span class="acHorizonBand">' + esc(h.band) + "</span>"];
    if (h.catalyst) {
      var d = (h.daysToCatalyst != null) ? " (" + h.daysToCatalyst + "d)" : "";
      parts.push('<span class="acHorizonSep">\u00b7</span><span>' + esc(h.catalyst) + d + "</span>");
    }
    if (h.impliedMovePct != null && h.thesisMovePct != null) {
      parts.push('<span class="acHorizonSep">\u00b7</span><span>implied \u00b1' + h.impliedMovePct + "% vs thesis ~" + h.thesisMovePct + "%</span>");
    }
    if (h.assessment) {
      parts.push('<span class="acAssess acAssess--' + esc(h.assessment) + '">' + esc(h.assessment) + "</span>");
    }
    return '<div class="acHorizon">' + parts.join("") + "</div>";
  }

  function detailRow(v) {
    var evList = v.topEvidence || [];
    var ev = evList.map(evidenceRow).join("") || '<span class="acMuted">No evidence captured.</span>';
    var ideas = (v.tradeIdeas || []).map(ideaRow).join("") || '<span class="acMuted">No trade expression yet — not actionable.</span>';
    var dirCls = v.direction === "long" ? "acDir--long" : (v.direction === "short" ? "acDir--short" : "acDir--neutral");
    var gap = v.consensusGap;
    var action =
      '<div class="acActionBar">' +
        '<span class="acActionTicker">' + esc(v.ticker) + "</span>" +
        labelChip(v.label, v.labelDisplay) +
        '<span class="acActionDir ' + dirCls + '">' + esc(v.direction) + "</span>" +
        '<span class="acActionMeta">conviction <b>' + num(v.conviction, 0) + "</b> \u00b7 reality <b>" + num(v.realityScore, 0) +
          "</b> \u00b7 gap <b>" + (gap > 0 ? "+" : "") + num(gap, 0) + "</b></span>" +
      "</div>";
    return '<tr class="acDetail"><td colspan="9"><div class="acDetailPanel">' +
      action +
      horizonRow(v) +
      (v.rationale ? '<div class="acRationale">' + esc(v.rationale) + "</div>" : "") +
      '<div class="acCtxChips">' + ctxChips(v) + "</div>" +
      '<div class="acSection"><div class="acColHead">What to do</div><div class="acIdeas">' + ideas + "</div></div>" +
      '<div class="acSection"><div class="acColHead">Evidence trail \u00b7 ' + evList.length + " shown</div>" + ev + "</div>" +
    "</div></td></tr>";
  }

  // The detail panel lives in a colspan cell of a wide, horizontally-scrollable
  // table, so left unconstrained it inherits the table's (min 920px) width and
  // clips. Pin each open panel (position: sticky, left:0) to the *visible*
  // content width instead.
  //
  // The wrapper's overflow-x:auto means its clientWidth is the *visible* width
  // and is immune to the (min 920px) table inside it — the table scrolls, the
  // wrapper box doesn't grow. We fall back to the container's inner width if the
  // wrapper isn't measurable yet. Both width and max-width are set, inside rAF
  // (after the freshly-inserted rows are laid out), so the panel can never
  // exceed the viewport even on browsers slow to honor an explicit width on a
  // sticky table-cell child.
  function sizePanels() {
    var panels = document.querySelectorAll(".acDetailPanel");
    if (!panels.length) return;
    var wrap = document.querySelector(".acTableWrap");
    var container = document.querySelector(".container");
    requestAnimationFrame(function () {
      var w = (wrap && wrap.clientWidth) || (container && container.clientWidth) || 0;
      if (!w) return;
      Array.prototype.forEach.call(panels, function (p) {
        p.style.width = w + "px";
        p.style.maxWidth = w + "px";
      });
    });
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
        '<td class="acNum">' + (v.evidenceCount || 0) + "</td>" +
        '<td class="acNum">' + srcCell(v.independentSources) + "</td>";
      tr.addEventListener("click", function () { toggle(v.ticker, tr); });
      body.appendChild(tr);
      if (state.open[v.ticker]) {
        body.insertAdjacentHTML("beforeend", detailRow(v));
      }
    });
    sizePanels();
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
    window.addEventListener("resize", sizePanels);
    load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
