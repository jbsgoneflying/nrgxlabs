/* global window, document */

function fmtMoneyShort(x) {
  const n = Number(x);
  if (!Number.isFinite(n)) return "—";
  const s = n < 0 ? "-" : "+";
  const a = Math.abs(n);
  if (a >= 1e12) return `${s}$${(a / 1e12).toFixed(2)}T`;
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(2)}K`;
  return `${s}$${a.toFixed(0)}`;
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

let lastPayload = null;
let lastGammaPayload = null;
const _e2TrackBuilderState = {
  defaultEmMultiple: 1.25,
  defaultWingWidth: 10,
};
const gammaState = {
  view: "weekly", // weekly|nearest
  layers: { putWall: true, callWall: true, clusters: true, gammaPeaks: true, gammaFlip: true },
};

const gexState = {
  view: "composite", // composite|raw
  mode: "slope", // net|slope
};

const engine2UnderlyingState = {
  symbol: "SPX", // SPX|SPY
};

let _engine2TitleTemplate = null;
let _engine2SubtitleTemplate = null;

function setLoading(isLoading, statusMsg) {
  const btn = $("runBtn");
  if (!btn) return;
  btn.disabled = !!isLoading;
  btn.classList.toggle("isLoading", !!isLoading);
  document.body.classList.toggle("isApiLoading", !!isLoading);
  
  // Raven Loading Overlay
  if (window.RavenLoading) {
    if (isLoading) {
      window.RavenLoading.show({ status: statusMsg || "Analyzing SPX..." });
    } else {
      window.RavenLoading.hide();
    }
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
// PERFORMANCE OPTIMIZATION: Client-side response cache with stale-while-revalidate
// -----------------------------------------------------------------------------
const _apiCache = new Map();
const API_CACHE_TTL_MS = 5 * 60 * 1000; // 5 minutes fresh TTL
const API_CACHE_STALE_TTL_MS = 30 * 60 * 1000; // 30 minutes stale-but-usable TTL

function _getCacheKey(url) {
  return url.split("?")[0] + "?" + new URLSearchParams(url.split("?")[1] || "").toString();
}

function _getCached(url) {
  const key = _getCacheKey(url);
  const entry = _apiCache.get(key);
  if (!entry) return null;
  const age = Date.now() - entry.ts;
  return {
    data: entry.data,
    isFresh: age < API_CACHE_TTL_MS,
    isStale: age < API_CACHE_STALE_TTL_MS,
  };
}

function _setCache(url, data) {
  const key = _getCacheKey(url);
  _apiCache.set(key, { data, ts: Date.now() });
  if (_apiCache.size > 100) {
    const oldest = [..._apiCache.entries()].sort((a, b) => a[1].ts - b[1].ts);
    for (let i = 0; i < 20; i++) _apiCache.delete(oldest[i][0]);
  }
}

async function fetchJson(url, { timeoutMs = 90000 } = {}) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), Number(timeoutMs));
  try {
    const r = await fetch(url, { signal: ctrl.signal });
    const txt = await r.text();
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${txt.slice(0, 300)}`);
    const data = JSON.parse(txt);
    _setCache(url, data);
    return data;
  } catch (e) {
    if (String(e?.name || "").toLowerCase() === "aborterror") {
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw e;
  } finally {
    clearTimeout(t);
  }
}

async function checkFlags() {
  try {
    const f = await fetchJson("/api/flags");
    return f || {};
  } catch {
    return {};
  }
}

function _loadUnderlyingPref() {
  try {
    const raw = window.localStorage?.getItem("engine2Underlying") || "";
    const v = String(raw).trim().toUpperCase();
    if (v === "SPY" || v === "SPX" || v === "QQQ") engine2UnderlyingState.symbol = v;
  } catch {
    // ignore
  }
}

function _persistUnderlyingPref() {
  try {
    window.localStorage?.setItem("engine2Underlying", String(engine2UnderlyingState.symbol || "SPX"));
  } catch {
    // ignore
  }
}

function _applyUnderlyingUI() {
  const spxBtn = $("e2UnderlyingSPX");
  const spyBtn = $("e2UnderlyingSPY");
  const qqqBtn = $("e2UnderlyingQQQ");
  const sym = String(engine2UnderlyingState.symbol || "SPX").toUpperCase();

  if (spxBtn) {
    const on = sym === "SPX";
    spxBtn.classList.toggle("isActive", on);
    spxBtn.setAttribute("aria-pressed", on ? "true" : "false");
  }
  if (spyBtn) {
    const on = sym === "SPY";
    spyBtn.classList.toggle("isActive", on);
    spyBtn.setAttribute("aria-pressed", on ? "true" : "false");
  }
  if (qqqBtn) {
    const on = sym === "QQQ";
    qqqBtn.classList.toggle("isActive", on);
    qqqBtn.setAttribute("aria-pressed", on ? "true" : "false");
  }

  // Header/title text
  const subEl = document.querySelector(".appSubtitle");
  if (_engine2TitleTemplate) {
    document.title = String(_engine2TitleTemplate).replace(/\bSPX\b/g, sym);
  }
  if (subEl && _engine2SubtitleTemplate) {
    subEl.textContent = String(_engine2SubtitleTemplate).replace(/\bSPX\b/g, sym);
  }
}

function initUnderlyingUI() {
  _engine2TitleTemplate = document.title;
  const subEl = document.querySelector(".appSubtitle");
  _engine2SubtitleTemplate = subEl ? String(subEl.textContent || "") : null;

  _loadUnderlyingPref();
  _applyUnderlyingUI();

  const spxBtn = $("e2UnderlyingSPX");
  const spyBtn = $("e2UnderlyingSPY");
  const qqqBtn = $("e2UnderlyingQQQ");
  const status = $("status");
  const results = $("results");

  const setSym = (sym) => {
    const s = String(sym || "").toUpperCase();
    const next = (s === "SPY" || s === "QQQ") ? s : "SPX";
    if (engine2UnderlyingState.symbol === next) return;
    engine2UnderlyingState.symbol = next;
    _persistUnderlyingPref();
    _applyUnderlyingUI();

    // Keep outputs consistent: require explicit re-run.
    if (results) results.classList.toggle("hidden", true);
    lastPayload = null;
    lastGammaPayload = null;
    if (status) {
      status.textContent = `Underlying set to ${next}. Click Run.`;
      status.classList.remove("isError", "isRunning", "isOk");
      status.classList.remove("hidden");
    }
  };

  if (spxBtn) spxBtn.addEventListener("click", () => setSym("SPX"));
  if (spyBtn) spyBtn.addEventListener("click", () => setSym("SPY"));
  if (qqqBtn) qqqBtn.addEventListener("click", () => setSym("QQQ"));
}

function clamp(x, lo, hi) {
  const n = Number(x);
  if (!Number.isFinite(n)) return lo;
  return Math.max(Number(lo), Math.min(Number(hi), n));
}

function _fmtDateShort(iso) {
  const s = String(iso || "").slice(0, 10);
  if (!s) return "—";
  return s;
}

function _fmtNum(x, d = 0) {
  const n = Number(x);
  if (!Number.isFinite(n)) return "—";
  return n.toFixed(d);
}

function initGammaMapUI() {
  const weeklyBtn = $("gammaViewWeekly");
  const nearestBtn = $("gammaViewNearest");

  const setView = (v) => {
    gammaState.view = (v === "nearest") ? "nearest" : "weekly";
    if (weeklyBtn) {
      const on = gammaState.view === "weekly";
      weeklyBtn.classList.toggle("isOn", on);
      weeklyBtn.setAttribute("aria-pressed", on ? "true" : "false");
    }
    if (nearestBtn) {
      const on = gammaState.view === "nearest";
      nearestBtn.classList.toggle("isOn", on);
      nearestBtn.setAttribute("aria-pressed", on ? "true" : "false");
    }
    // Re-fetch so expiry selection matches the view.
    loadGammaMap();
  };

  if (weeklyBtn) weeklyBtn.addEventListener("click", () => setView("weekly"));
  if (nearestBtn) nearestBtn.addEventListener("click", () => setView("nearest"));

  const legend = document.querySelector(".gammaLegend");
  if (legend) {
    legend.addEventListener("click", (ev) => {
      const t = ev.target;
      if (!t || !t.closest) return;
      const btn = t.closest("button[data-layer]");
      if (!btn) return;
      const k = String(btn.getAttribute("data-layer") || "");
      if (!k) return;
      const cur = !!gammaState.layers[k];
      gammaState.layers[k] = !cur;
      btn.classList.toggle("isOn", !cur);
      btn.setAttribute("aria-pressed", (!cur) ? "true" : "false");
      renderGammaMap(lastGammaPayload);
    });
  }

  window.addEventListener("resize", () => {
    // Cheap reflow: redraw using cached payload.
    renderGammaMap(lastGammaPayload);
  });
}

function initGexHeatmapUI() {
  const btnComp = $("gexViewComposite");
  const btnRaw = $("gexViewRaw");
  const btnNet = $("gexModeNet");
  const btnSlope = $("gexModeSlope");

  // Ensure initial UI matches state (HTML defaults should match too, but keep this robust)
  if (btnComp) { btnComp.classList.toggle("isOn", gexState.view === "composite"); btnComp.setAttribute("aria-pressed", gexState.view === "composite" ? "true" : "false"); }
  if (btnRaw) { btnRaw.classList.toggle("isOn", gexState.view === "raw"); btnRaw.setAttribute("aria-pressed", gexState.view === "raw" ? "true" : "false"); }
  if (btnNet) { btnNet.classList.toggle("isOn", gexState.mode === "net"); btnNet.setAttribute("aria-pressed", gexState.mode === "net" ? "true" : "false"); }
  if (btnSlope) { btnSlope.classList.toggle("isOn", gexState.mode === "slope"); btnSlope.setAttribute("aria-pressed", gexState.mode === "slope" ? "true" : "false"); }

  const setView = (v) => {
    gexState.view = (v === "raw") ? "raw" : "composite";
    if (btnComp) {
      const on = gexState.view === "composite";
      btnComp.classList.toggle("isOn", on);
      btnComp.setAttribute("aria-pressed", on ? "true" : "false");
    }
    if (btnRaw) {
      const on = gexState.view === "raw";
      btnRaw.classList.toggle("isOn", on);
      btnRaw.setAttribute("aria-pressed", on ? "true" : "false");
    }
    // Re-fetch so backend display hints + cache key align (cheap; cached).
    loadGammaMap();
  };

  const setMode = (m) => {
    gexState.mode = (m === "slope") ? "slope" : "net";
    if (btnNet) {
      const on = gexState.mode === "net";
      btnNet.classList.toggle("isOn", on);
      btnNet.setAttribute("aria-pressed", on ? "true" : "false");
    }
    if (btnSlope) {
      const on = gexState.mode === "slope";
      btnSlope.classList.toggle("isOn", on);
      btnSlope.setAttribute("aria-pressed", on ? "true" : "false");
    }
    loadGammaMap();
  };

  if (btnComp) btnComp.addEventListener("click", () => setView("composite"));
  if (btnRaw) btnRaw.addEventListener("click", () => setView("raw"));
  if (btnNet) btnNet.addEventListener("click", () => setMode("net"));
  if (btnSlope) btnSlope.addEventListener("click", () => setMode("slope"));
}

async function loadGammaMap() {
  const meta = $("gammaMeta");
  const note = $("gammaNote");
  const chart = $("gammaChart");
  if (!chart) return;

  try {
    if (meta) meta.textContent = "Loading…";
    if (note) note.textContent = "—";
    const v = gammaState.view;
    const under = encodeURIComponent(String(engine2UnderlyingState.symbol || "SPX"));
    const payload = await fetchJson(
      `/api/spx-levels?underlying=${under}&view=${encodeURIComponent(v)}&points=90&window_days=180&include_heatmap=1`
      + `&heatmap_view=${encodeURIComponent(gexState.view)}`
      + `&heatmap_mode=${encodeURIComponent(gexState.mode)}`
      + `&slope_window=5&flip_adjacent_n=5`,
      { timeoutMs: 45000 }
    );
    lastGammaPayload = payload;
    renderGammaMap(payload);
    renderGexHeatmap(payload);
  } catch (e) {
    lastGammaPayload = null;
    if (meta) meta.textContent = "Dealer Gamma Map unavailable";
    if (note) note.textContent = String(e?.message || e || "Error");
    chart.innerHTML = `<div class="muted" style="padding:14px;">${escapeHtml(String(e?.message || e || "Failed to load."))}</div>`;
    renderGexHeatmap(null);
  }
}

function renderGexHeatmap(payload) {
  const wrap = $("gexHeatmap");
  const meta = $("gexMeta");
  const note = $("gexNote");
  const tip = $("gexHeatTip");
  const downPtsEl = $("gexDownPts");
  const downEmEl = $("gexDownEm");
  const upPtsEl = $("gexUpPts");
  const upEmEl = $("gexUpEm");
  const stabEl = $("gexStability");
  if (!wrap) return;

  const heat = payload?.levels?.gexHeatmap || null;
  const enabled = !!heat?.enabled;
  const spot = Number(heat?.spot);
  const band = Number(heat?.bandPct);
  const wmode = String(heat?.weightingMode || "");
  const denom = Number(heat?.scaleDenom);
  const ivUsed = Number(heat?.atmIvUsedPct);
  const underLabel = String(engine2UnderlyingState.symbol || "SPX");

  // Metrics strip
  const m = heat?.metrics || {};
  if (downPtsEl) downPtsEl.textContent = Number.isFinite(Number(m?.downsideDistancePts)) ? fmt2(m.downsideDistancePts) : "—";
  if (upPtsEl) upPtsEl.textContent = Number.isFinite(Number(m?.upsideDistancePts)) ? fmt2(m.upsideDistancePts) : "—";
  if (downEmEl) downEmEl.textContent = Number.isFinite(Number(m?.downsideDistanceEm)) ? fmt2(m.downsideDistanceEm) : "—";
  if (upEmEl) upEmEl.textContent = Number.isFinite(Number(m?.upsideDistanceEm)) ? fmt2(m.upsideDistanceEm) : "—";
  const st = heat?.stability || {};
  if (stabEl) {
    const lab = String(st?.label || "—");
    stabEl.textContent = lab;
    stabEl.classList.toggle("isStable", lab === "Stable");
    stabEl.classList.toggle("isAsym", lab === "Asymmetric");
    stabEl.classList.toggle("isFragile", lab === "Fragile");
    const rs = Array.isArray(st?.reasons) ? st.reasons.filter(Boolean) : [];
    stabEl.title = rs.join("\n");
  }

  const hideTip = () => { if (tip) tip.classList.add("hidden"); };
  const showTip = (html, x, y) => {
    if (!tip) return;
    tip.innerHTML = html;
    tip.classList.remove("hidden");
    const box = wrap.getBoundingClientRect();
    const left = clamp(x - box.left + 12, 8, box.width - 260);
    const top = clamp(y - box.top + 12, 8, box.height - 140);
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  };

  // Determine which dataset to render
  let yLabels = [];
  let strikes = [];
  let mat = [];
  let rowMeta = []; // optional per-row meta for tooltip (e.g. effectiveDte / EM)

  const raw = heat?.raw || {};
  const comp = heat?.composite || {};
  if (gexState.view === "raw") {
    const expiries = Array.isArray(raw?.expiries) ? raw.expiries : [];
    strikes = Array.isArray(raw?.strikes) ? raw.strikes : [];
    const net = Array.isArray(raw?.netDollarGex) ? raw.netDollarGex : [];
    const slope = Array.isArray(raw?.slopeNetDollarGex) ? raw.slopeNetDollarGex : [];
    mat = (gexState.mode === "slope") ? slope : net;
    yLabels = expiries.map((e) => String(e).slice(5)); // MM-DD
    rowMeta = expiries.map((e) => ({ expiry: String(e) }));
  } else {
    const buckets = Array.isArray(comp?.buckets) ? comp.buckets : [];
    strikes = Array.isArray(comp?.strikes) ? comp.strikes : [];
    yLabels = buckets.map((b) => String(b?.label || b?.key || "—"));
    rowMeta = buckets.map((b) => ({ key: b?.key, effectiveDte: b?.effectiveDte, expectedMovePts: b?.expectedMovePts }));
    mat = buckets.map((b) => (gexState.mode === "slope") ? (b?.slopeNetDollarGex || []) : (b?.netDollarGex || []));
  }

  if (!payload || !enabled || !yLabels.length || !strikes.length || !mat.length) {
    wrap.innerHTML = `<div class="muted" style="padding:14px;">Run Engine 2 to load the heat map.</div>`;
    if (meta) meta.textContent = "—";
    if (note) {
      const err = heat?.error ? `Heatmap unavailable (${String(heat.error)}).` : "—";
      note.textContent = payload && !enabled ? err : "—";
    }
    hideTip();
    return;
  }

  // Compute max abs for scaling (ignore nulls)
  let maxAbs = 0;
  for (let i = 0; i < mat.length; i++) {
    const row = Array.isArray(mat[i]) ? mat[i] : [];
    for (let j = 0; j < row.length; j++) {
      const v0 = Number(row[j]);
      if (!Number.isFinite(v0)) continue;
      const v = (Number.isFinite(denom) && denom > 0) ? (v0 / denom) : v0; // normalization is render-only
      maxAbs = Math.max(maxAbs, Math.abs(v));
    }
  }
  if (!Number.isFinite(maxAbs) || maxAbs <= 0) maxAbs = 1;

  const w = Math.max(320, wrap.clientWidth || 640);
  const pad = { l: 74, r: 10, t: 10, b: 26 };
  const rows = yLabels.length;
  const cols = strikes.length;
  const cellH = (gexState.view === "composite") ? 64 : 16; // enlarge composite rows; keep raw unchanged
  const cellW = Math.max(6, Math.floor((w - pad.l - pad.r) / Math.max(1, cols)));
  const h = pad.t + pad.b + rows * cellH;

  const xForCol = (c) => pad.l + c * cellW;
  const yForRow = (r) => pad.t + r * cellH;

  const scale = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n)) return 0;
    const nn = (Number.isFinite(denom) && denom > 0) ? (n / denom) : n; // normalization is render-only
    const a = Math.abs(nn);
    // compress large dynamic range
    const t = Math.log10(1 + a / 1e6);
    const tMax = Math.log10(1 + maxAbs / 1e6);
    const u = tMax > 0 ? (t / tMax) : 0;
    return (nn < 0 ? -u : u);
  };

  const colorFor = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n)) return "rgba(120,120,130,0.10)"; // missing
    const t = scale(n); // -1..1
    const a = Math.min(1, Math.abs(t));
    // Diverging: blue (neg) to orange (pos)
    const hue = (t < 0) ? 210 : 20;
    const sat = 72;
    // darker = stronger
    const light = 82 - (a * 34);
    const alpha = 0.95;
    return `hsla(${hue}, ${sat}%, ${light}%, ${alpha})`;
  };

  if (meta) {
    const b = Number.isFinite(band) ? `${Math.round(band * 100)}%` : "—";
    const ivTxt = Number.isFinite(ivUsed) ? `${ivUsed.toFixed(2)}%` : "—";
    meta.textContent = `spot=${Number.isFinite(spot) ? _fmtNum(spot, 2) : "—"} · band=±${b} · iv=${ivTxt} · mode=${wmode || "—"} · rows=${rows} · cols=${cols}`;
  }
  if (note) {
    const warns = Array.isArray(heat?.warnings) ? heat.warnings.filter(Boolean) : [];
    const notes = Array.isArray(heat?.notes) ? heat.notes.filter(Boolean) : [];
    note.textContent = warns[0] || notes[0] || "Live, informational only.";
  }

  const tickEvery = Math.max(1, Math.round(cols / 6));
  const xTicks = strikes.map((s, i) => ({ s: Number(s), i })).filter(t => (t.i % tickEvery) === 0);

  const bnds = heat?.boundaries || {};
  const downB = Number(bnds?.downsideAccelerationBoundaryStrike);
  const upB = Number(bnds?.upsideAccelerationBoundaryStrike);
  const xForStrike = (k) => {
    const kk = Number(k);
    if (!Number.isFinite(kk)) return null;
    let best = null;
    let bestD = null;
    for (let i = 0; i < strikes.length; i++) {
      const s = Number(strikes[i]);
      if (!Number.isFinite(s)) continue;
      const d = Math.abs(s - kk);
      if (best === null || bestD === null || d < bestD) {
        best = i;
        bestD = d;
      }
    }
    return best === null ? null : (xForCol(best) + (cellW / 2));
  };
  const xDown = xForStrike(downB);
  const xUp = xForStrike(upB);
  const xSpot = xForStrike(spot);

  wrap.innerHTML = `
    <svg class="gexSvg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${escapeHtml(underLabel)} net $GEX heat map">
      <rect x="0" y="0" width="${w}" height="${h}" class="gexBg"></rect>
      ${yLabels.map((lab, r) => `<text x="${pad.l - 8}" y="${yForRow(r) + 12}" class="gexAxis gexAxis--y" text-anchor="end">${escapeHtml(lab)}</text>`).join("")}
      ${xTicks.map(t => `<text x="${xForCol(t.i) + 2}" y="${h - 10}" class="gexAxis gexAxis--x">${escapeHtml(fmt0(t.s))}</text>`).join("")}
      ${xSpot === null ? "" : `<line x1="${xSpot}" x2="${xSpot}" y1="${pad.t}" y2="${pad.t + rows * cellH}" class="gexSpot"></line>
        <text x="${xSpot + 6}" y="${pad.t + 34}" class="gexSpotLabel">Spot</text>`}
      ${xDown === null ? "" : `<line x1="${xDown}" x2="${xDown}" y1="${pad.t}" y2="${pad.t + rows * cellH}" class="gexBoundary gexBoundary--down"></line>
        <text x="${xDown + 6}" y="${pad.t + 10}" class="gexBoundaryLabel">Downside acceleration boundary</text>`}
      ${xUp === null ? "" : `<line x1="${xUp}" x2="${xUp}" y1="${pad.t}" y2="${pad.t + rows * cellH}" class="gexBoundary gexBoundary--up"></line>
        <text x="${xUp + 6}" y="${pad.t + 22}" class="gexBoundaryLabel">Upside acceleration boundary</text>`}
      ${mat.map((row, r) => {
        const rr = Array.isArray(row) ? row : [];
        return rr.map((v, c) => {
          const x = xForCol(c);
          const y = yForRow(r);
          const fill = colorFor(v);
          return `<rect class="gexCell" data-r="${r}" data-c="${c}" x="${x}" y="${y}" width="${cellW}" height="${cellH - 1}" rx="2" ry="2" fill="${fill}"></rect>`;
        }).join("");
      }).join("")}
    </svg>
  `;

  const svg = wrap.querySelector("svg");
  if (!svg) return;

  svg.addEventListener("mouseleave", () => hideTip());
  svg.addEventListener("mousemove", (ev) => {
    const box = svg.getBoundingClientRect();
    const mx = ev.clientX - box.left;
    const my = ev.clientY - box.top;
    const col = Math.floor((mx - pad.l) / cellW);
    const row = Math.floor((my - pad.t) / cellH);
    if (row < 0 || row >= rows || col < 0 || col >= cols) {
      hideTip();
      return;
    }
    const rowInfo = rowMeta[row] || {};
    const rowLabel = yLabels[row];
    const strike = strikes[col];
    const v = (Array.isArray(mat[row]) ? mat[row][col] : null);
    const vNum = Number(v);
    const isMoney = (gexState.mode === "net");
    const valTxt = Number.isFinite(vNum) ? (isMoney ? fmtMoneyShort(vNum) : fmtMoneyShort(vNum)) : "—";
    const eff = rowInfo?.effectiveDte;
    const emPts = rowInfo?.expectedMovePts;
    const extra = (eff !== undefined && eff !== null) ? `effectiveDTE=${escapeHtml(String(eff))} · EM=${escapeHtml(String(emPts ?? "—"))} pts` : "";
    const html = `
      <div class="chartTipTitle">${escapeHtml(gexState.mode === "slope" ? "GEX slope (Δ per strike)" : "Net $GEX")}</div>
      <div class="chartTipBody mono">${escapeHtml(String(rowLabel))} · strike ${escapeHtml(fmt0(strike))}</div>
      <div class="chartTipDivider"></div>
      <div class="chartTipBody mono">${escapeHtml(valTxt)}</div>
      ${extra ? `<div class="chartTipBody muted">${extra}</div>` : ""}
      <div class="chartTipBody muted">spot=${escapeHtml(Number.isFinite(spot) ? _fmtNum(spot, 2) : "—")} · band=±${escapeHtml(Number.isFinite(band) ? String(Math.round(band * 100)) : "—")}% · normalization=${Number.isFinite(denom) && denom > 0 ? "on (render-only)" : "off"}</div>
    `;
    showTip(html, ev.clientX, ev.clientY);
  });
}

