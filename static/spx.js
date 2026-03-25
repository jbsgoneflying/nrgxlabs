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

  // Width comparison from scan payload (if multi-wing enabled)
  var wc = payload?.widthComparison;
  if (Array.isArray(wc) && wc.length > 1) {
    var advisorSec = $("e2AdvisorSection");
    var advisorEl = $("e2AdvisorContent");
    if (advisorSec && advisorEl) {
      advisorSec.classList.remove("hidden");
      advisorEl.innerHTML = '<div class="taPanel"><div class="taHeader"><div class="taHeaderRow">' +
        '<span class="taHeaderTitle">AI Trade Advisor</span>' +
        '<span class="taHeaderActions"><button id="e2RunAdvisorBtn" class="primaryButton" type="button" style="font-size:11px;padding:6px 16px">Run Advisor</button></span>' +
        '</div></div></div>';
      renderWidthComparison(wc);
      var advBtn = $("e2RunAdvisorBtn");
      if (advBtn) advBtn.addEventListener("click", function () { _runAdvisor(); });
    }
  }

  // Load active trades
  _loadActiveTrades();

  if (status) {
    status.textContent = "";
    status.classList.remove("isRunning", "isError");
    status.classList.add("hidden");
  }
}

async function run() {
  const status = $("status");
  const entryDay = $("entryDay")?.value || "mon";
  const seasonalityMode = $("seasonalityMode")?.value || "none";
  // Desk-locked params
  const years = "2";
  const widths = "1.0,1.5,2.0";
  const weeksLimit = "0";

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

  if (verdict !== "PASS" && ticket.shortPutStrike) {
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

  // Log trade button
  if (verdict === "TRADE" || verdict === "LEAN_PASS") {
    html += '<div style="padding:12px 20px;border-top:1px solid var(--border);text-align:right">';
    html += '<button id="e2LogTradeBtn" class="primaryButton" type="button" style="font-size:12px;padding:8px 20px">Log This Trade</button>';
    html += '</div>';
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
}

function renderWidthComparison(widthComparison) {
  if (!Array.isArray(widthComparison) || !widthComparison.length) return;
  var el = $("e2AdvisorContent");
  if (!el) return;

  var emPref = lastPayload?.emPreference || {};
  var emBreachSummary = lastPayload?.emBreachSummary || {};
  var prefEm = emPref.emPreference || null;

  var emGroups = {};
  var emOrder = [];
  widthComparison.forEach(function (w) {
    var ek = String(w.emMult || "unknown");
    if (!emGroups[ek]) { emGroups[ek] = []; emOrder.push(ek); }
    emGroups[ek].push(w);
  });

  var _f = function(v, d) { return v !== null && v !== undefined ? Number(v).toFixed(d) + "%" : "—"; };
  var _fv = function(v, d) { return v !== null && v !== undefined ? Number(v).toFixed(d) : "—"; };
  var emLabels = { "1": "Aggressive", "1.0": "Aggressive", "1.5": "Standard", "2": "Defensive", "2.0": "Defensive" };

  var html = '<div class="taPanel" style="gap:8px;padding:14px">';
  html += '<div class="taHeader" style="padding:12px 14px 10px">';
  html += '<div class="taHeaderRow">';
  html += '<span class="taHeaderTitle">EM × Wing Width Analysis</span>';
  if (emPref.compositeScore != null) {
    html += '<span class="taHeaderMeta">Preferred: <strong>' + (prefEm || "—") + 'x</strong> ' + escapeHtml(emPref.label || "") + ' · Score ' + Number(emPref.compositeScore).toFixed(0) + '</span>';
  }
  html += '</div></div>';

  emOrder.forEach(function (ek) {
    var rows = emGroups[ek];
    var emVal = parseFloat(ek);
    var breachPct = emBreachSummary[ek];
    var isRec = prefEm != null && Math.abs(emVal - prefEm) < 0.01;
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

    html += '<div style="overflow:auto"><table class="dataTable" style="min-width:800px"><thead><tr>';
    html += '<th>Wing</th><th class="num">Outside %</th><th class="num">Max Loss</th><th class="num">Credit</th><th class="num">ROC %</th><th class="num">Risk-Adj ROC</th><th class="num">MAE95×Wing</th><th>Label</th>';
    html += '</tr></thead><tbody>';

    rows.sort(function(a,b){ return (b.riskAdjRocPct || 0) - (a.riskAdjRocPct || 0); });
    var topRaroc = rows.length ? rows[0].riskAdjRocPct : null;

    rows.forEach(function (w) {
      var isTop = isRec && topRaroc != null && w.riskAdjRocPct === topRaroc;
      var rowStyle = isTop ? ' style="background:rgba(52,199,89,0.06);font-weight:600"' : '';
      html += '<tr' + rowStyle + '>';
      html += '<td class="mono">$' + w.wingWidthPts + '</td>';
      html += '<td class="num mono">' + _f(w.outsidePct, 1) + '</td>';
      html += '<td class="num mono">$' + Number(w.maxLoss).toLocaleString() + '</td>';
      html += '<td class="num mono">$' + _fv(w.creditProxy, 0) + '</td>';
      html += '<td class="num mono">' + _f(w.rocPct, 1) + '</td>';
      html += '<td class="num mono">' + _f(w.riskAdjRocPct, 1) + '</td>';
      html += '<td class="num mono">' + _fv(w.avgMae95xWing, 3) + '</td>';
      html += '<td>' + escapeHtml(w.label || "") + (isTop ? " ★" : "") + '</td>';
      html += '</tr>';
    });
    html += '</tbody></table></div></div>';
  });
  html += '</div>';

  el.insertAdjacentHTML("beforeend", html);
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
  } catch (err) {
    if (el) el.innerHTML = '<div style="padding:16px;color:#ff453a;font-size:13px">Advisor error: ' + escapeHtml(String(err.message || err)) + '</div>';
  } finally {
    _advisorLoading = false;
  }
}

async function _logAdvisorTrade(advisorResult) {
  if (!advisorResult?.advisor?.tradeTicket) return;
  var btn = $("e2LogTradeBtn");
  if (btn) { btn.disabled = true; btn.textContent = "Logging..."; }
  try {
    var ticket = advisorResult.advisor.tradeTicket;
    var current = advisorResult.current || {};
    var em = advisorResult.expectedMove || {};
    var body = {
      source: "advisor",
      entry: {
        underlying: ticket.underlying || "SPX",
        spotAtEntry: Number(current.regime?.components?.spotPrice) || 0,
        entryDate: advisorResult.asOfDate || new Date().toISOString().slice(0, 10),
        expiryDate: ticket.expiry,
        shortPutStrike: ticket.shortPutStrike,
        longPutStrike: ticket.longPutStrike,
        shortCallStrike: ticket.shortCallStrike,
        longCallStrike: ticket.longCallStrike,
        wingWidth: ticket.wingWidth,
        emMultiple: ticket.emMultiple,
        entryCredit: parseFloat(String(ticket.estimatedCredit || "0").replace(/[^0-9.]/g, "")) || 0,
      },
      entryContext: {
        regimeScore: current.regime?.score,
        regimeBucket: current.regime?.bucket,
        macroBucket: current.macro?.bucket,
        emPct: em.oratsExpectedMovePct || em.expectedMovePct,
        volPressureState: lastPayload?.liveContext?.volPressure?.state,
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

// ---------------------------------------------------------------------------
// Active Trades Panel
// ---------------------------------------------------------------------------

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
    var html = '<div class="taPanel">';
    html += '<div class="taHeader"><span class="taHeaderTitle">Active Trades</span><span class="taHeaderMeta">' + trades.length + ' open</span></div>';

    trades.forEach(function (t) {
      var entry = t.entry || {};
      var tracking = t.tracking || {};
      var status = tracking.deterministicStatus || "on_track";
      var lastCheckin = (t.checkIns || []).slice(-1)[0];

      html += '<div style="padding:12px 20px;border-bottom:1px solid var(--border)">';
      html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">';
      html += '<div style="font-size:13px;font-weight:600">' + escapeHtml(entry.underlying || "SPX") + ' IC · $' + escapeHtml(String(entry.wingWidth || "")) + ' wings</div>';
      html += '<div style="display:flex;align-items:center;gap:8px">' + _statusBadge(status);
      html += '<button class="chipToggle" data-checkin-id="' + escapeHtml(t.tradeId) + '" type="button" style="font-size:10px;padding:3px 10px">Check In</button>';
      html += '<button class="chipToggle" data-close-id="' + escapeHtml(t.tradeId) + '" type="button" style="font-size:10px;padding:3px 10px">Close</button>';
      html += '</div></div>';

      html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:6px;font-size:11px">';
      html += '<div><span style="color:var(--text-secondary)">Short Put</span> <span class="mono">' + escapeHtml(String(entry.shortPutStrike || "—")) + '</span></div>';
      html += '<div><span style="color:var(--text-secondary)">Short Call</span> <span class="mono">' + escapeHtml(String(entry.shortCallStrike || "—")) + '</span></div>';
      html += '<div><span style="color:var(--text-secondary)">Spot</span> <span class="mono">' + (tracking.currentSpot ? Number(tracking.currentSpot).toFixed(2) : "—") + '</span></div>';
      html += '<div><span style="color:var(--text-secondary)">DTE</span> <span class="mono">' + (tracking.dte !== null && tracking.dte !== undefined ? tracking.dte : "—") + '</span></div>';
      html += '<div><span style="color:var(--text-secondary)">Put dist</span> <span class="mono">' + (tracking.distPutPct !== null ? tracking.distPutPct + "%" : "—") + '</span></div>';
      html += '<div><span style="color:var(--text-secondary)">Call dist</span> <span class="mono">' + (tracking.distCallPct !== null ? tracking.distCallPct + "%" : "—") + '</span></div>';
      html += '</div>';

      if (lastCheckin) {
        html += '<div style="margin-top:8px;padding:8px 10px;background:var(--bg-secondary);border-radius:8px;font-size:11px">';
        html += '<div style="font-weight:600;margin-bottom:2px">Latest check-in: ' + _statusBadge(lastCheckin.status || status) + '</div>';
        if (lastCheckin.headline) html += '<div style="color:var(--text-primary)">' + escapeHtml(lastCheckin.headline) + '</div>';
        if (lastCheckin.recommendation) html += '<div style="color:var(--text-secondary);margin-top:2px">' + escapeHtml(lastCheckin.recommendation) + '</div>';
        html += '</div>';
      }

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

async function _closeTrade(tradeId) {
  if (!confirm("Close this trade?")) return;
  try {
    var resp = await fetch("/api/spx-ic/trade/" + encodeURIComponent(tradeId) + "/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "manual" }),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    _loadActiveTrades();
  } catch (err) {
    alert("Close error: " + err.message);
  }
}

main();

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
      expected_move_read:"Expected Move Read",strike_targets:"Strike Targets",vwap_context:"VWAP Context",em_trend:"EM Trend",
      directional_read:"Directional Read",momentum_analysis:"Momentum Analysis",volatility_context:"Volatility Context",condor_relevance:"Condor Relevance",
      desk_takeaway:"Desk Takeaway",
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
          d = { expectedMove: lastPayload?.expectedMove || {}, vwap: lastPayload?.current?.vwap || {}, strikeTargets: lastPayload?.strikeTargets || {} };
          break;
        default:
          return;
      }
      if (d) fetchInsight(type, d, title, ev.clientX, ev.clientY);
    });
  }
})();


