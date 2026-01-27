/* global window, document */

/**
 * Engine 3: Red Dog Reversal Scanner
 * Client-side JavaScript for the Red Dog Reversal UI
 */

function $(id) { return document.getElementById(id); }

function escapeHtml(s) {
  const t = String(s ?? "");
  return t
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function fmtPct(x, d = 1) {
  const n = Number(x);
  if (!Number.isFinite(n)) return "—";
  return `${n.toFixed(d)}%`;
}

function fmt0(x) {
  const n = Number(x);
  return Number.isFinite(n) ? n.toFixed(0) : "—";
}

function fmt2(x) {
  const n = Number(x);
  return Number.isFinite(n) ? n.toFixed(2) : "—";
}

function fmtMoney(x) {
  const n = Number(x);
  if (!Number.isFinite(n)) return "—";
  return `$${n.toFixed(2)}`;
}

// State
let lastPayload = null;

function setLoading(isLoading) {
  const btn = $("runBtn");
  if (!btn) return;
  btn.disabled = !!isLoading;
  btn.classList.toggle("isLoading", !!isLoading);
  document.body.classList.toggle("isApiLoading", !!isLoading);
}

function setStatus(msg, type = "ok") {
  const el = $("status");
  if (!el) return;
  el.textContent = msg;
  el.className = `status is${type.charAt(0).toUpperCase()}${type.slice(1)}`;
}

function initTooltips() {
  const wraps = Array.from(document.querySelectorAll(".tipWrap"));
  const closeAll = () => {
    wraps.forEach(w => {
      w.classList.remove("isOpen");
      const b = w.querySelector(".tipBtn");
      if (b) b.setAttribute("aria-expanded", "false");
    });
  };

  wraps.forEach((w) => {
    const btn = w.querySelector(".tipBtn");
    if (!btn) return;
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const isOpen = w.classList.contains("isOpen");
      closeAll();
      if (!isOpen) {
        w.classList.add("isOpen");
        btn.setAttribute("aria-expanded", "true");
      }
    });
  });

  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (t && t.closest && t.closest(".tipWrap")) return;
    closeAll();
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeAll();
  });
}

// -----------------------------------------------------------------------------
// API
// -----------------------------------------------------------------------------