function renderGammaMap(payload) {
  const chart = $("gammaChart");
  const tip = $("gammaTooltip");
  const meta = $("gammaMeta");
  const note = $("gammaNote");
  if (!chart) return;

  // Empty/initial state
  if (!payload || typeof payload !== "object") {
    chart.innerHTML = `<div class="muted" style="padding:14px;">Run Engine 2 to load the map.</div>`;
    if (meta) meta.textContent = "—";
    if (note) note.textContent = "—";
    if (tip) tip.classList.add("hidden");
    return;
  }

  const series = Array.isArray(payload?.priceSeries) ? payload.priceSeries : [];
  const levels = payload?.levels || {};
  const enabled = !!levels?.enabled;

  const expiry = levels?.expiry ? String(levels.expiry).slice(0, 10) : "—";
  const spot = Number(levels?.spot);
  const sym = levels?.symbolUsed ? String(levels.symbolUsed) : "—";
  const bandPct = Number(levels?.bandPct);
  const underLabel = String(engine2UnderlyingState.symbol || sym || "SPX");

  if (meta) {
    const b = Number.isFinite(bandPct) ? `${Math.round(bandPct * 100)}%` : "—";
    meta.textContent = `expiry=${expiry} · spot=${Number.isFinite(spot) ? _fmtNum(spot, 2) : "—"} · band=±${b} · src=${sym}`;
  }

  const notes = Array.isArray(levels?.notes) ? levels.notes.filter(Boolean) : [];
  const warns = Array.isArray(levels?.warnings) ? levels.warnings.filter(Boolean) : [];
  if (note) note.textContent = notes[0] || (warns[0] || "Live, informational only.");

  if (!enabled || !series.length) {
    const msg = !series.length ? `No ${underLabel} price series returned.` : "Live levels unavailable (missing live chain).";
    chart.innerHTML = `<div class="muted" style="padding:14px;">${escapeHtml(msg)}</div>`;
    if (tip) tip.classList.add("hidden");
    return;
  }

  // --- Build overlay items from backend payload ---
  const oi = levels?.oiClusters || {};
  const dg = levels?.dealerGamma || {};
  const flip = Number(levels?.gammaFlipStrike);

  const overlayLines = [];

  const putWall = oi?.putWall;
  const callWall = oi?.callWall;
  if (gammaState.layers.putWall && putWall && Number.isFinite(Number(putWall?.peakStrike ?? putWall?.centerStrike))) {
    const y = Number(putWall?.peakStrike ?? putWall?.centerStrike);
    overlayLines.push({ kind: "putWall", y, title: "Put wall", detail: `strike ${_fmtNum(y, 0)} · totalOI ${_fmtNum(putWall?.totalOI, 0)} · range ${_fmtNum(putWall?.minStrike, 0)}–${_fmtNum(putWall?.maxStrike, 0)}` });
  }
  if (gammaState.layers.callWall && callWall && Number.isFinite(Number(callWall?.peakStrike ?? callWall?.centerStrike))) {
    const y = Number(callWall?.peakStrike ?? callWall?.centerStrike);
    overlayLines.push({ kind: "callWall", y, title: "Call wall", detail: `strike ${_fmtNum(y, 0)} · totalOI ${_fmtNum(callWall?.totalOI, 0)} · range ${_fmtNum(callWall?.minStrike, 0)}–${_fmtNum(callWall?.maxStrike, 0)}` });
  }

  if (gammaState.layers.clusters) {
    const mk = (c, side) => {
      const peak = Number(c?.peakStrike ?? c?.centerStrike);
      const lo = Number(c?.minStrike);
      const hi = Number(c?.maxStrike);
      const total = Number(c?.totalOI);
      const sideLabel = side === "P" ? "Put cluster" : "Call cluster";
      const detail = `peak ${_fmtNum(peak, 0)} · totalOI ${_fmtNum(total, 0)} · band ${_fmtNum(lo, 0)}–${_fmtNum(hi, 0)} · n ${_fmtNum(c?.nStrikes, 0)}`;
      // Represent as two lines (lo/hi) but same tooltip.
      if (Number.isFinite(lo)) overlayLines.push({ kind: "cluster", y: lo, title: sideLabel, detail });
      if (Number.isFinite(hi)) overlayLines.push({ kind: "cluster", y: hi, title: sideLabel, detail });
    };
    (Array.isArray(oi?.putClusters) ? oi.putClusters : []).slice(0, 3).forEach(c => mk(c, "P"));
    (Array.isArray(oi?.callClusters) ? oi.callClusters : []).slice(0, 3).forEach(c => mk(c, "C"));
  }

  if (gammaState.layers.gammaPeaks) {
    const tops = Array.isArray(dg?.topGammaStrikes) ? dg.topGammaStrikes : [];
    tops.slice(0, 5).forEach((t) => {
      const y = Number(t?.strike);
      if (!Number.isFinite(y)) return;
      const side = String(t?.side || "");
      const title = "Gamma peak";
      const detail = `strike ${_fmtNum(y, 0)} · side ${escapeHtml(side)} · gex ${_fmtNum(t?.gex, 0)}`;
      overlayLines.push({ kind: "gammaPeak", y, title, detail });
    });
  }

  if (gammaState.layers.gammaFlip && Number.isFinite(flip)) {
    overlayLines.push({ kind: "gammaFlip", y: flip, title: "Gamma flip", detail: `~${_fmtNum(flip, 0)} (best-effort proxy)` });
  }

  // --- Render SVG chart ---
  const w = Math.max(320, chart.clientWidth || 640);
  const h = 260;
  const pad = { l: 10, r: 10, t: 10, b: 10 };
  const pw = w - pad.l - pad.r;
  const ph = h - pad.t - pad.b;

  const closes = series.map(p => Number(p?.close)).filter(Number.isFinite);
  const lvlYs = overlayLines.map(o => Number(o?.y)).filter(Number.isFinite);
  let yMin = Math.min(...closes, ...(lvlYs.length ? lvlYs : [Number.POSITIVE_INFINITY]));
  let yMax = Math.max(...closes, ...(lvlYs.length ? lvlYs : [Number.NEGATIVE_INFINITY]));
  if (!Number.isFinite(yMin) || !Number.isFinite(yMax) || yMin === yMax) {
    yMin = (Number.isFinite(spot) ? spot * 0.98 : 4000);
    yMax = (Number.isFinite(spot) ? spot * 1.02 : 5000);
  }
  const yPad = (yMax - yMin) * 0.06;
  yMin -= yPad;
  yMax += yPad;

  const xForIdx = (i) => pad.l + (pw * (i / Math.max(1, series.length - 1)));
  const yForVal = (v) => pad.t + (ph * (1 - ((v - yMin) / (yMax - yMin))));

  const pts = series.map((p, i) => {
    const y = yForVal(Number(p?.close));
    return `${xForIdx(i)},${y}`;
  }).join(" ");

  chart.innerHTML = `
    <svg class="gammaSvg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${escapeHtml(underLabel)} close chart">
      <rect x="0" y="0" width="${w}" height="${h}" class="gammaBg"></rect>
      <polyline points="${pts}" class="gammaPrice"></polyline>
      ${overlayLines.map((o, idx) => {
        const y = yForVal(Number(o.y));
        return `<line x1="${pad.l}" x2="${w - pad.r}" y1="${y}" y2="${y}" class="gammaLine gammaLine--${escapeHtml(o.kind)}" data-idx="${idx}"></line>`;
      }).join("")}
      <line x1="${pad.l}" x2="${pad.l}" y1="${pad.t}" y2="${h - pad.b}" class="gammaCross gammaCross--v hidden"></line>
      <line x1="${pad.l}" x2="${w - pad.r}" y1="${pad.t}" y2="${pad.t}" class="gammaCross gammaCross--h hidden"></line>
      <circle cx="${pad.l}" cy="${pad.t}" r="3" class="gammaDot hidden"></circle>
    </svg>
  `;

  const svg = chart.querySelector("svg");
  if (!svg) return;
  const vLine = svg.querySelector(".gammaCross--v");
  const hLine = svg.querySelector(".gammaCross--h");
  const dot = svg.querySelector(".gammaDot");

  const clearHover = () => {
    const lines = Array.from(svg.querySelectorAll(".gammaLine"));
    lines.forEach(l => l.classList.remove("isHover"));
    if (tip) tip.classList.add("hidden");
  };

  const showTip = (html, x, y) => {
    if (!tip) return;
    tip.innerHTML = html;
    tip.classList.remove("hidden");
    const box = chart.getBoundingClientRect();
    const left = clamp(x - box.left + 12, 8, box.width - 240);
    const top = clamp(y - box.top + 12, 8, box.height - 120);
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  };

  svg.addEventListener("mouseleave", () => {
    clearHover();
    [vLine, hLine, dot].forEach(el => el && el.classList.add("hidden"));
  });

  svg.addEventListener("mousemove", (ev) => {
    const box = svg.getBoundingClientRect();
    const mx = ev.clientX - box.left;
    const my = ev.clientY - box.top;
    const inPlot = (mx >= pad.l && mx <= (w - pad.r) && my >= pad.t && my <= (h - pad.b));
    if (!inPlot) {
      clearHover();
      [vLine, hLine, dot].forEach(el => el && el.classList.add("hidden"));
      return;
    }

    // Crosshair / nearest point
    const idx = Math.round(((mx - pad.l) / pw) * (series.length - 1));
    const i = clamp(idx, 0, series.length - 1);
    const pt = series[Number(i)] || {};
    const px = xForIdx(Number(i));
    const py = yForVal(Number(pt?.close));

    if (vLine) {
      vLine.setAttribute("x1", String(px));
      vLine.setAttribute("x2", String(px));
      vLine.classList.remove("hidden");
    }
    if (hLine) {
      hLine.setAttribute("y1", String(py));
      hLine.setAttribute("y2", String(py));
      hLine.classList.remove("hidden");
    }
    if (dot) {
      dot.setAttribute("cx", String(px));
      dot.setAttribute("cy", String(py));
      dot.classList.remove("hidden");
    }

    // Nearest overlay line by y-distance (pixels)
    const lines = Array.from(svg.querySelectorAll(".gammaLine"));
    lines.forEach(l => l.classList.remove("isHover"));

    let bestIdx = null;
    let bestDist = null;
    overlayLines.forEach((o, j) => {
      const yy = yForVal(Number(o.y));
      const d = Math.abs(yy - my);
      if (d <= 6 && (bestDist === null || d < bestDist)) {
        bestDist = d;
        bestIdx = j;
      }
    });

    const priceHtml = `
      <div class="chartTipTitle">${escapeHtml(underLabel)}</div>
      <div class="chartTipBody mono">${escapeHtml(_fmtDateShort(pt?.date))} · ${escapeHtml(_fmtNum(pt?.close, 2))}</div>
    `;

    if (bestIdx !== null) {
      const o = overlayLines[bestIdx];
      const lineEl = svg.querySelector(`.gammaLine[data-idx="${bestIdx}"]`);
      if (lineEl) lineEl.classList.add("isHover");
      const html = `
        ${priceHtml}
        <div class="chartTipDivider"></div>
        <div class="chartTipTitle">${escapeHtml(o.title)}</div>
        <div class="chartTipBody">${escapeHtml(o.detail)}</div>
      `;
      showTip(html, ev.clientX, ev.clientY);
    } else {
      showTip(priceHtml, ev.clientX, ev.clientY);
    }
  });
}

function getMacroCap() {
  const cap = Number(window.__FLAGS?.ENGINE2_MACRO_MULTIPLIER_CAP);
  return Number.isFinite(cap) && cap > 1 ? cap : 1.8;
}

function macroLevel(mult, cap) {
  const m = Number(mult);
  const c = Number(cap);
  if (!Number.isFinite(m) || !Number.isFinite(c) || c <= 1) return "—";
  const t = (m - 1.0) / (c - 1.0); // 0..1
  if (t < 0.25) return `Low (near baseline)`;
  if (t < 0.60) return `Moderate`;
  return `High (near cap)`;
}

function toCsv(rows) {
  const esc = (v) => {
    const s = String(v ?? "");
    if (s.includes(",") || s.includes('"') || s.includes("\n")) return `"${s.replaceAll('"', '""')}"`;
    return s;
  };
  if (!rows || !rows.length) return "";
  const cols = Object.keys(rows[0]);
  const head = cols.join(",");
  const body = rows.map(r => cols.map(c => esc(r[c])).join(",")).join("\n");
  return `${head}\n${body}\n`;
}

