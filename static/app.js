function $(id) {
  return document.getElementById(id);
}

function fmtPct(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${Number(v).toFixed(2)}%`;
}

function fmtX(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${Number(v).toFixed(2)}×`;
}

function logoUrlForTicker(ticker) {
  const t = String(ticker || "").trim().toUpperCase();
  if (!t) return null;
  // FMP serves ticker logos from a stable static URL. No API key required.
  return `https://financialmodelingprep.com/image-stock/${encodeURIComponent(t)}.png`;
}

function setTickerLogo(ticker) {
  const img = $("tickerLogo");
  if (!img) return;
  const src = logoUrlForTicker(ticker);
  if (!src) {
    img.classList.add("hidden");
    img.removeAttribute("src");
    img.removeAttribute("alt");
    return;
  }
  img.src = src;
  img.alt = `${String(ticker || "").toUpperCase()} logo`;
  img.classList.remove("hidden");
  img.onerror = () => img.classList.add("hidden");
}

function fmtNum(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(2) : "—";
}

function fmtMoney(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const sign = n > 0 ? "-" : ""; // losses are positive; display as -$ for trader intuition
  const abs = Math.abs(n);
  return `${sign}$${abs.toFixed(2)}`;
}

function fmtSignedPct(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}%`;
}

function fmt0(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(0) : "—";
}

function _pickMeaningfulClusters(sideClusters, spot, strikeStep) {
  const xs = Array.isArray(sideClusters) ? sideClusters : [];
  if (!xs.length) return [];

  const top = xs[0];
  const topTotal = Number(top?.totalOI || 0);
  const topPeak = Number(top?.peakStrike ?? top?.maxStrike);
  const topDist = (Number.isFinite(spot) && Number.isFinite(topPeak)) ? Math.abs(topPeak - spot) : Number.POSITIVE_INFINITY;

  const out = [top];
  for (let i = 1; i < xs.length && out.length < 3; i++) {
    const c = xs[i];
    const total = Number(c?.totalOI || 0);
    const peak = Number(c?.peakStrike ?? c?.maxStrike);
    const dist = (Number.isFinite(spot) && Number.isFinite(peak)) ? Math.abs(peak - spot) : Number.POSITIVE_INFINITY;

    const bigEnough = (topTotal > 0) ? (total / topTotal) >= 0.6 : false;
    const closerToSpot = dist + 1e-9 < topDist;
    const separated = (Number.isFinite(strikeStep) && strikeStep > 0 && Number.isFinite(peak) && Number.isFinite(topPeak))
      ? Math.abs(peak - topPeak) >= 2 * strikeStep
      : false;

    if (bigEnough || closerToSpot || separated) out.push(c);
  }
  return out;
}

function _fmtClusterLine(c) {
  const peak = c?.peakStrike ?? c?.maxStrike;
  return `${fmt0(peak)} (${fmt0(c?.totalOI)}) · range=${fmt0(c?.minStrike)}–${fmt0(c?.maxStrike)} · n=${fmt0(c?.nStrikes)}`;
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
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

function pill(text, kind) {
  const cls = kind ? `pill ${kind}` : "pill";
  return `<span class="${cls}">${escapeHtml(text)}</span>`;
}

let lastPayload = null;
let isBusy = false;
let earningsExpanded = false;
let bufferModePref = null; // "symmetric" | "asymmetric" | null (auto)
let showAdvancedCols = false;
let tradeBuilderExpanded = false;
let mcEnabledPref = false;
let mcEventOverride = { date: null, timing: "AUTO" };

const tradeBuilderState = {
  mode: "auto", // auto | equal_delta | equal_premium
  symmetry: "auto", // auto | symmetric | manual
  target_delta: 0.10,
  target_premium: 0.50,
  wing_width: 5,
  put_mult: null,
  call_mult: null,
};

const NEAR_BREACH_THRESHOLD = 0.9;

async function fetchJson(url) {
  const res = await fetch(url, { headers: { "Accept": "application/json" } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = body?.detail || `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return body;
}

function recBadge(rec) {
  const r = String(rec || "");
  if (r === "Tight") return pill("Tight", "good");
  if (r === "Standard") return pill("Standard", "neutral");
  if (r === "Wide") return pill("Wide", "warn");
  if (r.startsWith("Avoid")) return pill(r, "bad");
  return escapeHtml(r || "—");
}

function regimeBadge(label, tm) {
  const l = String(label || "");
  const mult = (tm === null || tm === undefined) ? null : `${Number(tm).toFixed(2)}×`;
  let badge = null;
  if (l === "Stress") badge = pill("Stress", "bad");
  else if (l === "Elevated") badge = pill("Elevated", "warn");
  else if (l === "Calm") badge = pill("Calm", "neutral");
  else if (l === "Normal") badge = pill("Normal", "neutral");
  else badge = "—";
  const multSpan = mult ? `<span class="regimeMult mono">${escapeHtml(mult)}</span>` : "";
  return `<span class="regimeCellInner">${badge}${multSpan}</span>`;
}

function gateBadge(gate) {
  const g = String(gate || "");
  if (g === "NO_TRADE") return pill("No Trade", "bad");
  if (g === "CAUTION") return pill("Caution", "warn");
  if (g === "OK") return pill("OK", "neutral");
  return "—";
}

function fmtSignedPP(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const n = Number(v);
  const sign = n > 0 ? "+" : n < 0 ? "" : "";
  return `${sign}${n.toFixed(2)}pp`;
}

function deltaClass(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "";
  const n = Number(v);
  if (n > 0) return "pos";
  if (n < 0) return "neg";
  return "zero";
}

function _normRec(rec) {
  if (!rec) return null;
  const r = String(rec);
  if (r.startsWith("Avoid")) return "Avoid";
  return r;
}

function _fmtQuarterList(qs) {
  const order = ["Q1", "Q2", "Q3", "Q4"];
  const idx = qs.map(q => order.indexOf(q)).filter(i => i >= 0).sort((a, b) => a - b);
  const uniq = [...new Set(idx)].map(i => order[i]);
  if (uniq.length === 0) return "";
  if (uniq.length === 1) return uniq[0];
  // range if contiguous; else comma-separated
  const isContig = uniq.every((q, i) => i === 0 || order.indexOf(q) === order.indexOf(uniq[i - 1]) + 1);
  if (isContig) return `${uniq[0]}–${uniq[uniq.length - 1]}`;
  return uniq.join(", ");
}

function buildActionSummary(quarters) {
  const qs = quarters || {};
  const groups = { Tight: [], Standard: [], Wide: [], Avoid: [] };
  for (const q of ["Q1", "Q2", "Q3", "Q4"]) {
    const rec = _normRec(qs?.[q]?.recommendation);
    if (!rec) continue;
    if (groups[rec]) groups[rec].push(q);
  }

  const parts = [];
  if (groups.Tight.length) parts.push(`Tight wings favored in ${_fmtQuarterList(groups.Tight)}`);
  if (groups.Standard.length) parts.push(`Standard in ${_fmtQuarterList(groups.Standard)}`);
  if (groups.Wide.length) parts.push(`Wider wings in ${_fmtQuarterList(groups.Wide)}`);
  if (groups.Avoid.length) parts.push(`Avoid ${_fmtQuarterList(groups.Avoid)} due to tail risk`);
  return parts.length ? parts.join(" · ") : "—";
}

function _quarterFromDateStr(dateStr) {
  const s = String(dateStr || "");
  const m = Number(s.slice(5, 7));
  if (!m || Number.isNaN(m)) return null;
  const q = Math.floor((m - 1) / 3) + 1;
  return `Q${q}`;
}

function _baseWingFactorFromRec(rec) {
  const r = (String(rec || "Standard").startsWith("Avoid")) ? "Avoid" : String(rec || "Standard");
  if (r === "Tight") return 0.5;
  if (r === "Standard") return 1.0;
  if (r === "Wide") return 1.5;
  return null;
}

