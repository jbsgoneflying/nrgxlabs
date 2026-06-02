/* global window, document */

/**
 * Engine 4: Ichimoku Cloud Continuation Scanner
 * Client-side JavaScript for the Ichimoku Continuation UI
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
  el.className = `status ${type === "error" ? "statusError" : ""}`;
}

function showResults(show) {
  const results = $("results");
  if (results) {
    results.classList.toggle("hidden", !show);
  }
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

async function fetchScan(direction, force) {
  const params = new URLSearchParams();
  if (direction) params.set("direction", direction);
  // A force=true bypasses the structure-scan cache for a fully fresh pull.
  if (force) params.set("force", "true");
  // Always A+ only - no min_score parameter needed
  
  const url = `/api/engine4-ichimoku?${params.toString()}`;
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
  const actionableCount = payload.actionableCount ?? 0;
  const structureCount = payload.structureTotal ?? payload.structureCount ?? 0;
  const rejectedCount = payload.rejectedCount ?? 0;
  const duration = payload.meta?.scanDurationMs ?? 0;
  
  setText("statScanned", fmt0(scanned));
  setText("statActionable", fmt0(actionableCount));
  setText("statStructure", fmt0(structureCount));
  setText("statRejected", fmt0(rejectedCount));
  
  setText("statsMeta", `A+ setups only | ${payload.asOfDate || "—"} | ${duration > 0 ? `${(duration / 1000).toFixed(1)}s` : "—"}`);
}

function renderGammaContext(payload) {
  const gamma = payload.marketGamma || {};
  const spx = gamma.spx || {};
  const ndx = gamma.ndx || {};
  
  // SPX Gamma
  const spxAvailable = spx.available !== false && spx.netGammaSign;
  const spxSign = spx.netGammaSign || "unknown";
  if (spxSign === "positive") {
    setHtml("spxGammaSign", `<span class="gammaPositive">POSITIVE</span>`);
  } else if (spxSign === "negative") {
    setHtml("spxGammaSign", `<span class="gammaNegative">NEGATIVE</span>`);
  } else {
    setHtml("spxGammaSign", `<span style="color: var(--muted);">Unavailable</span>`);
  }
  
  const spxEnv = spx.environment || "unknown";
  if (spxEnv === "supportive") {
    setHtml("spxGammaEnv", `<span class="gammaEnvSupportive">Supportive</span>`);
  } else if (spxEnv === "challenging") {
    setHtml("spxGammaEnv", `<span class="gammaEnvChallenging">Challenging</span>`);
  } else {
    setHtml("spxGammaEnv", `<span style="color: var(--muted);">—</span>`);
  }
  
  // Show recommendation or unavailable message
  const spxNote = spx.recommendation || (spx.warnings ? spx.warnings[0] : "Gamma context unavailable.");
  setText("spxGammaNote", spxNote);
  
  // NDX Gamma
  const ndxAvailable = ndx.available !== false && ndx.netGammaSign;
  const ndxSign = ndx.netGammaSign || "unknown";
  if (ndxSign === "positive") {
    setHtml("ndxGammaSign", `<span class="gammaPositive">POSITIVE</span>`);
  } else if (ndxSign === "negative") {
    setHtml("ndxGammaSign", `<span class="gammaNegative">NEGATIVE</span>`);
  } else {
    setHtml("ndxGammaSign", `<span style="color: var(--muted);">Unavailable</span>`);
  }
  
  const ndxEnv = ndx.environment || "unknown";
  if (ndxEnv === "supportive") {
    setHtml("ndxGammaEnv", `<span class="gammaEnvSupportive">Supportive</span>`);
  } else if (ndxEnv === "challenging") {
    setHtml("ndxGammaEnv", `<span class="gammaEnvChallenging">Challenging</span>`);
  } else {
    setHtml("ndxGammaEnv", `<span style="color: var(--muted);">—</span>`);
  }
  
  // Show recommendation or unavailable message
  const ndxNote = ndx.recommendation || (ndx.warnings ? ndx.warnings[0] : "Gamma context unavailable.");
  setText("ndxGammaNote", ndxNote);
  
  setText("gammaMeta", spxAvailable || ndxAvailable ? "Dealer positioning by index" : "Gamma data unavailable for today");
}

function fmtAsOfTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  } catch (_) { return ""; }
}

// Live re-pricing strip: shows where price is RIGHT NOW relative to the
// (fixed) entry trigger, so a name that already ran reads "triggered" instead
// of a stale "0.29 to go".
function renderLiveStrip(live) {
  if (!live || live.available === false) return "";
  const state = live.state || "pending";
  const stateMap = {
    triggered:   { label: "Triggered", cls: "background:rgba(0,122,255,0.16);color:#0a62c9;" },
    target1:     { label: "Target 1 hit", cls: "background:rgba(52,199,89,0.18);color:#1b8a3e;" },
    stopped:     { label: "Stopped", cls: "background:rgba(255,59,48,0.16);color:#cc2f26;" },
    invalidated: { label: "Invalidated", cls: "background:rgba(255,59,48,0.12);color:#cc2f26;" },
    pending:     { label: "Pending", cls: "background:rgba(11,11,15,0.06);color:var(--muted);" },
  };
  const meta = stateMap[state] || stateMap.pending;
  const price = (live.price != null) ? fmtMoney(live.price) : "—";

  let distTxt = "";
  if (state === "pending" && live.toTrigger != null) {
    const atr = (live.toTriggerAtr != null) ? ` · ${fmt2(live.toTriggerAtr)} ATR` : "";
    distTxt = `<span class="liveDist">${fmtMoney(Math.abs(live.toTrigger))} to trigger${atr}</span>`;
  }
  const stamp = live.asOf ? `<span class="liveStamp">${live.marketOpen ? "live" : "last"} ${escapeHtml(fmtAsOfTime(live.asOf))}</span>` : "";

  return `<div class="signalCardLive">
    <span class="livePill" style="${meta.cls}">${meta.label}</span>
    <span class="livePrice">${price}</span>
    ${distTxt}
    ${stamp}
  </div>`;
}

function renderSignalCard(signal, isStructure = false) {
  const ticker = escapeHtml(signal.ticker || "");
  const direction = signal.direction || "bullish";
  const grade = signal.quality?.grade || "C";
  const score = signal.quality?.score ?? 0;
  const status = signal.status || "pending";
  
  const levels = signal.levels || {};
  const ichimoku = signal.ichimoku || {};
  const indicators = signal.indicators || {};
  const tags = signal.tags || [];
  const freshness = signal.freshness || {};
  
  // Grade class
  let gradeClass = "grade-c";
  if (grade === "A+") gradeClass = "grade-aplus";
  else if (grade === "A") gradeClass = "grade-a";
  else if (grade === "B") gradeClass = "grade-b";
  
  // Build tags HTML
  let tagsHtml = "";
  for (const tag of tags.slice(0, 6)) {
    const isPositive = ["Chikou Clear", "Vol Surge", "Strong Close", "Kijun Rising", "Kijun Falling", 
                        "RSI Confirm", "Cloud Aligned", "Cloud Optimal", "Gamma Supportive"].includes(tag);
    const isWarning = ["Earnings Warning"].includes(tag);
    const tagClass = isPositive ? "positive" : (isWarning ? "warning" : "");
    tagsHtml += `<span class="tagChip ${tagClass}">${escapeHtml(tag)}</span>`;
  }
  
  // Index badge
  const indexBadge = signal.indexMembership === "nasdaq100" ? "NDX" : 
                     signal.indexMembership === "both" ? "S&P/NDX" : "S&P";
  
  // Build freshness info
  let freshnessHtml = "";
  if (!isStructure) {
    // Actionable - show positive freshness metrics
    const reclaimBars = freshness.barsSinceReclaim;
    const kijunDist = freshness.kijunDistanceAtr;
    if (reclaimBars !== null && reclaimBars !== undefined) {
      freshnessHtml += `<span class="freshBadge positive">Reclaim ${reclaimBars} bar${reclaimBars !== 1 ? 's' : ''} ago</span>`;
    }
    if (kijunDist !== null && kijunDist !== undefined) {
      freshnessHtml += `<span class="freshBadge positive">${fmt2(kijunDist)} ATR from Kijun</span>`;
    }
  } else {
    // Structure - lead with distance-to-actionable, then the reasons
    const dist = freshness.distanceToActionable;
    if (dist !== null && dist !== undefined) {
      freshnessHtml += `<span class="freshBadge positive">≈${fmt2(dist)} to actionable</span>`;
    }
    const reasons = freshness.reasons || [];
    for (const reason of reasons.slice(0, 2)) {
      freshnessHtml += `<span class="freshBadge warning">${escapeHtml(reason)}</span>`;
    }
  }
  
  // Gate pill (Raven-Tech 2.0)
  let gatePillHtml = "";
  const gate = signal.gate || {};
  if (gate.status) {
    const gCls = gate.status === "TRADABLE" ? "background:rgba(52,199,89,0.14);color:#1b8a3e;" :
                 gate.status === "SUPPRESS" ? "background:rgba(255,59,48,0.14);color:#cc2f26;" :
                 "background:rgba(255,149,0,0.14);color:#995c00;";
    const reasons = (gate.reasons || []).map(r => r.label || r.code).slice(0, 3).join(", ");
    gatePillHtml = `<div style="margin:4px 0 2px;"><span style="display:inline-block;font-size:9px;font-weight:800;padding:2px 8px;border-radius:12px;text-transform:uppercase;${gCls}">${gate.status}</span>${reasons ? `<span style="font-size:10px;color:var(--muted);margin-left:4px;">${escapeHtml(reasons)}</span>` : ""}</div>`;
  }

  // Reconciled desk verdict — leads the card
  const verdict = signal.verdict || {};
  let verdictHtml = "";
  if (verdict.status) {
    const vCls = verdict.status === "TRADABLE" ? "vTradable" : (verdict.status === "STAND_DOWN" ? "vStandDown" : "vWatch");
    const driver = (verdict.drivers && verdict.drivers[0]) ? verdict.drivers[0] : "";
    verdictHtml = `<div class="verdictStrip ${vCls}"><span class="verdictPill">${escapeHtml(verdict.label || verdict.status)}</span><span class="verdictDriver">${escapeHtml(driver)}</span></div>`;
  }

  // Liquidity (20d $ ADV) + tracker state
  const dollarAdv = indicators.dollarAdv;
  const advTxt = (dollarAdv && Number.isFinite(Number(dollarAdv))) ? `$${(Number(dollarAdv) / 1e6).toFixed(0)}M` : "—";
  const isTracked = ["watching", "entered", "working", "broken", "exited"].includes(status);
  const actionsHtml = `
      <div class="signalCardActions">
        <button type="button" class="ikCardBtn ikInsightBtn" data-ticker="${ticker}">Insight</button>
        <button type="button" class="ikCardBtn ikTrackBtn ${isTracked ? 'isTracked' : ''}" data-ticker="${ticker}" data-act="watching">${isTracked ? escapeHtml(status) : 'Watch'}</button>
      </div>`;

  return `
    <div class="signalCard ${isStructure ? 'structureCard' : 'actionableCard'}" data-ticker="${ticker}">
      ${verdictHtml}
      <div class="signalCardHeader">
        <div class="signalCardTicker">
          <span class="signalCardSymbol">${ticker}</span>
          <span class="signalCardDirection ${direction}">${direction}</span>
          <span class="indexBadgeSmall">${indexBadge}</span>
          ${status !== "pending" ? `<span class="signalCardStatus ${status}">${status}</span>` : ""}
        </div>
        <span class="signalCardGrade ${gradeClass}">${grade} (${score})</span>
      </div>
      ${gatePillHtml}
      ${renderLiveStrip(signal.live)}
      ${freshnessHtml ? `<div class="signalCardFreshness">${freshnessHtml}</div>` : ""}
      <div class="signalCardBody">
        <div class="signalCardMetric">
          <span class="k">Entry</span>
          <span class="v">${fmtMoney(levels.entryTrigger)}</span>
        </div>
        <div class="signalCardMetric">
          <span class="k">Stop</span>
          <span class="v">${fmtMoney(levels.stopLoss)}</span>
        </div>
        <div class="signalCardMetric">
          <span class="k">Target 1</span>
          <span class="v">${fmtMoney(levels.target1)}</span>
        </div>
        <div class="signalCardMetric">
          <span class="k">Risk</span>
          <span class="v">${fmtMoney(levels.riskDollars)}</span>
        </div>
        <div class="signalCardMetric">
          <span class="k">RSI</span>
          <span class="v">${fmt0(indicators.rsi)}</span>
        </div>
        <div class="signalCardMetric">
          <span class="k">Vol Ratio</span>
          <span class="v">${fmt2(indicators.volumeRatio)}x</span>
        </div>
        <div class="signalCardMetric">
          <span class="k">$ ADV</span>
          <span class="v">${advTxt}</span>
        </div>
      </div>
      <div class="signalCardIchimoku">
        <div class="ichimokuValue">
          <span class="label">Tenkan</span>
          <span class="value">${fmt2(ichimoku.tenkan)}</span>
        </div>
        <div class="ichimokuValue">
          <span class="label">Kijun</span>
          <span class="value">${fmt2(ichimoku.kijun)}</span>
        </div>
        <div class="ichimokuValue">
          <span class="label">Cloud</span>
          <span class="value">${ichimoku.cloudBias || "—"}</span>
        </div>
      </div>
      ${tagsHtml ? `<div class="signalCardTags">${tagsHtml}</div>` : ""}
      ${isStructure ? '<div class="structureNote">Watch for next pullback to Kijun</div>' : ""}
      ${actionsHtml}
    </div>
  `;
}

function renderSignals(payload) {
  const actionable = payload.actionable || [];
  const structure = payload.structure || [];
  
  // Actionable Now Section
  const actionableGrid = $("actionableGrid");
  const actionableSection = $("actionableSection");
  const actionableMeta = $("actionableMeta");
  
  if (actionable.length > 0) {
    actionableGrid.innerHTML = actionable.map(s => renderSignalCard(s, false)).join("");
    // Lead with the reconciled verdict mix so the header never overstates "ready
    // to trade" when the regime/gamma has stood names down.
    const vCount = (st) => actionable.filter(s => (s.verdict && s.verdict.status) === st).length;
    const vt = vCount("TRADABLE"), vw = vCount("WATCH"), vs = vCount("STAND_DOWN");
    const vParts = [];
    if (vt) vParts.push(`${vt} tradable`);
    if (vw) vParts.push(`${vw} watch`);
    if (vs) vParts.push(`${vs} stand-down`);
    actionableMeta.textContent = `${actionable.length} fresh trigger${actionable.length !== 1 ? 's' : ''}` +
      (vParts.length ? ` — ${vParts.join(", ")}` : "");
    actionableSection.classList.remove("hidden");
  } else {
    actionableSection.classList.add("hidden");
  }
  
  // Approaching (Watchlist) Section — capped + ranked, collapsed by default
  const structureGrid = $("structureGrid");
  const structureSection = $("structureSection");
  const structureMeta = $("structureMeta");
  const total = payload.structureTotal ?? structure.length;
  
  if (structure.length > 0) {
    structureGrid.innerHTML = structure.map(s => renderSignalCard(s, true)).join("");
    const capNote = total > structure.length ? ` (top ${structure.length} of ${total})` : "";
    structureMeta.textContent = `${structure.length} approaching setup${structure.length !== 1 ? 's' : ''}${capNote} — ranked by distance to actionable`;
    structureSection.classList.remove("hidden");
    // Keep collapsed by default; reset toggle label each render.
    const toggle = $("approachingToggle");
    if (toggle) {
      structureGrid.style.display = "none";
      toggle.textContent = `Show approaching (${structure.length})`;
      toggle.style.display = "";
    }
  } else {
    structureSection.classList.add("hidden");
  }
  
  // Empty State
  const emptySection = $("emptySection");
  if (actionable.length === 0 && structure.length === 0) {
    emptySection.classList.remove("hidden");
  } else {
    emptySection.classList.add("hidden");
  }
}

function renderGateBanner(payload) {
  const banner = $("gateBanner");
  if (!banner) return;

  const gs = payload.gateSummary;
  if (!gs) { banner.style.display = "none"; return; }

  banner.style.display = "block";
  const total = gs.total || 0;
  const tradable = gs.TRADABLE || 0;
  const watch = gs.WATCH || 0;
  const suppress = gs.SUPPRESS || 0;

  const pill = (cls, text) =>
    `<span style="display:inline-block;font-size:10px;font-weight:800;padding:3px 10px;border-radius:20px;text-transform:uppercase;letter-spacing:0.04em;${cls}">${text}</span>`;

  const summaryEl = $("gateSummary");
  if (summaryEl) {
    summaryEl.innerHTML = [
      tradable > 0 ? pill("background:rgba(52,199,89,0.14);color:#1b8a3e;", `${tradable} Tradable`) : "",
      watch > 0 ? pill("background:rgba(255,149,0,0.14);color:#995c00;", `${watch} Watch`) : "",
      suppress > 0 ? pill("background:rgba(255,59,48,0.14);color:#cc2f26;", `${suppress} Suppress`) : "",
      pill("background:rgba(11,11,15,0.04);color:var(--muted);", `${total} Total`),
    ].filter(Boolean).join(" ");
  }

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
  if (!vs || !vs.total) { banner.style.display = "none"; return; }
  banner.style.display = "block";

  const pill = (cls, text) =>
    `<span style="display:inline-block;font-size:10px;font-weight:800;padding:3px 10px;border-radius:20px;text-transform:uppercase;letter-spacing:0.04em;${cls}">${text}</span>`;

  const tradable = vs.TRADABLE || 0;
  const watch = vs.WATCH || 0;
  const stand = vs.STAND_DOWN || 0;
  const el = $("verdictSummary");
  if (el) {
    el.innerHTML = [
      tradable > 0 ? pill("background:rgba(52,199,89,0.14);color:#1b8a3e;", `${tradable} Tradable`) : "",
      watch > 0 ? pill("background:rgba(255,149,0,0.14);color:#995c00;", `${watch} Watch`) : "",
      stand > 0 ? pill("background:rgba(255,59,48,0.14);color:#cc2f26;", `${stand} Stand Down`) : "",
      pill("background:rgba(11,11,15,0.04);color:var(--muted);", `${vs.total} Total`),
    ].filter(Boolean).join(" ");
  }
}

function render(payload) {
  lastPayload = payload;
  showResults(true);
  renderGateBanner(payload);
  renderVerdictBanner(payload);
  renderStats(payload);
  renderGammaContext(payload);
  renderSignals(payload);
}

// -----------------------------------------------------------------------------
// Event Handlers
// -----------------------------------------------------------------------------

async function handleScan(e, opts) {
  if (e) e.preventDefault();
  // Explicit "Scan Universe" clicks force a fully fresh pull; the auto-load on
  // page open uses the short structure cache (still live-repriced server-side).
  const force = !(opts && opts.force === false);

  const direction = $("direction")?.value || "";
  
  setLoading(true, "Scanning SP500 + Nasdaq100...");
  setStatus(force ? "Scanning universe for A+ setups (fresh pull)..." : "Loading latest scan...");
  
  // Progress updates
  if (window.RavenLoading) {
    window.RavenLoading.setProgress(10, "Scanning 516 tickers...");
  }
  
  try {
    const payload = await fetchScan(direction, force);
    
    if (window.RavenLoading) {
      window.RavenLoading.setProgress(75, "Classifying setups...");
    }
    
    render(payload);
    
    if (window.RavenLoading) {
      window.RavenLoading.setProgress(95, "Rendering results...");
    }
    
    const actionable = payload.actionableCount ?? 0;
    const structureTotal = payload.structureTotal ?? payload.structureCount ?? 0;
    const rejected = payload.rejectedCount ?? 0;
    const totalAPlus = payload.totalAPlus ?? (actionable + structureTotal);

    let statusMsg = `Found ${totalAPlus} A+ setup${totalAPlus !== 1 ? 's' : ''}`;
    if (actionable > 0) statusMsg += ` · ${actionable} actionable now`;
    if (structureTotal > 0) statusMsg += ` · ${structureTotal} approaching`;
    if (rejected > 0) statusMsg += ` · ${rejected} rejected (impulse bars)`;
    setStatus(statusMsg);
    
    // Newly scanned signals are persisted server-side; refresh the tracker
    // view AND re-evaluate open names against live price.
    loadTracker(true);
  } catch (err) {
    console.error("Scan failed:", err);
    setStatus(`Error: ${err.message}`, "error");
    showResults(false);
  } finally {
    setLoading(false);
  }
}

// -----------------------------------------------------------------------------
// Desk Trade Tracker + Backtest
// -----------------------------------------------------------------------------

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
    const resp = await fetch("/api/engine4-ichimoku/track", {
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
    // Optimistically reflect the desk state on any matching card button.
    try {
      const sel = `.signalCard[data-ticker="${(window.CSS && CSS.escape) ? CSS.escape(ticker) : ticker}"] .ikTrackBtn`;
      document.querySelectorAll(sel).forEach(b => {
        b.textContent = removed ? "Watch" : status;
        b.classList.toggle("isTracked", !removed);
      });
    } catch (_) { /* selector best-effort */ }
    setStatus(removed ? `${ticker} removed from desk book.` : `${ticker} marked "${status}".`);
  } catch (e) {
    setStatus(`Tracker error: ${e.message}`, "error");
  }
}

