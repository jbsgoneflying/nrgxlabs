/* global window, document */

/**
 * Engine 3: Red Dog Reversal Scanner
 * Client-side JavaScript for the Red Dog Reversal UI
 */

function fmtMoney(x) {
  const n = Number(x);
  if (!Number.isFinite(n)) return "—";
  return `$${n.toFixed(2)}`;
}

// State
let lastPayload = null;
let lastTrackerSignals = null;  // last desk-tracker payload, for row-level insight lookups

function setLoading(isLoading, statusMsg) {
  const btn = $("runBtn");
  if (!btn) return;
  btn.disabled = !!isLoading;
  btn.classList.toggle("isLoading", !!isLoading);
  document.body.classList.toggle("isApiLoading", !!isLoading);
  
  // Raven Loading Overlay
  if (window.RavenLoading) {
    if (isLoading) {
      window.RavenLoading.show({ status: statusMsg || "Scanning universe..." });
    } else {
      window.RavenLoading.hide();
    }
  }
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
  
  setText("statScanned", fmt0(scanned));
  setText("statSetups", fmt0(setups));
  setText("statAPlus", fmt0(aplus));
  setText("statDuration", duration > 0 ? `${(duration / 1000).toFixed(1)}s` : "—");
  
  setText("statsMeta", `As of ${payload.asOfDate || "—"}`);
}

function renderGammaContext(payload) {
  const gamma = payload.marketGamma || {};
  const available = gamma.available !== false;
  
  // Update meta with data source
  const expiry = gamma.expiry ? `SPX expiry ${gamma.expiry}` : "SPX";
  const spot = gamma.spot ? ` · Spot ${fmt0(gamma.spot)}` : "";
  const dataSource = gamma.dataSource;
  let sourceLabel = "";
  if (dataSource && dataSource.startsWith("eod:")) {
    const eodDate = dataSource.split(":")[1];
    sourceLabel = ` · EOD ${eodDate}`;
  } else if (dataSource === "live") {
    sourceLabel = " · Live";
  }
  setText("gammaMeta", available ? `${expiry}${spot}${sourceLabel}` : "Unavailable");
  
  // Gamma Sign
  const sign = gamma.netGammaSign || "unknown";
  if (sign === "positive") {
    setHtml("gammaSignValue", `<span class="gammaPositive">POSITIVE ✓</span>`);
    setText("gammaSignNote", "Dealers are long gamma — they buy dips, sell rips.");
  } else if (sign === "negative") {
    setHtml("gammaSignValue", `<span class="gammaNegative">NEGATIVE ⚠</span>`);
    setText("gammaSignNote", "Dealers are short gamma — they sell dips, buy rips.");
  } else {
    setText("gammaSignValue", "—");
    setText("gammaSignNote", "Unable to determine dealer positioning.");
  }
  
  // Environment
  const env = gamma.environment || "unknown";
  if (env === "supportive") {
    setHtml("gammaEnvValue", `<span class="gammaEnvSupportive">Supportive ✓</span>`);
    setText("gammaEnvNote", "Mean reversion patterns have dealer flow as a tailwind.");
  } else if (env === "challenging") {
    setHtml("gammaEnvValue", `<span class="gammaEnvChallenging">Challenging ⚠</span>`);
    setText("gammaEnvNote", "Momentum can accelerate — be more selective.");
  } else {
    setHtml("gammaEnvValue", `<span class="gammaEnvUnknown">Unknown</span>`);
    setText("gammaEnvNote", "Gamma context unavailable.");
  }
  
  // Recommendation
  const rec = gamma.recommendation || "Proceed based on pattern quality alone.";
  setText("gammaRecValue", rec);
  
  // Note with explanation
  const explanation = gamma.explanation || "";
  setText("gammaRecNote", explanation ? `Why: ${explanation.slice(0, 200)}${explanation.length > 200 ? '...' : ''}` : "");
}