function downloadText(filename, text) {
  const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function downloadBlob(filename, blob) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function _mdTable(headers, rows) {
  const esc = (v) => String(v ?? "").replaceAll("\n", " ").replaceAll("|", "\\|");
  const head = `| ${headers.map(esc).join(" | ")} |`;
  const bar = `| ${headers.map(() => "---").join(" | ")} |`;
  const body = (rows || []).map((r) => `| ${headers.map((h) => esc(r[h])).join(" | ")} |`).join("\n");
  return [head, bar, body].filter(Boolean).join("\n");
}

function buildEngine2OnePageMarkdown({ payload, levels, uiState }) {
  const sym = String(payload?.underlying?.symbol || "—").toUpperCase();
  const asOf = String(payload?.asOfDate || "—");
  const spot = payload?.current?.vwap?.livePrice ?? payload?.liveContext?.weeklyFriday?.dealerGamma?.spot ?? payload?.liveContext?.spot;
  const spotTxt = Number.isFinite(Number(spot)) ? Number(spot).toFixed(2) : "—";

  const reg = payload?.current?.regime || {};
  const regComp = reg?.components || {};
  const macro = payload?.current?.macro || {};
  const flags = macro?.flags || {};
  const like = payload?.oddsLikeNow || {};
  const rows = Array.isArray(like?.byWidth) ? like.byWidth : [];
  const vwap = payload?.current?.vwap || {};
  const lc = payload?.liveContext || {};
  const w = lc?.weeklyFriday || null;
  const n = lc?.nearestDaily || null;

  const heat = levels?.levels?.gexHeatmap || null;
  const hm = heat?.metrics || {};
  const hb = heat?.boundaries || {};
  const hs = heat?.stability || {};

  // Flattened key/value index for fast lookup.
  const kv = [];
  const add = (k, v) => kv.push({ key: k, value: (v === null || v === undefined || v === "" ? "—" : String(v)) });

  add("engine2.asOfDate", asOf);
  add("engine2.underlying.symbol", sym);
  add("engine2.spot", spotTxt);
  add("engine2.ui.underlyingSelected", uiState?.underlyingSelected);
  add("engine2.ui.entryDay", uiState?.entryDay);
  add("engine2.ui.seasonalityMode", uiState?.seasonalityMode);
  add("engine2.ui.gammaView", uiState?.gammaView);
  add("engine2.ui.heatmapView", uiState?.heatmapView);
  add("engine2.ui.heatmapMode", uiState?.heatmapMode);

  add("engine2.regime.score100", reg?.score100);
  add("engine2.regime.bucket", reg?.bucket);
  add("engine2.regime.component.trend", regComp?.trend);
  add("engine2.regime.component.volatility", regComp?.volatility);
  add("engine2.regime.component.stress", regComp?.stress);
  add("engine2.regime.component.event", regComp?.event);
  add("engine2.regime.component.dispersion", regComp?.dispersion);

  add("engine2.macro.multiplier", macro?.multiplier);
  add("engine2.macro.flag.CPI", flags?.CPI);
  add("engine2.macro.flag.FOMC", flags?.FOMC);
  add("engine2.macro.flag.NFP", flags?.NFP);
  add("engine2.macro.flag.OPEX", flags?.OPEX);
  add("engine2.macro.flag.REFUNDING", flags?.REFUNDING);
  add("engine2.oddsLikeNow.weeksUsed", like?.weeksUsed);
  add("engine2.oddsLikeNow.regimeBucket", like?.regimeBucket);
  add("engine2.oddsLikeNow.macroBucket", like?.macroBucket);
  add("engine2.oddsLikeNow.seasonBucket", like?.seasonBucket);

  add("engine2.vwap.enabled", vwap?.enabled);
  add("engine2.vwap.value", vwap?.value);
  add("engine2.vwap.distance", vwap?.distance ? JSON.stringify(vwap.distance) : "—");

  // Dealer gamma + addons (weekly + nearest)
  const wdg = w?.dealerGamma || {};
  const ndg = n?.dealerGamma || {};
  add("engine2.live.weekly.expiry", w?.expiry);
  add("engine2.live.weekly.dealerGamma.netGammaSign", wdg?.netGammaSign);
  add("engine2.live.weekly.dealerGamma.magnitudeBucket", wdg?.magnitudeBucket);
  add("engine2.live.weekly.gammaFlipStrike", w?.gammaFlipStrike);
  add("engine2.live.nearest.expiry", n?.expiry);
  add("engine2.live.nearest.dealerGamma.netGammaSign", ndg?.netGammaSign);
  add("engine2.live.nearest.dealerGamma.magnitudeBucket", ndg?.magnitudeBucket);
  add("engine2.live.nearest.gammaFlipStrike", n?.gammaFlipStrike);

  const whp = w?.addons?.hedgingPressure || {};
  const nhp = n?.addons?.hedgingPressure || {};
  add("engine2.hpi.weekly.elasticity50bp", whp?.elasticity50bp);
  add("engine2.hpi.weekly.elasticityBucket", whp?.elasticityBucket);
  add("engine2.hpi.weekly.gammaTotal", whp?.gammaTotal);
  add("engine2.hpi.nearest.elasticity50bp", nhp?.elasticity50bp);
  add("engine2.hpi.nearest.elasticityBucket", nhp?.elasticityBucket);
  add("engine2.hpi.nearest.gammaTotal", nhp?.gammaTotal);

  const wt = w?.addons?.tailIgnition || {};
  const nt = n?.addons?.tailIgnition || {};
  add("engine2.tail.weekly.down.score", wt?.down?.score);
  add("engine2.tail.weekly.up.score", wt?.up?.score);
  add("engine2.tail.weekly.distToPutWallPct", wt?.distToPutWallPct);
  add("engine2.tail.weekly.distToCallWallPct", wt?.distToCallWallPct);
  add("engine2.tail.nearest.down.score", nt?.down?.score);
  add("engine2.tail.nearest.up.score", nt?.up?.score);

  const vp = lc?.volPressure || {};
  add("engine2.volPressure.state", vp?.state);
  add("engine2.volPressure.scoreZ", vp?.scoreZ);
  add("engine2.volPressure.inputs.iv7", vp?.inputs?.iv7);
  add("engine2.volPressure.inputs.iv30", vp?.inputs?.iv30);
  add("engine2.volPressure.inputs.rv10", vp?.inputs?.rv10);
  add("engine2.volPressure.inputs.termSlope", vp?.inputs?.termSlope);

  // Levels / heatmap highlights (from /api/spx-levels export)
  add("engine2.levels.gexHeatmap.enabled", heat?.enabled);
  add("engine2.levels.gexHeatmap.stability.label", hs?.label);
  add("engine2.levels.gexHeatmap.metrics.downsideDistancePts", hm?.downsideDistancePts);
  add("engine2.levels.gexHeatmap.metrics.upsideDistancePts", hm?.upsideDistancePts);
  add("engine2.levels.gexHeatmap.metrics.downsideDistanceEm", hm?.downsideDistanceEm);
  add("engine2.levels.gexHeatmap.metrics.upsideDistanceEm", hm?.upsideDistanceEm);
  add("engine2.levels.gexHeatmap.boundary.downStrike", hb?.downsideAccelerationBoundaryStrike);
  add("engine2.levels.gexHeatmap.boundary.upStrike", hb?.upsideAccelerationBoundaryStrike);

  const lines = [];
  lines.push(`# ${sym} — Engine 2 (One Page)`);
  lines.push("");
  lines.push(`If you need a number, reference it by the **Key** in the Key/Value index below (stable keys).`);
  lines.push("");
  lines.push("## Key/Value index");
  lines.push(_mdTable(["key", "value"], kv));
  lines.push("");

  // Embedded key tables
  lines.push("## Odds like now (by width)");
  if (rows.length) {
    const table = rows.map((r) => ({
      w: r?.w,
      n: r?.n,
      breachEitherPct: r?.breachEitherPct,
      breachPutPct: r?.breachPutPct,
      breachCallPct: r?.breachCallPct,
      avgAbsRetPct: r?.avgAbsRetPct,
    }));
    lines.push(_mdTable(["w", "n", "breachEitherPct", "breachPutPct", "breachCallPct", "avgAbsRetPct"], table));
  } else {
    lines.push("_No rows._");
  }
  lines.push("");

  lines.push("## Macro high-impact US events");
  const hiTop = Array.isArray(macro?.highImpactUS?.top) ? macro.highImpactUS.top : [];
  if (hiTop.length) hiTop.forEach((x) => lines.push(`- ${String(x)}`));
  else lines.push("_None._");
  lines.push("");

  return lines.join("\n");
}

function buildEngine2SnapshotMarkdown({ payload, levels, uiState }) {
  const sym = String(payload?.underlying?.symbol || "—").toUpperCase();
  const asOf = String(payload?.asOfDate || "—");
  const spot = payload?.current?.vwap?.livePrice ?? payload?.liveContext?.weeklyFriday?.dealerGamma?.spot ?? payload?.liveContext?.spot;
  const spotTxt = Number.isFinite(Number(spot)) ? Number(spot).toFixed(2) : "—";

  const reg = payload?.current?.regime || {};
  const macro = payload?.current?.macro || {};
  const vwap = payload?.current?.vwap || {};
  const like = payload?.oddsLikeNow || {};
  const lc = payload?.liveContext || {};

  const lines = [];
  lines.push(`# ${sym} — Engine 2 Export`);
  lines.push("");
  lines.push(`- generatedAt: ${new Date().toISOString()}`);
  lines.push(`- asOf: ${asOf}`);
  lines.push(`- spot: ${spotTxt}`);
  lines.push(`- url: ${String(uiState?.url || "")}`);
  lines.push("");

  lines.push("## UI state");
  lines.push("```json");
  lines.push(JSON.stringify(uiState || {}, null, 2));
  lines.push("```");
  lines.push("");

  lines.push("## Regime");
  lines.push(`- score100: ${reg?.score100 ?? "—"}`);
  lines.push(`- bucket: ${reg?.bucket ?? "—"}`);
  if (reg?.components) lines.push(`- components: ${JSON.stringify(reg.components)}`);
  lines.push("");

  lines.push("## Macro");
  lines.push(`- multiplier: ${macro?.multiplier ?? "—"}`);
  if (macro?.flags) lines.push(`- flags: ${JSON.stringify(macro.flags)}`);
  const hiTop = Array.isArray(macro?.highImpactUS?.top) ? macro.highImpactUS.top : [];
  if (hiTop.length) {
    lines.push("");
    lines.push("### High-impact US events");
    hiTop.forEach((x) => lines.push(`- ${String(x)}`));
  }
  lines.push("");

  lines.push("## Odds like now (by width)");
  const bw = Array.isArray(like?.byWidth) ? like.byWidth : [];
  if (bw.length) {
    const table = bw.map((r) => ({
      w: r?.w,
      n: r?.n,
      breachEitherPct: r?.breachEitherPct,
      breachPutPct: r?.breachPutPct,
      breachCallPct: r?.breachCallPct,
      avgAbsRetPct: r?.avgAbsRetPct,
    }));
    lines.push(_mdTable(["w", "n", "breachEitherPct", "breachPutPct", "breachCallPct", "avgAbsRetPct"], table));
  } else {
    lines.push("_No odds rows._");
  }
  lines.push("");

  lines.push("## VWAP");
  lines.push(`- enabled: ${vwap?.enabled ?? false}`);
  lines.push(`- value: ${vwap?.value ?? "—"}`);
  lines.push(`- barDateUsed: ${vwap?.barDateUsed ?? "—"}`);
  lines.push(`- livePrice: ${vwap?.livePrice ?? "—"}`);
  if (vwap?.distance) lines.push(`- distance: ${JSON.stringify(vwap.distance)}`);
  if (Array.isArray(vwap?.notes) && vwap.notes.length) lines.push(`- notes: ${vwap.notes.join(" | ")}`);
  lines.push("");

  function _viewSummary(view) {
    const out = {};
    out.enabled = !!view?.enabled;
    out.symbolUsed = view?.symbolUsed;
    out.expiry = view?.expiry;
    out.spot = view?.spot;
    out.bandPct = view?.bandPct;
    out.atmIvPct = view?.atmIvPct;
    out.gammaFlipStrike = view?.gammaFlipStrike;
    out.dealerGamma = view?.dealerGamma;
    out.oiClusters = view?.oiClusters;
    out.addons = view?.addons;
    out.warnings = view?.warnings;
    out.notes = view?.notes;
    return out;
  }

  lines.push("## Live context (weeklyFriday)");
  lines.push("```json");
  lines.push(JSON.stringify(_viewSummary(lc?.weeklyFriday || {}), null, 2));
  lines.push("```");
  lines.push("");

  lines.push("## Live context (nearestDaily)");
  lines.push("```json");
  lines.push(JSON.stringify(_viewSummary(lc?.nearestDaily || {}), null, 2));
  lines.push("```");
  lines.push("");

  lines.push("## Vol pressure");
  lines.push("```json");
  lines.push(JSON.stringify(lc?.volPressure || {}, null, 2));
  lines.push("```");
  lines.push("");

  lines.push("## Levels payload (spx-levels)");
  if (levels) {
    const heat = levels?.levels?.gexHeatmap || null;
    const keep = {
      schemaVersion: levels?.schemaVersion,
      priceSeriesPoints: Array.isArray(levels?.priceSeries) ? levels.priceSeries.length : 0,
      view: levels?.levels?.view,
      symbolUsed: levels?.levels?.symbolUsed,
      expiry: levels?.levels?.expiry,
      spot: levels?.levels?.spot,
      gammaFlipStrike: levels?.levels?.gammaFlipStrike,
      heatmap: heat
        ? {
            enabled: heat.enabled,
            spot: heat.spot,
            bandPct: heat.bandPct,
            atmIvUsedPct: heat.atmIvUsedPct,
            metrics: heat.metrics,
            stability: heat.stability,
            boundaries: heat.boundaries,
            warnings: heat.warnings,
            notes: heat.notes,
          }
        : null,
      warnings: levels?.levels?.warnings,
      notes: levels?.levels?.notes,
    };
    lines.push("```json");
    lines.push(JSON.stringify(keep, null, 2));
    lines.push("```");
  } else {
    lines.push("_Levels payload unavailable._");
  }
  lines.push("");

  lines.push("## Notes / Warnings");
  const notes = Array.isArray(payload?.notes) ? payload.notes : [];
  const warns = Array.isArray(lc?.warnings) ? lc.warnings : [];
  if (notes.length) lines.push(`- engineNotes: ${notes.join(" | ")}`);
  if (warns.length) lines.push(`- liveWarnings: ${warns.join(" | ")}`);

  return lines.join("\n");
}

function _engine2ExportFileNameBase(payload) {
  const sym = String(payload?.underlying?.symbol || "SPX").toUpperCase();
  const asOf = String(payload?.asOfDate || "").slice(0, 10) || "asof";
  return `engine2-export-${sym}-${asOf}`;
}

async function _ensureEngine2LevelsPayload() {
  if (lastGammaPayload) return lastGammaPayload;
  const v = gammaState.view;
  const under = encodeURIComponent(String(engine2UnderlyingState.symbol || "SPX"));
  const payload = await fetchJson(
    `/api/spx-levels?underlying=${under}&view=${encodeURIComponent(v)}&points=90&window_days=180&include_heatmap=1`
    + `&heatmap_view=${encodeURIComponent(gexState.view)}`
    + `&heatmap_mode=${encodeURIComponent(gexState.mode)}`
    + `&slope_window=5&flip_adjacent_n=5`,
    { timeoutMs: 45000 }
  );
  lastGammaPayload = payload;
  return payload;
}

async function exportEngine2LLMBundle() {
  const status = $("status");
  const payload = lastPayload;
  if (!payload) {
    if (status) {
      status.textContent = "Export: run Engine 2 first (no payload yet).";
      status.classList.add("isError");
      status.classList.remove("hidden");
    }
    return;
  }

  try {
    if (status) {
      status.textContent = "Exporting…";
      status.classList.remove("isError");
      status.classList.add("isRunning");
      status.classList.remove("hidden");
    }

    const levels = await _ensureEngine2LevelsPayload().catch(() => null);

    const uiState = {
      engine: "engine2",
      url: String(window.location?.href || ""),
      underlyingSelected: String(engine2UnderlyingState.symbol || ""),
      entryDay: String($("entryDay")?.value || ""),
      seasonalityMode: String($("seasonalityMode")?.value || ""),
      gammaView: String(gammaState.view || ""),
      gammaLayers: { ...(gammaState.layers || {}) },
      heatmapView: String(gexState.view || ""),
      heatmapMode: String(gexState.mode || ""),
    };

    const base = _engine2ExportFileNameBase(payload);
    const zip = window.ZipStore ? new window.ZipStore() : null;
    if (!zip) throw new Error("ZIP module missing (ZipStore not loaded).");

    // snapshot.md
    const md = buildEngine2SnapshotMarkdown({ payload, levels, uiState });
    zip.addText("snapshot.md", md);
    const onePageMd = buildEngine2OnePageMarkdown({ payload, levels, uiState });
    zip.addText("one_page.md", onePageMd);

    // Raw payloads
    zip.addText("payload.engine2.json", JSON.stringify(payload, null, 2));
    if (levels) zip.addText("payload.levels.json", JSON.stringify(levels, null, 2));
    zip.addText("ui_state.json", JSON.stringify(uiState, null, 2));

    // Tables
    const odds = Array.isArray(payload?.oddsLikeNow?.byWidth) ? payload.oddsLikeNow.byWidth : [];
    const oddsRows = odds.map((r) => ({
      width: r?.w,
      n: r?.n,
      breachEitherPct: r?.breachEitherPct,
      breachPutPct: r?.breachPutPct,
      breachCallPct: r?.breachCallPct,
      avgAbsRetPct: r?.avgAbsRetPct,
    }));
    zip.addText("tables/odds_by_width.csv", toCsv(oddsRows));

    const hiTop = Array.isArray(payload?.current?.macro?.highImpactUS?.top) ? payload.current.macro.highImpactUS.top : [];
    const macroRows = hiTop.map((s, i) => {
      const t = String(s || "");
      const date = t.slice(0, 10);
      const name = t.length > 11 ? t.slice(11) : "";
      return { idx: i + 1, date, name, raw: t };
    });
    zip.addText("tables/macro_events.csv", toCsv(macroRows));

    const vp = payload?.liveContext?.volPressure || {};
    const vpRow = {
      asOfDate: vp?.asOfDate,
      state: vp?.state,
      scoreZ: vp?.scoreZ,
      ...(vp?.inputs || {}),
      ...(vp?.z ? { z_dIv: vp.z.dIv, z_dSkew: vp.z.dSkew, z_ivRv: vp.z.ivRv, z_term: vp.z.term } : {}),
    };
    zip.addText("tables/vol_pressure_inputs.csv", toCsv([vpRow]));

    const blob = zip.toBlob();
    downloadBlob(`${base}.zip`, blob);

    if (status) {
      status.textContent = `Exported: ${base}.zip`;
      status.classList.remove("isRunning", "isError");
      status.classList.add("isOk");
      status.classList.remove("hidden");
      // auto-hide after a moment to keep UI clean
      window.setTimeout(() => status.classList.add("hidden"), 4000);
    }
  } catch (e) {
    if (status) {
      status.textContent = `Export error: ${String(e?.message || e)}`;
      status.classList.remove("isRunning");
      status.classList.add("isError");
      status.classList.remove("hidden");
    }
  }
}

async function exportEngine2OnePageOnly() {
  const status = $("status");
  const payload = lastPayload;
  if (!payload) {
    if (status) {
      status.textContent = "Export: run Engine 2 first (no payload yet).";
      status.classList.add("isError");
      status.classList.remove("hidden");
    }
    return;
  }

  try {
    if (status) {
      status.textContent = "Exporting one-page…";
      status.classList.remove("isError");
      status.classList.add("isRunning");
      status.classList.remove("hidden");
    }

    const levels = await _ensureEngine2LevelsPayload().catch(() => null);
    const uiState = {
      engine: "engine2",
      url: String(window.location?.href || ""),
      underlyingSelected: String(engine2UnderlyingState.symbol || ""),
      entryDay: String($("entryDay")?.value || ""),
      seasonalityMode: String($("seasonalityMode")?.value || ""),
      gammaView: String(gammaState.view || ""),
      gammaLayers: { ...(gammaState.layers || {}) },
      heatmapView: String(gexState.view || ""),
      heatmapMode: String(gexState.mode || ""),
    };

    const base = _engine2ExportFileNameBase(payload);
    const md = buildEngine2OnePageMarkdown({ payload, levels, uiState });
    const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    downloadBlob(`${base}-one_page.md`, blob);

    if (status) {
      status.textContent = `Exported: ${base}-one_page.md`;
      status.classList.remove("isRunning", "isError");
      status.classList.add("isOk");
      status.classList.remove("hidden");
      window.setTimeout(() => status.classList.add("hidden"), 3500);
    }
  } catch (e) {
    if (status) {
      status.textContent = `Export error: ${String(e?.message || e)}`;
      status.classList.remove("isRunning");
      status.classList.add("isError");
      status.classList.remove("hidden");
    }
  }
}

function renderEngine2DecisionPanel(payload) {
  const host = $("e2DecisionSection");
  if (!host) return;

  const sym = String(payload?.underlying?.symbol || "—").toUpperCase();
  const asOf = String(payload?.asOfDate || "—");

  const reg = payload?.current?.regime || {};
  const score = (reg?.score100 !== null && reg?.score100 !== undefined) ? Number(reg.score100) : null;
  const bucket = String(reg?.bucket || "—");
  const regComp = reg?.components || {};
  const regChips = [];
  if (regComp.trend !== null && regComp.trend !== undefined) regChips.push(`trend ${Number(regComp.trend).toFixed(2)}`);
  if (regComp.volatility !== null && regComp.volatility !== undefined) regChips.push(`vol ${Number(regComp.volatility).toFixed(2)}`);
  if (regComp.stress !== null && regComp.stress !== undefined) regChips.push(`stress ${Number(regComp.stress).toFixed(2)}`);
  if (regComp.event !== null && regComp.event !== undefined) regChips.push(`event ${Number(regComp.event).toFixed(2)}`);
  if (regComp.dispersion !== null && regComp.dispersion !== undefined) regChips.push(`disp ${Number(regComp.dispersion).toFixed(2)}`);

  const macro = payload?.current?.macro || {};
  const multVal = (macro?.multiplier !== null && macro?.multiplier !== undefined) ? Number(macro.multiplier) : null;
  const flags = macro?.flags || {};
  const hi = ["CPI","FOMC","NFP"].some(k => flags && flags[k]);
  const macroBucket = (Number.isFinite(multVal) && (multVal >= 1.25 || hi)) ? "MACRO" : "NORMAL";
  const hiCount = macro?.highImpactUS?.count;
  const hiTop = Array.isArray(macro?.highImpactUS?.top) ? macro.highImpactUS.top : [];
  const top3 = hiTop.slice(0, 3);
  const moreN = Math.max(0, hiTop.length - top3.length);
  const macroChipList = ["CPI", "FOMC", "NFP", "OPEX", "REFUNDING"].filter((k) => flags && flags[k]).slice(0, 2);

  const vwap = payload?.current?.vwap || {};
  const vwapEnabled = !!vwap?.enabled;
  const vwapVal = vwapEnabled ? Number(vwap?.value) : null;
  const lp = Number(vwap?.livePrice);
  const spot = Number.isFinite(lp) ? lp : Number(payload?.liveContext?.weeklyFriday?.dealerGamma?.spot);
  const spotTxt = Number.isFinite(spot) ? spot.toFixed(2) : "—";
  const d = vwap?.distance || null;
  const dp = Number(d?.diffPts);
  const dpc = Number(d?.diffPct);
  const side = String(d?.side || "");
  let vwapDist = "—";
  if (Number.isFinite(dp)) {
    const absPts = Math.abs(dp).toFixed(2);
    const pctTxt = Number.isFinite(dpc) ? `${Math.abs(dpc).toFixed(2)}%` : "—";
    if (side === "above") vwapDist = `spot above by ${absPts} (${pctTxt})`;
    else if (side === "below") vwapDist = `spot below by ${absPts} (${pctTxt})`;
    else if (side === "at") vwapDist = `spot ≈ VWAP`;
    else vwapDist = `Δ=${dp.toFixed(2)}`;
  }

  const like = payload?.oddsLikeNow || {};
  const rows = Array.isArray(like?.byWidth) ? like.byWidth : [];
  const row10 = rows.find(r => Number(r?.w) === 1.0) || rows[0] || null;
  const odds10 = (row10 && row10.breachEitherPct !== null && row10.breachEitherPct !== undefined) ? Number(row10.breachEitherPct) : null;
  const n10 = row10 ? Number(row10?.n) : null;
  const row15 = rows.find(r => Number(r?.w) === 1.5) || null;
  const row20 = rows.find(r => Number(r?.w) === 2.0) || null;

  const lc = payload?.liveContext || null;
  const weekly = lc?.weeklyFriday || null;
  const nearest = lc?.nearestDaily || null;

  function _dgSummary(view) {
    const dg = view?.dealerGamma || null;
    if (!(view && view.enabled && dg && dg.netGammaSign)) {
      const notes = Array.isArray(view?.notes) ? view.notes.filter(Boolean) : [];
      const warn = Array.isArray(view?.warnings) ? view.warnings.filter(Boolean) : [];
      return { main: "—", sub: notes[0] || warn[0] || "Live context unavailable." };
    }
    const sign = String(dg.netGammaSign || "").toUpperCase();
    const mag = String(dg.magnitudeBucket || "").toUpperCase();
    const symUsed = String(view.symbolUsed || "—").toUpperCase();
    const exp = String(view.expiry || "—");
    const band = Math.round(Number(dg.bandPct || 0.05) * 100);
    const oi = view?.oiClusters || null;
    const putWall = oi && typeof oi === "object" ? oi.putWall : null;
    const callWall = oi && typeof oi === "object" ? oi.callWall : null;
    const putStrike = putWall && (putWall.peakStrike ?? putWall.maxStrike);
    const callStrike = callWall && (callWall.peakStrike ?? callWall.maxStrike);
    const putTxt = putWall && Number.isFinite(Number(putStrike)) ? `${Number(putStrike).toFixed(0)} (${Number(putWall.totalOI || 0).toFixed(0)})` : "—";
    const callTxt = callWall && Number.isFinite(Number(callStrike)) ? `${Number(callStrike).toFixed(0)} (${Number(callWall.totalOI || 0).toFixed(0)})` : "—";
    return {
      main: `${symUsed} · ${sign} · ${mag}`,
      sub: `expiry=${exp} · band=±${band}% · walls: put=${putTxt} | call=${callTxt}`,
    };
  }

  function _oiDetail(view) {
    const oi = view?.oiClusters || null;
    const enabled = !!(view && view.enabled && oi && typeof oi === "object");
    if (!enabled) return { meta: "—", put: "Put: —", call: "Call: —" };
    const spot = Number(oi.spot);
    const step = Number(oi.strikeStep);
    const band = Number(oi.bandPct);
    const expiry = String(oi.expiry || view.expiry || "—");
    const meta = `expiry=${expiry} · spot=${fmt0(spot)} · band=±${Math.round((Number.isFinite(band) ? band : 0.05) * 100)}% · step=${fmt0(step)}`;
    const puts = _pickMeaningfulClusters(oi.putClusters, spot, step).map(_fmtClusterLine);
    const calls = _pickMeaningfulClusters(oi.callClusters, spot, step).map(_fmtClusterLine);
    return {
      meta,
      put: puts.length ? `Put: ${puts.join(" | ")}` : "Put: —",
      call: calls.length ? `Call: ${calls.join(" | ")}` : "Call: —",
    };
  }

  const wSum = _dgSummary(weekly);
  const nSum = _dgSummary(nearest);
  const wOi = _oiDetail(weekly);
  const nOi = _oiDetail(nearest);

  function _hpSummary(view) {
    const hp = view?.addons?.hedgingPressure || null;
    const enabled = !!(view && view.enabled && hp && hp.enabled);
    if (!enabled) return { main: "—", sub: "Live hedging-pressure unavailable." };

    const bucket = String(hp.elasticityBucket || "").toUpperCase();
    const e = (hp.elasticity50bp === null || hp.elasticity50bp === undefined) ? null : Number(hp.elasticity50bp);
    const scen = Array.isArray(hp.scenarios) ? hp.scenarios : [];
    const s50 = scen.find(x => Number(x?.movePct) === 0.5) || null;
    const n50 = s50 ? Number(s50.hedgeNotional) : null;

    const main = (e !== null && Number.isFinite(e))
      ? `${(e * 100).toFixed(2)}% ADV${bucket ? ` ${bucket}` : ""}`
      : (Number.isFinite(n50) ? `${fmtMoneyShort(n50)} @50bp` : "—");

    const gTot = Number(hp.gammaTotal);
    const gTxt = Number.isFinite(gTot) ? `Γ=${gTot.toExponential(2)}` : "Γ=—";
    const band = Math.round(Number(hp.bandPct || 0.05) * 100);
    const sub = `${gTxt} · band=±${band}% · strikes=${fmt0(hp.strikesUsed)}`;
    return { main, sub };
  }

  function _tailSummary(view) {
    const t = view?.addons?.tailIgnition || null;
    const enabled = !!(view && view.enabled && t && t.enabled);
    if (!enabled) return { main: "—", sub: "Tail ignition unavailable." };

    const d = t.down || {};
    const u = t.up || {};
    const dScore = Number(d.score);
    const uScore = Number(u.score);
    const dLbl = String(d.label || "—").toUpperCase();
    const uLbl = String(u.label || "—").toUpperCase();

    const main = `Down ${Number.isFinite(dScore) ? dScore : "—"} ${dLbl} · Up ${Number.isFinite(uScore) ? uScore : "—"} ${uLbl}`;

    const dp = (t.distToPutWallPct === null || t.distToPutWallPct === undefined) ? null : Number(t.distToPutWallPct);
    const cp = (t.distToCallWallPct === null || t.distToCallWallPct === undefined) ? null : Number(t.distToCallWallPct);
    const fp = (t.flipDistancePct === null || t.flipDistancePct === undefined) ? null : Number(t.flipDistancePct);
    const sub = `walls: put=${(dp !== null && Number.isFinite(dp)) ? dp.toFixed(2) + "%" : "—"} · call=${(cp !== null && Number.isFinite(cp)) ? cp.toFixed(2) + "%" : "—"} · flip=${(fp !== null && Number.isFinite(fp)) ? fp.toFixed(2) + "%" : "—"}`;
    return { main, sub };
  }

  const wHp = _hpSummary(weekly);
  const nHp = _hpSummary(nearest);
  const wTail = _tailSummary(weekly);
  const nTail = _tailSummary(nearest);

  const vp = lc?.volPressure || null;
  const vpEnabled = !!(vp && vp.enabled);
  const vpState = vpEnabled ? String(vp.state || "—") : "—";
  const vpScore = (vpEnabled && vp.scoreZ !== null && vp.scoreZ !== undefined) ? Number(vp.scoreZ) : null;
  const vpInp = vpEnabled ? (vp.inputs || {}) : {};
  const _vpNum = (v, d = 2) => {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(d) : "—";
  };
  const vpSub = vpEnabled
    ? `iv7=${_vpNum(vpInp.iv7, 2)} · rv10=${_vpNum(vpInp.rv10, 2)} · term=${_vpNum(vpInp.termSlope, 2)}`
    : "Vol pressure unavailable.";

  // --- Expected Move (weekly Friday options only) ---
  const em = payload?.expectedMove || {};
  const emEnabled = !!em?.enabled;
  const emPct = (emEnabled && em?.expectedMovePct != null) ? Number(em.expectedMovePct) : NaN;
  const emDollars = (emEnabled && em?.expectedMoveDollars != null) ? Number(em.expectedMoveDollars) : NaN;
  const emExpiry = String(em?.expiry || "").slice(0, 10);
  const emDte = (em?.dte !== null && em?.dte !== undefined) ? Number(em.dte) : null;
  const emSource = String(em?.source || "").toLowerCase();
  const emSourceLabel = emSource === "live" ? "Live" : emSource === "eod" ? "EOD" : emSource ? emSource : "—";
  const emSymbol = String(em?.symbolUsed || "").toUpperCase();
  const oratsEodEmPct = (em?.eodImpliedMovePct != null) ? Number(em.eodImpliedMovePct) : NaN;
  const oratsEodAsOf = String(em?.eodAsOfDate || "").slice(0, 10);
  const delayedEmPct = (em?.delayedImpliedMovePct != null) ? Number(em.delayedImpliedMovePct) : NaN;
  const delayedUpdatedAt = String(em?.delayedUpdatedAt || "").trim();
  const delayedTradeDate = String(em?.delayedTradeDate || "").slice(0, 10);

  // --- Strike Targets ---
  const st = payload?.strikeTargets || null;
  const stWhite = (st?.whitePct !== null && st?.whitePct !== undefined) ? Number(st?.whitePct) : null;
  const stBlue = (st?.bluePct !== null && st?.bluePct !== undefined) ? Number(st?.bluePct) : null;
  const stRed = (st?.redPct !== null && st?.redPct !== undefined) ? Number(st?.redPct) : null;
  const stEmSource = String(st?.emSource || "");
  const stSourceLabel = stEmSource === "delayed" ? "15-min delayed EM" : stEmSource === "eod" ? "ORATS EOD EM" : stEmSource === "straddle" ? "straddle EM fallback" : "";

  const dots = Array.from({ length: 5 }).map((_, i) => `<span class="taDot ${i < 3 ? "isOn" : ""}"></span>`).join("");
  const chips = [
    `Regime: ${bucket}`,
    `Macro: ${macroBucket}`,
    (Number.isFinite(n10) ? `n=${n10}` : null),
  ].filter(Boolean);
  const chipHtml = chips.slice(0, 3).map((c) => `<span class="taChip">${escapeHtml(c)}</span>`).join("");

  host.classList.toggle("hidden", !sym || sym === "—");
  if (!sym || sym === "—") return;

  host.innerHTML = `
    <div class="taPanel e2Conditions">
      <div class="taHeader">
        <div class="taHeaderRow">
          <div class="taHeaderTitle">${escapeHtml(sym)} — Engine 2</div>
          <div class="taHeaderMeta">asOf: ${escapeHtml(asOf)} • spot: <span class="mono">${escapeHtml(spotTxt)}</span></div>
        </div>
        <div class="taHeaderRow taHeaderRow--sub">
          <div class="taBiasPill taBiasPill--neu">WEEKLY IC</div>
          <div class="taConf" title="Confidence dots (heuristic)">${dots}</div>
          <div class="taChips">${chipHtml}</div>
          <div class="taHeaderActions">
            <button class="taActionBtn" type="button" id="e2ExportLLM">Export (LLM)</button>
            <button class="taActionBtn" type="button" id="e2ExportOnePage">Export One-Page (LLM)</button>
          </div>
        </div>
      </div>

      <div class="taGrid" aria-label="Engine 2 instrument cards">
        <div class="taCard e2Click" data-e2-insight="e2_regime" data-e2-title="Regime Score" title="Click for desk insight">
          <div class="taCardTop"><div class="taCardTitle">Regime score</div></div>
          <div class="taCardState mono">${Number.isFinite(score) ? escapeHtml(score.toFixed(1)) + " / 100" : "—"}</div>
          <div class="taCardInterp">Bucket: ${escapeHtml(bucket)}</div>
          ${regChips.length ? `<div class="taCardInterp muted">${regChips.slice(0, 5).map(escapeHtml).join(" · ")}</div>` : ``}
        </div>
        <div class="taCard e2Click" data-e2-insight="e2_macro" data-e2-title="Macro Multiplier" title="Click for desk insight">
          <div class="taCardTop"><div class="taCardTitle">Macro multiplier</div></div>
          <div class="taCardState mono">${Number.isFinite(multVal) ? escapeHtml(multVal.toFixed(2)) + "×" : "—"}</div>
          <div class="taCardInterp">Bucket: ${escapeHtml(macroBucket)}${macroChipList.length ? ` · ${macroChipList.map(escapeHtml).join(" · ")}` : ""}</div>
          ${top3.length ? `<div class="taCardInterp muted">${top3.map((x) => `<div class="macroEventLine">${escapeHtml(String(x))}</div>`).join("")}${moreN ? `<div class="ref">+${escapeHtml(String(moreN))} more</div>` : ""}</div>` : ``}
        </div>
        <div class="taCard e2Click" data-e2-insight="e2_odds" data-e2-title="Breach Odds" title="Click for desk insight">
          <div class="taCardTop"><div class="taCardTitle">Breach odds (1.0×)</div></div>
          <div class="taCardState mono">${Number.isFinite(odds10) ? escapeHtml(odds10.toFixed(2)) + "%" : "—"}</div>
          <div class="taCardInterp">${Number.isFinite(n10) ? `n=${escapeHtml(String(n10))}` : "—"}</div>
          <div class="taCardInterp muted">${row15 ? `1.5× ${fmtPct(row15?.breachEitherPct, 2)} (n=${escapeHtml(String(row15?.n ?? "—"))})` : ""}${row20 ? `<br/>2.0× ${fmtPct(row20?.breachEitherPct, 2)} (n=${escapeHtml(String(row20?.n ?? "—"))})` : ""}</div>
        </div>
        <div class="taCard e2Click" data-e2-insight="e2_expected_move" data-e2-title="VWAP & EM" title="Click for desk insight">
          <div class="taCardTop"><div class="taCardTitle">VWAP (daily)</div></div>
          <div class="taCardState mono">${Number.isFinite(vwapVal) ? escapeHtml(vwapVal.toFixed(2)) : "—"}</div>
          <div class="taCardInterp">${escapeHtml(vwapDist)}</div>
        </div>
        <div class="taCard e2Click" data-e2-insight="e2_dealer_gamma" data-e2-title="Dealer Gamma (Weekly)" title="Click for desk insight">
          <div class="taCardTop"><div class="taCardTitle">Dealer gamma (weekly)</div></div>
          <div class="taCardState">${escapeHtml(wSum.main)}</div>
          <div class="taCardInterp muted">${escapeHtml(wSum.sub)}</div>
        </div>
        <div class="taCard e2Click" data-e2-insight="e2_dealer_gamma" data-e2-title="Dealer Gamma (Nearest)" title="Click for desk insight">
          <div class="taCardTop"><div class="taCardTitle">Dealer gamma (nearest)</div></div>
          <div class="taCardState">${escapeHtml(nSum.main)}</div>
          <div class="taCardInterp muted">${escapeHtml(nSum.sub)}</div>
        </div>
        <div class="taCard e2Click" data-e2-insight="e2_hedging_pressure" data-e2-title="Hedging Pressure (HPI)" title="Click for desk insight">
          <div class="taCardTop"><div class="taCardTitle">Hedging pressure (HPI)</div></div>
          <div class="taCardState mono">${escapeHtml(wHp.main)}</div>
          <div class="taCardInterp muted">${escapeHtml(wHp.sub)}</div>
          <div class="taCardInterp muted">Nearest: ${escapeHtml(nHp.main)} · ${escapeHtml(nHp.sub)}</div>
        </div>
        <div class="taCard e2Click" data-e2-insight="e2_tail_ignition" data-e2-title="Tail Ignition" title="Click for desk insight">
          <div class="taCardTop"><div class="taCardTitle">Tail ignition</div></div>
          <div class="taCardState">${escapeHtml(wTail.main)}</div>
          <div class="taCardInterp muted">${escapeHtml(wTail.sub)}</div>
          <div class="taCardInterp muted">Nearest: ${escapeHtml(nTail.main)} · ${escapeHtml(nTail.sub)}</div>
        </div>
        <div class="taCard e2Click" data-e2-insight="e2_vol_pressure" data-e2-title="Vol Pressure" title="Click for desk insight">
          <div class="taCardTop"><div class="taCardTitle">Vol pressure</div></div>
          <div class="taCardState mono">${escapeHtml(vpState)}${Number.isFinite(vpScore) ? ` · z=${escapeHtml(vpScore.toFixed(2))}` : ""}</div>
          <div class="taCardInterp muted">${escapeHtml(vpSub)}</div>
        </div>
        <div class="taCard e2Click" data-e2-insight="e2_expected_move" data-e2-title="ORATS EM" title="Click for desk insight">
          <div class="taCardTop">
            <div class="taCardTitle">ORATS EM</div>
            <span class="info" title="ORATS expected move from /cores (IV-derived for indices, impErnMv for stocks). EOD shown with delayed overlay when available. Strike targets use delayed first, then EOD.">ⓘ</span>
          </div>
          <div class="taCardState mono">${Number.isFinite(oratsEodEmPct) ? escapeHtml(oratsEodEmPct.toFixed(2)) + "%" : "—"}</div>
          <div class="taCardInterp">${oratsEodAsOf ? `As of: ${escapeHtml(oratsEodAsOf)}` : "—"} · EOD (used for breach history)</div>
          <div style="border-top:1px solid rgba(0,0,0,0.06); margin-top:6px; padding-top:6px;">
            <div class="taCardState mono" style="font-size:0.95em;">${Number.isFinite(delayedEmPct) ? escapeHtml(delayedEmPct.toFixed(2)) + "%" : "—"}</div>
            <div class="taCardInterp">${delayedUpdatedAt ? `Updated: ${escapeHtml(delayedUpdatedAt)}` : delayedTradeDate ? `As of: ${escapeHtml(delayedTradeDate)}` : "—"} · 15-min delayed${Number.isFinite(delayedEmPct) ? " · <strong>Used for strike targets</strong>" : ""}</div>
          </div>
        </div>
        <div class="taCard e2Click" data-e2-insight="e2_expected_move" data-e2-title="Straddle EM" title="Click for desk insight">
          <div class="taCardTop">
            <div class="taCardTitle">Straddle EM</div>
            <span class="info" title="Risk-neutral expected move from weekly Friday ATM-forward straddle. Spot normalization uses the smart live/close pricing mode.">ⓘ</span>
          </div>
          <div class="taCardState mono">${Number.isFinite(emPct) ? escapeHtml(emPct.toFixed(2)) + "%" : "—"}</div>
          <div class="taCardInterp">${Number.isFinite(emDollars) ? `$${escapeHtml(emDollars.toFixed(2))} pts` : "—"} · ${emExpiry ? `Exp: ${escapeHtml(emExpiry)}` : "—"}${Number.isFinite(emDte) ? ` (${emDte}d)` : ""}</div>
          <div class="taCardInterp muted">${emSymbol ? escapeHtml(emSymbol) : ""} · ${escapeHtml(emSourceLabel)} · Weekly Friday only</div>
        </div>
        <div class="taCard e2Click" data-e2-insight="e2_expected_move" data-e2-title="Strike Targets" title="Click for desk insight">
          <div class="taCardTop">
            <div class="taCardTitle">Strike Targets (EM)</div>
            <span class="info" title="Wing strike distances based on ORATS EM (delayed preferred). Values are expressed as a percentage of spot.">ⓘ</span>
          </div>
          <div class="emTargetGrid">
            <div class="emRow emBox--white"><span class="k">1.0× EM</span><span class="v mono">${Number.isFinite(stWhite) ? escapeHtml(stWhite.toFixed(2)) + "%" : "—"}</span></div>
            <div class="emRow emBox--blue"><span class="k">1.5× EM</span><span class="v mono">${Number.isFinite(stBlue) ? escapeHtml(stBlue.toFixed(2)) + "%" : "—"}</span></div>
            <div class="emRow emBox--red"><span class="k">2.0× EM</span><span class="v mono">${Number.isFinite(stRed) ? escapeHtml(stRed.toFixed(2)) + "%" : "—"}</span></div>
          </div>
          <div class="taCardInterp">Wing distance as % of spot${stSourceLabel ? ` (${escapeHtml(stSourceLabel)})` : ""}.</div>
        </div>
      </div>

      <details class="taDetails e2Details" style="margin-top:12px;">
        <summary>This Week Details</summary>
        <div class="finePrint muted" style="margin-top:10px;">
          ${Number.isFinite(Number(hiCount)) ? `High-impact US events: ${escapeHtml(String(hiCount))}. ` : ""}Full macro list + OI clusters (kept out of the scan grid).
        </div>
        <div style="margin-top:10px;">
          ${hiTop.length ? `<ul class="taMiniList">${hiTop.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>` : `<div class="muted">—</div>`}
        </div>
        <div class="taGrid" style="margin-top:10px;">
          <div class="taCard">
            <div class="taCardTop"><div class="taCardTitle">OI clusters (weekly)</div></div>
            <div class="taCardState mono">${escapeHtml(wOi.meta)}</div>
            <div class="taCardInterp muted">${escapeHtml(wOi.put)}</div>
            <div class="taCardInterp muted">${escapeHtml(wOi.call)}</div>
          </div>
          <div class="taCard">
            <div class="taCardTop"><div class="taCardTitle">OI clusters (nearest)</div></div>
            <div class="taCardState mono">${escapeHtml(nOi.meta)}</div>
            <div class="taCardInterp muted">${escapeHtml(nOi.put)}</div>
            <div class="taCardInterp muted">${escapeHtml(nOi.call)}</div>
          </div>
        </div>
      </details>
    </div>
  `;

  const expBtn = $("e2ExportLLM");
  if (expBtn) {
    expBtn.addEventListener("click", async () => {
      try {
        await exportEngine2LLMBundle();
      } catch {
        // ignore
      }
    });
  }

  const oneBtn = $("e2ExportOnePage");
  if (oneBtn) {
    oneBtn.addEventListener("click", async () => {
      try {
        await exportEngine2OnePageOnly();
      } catch {
        // ignore
      }
    });
  }
}

function render(payload) {
  lastPayload = payload;
  if (window.RavenChat) RavenChat.setEngineContext("engine2", payload);
  const status = $("status");
  const results = $("results");
  if (results) results.classList.toggle("hidden", false);
  if (status) status.classList.remove("isError", "isRunning", "isOk");

  // Scan-first decision header (instrument panel style)
  try { renderEngine2DecisionPanel(payload); } catch { /* ignore */ }

  const like = payload?.oddsLikeNow || {};
  try {
    if (typeof window.renderTechnicalsDailyPanel === "function") {
      // Engine 2 uses payload.underlying.symbol as the displayed symbol.
      const sym = payload?.underlying?.symbol || "SPX";
      window.renderTechnicalsDailyPanel(payload, { rootId: "technicalsSection", symbolOverride: sym });
    }
  } catch {
    // ignore TA panel errors to avoid breaking core workflow
  }

  // Dealer Gamma Map (clean hover chart)
  // Fetch after a successful run so the panel stays in sync with the user's session.
  loadGammaMap();

  const oddsMeta = $("oddsMeta");
  if (oddsMeta) {
    const rb = like?.regimeBucket || "—";
    const mb = like?.macroBucket || "—";
    const sb = like?.seasonBucket || "—";
    const n = like?.weeksUsed;
    oddsMeta.textContent = `bucket=${rb}/${mb}/${sb} · lookback=2y · weeksUsed=${Number.isFinite(Number(n)) ? Number(n) : "—"}`;
  }

  const tbody = $("oddsBody");
  if (tbody) {
    const rows = Array.isArray(like?.byWidth) ? like.byWidth : [];
    tbody.innerHTML = rows.map(r => {
      const w = Number(r.w);
      const n = r.n ?? "—";
      const be = r.breachEitherPct;
      const bp = r.breachPutPct;
      const bc = r.breachCallPct;
      const ar = r.avgAbsRetPct;
      return `<tr>
        <td class="mono">${Number.isFinite(w) ? w.toFixed(2) : "—"}</td>
        <td class="num mono">${escapeHtml(String(n))}</td>
        <td class="num mono">${be === null || be === undefined ? "—" : fmtPct(be, 2)}</td>
        <td class="num mono">${bp === null || bp === undefined ? "—" : fmtPct(bp, 2)}</td>
        <td class="num mono">${bc === null || bc === undefined ? "—" : fmtPct(bc, 2)}</td>
        <td class="num mono">${ar === null || ar === undefined ? "—" : fmtPct(ar, 2)}</td>
      </tr>`;
    }).join("");
  }

  // v2: advisor is always-on post-scan, not gated on multi-wing.
  // Width-comparison panel still renders when the engine returns a
  // multi-wing grid, but the button sits independently so the desk
  // can narrate any scan.
  try {
    var advisorSec = $("e2AdvisorSection");
    var advisorEl = $("e2AdvisorContent");
    if (advisorSec && advisorEl) {
      advisorSec.classList.remove("hidden");
      advisorEl.innerHTML =
        '<div class="taPanel"><div class="taHeader"><div class="taHeaderRow">' +
        '<span class="taHeaderTitle">AI Trade Advisor</span>' +
        '<span class="taHeaderActions"><button id="e2RunAdvisorBtn" class="primaryButton" type="button" style="font-size:11px;padding:6px 16px">Run Advisor</button></span>' +
        '</div><div class="taMuted" style="font-size:12px;margin-top:4px">' +
        'On-demand narrative layer over the tracked/live workflow + scan. One click = one LLM call.' +
        '</div></div></div>';
      var _wc = Array.isArray(payload?.widthComparison) ? payload.widthComparison : [];
      if (_wc.length > 0 && typeof renderWidthComparison === "function") {
        try { renderWidthComparison(_wc); } catch (_e) { /* ignore */ }
      }
      var advBtn = $("e2RunAdvisorBtn");
      if (advBtn) advBtn.addEventListener("click", function () { _runAdvisor(); });
    }
  } catch (_e) { console.warn("e2 advisor panel:", _e); }

  // v2: Track Builder + source chip + orphan drilldowns.
  try { renderE2CommandDeck(payload); } catch (err) { console.warn("e2 command deck:", err); }

  // Load active trades
  _loadActiveTrades();

  if (status) {
    status.textContent = "";
    status.classList.remove("isRunning", "isError");
    status.classList.add("hidden");
  }
}

// ==========================================================================
// v2 Command Deck — Wing Console, MI v2 regime, source chip, orphan dividers
// ==========================================================================

const _E2CommandDeckState = {
  underlying: "SPX",
  entry_day: "mon",
  as_of_date: "",
  lastDeck: null,
  selectedIndex: 0,
};

const _E2_SOURCE_LABELS = {
  desk_default:  "Desk default",
  user_override: "Override",
  unknown:       "",
};

function _setE2SourceChip(source) {
  const chip = document.getElementById("e2EventSourceChip");
  if (!chip) return;
  const s = String(source || "unknown").toLowerCase();
  chip.className = `e1SourceChip e1SourceChip--${s === "user_override" ? "user_override" : (s === "desk_default" ? "orats_cores" : "unknown")}`;
  const label = _E2_SOURCE_LABELS[s] ?? "";
  chip.textContent = label;
  chip.title = label ? `Entry-day source: ${label.toLowerCase()}` : "";
}

function _scoreColorE2(score) {
  if (score >= 75) return "e2MetricGood";
  if (score >= 55) return "e2MetricMed";
  return "e2MetricRisky";
}

function _fmtPctE2(x, digits = 1) {
  if (x === null || x === undefined) return "—";
  const n = Number(x);
  if (!Number.isFinite(n)) return "—";
  return `${n.toFixed(digits)}%`;
}

function renderE2CommandDeck(payload) {
  try { _renderTrackBuilder(payload); } catch (_e) { /* ignore */ }
  try { _renderMacroProximity(payload); } catch (_e) { /* ignore */ }
}

function _roundToStep(x, step) {
  const n = Number(x);
  const s = Number(step) || 1;
  if (!Number.isFinite(n)) return null;
  return Math.round(n / s) * s;
}

function _seedTrackedShortStrikes(spot, emPct, emMult) {
  const spotN = Number(spot);
  const emN = Number(emPct);
  const emm = Number(emMult);
  if (!Number.isFinite(spotN) || !Number.isFinite(emN) || !Number.isFinite(emm) || spotN <= 0 || emN <= 0 || emm <= 0) {
    return { shortPut: null, shortCall: null };
  }
  const distPts = spotN * (emN / 100.0) * emm;
  return {
    shortPut: _roundToStep(spotN - distPts, 5),
    shortCall: _roundToStep(spotN + distPts, 5),
  };
}

function _renderTrackBuilder(payload) {
  const body = document.getElementById("e2TrackBuilderContent");
  const sec = document.getElementById("e2TrackBuilderSection");
  if (!body || !sec) return;
  const em = (payload && payload.expectedMove) || {};
  const current = (payload && payload.current) || {};
  const regime = current.regime || {};
  const macro = current.macro || {};
  const hb = payload && payload.historyBreakerRisk;
  const spot = Number(current.stockPrice || em.smartSpotPrice || em.spotPrice || 0);
  const emPct = Number(em.expectedMovePct || em.oratsExpectedMovePct || 0);
  const seeded = _seedTrackedShortStrikes(spot, emPct, _e2TrackBuilderState.defaultEmMultiple);
  sec.classList.remove("hidden");
  body.innerHTML = (
    `<div class="taPanel" style="padding:14px 16px">` +
      `<div style="font-size:12px;color:var(--text-secondary);margin-bottom:10px">` +
      `Build tracked candidates (not live). Spot <strong>${escapeHtml(String(spot || "—"))}</strong> · 1σ EM <strong>${_fmtPctE2(emPct, 2)}</strong> · Expiry <strong>${escapeHtml(String(em.expiry || "—"))}</strong> · Regime <strong>${escapeHtml(String(regime.label || regime.bucket || "—"))}</strong>` +
      `</div>` +
      `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px">` +
        `<div><label style="font-size:11px;color:var(--text-secondary);display:block;margin-bottom:4px">EM multiple</label><input id="e2TrackEm" type="number" step="0.05" min="0.5" max="4.0" value="${_e2TrackBuilderState.defaultEmMultiple.toFixed(2)}" style="width:100%;padding:7px 8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-primary);color:var(--text-primary);font-size:12px"/></div>` +
        `<div><label style="font-size:11px;color:var(--text-secondary);display:block;margin-bottom:4px">Wing width (pts)</label><input id="e2TrackWing" type="number" step="1" min="1" max="100" value="${_e2TrackBuilderState.defaultWingWidth}" style="width:100%;padding:7px 8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-primary);color:var(--text-primary);font-size:12px"/></div>` +
        `<div><label style="font-size:11px;color:var(--text-secondary);display:block;margin-bottom:4px">Short put</label><input id="e2TrackShortPut" type="number" step="1" value="${seeded.shortPut != null ? String(seeded.shortPut) : ""}" style="width:100%;padding:7px 8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-primary);color:var(--text-primary);font-size:12px"/></div>` +
        `<div><label style="font-size:11px;color:var(--text-secondary);display:block;margin-bottom:4px">Short call</label><input id="e2TrackShortCall" type="number" step="1" value="${seeded.shortCall != null ? String(seeded.shortCall) : ""}" style="width:100%;padding:7px 8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-primary);color:var(--text-primary);font-size:12px"/></div>` +
        `<div><label style="font-size:11px;color:var(--text-secondary);display:block;margin-bottom:4px">Est. credit ($)</label><input id="e2TrackCredit" type="number" step="0.01" min="0" value="2.00" style="width:100%;padding:7px 8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-primary);color:var(--text-primary);font-size:12px"/></div>` +
        `<div><label style="font-size:11px;color:var(--text-secondary);display:block;margin-bottom:4px">Entry day</label><input id="e2TrackEntryDay" type="text" value="${escapeHtml(String(document.getElementById("entryDay")?.value || "mon"))}" style="width:100%;padding:7px 8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-primary);color:var(--text-primary);font-size:12px"/></div>` +
      `</div>` +
      `<div style="margin-top:10px"><label style="font-size:11px;color:var(--text-secondary);display:block;margin-bottom:4px">Note (optional)</label><input id="e2TrackNote" type="text" placeholder="Why this candidate is worth tracking..." style="width:100%;padding:7px 8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-primary);color:var(--text-primary);font-size:12px"/></div>` +
      (hb && hb.level && hb.level !== "low"
        ? `<div style="margin-top:10px;padding:8px 10px;border-radius:8px;background:rgba(255,159,10,.10);border:1px solid rgba(255,159,10,.25);font-size:11px;color:#ffb46a">History-breaker ${escapeHtml(String(hb.level).toUpperCase())} (${escapeHtml(String(hb.score || "—"))}) warn-only. Track with tighter management assumptions.</div>`
        : "") +
      `<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">` +
        `<button id="e2TrackSeedBtn" type="button" class="chipToggle" style="font-size:11px;padding:6px 10px">Seed strikes</button>` +
        `<button id="e2TrackSubmitBtn" type="button" class="primaryButton" style="font-size:11px;padding:7px 14px">Track Candidate</button>` +
      `</div>` +
    `</div>`
  );

  const seedBtn = document.getElementById("e2TrackSeedBtn");
  if (seedBtn) {
    seedBtn.addEventListener("click", function () {
      const emMult = Number(document.getElementById("e2TrackEm")?.value || _e2TrackBuilderState.defaultEmMultiple);
      const seededNow = _seedTrackedShortStrikes(spot, emPct, emMult);
      if (seededNow.shortPut != null) document.getElementById("e2TrackShortPut").value = String(seededNow.shortPut);
      if (seededNow.shortCall != null) document.getElementById("e2TrackShortCall").value = String(seededNow.shortCall);
    });
  }

  const submitBtn = document.getElementById("e2TrackSubmitBtn");
  if (submitBtn) {
    submitBtn.addEventListener("click", async function () {
      submitBtn.disabled = true;
      submitBtn.textContent = "Tracking...";
      try {
        const emMult = Number(document.getElementById("e2TrackEm")?.value || _e2TrackBuilderState.defaultEmMultiple);
        const wing = Number(document.getElementById("e2TrackWing")?.value || _e2TrackBuilderState.defaultWingWidth);
        const shortPut = Number(document.getElementById("e2TrackShortPut")?.value || 0);
        const shortCall = Number(document.getElementById("e2TrackShortCall")?.value || 0);
        const credit = Number(document.getElementById("e2TrackCredit")?.value || 0);
        const entryDay = String(document.getElementById("e2TrackEntryDay")?.value || "mon").trim().toLowerCase();
        const note = String(document.getElementById("e2TrackNote")?.value || "").trim();
        if (!Number.isFinite(shortPut) || !Number.isFinite(shortCall) || shortPut <= 0 || shortCall <= 0 || shortPut >= shortCall) {
          throw new Error("Enter valid short put/call strikes.");
        }
        if (!Number.isFinite(wing) || wing <= 0) throw new Error("Wing width must be > 0.");
        _e2TrackBuilderState.defaultEmMultiple = Number.isFinite(emMult) && emMult > 0 ? emMult : _e2TrackBuilderState.defaultEmMultiple;
        _e2TrackBuilderState.defaultWingWidth = wing;
        const bodyReq = {
          source: "track_builder",
          mode: "tracked",
          entry: {
            underlying: String((payload?.underlying || {}).symbol || "SPX"),
            spotAtEntry: Number(spot || 0),
            entryDate: String(payload?.asOfDate || new Date().toISOString().slice(0, 10)).slice(0, 10),
            expiryDate: String(em.expiry || ""),
            shortPutStrike: shortPut,
            longPutStrike: shortPut - wing,
            shortCallStrike: shortCall,
            longCallStrike: shortCall + wing,
            wingWidth: wing,
            emMultiple: emMult,
            entryCredit: credit,
          },
          entryContext: {
            regimeScore: regime.score,
            regimeBucket: regime.bucket || regime.label,
            macroBucket: macro.bucket,
            emPct: emPct,
            volPressureState: payload?.liveContext?.volPressure?.state,
            historyBreakerRisk: payload?.historyBreakerRisk || null,
          },
          note: note || "Tracked from SPX Track Builder.",
          entryDay: entryDay || "mon",
        };
        const resp = await fetch("/api/spx-ic/trade", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(bodyReq),
        });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        submitBtn.textContent = "Tracked ✓";
        submitBtn.style.background = "#34c759";
        await _loadActiveTrades();
        const at = document.getElementById("e2ActiveTrades");
        if (at) {
          try { at.scrollIntoView({ behavior: "smooth", block: "start" }); }
          catch (_e) { at.scrollIntoView(); }
        }
      } catch (e) {
        alert("Track candidate failed: " + String(e?.message || e));
        submitBtn.disabled = false;
        submitBtn.textContent = "Track Candidate";
      }
    });
  }
}

function _renderMacroProximity(payload) {
  const body = document.getElementById("e2MacroProximityBody");
  const sec  = document.getElementById("e2MacroProximitySection");
  if (!body || !sec) return;
  const macro = (payload && payload.current && payload.current.macro) || (payload && payload.macro) || null;
  if (!macro) { sec.classList.add("hidden"); return; }
  sec.classList.remove("hidden");
  body.innerHTML = (
    `<div class="taPanel" style="padding:12px 16px"><div class="taMuted" style="font-size:12px">` +
    `Macro multiplier: <strong>${escapeHtml(String(macro.multiplier || "—"))}</strong> · bucket: <strong>${escapeHtml(String(macro.bucket || "—"))}</strong>` +
    `</div></div>`
  );
}

async function _fetchAndRenderWingConsole(payload) {
  const section = document.getElementById("e2WingConsoleSection");
  const host = document.getElementById("e2WingConsole");
  if (!section || !host) return;
  section.classList.remove("hidden");
  host.innerHTML = `<div class="e2ConsoleWarnings">Scoring wing placements…</div>`;

  const under = (document.querySelector(".underlyingToggle .isActive")?.id || "").replace("e2Underlying", "") || "SPX";
  const entryDay = document.getElementById("entryDay")?.value || "mon";
  const seasonality = document.getElementById("seasonalityMode")?.value || "none";

  _E2CommandDeckState.underlying = String(under).toUpperCase();
  _E2CommandDeckState.entry_day = String(entryDay).toLowerCase();
  _E2CommandDeckState.as_of_date = (payload && payload.asOfDate) || "";
  // Stash the Friday weekly expiry from the scan's expectedMove so the
  // "Simulate in E14" handoff can pass unambiguous dates.
  _E2CommandDeckState.expiry_date = ((payload || {}).expectedMove || {}).expiry || "";
  _E2CommandDeckState.lastPayload = payload;

  try {
    const resp = await fetch("/api/spx-ic/wing-console", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        underlying: _E2CommandDeckState.underlying,
        entry_day: _E2CommandDeckState.entry_day,
        seasonality_mode: seasonality,
      }),
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`wing-console ${resp.status}: ${text}`);
    }
    const deck = await resp.json();
    _E2CommandDeckState.lastDeck = deck;
    _paintE2WingConsole(host, deck);
    _renderMaeDistribution(deck);
    _renderMcReading(deck);
  } catch (err) {
    host.innerHTML = `<div class="e2ConsoleWarnings">Wing Console unavailable: ${escapeHtml(String(err?.message || err))}</div>`;
  }
}

