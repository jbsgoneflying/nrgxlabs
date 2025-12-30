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

async function loadGammaMap() {
  const meta = $("gammaMeta");
  const note = $("gammaNote");
  const chart = $("gammaChart");
  if (!chart) return;

  try {
    if (meta) meta.textContent = "Loading…";
    if (note) note.textContent = "—";
    const v = gammaState.view;
    const payload = await fetchJson(`/api/spx-levels?view=${encodeURIComponent(v)}&points=90&window_days=180`, { timeoutMs: 45000 });
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
  if (!wrap) return;

  const heat = payload?.levels?.gexHeatmap || null;
  const enabled = !!heat?.enabled;
  const expiries = Array.isArray(heat?.expiries) ? heat.expiries : [];
  const strikes = Array.isArray(heat?.strikes) ? heat.strikes : [];
  const mat = Array.isArray(heat?.netDollarGex) ? heat.netDollarGex : [];
  const spot = Number(heat?.spot);
  const band = Number(heat?.bandPct);
  const wmode = String(heat?.weightingMode || "");

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

  if (!payload || !enabled || !expiries.length || !strikes.length || !mat.length) {
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
      const v = Number(row[j]);
      if (!Number.isFinite(v)) continue;
      maxAbs = Math.max(maxAbs, Math.abs(v));
    }
  }
  if (!Number.isFinite(maxAbs) || maxAbs <= 0) maxAbs = 1;

  const w = Math.max(320, wrap.clientWidth || 640);
  const pad = { l: 74, r: 10, t: 10, b: 26 };
  const rows = expiries.length;
  const cols = strikes.length;
  const cellH = 16;
  const cellW = Math.max(6, Math.floor((w - pad.l - pad.r) / Math.max(1, cols)));
  const h = pad.t + pad.b + rows * cellH;

  const xForCol = (c) => pad.l + c * cellW;
  const yForRow = (r) => pad.t + r * cellH;

  const scale = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n)) return 0;
    const a = Math.abs(n);
    // compress large dynamic range
    const t = Math.log10(1 + a / 1e6);
    const tMax = Math.log10(1 + maxAbs / 1e6);
    const u = tMax > 0 ? (t / tMax) : 0;
    return (n < 0 ? -u : u);
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
    meta.textContent = `spot=${Number.isFinite(spot) ? _fmtNum(spot, 2) : "—"} · band=±${b} · mode=${wmode || "—"} · rows=${rows} · cols=${cols}`;
  }
  if (note) {
    const warns = Array.isArray(heat?.warnings) ? heat.warnings.filter(Boolean) : [];
    const notes = Array.isArray(heat?.notes) ? heat.notes.filter(Boolean) : [];
    note.textContent = warns[0] || notes[0] || "Live, informational only.";
  }

  const yLabels = expiries.map((e) => String(e).slice(5)); // MM-DD
  const tickEvery = Math.max(1, Math.round(cols / 6));
  const xTicks = strikes.map((s, i) => ({ s: Number(s), i })).filter(t => (t.i % tickEvery) === 0);

  wrap.innerHTML = `
    <svg class="gexSvg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="SPX net $GEX heat map">
      <rect x="0" y="0" width="${w}" height="${h}" class="gexBg"></rect>
      ${yLabels.map((lab, r) => `<text x="${pad.l - 8}" y="${yForRow(r) + 12}" class="gexAxis gexAxis--y" text-anchor="end">${escapeHtml(lab)}</text>`).join("")}
      ${xTicks.map(t => `<text x="${xForCol(t.i) + 2}" y="${h - 10}" class="gexAxis gexAxis--x">${escapeHtml(fmt0(t.s))}</text>`).join("")}
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
    const exp = expiries[row];
    const strike = strikes[col];
    const v = (Array.isArray(mat[row]) ? mat[row][col] : null);
    const vNum = Number(v);
    const valTxt = Number.isFinite(vNum) ? fmtMoneyShort(vNum) : "—";
    const html = `
      <div class="chartTipTitle">Net $GEX</div>
      <div class="chartTipBody mono">${escapeHtml(String(exp))} · strike ${escapeHtml(fmt0(strike))}</div>
      <div class="chartTipDivider"></div>
      <div class="chartTipBody mono">${escapeHtml(valTxt)}</div>
      <div class="chartTipBody muted">spot=${escapeHtml(Number.isFinite(spot) ? _fmtNum(spot, 2) : "—")} · band=±${escapeHtml(Number.isFinite(band) ? String(Math.round(band * 100)) : "—")}%</div>
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

  if (meta) {
    const b = Number.isFinite(bandPct) ? `${Math.round(bandPct * 100)}%` : "—";
    meta.textContent = `expiry=${expiry} · spot=${Number.isFinite(spot) ? _fmtNum(spot, 2) : "—"} · band=±${b} · src=${sym}`;
  }

  const notes = Array.isArray(levels?.notes) ? levels.notes.filter(Boolean) : [];
  const warns = Array.isArray(levels?.warnings) ? levels.warnings.filter(Boolean) : [];
  if (note) note.textContent = notes[0] || (warns[0] || "Live, informational only.");

  if (!enabled || !series.length) {
    const msg = !series.length ? "No SPX price series returned." : "Live levels unavailable (missing live chain).";
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
    <svg class="gammaSvg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="SPX close chart">
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
      <div class="chartTipTitle">SPX</div>
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

function render(payload) {
  lastPayload = payload;
  const status = $("status");
  const results = $("results");
  if (results) results.classList.toggle("hidden", false);
  if (status) status.classList.remove("isError", "isRunning", "isOk");

  const meta = $("snapshotMeta");
  if (meta) meta.textContent = `asOf=${payload?.asOfDate || "—"} · entry=${payload?.params?.entryDay || "—"} · lookback=${payload?.params?.years || "—"}y · seasonality=${payload?.params?.seasonalityMode || "—"}`;

  const like = payload?.oddsLikeNow || {};
  const recMain = $("recMain");
  const recNote = $("recNote");
  if (recMain) {
    const rb = like?.regimeBucket || "—";
    const mb = like?.macroBucket || "—";
    const sb = like?.seasonBucket || "—";
    const n = like?.weeksUsed;
    recMain.textContent = `Bucket: ${rb} · ${mb} · ${sb}`;
    if (Number.isFinite(Number(n))) recMain.textContent += ` · n=${Number(n)}`;
  }
  if (recNote) {
    const rows = Array.isArray(like?.byWidth) ? like.byWidth : [];
    const notes = Array.isArray(like?.notes) ? like.notes.filter(Boolean) : [];
    const oddsLine = (r) => {
      const w = Number(r?.w);
      const n = Number(r?.n);
      const be = r?.breachEitherPct;
      return `<span class="pill pill--mini neutral">${Number.isFinite(w) ? w.toFixed(2) : "—"}× EM</span><span class="mono">${be === null || be === undefined ? "—" : Number(be).toFixed(2)}%</span><span class="ref">n=${Number.isFinite(n) ? n : "—"}</span>`;
    };
    recNote.innerHTML = `
      <div class="snapshotLines">
        <div class="snapLine">
          <div class="snapKey">Odds</div>
          <div class="pillRow">
            ${rows.length ? rows.map(r => `<span class="pillRow" style="gap:10px;">${oddsLine(r)}</span>`).join("<span class='spacerDot'>·</span>") : "—"}
          </div>
        </div>
        ${notes.length ? `<details class="snapDetails"><summary>Details</summary><div class="snapDetailBody">${escapeHtml(notes.join(" "))}</div></details>` : ""}
      </div>
    `;
  }

  const reg = payload?.current?.regime || {};
  const regimeMain = $("regimeMain");
  const regimeNote = $("regimeNote");
  if (regimeMain) {
    const s = reg.score100 !== null && reg.score100 !== undefined ? Number(reg.score100).toFixed(1) : "—";
    const b = reg.bucket || "—";
    regimeMain.textContent = `${s} / 100 · ${b}`;
  }
  if (regimeNote) {
    const c = reg?.components || {};
    const chips = [];
    if (c.trend !== null && c.trend !== undefined) chips.push({ k: "trend", v: Number(c.trend).toFixed(2) });
    if (c.volatility !== null && c.volatility !== undefined) chips.push({ k: "vol", v: Number(c.volatility).toFixed(2) });
    if (c.stress !== null && c.stress !== undefined) chips.push({ k: "stress", v: Number(c.stress).toFixed(2) });
    if (c.event !== null && c.event !== undefined) chips.push({ k: "event", v: Number(c.event).toFixed(2) });
    if (c.dispersion !== null && c.dispersion !== undefined) chips.push({ k: "disp", v: Number(c.dispersion).toFixed(2) });
    regimeNote.innerHTML = chips.length
      ? `<div class="chipRow">${chips.map(x => `<span class="chip"><span class="k">${escapeHtml(x.k)}</span><span class="mono">${escapeHtml(x.v)}</span></span>`).join("")}</div>`
      : "—";
  }

  const und = payload?.underlying || {};
  const underlying = $("underlying");
  const underlyingNote = $("underlyingNote");
  if (underlying) underlying.textContent = und.symbol || "—";
  if (underlyingNote) underlyingNote.textContent = (und.isProxy ? `Proxy used. ${Array.isArray(und.notes) ? und.notes.join(" ") : ""}` : "Direct") || "—";

  // Actionable VWAP level (daily; proxy)
  const vwap = payload?.current?.vwap || {};
  const vwapMain = $("vwapMain");
  const vwapNote = $("vwapNote");
  if (vwapMain || vwapNote) {
    const enabled = !!vwap?.enabled;
    if (!enabled) {
      if (vwapMain) vwapMain.textContent = "—";
      if (vwapNote) {
        const notes = Array.isArray(vwap?.notes) ? vwap.notes.filter(Boolean) : [];
        vwapNote.textContent = notes[0] || "VWAP level unavailable.";
      }
    } else {
      if (vwapMain) vwapMain.textContent = fmt2(vwap?.value);
      if (vwapNote) {
        const lp = vwap?.livePrice;
        const bd = vwap?.barDateUsed || "—";
        const mode = String(vwap?.mode || "");
        const modeLabel = (mode === "orats_daily_vwap")
          ? "ORATS daily VWAP"
          : (mode === "rolling_daily_typical_price_vwap")
            ? `Rolling VWAP proxy (window=${String(vwap?.window ?? "—")})`
            : (mode === "daily_typical_price")
              ? "Typical price (H+L+C)/3"
              : (mode ? mode : "—");

        const d = vwap?.distance || null;
        const side = String(d?.side || "");
        const dp = Number(d?.diffPts);
        const dpc = Number(d?.diffPct);

        const spotTxt = Number.isFinite(Number(lp)) ? Number(lp).toFixed(2) : "—";
        let distTxt = "distance=—";
        if (Number.isFinite(dp)) {
          const absPts = Math.abs(dp).toFixed(2);
          const pctTxt = Number.isFinite(dpc) ? `${Math.abs(dpc).toFixed(2)}%` : "—";
          if (side === "above") distTxt = `spot above by ${absPts} pts (${pctTxt})`;
          else if (side === "below") distTxt = `spot below by ${absPts} pts (${pctTxt})`;
          else if (side === "at") distTxt = `spot ≈ VWAP`;
          else distTxt = `Δ=${dp.toFixed(2)} pts`;
        }

        vwapNote.textContent = `bar=${String(bd)} · spot=${spotTxt} · ${distTxt} · ${modeLabel}`;
      }
    }
  }

  // Dealer Gamma Map (clean hover chart)
  // Fetch after a successful run so the panel stays in sync with the user's session.
  loadGammaMap();

  const macro = payload?.current?.macro || {};
  const macroMain = $("macroMain");
  const macroNote = $("macroNote");
  const macroMult = $("macroMult");
  const macroFlags = $("macroFlags");
  if (macroMain) {
    const c = macro?.highImpactUS?.count;
    const top = Array.isArray(macro?.highImpactUS?.top) ? macro.highImpactUS.top : [];
    macroMain.textContent = (c !== null && c !== undefined) ? `High-impact US events: ${c}` : "—";
    if (macroNote) {
      if (top.length) {
        macroNote.innerHTML = `<div class="macroEventList">${top.map(x => `<div class="macroEventLine">${escapeHtml(x)}</div>`).join("")}</div>`;
      } else if (Array.isArray(macro?.notes) && macro.notes.length) {
        macroNote.textContent = macro.notes.join(" ");
      } else {
        // If Benzinga is enabled and returns no high-impact events, show a clear "none" state.
        const count = (c !== null && c !== undefined) ? Number(c) : null;
        macroNote.textContent = (count === 0) ? "No high-impact US events detected for this window." : "—";
      }
    }
  }
  const multVal = (macro?.multiplier !== null && macro?.multiplier !== undefined) ? Number(macro.multiplier) : null;
  if (macroMult) macroMult.textContent = (multVal !== null && Number.isFinite(multVal)) ? Number(multVal).toFixed(2) + "×" : "—";

  // Tooltip range + interpretation (if present in DOM)
  const macroCapEl = $("macroCap");
  const macroRelEl = $("macroRel");
  if (macroCapEl || macroRelEl) {
    const cap = getMacroCap();
    if (macroCapEl) macroCapEl.textContent = Number(cap).toFixed(2);
    if (macroRelEl) {
      macroRelEl.textContent = (multVal !== null && Number.isFinite(multVal))
        ? `${Number(multVal).toFixed(2)}× → ${macroLevel(multVal, cap)}`
        : "—";
    }
  }

  if (macroFlags) {
    const f = macro?.flags || {};
    const bits = ["CPI","FOMC","NFP","OPEX","REFUNDING"].filter(k => f && f[k]);
    const mult = Number(macro?.multiplier);
    const hi = ["CPI","FOMC","NFP"].some(k => f && f[k]);
    const bucket = (Number.isFinite(mult) && (mult >= 1.25 || hi)) ? "MACRO" : "NORMAL";
    const pills = [`<span class="pill pill--mini neutral">${escapeHtml(bucket)}</span>`]
      .concat(bits.map(k => `<span class="pill pill--mini neutral">${escapeHtml(k)}</span>`));
    macroFlags.innerHTML = `<div class="pillRow">${pills.join("")}</div>`;
    macroFlags.classList.remove("hidden");
  }

  const bt = payload?.backtest || {};
  const btMain = $("btMain");
  const btNote = $("btNote");
  if (btMain) btMain.textContent = (bt.rowsUsed !== null && bt.rowsUsed !== undefined) ? `${bt.rowsUsed}` : "—";
  if (btNote) btNote.textContent = "Weeks used in backtest (filtered for missing prices/IV).";

  // Live dealer gamma context (informational only) — render BOTH weekly + nearest-daily views
  const lc = payload?.liveContext || null;
  const weekly = lc?.weeklyFriday || null;
  const daily = lc?.nearestDaily || null;

  // Backwards compatibility: if backend returns legacy liveContext shape, treat it as weekly.
  const weeklyFallback = (!weekly && lc && lc.dealerGamma) ? {
    enabled: !!lc.enabled,
    symbolUsed: lc.symbolUsed,
    expiry: lc.expiry,
    dealerGamma: lc.dealerGamma,
    oiClusters: lc.oiClusters,
    warnings: lc.warnings,
    notes: lc.notes,
  } : null;

  function renderLive(prefix, view) {
    const dgMain = $(prefix === "W" ? "dgMainW" : "dgMainD");
    const dgNote = $(prefix === "W" ? "dgNoteW" : "dgNoteD");
    const dgTop = $(prefix === "W" ? "dgTopW" : "dgTopD");
    const dgOi = $(prefix === "W" ? "dgOiW" : "dgOiD");
    const oiMeta = $(prefix === "W" ? "oiMetaW" : "oiMetaD");
    const oiPut = $(prefix === "W" ? "oiPutW" : "oiPutD");
    const oiCall = $(prefix === "W" ? "oiCallW" : "oiCallD");
    if (!dgMain || !dgNote || !dgTop || !dgOi || !oiMeta || !oiPut || !oiCall) return;

    const dg = view?.dealerGamma || null;
    const oi = view?.oiClusters || null;
    const enabled = !!(view && view.enabled && dg && dg.netGammaSign);
    if (!enabled) {
      dgMain.textContent = "—";
      const notes = Array.isArray(view?.notes) ? view.notes.filter(Boolean) : [];
      const warn = Array.isArray(view?.warnings) ? view.warnings.filter(Boolean) : [];
      dgNote.textContent = notes[0] || warn[0] || "Live context unavailable.";
      dgTop.textContent = "";
      dgOi.textContent = "—";
      oiMeta.textContent = "—";
      oiPut.textContent = "Put: —";
      oiCall.textContent = "Call: —";
      return;
    }

    dgMain.textContent = `${String(dg.netGammaSign || "").toUpperCase()} · ${String(dg.magnitudeBucket || "").toUpperCase()}`;
    dgNote.textContent = `symbol=${String(view.symbolUsed || "—")} · expiry=${String(view.expiry || "—")} · spot=${Number(dg.spot || 0).toFixed(2)} · band=±${Math.round(Number(dg.bandPct || 0.05) * 100)}% · weighting=${String(dg.weightingMode || "—")}`;

    const tops = Array.isArray(dg.topGammaStrikes) ? dg.topGammaStrikes : [];
    dgTop.textContent = tops.length ? `Top strikes: ${tops.map(x => `${Number(x.strike).toFixed(0)}${String(x.side || "")}`).join(" · ")}` : "";

    const putWall = oi && typeof oi === "object" ? oi.putWall : null;
    const callWall = oi && typeof oi === "object" ? oi.callWall : null;
    const putStrike = putWall && (putWall.peakStrike ?? putWall.maxStrike);
    const callStrike = callWall && (callWall.peakStrike ?? callWall.maxStrike);
    const putTxt = putWall && Number.isFinite(Number(putStrike)) ? `${Number(putStrike).toFixed(0)} (${Number(putWall.totalOI || 0).toFixed(0)})` : "—";
    const callTxt = callWall && Number.isFinite(Number(callStrike)) ? `${Number(callStrike).toFixed(0)} (${Number(callWall.totalOI || 0).toFixed(0)})` : "—";
    dgOi.textContent = `OI walls: put=${putTxt} | call=${callTxt}`;

    // OI clusters card
    const spot = Number(oi?.spot);
    const step = Number(oi?.strikeStep);
    const band = Number(oi?.bandPct);
    oiMeta.textContent = `expiry=${String(oi?.expiry || view.expiry || "—")} · spot=${fmt0(spot)} · band=±${Math.round((Number.isFinite(band) ? band : 0.05) * 100)}% · step=${fmt0(step)}`;

    const puts = _pickMeaningfulClusters(oi?.putClusters, spot, step).map(_fmtClusterLine);
    const calls = _pickMeaningfulClusters(oi?.callClusters, spot, step).map(_fmtClusterLine);
    oiPut.textContent = puts.length ? `Put: ${puts.join(" | ")}` : "Put: —";
    oiCall.textContent = calls.length ? `Call: ${calls.join(" | ")}` : "Call: —";
  }

  renderLive("W", weekly || weeklyFallback);
  renderLive("D", daily);

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

  initTooltips();
  initGammaMapUI();
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