function renderTrendContext(payload) {
  const trend = payload.marketTrend || {};
  const available = trend.available !== false;
  
  // Update meta with data source
  const price = trend.currentPrice ? `SPY ${fmt2(trend.currentPrice)}` : "";
  const ema = trend.ema21 ? ` · 21 EMA ${fmt2(trend.ema21)}` : "";
  const dataSource = trend.dataSource;
  const dataDate = trend.dataAsOfDate || trend.asOfDate;
  let sourceLabel = "";
  if (dataSource && dataSource.startsWith("eod:")) {
    sourceLabel = ` · EOD ${dataDate}`;
  } else {
    sourceLabel = dataDate ? ` · ${dataDate}` : "";
  }
  setText("trendMeta", available ? `${price}${ema}${sourceLabel}` : "Unavailable");
  
  // Trend Status (above/below EMA)
  const aboveEma = trend.aboveEma;
  const distPct = trend.distancePct || 0;
  
  if (aboveEma === true) {
    setHtml("trendStatusValue", `<span class="trendAbove">ABOVE +${Math.abs(distPct).toFixed(1)}%</span>`);
    setText("trendStatusNote", "SPX is in an uptrend (above 21 EMA).");
  } else if (aboveEma === false) {
    setHtml("trendStatusValue", `<span class="trendBelow">BELOW −${Math.abs(distPct).toFixed(1)}%</span>`);
    setText("trendStatusNote", "SPX is in a downtrend (below 21 EMA).");
  } else {
    setText("trendStatusValue", "—");
    setText("trendStatusNote", "Unable to determine trend status.");
  }
  
  // Favored Direction
  const trendDir = trend.trendDirection || "unknown";
  
  if (trendDir === "bullish") {
    setHtml("trendFavorValue", `<span class="favorBullish">BULLISH ↑</span>`);
    setText("trendFavorNote", "Failed breakdowns (bullish setups) trade WITH the trend.");
  } else if (trendDir === "bearish") {
    setHtml("trendFavorValue", `<span class="favorBearish">BEARISH ↓</span>`);
    setText("trendFavorNote", "Failed breakouts (bearish setups) trade WITH the trend.");
  } else {
    setHtml("trendFavorValue", `<span class="gammaEnvUnknown">Unknown</span>`);
    setText("trendFavorNote", "Trend direction unavailable.");
  }
  
  // Trend Recommendation
  const rec = trend.recommendation || "Trend filter unavailable. Use pattern quality for decisions.";
  setText("trendRecValue", rec);
  
  // Note
  const explanation = trend.explanation || "";
  setText("trendRecNote", explanation ? explanation.slice(0, 250) + (explanation.length > 250 ? '...' : '') : "");
}

function getGradeClass(grade) {
  switch ((grade || "").toUpperCase()) {
    case "A+": return "grade-aplus";
    case "A": return "grade-a";
    case "B": return "grade-b";
    default: return "grade-c";
  }
}