function _paintE2WingConsole(host, deck) {
  const wc = deck && deck.wingConsole;
  if (!wc) { host.innerHTML = `<div class="e2ConsoleWarnings">No wing console payload.</div>`; return; }
  const placements = Array.isArray(wc.placements) ? wc.placements : [];
  if (!placements.length) {
    host.innerHTML = `<div class="e2ConsoleWarnings">${escapeHtml((wc.warnings || ["No placements returned."]).join(" · "))}</div>`;
    return;
  }
  const top = placements.slice(0, 8);
  const subtitle = [
    `Ticker <strong>${escapeHtml(String(wc.underlying))}</strong>`,
    `Entry <strong>${escapeHtml(String(wc.entry_day))}</strong>`,
    `Spot <strong>${wc.spot != null ? Number(wc.spot).toFixed(2) : "—"}</strong>`,
    `1σ EM <strong>${_fmtPctE2(wc.em_pct, 2)}</strong>`,
    `Regime <strong>${escapeHtml(String(wc.regime_label || "—"))}</strong>${wc.regime_mi_v2 && wc.regime_mi_v2.label ? ` · HMM <strong>${escapeHtml(String(wc.regime_mi_v2.label))}</strong>` : ""}`,
    `MC <strong>${(deck.mcResults && deck.mcResults.n_sims) || 0}</strong> sims${deck.mcResults && deck.mcResults.conditioning_used ? ` · ${escapeHtml(String(deck.mcResults.conditioning_used))}` : ""}`,
  ].map((s) => `<span>${s}</span>`).join("");
  const warningsHtml = Array.isArray(wc.warnings) && wc.warnings.length
    ? `<div class="e2ConsoleWarnings">${wc.warnings.map(escapeHtml).join(" · ")}</div>` : "";

  const rows = top.map((p, i) => {
    const scoreClass = _scoreColorE2(Number(p.composite_score || 0));
    const maeClass  = (p.mae_p95_vs_wing || 0) >= 0.9 ? "e2MetricRisky" : ((p.mae_p95_vs_wing || 0) >= 0.5 ? "e2MetricMed" : "e2MetricGood");
    const closeClass = (p.breach_close_prob || 0) >= 0.2 ? "e2MetricRisky" : ((p.breach_close_prob || 0) >= 0.1 ? "e2MetricMed" : "e2MetricGood");
    const touchClass = (p.touch_intraweek_prob || 0) >= 0.3 ? "e2MetricRisky" : ((p.touch_intraweek_prob || 0) >= 0.15 ? "e2MetricMed" : "e2MetricGood");
    return `
      <tr class="e2PlacementRow ${i === 0 ? "e2PlacementRow--top" : ""}" data-index="${i}">
        <td class="e2RankCell">${i + 1}${i === 0 ? '<span class="e2StarTop">★</span>' : ""}</td>
        <td>${Number(p.em_mult).toFixed(2)}</td>
        <td>${Number(p.wing_pts).toFixed(0)}</td>
        <td>${Number(p.short_put_strike).toFixed(0)} / ${Number(p.short_call_strike).toFixed(0)}</td>
        <td>$${Number(p.credit_dollars).toFixed(0)}</td>
        <td class="${closeClass}">${_fmtPctE2((p.breach_close_prob || 0) * 100)}</td>
        <td class="${touchClass}">${_fmtPctE2((p.touch_intraweek_prob || 0) * 100)}</td>
        <td class="${maeClass}">${_fmtPctE2((p.mae_p95_vs_wing || 0) * 100)}</td>
        <td>${_fmtPctE2(p.theta_capture_pct)}</td>
        <td class="e2ScoreCell ${scoreClass}">${Number(p.composite_score).toFixed(1)}</td>
        <td>${escapeHtml(String(p.confidence || "—"))}</td>
      </tr>`;
  }).join("");

  // Slider defaults from top placement.
  const topP = top[0] || { em_mult: 1.25, wing_pts: 10 };

  host.innerHTML = `
    <div class="e2Console">
      <div class="e2ConsoleHeader">
        <div>
          <h3 class="e2ConsoleTitle">Ranked weekly-IC placements</h3>
          <div class="e2ConsoleSubtitle">${subtitle}</div>
        </div>
      </div>
      ${warningsHtml}
      <table class="e2PlacementTable"><thead><tr>
        <th>#</th><th>EM×</th><th>Wings (pts)</th>
        <th>P short / C short</th>
        <th>Credit ($)</th>
        <th>Brch close</th><th>Touch intraweek</th>
        <th>MAE p95 (% wing)</th>
        <th>Theta cap</th>
        <th>Score</th>
        <th>Conf.</th>
      </tr></thead>
      <tbody id="e2PlacementRows">${rows}</tbody></table>

      <div class="e2Tuner">
        <div class="e2TunerField">
          <label for="e2TunerEm">EM multiple <span id="e2TunerEmValue" class="e2TunerValue">${Number(topP.em_mult).toFixed(2)}</span></label>
          <input id="e2TunerEm" type="range" min="0.75" max="2.5" step="0.05" value="${Number(topP.em_mult).toFixed(2)}" />
        </div>
        <div class="e2TunerField">
          <label for="e2TunerWp">Wing width (pts) <span id="e2TunerWpValue" class="e2TunerValue">${Number(topP.wing_pts).toFixed(0)}</span></label>
          <input id="e2TunerWp" type="range" min="2" max="30" step="1" value="${Number(topP.wing_pts).toFixed(0)}" />
        </div>
        <div class="e2TunerScoreBox">
          <div>Custom placement score: <strong id="e2TunerScore">—</strong></div>
          <div style="font-size:11px;color:var(--muted,#9aa0a6)" id="e2TunerScoreNote">snap to nearest grid</div>
        </div>
      </div>

      <div class="e2ConsoleActions">
        <button type="button" id="e2BuildTradeBtn" class="e2ConsoleActions--primary">Use Selected Placement (Log Trade)</button>
        <button type="button" id="e2SimE14Btn">Preview Replay (in-page)</button>
        <button type="button" id="e2ExportBtn">Export JSON</button>
      </div>
    </div>
  `;

  // Wire per-row click -> update selectedIndex + visual highlight so
  // "Use Selected Placement" knows which placement the desk picked.
  const rowsEl = host.querySelector("#e2PlacementRows");
  if (rowsEl) {
    rowsEl.querySelectorAll(".e2PlacementRow").forEach((row) => {
      row.addEventListener("click", () => {
        const idx = Number(row.getAttribute("data-index") || 0);
        _E2CommandDeckState.selectedIndex = idx;
        rowsEl.querySelectorAll(".e2PlacementRow").forEach((r) => {
          r.classList.remove("e2PlacementRow--selected");
        });
        row.classList.add("e2PlacementRow--selected");
      });
    });
  }

  _wireE2Tuner(deck);
  _wireE2DeckActions(deck);
}

