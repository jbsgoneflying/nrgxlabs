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
    <div class="${cardClass}" data-ticker="${ticker}">
      <div class="signalCardHeader">
        <div class="signalCardTicker">
          <span class="signalCardSymbol">${ticker}</span>
          <span class="signalCardDirection ${dirClass}">${direction}</span>
        </div>
        <span class="signalCardGrade ${gradeClass}">${grade} (${score})</span>
      </div>
      ${gatePillHtml}
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
      </div>
      ${chipsHtml}
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
  
  // Add click handlers for Position Calculator
  container.querySelectorAll(".signalCard").forEach(card => {
    card.addEventListener("click", (e) => {
      const ticker = card.dataset.ticker;
      if (!ticker || !lastPayload) return;
      
      // Find the signal data for this ticker (Engine 3 uses aPlus and standard)
      const allSignals = [
        ...(lastPayload.aPlus || []),
        ...(lastPayload.standard || []),
      ];
      
      const signal = allSignals.find(s => s.ticker === ticker);
      if (!signal) return;
      
      // Open the Position Calculator with this signal's data
      if (window.PositionCalculator) {
        window.PositionCalculator.open(signal, e);
      }
    });
  });
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

function renderResults(payload) {
  lastPayload = payload;
  
  // Show results section
  $("results").classList.remove("hidden");
  
  // Render gate banner (Raven-Tech 2.0)
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

// ---------------------------------------------------------------------------
// Desk Insight Popup — LLM-powered card insights for Red Dog
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  var _rdCache = {};
  var popup      = $("rdInsightPopup");
  var popHeader  = $("rdInsightHeader");
  var popTitle   = $("rdInsightTitle");
  var popClose   = $("rdInsightClose");
  var popBody    = $("rdInsightBody");
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
    popBody.innerHTML="<div class='rdInsightLoading'><span class='rdInsightDot'></span><span class='rdInsightDot'></span><span class='rdInsightDot'></span><br>Generating desk insight\u2026</div>";
    popup.style.left=Math.min(x,window.innerWidth-460)+"px";
    popup.style.top=Math.min(y,window.innerHeight-300)+"px";
    popup.style.display="block";
  }

  var _lbl={
    setup_quality:"Setup Quality",entry_mechanics:"Entry Mechanics",indicator_confluence:"Indicator Confluence",alignment_check:"Alignment Check",
    gamma_environment:"Gamma Environment",directional_bias:"Directional Bias",mean_reversion_impact:"Mean-Reversion Impact",
    trend_read:"Trend Read",alignment_value:"Alignment Value",distance_context:"Distance Context",
    scan_read:"Scan Read",aplus_concentration:"A+ Concentration",directional_skew:"Directional Skew",
    gate_status:"Gate Status",regime_impact:"Regime Impact",vol_and_flow:"Vol & Flow",
    desk_takeaway:"Desk Takeaway",
  };

  function renderInsight(data){
    if(!data){popBody.innerHTML="<div class='rdInsightLoading'>No insight data.</div>";return;}
    var html="";
    if(data._fallback_reason) html+="<div style='background:rgba(255,107,107,.15);border:1px solid rgba(255,107,107,.3);border-radius:8px;padding:10px 12px;margin-bottom:14px;font-size:11px;color:#ff6b6b;'>"+escapeHtml(data._fallback_reason)+"</div>";
    var skip=new Set(["_source","_meta","_card_type","_fallback_reason"]);
    for(var key in data){
      if(skip.has(key))continue;
      var label=_lbl[key]||key.replace(/_/g," ").replace(/\b\w/g,function(c){return c.toUpperCase();});
      var isDesk=key==="desk_takeaway";
      html+="<div class='rdInsightSection'><div class='rdInsightSectionTitle'>"+escapeHtml(label)+"</div><div class='rdInsightText'"+(isDesk?" style='color:#34c759;font-weight:600;'":"")+">"+escapeHtml(String(data[key]))+"</div></div>";
    }
    if(data._source) html+="<div class='rdInsightSource'>Source: "+escapeHtml(data._source)+"</div>";
    popBody.innerHTML=html;
  }

  function fetchInsight(cardType,cardData,title,x,y){
    var cacheKey=cardType+":"+JSON.stringify(cardData).substring(0,100);
    if(_rdCache[cacheKey]){openPopup(title,x,y);renderInsight(_rdCache[cacheKey]);return;}
    openPopup(title,x,y);
    var ctx={};
    if(lastPayload){ctx.marketGamma=lastPayload.marketGamma||{};ctx.marketTrend=lastPayload.marketTrend||{};ctx.asOfDate=lastPayload.asOfDate;}
    fetch("/api/front-layer/card-insight",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({card_type:cardType,card_data:cardData,dms_summary:ctx})})
    .then(function(r){return r.json();})
    .then(function(resp){if(resp.error||resp.detail){popBody.innerHTML="<div class='rdInsightLoading' style='color:#ff6b6b;'>Error: "+escapeHtml(resp.error||resp.detail||"Unknown")+"</div>";return;}_rdCache[cacheKey]=resp;renderInsight(resp);})
    .catch(function(){popBody.innerHTML="<div class='rdInsightLoading' style='color:#ff6b6b;'>Failed to load insight.</div>";});
  }

  // ── Signal cards (A+ and Standard) ──
  var aplusGrid = $("aplusGrid");
  var standardGrid = $("standardGrid");
  function onCardClick(ev) {
    var card = ev.target.closest(".signalCard");
    if (!card || !lastPayload) return;
    // Don't trigger on position calculator buttons
    if (ev.target.closest("button, a, input")) return;
    var ticker = card.getAttribute("data-ticker");
    var allSignals = [].concat(lastPayload.aPlus || [], lastPayload.standard || []);
    var sig = allSignals.find(function(s) { return s.ticker === ticker; });
    if (!sig) return;
    ev.stopPropagation();
    fetchInsight("rd_signal", sig, "Red Dog: " + ticker + " (" + (sig.direction || "") + ")", ev.clientX, ev.clientY);
  }
  if (aplusGrid) aplusGrid.addEventListener("click", onCardClick);
  if (standardGrid) standardGrid.addEventListener("click", onCardClick);

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
