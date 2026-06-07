/* NRGX Labs v2 — engine landing pages.
 * One static HTML, populated based on URL pathname. */

(function () {
  "use strict";

  var SPECS = {
    e1: {
      tag: "v2 · single-name earnings IC",
      badge: "phase 0 · scaffolding · v1 still primary",
      title: "Single-name <em>earnings IC</em>",
      sub: "v1 ranks earnings IC trades with hand-set VRP weights (30/25/20/15/10) and a fixed wing-console composite. v2 trains a learned ranker on your own desk PnL, retrieves cross-ticker analogues, and wraps every probability in a conformal coverage interval.",
      cta: { href: "https://app.nrgxlabs.com", label: "Use v1 today" },
      roadmap: [
        ["learned ranker", "offline contextual bandit", "Drops the 30/25/20/15/10 VRP weights. Trained on the existing trade journal in Redis. Doubly-robust off-policy estimator bounds improvement before live A/B."],
        ["cross-ticker analogues", "contrastive embedder", "Replaces same-ticker breach stats with up to 80 peer-event neighbors that share the same fingerprint."],
        ["diffusion paths", "intraday tick model", "Replaces the OHLC MAE proxy. Calibrated touch / breach / MAE distributions, not biased close-only proxies."],
        ["conformal CI", "split-conformal wrapper", "Every probability lands inside a coverage interval that holds out-of-sample. The trust layer."],
      ],
      endpoints: [
        ["GET", "/api/v2/regime/embed", "regime embedding"],
        ["GET", "/api/v2/analogues/search?ticker=...", "cross-ticker analogues"],
      ],
    },
    e15: {
      tag: "v2 · earnings IC scenario",
      badge: "phase 0 · the killer module · v1 still primary",
      title: "Earnings IC <em>scenario</em>",
      sub: "v1's biggest gap: same-ticker analogues only. v2 retrieves cross-ticker, cross-time neighbors in a contrastive embedding space — a NVDA setup can pull peer events from 8 tickers and 5 different years that share the same fingerprint. This is the v2 wedge: the moment a desk member says \"wait, it found analogues across other tickers? show me more.\"",
      cta: { href: "https://app.nrgxlabs.com/earnings-ic", label: "Use v1 today" },
      roadmap: [
        ["contrastive matcher", "the killer module", "ANN search in a learned event-embedding space. Drops same-ticker filter. Up to 80 peer events with similar forward 5-day path distributions."],
        ["learned conditioning", "outcome regression", "Replaces hand-coded multipliers with a regression on retrieved analogues conditioned on (VRP, news theme, anncTod, regime embedding)."],
        ["chain replay (kept)", "real ORATS chains", "Path replay still uses live chains for retrieved analogues — diffusion paths are an additive layer, not a replacement."],
        ["conformal CI", "calibrated breach %", "Out-of-sample coverage guarantees on the headline outcome distribution."],
      ],
      endpoints: [
        ["GET", "/api/v2/analogues/search?ticker=...&event_date=...&cross_ticker=true", "cross-ticker analogues"],
      ],
    },
    e2: {
      tag: "v2 · SPX weekly IC",
      badge: "phase 0 · scaffolding · v1 still primary",
      title: "SPX <em>weekly IC</em>",
      sub: "v1 bootstraps from historical weekly paths but the daily_returns field is unpopulated, so paths often degrade to a uniform-split fallback. v2 swaps bootstrap for a regime-conditional path generator, lets dealer gamma enter the wing composite directly as a learned feature, and calibrates every probability.",
      cta: { href: "https://app.nrgxlabs.com/spx", label: "Use v1 today" },
      roadmap: [
        ["path generator", "regime-conditional diffusion", "Replaces bootstrap MC. Fixes the silent daily_returns gap. Calibrated path distributions per regime cluster."],
        ["gamma feature", "first-class wing input", "Dealer gamma enters the wing composite directly, not just as an EM-preference nudge."],
        ["conformal CI", "guaranteed coverage", "Every breach / touch / MAE probability lands inside a calibrated interval."],
      ],
      endpoints: [
        ["GET", "/api/v2/regime/embed", "regime embedding"],
      ],
    },
    e14: {
      tag: "v2 · SPX IC scenario",
      badge: "phase 0 · scaffolding · v1 still primary",
      title: "SPX IC <em>scenario</em>",
      sub: "v1 ships hand-set kNN weights [vix 1.0, vix9d 0.8, vvix 0.6, term_slope 0.5, rv20 0.8, net_gex 0.5, credit 0.4] and deterministic Kelly sizing. v2 swaps the weighted-L2 kNN for a contrastive embedder, makes sizing risk-budget-aware on conformal width, and turns the exit-rule grid into a learned policy.",
      cta: { href: "https://app.nrgxlabs.com/ic-scenario", label: "Use v1 today" },
      roadmap: [
        ["contrastive matcher", "drops weighted-L2 kNN", "Learned distance metric over a learned feature space. No more hand-set economic weights."],
        ["risk-budget sizing", "confidence-aware", "Position sizing conditions on committee confidence and conformal interval width, not just historical PnL series."],
        ["learned exit policy", "replaces grid heuristic", "Trained on replayed paths plus realized desk PnL."],
      ],
      endpoints: [
        ["GET", "/api/v2/analogues/search?cross_ticker=false", "SPX-window analogues"],
      ],
    },
    mi: {
      tag: "v2 · market brain",
      badge: "phase 0 · scaffolding · v1 still primary",
      title: "Market <em>brain</em>",
      sub: "v1's MI is an 8-factor Gaussian HMM with a misleadingly-named pc1_proxy_stress (z-composite, not real PCA) and two siloed LLM narrators. v2 collapses everything into one learned regime embedding with a live UMAP projection — \"today is closest to these 5 historical days\" with one-tap drill-in to how each engine behaved in those analogues.",
      cta: { href: "https://app.nrgxlabs.com/market-intelligence", label: "Use v1 today" },
      roadmap: [
        ["regime encoder", "learned 64-d latent", "Replaces the HMM. Produces probabilities over learned clusters plus nearest historical days."],
        ["UMAP projection", "live spatial map", "2-D map of the embedding space. The desk literally sees where today sits relative to past episodes."],
        ["live attribution", "factor pulls", "Which factors are pulling regime probability today, with sign and magnitude."],
        ["one brain", "shared memory", "Three siloed LLMs (desk_insight, front_layer, per-engine advisors) collapse into one Claude with persistent memory across sessions."],
      ],
      endpoints: [
        ["GET", "/api/v2/regime/embed", "regime embedding"],
        ["GET", "/api/v2/regime/nearest?k=5", "nearest historical days"],
      ],
    },
  };

  function getEngine() {
    var p = (window.location.pathname || "/").replace(/\/+$/, "").toLowerCase();
    var slug = p.replace(/^\//, "");
    return SPECS[slug] ? slug : "e15";
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function render(slug) {
    var spec = SPECS[slug];
    document.title = "NRGX Labs v2 — " + slug.toUpperCase();

    document.getElementById("v2EngineTag").textContent = spec.tag;
    document.getElementById("v2EngineBadge").textContent = spec.badge;
    document.getElementById("v2EngineTitle").innerHTML = spec.title;
    document.getElementById("v2EngineSub").textContent = spec.sub;

    var cta = document.getElementById("v2EngineCTA");
    cta.href = spec.cta.href;
    cta.firstChild.nodeValue = spec.cta.label + " ";

    var roadmap = document.getElementById("v2EngineRoadmap");
    roadmap.innerHTML = "";
    spec.roadmap.forEach(function (row) {
      var tile = el("article", "v2BrainTile");
      tile.appendChild(el("div", "v2BrainTileLabel", row[0]));
      tile.appendChild(el("div", "v2BrainTileName", row[1]));
      tile.appendChild(el("div", "v2BrainTileNote", row[2]));
      var bar = el("div", "v2BrainBar");
      var fill = el("div", "v2BrainBarFill");
      fill.style.width = (5 + Math.random() * 8).toFixed(1) + "%";
      bar.appendChild(fill);
      tile.appendChild(bar);
      roadmap.appendChild(tile);
    });

    var endpoints = document.getElementById("v2EngineEndpoints");
    endpoints.innerHTML = "";
    spec.endpoints.forEach(function (row) {
      var card = el("article", "v2Card");
      var head = el("div", "v2CardHead");
      head.appendChild(el("span", "v2CardCode", row[0]));
      head.appendChild(el("span", "v2CardStatus", "phase 0"));
      card.appendChild(head);
      card.appendChild(el("h3", "v2CardTitle mono", row[1]));
      card.appendChild(el("p", "v2CardSub", row[2]));
      var foot = el("div", "v2CardFoot");
      var pill = el("span", "v2Pill v2Pill--cyan", "stub returns shape");
      foot.appendChild(pill);
      card.appendChild(foot);
      endpoints.appendChild(card);
    });

    document.querySelectorAll(".v2NavLink[data-engine]").forEach(function (a) {
      if (a.dataset.engine === slug) a.classList.add("is-active");
    });
  }

  // Mobile drawer (mirrors v2.js — engine pages don't load v2.js, so we
  // inline the lean version here).
  function wireDrawer() {
    var btn = document.getElementById("v2MenuToggle");
    var ovl = document.getElementById("v2NavOverlay");
    var side = document.getElementById("v2Sidebar");
    if (!btn) return;
    var open  = function () { document.body.classList.add("v2NavOpen");    btn.setAttribute("aria-expanded", "true"); };
    var close = function () { document.body.classList.remove("v2NavOpen"); btn.setAttribute("aria-expanded", "false"); };
    btn.addEventListener("click", function () {
      document.body.classList.contains("v2NavOpen") ? close() : open();
    });
    if (ovl) ovl.addEventListener("click", close);
    if (side) side.addEventListener("click", function (ev) {
      if (ev.target.closest("a")) close();
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") close();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    render(getEngine());
    wireDrawer();
  });
})();