function _wireE2Tuner(deck) {
  const emEl = document.getElementById("e2TunerEm");
  const wpEl = document.getElementById("e2TunerWp");
  const emV = document.getElementById("e2TunerEmValue");
  const wpV = document.getElementById("e2TunerWpValue");
  const scoreEl = document.getElementById("e2TunerScore");
  const noteEl = document.getElementById("e2TunerScoreNote");
  if (!emEl || !wpEl) return;

  const placements = Array.isArray(deck?.wingConsole?.placements) ? deck.wingConsole.placements : [];
  let seq = 0;
  let debounceTimer = null;
  const DEBOUNCE_MS = 220;

  function nearest(em, wp) {
    let best = null, bestDist = Infinity;
    for (const p of placements) {
      const d = Math.pow(p.em_mult - em, 2) + Math.pow((p.wing_pts - wp) / 5, 2);
      if (d < bestDist) { best = p; bestDist = d; }
    }
    return best;
  }

  function paint(p, tag) {
    if (!p || !scoreEl) return;
    scoreEl.textContent = Number(p.composite_score).toFixed(1);
    scoreEl.className = _scoreColorE2(Number(p.composite_score));
    if (noteEl) {
      noteEl.textContent = `${tag} · brch ${_fmtPctE2((p.breach_close_prob || 0) * 100)} · touch ${_fmtPctE2((p.touch_intraweek_prob || 0) * 100)} · credit $${Number(p.credit_dollars || 0).toFixed(0)}`;
    }
  }

  async function fetchExact(em, wp) {
    const mySeq = ++seq;
    try {
      const resp = await fetch("/api/spx-ic/wing-console/score-placement", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          underlying: _E2CommandDeckState.underlying,
          entry_day: _E2CommandDeckState.entry_day,
          as_of_date: _E2CommandDeckState.as_of_date,
          em_mult: em, wing_pts: wp,
        }),
      });
      if (!resp.ok) return;
      const body = await resp.json();
      if (mySeq !== seq) return;
      paint(body.placement, "exact");
    } catch { /* ignore */ }
  }

  function recompute() {
    const em = Number(emEl.value);
    const wp = Number(wpEl.value);
    if (emV) emV.textContent = em.toFixed(2);
    if (wpV) wpV.textContent = wp.toFixed(0);
    const near = nearest(em, wp);
    if (near) paint(near, `grid ~ EM ${Number(near.em_mult).toFixed(2)} / ${Number(near.wing_pts).toFixed(0)}pt`);
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => fetchExact(em, wp), DEBOUNCE_MS);
  }
  emEl.addEventListener("input", recompute);
  wpEl.addEventListener("input", recompute);
  recompute();
}

