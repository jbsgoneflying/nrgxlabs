/* global window, document */

/**
 * Engine 4: Ichimoku Cloud Continuation Scanner
 * Client-side JavaScript for the Ichimoku Continuation UI
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

async function fetchScan(direction) {
  const params = new URLSearchParams();
  if (direction) params.set("direction", direction);
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

// Safe text setter - prevents "Cannot set properties of null" errors
function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function setHtml(id, html) {
  const el = $(id);
  if (el) el.innerHTML = html;
}

function renderStats(payload) {
  const scanned = payload.scannedCount ?? 0;
  const actionableCount = payload.actionableCount ?? 0;
  const structureCount = payload.structureCount ?? 0;
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
    // Structure - show the reasons why not actionable
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

  return `
    <div class="signalCard ${isStructure ? 'structureCard' : 'actionableCard'}" data-ticker="${ticker}">
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
    actionableMeta.textContent = `${actionable.length} fresh trigger${actionable.length !== 1 ? 's' : ''} ready to trade`;
    actionableSection.classList.remove("hidden");
  } else {
    actionableSection.classList.add("hidden");
  }
  
  // Structure Only (Watchlist) Section
  const structureGrid = $("structureGrid");
  const structureSection = $("structureSection");
  const structureMeta = $("structureMeta");
  
  if (structure.length > 0) {
    structureGrid.innerHTML = structure.map(s => renderSignalCard(s, true)).join("");
    structureMeta.textContent = `${structure.length} setup${structure.length !== 1 ? 's' : ''} for watchlist`;
    structureSection.classList.remove("hidden");
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

function render(payload) {
  lastPayload = payload;
  showResults(true);
  renderGateBanner(payload);
  renderStats(payload);
  renderGammaContext(payload);
  renderSignals(payload);
}

// -----------------------------------------------------------------------------
// Event Handlers
// -----------------------------------------------------------------------------

async function handleScan(e) {
  if (e) e.preventDefault();
  
  const direction = $("direction")?.value || "";
  
  setLoading(true, "Scanning SP500 + Nasdaq100...");
  setStatus("Scanning universe for A+ setups...");
  
  // Progress updates
  if (window.RavenLoading) {
    window.RavenLoading.setProgress(10, "Scanning 516 tickers...");
  }
  
  try {
    const payload = await fetchScan(direction);
    
    if (window.RavenLoading) {
      window.RavenLoading.setProgress(75, "Classifying setups...");
    }
    
    render(payload);
    
    if (window.RavenLoading) {
      window.RavenLoading.setProgress(95, "Rendering results...");
    }
    
    const actionable = payload.actionableCount ?? 0;
    const structure = payload.structureCount ?? 0;
    const rejected = payload.rejectedCount ?? 0;
    const total = actionable + structure;
    
    let statusMsg = `Found ${total} A+ setup${total !== 1 ? 's' : ''}`;
    if (actionable > 0) statusMsg += ` (${actionable} actionable)`;
    if (rejected > 0) statusMsg += `. ${rejected} rejected as impulse bars.`;
    setStatus(statusMsg);
  } catch (err) {
    console.error("Scan failed:", err);
    setStatus(`Error: ${err.message}`, "error");
    showResults(false);
  } finally {
    setLoading(false);
  }
}

function handleCardClick(e) {
  const card = e.target.closest(".signalCard");
  if (!card) return;
  
  const ticker = card.dataset.ticker;
  if (!ticker) return;
  
  // Find the signal data for this ticker
  if (!lastPayload) return;
  
  const allSignals = [
    ...(lastPayload.actionable || []),
    ...(lastPayload.structure || []),
  ];
  
  const signal = allSignals.find(s => s.ticker === ticker);
  if (!signal) return;
  
  // Open the Position Calculator with this signal's data
  if (window.PositionCalculator) {
    window.PositionCalculator.open(signal, e);
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
  
  // Card click handler
  document.addEventListener("click", handleCardClick);
  
  // Initialize tooltips
  initTooltips();
  
  // Don't auto-run - let user adjust filters and click Scan manually
  setStatus("Adjust filters above and click \"Scan Universe\" to find Ichimoku continuation setups.");
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

  var _ikCache = {};
  var popup     = $("ikInsightPopup");
  var popHeader = $("ikInsightHeader");
  var popTitle  = $("ikInsightTitle");
  var popClose  = $("ikInsightClose");
  var popBody   = $("ikInsightBody");
  if (!popup) return;

  // ── Drag ──
  (function () {
    var ox=0,oy=0,sx=0,sy=0,dragging=false;
    function onDown(ev){if(ev.target===popClose)return;dragging=true;ox=ev.clientX;oy=ev.clientY;var r=popup.getBoundingClientRect();sx=r.left;sy=r.top;popup.classList.add("isDragging");document.addEventListener("mousemove",onMove);document.addEventListener("mouseup",onUp);}
    function onMove(ev){if(!dragging)return;popup.style.left=(sx+ev.clientX-ox)+"px";popup.style.top=(sy+ev.clientY-oy)+"px";}
    function onUp(){dragging=false;popup.classList.remove("isDragging");document.removeEventListener("mousemove",onMove);document.removeEventListener("mouseup",onUp);}
    popHeader.addEventListener("mousedown",onDown);
  })();
  popClose.addEventListener("click",function(){popup.style.display="none";});

  function openPopup(title,x,y){
    popTitle.textContent=title;
    popBody.innerHTML="<div class='ikInsightLoading'><span class='ikInsightDot'></span><span class='ikInsightDot'></span><span class='ikInsightDot'></span><br>Generating desk insight\u2026</div>";
    popup.style.left=Math.min(x,window.innerWidth-460)+"px";
    popup.style.top=Math.min(y,window.innerHeight-300)+"px";
    popup.style.display="block";
  }

  var _lbl={
    ichimoku_structure:"Ichimoku Structure",entry_quality:"Entry Quality",freshness_read:"Freshness Read",
    risk_framework:"Risk Framework",component_analysis:"Component Analysis",
    dual_index_read:"Dual Index Read",continuation_impact:"Continuation Impact",index_membership:"Index Membership",
    opportunity_read:"Opportunity Read",actionable_vs_structure:"Actionable vs Structure",rejection_rate:"Rejection Rate",
    gate_status:"Gate Status",regime_for_continuation:"Regime for Continuation",vol_direction_impact:"Vol Direction Impact",
    desk_takeaway:"Desk Takeaway",
  };

  function renderInsight(data){
    if(!data){popBody.innerHTML="<div class='ikInsightLoading'>No insight data.</div>";return;}
    var html="";
    if(data._fallback_reason) html+="<div style='background:rgba(255,107,107,.15);border:1px solid rgba(255,107,107,.3);border-radius:8px;padding:10px 12px;margin-bottom:14px;font-size:11px;color:#ff6b6b;'>"+escapeHtml(data._fallback_reason)+"</div>";
    var skip=new Set(["_source","_meta","_card_type","_fallback_reason"]);
    for(var key in data){
      if(skip.has(key))continue;
      var label=_lbl[key]||key.replace(/_/g," ").replace(/\b\w/g,function(c){return c.toUpperCase();});
      var isDesk=key==="desk_takeaway";
      html+="<div class='ikInsightSection'><div class='ikInsightSectionTitle'>"+escapeHtml(label)+"</div><div class='ikInsightText'"+(isDesk?" style='color:#34c759;font-weight:600;'":"")+">"+escapeHtml(String(data[key]))+"</div></div>";
    }
    if(data._source) html+="<div class='ikInsightSource'>Source: "+escapeHtml(data._source)+"</div>";
    popBody.innerHTML=html;
  }

  function fetchInsight(cardType,cardData,title,x,y){
    var cacheKey=cardType+":"+JSON.stringify(cardData).substring(0,100);
    if(_ikCache[cacheKey]){openPopup(title,x,y);renderInsight(_ikCache[cacheKey]);return;}
    openPopup(title,x,y);
    var ctx={};
    if(lastPayload){ctx.marketGamma=lastPayload.marketGamma||{};ctx.asOfDate=lastPayload.asOfDate;}
    fetch("/api/front-layer/card-insight",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({card_type:cardType,card_data:cardData,dms_summary:ctx})})
    .then(function(r){return r.json();})
    .then(function(resp){if(resp.error||resp.detail){popBody.innerHTML="<div class='ikInsightLoading' style='color:#ff6b6b;'>Error: "+escapeHtml(resp.error||resp.detail||"Unknown")+"</div>";return;}_ikCache[cacheKey]=resp;renderInsight(resp);})
    .catch(function(){popBody.innerHTML="<div class='ikInsightLoading' style='color:#ff6b6b;'>Failed to load insight.</div>";});
  }

  // ── Signal cards (Actionable and Structure) ──
  var actionableGrid = $("actionableGrid");
  var structureGrid = $("structureGrid");
  function onCardClick(ev) {
    var card = ev.target.closest(".signalCard");
    if (!card || !lastPayload) return;
    if (ev.target.closest("button, a, input")) return;
    var ticker = card.getAttribute("data-ticker");
    var allSignals = [].concat(lastPayload.actionable || [], lastPayload.structure || []);
    var sig = allSignals.find(function(s) { return s.ticker === ticker; });
    if (!sig) return;
    ev.stopPropagation();
    // Open Position Calculator alongside the desk insight (same as Red Dog)
    if (window.PositionCalculator) {
      window.PositionCalculator.open(sig, ev);
    }
    fetchInsight("ik_signal", sig, "Ichimoku: " + ticker + " (" + (sig.direction || "") + ")", ev.clientX, ev.clientY);
  }
  if (actionableGrid) actionableGrid.addEventListener("click", onCardClick);
  if (structureGrid) structureGrid.addEventListener("click", onCardClick);

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