function renderSignalCard(signal, isAPlus = false) {
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
  const dollarAdv = signal.indicators?.dollarAdv;
  const advTxt = (dollarAdv && Number.isFinite(Number(dollarAdv))) ? `$${(Number(dollarAdv) / 1e6).toFixed(0)}M` : "—";
  const status = signal.status || "pending";
  const isTracked = ["watching", "entered", "working", "broken", "exited"].includes(status);
  
  // Trend alignment
  const trendAlign = signal.trendAlignment || {};
  const alignClass = trendAlign.alignment === "aligned" ? "aligned" : 
                     trendAlign.alignment === "counter" ? "counter" : "unknown";
  const alignLabel = trendAlign.label || "Trend N/A";
  const alignGuidance = trendAlign.guidance || "";
  
  // Build freshness-style badges for A+ cards
  let freshnessHtml = "";
  if (isAPlus) {
    // RSI badge
    const rsiActive = (direction === "bullish" && rsi <= 35) || (direction === "bearish" && rsi >= 65);
    if (rsiActive) {
      freshnessHtml += `<span class="freshBadge positive">RSI ${fmt0(rsi)}</span>`;
    }
    // Stoch badge
    const stochActive = (direction === "bullish" && stoch <= 25) || (direction === "bearish" && stoch >= 75);
    if (stochActive) {
      freshnessHtml += `<span class="freshBadge positive">Stoch ${fmt0(stoch)}</span>`;
    }
    // Volume badge
    if (volRatio >= 1.5) {
      freshnessHtml += `<span class="freshBadge positive">Vol ${fmt2(volRatio)}x</span>`;
    }
    // Trend alignment badge
    if (alignClass === "aligned") {
      freshnessHtml += `<span class="freshBadge positive">${alignLabel}</span>`;
    } else if (alignClass === "counter") {
      freshnessHtml += `<span class="freshBadge warning">${alignLabel}</span>`;
    }
  }
  
  // Build indicator chips for non-A+ cards
  let chipsHtml = "";
  if (!isAPlus) {
    const chips = [];
    chips.push(`<span class="trendAlignBadge ${alignClass}" title="${escapeHtml(alignGuidance)}">${alignLabel}</span>`);
    const rsiActive = (direction === "bullish" && rsi <= 30) || (direction === "bearish" && rsi >= 70);
    chips.push(`<span class="indicatorChip ${rsiActive ? 'active' : 'inactive'}">RSI ${fmt0(rsi)}</span>`);
    const stochActive = (direction === "bullish" && stoch <= 20) || (direction === "bearish" && stoch >= 80);
    chips.push(`<span class="indicatorChip ${stochActive ? 'active' : 'inactive'}">Stoch ${fmt0(stoch)}</span>`);
    const volActive = volRatio >= 1.5;
    chips.push(`<span class="indicatorChip ${volActive ? 'active' : 'inactive'}">Vol ${fmt2(volRatio)}x</span>`);
    chipsHtml = `<div class="signalCardIndicators">${chips.join("")}</div>`;
  }
  
  // Card class - A+ gets green border, standard gets amber
  const cardClass = isAPlus ? "signalCard actionableCard" : "signalCard structureCard";
  
  // Reconciled desk verdict — LEAD with this (grade + gate + gamma + trend).
  let verdictPillHtml = "";
  const verdict = signal.verdict || {};
  if (verdict.status) {
    const vCls = verdict.status === "TRADABLE" ? "background:rgba(52,199,89,0.18);color:#1b8a3e;" :
                 verdict.status === "STAND_DOWN" ? "background:rgba(255,59,48,0.16);color:#cc2f26;" :
                 "background:rgba(255,149,0,0.16);color:#995c00;";
    const drivers = (verdict.drivers || []).slice(0, 2).join(" · ");
    verdictPillHtml = `<div style="margin:2px 0 4px;"><span style="display:inline-block;font-size:10px;font-weight:900;padding:3px 10px;border-radius:12px;text-transform:uppercase;letter-spacing:0.04em;${vCls}">${escapeHtml(verdict.label || verdict.status)}</span>${drivers ? `<span style="font-size:10px;color:var(--muted);margin-left:6px;">${escapeHtml(drivers)}</span>` : ""}</div>`;
  }

  // Score detail: show base → trend-adjusted when penalized.
  const baseScore = signal.quality?.baseScore;
  const penalty = signal.quality?.trendPenalty || 0;
  const confirmed = signal.quality?.confirmed !== false;
  let scoreDetailHtml = "";
  if (penalty > 0.5) {
    scoreDetailHtml = `<span style="font-size:10px;color:var(--muted);">base ${fmt0(baseScore)} − ${fmt0(penalty)} counter-trend</span>`;
  } else if (!confirmed) {
    scoreDetailHtml = `<span style="font-size:10px;color:#995c00;">unconfirmed reversal</span>`;
  }

  // Gate pill (secondary to verdict now)
  let gatePillHtml = "";
  const gate = signal.gate || {};
  if (gate.status) {
    const gCls = gate.status === "TRADABLE" ? "color:#1b8a3e;" :
                 gate.status === "SUPPRESS" ? "color:#cc2f26;" : "color:#995c00;";
    gatePillHtml = `<span style="font-size:9px;font-weight:700;${gCls}">gate: ${gate.status.toLowerCase()}</span>`;
  }

  return `
    <div class="${cardClass}" data-ticker="${ticker}">
      <div class="signalCardHeader">
        <div class="signalCardTicker">
          <span class="signalCardSymbol">${ticker}</span>
          <span class="signalCardDirection ${dirClass}">${direction}</span>
        </div>
        <span class="signalCardGrade ${gradeClass}">${grade} (${fmt0(score)})</span>
      </div>
      ${verdictPillHtml}
      <div style="margin:0 0 4px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">${scoreDetailHtml}${gatePillHtml}</div>
      ${freshnessHtml ? `<div class="signalCardFreshness">${freshnessHtml}</div>` : ""}
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
        <div class="signalCardMetric">
          <span class="k">RSI</span>
          <span class="v">${fmt0(rsi)}</span>
        </div>
        <div class="signalCardMetric">
          <span class="k">Vol Ratio</span>
          <span class="v">${fmt2(volRatio)}x</span>
        </div>
        <div class="signalCardMetric">
          <span class="k">$ ADV</span>
          <span class="v">${advTxt}</span>
        </div>
      </div>
      ${chipsHtml}
      <div class="signalCardActions">
        <button type="button" class="rdCardBtn rdInsightBtn" data-ticker="${ticker}">Insight</button>
        <button type="button" class="rdCardBtn rdTrackBtn ${isTracked ? 'isTracked' : ''}" data-ticker="${ticker}" data-act="watching">${isTracked ? escapeHtml(status) : 'Watch'}</button>
      </div>
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

function renderWatchlist(containerId, signals, metaId, label, isAPlus = false) {
  const container = $(containerId);
  const meta = $(metaId);
  
  if (!container) return;
  
  if (!signals || signals.length === 0) {
    container.innerHTML = renderEmptyState(`No ${label} setups detected in the current scan.`);
    if (meta) meta.textContent = "0 setups";
    return;
  }
  
  container.innerHTML = signals.map(s => renderSignalCard(s, isAPlus)).join("");
  if (meta) meta.textContent = `${signals.length} setup${signals.length !== 1 ? "s" : ""}`;
  // Card clicks (Position Sizer + Insight) are handled by the single
  // delegated onCardClick listener on the grids — see the insight IIFE below.
  // No per-card listener here, so the two popups never double-open.
}

function renderGateBanner(payload) {
  const banner = $("gateBanner");
  if (!banner) return;

  const gateSummary = payload.gateSummary;
  if (!gateSummary) { banner.style.display = "none"; return; }

  banner.style.display = "block";
  const total = gateSummary.total || 0;
  const tradable = gateSummary.TRADABLE || 0;
  const watch = gateSummary.WATCH || 0;
  const suppress = gateSummary.SUPPRESS || 0;

  const pillStyle = (cls, text) =>
    `<span style="display:inline-block;font-size:10px;font-weight:800;padding:3px 10px;border-radius:20px;text-transform:uppercase;letter-spacing:0.04em;${cls}">${text}</span>`;

  const summaryEl = $("gateSummary");
  if (summaryEl) {
    summaryEl.innerHTML = [
      tradable > 0 ? pillStyle("background:rgba(52,199,89,0.14);color:#1b8a3e;", `${tradable} Tradable`) : "",
      watch > 0 ? pillStyle("background:rgba(255,149,0,0.14);color:#995c00;", `${watch} Watch`) : "",
      suppress > 0 ? pillStyle("background:rgba(255,59,48,0.14);color:#cc2f26;", `${suppress} Suppress`) : "",
      pillStyle("background:rgba(11,11,15,0.04);color:var(--muted);", `${total} Total`),
    ].filter(Boolean).join(" ");
  }

  // Show regime/vol context if available
  const reasonsEl = $("gateReasons");
  if (reasonsEl && payload.gateContext) {
    const ctx = payload.gateContext;
    const parts = [];
    if (ctx.regime_label) parts.push(`Regime: ${ctx.regime_label}`);
    if (ctx.vol_direction) parts.push(`Vol: ${ctx.vol_direction}`);
    reasonsEl.textContent = parts.join(" · ") || "";
  }
}

function renderVerdictBanner(payload) {
  const banner = $("verdictBanner");
  if (!banner) return;
  const vs = payload.verdictSummary;
  if (!vs) { banner.style.display = "none"; return; }
  banner.style.display = "block";

  const tradable = vs.TRADABLE || 0;
  const watch = vs.WATCH || 0;
  const stand = vs.STAND_DOWN || 0;
  const total = vs.total || 0;

  const pill = (cls, text) =>
    `<span style="display:inline-block;font-size:10px;font-weight:800;padding:3px 10px;border-radius:20px;text-transform:uppercase;letter-spacing:0.04em;${cls}">${text}</span>`;

  const el = $("verdictSummary");
  if (el) {
    el.innerHTML = [
      tradable > 0 ? pill("background:rgba(52,199,89,0.16);color:#1b8a3e;", `${tradable} Tradable`) : "",
      watch > 0 ? pill("background:rgba(255,149,0,0.16);color:#995c00;", `${watch} Watch`) : "",
      stand > 0 ? pill("background:rgba(255,59,48,0.16);color:#cc2f26;", `${stand} Stand Down`) : "",
      pill("background:rgba(11,11,15,0.04);color:var(--muted);", `${total} Total`),
    ].filter(Boolean).join(" ");
  }
}

function renderResults(payload) {
  lastPayload = payload;
  
  // Show results section
  $("results").classList.remove("hidden");

  // Render reconciled desk verdict (leads the gate)
  renderVerdictBanner(payload);
  
  // Render gate banner (NRGX Labs 2.0)
  renderGateBanner(payload);
  
  // Render stats
  renderStats(payload);
  
  // Render gamma context
  renderGammaContext(payload);
  
  // Render trend context (21 EMA)
  renderTrendContext(payload);
  
  // Render A+ watchlist (with green border styling)
  renderWatchlist("aplusGrid", payload.aPlus, "aplusMeta", "A+", true);
  
  // Render standard setups (with amber border styling)
  renderWatchlist("standardGrid", payload.standard, "standardMeta", "standard", false);
}

// -----------------------------------------------------------------------------
// Form Handling
// -----------------------------------------------------------------------------

async function handleSubmit(ev) {
  ev.preventDefault();
  
  const direction = $("direction")?.value || "";
  const minScore = parseInt($("minScore")?.value || "50", 10);
  
  setLoading(true, "Scanning SP500 + Nasdaq100...");
  setStatus("Scanning SP500 + Nasdaq100 (516 tickers) for Red Dog setups...", "running");
  
  // Progress updates
  if (window.RavenLoading) {
    window.RavenLoading.setProgress(10, "Scanning 516 tickers...");
  }
  
  try {
    const payload = await fetchScan(direction, minScore);
    
    if (window.RavenLoading) {
      window.RavenLoading.setProgress(75, "Processing setups...");
    }
    
    renderResults(payload);
    
    if (window.RavenLoading) {
      window.RavenLoading.setProgress(95, "Rendering results...");
    }
    
    const count = payload.setupsFound || 0;
    const aplusCount = (payload.aPlus || []).length;
    
    if (count === 0) {
      setStatus("Scan complete. No Red Dog setups found matching your filters.", "ok");
    } else {
      setStatus(`Scan complete. Found ${count} setup${count !== 1 ? "s" : ""} (${aplusCount} A+).`, "ok");
    }

    // Newly scanned signals are persisted server-side; refresh the tracker view.
    loadTracker(false);
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

// -----------------------------------------------------------------------------
// Signal Tracker (live outcomes)
// -----------------------------------------------------------------------------

function metricCard(label, value, caption) {
  return `<div class="metricCard"><div class="metricLabel">${label}</div>` +
    `<div class="metricValue mono">${value}</div>` +
    `<div class="metricCaption muted">${caption || ""}</div></div>`;
}

const TRACKER_STATUSES = ["watching", "entered", "working", "broken", "exited"];

function trkPillStyle(status) {
  const map = {
    pending: "background:rgba(11,11,15,0.06);color:var(--muted);",
    triggered: "background:rgba(0,122,255,0.14);color:#0a62c9;",
    target_hit: "background:rgba(52,199,89,0.16);color:#1b8a3e;",
    stopped: "background:rgba(255,59,48,0.14);color:#cc2f26;",
    expired: "background:rgba(11,11,15,0.06);color:var(--muted);",
    invalidated: "background:rgba(255,59,48,0.10);color:#cc2f26;",
    watching: "background:rgba(255,149,0,0.16);color:#995c00;",
    entered: "background:rgba(0,122,255,0.16);color:#0a62c9;",
    working: "background:rgba(0,122,255,0.10);color:#0a62c9;",
    broken: "background:rgba(255,59,48,0.16);color:#cc2f26;",
    exited: "background:rgba(11,11,15,0.08);color:var(--text);",
  };
  return map[status] || map.pending;
}

async function deskTrack(ticker, status, signalDate, note) {
  try {
    const resp = await fetch("/api/engine3-red-dog/track", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker, status, signalDate, note }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const data = await resp.json();
    renderTracker(data.signals);
    const removed = ["untrack", "remove", "clear"].includes(status);
    try {
      const sel = `.signalCard[data-ticker="${(window.CSS && CSS.escape) ? CSS.escape(ticker) : ticker}"] .rdTrackBtn`;
      document.querySelectorAll(sel).forEach(b => { b.textContent = removed ? "Watch" : status; b.classList.toggle("isTracked", !removed); });
    } catch (_) { /* best effort */ }
    setStatus(removed ? `${ticker} removed from desk book.` : `${ticker} marked "${status}".`, "ok");
  } catch (e) {
    setStatus(`Tracker error: ${e.message}`, "error");
  }
}

function renderTracker(s) {
  const summary = $("trackerSummary");
  const body = $("trackerBody");
  if (!s) return;
  lastTrackerSignals = s;
  const c = s.counts || {};

  if (summary) {
    summary.innerHTML = [
      metricCard("Tracked", fmt0(s.totalSignals || 0), "signals"),
      metricCard("Desk Book", fmt0(s.deskBookCount || 0), "actively managed"),
      metricCard("Win Rate", s.winRate != null ? `${s.winRate}%` : "—", `${s.resolvedCount || 0} resolved`),
      metricCard("Open", fmt0((c.pending || 0) + (c.triggered || 0)), "pending + triggered"),
    ].join("");
  }

  if (!body) return;

  // Desk book first (trader-managed), then live auto-tracked lifecycle.
  const deskBook = [].concat(s.watching || [], s.entered || [], s.working || [], s.broken || [], s.exited || []);
  const live = [].concat(s.triggered || [], s.pending || []);
  const rows = deskBook.concat(live).slice(0, 40);

  if (!rows.length) {
    body.innerHTML = '<span class="muted" style="font-size:12px;">No tracked signals yet. Click <b>Watch</b> on a card to start a desk book.</span>';
    return;
  }

  body.innerHTML = rows.map(r => {
    const t = escapeHtml(r.ticker || "");
    const sd = escapeHtml(r.signalDate || "");
    const st = r.status || "pending";
    const dir = escapeHtml(r.direction || "");
    const opts = TRACKER_STATUSES.map(x => `<option value="${x}" ${x === st ? "selected" : ""}>${x}</option>`).join("");
    return `
      <div class="trkRow" data-ticker="${t}" data-date="${sd}">
        <span class="trkSym">${t}</span>
        <span class="muted" style="font-size:11px;">${dir} · ${sd}</span>
        <span class="trkPill" style="${trkPillStyle(st)}">${st.replace('_', ' ')}</span>
        <select class="trkSelect" data-ticker="${t}" data-date="${sd}">
          <option value="">advance…</option>
          ${opts}
        </select>
        <button type="button" class="trkInsight" data-ticker="${t}" data-date="${sd}" title="Desk insight on this trade" style="font-size:11px;font-weight:700;padding:3px 8px;border-radius:6px;border:1px solid rgba(0,122,255,.25);background:rgba(0,122,255,.06);color:#0a62c9;cursor:pointer;">Insight</button>
        <button type="button" class="trkRemove" data-ticker="${t}" data-date="${sd}" title="Remove from desk book" style="border:none;background:none;color:var(--muted);cursor:pointer;font-size:13px;line-height:1;">✕</button>
      </div>`;
  }).join("");

  body.querySelectorAll(".trkSelect").forEach(sel => {
    sel.addEventListener("change", (ev) => {
      const v = ev.target.value;
      if (!v) return;
      deskTrack(ev.target.getAttribute("data-ticker"), v, ev.target.getAttribute("data-date"));
    });
  });
  body.querySelectorAll(".trkRemove").forEach(b => {
    b.addEventListener("click", (ev) => {
      deskTrack(ev.target.getAttribute("data-ticker"), "untrack", ev.target.getAttribute("data-date"));
    });
  });
}

async function loadTracker(refresh) {
  const btn = $("trackerRefreshBtn");
  if (btn && refresh) { btn.disabled = true; btn.textContent = "Evaluating…"; }
  try {
    const resp = await fetch(`/api/engine3-red-dog/status${refresh ? "?refresh=true" : ""}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    renderTracker(data);
  } catch (err) {
    const body = $("trackerBody");
    if (body) body.innerHTML = `<span class="muted">Tracker error: ${escapeHtml(err.message)}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Load / Refresh"; }
  }
}

// -----------------------------------------------------------------------------
// Backtest
// -----------------------------------------------------------------------------

function renderBacktestGroup(title, stats) {
  if (!stats || !stats.signals) return "";
  const wr = stats.winRate != null ? `${stats.winRate}%` : "—";
  const exp = stats.expectancy != null ? `${stats.expectancy}R` : "—";
  return `<tr><td style="padding:4px 8px;font-weight:700;">${escapeHtml(title)}</td>` +
    `<td style="padding:4px 8px;text-align:right;">${fmt0(stats.signals)}</td>` +
    `<td style="padding:4px 8px;text-align:right;">${fmt0(stats.triggered)}</td>` +
    `<td style="padding:4px 8px;text-align:right;">${wr}</td>` +
    `<td style="padding:4px 8px;text-align:right;">${exp}</td>` +
    `<td style="padding:4px 8px;text-align:right;">${stats.avgMae != null ? stats.avgMae : "—"}</td>` +
    `<td style="padding:4px 8px;text-align:right;">${stats.avgMfe != null ? stats.avgMfe : "—"}</td></tr>`;
}

async function runBacktest() {
  const btn = $("backtestRunBtn");
  const body = $("backtestBody");
  const years = $("backtestYears")?.value || "3";
  const minScore = $("backtestMinScore")?.value ?? "60";
  if (btn) { btn.disabled = true; btn.textContent = "Running…"; }
  if (body) body.innerHTML = `<span class="muted">Replaying ${years}y of history across the universe sample… this can take 20-40s.</span>`;
  try {
    const resp = await fetch(`/api/engine3-red-dog/backtest?years=${years}&min_score=${minScore}&max_tickers=60`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const o = data.overall || {};
    const summary = $("backtestSummary");
    if (summary) {
      summary.innerHTML = [
        metricCard("Signals", fmt0(o.signals || 0), `${o.triggered || 0} triggered`),
        metricCard("Win Rate", o.winRate != null ? `${o.winRate}%` : "—", "target vs stop"),
        metricCard("Expectancy", o.expectancy != null ? `${o.expectancy}R` : "—", "per triggered"),
        metricCard("Avg MFE", o.avgMfe != null ? `${o.avgMfe}R` : "—", `MAE ${o.avgMae != null ? o.avgMae : "—"}R`),
      ].join("");
    }
    if (body) {
      const rows = [];
      const bg = data.byGrade || {};
      ["A+", "A", "B", "C"].forEach(g => { if (bg[g]) rows.push(renderBacktestGroup(`Grade ${g}`, bg[g])); });
      const ba = data.byTrendAlignment || {};
      ["aligned", "counter", "neutral", "unknown"].forEach(a => { if (ba[a]) rows.push(renderBacktestGroup(a, ba[a])); });
      const win = data.window || {};
      body.innerHTML =
        `<div style="font-size:11px;overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:11px;">` +
        `<thead><tr style="color:var(--muted);text-align:right;">` +
        `<th style="padding:4px 8px;text-align:left;">Bucket</th><th style="padding:4px 8px;">Signals</th>` +
        `<th style="padding:4px 8px;">Triggered</th><th style="padding:4px 8px;">Win%</th>` +
        `<th style="padding:4px 8px;">Exp(R)</th><th style="padding:4px 8px;">MAE</th><th style="padding:4px 8px;">MFE</th></tr></thead>` +
        `<tbody>${rows.join("")}</tbody></table>` +
        `<div class="muted" style="margin-top:6px;">Window: ${win.start || "?"} → ${win.end || "?"} · ${data.params?.tickersTested || 0} names sampled · min score ${data.params?.minScore ?? minScore}. ` +
        `Compare grade tiers and (critically) <b>aligned vs counter</b> expectancy.</div></div>`;
    }
  } catch (err) {
    if (body) body.innerHTML = `<span class="muted">Backtest error: ${escapeHtml(err.message)}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Run Backtest"; }
  }
}