function _wireE2DeckActions(deck) {
  const exportBtn = document.getElementById("e2ExportBtn");
  if (exportBtn) {
    exportBtn.addEventListener("click", () => {
      const blob = new Blob([JSON.stringify(deck, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `spx-wing-console-${(deck.wingConsole || {}).underlying || "SPX"}-${(deck.wingConsole || {}).as_of_date || "now"}.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
  }
  // Single-surface E2/E14 UX: run replay in-page instead of opening /ic-scenario.
  const simBtn = document.getElementById("e2SimE14Btn");
  if (simBtn) {
    simBtn.addEventListener("click", async () => {
      const idx = _E2CommandDeckState.selectedIndex || 0;
      const wc = (deck && deck.wingConsole) || {};
      const p = (wc.placements || [])[idx];
      if (!p) return;
      const entryDate = _E2CommandDeckState.as_of_date || (wc.as_of_date || "");
      const expiry = _E2CommandDeckState.expiry_date ||
        ((_E2CommandDeckState.lastPayload || {}).expectedMove || {}).expiry || "";
      if (!entryDate || !expiry) {
        alert("Replay preview needs entry + expiry dates from the scan. Click Run first, then try again.");
        return;
      }
      await _showE2ReplayPreview({
        entry_date: String(entryDate).slice(0, 10),
        expiry: String(expiry).slice(0, 10),
        short_put: Number(p.short_put_strike || 0),
        long_put: Number(p.long_put_strike || 0),
        short_call: Number(p.short_call_strike || 0),
        long_call: Number(p.long_call_strike || 0),
        credit_received: Number(((p.credit_dollars || 0) / 100.0).toFixed(2)),
      });
    });
  }

  // v2: Wire "Use Selected Placement" -> Adjust & Log modal.
  const buildBtn = document.getElementById("e2BuildTradeBtn");
  if (buildBtn) {
    buildBtn.addEventListener("click", () => {
      const idx = _E2CommandDeckState.selectedIndex || 0;
      const wc = (deck && deck.wingConsole) || {};
      const p = (wc.placements || [])[idx];
      if (!p) return;
      const withRank = Object.assign({}, p, { _rank: idx });
      try {
        _showE2AdjustModal(deck, withRank);
      } catch (err) {
        console.warn("E2 adjust modal failed", err);
      }
    });
  }
}

async function _showE2ReplayPreview(req) {
  var existing = document.getElementById("e2ReplayPreviewOverlay");
  if (existing) existing.remove();
  var overlay = document.createElement("div");
  overlay.id = "e2ReplayPreviewOverlay";
  overlay.style.cssText = "position:fixed;inset:0;z-index:10002;background:rgba(0,0,0,.58);display:flex;align-items:center;justify-content:center";
  overlay.innerHTML =
    '<div style="width:min(720px,92vw);background:var(--surface,#161a22);border:1px solid var(--border,rgba(255,255,255,.12));border-radius:12px;padding:18px;color:var(--text,#f3f4f6)">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">' +
        '<div style="font-size:15px;font-weight:700">Replay Preview (E14 backend)</div>' +
        '<button id="e2ReplayPreviewClose" type="button" class="chipToggle" style="font-size:11px;padding:3px 10px">Close</button>' +
      '</div>' +
      '<div id="e2ReplayPreviewBody" style="font-size:12px;color:var(--text-secondary,#c7ceda)">Running replay...</div>' +
    "</div>";
  document.body.appendChild(overlay);
  function close() { overlay.remove(); }
  document.getElementById("e2ReplayPreviewClose").addEventListener("click", close);
  overlay.addEventListener("click", function (ev) { if (ev.target === overlay) close(); });
  var bodyEl = document.getElementById("e2ReplayPreviewBody");
  try {
    var resp = await fetch("/api/ic-scenario", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        underlying: "SPX",
        entry_date: req.entry_date,
        expiry: req.expiry,
        short_put: req.short_put,
        long_put: req.long_put,
        short_call: req.short_call,
        long_call: req.long_call,
        credit_received: req.credit_received,
      }),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    var data = await resp.json();
    var tl = Array.isArray(data.mtmTimeline) ? data.mtmTimeline : [];
    var last = tl.length ? tl[tl.length - 1] : {};
    var out = data.outcomeDistribution || {};
    bodyEl.innerHTML =
      '<div style="display:grid;grid-template-columns:repeat(3,minmax(120px,1fr));gap:8px;margin-bottom:10px">' +
        '<div class="e15Card"><div class="e15CardLabel">P10</div><div class="e15CardValue">' + _fmtPctE2(last.p10, 1) + '</div></div>' +
        '<div class="e15Card"><div class="e15CardLabel">P50</div><div class="e15CardValue">' + _fmtPctE2(last.p50, 1) + '</div></div>' +
        '<div class="e15Card"><div class="e15CardLabel">P90</div><div class="e15CardValue">' + _fmtPctE2(last.p90, 1) + '</div></div>' +
      '</div>' +
      '<div style="display:grid;grid-template-columns:repeat(2,minmax(180px,1fr));gap:6px">' +
        '<div>Full collect: <strong>' + _fmtPctE2((out.fullCollect || {}).pct, 1) + '</strong></div>' +
        '<div>Early target: <strong>' + _fmtPctE2((out.earlyTarget || {}).pct, 1) + '</strong></div>' +
        '<div>White-knuckle: <strong>' + _fmtPctE2((out.whiteKnuckle || {}).pct, 1) + '</strong></div>' +
        '<div>Breach: <strong>' + _fmtPctE2((out.breach || {}).pct, 1) + '</strong></div>' +
      '</div>';
  } catch (err) {
    bodyEl.innerHTML = '<span style="color:#ff7a7a">Replay preview failed: ' + escapeHtml(String(err.message || err)) + "</span>";
  }
}

// --- v2: Adjust & Log modal for E2 SPX IC placements ---
// Parallel to E1's _showE1AdjustModalFromPlacement. Takes the deck +
// selected placement, pops an editable form so the desk can tune
// em_mult / wing_pts / strikes / entry_credit / entry_day + notes
// before the trade is persisted via POST /api/spx-ic/trade.
function _showE2AdjustModal(deck, placement) {
  const wc = (deck && deck.wingConsole) || {};
  const underlying = String(wc.underlying || "SPX").toUpperCase();
  const entryDay = String(wc.entry_day || "mon").toLowerCase();

  const defEm = Number(placement.em_mult || 1.25);
  const defWp = Number(placement.wing_pts || 10);
  const defCredit = Number(placement.credit_dollars || 0) / 100.0;
  const defSP = placement.short_put_strike != null ? placement.short_put_strike : "";
  const defSC = placement.short_call_strike != null ? placement.short_call_strike : "";

  const overlay = document.createElement("div");
  overlay.id = "e2AdjustOverlay";
  overlay.style.cssText = "position:fixed;inset:0;z-index:10001;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center";
  overlay.innerHTML = '<div style="background:var(--bg,#121418);color:var(--text,#e6e6e6);border-radius:12px;padding:24px;width:460px;max-width:90vw;max-height:90vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.4)">'
    + '<h3 style="margin:0 0 6px">Adjust &amp; Log Trade — ' + escapeHtml(underlying) + '</h3>'
    + '<div style="font-size:11px;opacity:.6;margin-bottom:14px">'
    + 'Seeded from Wing Console · rank ' + ((placement._rank || 0) + 1)
    + ' · composite ' + Number(placement.composite_score || 0).toFixed(1)
    + ' · ' + escapeHtml(placement.confidence || "—") + ' confidence'
    + '</div>'
    + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:13px">'
    + _e2AdjField("EM Multiple", "e2AdjEm", defEm.toFixed(2), "number", "0.5", "3.0", "0.05")
    + _e2AdjField("Wing Width (pts)", "e2AdjWp", defWp.toFixed(0), "number", "2", "30", "0.5")
    + _e2AdjField("Entry Credit ($)", "e2AdjCredit", defCredit.toFixed(2), "number", "0", "9999", "0.01")
    + _e2AdjField("Short Put Strike", "e2AdjSP", defSP, "number", "", "", "1")
    + _e2AdjField("Short Call Strike", "e2AdjSC", defSC, "number", "", "", "1")
    + _e2AdjField("Entry Day", "e2AdjEntryDay", entryDay, "select", "", "", "", ["mon", "tue", "wed"])
    + '</div>'
    + '<div style="margin-top:12px"><label style="font-size:12px;opacity:.7">Notes (optional)</label>'
    + '<textarea id="e2AdjNotes" rows="2" style="width:100%;font-size:12px;padding:6px;border-radius:6px;border:1px solid var(--border,rgba(255,255,255,.12));background:var(--surface-2,#1a1d24);color:inherit;margin-top:4px" placeholder="Fill price, wing-width rationale, anything worth pinning to the journal…"></textarea></div>'
    + '<div style="display:flex;gap:12px;margin-top:20px;justify-content:flex-end">'
    + '<button id="e2AdjCancelBtn" style="padding:8px 20px;font-size:12px;border-radius:6px;border:1px solid var(--border,rgba(255,255,255,.12));background:none;color:inherit;cursor:pointer">Cancel</button>'
    + '<button id="e2AdjSubmitBtn" class="primaryButton" style="padding:8px 20px;font-size:12px;font-weight:600">Log Trade as Open</button>'
    + '</div></div>';

  document.body.appendChild(overlay);
  document.getElementById("e2AdjCancelBtn").addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) overlay.remove(); });

  document.getElementById("e2AdjSubmitBtn").addEventListener("click", async () => {
    const emVal = parseFloat(document.getElementById("e2AdjEm").value) || defEm;
    const wpVal = parseFloat(document.getElementById("e2AdjWp").value) || defWp;
    const creditVal = parseFloat(document.getElementById("e2AdjCredit").value) || 0;
    const spVal = parseFloat(document.getElementById("e2AdjSP").value) || null;
    const scVal = parseFloat(document.getElementById("e2AdjSC").value) || null;
    const edVal = document.getElementById("e2AdjEntryDay").value || entryDay;
    const notes = (document.getElementById("e2AdjNotes").value || "").trim();

    const body = {
      source: "wing_console",
      mode: "tracked",
      underlying: underlying,
      entryDay: edVal,
      entry: {
        emMultiple: emVal,
        wingWidth: wpVal,
        entryCredit: creditVal,
        shortPutStrike: spVal,
        shortCallStrike: scVal,
        longPutStrike:  spVal ? spVal - wpVal : null,
        longCallStrike: scVal ? scVal + wpVal : null,
        spotAtEntry:    wc.spot,
        emPctAtEntry:   wc.em_pct,
        holdDays:       5,
      },
      entryContext: {
        regimeBucket: wc.regime_bucket,
        regimeLabel:  wc.regime_label,
        miV2Label:    (wc.regime_mi_v2 || {}).label,
        macroBucket:  wc.macro_bucket,
        breachCloseProb:     placement.breach_close_prob,
        touchIntraweekProb:  placement.touch_intraweek_prob,
        maeP95VsWing:        placement.mae_p95_vs_wing,
        historyBreakerRisk:  deck && deck.historyBreakerRisk ? deck.historyBreakerRisk : null,
      },
      wingConsole: {
        rank:           placement._rank,
        compositeScore: placement.composite_score,
        confidence:     placement.confidence,
        nMcSims:        placement.n_mc_sims,
      },
      note: notes || "Logged from E2 Wing Console.",
    };

    try {
      const resp = await fetch("/api/spx-ic/trade", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const txt = await resp.text();
        throw new Error("HTTP " + resp.status + ": " + txt.slice(0, 200));
      }
      const result = await resp.json();
      overlay.remove();
      alert("Trade logged as open: " + underlying + " · " + emVal + "× EM · " + wpVal + "pt wings\nTrade ID: " + (result.tradeId || result.trade_id || "ok"));
      if (typeof _loadActiveTrades === "function") {
        // Reload, then scroll the Active Trades section into view.
        // The section lives above the Wing Console, so without an
        // explicit scroll the desk sits parked at the console and
        // thinks the trade vanished.
        await _loadActiveTrades();
        const target = document.getElementById("e2ActiveTrades");
        if (target) {
          try { target.scrollIntoView({ behavior: "smooth", block: "start" }); }
          catch (_e) { target.scrollIntoView(); }
        }
      }
    } catch (e) {
      alert("Failed to log trade: " + e.message);
    }
  });
}

function _e2AdjField(label, id, defVal, type, min, max, step, options) {
  let html = '<div><label for="' + id + '" style="font-size:11px;opacity:.7;display:block;margin-bottom:3px">' + label + '</label>';
  if (type === "select" && options) {
    html += '<select id="' + id + '" style="width:100%;padding:6px;font-size:12px;border-radius:6px;border:1px solid var(--border,rgba(255,255,255,.12));background:var(--surface-2,#1a1d24);color:inherit">';
    for (const opt of options) {
      const sel = opt === defVal ? " selected" : "";
      html += '<option value="' + opt + '"' + sel + '>' + opt + '</option>';
    }
    html += '</select>';
  } else {
    html += '<input id="' + id + '" type="' + (type || "text") + '" value="' + (defVal != null ? String(defVal) : "") + '"'
      + (min ? ' min="' + min + '"' : '') + (max ? ' max="' + max + '"' : '') + (step ? ' step="' + step + '"' : '')
      + ' style="width:100%;padding:6px;font-size:12px;border-radius:6px;border:1px solid var(--border,rgba(255,255,255,.12));background:var(--surface-2,#1a1d24);color:inherit">';
  }
  html += '</div>';
  return html;
}

function _renderMaeDistribution(deck) {
  const body = document.getElementById("e2MaeDistributionBody");
  const sec  = document.getElementById("e2MaeDistributionSection");
  if (!body || !sec) return;
  const m = deck && deck.maeDistribution;
  if (!m || !m.n) { sec.classList.add("hidden"); return; }
  sec.classList.remove("hidden");
  body.innerHTML = (
    `<div class="taPanel" style="padding:12px 16px">` +
      `<div class="taMuted" style="font-size:12px;margin-bottom:8px">` +
      `n=<strong>${m.n}</strong> weeks · source: <strong>${escapeHtml(String(m.source || "—"))}</strong>` +
      `</div>` +
      `<div class="e15Grid" style="display:grid;grid-template-columns:repeat(auto-fit, minmax(120px, 1fr));gap:8px">` +
        `<div class="e15Card"><div class="e15CardLabel">p50</div><div class="e15CardValue">${_fmtPctE2(m.p50, 2)}</div></div>` +
        `<div class="e15Card"><div class="e15CardLabel">p75</div><div class="e15CardValue">${_fmtPctE2(m.p75, 2)}</div></div>` +
        `<div class="e15Card"><div class="e15CardLabel">p90</div><div class="e15CardValue">${_fmtPctE2(m.p90, 2)}</div></div>` +
        `<div class="e15Card"><div class="e15CardLabel">p95</div><div class="e15CardValue">${_fmtPctE2(m.p95, 2)}</div></div>` +
        `<div class="e15Card"><div class="e15CardLabel">max</div><div class="e15CardValue">${_fmtPctE2(m.max, 2)}</div></div>` +
      `</div>` +
    `</div>`
  );
}

function _renderMcReading(deck) {
  const body = document.getElementById("e2McReadingBody");
  const sec  = document.getElementById("e2McReadingSection");
  if (!body || !sec) return;
  const mc = deck && deck.mcResults;
  if (!mc || !mc.n_sims) { sec.classList.add("hidden"); return; }
  sec.classList.remove("hidden");
  const hb = deck && (deck.historyBreakerRisk || (deck.scan || {}).historyBreakerRisk);
  const hbNote = (hb && String(hb.gate || "").toUpperCase() === "NO_TRADE")
    ? '<div style="margin-top:6px;color:#ffb388;font-size:11px">Warn-only guardrail is elevated (NO_TRADE). Conservative defaults are intentional, not missing data.</div>'
    : "";
  body.innerHTML = (
    `<div class="taPanel" style="padding:12px 16px">` +
      `<div class="taMuted" style="font-size:12px;margin-bottom:6px">` +
      `n_sims: <strong>${mc.n_sims}</strong> · mode: <strong>${escapeHtml(String(mc.mode))}</strong> · ` +
      `conditioning: <strong>${escapeHtml(String(mc.conditioning_used))}</strong> · ` +
      `pool used/total: <strong>${mc.pool_size_used}/${mc.pool_size_total}</strong>` +
      `</div>` +
      (Array.isArray(mc.notes) && mc.notes.length ? `<div class="taMuted" style="font-size:11px">${mc.notes.map(escapeHtml).join(" · ")}</div>` : "") +
      hbNote +
    `</div>`
  );
}

async function run() {
  const status = $("status");
  const entryDay = $("entryDay")?.value || "mon";
  const seasonalityMode = $("seasonalityMode")?.value || "none";
  // v2: defaults preserved; year + widths flow through to the engine now
  // (the engine layer respects them when provided).
  const years = "2";
  const widths = "1.0,1.25,1.5,2.0";
  const weeksLimit = "0";

  // Entry-day source chip: "desk_default" when using a documented entry
  // day (Mon/Tue/Wed); "user_override" when an env override has shifted
  // the allowed set. The chip is a visual parity cue for the desk —
  // today the engine always gates on ENGINE2_ENTRY_DAYS so any non-default
  // flips the chip.
  try {
    const cfgAllowed = ["mon", "tue", "wed"];
    const ed = String(entryDay || "").toLowerCase();
    _setE2SourceChip(cfgAllowed.includes(ed) ? "desk_default" : "user_override");
  } catch { _setE2SourceChip("unknown"); }

  const qs = new URLSearchParams({
    underlying: String(engine2UnderlyingState.symbol || "SPX"),
    entry_day: entryDay,
    years: String(years),
    widths: String(widths),
    seasonality_mode: String(seasonalityMode),
    weeks_limit: String(weeksLimit),
  });

  const url = `/api/spx-ic?${qs.toString()}`;
  
  // PERFORMANCE: Show cached data immediately if available
  const cached = _getCached(url);
  if (cached?.isStale) {
    render(cached.data);
    if (status) {
      status.textContent = "Refreshing…";
      status.classList.remove("isError", "isOk");
      status.classList.add("isRunning");
      status.classList.remove("hidden");
    }
  }

  try {
    setLoading(true, "Analyzing Iron Condor setups...");
    if (!cached?.isStale && status) {
      status.textContent = "Running…";
      status.classList.remove("isError", "isOk");
      status.classList.add("isRunning");
      status.classList.remove("hidden");
    }
    
    // Progress updates
    if (window.RavenLoading) {
      window.RavenLoading.setProgress(15, "Fetching market data...");
    }
    
    const payload = await fetchJson(url);
    
    if (window.RavenLoading) {
      window.RavenLoading.setProgress(70, "Processing strikes...");
    }
    
    render(payload);
    
    if (window.RavenLoading) {
      window.RavenLoading.setProgress(95, "Rendering results...");
    }
    
    if (status) {
      status.classList.add("hidden");
    }
  } catch (e) {
    if (!cached?.isStale) {
      if (status) {
        status.textContent = `Error: ${String(e?.message || e)}`;
        status.classList.remove("isRunning", "isOk");
        status.classList.add("isError");
        status.classList.remove("hidden");
      }
      const results = $("results");
      if (results) results.classList.toggle("hidden", true);
    } else {
      // Had cached data, just note the refresh failed
      if (status) {
        status.textContent = "Refresh failed (showing cached data)";
        status.classList.remove("isRunning", "isError");
        status.classList.add("isOk");
      }
    }
  } finally {
    setLoading(false);
  }
}

async function main() {
  const status = $("status");
  const flags = await checkFlags();
  window.__FLAGS = flags || {};
  if (!flags?.ENABLE_ENGINE2_SPX_IC) {
    if (status) {
      status.textContent = "Engine 2 disabled. Set ENABLE_ENGINE2_SPX_IC=1 and restart the server.";
      status.classList.remove("isRunning", "isOk");
      status.classList.add("isError");
    }
    setLoading(true);
    return;
  }

  const form = $("spxForm");
  if (form) {
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      run();
    });
  }

  initUnderlyingUI();
  initTooltips();
  try { window.RavenUI?.initInfoTips?.(); } catch { /* ignore */ }
  initGammaMapUI();
  initGexHeatmapUI();

  // Do NOT auto-run: user must review selections and click Run.
  const results = $("results");
  if (results) results.classList.toggle("hidden", true);
  if (status) {
    status.textContent = "Select parameters, then click Run.";
    status.classList.remove("isError", "isRunning", "isOk");
  }
}

// ---------------------------------------------------------------------------
// AI Trade Advisor — inline panel
// ---------------------------------------------------------------------------

var _advisorLoading = false;
var _lastAdvisorResult = null;
var _lastEmInsight = null;

function _verdictColor(v) {
  if (v === "TRADE") return "#34c759";
  if (v === "LEAN_PASS") return "#ff9f0a";
  return "#ff453a";
}

function _statusBadge(s) {
  var colors = { on_track: "#34c759", caution: "#ff9f0a", adjust: "#ff453a", exit: "#ff2d55" };
  var labels = { on_track: "On Track", caution: "Caution", adjust: "Adjust", exit: "Exit" };
  var c = colors[s] || "#8e8e93";
  var l = labels[s] || s;
  return '<span style="display:inline-block;padding:2px 10px;border-radius:6px;font-size:11px;font-weight:700;color:#fff;background:' + c + '">' + escapeHtml(l) + '</span>';
}

// Engine-14 reconcile chip — shown next to a trade when the entry-time
// reconciliation snapshot is available. Gives the desk an at-a-glance read
// on whether the trade was opened with a clean E2↔E14↔LLM↔live alignment,
// with the top finding surfaced as a tooltip.
function _reconcileBadge(reconcile) {
  if (!reconcile || !reconcile.overall) return "";
  var ov = reconcile.overall || {};
  var status = String(ov.status || "na").toLowerCase();
  var colors = { agree: "#34c759", drift: "#ff9f0a", mismatch: "#ff453a", na: "#8e8e93" };
  var labels = { agree: "Recon ✓", drift: "Recon drift", mismatch: "Recon mismatch", na: "Recon n/a" };
  var color = colors[status] || colors.na;
  var label = labels[status] || ("Recon " + status);
  var counts = ov.counts || {};
  var countSummary = [];
  ["agree", "drift", "mismatch", "na"].forEach(function (k) {
    var n = Number(counts[k]);
    if (n > 0) countSummary.push(n + " " + k);
  });
  var firstFinding = (ov.topFindings && ov.topFindings[0]) || "";
  var tipParts = [label, countSummary.join(" · ")];
  if (firstFinding) tipParts.push(firstFinding);
  var tip = tipParts.filter(Boolean).join(" — ");
  return '<span title="' + escapeHtml(tip) + '" ' +
    'style="display:inline-block;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:700;' +
    'color:#fff;background:' + color + ';letter-spacing:0.2px;">' + escapeHtml(label) + '</span>';
}

function renderAdvisorPanel(advisorResult) {
  var el = $("e2AdvisorContent");
  var sec = $("e2AdvisorSection");
  if (!el || !sec) return;

  var a = advisorResult?.advisor;
  if (!a || a._source === "fallback") {
    var reason = a?._fallback_reason || "Advisor unavailable";
    el.innerHTML = '<div class="taPanel"><div class="taHeader"><div class="taHeaderRow"><span class="taHeaderTitle">AI Trade Advisor</span></div></div>' +
      '<div style="padding:16px;color:var(--text-secondary);font-size:13px;">' + escapeHtml(reason) + '</div></div>';
    sec.classList.remove("hidden");
    return;
  }

  var verdict = a.verdict || "PASS";
  var conf = Number(a.confidence) || 0;
  var ticket = a.tradeTicket || {};
  var vc = _verdictColor(verdict);

  var html = '<div class="taPanel">';
  html += '<div class="taHeader"><div class="taHeaderRow"><span class="taHeaderTitle">AI Trade Advisor</span><span class="taHeaderMeta">Powered by LLM · ' + escapeHtml(a._model || "") + '</span></div></div>';

  // Verdict badge
  html += '<div style="display:flex;align-items:center;gap:16px;padding:16px 20px;border-bottom:1px solid var(--border)">';
  html += '<div style="display:flex;flex-direction:column;align-items:center;min-width:100px">';
  html += '<div style="font-size:28px;font-weight:800;color:' + vc + ';letter-spacing:0.5px">' + escapeHtml(verdict.replace("_", " ")) + '</div>';
  html += '<div style="font-size:11px;color:var(--text-secondary);margin-top:2px">Confidence: ' + conf + '%</div></div>';
  html += '<div style="flex:1;font-size:13px;color:var(--text-primary);line-height:1.5">' + escapeHtml(a.deskNote || "") + '</div>';
  html += '</div>';

  if (ticket.shortPutStrike) {
    html += '<div style="padding:12px 20px;border-bottom:1px solid var(--border)">';
    html += '<div style="font-size:10px;font-weight:700;text-transform:uppercase;color:var(--text-secondary);letter-spacing:0.5px;margin-bottom:8px">Trade Ticket</div>';
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;font-size:12px">';
    html += '<div><span style="color:var(--text-secondary)">Short Put</span><br><span class="mono" style="font-weight:600">' + escapeHtml(String(ticket.shortPutStrike)) + '</span></div>';
    html += '<div><span style="color:var(--text-secondary)">Long Put</span><br><span class="mono" style="font-weight:600">' + escapeHtml(String(ticket.longPutStrike)) + '</span></div>';
    html += '<div><span style="color:var(--text-secondary)">Short Call</span><br><span class="mono" style="font-weight:600">' + escapeHtml(String(ticket.shortCallStrike)) + '</span></div>';
    html += '<div><span style="color:var(--text-secondary)">Long Call</span><br><span class="mono" style="font-weight:600">' + escapeHtml(String(ticket.longCallStrike)) + '</span></div>';
    html += '<div><span style="color:var(--text-secondary)">Wing</span><br><span class="mono" style="font-weight:600">$' + escapeHtml(String(ticket.wingWidth)) + '</span></div>';
    html += '<div><span style="color:var(--text-secondary)">EM Mult</span><br><span class="mono" style="font-weight:600">' + escapeHtml(String(ticket.emMultiple)) + '×</span></div>';
    html += '<div><span style="color:var(--text-secondary)">Est. Credit</span><br><span class="mono" style="font-weight:600">' + escapeHtml(String(ticket.estimatedCredit || "—")) + '</span></div>';
    html += '<div><span style="color:var(--text-secondary)">Max Loss</span><br><span class="mono" style="font-weight:600">' + escapeHtml(String(ticket.maxLoss || "—")) + '</span></div>';
    html += '</div></div>';
  }

  // Sections
  var sections = [
    ["Wing Width Rationale", a.wingWidthRationale],
    ["Risk Context", a.riskContext],
    ["Entry Plan", a.entryPlan],
    ["Management Plan", a.managementPlan],
    ["Exit Rules", a.exitRules],
  ];
  if (a.passReason) sections.unshift(["Pass Reason", a.passReason]);

  html += '<div style="padding:12px 20px">';
  sections.forEach(function (s) {
    if (!s[1]) return;
    html += '<div style="margin-bottom:12px"><div style="font-size:10px;font-weight:700;text-transform:uppercase;color:var(--text-secondary);letter-spacing:0.5px;margin-bottom:4px">' + escapeHtml(s[0]) + '</div>';
    html += '<div style="font-size:12px;line-height:1.5;color:var(--text-primary)">' + escapeHtml(s[1]) + '</div></div>';
  });

  if (Array.isArray(a.keyRisks) && a.keyRisks.length) {
    html += '<div style="margin-bottom:12px"><div style="font-size:10px;font-weight:700;text-transform:uppercase;color:var(--text-secondary);letter-spacing:0.5px;margin-bottom:4px">Key Risks</div>';
    html += '<ul style="margin:0;padding-left:18px;font-size:12px;line-height:1.6;color:var(--text-primary)">';
    a.keyRisks.forEach(function (r) { html += '<li>' + escapeHtml(r) + '</li>'; });
    html += '</ul></div>';
  }
  html += '</div>';

  // Log trade buttons (allow logging for any verdict, including PASS)
  if (verdict === "TRADE" || verdict === "LEAN_PASS" || verdict === "PASS") {
    html += '<div style="padding:12px 20px;border-top:1px solid var(--border);display:flex;align-items:center;justify-content:flex-end;gap:10px">';
    html += '<button id="e2AdjustTradeBtn" class="chipToggle" type="button" style="font-size:12px;padding:8px 16px">Adjust & Log</button>';
    html += '<button id="e2LogTradeBtn" class="primaryButton" type="button" style="font-size:12px;padding:8px 20px">Log This Trade</button>';
    html += '</div>';

    html += '<div id="e2AdjustForm" class="hidden" style="padding:16px 20px;border-top:1px solid var(--border);background:var(--bg-secondary)">';
    html += '<div style="font-size:10px;font-weight:700;text-transform:uppercase;color:var(--text-secondary);letter-spacing:0.5px;margin-bottom:10px">Adjust Trade Before Logging</div>';
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;font-size:12px">';
    var fields = [
      ["adj_shortPut", "Short Put", ticket.shortPutStrike],
      ["adj_longPut", "Long Put", ticket.longPutStrike],
      ["adj_shortCall", "Short Call", ticket.shortCallStrike],
      ["adj_longCall", "Long Call", ticket.longCallStrike],
      ["adj_wingWidth", "Wing Width ($)", ticket.wingWidth],
      ["adj_credit", "Entry Credit ($)", String(ticket.estimatedCredit || "").replace(/[^0-9.]/g, "")],
    ];
    fields.forEach(function (f) {
      html += '<div><label style="color:var(--text-secondary);display:block;margin-bottom:3px">' + f[1] + '</label>';
      html += '<input id="' + f[0] + '" type="number" step="any" value="' + escapeHtml(String(f[2] || "")) + '" style="width:100%;padding:6px 8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-primary);color:var(--text-primary);font-family:var(--mono);font-size:12px" /></div>';
    });
    html += '</div>';
    html += '<div style="margin-top:10px"><label style="color:var(--text-secondary);font-size:12px;display:block;margin-bottom:3px">Adjustment Note (optional)</label>';
    html += '<input id="adj_note" type="text" placeholder="e.g. Better fill at wider put" style="width:100%;padding:6px 8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-primary);color:var(--text-primary);font-size:12px" /></div>';
    html += '<div style="margin-top:12px;text-align:right">';
    html += '<button id="e2AdjustCancelBtn" class="chipToggle" type="button" style="font-size:11px;padding:6px 14px;margin-right:8px">Cancel</button>';
    html += '<button id="e2AdjustSubmitBtn" class="primaryButton" type="button" style="font-size:11px;padding:6px 14px">Log Adjusted Trade</button>';
    html += '</div></div>';
  }

  html += '</div>';
  el.innerHTML = html;
  sec.classList.remove("hidden");

  var logBtn = $("e2LogTradeBtn");
  if (logBtn) {
    logBtn.addEventListener("click", function () {
      _logAdvisorTrade(advisorResult);
    });
  }

  var adjBtn = $("e2AdjustTradeBtn");
  if (adjBtn) {
    adjBtn.addEventListener("click", function () {
      var form = $("e2AdjustForm");
      if (form) form.classList.toggle("hidden");
    });
  }
  var adjCancel = $("e2AdjustCancelBtn");
  if (adjCancel) {
    adjCancel.addEventListener("click", function () {
      var form = $("e2AdjustForm");
      if (form) form.classList.add("hidden");
    });
  }
  var adjSubmit = $("e2AdjustSubmitBtn");
  if (adjSubmit) {
    adjSubmit.addEventListener("click", function () {
      _logAdjustedTrade(advisorResult);
    });
  }
}

function renderWidthComparison(widthComparison) {
  if (!Array.isArray(widthComparison) || !widthComparison.length) return;
  var el = $("e2AdvisorContent");
  if (!el) return;

  var emPref = lastPayload?.emPreference || {};
  var emBreachSummary = lastPayload?.emBreachSummary || {};
  var deskCon = lastPayload?.deskConsensus || {};
  var rawPrefEm = emPref.emPreference || null;
  var consensusFloor = deskCon.suggestedEmFloor || null;
  var effectiveEm = rawPrefEm;
  var floorOverride = false;
  if (consensusFloor != null && rawPrefEm != null && consensusFloor > rawPrefEm) {
    effectiveEm = consensusFloor;
    floorOverride = true;
  }

  var emGroups = {};
  var emOrder = [];
  widthComparison.forEach(function (w) {
    var ek = Number(w.emMult).toFixed(1);
    if (!emGroups[ek]) { emGroups[ek] = []; emOrder.push(ek); }
    emGroups[ek].push(w);
  });

  var _f = function(v, d) { return v !== null && v !== undefined ? Number(v).toFixed(d) + "%" : "—"; };
  var _fv = function(v, d) { return v !== null && v !== undefined ? Number(v).toFixed(d) : "—"; };
  var emLabels = { "1.0": "Aggressive", "1.5": "Standard", "2.0": "Defensive" };
  var effectiveLabel = effectiveEm === 2.0 ? "defensive" : effectiveEm === 1.0 ? "aggressive" : "standard";

  var html = '<div class="taPanel" style="gap:8px;padding:14px">';
  html += '<div class="taHeader" style="padding:12px 14px 10px">';
  html += '<div class="taHeaderRow">';
  html += '<span class="taHeaderTitle">EM × Wing Width Analysis</span>';
  if (emPref.compositeScore != null) {
    html += '<span class="taHeaderMeta">';
    html += 'Preferred: <strong>' + (effectiveEm || "—") + 'x</strong> ' + escapeHtml(effectiveLabel);
    if (floorOverride) {
      html += ' <span style="color:var(--text-secondary);font-size:10px">(from ' + rawPrefEm + 'x · consensus: ' + escapeHtml(deskCon.riskLevel || "") + ')</span>';
    }
    html += ' · Score ' + Number(emPref.compositeScore).toFixed(0);
    if (deskCon.riskLevel) {
      html += ' · Consensus: ' + escapeHtml(deskCon.riskLevel);
    }
    html += '</span>';
  }
  html += '</div></div>';

  emOrder.forEach(function (ek) {
    var rows = emGroups[ek];
    var emVal = parseFloat(ek);
    var breachPct = emBreachSummary[ek];
    var isRec = effectiveEm != null && Math.abs(emVal - effectiveEm) < 0.01;
    var emLabel = emLabels[ek] || "";

    var borderColor = isRec ? 'rgba(52,199,89,0.45)' : 'var(--ta-glass-border)';
    var bgColor = isRec ? 'rgba(52,199,89,0.03)' : 'var(--ta-glass-bg)';
    html += '<div style="border:1px solid ' + borderColor + ';border-radius:var(--ta-radius-lg);background:' + bgColor + ';overflow:hidden">';

    html += '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 14px;border-bottom:1px solid var(--ta-glass-border)">';
    html += '<span style="font-size:13px;font-weight:700">EM ' + ek + 'x</span>';
    if (emLabel) html += '<span style="font-size:11px;font-weight:600;color:var(--ta-muted)">' + emLabel + '</span>';
    html += '<span class="mono" style="font-size:12px;font-weight:600">Breach: ' + (breachPct != null ? Number(breachPct).toFixed(1) + '%' : '—') + '</span>';
    if (isRec) html += '<span style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:rgba(52,199,89,1);background:rgba(52,199,89,0.10);padding:2px 8px;border-radius:6px">Recommended</span>';
    html += '</div>';

    html += '<div style="overflow:auto"><table class="dataTable" style="min-width:720px"><thead><tr>';
    html += '<th>Wing</th><th class="num">Full Loss %</th><th class="num">E[Loss]</th><th class="num">Credit</th><th class="num">Max Loss</th><th class="num">ROC %</th><th class="num">Risk-Adj ROC</th><th class="num">Obs</th><th>Label</th>';
    html += '</tr></thead><tbody>';

    rows.sort(function(a,b){ return (b.riskAdjRocPct || 0) - (a.riskAdjRocPct || 0); });
    var topRaroc = rows.length ? rows[0].riskAdjRocPct : null;

    rows.forEach(function (w) {
      var isTop = isRec && topRaroc != null && w.riskAdjRocPct === topRaroc;
      var rowStyle = isTop ? ' style="background:rgba(52,199,89,0.06);font-weight:600"' : '';
      html += '<tr' + rowStyle + '>';
      html += '<td class="mono">$' + w.wingWidthPts + '</td>';
      html += '<td class="num mono">' + _f(w.fullLossPct != null ? w.fullLossPct : w.outsidePct, 1) + '</td>';
      html += '<td class="num mono">' + (w.expectedLoss != null ? '$' + Number(w.expectedLoss).toLocaleString() : '—') + '</td>';
      html += '<td class="num mono">' + (w.creditProxy != null ? '$' + Number(w.creditProxy).toLocaleString() : '—') + '</td>';
      html += '<td class="num mono">$' + Number(w.maxLoss).toLocaleString() + '</td>';
      html += '<td class="num mono">' + _f(w.rocPct, 1) + '</td>';
      html += '<td class="num mono">' + _f(w.riskAdjRocPct, 1) + '</td>';
      html += '<td class="num mono">' + (w.totalObs || '—') + '</td>';
      html += '<td>' + escapeHtml(w.label || "") + (isTop ? " ★" : "") + '</td>';
      html += '</tr>';
    });
    html += '</tbody></table></div></div>';
  });
  html += '</div>';

  el.insertAdjacentHTML("beforeend", html);
}

function renderConvergence() {
  var existing = $("e2ConvergenceBar");
  if (existing) existing.remove();

  if (!_lastAdvisorResult || !_lastEmInsight) return;

  var advisor = _lastAdvisorResult.advisor || {};
  var tooltip = _lastEmInsight;

  var advisorEm = advisor.tradeTicket?.emMultiple || null;
  var advisorVerdict = advisor.verdict || "";
  var tooltipEm = tooltip.recommended_em != null ? Number(tooltip.recommended_em) : null;
  var tooltipStance = tooltip.risk_stance || "";
  var tooltipConf = tooltip.confidence != null ? Number(tooltip.confidence) : null;
  var advisorConf = advisor.confidence != null ? Number(advisor.confidence) : null;

  if (advisorEm == null && tooltipEm == null) return;

  var emMatch = advisorEm != null && tooltipEm != null && Math.abs(advisorEm - tooltipEm) < 0.01;
  var stanceMap = { "TRADE": "aggressive", "LEAN_PASS": "standard", "PASS": "defensive" };
  var advisorStance = stanceMap[advisorVerdict] || "standard";
  var stanceMatch = tooltipStance === advisorStance;

  var level, color, bgColor, label, detail;
  if (emMatch && stanceMatch) {
    level = "agree";
    color = "rgba(52,199,89,1)";
    bgColor = "rgba(52,199,89,0.06)";
    label = "Systems Agree";
    detail = "Both recommend " + (tooltipEm || "—") + "x EM (" + (tooltipStance || "—") + ")";
  } else if (emMatch) {
    level = "partial";
    color = "rgba(255,159,10,1)";
    bgColor = "rgba(255,159,10,0.06)";
    label = "Partial Agreement";
    detail = "Both pick " + (tooltipEm || "—") + "x EM but differ on stance: Tooltip '" + tooltipStance + "' vs Advisor '" + advisorVerdict + "'";
  } else {
    level = "diverge";
    color = "rgba(255,69,58,1)";
    bgColor = "rgba(255,69,58,0.06)";
    label = "Systems Diverge";
    detail = "Tooltip recommends " + (tooltipEm || "—") + "x (" + (tooltipStance || "—") + "); Advisor chose " + (advisorEm || "—") + "x (" + advisorVerdict + ")";
  }

  var confHtml = "";
  if (tooltipConf != null && advisorConf != null) {
    confHtml = ' <span class="mono" style="font-size:10px;opacity:0.7">Confidence: EM Insight ' + tooltipConf + '% · Advisor ' + advisorConf + '%</span>';
  }

  var html = '<div id="e2ConvergenceBar" style="border:1px solid ' + color + ';border-radius:var(--ta-radius-lg);background:' + bgColor +
    ';padding:10px 14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">' +
    '<span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:' + color + '">' + escapeHtml(label) + '</span>' +
    '<span style="font-size:12px;color:var(--ta-muted)">' + escapeHtml(detail) + '</span>' +
    confHtml + '</div>';

  var el = $("e2AdvisorContent");
  if (el) el.insertAdjacentHTML("afterbegin", html);
}

async function _runAdvisor() {
  if (_advisorLoading) return;
  _advisorLoading = true;
  var el = $("e2AdvisorContent");
  var sec = $("e2AdvisorSection");
  if (el && sec) {
    sec.classList.remove("hidden");
    el.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text-secondary);font-size:13px">' +
      '<span class="btnSpinner" style="display:inline-block;margin-right:8px"></span>Running AI Trade Advisor...</div>';
  }
  try {
    var entryDay = $("entryDay")?.value || "mon";
    var seasonalityMode = $("seasonalityMode")?.value || "none";
    var underlying = String(window.engine2UnderlyingState?.symbol || "SPX");
    var qs = new URLSearchParams({ underlying: underlying, entry_day: entryDay, seasonality_mode: seasonalityMode });
    var fetchOpts = { method: "POST", headers: { "Content-Type": "application/json" } };
    if (lastPayload) {
      fetchOpts.body = JSON.stringify(lastPayload);
    }
    var resp = await fetch("/api/spx-ic/advisor?" + qs.toString(), fetchOpts);
    if (!resp.ok) {
      var detail = "";
      try { var errBody = await resp.json(); detail = errBody.detail || ""; } catch (_) {}
      throw new Error(detail || ("HTTP " + resp.status));
    }
    var data = await resp.json();
    _lastAdvisorResult = data;
    renderAdvisorPanel(data);
    if (data.widthComparison) renderWidthComparison(data.widthComparison);
    renderConvergence();
  } catch (err) {
    if (el) el.innerHTML = '<div style="padding:16px;color:#ff453a;font-size:13px">Advisor error: ' + escapeHtml(String(err.message || err)) + '</div>';
  } finally {
    _advisorLoading = false;
  }
}

async function _logAdvisorTrade(advisorResult) {
  if (!advisorResult?.advisor) return;
  var btn = $("e2LogTradeBtn");
  if (btn) { btn.disabled = true; btn.textContent = "Logging..."; }
  try {
    var ticket = advisorResult.advisor.tradeTicket || {};
    var current = advisorResult.current || {};
    var em = advisorResult.expectedMove || {};
    var body = {
      source: "advisor",
      mode: "live",
      entry: {
        underlying: ticket.underlying || "SPX",
        spotAtEntry: Number(current.regime?.components?.spotPrice) || 0,
        entryDate: advisorResult.asOfDate || new Date().toISOString().slice(0, 10),
        expiryDate: ticket.expiry || null,
        shortPutStrike: ticket.shortPutStrike || null,
        longPutStrike: ticket.longPutStrike || null,
        shortCallStrike: ticket.shortCallStrike || null,
        longCallStrike: ticket.longCallStrike || null,
        wingWidth: ticket.wingWidth || null,
        emMultiple: ticket.emMultiple || null,
        entryCredit: parseFloat(String(ticket.estimatedCredit || "0").replace(/[^0-9.]/g, "")) || 0,
      },
      entryContext: {
        regimeScore: current.regime?.score,
        regimeBucket: current.regime?.bucket,
        macroBucket: current.macro?.bucket,
        emPct: em.oratsExpectedMovePct || em.expectedMovePct,
        volPressureState: lastPayload?.liveContext?.volPressure?.state,
        historyBreakerRisk: advisorResult.historyBreakerRisk || null,
      },
      advisorVerdict: advisorResult.advisor,
    };
    var resp = await fetch("/api/spx-ic/trade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    var data = await resp.json();
    if (btn) { btn.textContent = "Logged ✓"; btn.style.background = "#34c759"; }
    _loadActiveTrades();
  } catch (err) {
    if (btn) { btn.textContent = "Error"; btn.style.background = "#ff453a"; }
  }
}

async function _logAdjustedTrade(advisorResult) {
  if (!advisorResult?.advisor) return;
  var btn = $("e2AdjustSubmitBtn");
  if (btn) { btn.disabled = true; btn.textContent = "Logging..."; }
  try {
    var ticket = advisorResult.advisor.tradeTicket || {};
    var current = advisorResult.current || {};
    var em = advisorResult.expectedMove || {};

    var adjSP = parseFloat($("adj_shortPut")?.value) || ticket.shortPutStrike;
    var adjLP = parseFloat($("adj_longPut")?.value) || ticket.longPutStrike;
    var adjSC = parseFloat($("adj_shortCall")?.value) || ticket.shortCallStrike;
    var adjLC = parseFloat($("adj_longCall")?.value) || ticket.longCallStrike;
    var adjWing = parseFloat($("adj_wingWidth")?.value) || ticket.wingWidth;
    var adjCredit = parseFloat($("adj_credit")?.value) || 0;
    var adjNote = ($("adj_note")?.value || "").trim();

    var body = {
      source: "adjusted",
      mode: "live",
      entry: {
        underlying: ticket.underlying || "SPX",
        spotAtEntry: Number(current.regime?.components?.spotPrice) || 0,
        entryDate: advisorResult.asOfDate || new Date().toISOString().slice(0, 10),
        expiryDate: ticket.expiry,
        shortPutStrike: adjSP,
        longPutStrike: adjLP,
        shortCallStrike: adjSC,
        longCallStrike: adjLC,
        wingWidth: adjWing,
        emMultiple: ticket.emMultiple,
        entryCredit: adjCredit,
      },
      entryContext: {
        regimeScore: current.regime?.score,
        regimeBucket: current.regime?.bucket,
        macroBucket: current.macro?.bucket,
        emPct: em.oratsExpectedMovePct || em.expectedMovePct,
        volPressureState: lastPayload?.liveContext?.volPressure?.state,
        historyBreakerRisk: advisorResult.historyBreakerRisk || null,
      },
      advisorVerdict: advisorResult.advisor,
      originalTicket: {
        shortPutStrike: ticket.shortPutStrike,
        longPutStrike: ticket.longPutStrike,
        shortCallStrike: ticket.shortCallStrike,
        longCallStrike: ticket.longCallStrike,
        wingWidth: ticket.wingWidth,
        estimatedCredit: ticket.estimatedCredit,
      },
      adjustmentNote: adjNote || null,
    };
    var resp = await fetch("/api/spx-ic/trade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    if (btn) { btn.textContent = "Logged ✓"; btn.style.background = "#34c759"; }
    var form = $("e2AdjustForm");
    if (form) form.classList.add("hidden");
    _loadActiveTrades();
  } catch (err) {
    if (btn) { btn.textContent = "Error"; btn.style.background = "#ff453a"; }
  }
}

// ---------------------------------------------------------------------------
// Active Trades Panel
// ---------------------------------------------------------------------------

var _autoCheckinRunning = false;

async function _loadActiveTrades() {
  var el = $("e2ActiveTradesContent");
  var sec = $("e2ActiveTrades");
  if (!el || !sec) return;

  try {
    var resp = await fetch("/api/spx-ic/trades");
    if (!resp.ok) return;
    var data = await resp.json();
    var trades = data.trades || [];
    if (!trades.length) {
      sec.classList.add("hidden");
      return;
    }

    sec.classList.remove("hidden");
    var liveCount = trades.filter(function (t) { return String(t.mode || "live").toLowerCase() === "live"; }).length;
    var trackedCount = trades.length - liveCount;
    var html = '<div class="taPanel">';
    html += '<div class="taHeader"><span class="taHeaderTitle">Active Trades</span><span class="taHeaderMeta">' + liveCount + ' live · ' + trackedCount + ' tracked</span></div>';

    var staleCheckinTradeIds = [];

    trades.forEach(function (t) {
      var entry = t.entry || {};
      var mode = String(t.mode || "live").toLowerCase();
      var tracking = t.tracking || {};
      var status = tracking.deterministicStatus || "on_track";
      var lastCheckin = (t.checkIns || []).slice(-1)[0];

      var checkinAge = null;
      var checkinAgeLabel = "";
      var isStale = false;
      if (lastCheckin && lastCheckin.timestamp) {
        var checkinMs = new Date(lastCheckin.timestamp).getTime();
        var ageMs = Date.now() - checkinMs;
        var ageMins = Math.floor(ageMs / 60000);
        if (ageMins < 60) checkinAgeLabel = ageMins + "m ago";
        else if (ageMins < 1440) checkinAgeLabel = Math.floor(ageMins / 60) + "h ago";
        else checkinAgeLabel = Math.floor(ageMins / 1440) + "d ago";
        isStale = ageMins > 360;
      }

      var statusDiverged = lastCheckin && lastCheckin.status && lastCheckin.status !== status;
      if (statusDiverged || (isStale && (status === "exit" || status === "adjust"))) {
        staleCheckinTradeIds.push(t.tradeId);
      }

      var entryReconcile = (t.entryContext || {}).reconcile;
      html += '<div style="padding:12px 20px;border-bottom:1px solid var(--border)">';
      html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">';
      html += '<div style="font-size:13px;font-weight:600;display:flex;align-items:center;gap:8px">';
      html += escapeHtml(entry.underlying || "SPX") + ' IC · $' + escapeHtml(String(entry.wingWidth || "")) + ' wings';
      html += mode === "tracked"
        ? ' <span style="font-size:9px;padding:1px 6px;border-radius:4px;background:#0a84ff;color:#fff;font-weight:700">TRACKED</span>'
        : ' <span style="font-size:9px;padding:1px 6px;border-radius:4px;background:#34c759;color:#fff;font-weight:700">LIVE</span>';
      if (t.source === "engine14") html += ' <span style="font-size:9px;padding:1px 6px;border-radius:4px;background:#0a84ff;color:#fff;font-weight:600">E14</span>';
      else if (t.source === "adjusted") html += ' <span style="font-size:9px;padding:1px 6px;border-radius:4px;background:#5856d6;color:#fff;font-weight:600">ADJ</span>';
      if (entryReconcile) html += ' ' + _reconcileBadge(entryReconcile);
      html += '</div>';
      html += '<div style="display:flex;align-items:center;gap:8px">' + _statusBadge(status);
      html += '<button class="chipToggle" data-checkin-id="' + escapeHtml(t.tradeId) + '" type="button" style="font-size:10px;padding:3px 10px">Check In</button>';
      html += '<button class="chipToggle" data-live-review-id="' + escapeHtml(t.tradeId) + '" data-live-review-phase="pre_event" type="button" style="font-size:10px;padding:3px 8px">Pre-event</button>';
      html += '<button class="chipToggle" data-live-review-id="' + escapeHtml(t.tradeId) + '" data-live-review-phase="pre_open" type="button" style="font-size:10px;padding:3px 8px">Pre-open</button>';
      html += '<button class="chipToggle" data-live-review-id="' + escapeHtml(t.tradeId) + '" data-live-review-phase="post_open" type="button" style="font-size:10px;padding:3px 8px">Post-open</button>';
      if (mode === "tracked") {
        html += '<button class="chipToggle" data-promote-id="' + escapeHtml(t.tradeId) + '" type="button" style="font-size:10px;padding:3px 10px;background:#34c759;color:#fff;border-color:#34c759">Promote LIVE</button>';
        html += '<button class="chipToggle" data-stop-id="' + escapeHtml(t.tradeId) + '" type="button" style="font-size:10px;padding:3px 10px">Stop Tracking</button>';
      } else {
        html += '<button class="chipToggle" data-close-id="' + escapeHtml(t.tradeId) + '" type="button" style="font-size:10px;padding:3px 10px">Close</button>';
      }
      html += '</div></div>';

      html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:6px;font-size:11px">';
      html += '<div><span style="color:var(--text-secondary)">Short Put</span> <span class="mono">' + escapeHtml(String(entry.shortPutStrike || "—")) + '</span></div>';
      html += '<div><span style="color:var(--text-secondary)">Short Call</span> <span class="mono">' + escapeHtml(String(entry.shortCallStrike || "—")) + '</span></div>';
      html += '<div><span style="color:var(--text-secondary)">Spot</span> <span class="mono">' + (tracking.currentSpot ? Number(tracking.currentSpot).toFixed(2) : "—") + '</span></div>';
      html += '<div><span style="color:var(--text-secondary)">DTE</span> <span class="mono">' + (tracking.dte !== null && tracking.dte !== undefined ? tracking.dte : "—") + '</span></div>';
      html += '<div><span style="color:var(--text-secondary)">Put dist</span> <span class="mono">' + (tracking.distPutPct !== null ? tracking.distPutPct + "%" : "—") + '</span></div>';
      html += '<div><span style="color:var(--text-secondary)">Call dist</span> <span class="mono">' + (tracking.distCallPct !== null ? tracking.distCallPct + "%" : "—") + '</span></div>';
      html += '</div>';

      if (entryReconcile && entryReconcile.overall) {
        var ovStatus = String(entryReconcile.overall.status || "na").toLowerCase();
        var findings = entryReconcile.overall.topFindings || [];
        if ((ovStatus === "drift" || ovStatus === "mismatch") && findings.length) {
          var border = ovStatus === "mismatch" ? "3px solid #ff453a" : "3px solid #ff9f0a";
          html += '<div style="margin-top:8px;padding:8px 10px;background:var(--bg-secondary);border-radius:8px;font-size:11px;border-left:' + border + '">';
          html += '<div style="font-weight:600;margin-bottom:2px;color:var(--text-primary)">Entry reconcile ' + escapeHtml(ovStatus) + '</div>';
          findings.slice(0, 3).forEach(function (f) {
            html += '<div style="color:var(--text-secondary);margin-top:2px">• ' + escapeHtml(String(f)) + '</div>';
          });
          html += '</div>';
        }
      }

      if (lastCheckin) {
        var ciBoxBorder = statusDiverged ? "border-left:3px solid #ff9f0a" : "";
        html += '<div style="margin-top:8px;padding:8px 10px;background:var(--bg-secondary);border-radius:8px;font-size:11px;' + ciBoxBorder + '">';

        var ciHeader = 'Latest check-in: ' + _statusBadge(lastCheckin.status || status);
        if (checkinAgeLabel) {
          var ageColor = isStale ? "#ff9f0a" : "var(--text-secondary)";
          ciHeader += ' <span style="color:' + ageColor + ';font-weight:400;font-size:10px;margin-left:4px">' + escapeHtml(checkinAgeLabel) + '</span>';
        }
        html += '<div style="font-weight:600;margin-bottom:2px">' + ciHeader + '</div>';

        if (statusDiverged) {
          html += '<div style="color:#ff9f0a;font-weight:600;margin-bottom:4px;font-size:10px">';
          html += 'Status changed since last check-in (' + escapeHtml(lastCheckin.status) + ' \u2192 ' + escapeHtml(status) + ') \u2014 refreshing\u2026';
          html += '</div>';
        }

        if (lastCheckin.headline) html += '<div style="color:var(--text-primary)">' + escapeHtml(lastCheckin.headline) + '</div>';
        if (lastCheckin.recommendation) html += '<div style="color:var(--text-secondary);margin-top:2px">' + escapeHtml(lastCheckin.recommendation) + '</div>';
        html += '</div>';
      }

      var hb = (t.entryContext || {}).historyBreakerRisk;
      if (hb && hb.level && hb.level !== "low") {
        var hbColor = hb.level === "high" ? "#ff453a" : "#ff9f0a";
        html += '<div style="margin-top:8px;padding:8px 10px;background:rgba(255,255,255,.03);border-radius:8px;border-left:3px solid ' + hbColor + '">';
        html += '<div style="font-size:11px;font-weight:700;color:' + hbColor + '">History-breaker ' + escapeHtml(String(hb.level || "").toUpperCase()) + ' · score ' + escapeHtml(String(hb.score || "—")) + '</div>';
        if (Array.isArray(hb.drivers) && hb.drivers.length) {
          html += '<div style="font-size:11px;color:var(--text-secondary);margin-top:3px">' + escapeHtml(String(hb.drivers[0])) + '</div>';
        }
        html += '</div>';
      }
      html += '<div id="e2LiveReviewBox-' + escapeHtml(t.tradeId) + '" style="margin-top:8px"></div>';

      html += '</div>';
    });

    html += '</div>';
    el.innerHTML = html;

    el.querySelectorAll("[data-checkin-id]").forEach(function (btn) {
      btn.addEventListener("click", function () { _runCheckin(btn.dataset.checkinId); });
    });
    el.querySelectorAll("[data-close-id]").forEach(function (btn) {
      btn.addEventListener("click", function () { _closeTrade(btn.dataset.closeId); });
    });
    el.querySelectorAll("[data-promote-id]").forEach(function (btn) {
      btn.addEventListener("click", function () { _promoteTrackedTrade(btn.dataset.promoteId); });
    });
    el.querySelectorAll("[data-stop-id]").forEach(function (btn) {
      btn.addEventListener("click", function () { _stopTrackingTrade(btn.dataset.stopId); });
    });
    el.querySelectorAll("[data-live-review-id]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        _runE2LiveReview(btn.dataset.liveReviewId, btn.dataset.liveReviewPhase || "pre_event");
      });
    });

    if (staleCheckinTradeIds.length > 0 && !_autoCheckinRunning) {
      _autoCheckinRunning = true;
      (async function () {
        for (var i = 0; i < staleCheckinTradeIds.length; i++) {
          try {
            var ciResp = await fetch("/api/spx-ic/trade/" + encodeURIComponent(staleCheckinTradeIds[i]) + "/checkin", { method: "POST" });
            if (!ciResp.ok) console.warn("Auto check-in failed for", staleCheckinTradeIds[i]);
          } catch (e) { console.warn("Auto check-in error", e); }
        }
        _autoCheckinRunning = false;
        _loadActiveTrades();
      })();
    }
  } catch {
    // silently fail
  }
}

async function _runCheckin(tradeId) {
  try {
    var resp = await fetch("/api/spx-ic/trade/" + encodeURIComponent(tradeId) + "/checkin", { method: "POST" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    _loadActiveTrades();
  } catch (err) {
    alert("Check-in error: " + err.message);
  }
}

async function _promoteTrackedTrade(tradeId) {
  try {
    var resp = await fetch("/api/spx-ic/trade/" + encodeURIComponent(tradeId) + "/promote", { method: "POST" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    _loadActiveTrades();
  } catch (err) {
    alert("Promote error: " + err.message);
  }
}

async function _stopTrackingTrade(tradeId) {
  try {
    var resp = await fetch("/api/spx-ic/trade/" + encodeURIComponent(tradeId) + "/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "cancelled_tracking", notes: "Stopped tracking candidate." }),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    _loadActiveTrades();
    _loadTradeJournal();
  } catch (err) {
    alert("Stop tracking error: " + err.message);
  }
}

function _fmtPctOrDash(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return Number(v).toFixed(1) + "%";
}

function _paintE2LiveReview(tradeId, review) {
  var host = document.getElementById("e2LiveReviewBox-" + tradeId);
  if (!host) return;
  var proj = review.projection || {};
  var ladder = review.actionLadder || {};
  var llm = review.llm || {};
  var hb = review.historyBreaker || null;
  var hbHtml = "";
  if (hb && hb.level && hb.level !== "low") {
    var c = hb.level === "high" ? "#ff453a" : "#ff9f0a";
    hbHtml = '<div style="margin-top:8px;padding:8px;border-radius:8px;background:rgba(255,255,255,.02);border:1px solid ' + c + ';font-size:11px;color:#f4f6fa">' +
      'History-breaker ' + escapeHtml(String(hb.level || "").toUpperCase()) + ' (' + escapeHtml(String(hb.score || "—")) + ')' +
      "</div>";
  }
  var rows = Array.isArray(ladder.rows) ? ladder.rows : [];
  host.innerHTML =
    '<div style="margin-top:8px;padding:10px;border-radius:10px;background:#171b24;border:1px solid rgba(255,255,255,.1)">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">' +
        '<div style="font-size:12px;font-weight:700;color:#f5f7ff">Live Review · ' + escapeHtml(String(review.phase || "pre_event")) + '</div>' +
        '<div style="font-size:11px;color:#d9e1ef">Pre-verdict: <strong style="color:#fff">' + escapeHtml(String(ladder.preVerdict || "HOLD")) + "</strong></div>" +
      "</div>" +
      '<div style="display:grid;grid-template-columns:repeat(3,minmax(100px,1fr));gap:8px">' +
        '<div><div style="font-size:10px;color:#aeb8ca">P10</div><div style="font-size:14px;font-weight:700;color:#f0f4ff">' + _fmtPctOrDash(proj.p10) + "</div></div>" +
        '<div><div style="font-size:10px;color:#aeb8ca">P50</div><div style="font-size:14px;font-weight:700;color:#f0f4ff">' + _fmtPctOrDash(proj.p50) + "</div></div>" +
        '<div><div style="font-size:10px;color:#aeb8ca">P90</div><div style="font-size:14px;font-weight:700;color:#f0f4ff">' + _fmtPctOrDash(proj.p90) + "</div></div>" +
      "</div>" +
      '<div style="margin-top:8px;font-size:11px;color:#dbe3f2">Full collect ' + _fmtPctOrDash(proj.fullCollectRate) +
      ' · White-knuckle ' + _fmtPctOrDash(proj.whiteKnuckleRate) + ' · Breach ' + _fmtPctOrDash(proj.breachRate) + "</div>" +
      '<div style="margin-top:8px;font-size:11px;color:#e7ecf8;background:rgba(255,255,255,.03);padding:8px;border-radius:8px">' +
      escapeHtml(String(llm.headline || "No narrative returned.")) + "</div>" +
      (rows.length ? ('<div style="margin-top:8px;font-size:11px;color:#dce4f2">' + rows.slice(0, 3).map(function (r) {
        return '<div style="margin-top:2px"><strong style="color:#fff">' + escapeHtml(String(r.action || "HOLD")) + "</strong> " +
          escapeHtml(String(r.probability || "—")) + "% · " + escapeHtml(String(r.note || "")) + "</div>";
      }).join("") + "</div>") : "") +
      hbHtml +
    "</div>";
}

async function _runE2LiveReview(tradeId, phase) {
  var host = document.getElementById("e2LiveReviewBox-" + tradeId);
  if (host) {
    host.innerHTML = '<div style="margin-top:8px;padding:8px;border-radius:8px;background:#1b1f2a;color:#d7dfef;font-size:11px">Running ' + escapeHtml(String(phase || "pre_event")) + ' review…</div>';
  }
  try {
    var resp = await fetch("/api/spx-ic/trade/" + encodeURIComponent(tradeId) + "/live-review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phase: phase || "pre_event" }),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    var data = await resp.json();
    _paintE2LiveReview(tradeId, data.review || {});
  } catch (err) {
    if (host) {
      host.innerHTML = '<div style="margin-top:8px;padding:8px;border-radius:8px;background:#2a1b1b;color:#ffb2b2;font-size:11px">Live review failed: ' + escapeHtml(String(err.message || err)) + "</div>";
    }
  }
}

function _closeTrade(tradeId) {
  var existing = $("e2CloseOverlay");
  if (existing) existing.remove();

  var overlay = document.createElement("div");
  overlay.id = "e2CloseOverlay";
  overlay.style.cssText = "position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center";

  var box = document.createElement("div");
  box.style.cssText = "background:#1c1c1e;border:1px solid rgba(255,255,255,0.12);border-radius:12px;padding:24px;width:380px;max-width:90vw;color:var(--text-primary);font-family:var(--font);box-shadow:0 8px 32px rgba(0,0,0,0.5)";

  box.innerHTML = '<div style="font-size:14px;font-weight:700;margin-bottom:16px">Close Trade</div>' +
    '<div style="display:grid;gap:12px;font-size:12px">' +
    '<div><label style="color:rgba(255,255,255,0.6);display:block;margin-bottom:3px">How did it close?</label>' +
    '<select id="closeType" style="width:100%;padding:6px 8px;border-radius:6px;border:1px solid rgba(255,255,255,0.12);background:#2c2c2e;color:#f5f5f7;font-size:12px">' +
    '<option value="expired_worthless">Expired Worthless (Full Win)</option>' +
    '<option value="closed_early">Closed Early</option>' +
    '<option value="stopped_out">Stopped Out</option>' +
    '<option value="rolled">Rolled / Adjusted Out</option>' +
    '</select></div>' +
    '<div id="closeExitRow"><label style="color:rgba(255,255,255,0.6);display:block;margin-bottom:3px">Exit Debit Paid ($)</label>' +
    '<input id="closeExitCredit" type="number" step="0.01" value="0" style="width:100%;padding:6px 8px;border-radius:6px;border:1px solid rgba(255,255,255,0.12);background:#2c2c2e;color:#f5f5f7;font-family:var(--mono);font-size:12px" />' +
    '<div style="color:rgba(255,255,255,0.4);font-size:10px;margin-top:2px">Cost to close (0 if expired worthless)</div></div>' +
    '<div><label style="color:rgba(255,255,255,0.6);display:block;margin-bottom:3px">Notes (optional)</label>' +
    '<input id="closeNotes" type="text" placeholder="e.g. Closed for .30 debit with 1 DTE" style="width:100%;padding:6px 8px;border-radius:6px;border:1px solid rgba(255,255,255,0.12);background:#2c2c2e;color:#f5f5f7;font-size:12px" /></div>' +
    '</div>' +
    '<div style="margin-top:16px;display:flex;justify-content:flex-end;gap:8px">' +
    '<button id="closeCancelBtn" class="chipToggle" type="button" style="font-size:12px;padding:6px 14px">Cancel</button>' +
    '<button id="closeSubmitBtn" class="primaryButton" type="button" style="font-size:12px;padding:6px 14px">Close Trade</button>' +
    '</div>';

  overlay.appendChild(box);
  document.body.appendChild(overlay);

  overlay.addEventListener("click", function (e) { if (e.target === overlay) overlay.remove(); });
  $("closeCancelBtn").addEventListener("click", function () { overlay.remove(); });

  var typeSelect = $("closeType");
  typeSelect.addEventListener("change", function () {
    var exitRow = $("closeExitRow");
    if (typeSelect.value === "expired_worthless") {
      exitRow.style.opacity = "0.4";
      $("closeExitCredit").value = "0";
    } else {
      exitRow.style.opacity = "1";
    }
  });

  $("closeSubmitBtn").addEventListener("click", async function () {
    var btn = $("closeSubmitBtn");
    btn.disabled = true;
    btn.textContent = "Closing...";
    try {
      var closeType = typeSelect.value;
      var exitCredit = parseFloat($("closeExitCredit").value) || 0;
      var notes = ($("closeNotes").value || "").trim();
      var body = {
        reason: closeType,
        exitCredit: exitCredit,
        expiredWorthless: closeType === "expired_worthless",
        notes: notes || null,
      };
      var resp = await fetch("/api/spx-ic/trade/" + encodeURIComponent(tradeId) + "/close", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      overlay.remove();
      _loadActiveTrades();
      _loadTradeJournal();
    } catch (err) {
      btn.textContent = "Error";
      btn.style.background = "#ff453a";
    }
  });
}

// ---------------------------------------------------------------------------
// Trade Journal — closed trade history + performance stats
// ---------------------------------------------------------------------------

let _e2TradeJournalExpanded = false;

function _outcomeBadge(oc) {
  var colors = { win: "#34c759", loss: "#ff453a", scratch: "#8e8e93" };
  var labels = { win: "Win", loss: "Loss", scratch: "Scratch" };
  var c = colors[oc] || "#8e8e93";
  var l = labels[oc] || (oc || "—");
  return '<span style="display:inline-block;padding:2px 10px;border-radius:6px;font-size:10px;font-weight:700;color:#fff;background:' + c + '">' + escapeHtml(l) + '</span>';
}

function _sourceBadge(src) {
  if (src === "adjusted") return ' <span style="font-size:9px;padding:1px 6px;border-radius:4px;background:#5856d6;color:#fff;font-weight:600">ADJ</span>';
  return "";
}

async function _loadTradeJournal() {
  var el = $("e2TradeJournalContent");
  var sec = $("e2TradeJournal");
  if (!el || !sec) return;

  try {
    var [histResp, perfResp] = await Promise.all([
      fetch("/api/spx-ic/trades/history?limit=20"),
      fetch("/api/spx-ic/trades/performance"),
    ]);
    if (!histResp.ok || !perfResp.ok) return;
    var histData = await histResp.json();
    var perfData = await perfResp.json();
    var trades = histData.trades || [];

    if (!trades.length && !perfData.hasData) {
      sec.classList.add("hidden");
      return;
    }

    sec.classList.remove("hidden");
    var html = '<div class="taPanel">';
    html += '<button id="e2TradeJournalToggle" type="button" style="width:100%;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 16px;border:none;background:transparent;cursor:pointer;border-bottom:1px solid var(--border);text-align:left">';
    html += '<span style="font-size:15px;font-weight:700">Trade Journal</span>';
    html += '<span style="font-size:12px;opacity:.6">Learning System · ' + (perfData.totalClosed || 0) + ' closed trades · ' + (_e2TradeJournalExpanded ? "Hide" : "Show") + '</span>';
    html += "</button>";
    html += '<div id="e2TradeJournalBody" style="display:' + (_e2TradeJournalExpanded ? "block" : "none") + ';">';

    if (perfData.hasData) {
      html += '<div style="padding:14px 20px;border-bottom:1px solid var(--border)">';
      html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:12px;font-size:12px">';

      var wr = perfData.winRate != null ? perfData.winRate + "%" : "—";
      var wrColor = perfData.winRate != null ? (perfData.winRate >= 60 ? "#34c759" : perfData.winRate >= 40 ? "#ff9f0a" : "#ff453a") : "var(--text-secondary)";
      html += '<div style="text-align:center"><div style="font-size:22px;font-weight:800;color:' + wrColor + '">' + wr + '</div><div style="color:var(--text-secondary);font-size:10px">Win Rate</div></div>';

      var pnlStr = perfData.totalPnl != null ? "$" + perfData.totalPnl.toFixed(2) : "—";
      var pnlColor = perfData.totalPnl > 0 ? "#34c759" : perfData.totalPnl < 0 ? "#ff453a" : "var(--text-secondary)";
      html += '<div style="text-align:center"><div style="font-size:22px;font-weight:800;color:' + pnlColor + '">' + pnlStr + '</div><div style="color:var(--text-secondary);font-size:10px">Total P&L</div></div>';

      var avgStr = perfData.avgPnl != null ? "$" + perfData.avgPnl.toFixed(2) : "—";
      html += '<div style="text-align:center"><div style="font-size:22px;font-weight:800;color:var(--text-primary)">' + avgStr + '</div><div style="color:var(--text-secondary);font-size:10px">Avg P&L</div></div>';

      html += '<div style="text-align:center"><div style="font-size:22px;font-weight:800;color:var(--text-primary)">' + perfData.wins + '-' + perfData.losses + '-' + perfData.scratches + '</div><div style="color:var(--text-secondary);font-size:10px">W-L-S</div></div>';

      html += '</div>';

      var tendency = perfData.riskTendency;
      if (tendency && tendency !== "balanced") {
        var tendencyLabels = {
          too_conservative: "System may be too conservative — high win rate but low P&L. Consider tighter EM or wider wings.",
          too_aggressive: "System may be too aggressive — low win rate suggests loosening EM or more PASS decisions.",
          risk_reward_skewed: "Risk/reward skewed — average losses significantly exceed average wins. Tighten management rules.",
        };
        html += '<div style="margin-top:10px;padding:8px 12px;border-radius:8px;background:rgba(255,159,10,0.1);border:1px solid rgba(255,159,10,0.2);font-size:11px;color:#ff9f0a">';
        html += '<span style="font-weight:700">Learning Insight:</span> ' + escapeHtml(tendencyLabels[tendency] || tendency);
        html += '</div>';
      }

      // EM and Wing breakdowns side by side
      var byEm = perfData.byEm || {};
      var byWing = perfData.byWing || {};
      if (Object.keys(byEm).length || Object.keys(byWing).length) {
        html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:12px">';
        if (Object.keys(byEm).length) {
          html += '<div><div style="font-size:10px;font-weight:700;text-transform:uppercase;color:var(--text-secondary);margin-bottom:4px">By EM Multiple</div>';
          html += '<div style="font-size:11px">';
          Object.keys(byEm).sort().forEach(function (k) {
            var b = byEm[k];
            html += '<div style="display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid var(--border)">';
            html += '<span>' + escapeHtml(k) + 'x</span><span class="mono">' + (b.winRate != null ? b.winRate + "%" : "—") + ' (' + b.n + ')</span>';
            html += '</div>';
          });
          html += '</div></div>';
        }
        if (Object.keys(byWing).length) {
          html += '<div><div style="font-size:10px;font-weight:700;text-transform:uppercase;color:var(--text-secondary);margin-bottom:4px">By Wing Width</div>';
          html += '<div style="font-size:11px">';
          Object.keys(byWing).sort().forEach(function (k) {
            var b = byWing[k];
            html += '<div style="display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid var(--border)">';
            html += '<span>' + escapeHtml(k) + '</span><span class="mono">' + (b.winRate != null ? b.winRate + "%" : "—") + ' (' + b.n + ')</span>';
            html += '</div>';
          });
          html += '</div></div>';
        }
        html += '</div>';
      }
      html += '</div>';
    }

    // Recent closed trades list
    if (trades.length) {
      html += '<div style="padding:10px 20px;border-bottom:1px solid var(--border)"><div style="font-size:10px;font-weight:700;text-transform:uppercase;color:var(--text-secondary);letter-spacing:0.5px">Recent Closed Trades</div></div>';
      trades.forEach(function (t) {
        var entry = t.entry || {};
        var outcome = t.outcome || {};
        var pnl = outcome.realizedPnl;
        var pnlDisp = pnl != null ? (pnl >= 0 ? "+$" + pnl.toFixed(2) : "-$" + Math.abs(pnl).toFixed(2)) : "—";
        var pnlC = pnl > 0 ? "#34c759" : pnl < 0 ? "#ff453a" : "var(--text-secondary)";

        html += '<div style="padding:10px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;font-size:12px">';
        html += '<div style="display:flex;align-items:center;gap:10px">';
        html += _outcomeBadge(outcome.outcomeClass);
        html += '<span style="font-weight:600">' + escapeHtml(entry.underlying || "SPX") + ' $' + escapeHtml(String(entry.wingWidth || "")) + ' wings · ' + escapeHtml(String(entry.emMultiple || "")) + 'x EM</span>';
        html += _sourceBadge(t.source);
        html += '</div>';
        html += '<div style="display:flex;align-items:center;gap:14px">';
        html += '<span class="mono" style="font-weight:700;color:' + pnlC + '">' + pnlDisp + '</span>';
        html += '<span style="color:var(--text-secondary);font-size:10px">' + escapeHtml(String(t.closedAt || "").slice(0, 10)) + '</span>';
        html += '</div></div>';
      });
    }

    html += "</div></div>";
    el.innerHTML = html;
    var tg = $("e2TradeJournalToggle");
    if (tg) {
      tg.addEventListener("click", function () {
        _e2TradeJournalExpanded = !_e2TradeJournalExpanded;
        _loadTradeJournal();
      });
    }
  } catch {
    // silently fail
  }
}

main();
_loadTradeJournal();

// ---------------------------------------------------------------------------
// Desk Insight Popup — LLM-powered card insights for Engine 2
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  var e2Popup = $("e2InsightPopup");
  if (!e2Popup) return;

  initDrag(e2Popup, $("e2InsightHeader"), { closeSelector: "#e2InsightClose" });
  $("e2InsightClose").addEventListener("click", function () { e2Popup.style.display = "none"; });

  var e2Insight = new InsightPopup({
    popupEl: e2Popup,
    titleEl: $("e2InsightTitle"),
    bodyEl:  $("e2InsightBody"),
    prefix:  "e2Insight",
    labels: {
      regime_read:"Regime Read",component_breakdown:"Component Breakdown",bucket_implications:"Bucket Implications",what_would_change:"What Would Change",
      macro_risk_level:"Macro Risk Level",key_events:"Key Events",multiplier_effect:"Multiplier Effect",
      probability_read:"Probability Read",width_selection:"Width Selection",conditioning_quality:"Conditioning Quality",directional_skew:"Directional Skew",
      dealer_regime:"Dealer Regime",key_levels:"Key Levels",gamma_peaks:"Gamma Peaks",condor_positioning:"Condor Positioning",
      stability_read:"Stability Read",flip_distances:"Flip Distances",risk_asymmetry:"Risk Asymmetry",condor_implications:"Condor Implications",
      hedging_flow_read:"Hedging Flow Read",elasticity_analysis:"Elasticity Analysis",scenario_walkthrough:"Scenario Walkthrough",
      tail_risk_map:"Tail Risk Map",air_pockets:"Air Pockets",wall_distances:"Wall Distances",
      vol_state:"Vol State",z_score_breakdown:"Z-Score Breakdown",iv_vs_rv:"IV vs RV",term_structure:"Term Structure",
      expected_move_read:"Expected Move Read",strike_targets:"Strike Targets",vwap_context:"VWAP Context",em_trend:"EM Trend",risk_overlay:"Risk Overlay",
      directional_read:"Directional Read",momentum_analysis:"Momentum Analysis",volatility_context:"Volatility Context",condor_relevance:"Condor Relevance",
      desk_takeaway:"Desk Takeaway",
    },
    onResult: function (cardType, resp) {
      if (cardType === "e2_expected_move" && resp && resp._source !== "fallback") {
        _lastEmInsight = resp;
        renderConvergence();
      }
    },
  });

  function fetchInsight(cardType, cardData, title, x, y) {
    var ctx = {};
    if (lastPayload) {
      ctx.underlying = lastPayload.underlying || {};
      ctx.regime = lastPayload.current?.regime || {};
      ctx.macro = lastPayload.current?.macro || {};
      ctx.expectedMove = lastPayload.expectedMove || {};
    }
    e2Insight.fetch(cardType, cardData, title, x, y, ctx);
  }

  var resultsObs = $("results");
  if (resultsObs) {
    var mo = new MutationObserver(function () { e2Insight.clearCache(); });
    mo.observe(resultsObs, { childList: true, subtree: false });
  }

  // Helper: wire a section by ID
  function wireSection(sectionId, cardType, titleFn, dataFn) {
    var el = $(sectionId);
    if (!el) return;
    el.classList.add("e2Click");
    el.title = "Click for desk insight";
    el.addEventListener("click", function (ev) {
      // Don't trigger on buttons, inputs, links inside the section
      if (ev.target.closest("button, a, input, select, .tipWrap, .gammaLegend, .tbInputs, .segmented")) return;
      if (!lastPayload) return;
      var d = dataFn();
      if (!d) return;
      fetchInsight(cardType, d, titleFn(), ev.clientX, ev.clientY);
    });
  }

  // Decision panel sub-cards are handled by event delegation above (data-e2-insight attrs)

  // ── Dealer Gamma Map ──
  wireSection("gammaMap", "e2_dealer_gamma",
    function () { return "Dealer Gamma Map"; },
    function () {
      var wf = lastPayload?.liveContext?.weeklyFriday || {};
      return { dealerGamma: wf.dealerGamma, oiClusters: wf.oiClusters, gammaFlipStrike: wf.gammaFlipStrike, spot: wf.spot };
    }
  );

  // ── GEX Heat Map ──
  wireSection("gexHeatMap", "e2_gex",
    function () { return "GEX Heatmap Analysis"; },
    function () {
      var gex = lastGammaPayload?.levels?.gexHeatmap || {};
      var lc = lastPayload?.liveContext?.weeklyFriday || {};
      return { stability: gex.stability, downFlip: gex.downFlip, upFlip: gex.upFlip, gammaFlipStrike: lc.gammaFlipStrike, spot: lc.spot };
    }
  );

  // ── Historical Odds Table ──
  wireSection("oddsSection", "e2_odds",
    function () { return "Historical Breach Odds"; },
    function () { return lastPayload?.oddsLikeNow || null; }
  );

  // ── Technicals ──
  wireSection("technicalsSection", "e2_technicals",
    function () { return "Technical Analysis"; },
    function () { return lastPayload?.technicals || null; }
  );

  // The decision panel contains sub-cards that are dynamically rendered.
  // We use event delegation on the decision section for sub-card clicks.
  var decisionEl = $("e2DecisionSection");
  if (decisionEl) {
    decisionEl.addEventListener("click", function (ev) {
      // Skip if the section-level handler already fired, or if a button/link
      if (ev.target.closest("button, a, input, select, .tipWrap")) return;
      if (!lastPayload) return;

      // Check for data attributes on clicked card
      var card = ev.target.closest("[data-e2-insight]");
      if (!card) return;
      ev.stopPropagation(); // prevent section-level regime insight

      var type = card.getAttribute("data-e2-insight");
      var title = card.getAttribute("data-e2-title") || type;
      var d = null;

      switch (type) {
        case "e2_regime":
          d = lastPayload?.current?.regime || null;
          break;
        case "e2_macro":
          d = lastPayload?.current?.macro || null;
          break;
        case "e2_odds":
          d = lastPayload?.oddsLikeNow || null;
          break;
        case "e2_dealer_gamma":
          // Check if clicked card is "nearest" variant
          var isNearest = (title || "").toLowerCase().indexOf("nearest") >= 0;
          var dgView = isNearest ? (lastPayload?.liveContext?.nearestDaily || {}) : (lastPayload?.liveContext?.weeklyFriday || {});
          d = { dealerGamma: dgView.dealerGamma, oiClusters: dgView.oiClusters, gammaFlipStrike: dgView.gammaFlipStrike, spot: dgView.spot };
          break;
        case "e2_hedging_pressure":
          var hpi = lastPayload?.liveContext?.weeklyFriday?.addons?.hedgingPressure || {};
          d = hpi;
          break;
        case "e2_tail_ignition":
          var ti = lastPayload?.liveContext?.weeklyFriday?.addons?.tailIgnition || {};
          d = ti;
          break;
        case "e2_vol_pressure":
          d = lastPayload?.liveContext?.volPressure || null;
          break;
        case "e2_expected_move":
          d = {
            expectedMove: lastPayload?.expectedMove || {},
            vwap: lastPayload?.current?.vwap || {},
            strikeTargets: lastPayload?.strikeTargets || {},
            riskContext: {
              macroMultiplier: lastPayload?.current?.macro?.multiplier,
              regimeScore: lastPayload?.current?.regime?.score,
              regimeBucket: lastPayload?.current?.regime?.bucket,
              dealerGamma: lastPayload?.liveContext?.weeklyFriday?.dealerGamma,
              gammaFlipStrike: lastPayload?.liveContext?.weeklyFriday?.gammaFlipStrike,
              volPressure: lastPayload?.liveContext?.volPressure,
              emPreference: lastPayload?.emPreference,
              emBreachSummary: lastPayload?.emBreachSummary,
            },
            deskConsensus: lastPayload?.deskConsensus || null,
          };
          break;
        default:
          return;
      }
      if (d) fetchInsight(type, d, title, ev.clientX, ev.clientY);
    });
  }
})();