async function fetchScan(direction, minScore) {
  const params = new URLSearchParams();
  if (direction) params.set("direction", direction);
  if (minScore !== undefined) params.set("min_score", minScore);
  
  const url = `/api/engine3-red-dog?${params.toString()}`;
  const resp = await fetch(url);
  
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${resp.status}`);
  }
  
  return resp.json();
}

// -----------------------------------------------------------------------------
// Render Functions
// -----------------------------------------------------------------------------

function renderStats(payload) {
  const scanned = payload.scannedCount ?? 0;
  const setups = payload.setupsFound ?? 0;
  const aplus = (payload.aPlus || []).length;
  const duration = payload.meta?.scanDurationMs ?? 0;
  
  $("statScanned").textContent = fmt0(scanned);
  $("statSetups").textContent = fmt0(setups);
  $("statAPlus").textContent = fmt0(aplus);
  $("statDuration").textContent = duration > 0 ? `${(duration / 1000).toFixed(1)}s` : "—";
  
  $("statsMeta").textContent = `As of ${payload.asOfDate || "—"}`;
}

function getGradeClass(grade) {
  switch ((grade || "").toUpperCase()) {
    case "A+": return "grade-aplus";
    case "A": return "grade-a";
    case "B": return "grade-b";
    default: return "grade-c";
  }
}

function renderSignalCard(signal) {
  const ticker = escapeHtml(signal.ticker || "???");
  const direction = signal.direction || "?";
  const dirClass = direction === "bullish" ? "bullish" : "bearish";
  const grade = signal.quality?.grade || "?";
  const score = signal.quality?.score ?? 0;
  const gradeClass = getGradeClass(grade);
  
  const entry = signal.levels?.entryTrigger;
  const stop = signal.levels?.stopLoss;
  const t1 = signal.levels?.target1;
  const risk = signal.levels?.riskDollars;
  
  const rsi = signal.indicators?.rsi;
  const stoch = signal.indicators?.stochastics;
  const volRatio = signal.indicators?.volumeRatio;
  const sma20Dev = signal.indicators?.sma20DeviationPct;
  
  // Build indicator chips
  const chips = [];
  
  // RSI chip
  const rsiActive = (direction === "bullish" && rsi <= 30) || (direction === "bearish" && rsi >= 70);
  chips.push(`<span class="indicatorChip ${rsiActive ? 'active' : 'inactive'}">RSI ${fmt0(rsi)}</span>`);
  
  // Stochastics chip
  const stochActive = (direction === "bullish" && stoch <= 20) || (direction === "bearish" && stoch >= 80);
  chips.push(`<span class="indicatorChip ${stochActive ? 'active' : 'inactive'}">Stoch ${fmt0(stoch)}</span>`);
  
  // Volume chip
  const volActive = volRatio >= 1.5;
  chips.push(`<span class="indicatorChip ${volActive ? 'active' : 'inactive'}">Vol ${fmt2(volRatio)}x</span>`);
  
  // Notes
  const notes = (signal.notes || []).slice(0, 2).map(n => escapeHtml(n)).join(" · ");
  
  return `
    <div class="signalCard" data-ticker="${ticker}">
      <div class="signalCardHeader">
        <div class="signalCardTicker">
          <span class="signalCardSymbol">${ticker}</span>
          <span class="signalCardDirection ${dirClass}">${direction}</span>
        </div>
        <span class="signalCardGrade ${gradeClass}">${grade} (${score})</span>
      </div>
      <div class="signalCardBody">
        <div class="signalCardMetric">
          <span class="k">Entry</span>
          <span class="v">${fmtMoney(entry)}</span>
        </div>
        <div class="signalCardMetric">
          <span class="k">Stop</span>
          <span class="v">${fmtMoney(stop)}</span>
        </div>
        <div class="signalCardMetric">
          <span class="k">Target 1</span>
          <span class="v">${fmtMoney(t1)}</span>
        </div>
        <div class="signalCardMetric">
          <span class="k">Risk</span>
          <span class="v">${fmtMoney(risk)}</span>
        </div>
      </div>
      <div class="signalCardIndicators">
        ${chips.join("")}
      </div>
      ${notes ? `<div class="signalCardNotes">${notes}</div>` : ""}
    </div>
  `;
}

function renderEmptyState(message) {
  return `
    <div class="emptyState">
      <div class="emptyStateTitle">No setups found</div>
      <div class="emptyStateBody">${escapeHtml(message)}</div>
    </div>
  `;
}

function renderWatchlist(containerId, signals, metaId, label) {
  const container = $(containerId);
  const meta = $(metaId);
  
  if (!container) return;
  
  if (!signals || signals.length === 0) {
    container.innerHTML = renderEmptyState(`No ${label} setups detected in the current scan.`);
    if (meta) meta.textContent = "0 setups";
    return;
  }
  
  container.innerHTML = signals.map(s => renderSignalCard(s)).join("");
  if (meta) meta.textContent = `${signals.length} setup${signals.length !== 1 ? "s" : ""}`;
  
  // Add click handlers for detail view
  container.querySelectorAll(".signalCard").forEach(card => {
    card.addEventListener("click", () => {
      const ticker = card.dataset.ticker;
      if (ticker) {
        // Open Engine 1 for detailed analysis
        window.open(`/breach?ticker=${encodeURIComponent(ticker)}`, "_blank");
      }
    });
  });
}

function renderResults(payload) {
  lastPayload = payload;
  
  // Show results section
  $("results").classList.remove("hidden");
  
  // Render stats
  renderStats(payload);
  
  // Render A+ watchlist
  renderWatchlist("aplusGrid", payload.aPlus, "aplusMeta", "A+");
  
  // Render standard setups
  renderWatchlist("standardGrid", payload.standard, "standardMeta", "standard");
}

// -----------------------------------------------------------------------------
// Form Handling
// -----------------------------------------------------------------------------

async function handleSubmit(ev) {
  ev.preventDefault();
  
  const direction = $("direction")?.value || "";
  const minScore = parseInt($("minScore")?.value || "50", 10);
  
  setLoading(true);
  setStatus("Scanning SP100 + Nasdaq100 for Red Dog setups...", "running");
  
  try {
    const payload = await fetchScan(direction, minScore);
    renderResults(payload);
    
    const count = payload.setupsFound || 0;
    const aplusCount = (payload.aPlus || []).length;
    
    if (count === 0) {
      setStatus("Scan complete. No Red Dog setups found matching your filters.", "ok");
    } else {
      setStatus(`Scan complete. Found ${count} setup${count !== 1 ? "s" : ""} (${aplusCount} A+).`, "ok");
    }
  } catch (err) {
    console.error("Scan error:", err);
    setStatus(`Error: ${err.message}`, "error");
    $("results").classList.add("hidden");
  } finally {
    setLoading(false);
  }
}

// -----------------------------------------------------------------------------
// Init
// -----------------------------------------------------------------------------

function init() {
  initTooltips();
  
  const form = $("e3Form");
  if (form) {
    form.addEventListener("submit", handleSubmit);
  }
  
  // Check if Engine 2 should be visible (same logic as other pages)
  // Engine 2 is always visible now, so we just ensure the link is there
  const e2Link = $("engine2Link");
  if (e2Link) {
    e2Link.classList.remove("hidden");
  }
}

document.addEventListener("DOMContentLoaded", init);