function renderTracker(signals) {
  const body = $("trackerBody");
  const meta = $("trackerMeta");
  const summary = $("trackerSummary");
  if (!body) return;
  if (!signals) { body.innerHTML = '<div class="muted" style="font-size:12px;">No tracked signals yet.</div>'; return; }
  lastTrackerSignals = signals;

  const counts = signals.counts || {};
  if (summary) {
    const order = ["watching", "entered", "working", "pending", "triggered", "target_hit", "stopped", "broken", "exited", "expired", "invalidated"];
    summary.innerHTML = order
      .filter(k => (counts[k] || 0) > 0)
      .map(k => `<span class="trkPill" style="${trkPillStyle(k)}">${counts[k]} ${k.replace('_', ' ')}</span>`)
      .join("");
  }
  if (meta) {
    const wr = signals.winRate;
    meta.textContent = `${signals.totalSignals || 0} tracked · ${signals.deskBookCount || 0} in desk book` +
      (wr !== null && wr !== undefined ? ` · ${wr}% win (resolved ${signals.resolvedCount || 0})` : "");
  }

  // Desk book first (anything the trader is managing), then live auto-tracked.
  const deskBook = [].concat(
    signals.watching || [], signals.entered || [], signals.working || [],
    signals.broken || [], signals.exited || []
  );
  const live = [].concat(signals.triggered || [], signals.pending || []);
  const rows = deskBook.concat(live).slice(0, 40);

  if (!rows.length) {
    body.innerHTML = '<div class="muted" style="font-size:12px;">No tracked signals yet. Click <b>Watch</b> on a card to start a desk book.</div>';
    return;
  }

  body.innerHTML = rows.map(r => {
    const t = escapeHtml(r.ticker || "");
    const sd = escapeHtml(r.signalDate || "");
    const st = r.status || "pending";
    const dir = escapeHtml(r.direction || "");
    const opts = TRACKER_STATUSES.map(s => `<option value="${s}" ${s === st ? 'selected' : ''}>${s}</option>`).join("");
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
      const t = ev.target.getAttribute("data-ticker");
      deskTrack(t, "untrack", ev.target.getAttribute("data-date"));
    });
  });
}