function buildBufferTarget(payload) {
  const rg = payload?.regime || {};
  const gate = rg?.guidance?.tradeGate || "OK";
  const asOf = rg?.asOfDate;
  const qk = _quarterFromDateStr(asOf) || null;
  const qRecRaw = qk ? payload?.quarters?.[qk]?.recommendation : null;
  const qRecStr = String(qRecRaw || "");
  const isAvoid = qRecStr.startsWith("Avoid");

  // Muted / no-trade display rules.
  if (gate === "NO_TRADE" || isAvoid) {
    return { primary: "—", secondary: null, subtext: "No trade", muted: true };
  }

  // Canonical "final" multiplier for strike selection is the model's base wing multiple
  // (quarter seasonality × regime tail multiplier). Asymmetry (put/call) is handled
  // separately via wingRecommendation.putWingMultiple / callWingMultiple.
  const wr = payload?.wingRecommendation || null;
  const baseWing = (wr && wr.baseWingMultiple !== null && wr.baseWingMultiple !== undefined)
    ? Number(wr.baseWingMultiple)
    : null;
  if (baseWing !== null && Number.isFinite(baseWing)) {
    return {
      primary: `~${baseWing.toFixed(2)}× EM`,
      secondary: null,
      subtext: "Symmetric base wing (quarter × regime).",
      muted: false,
    };
  }

  // Fallback if wingRecommendation is missing/incomplete: compute from quarter+regime inputs.
  const base = _baseWingFactorFromRec(qRecRaw);
  const tm = (rg.tailMultiplier === null || rg.tailMultiplier === undefined) ? null : Number(rg.tailMultiplier);
  if (base === null || tm === null || Number.isNaN(tm)) {
    return { primary: "—", secondary: null, subtext: "Unavailable: missing quarter/regime inputs.", muted: false };
  }
  const finalWing = Number(base) * Number(tm);
  if (!Number.isFinite(finalWing)) {
    return { primary: "—", secondary: null, subtext: "Unavailable: invalid quarter/regime inputs.", muted: false };
  }
  return {
    primary: `~${finalWing.toFixed(2)}× EM`,
    secondary: null,
    subtext: "Symmetric base wing (quarter × regime).",
    muted: false,
  };
}

function _wingRecLabel(label) {
  const l = String(label || "");
  if (l === "WIDEN_PUTS_TIGHTEN_CALLS") return "Widen puts / tighten calls";
  if (l === "WIDEN_CALLS_TIGHTEN_PUTS") return "Widen calls / tighten puts";
  if (l === "SYMMETRIC") return "Symmetric";
  if (l === "NO_TRADE") return "No trade (gate)";
  return l || "—";
}

function renderBufferTarget(payload) {
  const wing = payload?.wingRecommendation || null;
  const hasAsymNumbers = wing && wing.putWingMultiple !== null && wing.putWingMultiple !== undefined
    && wing.callWingMultiple !== null && wing.callWingMultiple !== undefined;
  const canDefaultAsym = hasAsymNumbers && String(wing.confidence || "") !== "LOW";

  const symBtn = $("bufferModeSymmetric");
  const asymBtn = $("bufferModeAsymmetric");
  if (asymBtn) asymBtn.disabled = !hasAsymNumbers;

  let mode = bufferModePref;
  if (!mode) mode = canDefaultAsym ? "asymmetric" : "symmetric";
  if (mode === "asymmetric" && !hasAsymNumbers) mode = "symmetric";

  if (symBtn) symBtn.classList.toggle("isActive", mode === "symmetric");
  if (asymBtn) asymBtn.classList.toggle("isActive", mode === "asymmetric");

  const bt = buildBufferTarget(payload);
  const btGrid = $("bufferTargetGrid");
  const btSub = $("bufferTargetSubtext");
  const btCard = $("bufferTargetCard");

  if (btGrid) {
    if (bt.muted) {
      btGrid.innerHTML = `<div class="k">Target</div><div class="v mono">—</div>`;
    } else if (mode === "asymmetric" && hasAsymNumbers) {
      btGrid.innerHTML = `
        <div class="k">Put</div><div class="v mono">~${Number(wing.putWingMultiple).toFixed(2)}× EM</div>
        <div class="k">Call</div><div class="v mono">~${Number(wing.callWingMultiple).toFixed(2)}× EM</div>
      `;
      if (btSub) btSub.textContent = "Final wing multipliers (includes TAS asymmetry). Multiply by EM to target short strikes.";
    } else {
      btGrid.innerHTML = `<div class="k">Target</div><div class="v mono">${escapeHtml(bt.primary)}</div>`;
      if (btSub) btSub.textContent = "Final symmetric wing multiplier. Multiply by EM to target short strikes.";
    }
  }
  if (btSub && !btSub.textContent) btSub.textContent = bt.subtext;
  if (btCard) btCard.classList.toggle("isMuted", !!bt.muted);
}

function renderSkewWings(payload) {
  const sec = $("skewWingsSection");
  if (!sec) return;

  const s = payload?.summary || {};
  const wr = payload?.wingRecommendation || null;
  const sk = payload?.skewOverlay?.current || null;

  const hasDirectional =
    s.upBreachRatePct !== undefined ||
    s.downBreachRatePct !== undefined ||
    s.avgUpOvershootPct !== undefined ||
    s.avgDownOvershootPct !== undefined;

  const dirCard = $("directionalTailCard");
  const dirGrid = $("directionalTailGrid");
  const dirFoot = $("directionalTailFootnote");

  if (dirCard) dirCard.classList.toggle("hidden", !hasDirectional);
  if (hasDirectional && dirGrid) {
    const tailBias = s.tailBias || "—";
    const biasBadge =
      tailBias === "DOWN" ? pill("DOWN", "bad") : tailBias === "UP" ? pill("UP", "warn") : pill("NEUTRAL", "neutral");
    dirGrid.innerHTML = `
      <div class="k">Down breach rate</div><div class="v mono">${fmtPct(s.downBreachRatePct)}</div>
      <div class="k">Up breach rate</div><div class="v mono">${fmtPct(s.upBreachRatePct)}</div>
      <div class="k">Avg overshoot (Down)</div><div class="v mono">${fmtPct(s.avgDownOvershootPct)}</div>
      <div class="k">Avg overshoot (Up)</div><div class="v mono">${fmtPct(s.avgUpOvershootPct)}</div>
      <div class="k">Tail bias</div><div class="v">${biasBadge}</div>
    `;
  }
  if (hasDirectional && dirFoot) {
    dirFoot.textContent = `Based on usable events (n=${s.events_used ?? "—"}).`;
  }

  const skewCard = $("skewSnapshotCard");
  const skewGrid = $("skewSnapshotGrid");
  const skewNotes = $("skewSnapshotNotes");
  const hasSkew = !!sk;
  if (skewCard) skewCard.classList.toggle("hidden", !hasSkew);
  if (hasSkew && skewGrid) {
    const q = String(sk.skewQuality || "MISSING");
    const qBadge =
      q === "OK" ? pill("OK", "good") : q === "PARTIAL" ? pill("PARTIAL", "warn") : pill("MISSING", "neutral");
    const rr25 = sk.rr25;
    const rrLabel = rr25 === null || rr25 === undefined || Number.isNaN(rr25) ? "—" : `${Number(rr25).toFixed(4)}`;
    const rrHint = rr25 < 0 ? "Put skew (downside priced)" : rr25 > 0 ? "Call skew (upside priced)" : "—";
    skewGrid.innerHTML = `
      <div class="k">RR25</div><div class="v mono">${escapeHtml(rrLabel)}</div>
      <div class="k">As of</div><div class="v mono">${escapeHtml(sk.asOfDate || "—")}</div>
      <div class="k">Quality</div><div class="v">${qBadge}</div>
      <div class="k">Read</div><div class="v muted">${escapeHtml(rrHint)}</div>
    `;
  }
  if (hasSkew && skewNotes) {
    if (String(sk.skewQuality || "") === "MISSING") skewNotes.textContent = sk.notes || "Skew unavailable";
    else skewNotes.textContent = sk.notes || "—";
  }

  const wingCard = $("wingBuilderCard");
  const wingGrid = $("wingBuilderGrid");
  const wingWhy = $("wingBuilderRationale");
  const hasWing = !!wr;

  if (wingCard) wingCard.classList.toggle("hidden", !hasWing);
  if (hasWing && wingGrid) {
    const conf = String(wr.confidence || "—");
    const confBadge =
      conf === "HIGH" ? pill("HIGH", "good") : conf === "MED" ? pill("MED", "warn") : pill("LOW", "neutral");
    const mode = String(wr.structureMode || "");
    const modeTxt =
      mode === "AUTO_EQUAL_PREMIUM" ? "Auto: Equal Premium" : mode === "AUTO_EQUAL_DELTA" ? "Auto: Equal Delta" : (mode || "—");
    wingGrid.innerHTML = `
      <div class="k">Recommendation</div><div class="v">${escapeHtml(_wingRecLabel(wr.recommendationLabel))}</div>
      <div class="k">Structure mode</div><div class="v">${escapeHtml(modeTxt)}</div>
      <div class="k">Confidence</div><div class="v">${confBadge}</div>
      <div class="k">TAS</div><div class="v mono">${wr.tas !== null && wr.tas !== undefined ? Number(wr.tas).toFixed(3) : "—"}</div>
      <div class="k">Base wing</div><div class="v mono">${wr.baseWingMultiple !== null && wr.baseWingMultiple !== undefined ? `${Number(wr.baseWingMultiple).toFixed(2)}× EM` : "—"}</div>
      <div class="k">Put wing</div><div class="v mono">${wr.putWingMultiple !== null && wr.putWingMultiple !== undefined ? `${Number(wr.putWingMultiple).toFixed(2)}× EM` : "—"}</div>
      <div class="k">Call wing</div><div class="v mono">${wr.callWingMultiple !== null && wr.callWingMultiple !== undefined ? `${Number(wr.callWingMultiple).toFixed(2)}× EM` : "—"}</div>
    `;
  }

  const gate = (payload?.regime?.guidance || {})?.tradeGate;
  const isNoTrade = gate === "NO_TRADE";
  if (wingCard) wingCard.classList.toggle("isMuted", isNoTrade);
  if (hasWing && wingWhy) {
    if (isNoTrade) wingWhy.textContent = "No Trade (Regime Gate).";
    else wingWhy.textContent = (wr.structureRationale || wr.rationale || "—");
  }

  const meta = $("skewWingsMeta");
  if (meta) meta.textContent = (hasDirectional || hasWing || hasSkew) ? "Directional tails + skew snapshot + wing multipliers" : "—";

  // Hide entire section if nothing to show (graceful degradation).
  const showSection = hasDirectional || hasWing || hasSkew;
  sec.classList.toggle("hidden", !showSection);
}

