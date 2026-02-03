/**
 * Backtest Engine - Historical Performance Analysis
 * Evaluates Engine 3 (Red Dog) and Engine 4 (Ichimoku) A+ signals
 */

(function() {
  "use strict";

  // -----------------------------------------------------------------------------
  // State
  // -----------------------------------------------------------------------------

  const state = {
    loading: false,
    result: null,
  };

  // -----------------------------------------------------------------------------
  // DOM Helpers
  // -----------------------------------------------------------------------------

  function $(id) {
    return document.getElementById(id);
  }

  function setText(id, text) {
    const el = $(id);
    if (el) el.textContent = text;
  }

  function setHtml(id, html) {
    const el = $(id);
    if (el) el.innerHTML = html;
  }

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // -----------------------------------------------------------------------------
  // Formatting
  // -----------------------------------------------------------------------------

  function formatPct(val, decimals = 1) {
    if (val == null || isNaN(val)) return "—";
    const sign = val >= 0 ? "+" : "";
    return sign + val.toFixed(decimals) + "%";
  }

  function formatR(val, decimals = 2) {
    if (val == null || isNaN(val)) return "—";
    const sign = val >= 0 ? "+" : "";
    return sign + val.toFixed(decimals) + "R";
  }

  function formatPrice(val) {
    if (val == null || isNaN(val)) return "—";
    return "$" + val.toFixed(2);
  }

  function getValueClass(val) {
    if (val == null) return "neutral";
    if (val > 0) return "positive";
    if (val < 0) return "negative";
    return "neutral";
  }

  // -----------------------------------------------------------------------------
  // API
  // -----------------------------------------------------------------------------

  async function runBacktest(engine, trades) {
    const url = `/api/backtest?engine=${encodeURIComponent(engine)}&trades=${encodeURIComponent(trades)}`;
    const resp = await fetch(url);
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`API error ${resp.status}: ${text.slice(0, 200)}`);
    }
    return resp.json();
  }

  // -----------------------------------------------------------------------------
  // Rendering
  // -----------------------------------------------------------------------------

  function renderOverallStats(data) {
    const overall = data.overall || {};
    
    // Total trades
    setText("statTotalTrades", overall.totalTrades || 0);
    
    // Win rate
    const winRateEl = $("statWinRate");
    if (winRateEl) {
      winRateEl.textContent = (overall.winRate || 0).toFixed(1) + "%";
      winRateEl.className = "metricValue " + (overall.winRate >= 50 ? "positive" : "negative");
    }
    setText("statWinLoss", `${overall.wins || 0} W / ${overall.losses || 0} L`);
    
    // Total P/L
    const plEl = $("statTotalPL");
    if (plEl) {
      plEl.textContent = formatPct(overall.totalPlPct, 2);
      plEl.className = "metricValue " + getValueClass(overall.totalPlPct);
    }
    
    // Avg R
    const avgREl = $("statAvgR");
    if (avgREl) {
      avgREl.textContent = formatR(overall.avgRMultiple);
      avgREl.className = "metricValue " + getValueClass(overall.avgRMultiple);
    }
    setText("statAvgWinLoss", `Win ${formatR(overall.avgWinR)} / Loss ${formatR(overall.avgLossR)}`);
    
    // Meta
    const dateRange = data.dateRange || {};
    setText("overallMeta", `${dateRange.start || "?"} to ${dateRange.end || "?"}`);
  }

  function renderComparison(data) {
    const aligned = data.aligned || {};
    const unaligned = data.unaligned || {};
    
    // Aligned row
    setText("alignedTrades", aligned.trades || 0);
    const alignedWrEl = $("alignedWinRate");
    if (alignedWrEl) {
      alignedWrEl.textContent = (aligned.winRate || 0).toFixed(1) + "%";
      alignedWrEl.className = aligned.winRate >= 50 ? "positive" : "negative";
    }
    const alignedPlEl = $("alignedPL");
    if (alignedPlEl) {
      alignedPlEl.textContent = formatPct(aligned.plPct, 2);
      alignedPlEl.className = getValueClass(aligned.plPct);
    }
    const alignedREl = $("alignedAvgR");
    if (alignedREl) {
      alignedREl.textContent = formatR(aligned.avgR);
      alignedREl.className = getValueClass(aligned.avgR);
    }
    
    // Unaligned row
    setText("unalignedTrades", unaligned.trades || 0);
    const unalignedWrEl = $("unalignedWinRate");
    if (unalignedWrEl) {
      unalignedWrEl.textContent = (unaligned.winRate || 0).toFixed(1) + "%";
      unalignedWrEl.className = unaligned.winRate >= 50 ? "positive" : "";
    }
    const unalignedPlEl = $("unalignedPL");
    if (unalignedPlEl) {
      unalignedPlEl.textContent = formatPct(unaligned.plPct, 2);
      unalignedPlEl.className = getValueClass(unaligned.plPct);
    }
    const unalignedREl = $("unalignedAvgR");
    if (unalignedREl) {
      unalignedREl.textContent = formatR(unaligned.avgR);
      unalignedREl.className = getValueClass(unaligned.avgR);
    }
  }

  function renderTradeLog(data) {
    const trades = data.trades || [];
    const container = $("tradeLog");
    
    if (!container) return;
    
    // Keep header, clear trades
    const header = container.querySelector(".tradeLogHeader");
    container.innerHTML = "";
    if (header) container.appendChild(header);
    
    if (trades.length === 0) {
      container.innerHTML += `<div class="emptyState"><div class="emptyStateTitle">No trades executed</div></div>`;
      setText("tradeLogMeta", "0 trades");
      return;
    }
    
    // Render trade rows
    trades.forEach(trade => {
      const exec = trade.execution || {};
      const perf = trade.performance || {};
      const levels = trade.levels || {};
      const ctx = trade.context || {};
      
      const dirClass = trade.direction === "bullish" ? "bullish" : "bearish";
      const resultClass = perf.isWin ? "win" : "loss";
      
      const gammaBadge = ctx.gammaSupportive
        ? `<span class="contextBadge active" title="Gamma Supportive">G</span>`
        : `<span class="contextBadge inactive" title="Gamma Not Supportive">G</span>`;
      
      const trendBadge = ctx.trendAligned
        ? `<span class="contextBadge active" title="Trend Aligned">T</span>`
        : `<span class="contextBadge inactive" title="Trend Counter">T</span>`;
      
      const row = document.createElement("div");
      row.className = "tradeRow";
      row.innerHTML = `
        <div class="tradeTicker">${escapeHtml(trade.ticker)}</div>
        <div>${escapeHtml(trade.signalDate || "").slice(5)}</div>
        <div><span class="tradeDirection ${dirClass}">${trade.direction === "bullish" ? "Bull" : "Bear"}</span></div>
        <div>${formatPrice(levels.entry)}</div>
        <div>${formatPrice(exec.exitPrice)}</div>
        <div class="tradeResult ${resultClass}">${formatPct(perf.plPct, 2)}</div>
        <div class="tradeResult ${resultClass}">${formatR(perf.rMultiple)}</div>
        <div class="tradeContext">${gammaBadge}${trendBadge}</div>
      `;
      container.appendChild(row);
    });
    
    setText("tradeLogMeta", `${trades.length} trades`);
  }

  function renderResults(data) {
    state.result = data;
    
    // Show results section
    $("results").classList.remove("hidden");
    
    // Render all sections
    renderOverallStats(data);
    renderComparison(data);
    renderTradeLog(data);
  }

  // -----------------------------------------------------------------------------
  // Loading & Status
  // -----------------------------------------------------------------------------

  function setLoading(isLoading, message) {
    state.loading = isLoading;
    
    const runBtn = $("runBtn");
    const btnText = runBtn?.querySelector(".btnText");
    const btnSpinner = runBtn?.querySelector(".btnSpinner");
    
    if (runBtn) runBtn.disabled = isLoading;
    if (btnText) btnText.classList.toggle("hidden", isLoading);
    if (btnSpinner) btnSpinner.classList.toggle("hidden", !isLoading);
    
    if (window.RavenLoading) {
      if (isLoading) {
        window.RavenLoading.show({ 
          status: message || "Running backtest...",
          expectedLoadMs: 120000,  // 2 minutes expected
          clearResults: false,
        });
      } else {
        window.RavenLoading.hide();
      }
    }
  }

  function setStatus(message, type) {
    const statusEl = $("status");
    const section = $("statusSection");
    
    if (statusEl) {
      statusEl.textContent = message;
      statusEl.className = "status";
      if (type === "error") statusEl.classList.add("status--error");
      if (type === "running") statusEl.classList.add("status--running");
    }
    
    if (section) {
      section.classList.toggle("hidden", type === "success");
    }
  }

  // -----------------------------------------------------------------------------
  // Form Handling
  // -----------------------------------------------------------------------------

  async function handleSubmit(ev) {
    ev.preventDefault();
    
    if (state.loading) return;
    
    const engine = $("engineSelect")?.value || "engine3";
    const trades = parseInt($("tradeCount")?.value || "50", 10);
    
    const engineLabel = engine === "engine3" ? "Red Dog" : "Ichimoku";
    
    setLoading(true, `Running ${engineLabel} backtest (${trades} trades)...`);
    setStatus(`Running ${engineLabel} backtest with ${trades} trades...`, "running");
    
    try {
      const data = await runBacktest(engine, trades);
      
      renderResults(data);
      
      const duration = data.meta?.scanDurationMs || 0;
      setStatus(`Backtest complete in ${(duration / 1000).toFixed(1)}s`, "success");
      
    } catch (err) {
      console.error("Backtest error:", err);
      setStatus(`Error: ${err.message}`, "error");
      $("results").classList.add("hidden");
    } finally {
      setLoading(false);
    }
  }

  // -----------------------------------------------------------------------------
  // Initialization
  // -----------------------------------------------------------------------------

  function init() {
    // Form submit
    const form = $("backtestForm");
    if (form) {
      form.addEventListener("submit", handleSubmit);
    }
    
    // Initialize tooltips
    if (window.initTooltips) {
      window.initTooltips();
    }
  }

  // Start on DOM ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