function init() {
  initTooltips();
  
  const form = $("e3Form");
  if (form) {
    form.addEventListener("submit", handleSubmit);
  }

  const trackerBtn = $("trackerRefreshBtn");
  if (trackerBtn) trackerBtn.addEventListener("click", () => loadTracker(true));
  const backtestBtn = $("backtestRunBtn");
  if (backtestBtn) backtestBtn.addEventListener("click", runBacktest);

  // Prime the desk tracker (cheap read, no refresh).
  loadTracker(false);
  
  // Check if Engine 2 should be visible (same logic as other pages)
  // Engine 2 is always visible now, so we just ensure the link is there
  const e2Link = $("engine2Link");
  if (e2Link) {
    e2Link.classList.remove("hidden");
  }
}

document.addEventListener("DOMContentLoaded", init);

// ---------------------------------------------------------------------------
// Desk Insight Popup — LLM-powered card insights for Red Dog
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  var popup = $("rdInsightPopup");
  if (!popup) return;

  initDrag(popup, $("rdInsightHeader"), { closeSelector: "#rdInsightClose" });
  $("rdInsightClose").addEventListener("click", function () { popup.style.display = "none"; });

  var rdInsight = new InsightPopup({
    popupEl: popup,
    titleEl: $("rdInsightTitle"),
    bodyEl:  $("rdInsightBody"),
    prefix:  "rdInsight",
    labels: {
      setup_quality:"Setup Quality",entry_mechanics:"Entry Mechanics",indicator_confluence:"Indicator Confluence",alignment_check:"Alignment Check",
      gamma_environment:"Gamma Environment",directional_bias:"Directional Bias",mean_reversion_impact:"Mean-Reversion Impact",
      trend_read:"Trend Read",alignment_value:"Alignment Value",distance_context:"Distance Context",
      scan_read:"Scan Read",aplus_concentration:"A+ Concentration",directional_skew:"Directional Skew",
      gate_status:"Gate Status",regime_impact:"Regime Impact",vol_and_flow:"Vol & Flow",
      desk_takeaway:"Desk Takeaway",
    },
  });

  function fetchInsight(cardType, cardData, title, x, y) {
    var ctx = {};
    if (lastPayload) { ctx.marketGamma = lastPayload.marketGamma || {}; ctx.marketTrend = lastPayload.marketTrend || {}; ctx.asOfDate = lastPayload.asOfDate; }
    rdInsight.fetch(cardType, cardData, title, x, y, ctx);
  }

  // ── Signal cards (A+ and Standard) ──
  var aplusGrid = $("aplusGrid");
  var standardGrid = $("standardGrid");
  // Single click model — deconflicts the two popups (same as Ichimoku):
  //   • "Insight" button → LLM popup, docked to the RIGHT edge
  //   • card body        → Position Sizer, near the click
  // No more double-open: the body no longer also fires the insight popup.
  function onCardClick(ev) {
    var card = ev.target.closest(".signalCard");
    if (!card || !lastPayload) return;
    var ticker = card.getAttribute("data-ticker");
    var allSignals = [].concat(lastPayload.aPlus || [], lastPayload.standard || []);
    var sig = allSignals.find(function(s) { return s.ticker === ticker; });
    if (!sig) return;

    // Dedicated insight affordance → LLM, docked right so it never overlaps the sizer.
    if (ev.target.closest(".rdInsightBtn")) {
      ev.stopPropagation();
      var ix = Math.max(20, window.innerWidth - 470);
      fetchInsight("rd_signal", sig, "Red Dog: " + ticker + " (" + (sig.direction || "") + ")", ix, 96);
      return;
    }
    // Desk tracker affordance → mark watching (no popup).
    if (ev.target.closest(".rdTrackBtn")) {
      ev.stopPropagation();
      var act = ev.target.closest(".rdTrackBtn").getAttribute("data-act") || "watching";
      deskTrack(ticker, act, sig.signalDate);
      return;
    }
    // Any other control inside the card: ignore.
    if (ev.target.closest("button, a, input, select")) return;
    // Card body → Position Sizer near the click.
    ev.stopPropagation();
    if (window.PositionCalculator) {
      window.PositionCalculator.open(sig, ev);
    }
  }
  if (aplusGrid) aplusGrid.addEventListener("click", onCardClick);
  if (standardGrid) standardGrid.addEventListener("click", onCardClick);

  // ── Desk Tracker rows → check in on a tracked trade with desk insight ──
  // The scan card disappears once a name is no longer surfaced, so the tracker
  // is where you "check in" on an open trade as the days progress.
  var trackerBody = $("trackerBody");
  if (trackerBody) {
    trackerBody.addEventListener("click", function (ev) {
      var btn = ev.target.closest(".trkInsight");
      if (!btn || !lastTrackerSignals) return;
      ev.stopPropagation();
      var ticker = btn.getAttribute("data-ticker");
      var date = btn.getAttribute("data-date");
      var buckets = ["watching", "entered", "working", "broken", "exited",
                     "triggered", "pending", "target_hit", "stopped", "expired", "invalidated"];
      var rec = null;
      for (var i = 0; i < buckets.length && !rec; i++) {
        var arr = lastTrackerSignals[buckets[i]] || [];
        for (var j = 0; j < arr.length; j++) {
          if (arr[j].ticker === ticker && (!date || String(arr[j].signalDate || "") === date)) { rec = arr[j]; break; }
        }
      }
      if (!rec) return;
      var ix = Math.max(20, window.innerWidth - 470);
      fetchInsight("rd_signal", rec, "Red Dog: " + ticker + " (" + (rec.direction || "") + ", " + (rec.status || "tracked") + ")", ix, 96);
    });
  }

  // ── Gamma Context ──
  var gammaEl = $("gammaSection");
  if (gammaEl) {
    gammaEl.classList.add("rdClick");
    gammaEl.title = "Click for desk insight";
    gammaEl.addEventListener("click", function(ev) {
      if (ev.target.closest(".signalCard, button, a")) return;
      if (!lastPayload || !lastPayload.marketGamma) return;
      fetchInsight("rd_gamma", lastPayload.marketGamma, "Market Gamma Context", ev.clientX, ev.clientY);
    });
  }

  // ── Trend Filter ──
  var trendEl = $("trendSection");
  if (trendEl) {
    trendEl.classList.add("rdClick");
    trendEl.title = "Click for desk insight";
    trendEl.addEventListener("click", function(ev) {
      if (ev.target.closest("button, a")) return;
      if (!lastPayload || !lastPayload.marketTrend) return;
      fetchInsight("rd_trend", lastPayload.marketTrend, "SPX Trend Filter", ev.clientX, ev.clientY);
    });
  }

  // ── Scan Summary ──
  var statsEl = $("statsSection");
  if (statsEl) {
    statsEl.classList.add("rdClick");
    statsEl.title = "Click for desk insight";
    statsEl.addEventListener("click", function(ev) {
      if (ev.target.closest("button, a")) return;
      if (!lastPayload) return;
      var data = {
        asOfDate: lastPayload.asOfDate,
        scannedCount: lastPayload.scannedCount,
        setupsFound: lastPayload.setupsFound,
        aPlusCount: (lastPayload.aPlus || []).length,
        standardCount: (lastPayload.standard || []).length,
        topSignals: (lastPayload.aPlus || []).slice(0, 5).map(function(s) { return { ticker: s.ticker, score: s.quality?.score, direction: s.direction }; }),
      };
      fetchInsight("rd_scan_summary", data, "Scan Summary", ev.clientX, ev.clientY);
    });
  }

  // ── Gate Banner ──
  var gateEl = $("gateBanner");
  if (gateEl) {
    gateEl.classList.add("rdClick");
    gateEl.title = "Click for desk insight";
    gateEl.addEventListener("click", function(ev) {
      if (ev.target.closest("button, a")) return;
      if (!lastPayload) return;
      var data = { gateSummary: lastPayload.gateSummary || {}, gateContext: lastPayload.gateContext || {} };
      fetchInsight("rd_gate", data, "Gate Context", ev.clientX, ev.clientY);
    });
  }
})();