async function loadTracker(refresh) {
  const btn = $("trackerRefreshBtn");
  try {
    if (btn) btn.disabled = true;
    const url = `/api/engine4-ichimoku/status${refresh ? "?refresh=true" : ""}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    renderTracker(data.signals);
  } catch (e) {
    setStatus(`Tracker load error: ${e.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function btRow(label, s) {
  if (!s) return "";
  const cell = (v, suffix) => (v === null || v === undefined) ? "—" : `${v}${suffix || ""}`;
  return `<tr>
    <td>${escapeHtml(label)}</td>
    <td>${s.signals || 0}</td>
    <td>${s.triggered || 0}</td>
    <td>${cell(s.winRate, "%")}</td>
    <td>${cell(s.avgR)}</td>
    <td>${cell(s.expectancy)}</td>
    <td>${cell(s.avgMae)}</td>
    <td>${cell(s.avgMfe)}</td>
  </tr>`;
}

async function runBacktest() {
  const btn = $("backtestRunBtn");
  const body = $("backtestBody");
  try {
    if (btn) { btn.disabled = true; btn.textContent = "Running…"; }
    if (body) body.innerHTML = '<div class="muted" style="font-size:12px;">Replaying history — this can take a moment…</div>';
    const yrsSel = $("backtestYears");
    const yrs = yrsSel ? parseInt(yrsSel.value, 10) || 2 : 2;
    const end = new Date();
    const start = new Date(end.getTime() - yrs * 365 * 24 * 3600 * 1000);
    const fmt = (d) => d.toISOString().slice(0, 10);
    const resp = await fetch(`/api/engine4-ichimoku/backtest?min_score=75&max_tickers=60&start=${fmt(start)}&end=${fmt(end)}`);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const data = await resp.json();
    const head = `<thead><tr><th>Cohort</th><th>Signals</th><th>Trig</th><th>Win</th><th>Avg R</th><th>Exp</th><th>MAE</th><th>MFE</th></tr></thead>`;
    let rows = btRow("Overall", data.overall);
    const byGrade = data.byGrade || {};
    Object.keys(byGrade).forEach(g => { rows += btRow(`Grade ${g}`, byGrade[g]); });
    const byBucket = data.byBucket || {};
    Object.keys(byBucket).forEach(b => { rows += btRow(`Bucket: ${b}`, byBucket[b]); });
    const p = data.params || {};
    const win = data.window || {};
    if (body) {
      body.innerHTML = `
        <table class="btTable">${head}<tbody>${rows}</tbody></table>
        <div class="muted" style="font-size:11px;margin-top:8px;">
          ${win.start || "?"} → ${win.end || "?"} · ${p.tickersWithSignals || 0}/${p.tickersTested || 0} names with signals · min score ${p.minScore}
        </div>`;
    }
  } catch (e) {
    if (body) body.innerHTML = `<div style="font-size:12px;color:var(--red,#cc2f26);">Backtest error: ${escapeHtml(e.message)}</div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Run backtest"; }
  }
}

// -----------------------------------------------------------------------------
// Initialization
// -----------------------------------------------------------------------------

function init() {
  // Form submission handler
  const form = $("e4Form");
  if (form) {
    form.addEventListener("submit", handleScan);
  }
  
  // Button handler (backup)
  const runBtn = $("runBtn");
  if (runBtn) {
    runBtn.addEventListener("click", handleScan);
  }
  
  // Approaching (structure) toggle — collapsed by default
  const approachingToggle = $("approachingToggle");
  if (approachingToggle) {
    approachingToggle.addEventListener("click", () => {
      const grid = $("structureGrid");
      if (!grid) return;
      const hidden = grid.style.display === "none";
      grid.style.display = hidden ? "" : "none";
      const n = (lastPayload && (lastPayload.structure || []).length) || 0;
      approachingToggle.textContent = hidden ? "Hide approaching" : `Show approaching (${n})`;
    });
  }
  
  // Desk tracker + backtest controls
  const trackerRefreshBtn = $("trackerRefreshBtn");
  if (trackerRefreshBtn) trackerRefreshBtn.addEventListener("click", () => loadTracker(true));
  const backtestRunBtn = $("backtestRunBtn");
  if (backtestRunBtn) backtestRunBtn.addEventListener("click", runBacktest);
  
  // Initialize tooltips
  initTooltips();
  
  // Auto-load on open: re-evaluate the desk book against live price, then pull
  // the latest scan (short-cached structure, live-repriced server-side) so the
  // desk immediately sees current plays + an up-to-date playbook. Reloading the
  // page any time during the day re-prices everything.
  loadTracker(true);
  handleScan(null, { force: false });
}

// Run on DOM ready
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}

// ---------------------------------------------------------------------------
// Desk Insight Popup — LLM-powered card insights for Ichimoku
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  var popup = $("ikInsightPopup");
  if (!popup) return;

  initDrag(popup, $("ikInsightHeader"), { closeSelector: "#ikInsightClose" });
  $("ikInsightClose").addEventListener("click", function () { popup.style.display = "none"; });

  var ikInsight = new InsightPopup({
    popupEl: popup,
    titleEl: $("ikInsightTitle"),
    bodyEl:  $("ikInsightBody"),
    prefix:  "ikInsight",
    labels: {
      ichimoku_structure:"Ichimoku Structure",entry_quality:"Entry Quality",freshness_read:"Freshness Read",
      risk_framework:"Risk Framework",component_analysis:"Component Analysis",
      dual_index_read:"Dual Index Read",continuation_impact:"Continuation Impact",index_membership:"Index Membership",
      opportunity_read:"Opportunity Read",actionable_vs_structure:"Actionable vs Structure",rejection_rate:"Rejection Rate",
      gate_status:"Gate Status",regime_for_continuation:"Regime for Continuation",vol_direction_impact:"Vol Direction Impact",
      desk_takeaway:"Desk Takeaway",
    },
  });

  function fetchInsight(cardType, cardData, title, x, y) {
    var ctx = {};
    if (lastPayload) { ctx.marketGamma = lastPayload.marketGamma || {}; ctx.asOfDate = lastPayload.asOfDate; }
    ikInsight.fetch(cardType, cardData, title, x, y, ctx);
  }

  // ── Signal cards (Actionable and Structure) ──
  var actionableGrid = $("actionableGrid");
  var structureGrid = $("structureGrid");
  // Single click model — deconflicts the two popups:
  //   • "Insight" button   → LLM popup, docked to the RIGHT edge
  //   • "Watch" button     → desk tracker (no popup)
  //   • card body          → Position Sizer, near the click (LEFT-ish)
  // No more double-open: the body no longer also fires the insight popup.
  function onCardClick(ev) {
    var card = ev.target.closest(".signalCard");
    if (!card || !lastPayload) return;
    var ticker = card.getAttribute("data-ticker");
    var allSignals = [].concat(lastPayload.actionable || [], lastPayload.structure || []);
    var sig = allSignals.find(function(s) { return s.ticker === ticker; });
    if (!sig) return;

    // Dedicated insight affordance → LLM, docked right so it never overlaps the sizer.
    if (ev.target.closest(".ikInsightBtn")) {
      ev.stopPropagation();
      var ix = Math.max(20, window.innerWidth - 470);
      fetchInsight("ik_signal", sig, "Ichimoku: " + ticker + " (" + (sig.direction || "") + ")", ix, 96);
      return;
    }
    // Desk tracker affordance → mark watching (no popup).
    if (ev.target.closest(".ikTrackBtn")) {
      ev.stopPropagation();
      var act = ev.target.closest(".ikTrackBtn").getAttribute("data-act") || "watching";
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
  if (actionableGrid) actionableGrid.addEventListener("click", onCardClick);
  if (structureGrid) structureGrid.addEventListener("click", onCardClick);

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
      fetchInsight("ik_signal", rec, "Ichimoku: " + ticker + " (" + (rec.direction || "") + ", " + (rec.status || "tracked") + ")", ix, 96);
    });
  }

  // ── Gamma Context (SPX + NDX) ──
  var gammaEl = $("gammaSection");
  if (gammaEl) {
    gammaEl.classList.add("ikClick");
    gammaEl.title = "Click for desk insight";
    gammaEl.addEventListener("click", function(ev) {
      if (ev.target.closest(".signalCard, button, a")) return;
      if (!lastPayload || !lastPayload.marketGamma) return;
      fetchInsight("ik_gamma", lastPayload.marketGamma, "Market Gamma Context (SPX + NDX)", ev.clientX, ev.clientY);
    });
  }

  // ── Scan Summary ──
  var statsEl = $("statsSection");
  if (statsEl) {
    statsEl.classList.add("ikClick");
    statsEl.title = "Click for desk insight";
    statsEl.addEventListener("click", function(ev) {
      if (ev.target.closest("button, a")) return;
      if (!lastPayload) return;
      var data = {
        asOfDate: lastPayload.asOfDate,
        scannedCount: lastPayload.scannedCount,
        actionableCount: lastPayload.actionableCount || (lastPayload.actionable || []).length,
        structureCount: lastPayload.structureCount || (lastPayload.structure || []).length,
        rejectedCount: lastPayload.rejectedCount || 0,
        direction: lastPayload.meta?.direction || null,
      };
      fetchInsight("ik_scan_summary", data, "Scan Summary", ev.clientX, ev.clientY);
    });
  }

  // ── Gate Banner ──
  var gateEl = $("gateBanner");
  if (gateEl) {
    gateEl.classList.add("ikClick");
    gateEl.title = "Click for desk insight";
    gateEl.addEventListener("click", function(ev) {
      if (ev.target.closest("button, a")) return;
      if (!lastPayload) return;
      var data = { gateSummary: lastPayload.gateSummary || {}, gateContext: lastPayload.gateContext || {} };
      fetchInsight("ik_gate", data, "Gate Context", ev.clientX, ev.clientY);
    });
  }
})();