function renderMonteCarlo(payload) {
  const sec = $("mcSection");
  if (!sec) return;
  const mc = payload?.monteCarlo || null;
  const ne = payload?.nextEvent || null;
  const opt = payload?.monteCarloOptimization || null;

  const hasAnyMc = !!mc;
  const hasMc = !!(mc && mc.nSims && Number(mc.nSims) > 0);
  const show = mcEnabledPref || hasAnyMc;
  sec.classList.toggle("hidden", !show);
  if (!show) return;

  const meta = $("mcMeta");
  const note = $("mcNote");
  const bEither = $("mcBreachEither");
  const bBreak = $("mcBreachBreakout");
  const expLoss = $("mcExpLoss");
  const cvar = $("mcCvar");
  const wingOpt = $("mcWingOpt");
  const wingOptNote = $("mcWingOptNote");

  const nSims = hasAnyMc ? Number(mc.nSims || 0) : 0;
  const cond = hasAnyMc ? (mc?.pool?.conditioningUsed || "—") : "—";
  const poolN = hasAnyMc ? (mc?.pool?.sizeUsed ?? "—") : "—";

  const anchorDate = ne?.pricingDatePlanned || "—";
  const anchorImp = ne?.impliedMovePctPlanned;
  const anchorTxt = (anchorImp !== null && anchorImp !== undefined) ? `${Number(anchorImp).toFixed(2)}%` : "—";

  const src = ne?.source ? String(ne.source) : null;
  const conf = ne?.confidence ? String(ne.confidence) : null;
  const prov = src ? ` · src=${src}${conf ? `/${conf}` : ""}` : "";
  if (meta) meta.textContent = hasAnyMc ? `n=${nSims} · conditioning=${cond} · pool=${poolN} · anchor=${anchorDate} (${anchorTxt})${prov}` : "MC requested (waiting for data)…";

  const notes = [];
  if (hasAnyMc && Array.isArray(mc.notes)) notes.push(...mc.notes);
  if (ne && Array.isArray(ne.notes) && ne.notes.length) notes.push(...ne.notes.map(s => `Next event: ${s}`));
  if (ne && (ne.rawTime || ne.dateConfirmed !== null && ne.dateConfirmed !== undefined)) {
    const rt = ne.rawTime ? `time=${ne.rawTime}` : null;
    const dc = (ne.dateConfirmed === true) ? "date_confirmed=1" : (ne.dateConfirmed === false) ? "date_confirmed=0" : null;
    const bits = [rt, dc].filter(Boolean).join(", ");
    if (bits) notes.push(`Next event meta: ${bits}.`);
  }
  if (!hasAnyMc) notes.push("MC unavailable: server did not return monteCarlo output. Ensure MC toggle is on and backend supports mc=1.");
  if (note) note.textContent = notes.length ? notes.join(" ") : "Simulates close→open earnings gap only (no intraday path).";

  if (!hasMc) {
    if (bEither) bEither.textContent = "—";
    if (bBreak) bBreak.textContent = "Put — · Call —";
    if (expLoss) expLoss.textContent = "—";
    if (cvar) cvar.textContent = "—";
    if (wingOpt) wingOpt.textContent = "—";
    if (wingOptNote) wingOptNote.textContent = "MC enabled, but no simulation results available.";
    return;
  }

  const bp = mc?.breachProb || {};
  const either = Number(bp.either);
  const put = Number(bp.put);
  const call = Number(bp.call);
  function fmtProbPct(p) {
    if (!Number.isFinite(p)) return "—";
    const pct = p * 100;
    // If we observed zero breaches, show an empirical upper bound if available.
    if (pct === 0) {
      const ub = mc?.diagnostics?.breachProbUpperBoundPct;
      const ubn = Number(ub);
      if (Number.isFinite(ubn) && ubn > 0) return `<${ubn.toFixed(2)}%`;
    }
    if (pct > 0 && pct < 0.1) return "<0.1%";
    return `${pct.toFixed(1)}%`;
  }
  if (bEither) bEither.textContent = fmtProbPct(either);
  if (bBreak) bBreak.textContent = `Put ${fmtProbPct(put)} · Call ${fmtProbPct(call)}`;

  const el = mc?.expectedLoss || {};
  if (expLoss) expLoss.textContent = fmtMoney(el.total);

  const cv = mc?.cvar95 || {};
  if (cvar) cvar.textContent = fmtMoney(cv.total);

  if (!opt) {
    if (wingOpt) wingOpt.textContent = "—";
    if (wingOptNote) wingOptNote.textContent = "Optimization not enabled.";
    return;
  }

  const mode = String(opt.mode || "");
  const pm = opt.optimalPutMultiple;
  const cm = opt.optimalCallMultiple;
  if (pm !== null && pm !== undefined && cm !== null && cm !== undefined) {
    if (wingOpt) wingOpt.textContent = `Put ${Number(pm).toFixed(2)}× · Call ${Number(cm).toFixed(2)}×`;
  } else {
    if (wingOpt) wingOpt.textContent = "—";
  }
  const optNotes = Array.isArray(opt.notes) ? opt.notes.join(" ") : "";
  if (wingOptNote) wingOptNote.textContent = mode ? `${mode}. ${optNotes}` : (optNotes || "—");
}

