/* global window, document */

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

function fmtPct(x, d = 2) {
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

function setLoading(isLoading) {
  const btn = $("runBtn");
  if (!btn) return;
  btn.disabled = !!isLoading;
  btn.classList.toggle("isLoading", !!isLoading);
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

async function fetchJson(url, { timeoutMs = 90000 } = {}) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), Number(timeoutMs));
  try {
    const r = await fetch(url, { signal: ctrl.signal });
    const txt = await r.text();
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${txt.slice(0, 300)}`);
    return JSON.parse(txt);
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

  const dots = Array.from({ length: 5 }).map((_, i) => `<span class="taDot ${i < 3 ? "isOn" : ""}"></span>`).join("");
  const chips = [
    `Regime: ${bucket}`,
    `Macro: ${macroBucket}`,
    (Number.isFinite(n10) ? `n=${n10}` : null),
  ].filter(Boolean);
  const chipHtml = chips.slice(0, 3).map((c) => `<span class="taChip">${escapeHtml(c)}</span>`).join("");

  host.classList.toggle("hidden", !sym || sym === "—");
  if (!sym || sym === "—") return;

  const snap = `${sym} (Engine 2) | asOf ${asOf} | spot ${spotTxt} | regime ${Number.isFinite(score) ? score.toFixed(1) : "—"}/100 ${bucket} | macro ${Number.isFinite(multVal) ? multVal.toFixed(2) + "×" : "—"} ${macroBucket} | odds(1.0x) ${Number.isFinite(odds10) ? odds10.toFixed(2) + "%" : "—"} | VWAP ${Number.isFinite(vwapVal) ? vwapVal.toFixed(2) : "—"} (${vwapDist})`;

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
            <button class="taActionBtn" type="button" id="e2CopySnapshot">Copy snapshot</button>
          </div>
        </div>
      </div>

      <div class="taGrid" aria-label="Engine 2 instrument cards">
        <div class="taCard">
          <div class="taCardTop"><div class="taCardTitle">Regime score</div></div>
          <div class="taCardState mono">${Number.isFinite(score) ? escapeHtml(score.toFixed(1)) + " / 100" : "—"}</div>
          <div class="taCardInterp">Bucket: ${escapeHtml(bucket)}</div>
          ${regChips.length ? `<div class="taCardInterp muted">${regChips.slice(0, 5).map(escapeHtml).join(" · ")}</div>` : ``}
        </div>
        <div class="taCard">
          <div class="taCardTop"><div class="taCardTitle">Macro multiplier</div></div>
          <div class="taCardState mono">${Number.isFinite(multVal) ? escapeHtml(multVal.toFixed(2)) + "×" : "—"}</div>
          <div class="taCardInterp">Bucket: ${escapeHtml(macroBucket)}${macroChipList.length ? ` · ${macroChipList.map(escapeHtml).join(" · ")}` : ""}</div>
          ${top3.length ? `<div class="taCardInterp muted">${top3.map((x) => `<div class="macroEventLine">${escapeHtml(String(x))}</div>`).join("")}${moreN ? `<div class="ref">+${escapeHtml(String(moreN))} more</div>` : ""}</div>` : ``}
        </div>
        <div class="taCard">
          <div class="taCardTop"><div class="taCardTitle">Breach odds (1.0×)</div></div>
          <div class="taCardState mono">${Number.isFinite(odds10) ? escapeHtml(odds10.toFixed(2)) + "%" : "—"}</div>
          <div class="taCardInterp">${Number.isFinite(n10) ? `n=${escapeHtml(String(n10))}` : "—"}</div>
          <div class="taCardInterp muted">${row15 ? `1.5× ${fmtPct(row15?.breachEitherPct, 2)} (n=${escapeHtml(String(row15?.n ?? "—"))})` : ""}${row20 ? `<br/>2.0× ${fmtPct(row20?.breachEitherPct, 2)} (n=${escapeHtml(String(row20?.n ?? "—"))})` : ""}</div>
        </div>
        <div class="taCard">
          <div class="taCardTop"><div class="taCardTitle">VWAP (daily)</div></div>
          <div class="taCardState mono">${Number.isFinite(vwapVal) ? escapeHtml(vwapVal.toFixed(2)) : "—"}</div>
          <div class="taCardInterp">${escapeHtml(vwapDist)}</div>
        </div>
        <div class="taCard">
          <div class="taCardTop"><div class="taCardTitle">Dealer gamma (weekly)</div></div>
          <div class="taCardState">${escapeHtml(wSum.main)}</div>
          <div class="taCardInterp muted">${escapeHtml(wSum.sub)}</div>
        </div>
        <div class="taCard">
          <div class="taCardTop"><div class="taCardTitle">Dealer gamma (nearest)</div></div>
          <div class="taCardState">${escapeHtml(nSum.main)}</div>
          <div class="taCardInterp muted">${escapeHtml(nSum.sub)}</div>
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

  const btn = $("e2CopySnapshot");
  if (btn) {
    btn.addEventListener("click", async () => {
      try {
        if (window.RavenUI?.copyToClipboard) await window.RavenUI.copyToClipboard(snap);
        else await navigator.clipboard.writeText(snap);
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
  // Snapshot section removed: regime/macro/odds/VWAP/live context are rendered in the decision header + details drawer.
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

  if (status) {
    // Keep the UI clean: hide status on success; it only matters for running/errors.
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

  try {
    setLoading(true);
    if (status) {
      status.textContent = "Running…";
      status.classList.remove("isError", "isOk");
      status.classList.add("isRunning");
      status.classList.remove("hidden");
    }
    const payload = await fetchJson(`/api/spx-ic?${qs.toString()}`);
    render(payload);
  } catch (e) {
    if (status) {
      status.textContent = `Error: ${String(e?.message || e)}`;
      status.classList.remove("isRunning", "isOk");
      status.classList.add("isError");
      status.classList.remove("hidden");
    }
    const results = $("results");
    if (results) results.classList.toggle("hidden", true);
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
  // AskRaven removed

  // Do NOT auto-run: user must review selections and click Run.
  const results = $("results");
  if (results) results.classList.toggle("hidden", true);
  if (status) {
    status.textContent = "Select parameters, then click Run.";
    status.classList.remove("isError", "isRunning", "isOk");
  }
}

main();



