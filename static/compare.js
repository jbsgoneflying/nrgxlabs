/**
 * Compare Page JavaScript
 * 
 * Handles multi-ticker comparison for Engine 1 earnings ranking.
 */

(function () {
  "use strict";

  // DOM helpers
  const $ = (id) => document.getElementById(id);
  const escapeHtml = (s) =>
    String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  // Format helpers
  const fmt0 = (v) => (v == null ? "—" : Math.round(v).toLocaleString());
  const fmt1 = (v) => (v == null ? "—" : Number(v).toFixed(1));
  const fmt2 = (v) => (v == null ? "—" : Number(v).toFixed(2));
  const fmtPct = (v) => (v == null ? "—" : `${Number(v).toFixed(1)}%`);

  // State
  let lastPayload = null;
  let lastGamePlan = null;
  let isBusy = false;
  let gamePlanBusy = false;

  /**
   * Slim compare + optional game plan for Raven Chat (omit full breach payloads so
   * trimEngineData never drops rankings entirely).
   */
  function buildChatContextPayload(comparePayload, gamePlanResponse) {
    const rankings = (comparePayload.rankings || []).map((r) => {
      const fp = r.fullPayload || {};
      const vrp = fp.vrpAnalysis || {};
      return {
        ticker: r.ticker,
        rank: r.rank,
        compositeScore: r.compositeScore,
        tier: r.tier,
        tierLabel: r.tierLabel,
        factors: r.factors,
        quickStats: r.quickStats,
        liquidityWarning: r.liquidityWarning,
        liquidityBlock: r.liquidityBlock,
        deskConsensus: fp.deskConsensus,
        nextEvent: fp.nextEvent,
        entryQuality: fp.entryQuality,
        widthComparison: fp.widthComparison,
        emPreference: fp.emPreference,
        vrpAnalysis:
          vrp && typeof vrp === "object"
            ? {
                vrpScore: vrp.vrpScore,
                ivElevation: vrp.ivElevation,
              }
            : undefined,
      };
    });

    const ctx = {
      engine: "Engine 10 — Multi-ticker Compare",
      asOfDate: comparePayload.asOfDate,
      k: comparePayload.k,
      n: comparePayload.n,
      years: comparePayload.years,
      tickersRequested: comparePayload.tickersRequested,
      tickersAnalyzed: comparePayload.tickersAnalyzed,
      tickerList: rankings.map((x) => x.ticker),
      summary: comparePayload.summary,
      rankings,
      errors: comparePayload.errors,
    };

    if (gamePlanResponse) {
      const advisor = gamePlanResponse.advisor || {};
      const det = gamePlanResponse.deterministicAllocation || {};
      const plan = advisor.allocationPlan || det.allocations || [];
      ctx.gamePlan = {
        source: advisor._source || "fallback",
        model: advisor._model || null,
        concentration: advisor.concentrationChoice,
        totalDeployedPct: advisor.totalDeployedPct ?? det.totalDeployed,
        cashReservePct: advisor.cashReservePct ?? det.cashReserve,
        regimeLabel: det.regimeLabel,
        regimeCap: det.regimeCap,
        allocations: plan,
        sectorBuckets: det.sectorBuckets,
        conflicts: det.conflicts,
        keyRisks: advisor.keyRisks,
        correlationNote: advisor.correlationNote,
        portfolioRationale: advisor.portfolioRationale,
        deskNote: advisor.deskNote,
        regimeAdjustment: advisor.regimeAdjustment,
        conflictResolution: advisor.conflictResolution,
      };
    }

    return ctx;
  }

  function pushRavenChatContext() {
    if (!window.RavenChat || !lastPayload) return;
    RavenChat.setEngineContext(
      "engine10",
      buildChatContextPayload(lastPayload, lastGamePlan)
    );
  }

  // Status icons
  const STATUS_ICONS = {
    good: '<span class="factorStatus factorStatus--good">✓</span>',
    ok: '<span class="factorStatus factorStatus--ok">⚠</span>',
    poor: '<span class="factorStatus factorStatus--poor">✗</span>',
  };

  /**
   * Fetch comparison data from API
   */
  async function fetchComparison(tickers, k) {
    const params = new URLSearchParams({
      tickers: tickers,
      k: k,
      n: "20",
      years: "5",
    });

    const res = await fetch(`/api/breach-compare?${params}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  /**
   * Render tier summary badges
   */
  function renderTierSummary(summary) {
    const container = $("tierSummary");
    if (!summary) {
      container.innerHTML = "";
      return;
    }

    const tiers = [
      { key: "slamDunk", label: "Slam Dunk", class: "slamDunk" },
      { key: "strong", label: "Strong", class: "strong" },
      { key: "standard", label: "Standard", class: "standard" },
      { key: "caution", label: "Caution", class: "caution" },
      { key: "avoid", label: "Avoid", class: "avoid" },
    ];

    const html = tiers
      .filter((t) => summary[t.key] > 0)
      .map(
        (t) =>
          `<span class="tierCount tierCount--${t.class}">${summary[t.key]} ${t.label}</span>`
      )
      .join("");

    container.innerHTML = html;
  }

  /**
   * Get ticker logo URL
   */
  function getLogoUrl(ticker) {
    return `https://financialmodelingprep.com/image-stock/${ticker}.png`;
  }

  /**
   * Render a single ranking card
   */
  function renderRankCard(item) {
    const ticker = escapeHtml(item.ticker);
    const tier = item.tier || "avoid";
    const tierLabel = escapeHtml(item.tierLabel || "—");
    const score = fmt0(item.compositeScore);
    const rank = item.rank || "?";
    const factors = item.factors || {};

    // Factor cells
    const factorCells = [
      {
        key: "breachRate",
        label: "Breach",
        value: factors.breachRate?.label || "—",
        status: factors.breachRate?.status || "ok",
      },
      {
        key: "ivElevation",
        label: "IV Rank",
        value: factors.ivElevation?.label || "—",
        status: factors.ivElevation?.status || "ok",
      },
      {
        key: "emRichness",
        label: "EM Rich",
        value: factors.emRichness?.label || "—",
        status: factors.emRichness?.status || "ok",
      },
      {
        key: "liquidity",
        label: "Liquidity",
        value: factors.liquidity?.label || "—",
        status: factors.liquidity?.status || "ok",
      },
      {
        key: "marketRegime",
        label: "Regime",
        value: factors.marketRegime?.label || "—",
        status: factors.marketRegime?.status || "ok",
      },
    ];

    const factorHtml = factorCells
      .map(
        (f) => `
        <div class="factorCell">
          <div class="factorLabel">${f.label}</div>
          <div class="factorValue">
            ${escapeHtml(f.value)}
            ${STATUS_ICONS[f.status] || ""}
          </div>
        </div>
      `
      )
      .join("");

    // Quick stats for details
    const qs = item.quickStats || {};
    const detailsHtml = `
      <div class="detailsGrid">
        <div class="detailItem">
          <div class="detailLabel">Breach Rate</div>
          <div class="detailValue">${fmtPct(qs.breachRatePct)}</div>
        </div>
        <div class="detailItem">
          <div class="detailLabel">ORATS EM</div>
          <div class="detailValue">${fmtPct(qs.oratsEmPct ?? qs.impliedMovePct)}</div>
        </div>
        <div class="detailItem">
          <div class="detailLabel">Straddle EM</div>
          <div class="detailValue">${fmtPct(qs.straddleEmPct)}</div>
        </div>
        <div class="detailItem">
          <div class="detailLabel">Events Used</div>
          <div class="detailValue">${fmt0(qs.eventsUsed)}</div>
        </div>
        <div class="detailItem">
          <div class="detailLabel">Tail Coverage</div>
          <div class="detailValue">${factors.tailCoverage?.label || "—"} ${STATUS_ICONS[factors.tailCoverage?.status] || ""}</div>
        </div>
        <div class="detailItem">
          <div class="detailLabel">Event Risk</div>
          <div class="detailValue">${factors.eventRisk?.label || "—"} ${STATUS_ICONS[factors.eventRisk?.status] || ""}</div>
        </div>
      </div>
    `;

    const liqWarning = item.liquidityWarning;
    const liqBlock = item.liquidityBlock;
    const liqCls = liqBlock ? " rankCard--liqBlock" : liqWarning ? " rankCard--liqFlag" : "";

    return `
      <div class="rankCard rankCard--${tier}${liqCls}" data-ticker="${ticker}">
        <div class="rankCardHeader">
          <div class="rankBadge">#${rank}</div>
          <div class="rankTickerInfo">
            <img 
              class="rankTickerLogo" 
              src="${getLogoUrl(ticker)}" 
              alt="${ticker}"
              onerror="this.style.display='none'"
            />
            <span class="rankTickerSymbol">${ticker}</span>
          </div>
          <span class="rankTierBadge rankTierBadge--${tier}">${tierLabel}</span>
          <span class="rankScore">Score: ${score}</span>
        </div>
        
        <div class="factorRow">
          ${factorHtml}
        </div>
        
        <div class="rankCardActions">
          <a class="rankCardLink" href="/breach?ticker=${ticker}&k=1.5&mc=1&autorun=1" target="_blank">
            Full Analysis →
          </a>
          <button class="detailsToggle" onclick="toggleDetails(this)">
            ▼ More Details
          </button>
        </div>
        
        <div class="rankCardDetails">
          ${detailsHtml}
        </div>
      </div>
    `;
  }

  /**
   * Toggle details panel
   */
  window.toggleDetails = function (btn) {
    const card = btn.closest(".rankCard");
    const details = card.querySelector(".rankCardDetails");
    const isOpen = details.classList.contains("open");

    if (isOpen) {
      details.classList.remove("open");
      btn.textContent = "▼ More Details";
    } else {
      details.classList.add("open");
      btn.textContent = "▲ Less Details";
    }
  };

  /**
   * Render all rankings
   */
  function renderRankings(rankings) {
    const container = $("rankingsGrid");
    if (!rankings || rankings.length === 0) {
      container.innerHTML =
        '<div class="loadingState">No tickers analyzed. Enter tickers above and click Compare.</div>';
      return;
    }

    container.innerHTML = rankings.map(renderRankCard).join("");
  }

  /**
   * Render errors list
   */
  function renderErrors(errors) {
    const container = $("errorsList");
    if (!errors || errors.length === 0) {
      container.classList.add("hidden");
      container.innerHTML = "";
      return;
    }

    container.classList.remove("hidden");
    container.innerHTML = `
      <h4>Failed to analyze (${errors.length}):</h4>
      ${errors
        .map(
          (e) =>
            `<div class="errorItem"><b>${escapeHtml(e.ticker)}</b>: ${escapeHtml(e.error)}</div>`
        )
        .join("")}
    `;
  }

  /**
   * Render full results
   */
  function renderResults(payload) {
    lastPayload = payload;
    lastGamePlan = null;
    pushRavenChatContext();

    // Show results section
    $("results").classList.remove("hidden");

    // Update meta
    const meta = `${payload.tickersAnalyzed} of ${payload.tickersRequested} analyzed · k=${payload.k}× · ${payload.asOfDate}`;
    $("resultsMeta").textContent = meta;

    // Render components
    renderTierSummary(payload.summary);
    renderRankings(payload.rankings);
    renderErrors(payload.errors);

    // Show game plan button if we have tradeable tickers
    const gpBtn = $("gamePlanBtn");
    if (payload.rankings && payload.rankings.length >= 1) {
      gpBtn.classList.remove("hidden");
    } else {
      gpBtn.classList.add("hidden");
    }
    $("gamePlanPanel").classList.add("hidden");
    $("gamePlanPanel").innerHTML = "";
  }

  /**
   * Show loading state
   */
  function showLoading(tickerCount) {
    // Use Raven Loading Overlay
    if (window.RavenLoading) {
      window.RavenLoading.show({ status: `Comparing ${tickerCount} ticker${tickerCount > 1 ? "s" : ""}...` });
      window.RavenLoading.setProgress(10, "Fetching data...");
    }
    
    // Also update inline UI
    $("results").classList.remove("hidden");
    $("resultsMeta").textContent = `Analyzing ${tickerCount} ticker${tickerCount > 1 ? "s" : ""}...`;
    $("tierSummary").innerHTML = "";
    $("rankingsGrid").innerHTML =
      '<div class="loadingState">Fetching data and computing rankings...</div>';
    $("errorsList").classList.add("hidden");
  }

  /**
   * Show error state
   */
  function showError(message) {
    $("results").classList.remove("hidden");
    $("resultsMeta").textContent = "Error";
    $("tierSummary").innerHTML = "";
    $("rankingsGrid").innerHTML = `<div class="loadingState" style="color: #b42823;">${escapeHtml(message)}</div>`;
    $("errorsList").classList.add("hidden");
  }

  /**
   * Handle form submission
   */
  async function handleSubmit(e) {
    e.preventDefault();
    if (isBusy) return;

    const tickersInput = $("tickers").value.trim();
    const k = $("k").value;

    if (!tickersInput) {
      showError("Please enter at least one ticker.");
      return;
    }

    // Parse and validate tickers
    const tickers = tickersInput
      .toUpperCase()
      .split(/[,\s]+/)
      .filter((t) => t.length > 0);

    if (tickers.length === 0) {
      showError("Please enter at least one valid ticker.");
      return;
    }

    if (tickers.length > 10) {
      showError("Maximum 10 tickers allowed. Please reduce your list.");
      return;
    }

    isBusy = true;
    showLoading(tickers.length);

    try {
      const payload = await fetchComparison(tickers.join(","), k);
      
      if (window.RavenLoading) {
        window.RavenLoading.setProgress(75, "Ranking results...");
      }
      
      renderResults(payload);
      
      if (window.RavenLoading) {
        window.RavenLoading.setProgress(95, "Rendering...");
      }
    } catch (err) {
      console.error("Comparison failed:", err);
      showError(err.message || "Failed to fetch comparison data.");
    } finally {
      isBusy = false;
      if (window.RavenLoading) {
        window.RavenLoading.hide();
      }
    }
  }

  // -----------------------------------------------------------------
  // Game Plan (E10 Portfolio Advisor)
  // -----------------------------------------------------------------

  function stripRankingsForAdvisor(rankings) {
    // KEEP enough per-ticker context for the server to recompute
    // deskConsensus/emPreference if the public /api/breach-compare
    // response stripped them (E1_EMIT_DESK_CONSENSUS=False default).
    // regime / gapVsCtc / eventRisk feed the regime-stress + macro
    // LEAN_PASS nudges in compute_e1_desk_consensus.
    const KEEP_KEYS = [
      "deskConsensus", "vrpAnalysis", "entryQuality", "emPreference",
      "widthComparison", "emBreachSummary", "nextEvent",
      "regime", "gapVsCtc", "eventRisk",
    ];
    return (rankings || []).map((r) => {
      const fp = r.fullPayload || {};
      const slim = {};
      for (const k of KEEP_KEYS) {
        if (fp[k] !== undefined) slim[k] = fp[k];
      }
      if (fp.current) {
        slim.current = {
          stockPrice: fp.current.stockPrice,
          impliedMovePct: fp.current.impliedMovePct,
        };
      }
      if (fp.summary) {
        slim.summary = { breachRate: fp.summary.breachRate };
      }
      return {
        ticker: r.ticker,
        compositeScore: r.compositeScore,
        tier: r.tier,
        tierLabel: r.tierLabel,
        rank: r.rank,
        factors: r.factors,
        fullPayload: slim,
      };
    });
  }

  async function fetchGamePlan(rankings) {
    const slim = stripRankingsForAdvisor(rankings);
    const res = await fetch("/api/breach-compare/advisor", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rankings: slim }),
    });
    if (res.redirected || res.url.includes("/login")) {
      throw new Error("Session expired — please refresh the page and log in again.");
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  function renderAllocCard(a) {
    const pct = a.allocationPct != null ? fmt1(a.allocationPct) : "0";
    const action = a.action || a.verdict || "SKIP";
    const em = a.emMultiple || a.preferredEm || "—";
    const wing = a.wingWidth || a.suggestedWing || "—";
    const entry = escapeHtml(a.entryWindow || "");
    const exit = escapeHtml(a.exitPlan || "");
    const rationale = escapeHtml(a.rationale || "");
    const sector = escapeHtml(a.sector || "");

    return `
      <div class="gpAllocCard">
        <div class="gpAllocPct">${pct}%</div>
        <div class="gpAllocInfo">
          <div class="gpAllocTicker">${escapeHtml(a.ticker)}</div>
          <div class="gpAllocMeta">
            <span>EM: ${em}x</span>
            <span>Wing: $${wing}</span>
            ${sector ? `<span>Sector: ${sector}</span>` : ""}
          </div>
          ${entry ? `<div class="gpAllocMeta">Entry: ${entry}</div>` : ""}
          ${exit ? `<div class="gpAllocMeta">Exit: ${exit}</div>` : ""}
          ${rationale ? `<div class="gpAllocRationale">${rationale}</div>` : ""}
        </div>
        <div class="gpAllocAction">
          <span class="gpActionBadge gpActionBadge--${action}">${action.replace("_", " ")}</span>
        </div>
      </div>
    `;
  }

  function renderGamePlan(data) {
    const panel = $("gamePlanPanel");
    const advisor = data.advisor || {};
    const det = data.deterministicAllocation || {};
    const plan = advisor.allocationPlan || det.allocations || [];
    const source = advisor._source || "fallback";
    const concentration = advisor.concentrationChoice || "balanced";

    const totalDeployed = advisor.totalDeployedPct ?? det.totalDeployed ?? 0;
    const cashReserve = advisor.cashReservePct ?? det.cashReserve ?? 0;
    const regimeLabel = det.regimeLabel || "moderate";
    const regimeCap = det.regimeCap != null ? `${Math.round(det.regimeCap * 100)}%` : "—";

    const tradeItems = plan.filter((a) => (a.action || a.verdict || "SKIP") !== "SKIP");
    const skipItems = plan.filter((a) => (a.action || a.verdict || "SKIP") === "SKIP");

    const sectorBuckets = det.sectorBuckets || {};
    const hasOverlap = Object.values(sectorBuckets).some(
      (arr) => Array.isArray(arr) && arr.length > 1
    );
    const conflicts = det.conflicts || [];
    const risks = advisor.keyRisks || [];

    let html = `
      <div class="gpHeader">
        <div class="gpTitle">Allocation Game Plan</div>
        <span class="gpBadge">${concentration}</span>
      </div>

      <div class="gpDeployBar">
        <div class="gpDeployStat">
          <div class="gpDeployLabel">Deployed</div>
          <div class="gpDeployValue">${fmt1(totalDeployed)}%</div>
        </div>
        <div class="gpDeployStat">
          <div class="gpDeployLabel">Cash Reserve</div>
          <div class="gpDeployValue">${fmt1(cashReserve)}%</div>
        </div>
        <div class="gpDeployStat">
          <div class="gpDeployLabel">Regime</div>
          <div class="gpDeployValue">${escapeHtml(regimeLabel)}</div>
        </div>
        <div class="gpDeployStat">
          <div class="gpDeployLabel">Regime Cap</div>
          <div class="gpDeployValue">${regimeCap}</div>
        </div>
      </div>
    `;

    if (tradeItems.length > 0) {
      html += `<div class="gpSectionTitle">Allocations</div>`;
      html += `<div class="gpAllocGrid">${tradeItems.map(renderAllocCard).join("")}</div>`;
    }

    if (skipItems.length > 0) {
      html += `<div class="gpSectionTitle" style="opacity:0.6;">Skipped</div>`;
      html += `<div class="gpAllocGrid">${skipItems.map(renderAllocCard).join("")}</div>`;
    }

    // Sector tags
    const sectorEntries = Object.entries(sectorBuckets);
    if (sectorEntries.length > 0) {
      html += `<div class="gpSectionTitle">Sector Exposure</div><div class="gpSectorTags">`;
      for (const [sector, tickers] of sectorEntries) {
        const overlap = Array.isArray(tickers) && tickers.length > 1;
        const cls = overlap ? "gpSectorTag gpSectorTag--overlap" : "gpSectorTag";
        const names = Array.isArray(tickers) ? tickers.join(", ") : "";
        html += `<span class="${cls}">${escapeHtml(sector)}: ${escapeHtml(names)}</span>`;
      }
      html += `</div>`;
    }

    // Correlation note
    if (advisor.correlationNote) {
      html += `<div class="gpSectionTitle">Correlation Assessment</div>`;
      html += `<div class="gpTextBlock">${escapeHtml(advisor.correlationNote)}</div>`;
    }

    // Conflicts
    if (conflicts.length > 0) {
      html += `<div class="gpSectionTitle">Timing Conflicts</div>`;
      for (const c of conflicts) {
        html += `<div class="gpConflicts"><b>${escapeHtml(c.session)} ${escapeHtml(c.date)}</b>: ${escapeHtml(c.tickers?.join(", "))} — ${escapeHtml(c.note)}</div>`;
      }
      if (advisor.conflictResolution) {
        html += `<div class="gpTextBlock">${escapeHtml(advisor.conflictResolution)}</div>`;
      }
    }

    // Regime adjustment
    if (advisor.regimeAdjustment) {
      html += `<div class="gpSectionTitle">Regime Adjustment</div>`;
      html += `<div class="gpTextBlock">${escapeHtml(advisor.regimeAdjustment)}</div>`;
    }

    // Portfolio rationale
    if (advisor.portfolioRationale) {
      html += `<div class="gpSectionTitle">Portfolio Rationale</div>`;
      html += `<div class="gpTextBlock">${escapeHtml(advisor.portfolioRationale)}</div>`;
    }

    // Key risks
    if (risks.length > 0) {
      html += `<div class="gpSectionTitle">Key Risks</div><div class="gpRiskList">`;
      for (const r of risks) {
        html += `<span class="gpRiskItem">${escapeHtml(r)}</span>`;
      }
      html += `</div>`;
    }

    // Desk note
    if (advisor.deskNote) {
      html += `<div class="gpSectionTitle">Desk Note</div>`;
      html += `<div class="gpDeskNote">${escapeHtml(advisor.deskNote)}</div>`;
    }

    html += `<div class="gpSourceTag">Source: ${source === "llm" ? "AI Portfolio Advisor" : "Deterministic Model"}${advisor._model ? ` (${advisor._model})` : ""}</div>`;

    panel.innerHTML = html;
    panel.classList.remove("hidden");
  }

  async function handleGamePlan() {
    if (gamePlanBusy || !lastPayload) return;
    gamePlanBusy = true;

    const btn = $("gamePlanBtn");
    btn.disabled = true;
    btn.textContent = "Building Game Plan...";

    const panel = $("gamePlanPanel");
    panel.classList.remove("hidden");
    panel.innerHTML = '<div class="loadingState">Running portfolio advisor — analyzing allocations, correlations, and timing...</div>';

    if (window.RavenLoading) {
      window.RavenLoading.show({ status: "Building game plan...", clearResults: false });
      window.RavenLoading.setProgress(20, "Running portfolio advisor...");
    }

    try {
      const data = await fetchGamePlan(lastPayload.rankings);

      if (window.RavenLoading) {
        window.RavenLoading.setProgress(80, "Rendering game plan...");
      }

      renderGamePlan(data);
      lastGamePlan = data;
      pushRavenChatContext();
    } catch (err) {
      console.error("Game plan failed:", err);
      panel.innerHTML = `<div class="loadingState" style="color: #b42823;">Game plan failed: ${escapeHtml(err.message)}</div>`;
    } finally {
      gamePlanBusy = false;
      btn.disabled = false;
      btn.textContent = "Get Game Plan";
      if (window.RavenLoading) {
        window.RavenLoading.hide();
      }
    }
  }

  /**
   * Initialize page
   */
  function init() {
    // Form submission
    $("form").addEventListener("submit", handleSubmit);

    // Game Plan button
    $("gamePlanBtn").addEventListener("click", handleGamePlan);

    // Auto-uppercase ticker input
    $("tickers").addEventListener("input", function () {
      const pos = this.selectionStart;
      this.value = this.value.toUpperCase();
      this.setSelectionRange(pos, pos);
    });

    // Check for URL params (for deep linking)
    const params = new URLSearchParams(window.location.search);
    const tickersParam = params.get("tickers");
    if (tickersParam) {
      $("tickers").value = tickersParam;
      const kParam = params.get("k");
      if (kParam) $("k").value = kParam;
      // Auto-run if tickers provided
      handleSubmit(new Event("submit"));
    }

    // Initialize tooltips
    if (typeof initTooltips === "function") {
      initTooltips();
    }
  }

  // Initialize on DOM ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