function _eventRiskBadge(label) {
  const l = String(label || "");
  if (l === "HIGH") return pill("HIGH", "bad");
  if (l === "MED") return pill("MED", "warn");
  if (l === "LOW") return pill("LOW", "neutral");
  return escapeHtml(l || "—");
}

function renderEventRisk(payload) {
  const sec = $("eventRiskSection");
  if (!sec) return;
  const er = payload?.eventRisk || null;
  const show = !!(er && (er.enabled || er.score01 !== null && er.score01 !== undefined));
  sec.classList.toggle("hidden", !show);
  if (!show) return;

  const meta = $("eventRiskMeta");
  const scoreEl = $("eventRiskScore");
  const labelEl = $("eventRiskLabel");
  const driversEl = $("eventRiskDrivers");
  const notesEl = $("eventRiskNotes");
  const impactEl = $("eventRiskImpact");
  const impactNotesEl = $("eventRiskImpactNotes");
  const explainEl = $("eventRiskExplain");

  const asOf = er?.asOfDate || "—";
  const anchor = er?.earnDateNext || "—";
  const w = er?.window || null;
  const wTxt = w ? `${w.start || "—"}→${w.end || "—"}` : "—";
  if (meta) meta.textContent = `asOf=${asOf} · anchor=${anchor} · window=${wTxt}`;

  const s = (er?.score01 === null || er?.score01 === undefined) ? null : Number(er.score01);
  if (scoreEl) scoreEl.textContent = (s !== null && Number.isFinite(s)) ? `${s.toFixed(3)}` : "—";
  if (labelEl) {
    const badge = _eventRiskBadge(er?.label);
    labelEl.innerHTML = `${badge} <span class="eventRiskMuted">Low &lt;0.33 · Med &lt;0.66 · High ≥0.66</span>`;
  }
  if (explainEl) {
    explainEl.textContent = "Composite score (0–1) summarizing near-term event risk around the earnings window. Use it as a guardrail, not a signal.";
  }

  const c = er?.components || {};
  const macro = c?.macroProximity || {};
  const head = c?.headlineShock || {};
  const rat = c?.analystCluster || {};
  const opt = c?.optionsActivity || {};

  const macroN = macro.countHighImpactUS ?? "—";
  const macroTop = Array.isArray(macro.top) ? macro.top : [];

  const newsN = head.newsCount3d ?? "—";
  const wiimN = head.wiimCount3d ?? "—";
  const ratN = rat.ratingsCount7d ?? "—";
  const ratActions = Array.isArray(rat.actions) ? rat.actions.filter(Boolean) : [];
  const optN = opt.signalsCount3d ?? "—";

  const driverItems = [];
  driverItems.push(`<li><strong>Macro</strong>: ${escapeHtml(String(macroN))} high-impact US events (importance ≥3) in window${macroTop.length ? `.<br/><span class="eventRiskMuted">${escapeHtml(macroTop.join(" · "))}</span>` : ""}</li>`);
  driverItems.push(`<li><strong>News</strong>: ${escapeHtml(String(newsN))} headlines (3d) · <strong>WIIM</strong>: ${escapeHtml(String(wiimN))} (3d)</li>`);
  driverItems.push(`<li><strong>Analyst ratings</strong>: ${escapeHtml(String(ratN))} (7d)${ratActions.length ? `.<br/><span class="eventRiskMuted">${escapeHtml(ratActions.join(" · "))}</span>` : ""}</li>`);
  driverItems.push(`<li><strong>Unusual options</strong>: ${escapeHtml(String(optN))} signals (3d)</li>`);
  if (driversEl) driversEl.innerHTML = `<ul class="eventRiskList">${driverItems.join("")}</ul>`;

  const notes = [];
  if (Array.isArray(er?.notes) && er.notes.length) notes.push(...er.notes);
  if (Array.isArray(er?.sources) && er.sources.length) notes.push(`Sources: ${er.sources.join(", ")}`);
  if (notesEl) notesEl.textContent = notes.length ? notes.join(" ") : "—";

  // Impact this run: show whether it actually adjusted regime/MC.
  const impacts = [];
  const rgAdj = payload?.regime?.eventRiskAdjustment || null;
  if (rgAdj && rgAdj.enabled) {
    const bump = rgAdj.tailMultiplierBumpPct;
    const gate = payload?.regime?.guidance?.tradeGate || payload?.regime?.tradeGate || "—";
    impacts.push(`Regime: +${Number(bump || 0).toFixed(1)}% tail · gate=${gate}`);
  } else {
    impacts.push("Regime: no adjustment");
  }
  const mc = payload?.monteCarlo || null;
  const mcNotes = Array.isArray(mc?.notes) ? mc.notes.join(" ") : "";
  if (mcNotes.includes("Event-risk widening applied")) impacts.push("MC: wing widening applied");
  else impacts.push("MC: no widening");
  if (impactEl) impactEl.innerHTML = `<div class="eventRiskKicker">${escapeHtml(impacts.join(" · "))}</div>`;

  const impactNotes = [];
  if (rgAdj && rgAdj.enabled && rgAdj.thresholds) {
    impactNotes.push(`Thresholds: caution≥${rgAdj.thresholds.caution} · high≥${rgAdj.thresholds.high}.`);
  }
  if (impactNotesEl) impactNotesEl.textContent = impactNotes.length ? impactNotes.join(" ") : "—";
}

function _bestUnderlyingPrice(payload) {
  const tb = payload?.tradeBuilder;
  if (tb && tb.underlyingPrice !== null && tb.underlyingPrice !== undefined) {
    const p = Number(tb.underlyingPrice);
    if (Number.isFinite(p) && p > 0) return p;
  }
  const cur = payload?.current;
  if (cur && cur.stockPrice !== null && cur.stockPrice !== undefined) {
    const p = Number(cur.stockPrice);
    if (Number.isFinite(p) && p > 0) return p;
  }
  const events = Array.isArray(payload?.events) ? payload.events : [];
  for (const e of events) {
    const px = Number(e?.closePx);
    if (Number.isFinite(px) && px > 0) return px;
  }
  return null;
}

function _bestImpliedMovePct(payload) {
  const cur = payload?.current;
  if (cur && cur.impliedMovePct !== null && cur.impliedMovePct !== undefined) {
    const imp = Number(cur.impliedMovePct);
    if (Number.isFinite(imp) && imp > 0) return imp;
  }
  const events = Array.isArray(payload?.events) ? payload.events : [];
  for (const e of events) {
    const imp = Number(e?.impliedMovePct);
    if (Number.isFinite(imp) && imp > 0) return imp;
  }
  const s = payload?.summary || {};
  const avg = Number(s.avg_implied_all_pct);
  return Number.isFinite(avg) && avg > 0 ? avg : null;
}

function renderTradeBuilder(payload) {
  const panel = $("tradeBuilderPanel");
  if (!panel || panel.classList.contains("hidden")) return;

  const putOut = $("tbPutOut");
  const callOut = $("tbCallOut");
  const notes = $("tradeBuilderNotes");

  const tb = payload?.tradeBuilder || null;
  const hasStrikes = !!(tb?.put?.shortStrike && tb?.call?.shortStrike);

  if (hasStrikes) {
    const put = tb.put || {};
    const call = tb.call || {};
    const price = Number(tb.underlyingPrice);
    const credit = tb.totalCredit;
    if (putOut) {
      putOut.innerHTML = `
        <div class="k">Short strike</div><div class="v mono">${put.shortStrike ?? "—"}</div>
        <div class="k">Long strike</div><div class="v mono">${put.longStrike ?? "—"}</div>
        <div class="k">Short delta</div><div class="v mono">${put.shortDelta !== null && put.shortDelta !== undefined ? Number(put.shortDelta).toFixed(3) : "—"}</div>
        <div class="k">Short mid</div><div class="v mono">${put.shortMid !== null && put.shortMid !== undefined ? `$${Number(put.shortMid).toFixed(2)}` : "—"}</div>
        <div class="k">Wing credit</div><div class="v mono">${put.credit !== null && put.credit !== undefined ? `$${Number(put.credit).toFixed(2)}` : "—"}</div>
      `;
    }
    if (callOut) {
      callOut.innerHTML = `
        <div class="k">Short strike</div><div class="v mono">${call.shortStrike ?? "—"}</div>
        <div class="k">Long strike</div><div class="v mono">${call.longStrike ?? "—"}</div>
        <div class="k">Short delta</div><div class="v mono">${call.shortDelta !== null && call.shortDelta !== undefined ? Number(call.shortDelta).toFixed(3) : "—"}</div>
        <div class="k">Short mid</div><div class="v mono">${call.shortMid !== null && call.shortMid !== undefined ? `$${Number(call.shortMid).toFixed(2)}` : "—"}</div>
        <div class="k">Wing credit</div><div class="v mono">${call.credit !== null && call.credit !== undefined ? `$${Number(call.credit).toFixed(2)}` : "—"}</div>
      `;
    }
    const noteLines = Array.isArray(tb.notes) ? tb.notes : [];
    const head = credit !== null && credit !== undefined ? `Estimated total credit: $${Number(credit).toFixed(2)}.` : "Credit unavailable.";
    const mid = (Number.isFinite(price) && price > 0) ? `Assumed price $${price.toFixed(2)}.` : "";
    if (notes) notes.textContent = [head, mid, `Expiration ${tb.expiration || "—"}.`, ...noteLines].filter(Boolean).join(" ");
    return;
  }

  const price = _bestUnderlyingPrice(payload);
  const impPct = _bestImpliedMovePct(payload);
  const wr = payload?.wingRecommendation || null;
  const gate = (payload?.regime?.guidance || {})?.tradeGate;

  const baseMult = (wr && wr.baseWingMultiple !== null && wr.baseWingMultiple !== undefined) ? Number(wr.baseWingMultiple) : null;
  const recPut = (wr && wr.putWingMultiple !== null && wr.putWingMultiple !== undefined) ? Number(wr.putWingMultiple) : null;
  const recCall = (wr && wr.callWingMultiple !== null && wr.callWingMultiple !== undefined) ? Number(wr.callWingMultiple) : null;

  let putMult = baseMult;
  let callMult = baseMult;
  if (tradeBuilderState.symmetry === "auto") {
    if (recPut !== null && recCall !== null && String(wr?.confidence || "") !== "LOW") {
      putMult = recPut;
      callMult = recCall;
    }
  } else if (tradeBuilderState.symmetry === "symmetric") {
    putMult = baseMult;
    callMult = baseMult;
  } else if (tradeBuilderState.symmetry === "manual") {
    const pm = Number(tradeBuilderState.put_mult);
    const cm = Number(tradeBuilderState.call_mult);
    if (Number.isFinite(pm) && pm > 0) putMult = pm;
    if (Number.isFinite(cm) && cm > 0) callMult = cm;
  }

  if (!price || !impPct || !putMult || !callMult) {
    if (putOut) putOut.innerHTML = `<div class="k">Target</div><div class="v">—</div>`;
    if (callOut) callOut.innerHTML = `<div class="k">Target</div><div class="v">—</div>`;
    if (notes) notes.textContent = "Insufficient data to compute distance targets (need price + implied move + wing multipliers).";
    return;
  }

  const em$ = price * (impPct / 100.0);
  const putDist$ = em$ * putMult;
  const callDist$ = em$ * callMult;
  const width$ = Number(tradeBuilderState.wing_width) || 5;

  const putShort = price - putDist$;
  const putLong = putShort - width$;
  const callShort = price + callDist$;
  const callLong = callShort + width$;

  const putDistPct = (putDist$ / price) * 100.0;
  const callDistPct = (callDist$ / price) * 100.0;

  if (putOut) {
    putOut.innerHTML = `
      <div class="k">Target distance</div><div class="v mono">$${putDist$.toFixed(2)} (${putDistPct.toFixed(2)}%)</div>
      <div class="k">Short strike (est.)</div><div class="v mono">${putShort.toFixed(2)}</div>
      <div class="k">Long strike (width)</div><div class="v mono">${putLong.toFixed(2)} (−$${width$.toFixed(0)})</div>
      <div class="k">Wing multiple used</div><div class="v mono">${putMult.toFixed(2)}× EM</div>
    `;
  }
  if (callOut) {
    callOut.innerHTML = `
      <div class="k">Target distance</div><div class="v mono">$${callDist$.toFixed(2)} (${callDistPct.toFixed(2)}%)</div>
      <div class="k">Short strike (est.)</div><div class="v mono">${callShort.toFixed(2)}</div>
      <div class="k">Long strike (width)</div><div class="v mono">${callLong.toFixed(2)} (+$${width$.toFixed(0)})</div>
      <div class="k">Wing multiple used</div><div class="v mono">${callMult.toFixed(2)}× EM</div>
    `;
  }

  const extra = gate === "NO_TRADE" ? "No Trade (Regime Gate)." : "Chain-based strike selection not enabled; showing distance targets only.";
  const cur = payload?.current || null;
  const src = cur?.source ? `source=${cur.source}` : "";
  const asOf = cur?.asOfDate ? `asOf=${cur.asOfDate}` : "";
  const meta = [src, asOf].filter(Boolean).join(", ");
  if (notes) notes.textContent = `${extra} Assumed price $${price.toFixed(2)}; implied move ${impPct.toFixed(2)}%.${meta ? ` (${meta})` : ""}`;
}

function renderEarningsTable(events) {
  const tbody = $("tbody");
  tbody.innerHTML = "";
  const table = document.querySelector(".dataTable");
  if (table) table.classList.toggle("showAdvanced", !!showAdvancedCols);

  const rows = Array.isArray(events) ? events : [];
  const shown = earningsExpanded ? rows : rows.slice(0, 3);

  for (const e of shown) {
    const breach = e.breach;
    let breachCell = "—";
    if (breach === true) breachCell = `<span class="breachCell"><span class="breachDot breachDot--bad" title="Breach"></span><span class="breachLabel">Breach</span></span>`;
    if (breach === false) breachCell = `<span class="breachCell"><span class="breachDot breachDot--good" title="No breach"></span><span class="breachLabel">No</span></span>`;

    const notes = Array.isArray(e.notes) ? e.notes.join("; ") : "";
    const implied = e.impliedMovePct;
    const realized = e.realizedMovePct;
    const ratio = (implied && implied > 0 && realized !== null && realized !== undefined) ? (Number(realized) / Number(implied)) : null;
    const rge = e.regimeAtEvent || null;
    const regCell = rge ? regimeBadge(rge.label, rge.tailMultiplier) : "—";
    const gateCell = rge ? gateBadge(rge.tradeGate) : "—";

    const tr = document.createElement("tr");
    if (breach === true) tr.classList.add("row--breach");
    else if (ratio !== null && ratio >= NEAR_BREACH_THRESHOLD) tr.classList.add("row--near");

    tr.innerHTML = `
      <td>${escapeHtml(e.earnDate ?? "")}</td>
      <td class="ref">${escapeHtml(e.anncTod ?? "")}</td>
      <td class="ref">${escapeHtml(e.timing ?? "")}</td>
      <td class="ref">${escapeHtml(e.pricingDateUsed ?? "")}</td>
      <td class="num">${escapeHtml(e.impErnMv ?? "")}</td>
      <td class="num">${fmtPct(e.impliedMovePct)}</td>
      <td>${escapeHtml(e.closeDateUsed ?? "")}</td>
      <td class="num">${fmtNum(e.closePx)}</td>
      <td>${escapeHtml(e.openDateUsed ?? "")}</td>
      <td class="num">${fmtNum(e.openPx)}</td>
      <td class="num">${fmtPct(e.realizedMovePct)}</td>
      <td class="num advCol">${fmtSignedPct(e.signedMovePct)}</td>
      <td class="advCol">${escapeHtml(e.breachSide ?? "—")}</td>
      <td class="num advCol">${fmtPct(e.upOvershootPct ?? e.downOvershootPct)}</td>
      <td>${breachCell}</td>
      <td class="regimeCell">${regCell}</td>
      <td class="gateCell">${gateCell}</td>
      <td class="num">${fmtPct(e.aboveBreachPct)}</td>
      <td class="muted">${escapeHtml(notes)}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderQuarterCards(quarters) {
  const host = $("quarterCards");
  if (!host) return;
  host.innerHTML = "";
  const qs = quarters || {};

  for (const q of ["Q1", "Q2", "Q3", "Q4"]) {
    const row = qs[q] || {};
    const rec = row.recommendation;
    const season = row.seasonality || {};
    const breachDeltaPP = season.breach_delta_pp;
    const avgRatio = row.avg_ratio_realized_to_implied;
    const maxRatio = row.max_ratio_realized_to_implied;

    const tooltipParts = [];
    if (season.ratio_delta !== null && season.ratio_delta !== undefined) tooltipParts.push(`ratio_delta: ${season.ratio_delta}`);
    if (season.overshoot_delta_pp !== null && season.overshoot_delta_pp !== undefined) tooltipParts.push(`overshoot_delta_pp: ${season.overshoot_delta_pp}`);
    if (season.z_breach !== null && season.z_breach !== undefined) tooltipParts.push(`z_breach: ${season.z_breach}`);
    const tooltip = tooltipParts.length ? tooltipParts.join(" | ") : "—";

    const card = document.createElement("div");
    card.className = "quarterCard";
    card.innerHTML = `
      <div class="quarterTop">
        <div class="quarterHeading">
          <div class="quarterLabel">${escapeHtml(q)}</div>
          <div>${recBadge(rec)}</div>
        </div>
      </div>
      <div class="kv">
        <div class="k">Breach Δ vs baseline</div>
        <div class="v" title="${escapeHtml(tooltip)}"><span class="delta ${deltaClass(breachDeltaPP)}">${escapeHtml(fmtSignedPP(breachDeltaPP))}</span></div>
        <div class="k">Avg realized / implied</div>
        <div class="v mono">${escapeHtml(fmtX(avgRatio))}</div>
        <div class="k">Max realized / implied</div>
        <div class="v mono">${escapeHtml(fmtX(maxRatio))}</div>
      </div>
    `;
    host.appendChild(card);
  }
}

function render(payload) {
  $("results").classList.remove("hidden");
  lastPayload = payload;
  earningsExpanded = false;
  const toggle = $("earningsToggle");
  if (toggle) toggle.textContent = "Show earnings history";

  const s = payload.summary || {};
  const rate = s.breach_rate_pct;
  $("breachRate").textContent = fmtPct(rate);
  $("breachMeta").textContent = `${s.breaches || 0} breaches / ${s.events_used || 0} usable (found ${s.events_found || 0})`;

  $("avgAbove").textContent = fmtPct(s.avg_above_breach_pct);

  const b = payload.baseline || {};
  $("avgRatio").textContent = fmtX(b.avg_ratio_realized_to_implied);
  $("eventsUsed").textContent = `${s.events_used ?? "—"} / ${s.events_found ?? "—"}`;

  const rg = payload.regime || {};
  const asOf = $("regimeAsOf");
  if (asOf) asOf.textContent = rg.asOfDate ? `Latest ORATS EOD: ${rg.asOfDate}` : "—";
  const rl = $("regimeLabel");
  if (rl) rl.textContent = rg.label || "—";
  const tm = $("tailMultiplier");
  if (tm) tm.textContent = (rg.tailMultiplier === null || rg.tailMultiplier === undefined) ? "—" : `${Number(rg.tailMultiplier).toFixed(2)}×`;
  const tg = $("tradeGate");
  if (tg) {
    const gate = rg?.guidance?.tradeGate;
    if (gate === "NO_TRADE") tg.innerHTML = pill("No Trade", "bad");
    else if (gate === "CAUTION") tg.innerHTML = pill("Caution", "warn");
    else if (gate === "OK") tg.innerHTML = pill("OK", "neutral");
    else tg.textContent = "—";
  }
  // Market dealer gamma (live, informational only)
  const mgMain = $("marketGammaMain");
  const mgNote = $("marketGammaNote");
  const mgOi = $("marketOiNote");
  const mg = payload?.marketDealerGamma || null;
  if (mgMain && mgNote && mgOi) {
    const dg = mg?.dealerGamma || null;
    const oi = mg?.oiClusters || null;
    const enabled = !!(mg && mg.enabled && dg && dg.netGammaSign);
    if (!enabled) {
      mgMain.textContent = "—";
      const notes = Array.isArray(mg?.notes) ? mg.notes.filter(Boolean) : [];
      const warn = Array.isArray(mg?.warnings) ? mg.warnings.filter(Boolean) : [];
      mgNote.textContent = (notes[0] || warn[0] || "Live context unavailable (market closed, entitlement gap, or no live chain).");
      mgOi.textContent = "—";
    } else {
      const sign = String(dg.netGammaSign || "—");
      const bucket = String(dg.magnitudeBucket || "—");
      const exp = String(mg.expiry || "—");
      const sym = String(mg.symbolUsed || "SPX");
      mgMain.textContent = `${sym.toUpperCase()} · ${sign.toUpperCase()} · ${bucket.toUpperCase()}`;
      const warn = mg.warning ? ` ${String(mg.warning)}` : "";
      mgNote.textContent = `expiry=${exp} · band=±${Math.round(Number(dg.bandPct || 0.05) * 100)}% · weighting=${String(dg.weightingMode || "—")}.${warn}`;
      const putWall = oi && typeof oi === "object" ? oi.putWall : null;
      const callWall = oi && typeof oi === "object" ? oi.callWall : null;
      const putStrike = putWall && (putWall.peakStrike ?? putWall.maxStrike);
      const callStrike = callWall && (callWall.peakStrike ?? callWall.maxStrike);
      const putTxt = putWall && Number.isFinite(Number(putStrike)) ? `${Number(putStrike).toFixed(0)} (${Number(putWall.totalOI || 0).toFixed(0)})` : "—";
      const callTxt = callWall && Number.isFinite(Number(callStrike)) ? `${Number(callStrike).toFixed(0)} (${Number(callWall.totalOI || 0).toFixed(0)})` : "—";
      mgOi.textContent = `OI walls: put=${putTxt} | call=${callTxt}`;
    }
  }

  // Ticker dealer gamma (live, informational only)
  const tgCard = $("tickerGammaCard");
  const tgLabel = $("tickerGammaLabel");
  const tgMain = $("tickerGammaMain");
  const tgNote = $("tickerGammaNote");
  const tgOi = $("tickerOiNote");
  const tgd = payload?.tickerDealerGamma || null;
  if (tgLabel) tgLabel.textContent = `${String(payload?.ticker || "Ticker").toUpperCase()} Dealer Gamma (Live)`;
  if (tgCard && tgMain && tgNote && tgOi) {
    const dg = tgd?.dealerGamma || null;
    const oi = tgd?.oiClusters || null;
    const enabled = !!(tgd && tgd.enabled && dg && dg.netGammaSign);
    if (!enabled) {
      tgMain.textContent = "—";
      const notes = Array.isArray(tgd?.notes) ? tgd.notes.filter(Boolean) : [];
      const warn = Array.isArray(tgd?.warnings) ? tgd.warnings.filter(Boolean) : [];
      tgNote.textContent = (notes[0] || warn[0] || "Live context unavailable (market closed, entitlement gap, or no live chain).");
      tgOi.textContent = "—";
    } else {
      const sign = String(dg.netGammaSign || "—");
      const bucket = String(dg.magnitudeBucket || "—");
      const exp = String(tgd.expiry || "—");
      const sym = String(tgd.symbolUsed || payload?.ticker || "—");
      const earn = tgd.earnDateTarget ? ` · earn=${String(tgd.earnDateTarget)}` : "";
      tgMain.textContent = `${sym.toUpperCase()} · ${sign.toUpperCase()} · ${bucket.toUpperCase()}`;
      const warns = Array.isArray(tgd.warnings) && tgd.warnings.length ? ` · ${tgd.warnings.join(" · ")}` : "";
      const bandMode = tgd.bandMode ? ` · ${String(tgd.bandMode)}` : "";
      tgNote.textContent = `expiry=${exp}${earn} · band=±${Math.round(Number(dg.bandPct || 0.05) * 100)}%${bandMode} · weighting=${String(dg.weightingMode || "—")}${warns}`;
      const putWall = oi && typeof oi === "object" ? oi.putWall : null;
      const callWall = oi && typeof oi === "object" ? oi.callWall : null;
      const putStrike = putWall && (putWall.peakStrike ?? putWall.maxStrike);
      const callStrike = callWall && (callWall.peakStrike ?? callWall.maxStrike);
      const putTxt = putWall && Number.isFinite(Number(putStrike)) ? `${Number(putStrike).toFixed(0)} (${Number(putWall.totalOI || 0).toFixed(0)})` : "—";
      const callTxt = callWall && Number.isFinite(Number(callStrike)) ? `${Number(callStrike).toFixed(0)} (${Number(callWall.totalOI || 0).toFixed(0)})` : "—";
      tgOi.textContent = `OI walls: put=${putTxt} | call=${callTxt}`;
    }
  }

  // OI Clusters cards (Market + Ticker)
  function renderOiClustersCard(prefix, container) {
    const meta = $(`${prefix}OiMeta`);
    const put = $(`${prefix}OiPut`);
    const call = $(`${prefix}OiCall`);
    if (!meta || !put || !call) return;

    const oiObj = container?.oiClusters || null;
    const enabled = !!(container && container.enabled && oiObj && typeof oiObj === "object");
    if (!enabled) {
      meta.textContent = "—";
      put.textContent = "Put: —";
      call.textContent = "Call: —";
      return;
    }

    const spot = Number(oiObj.spot);
    const step = Number(oiObj.strikeStep);
    const band = Number(oiObj.bandPct);
    meta.textContent = `expiry=${String(oiObj.expiry || container.expiry || "—")} · spot=${fmt0(spot)} · band=±${Math.round((Number.isFinite(band) ? band : 0.05) * 100)}% · step=${fmt0(step)}`;

    const puts = _pickMeaningfulClusters(oiObj.putClusters, spot, step).map(_fmtClusterLine);
    const calls = _pickMeaningfulClusters(oiObj.callClusters, spot, step).map(_fmtClusterLine);
    put.textContent = puts.length ? `Put: ${puts.join(" | ")}` : "Put: —";
    call.textContent = calls.length ? `Call: ${calls.join(" | ")}` : "Call: —";
  }

  renderOiClustersCard("market", mg);
  renderOiClustersCard("ticker", tgd);

  const rm = $("regimeMessage");
  if (rm) rm.textContent = rg?.guidance?.message || "—";

  const rv = payload.regimeValidation || {};
  const rvMeta = $("regimeValidationMeta");
  if (rvMeta) rvMeta.textContent = rv.eventsUsed ? `${rv.eventsUsed} usable events` : "—";
  const rvFlagged = $("rvFlagged");
  if (rvFlagged) {
    const b = rv.breaches ?? 0;
    const bf = rv.breachesFlagged ?? 0;
    const pct = (b && b > 0) ? Math.round((bf / b) * 100) : 0;
    rvFlagged.textContent = `Breaches flagged (Caution/No-Trade): ${bf} / ${b} (${pct}%)`;
  }
  const rvMissed = $("rvMissed");
  if (rvMissed) {
    rvMissed.textContent = `Breaches missed (OK): ${rv.breachesMissed ?? 0}`;
  }
  const rvRates = $("rvRates");
  if (rvRates) {
    const br = rv.breachRateByGatePct || {};
    const ok = br.OK ?? "—";
    const ca = br.CAUTION ?? "—";
    const nt = br.NO_TRADE ?? "—";
    rvRates.textContent = `Breach rate by gate: OK ${ok}% · Caution ${ca}% · No-Trade ${nt}%`;
  }

  renderQuarterCards(payload.quarters);
  const a = $("actionSummary");
  if (a) a.textContent = buildActionSummary(payload.quarters);

  renderBufferTarget(payload);
  renderEventRisk(payload);
  renderSkewWings(payload);
  renderMonteCarlo(payload);
  renderTradeBuilder(payload);

  const skipped = payload.skipped || [];
  $("skippedSummary").textContent =
    skipped.length > 0 ? `${skipped.length} skipped (see notes column)` : "";

  renderEarningsTable(payload.events || []);
}

function setStatus(text, isError = false) {
  const el = $("status");
  el.textContent = text || "";
  el.classList.toggle("isError", !!isError);
  el.classList.toggle("isRunning", !isError && !!text && String(text).includes("…"));
  el.classList.toggle("isOk", !isError && String(text || "").toUpperCase() === "OK");
}

function setBusy(busy) {
  isBusy = !!busy;
  const btn = $("submit");
  const form = $("form");
  const ticker = $("ticker");
  const k = $("k");
  if (btn) btn.disabled = isBusy;
  if (ticker) ticker.disabled = isBusy;
  if (k) k.disabled = isBusy;
  if (form) form.classList.toggle("isLoading", isBusy);
}

document.addEventListener("DOMContentLoaded", () => {
  // Feature-gated nav links (Engine 2).
  (async () => {
    try {
      const flags = await fetchJson("/api/flags");
      const link = $("engine2Link");
      if (link) link.classList.toggle("hidden", !flags?.ENABLE_ENGINE2_SPX_IC);
    } catch {
      // If flags endpoint fails, keep the link hidden by default.
    }
  })();

  // Query params (calendar deep-links)
  const qs = new URLSearchParams(window.location.search || "");
  const qsTicker = (qs.get("ticker") || "").trim().toUpperCase();
  const qsMc = String(qs.get("mc") || "").trim().toLowerCase();
  const qsAutorun = String(qs.get("autorun") || "").trim().toLowerCase();

  const form = $("form");
  const ticker = $("ticker");
  const kSel = $("k");
  ticker.value = (qsTicker || ticker.value || "AAPL").toUpperCase();
  setTickerLogo(ticker.value);

  ticker.addEventListener("input", () => {
    ticker.value = ticker.value.toUpperCase();
    setTickerLogo(ticker.value);
  });

  async function runCalculation(extraParams = null) {
    setStatus("");
    const t = (ticker.value || "").trim().toUpperCase();
    const k = (kSel?.value || "1.0");
    if (!t) {
      setStatus("Enter a ticker.", true);
      return;
    }

    setBusy(true);
    setStatus(`Computing with k=${k}…`);
    try {
      let url = `/api/breach?ticker=${encodeURIComponent(t)}&n=20&years=5&k=${encodeURIComponent(k)}`;
      if (extraParams && typeof extraParams === "object") {
        for (const [kk, vv] of Object.entries(extraParams)) {
          if (vv === null || vv === undefined) continue;
          url += `&${encodeURIComponent(kk)}=${encodeURIComponent(String(vv))}`;
        }
      }
      if (mcEnabledPref) {
        // Default-on conditioning for trader relevance; backend will still fail-safe if pools are too small.
        url += `&mc=1&mc_cond_regime=1&mc_cond_quarter=1`;
        if (mcEventOverride?.date) url += `&mc_event_date=${encodeURIComponent(String(mcEventOverride.date))}`;
        if (mcEventOverride?.timing && String(mcEventOverride.timing).toUpperCase() !== "AUTO") {
          url += `&mc_event_timing=${encodeURIComponent(String(mcEventOverride.timing).toUpperCase())}`;
        }
      }
      const payload = await fetchJson(url);
      render(payload);
      setStatus("");
    } catch (e) {
      setStatus(e?.message || "Error", true);
    } finally {
      setBusy(false);
    }
  }

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (isBusy) return;
    await runCalculation();
  });

  // UX: if results are already present, changing breach multiple should immediately recompute.
  if (kSel) {
    kSel.addEventListener("change", async () => {
      if (isBusy) return;
      if (!lastPayload) return; // don't auto-run before first calculation
      await runCalculation();
    });
  }

  const toggle = $("earningsToggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      earningsExpanded = !earningsExpanded;
      toggle.textContent = earningsExpanded ? "Hide earnings history" : "Show earnings history";
      if (lastPayload) renderEarningsTable(lastPayload.events || []);
    });
  }

  const adv = $("advancedColsToggle");
  if (adv) {
    adv.addEventListener("click", () => {
      showAdvancedCols = !showAdvancedCols;
      adv.textContent = showAdvancedCols ? "Hide advanced columns" : "Show advanced columns";
      if (lastPayload) renderEarningsTable(lastPayload.events || []);
    });
  }

  const symBtn = $("bufferModeSymmetric");
  if (symBtn) {
    symBtn.addEventListener("click", () => {
      bufferModePref = "symmetric";
      if (lastPayload) renderBufferTarget(lastPayload);
    });
  }
  const asymBtn = $("bufferModeAsymmetric");
  if (asymBtn) {
    asymBtn.addEventListener("click", () => {
      bufferModePref = "asymmetric";
      if (lastPayload) renderBufferTarget(lastPayload);
    });
  }

  const tbToggle = $("tradeBuilderToggle");
  const tbPanel = $("tradeBuilderPanel");
  if (tbToggle && tbPanel) {
    tbToggle.addEventListener("click", () => {
      tradeBuilderExpanded = !tradeBuilderExpanded;
      tbPanel.classList.toggle("hidden", !tradeBuilderExpanded);
      tbToggle.textContent = tradeBuilderExpanded ? "Hide trade builder" : "Show trade builder";
      if (lastPayload) renderTradeBuilder(lastPayload);
    });
  }

  function setSegActive(ids, activeId) {
    for (const id of ids) {
      const el = $(id);
      if (el) el.classList.toggle("isActive", id === activeId);
    }
  }

  const modeBtns = ["tbModeAuto", "tbModeDelta", "tbModePremium"];
  const symBtns = ["tbSymAuto", "tbSymSym", "tbSymManual"];

  function syncTradeBuilderControls() {
    setSegActive(
      modeBtns,
      tradeBuilderState.mode === "auto" ? "tbModeAuto" : tradeBuilderState.mode === "equal_delta" ? "tbModeDelta" : "tbModePremium"
    );
    setSegActive(
      symBtns,
      tradeBuilderState.symmetry === "auto" ? "tbSymAuto" : tradeBuilderState.symmetry === "symmetric" ? "tbSymSym" : "tbSymManual"
    );
    const deltaRow = $("tbDeltaRow");
    const premRow = $("tbPremiumRow");
    const manualRow = $("tbManualMultRow");
    if (deltaRow) deltaRow.classList.toggle("hidden", tradeBuilderState.mode !== "equal_delta");
    if (premRow) premRow.classList.toggle("hidden", tradeBuilderState.mode !== "equal_premium");
    if (manualRow) manualRow.classList.toggle("hidden", tradeBuilderState.symmetry !== "manual");
  }

  for (const id of modeBtns) {
    const el = $(id);
    if (!el) continue;
    el.addEventListener("click", () => {
      if (id === "tbModeAuto") tradeBuilderState.mode = "auto";
      if (id === "tbModeDelta") tradeBuilderState.mode = "equal_delta";
      if (id === "tbModePremium") tradeBuilderState.mode = "equal_premium";
      syncTradeBuilderControls();
      if (lastPayload) renderTradeBuilder(lastPayload);
    });
  }
  for (const id of symBtns) {
    const el = $(id);
    if (!el) continue;
    el.addEventListener("click", () => {
      if (id === "tbSymAuto") tradeBuilderState.symmetry = "auto";
      if (id === "tbSymSym") tradeBuilderState.symmetry = "symmetric";
      if (id === "tbSymManual") tradeBuilderState.symmetry = "manual";
      syncTradeBuilderControls();
      if (lastPayload) renderTradeBuilder(lastPayload);
    });
  }

  const deltaPreset = $("tbDeltaPreset");
  if (deltaPreset) {
    deltaPreset.addEventListener("change", () => {
      const v = Number(deltaPreset.value);
      if (Number.isFinite(v)) tradeBuilderState.target_delta = v;
      const inp = $("tbTargetDelta");
      if (inp) inp.value = String(v);
    });
  }
  const deltaInp = $("tbTargetDelta");
  if (deltaInp) {
    deltaInp.addEventListener("input", () => {
      const v = Number(deltaInp.value);
      if (Number.isFinite(v)) tradeBuilderState.target_delta = v;
    });
  }
  const premInp = $("tbTargetPremium");
  if (premInp) {
    premInp.addEventListener("input", () => {
      const v = Number(premInp.value);
      if (Number.isFinite(v)) tradeBuilderState.target_premium = v;
    });
  }
  const putMultInp = $("tbPutMult");
  if (putMultInp) {
    putMultInp.addEventListener("input", () => {
      tradeBuilderState.put_mult = putMultInp.value;
      if (lastPayload) renderTradeBuilder(lastPayload);
    });
  }
  const callMultInp = $("tbCallMult");
  if (callMultInp) {
    callMultInp.addEventListener("input", () => {
      tradeBuilderState.call_mult = callMultInp.value;
      if (lastPayload) renderTradeBuilder(lastPayload);
    });
  }
  const widthSel = $("tbWingWidth");
  if (widthSel) {
    widthSel.addEventListener("change", () => {
      const v = Number(widthSel.value);
      if (Number.isFinite(v)) tradeBuilderState.wing_width = v;
      if (lastPayload) renderTradeBuilder(lastPayload);
    });
  }

  const tbRecalc = $("tradeBuilderRecalc");
  if (tbRecalc) {
    tbRecalc.addEventListener("click", async () => {
      if (isBusy) return;
      const gate = (lastPayload?.regime?.guidance || {})?.tradeGate;
      if (gate === "NO_TRADE") return;
      const extra = {
        mode: tradeBuilderState.mode,
        symmetry: tradeBuilderState.symmetry,
        target_delta: tradeBuilderState.target_delta,
        target_premium: tradeBuilderState.target_premium,
        wing_width: tradeBuilderState.wing_width,
        dte_target: 2,
      };
      await runCalculation(extra);
    });
  }

  syncTradeBuilderControls();

  const mcToggle = $("mcToggle");
  const mcGroup = $("mcOverrideGroup");
  const mcDate = $("mcEventDate");
  const mcTiming = $("mcEventTiming");
  if (mcToggle) {
    mcToggle.addEventListener("change", async () => {
      mcEnabledPref = !!mcToggle.checked;
      if (mcGroup) mcGroup.classList.toggle("hidden", !mcEnabledPref);
      if (!lastPayload) return;
      if (isBusy) return;
      await runCalculation();
    });
  }

  // Apply mc=1 from querystring without triggering a compute until we decide to autorun.
  if (mcToggle && (qsMc === "1" || qsMc === "true" || qsMc === "yes" || qsMc === "on")) {
    mcEnabledPref = true;
    mcToggle.checked = true;
    if (mcGroup) mcGroup.classList.toggle("hidden", false);
  }

  if (mcDate) {
    mcDate.addEventListener("change", async () => {
      mcEventOverride.date = mcDate.value || null;
      if (!mcEnabledPref) return;
      if (!lastPayload || isBusy) return;
      await runCalculation();
    });
  }
  if (mcTiming) {
    mcTiming.addEventListener("change", async () => {
      mcEventOverride.timing = mcTiming.value || "AUTO";
      if (!mcEnabledPref) return;
      if (!lastPayload || isBusy) return;
      await runCalculation();
    });
  }

  // AskRaven removed
  initTooltips();

  // Optional auto-run for calendar deep-links: /breach?ticker=...&mc=1&autorun=1
  if (qsAutorun === "1" || qsAutorun === "true" || qsAutorun === "yes" || qsAutorun === "on") {
    // Kick once on page load. Respect "busy" guardrails.
    if (!isBusy) runCalculation();
  }
});


