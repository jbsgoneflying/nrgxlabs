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

function logoUrlForTicker(ticker) {
  const t = String(ticker || "").trim().toUpperCase();
  if (!t) return null;
  // FMP serves ticker logos from a stable static URL. No API key required.
  // Example: https://financialmodelingprep.com/image-stock/AAPL.png
  return `https://financialmodelingprep.com/image-stock/${encodeURIComponent(t)}.png`;
}

function isoDate(d) {
  const dt = new Date(d);
  if (Number.isNaN(dt.getTime())) return null;
  const y = dt.getFullYear();
  const m = String(dt.getMonth() + 1).padStart(2, "0");
  const day = String(dt.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function parseIsoDate(s) {
  const t = String(s || "").slice(0, 10);
  const dt = new Date(`${t}T00:00:00`);
  return Number.isNaN(dt.getTime()) ? null : dt;
}

function addDays(d, n) {
  const dt = new Date(d);
  dt.setDate(dt.getDate() + Number(n));
  return dt;
}

function startOfWeek(d) {
  // Monday start (US markets planning)
  const dt = new Date(d);
  const dow = (dt.getDay() + 6) % 7; // Mon=0..Sun=6
  return addDays(dt, -dow);
}

function fmtRangeTitle(view, start, end, anchor) {
  const opts = { month: "long", day: "numeric", year: "numeric" };
  const s = start.toLocaleDateString(undefined, opts);
  const e = end.toLocaleDateString(undefined, opts);
  if (view === "day") return s;
  if (view === "week") return `${s} – ${e}`;
  // month: show Month YYYY from anchor (not padded start)
  const ref = anchor || start;
  const m = ref.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  return m;
}

async function fetchJson(url, { timeoutMs = 30000 } = {}) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), Number(timeoutMs));
  try {
    const r = await fetch(url, { signal: ctrl.signal });
    const txt = await r.text();
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${txt.slice(0, 300)}`);
    return JSON.parse(txt);
  } finally {
    clearTimeout(t);
  }
}

async function gateNavLinks() {
  // Feature-gated nav links (Engine 2).
  try {
    const flags = await fetchJson("/api/flags");
    const link = $("engine2Link");
    if (link) link.classList.toggle("hidden", !flags?.ENABLE_ENGINE2_SPX_IC);
  } catch {
    // If flags endpoint fails, keep the link hidden by default.
  }
}

const state = {
  view: "month",
  anchor: isoDate(new Date()),
  engine1Only: false,
  layers: { holiday: true, earlyClose: true, fed: true, econ: true, treasury: true, opex: true },
  lastPayload: null,
  rankCache: {},
};

function _allocMonthCaps({ bmoN, amcN, unkN }, totalCap = 8) {
  // Month view goal: keep each day to ~4 rows max.
  // With a 2-column tile grid, that means totalCap=8 tiles across all timing groups.
  const cap = Math.max(0, Number(totalCap) || 0);
  const groups = [
    { k: "BMO", n: Math.max(0, Number(bmoN) || 0) },
    { k: "AMC", n: Math.max(0, Number(amcN) || 0) },
    { k: "UNK", n: Math.max(0, Number(unkN) || 0) },
  ].filter(g => g.n > 0);

  const out = { BMO: 0, AMC: 0, UNK: 0 };
  if (!cap || groups.length === 0) return out;
  if (groups.length === 1) {
    out[groups[0].k] = cap;
    return out;
  }

  // Even split, then allocate leftovers in priority order: BMO -> AMC -> UNK.
  const base = Math.floor(cap / groups.length);
  let left = cap - base * groups.length;
  const order = ["BMO", "AMC", "UNK"];
  for (const g of groups) out[g.k] = base;
  for (const k of order) {
    if (left <= 0) break;
    if (out[k] > 0 || groups.some(g => g.k === k)) {
      out[k] += 1;
      left -= 1;
    }
  }
  return out;
}

function setStatus(msg, isError = false) {
  const el = $("status");
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("isError", !!isError);
  el.classList.toggle("isOk", !isError && !!msg);
}

function setView(view) {
  state.view = view;
  ["month", "week", "day"].forEach((v) => {
    const b = $(`view${v[0].toUpperCase()}${v.slice(1)}`);
    if (b) b.classList.toggle("isActive", v === view);
  });
}

function openSettings(open) {
  const m = $("settingsModal");
  if (!m) return;
  m.classList.toggle("hidden", !open);
}

function openPopover(open) {
  const p = $("popover");
  if (!p) return;
  p.classList.toggle("hidden", !open);
}

function buildCalendarUrl() {
  const params = new URLSearchParams();
  params.set("view", state.view);
  params.set("anchor", String(state.anchor || ""));
  params.set("tz", "America/New_York");
  params.set("engine1Only", state.engine1Only ? "1" : "0");
  params.set("includeEvents", "1");
  return `/api/calendar?${params.toString()}`;
}

function badgeForEvent(ev) {
  const kind = String(ev?.kind || "").toUpperCase();
  if (kind === "HOLIDAY") return "pill pill--event holiday";
  if (kind === "EARLY_CLOSE") return "pill pill--event earlyClose";
  if (kind === "FED") return "pill pill--event fed";
  if (kind === "ECON") return "pill pill--event econ";
  if (kind === "TREASURY") return "pill pill--event treasury";
  if (kind === "OPEX") return "pill pill--event opex";
  return "pill pill--event neutral";
}

function shouldShowEvent(ev) {
  const kind = String(ev?.kind || "").toUpperCase();
  if (kind === "HOLIDAY") return !!state.layers.holiday;
  if (kind === "EARLY_CLOSE") return !!state.layers.earlyClose;
  if (kind === "FED") return !!state.layers.fed;
  if (kind === "ECON") return !!state.layers.econ;
  if (kind === "TREASURY") return !!state.layers.treasury;
  if (kind === "OPEX") return !!state.layers.opex;
  return true;
}

function render(payload) {
  state.lastPayload = payload;
  const days = Array.isArray(payload?.days) ? payload.days : [];
  const view = String(payload?.view || state.view);
  const start = parseIsoDate(payload?.range?.start) || new Date();
  const end = parseIsoDate(payload?.range?.end) || new Date();
  const anchorDt = parseIsoDate(state.anchor) || new Date();

  const title = $("rangeTitle");
  const sub = $("rangeSub");
  if (title) title.textContent = fmtRangeTitle(view, start, end, anchorDt);
  if (sub) sub.textContent = `asOf ${payload?.meta?.generatedAt || "—"} · ${payload?.meta?.engine1Only ? "Engine‑1 eligible" : "All names"}`;

  const grid = $("calGrid");
  if (!grid) return;
  grid.className = `calGrid calGrid--${view}`;

  // Quick diagnostic: show a friendly hint if we got zero earnings.
  const totalTickers = days.reduce((acc, d) => {
    const e = d?.earnings || {};
    const b = Array.isArray(e?.BMO) ? e.BMO.length : 0;
    const a = Array.isArray(e?.AMC) ? e.AMC.length : 0;
    const u = Array.isArray(e?.UNK) ? e.UNK.length : 0;
    return acc + b + a + u;
  }, 0);
  if (totalTickers === 0) {
    const notes = Array.isArray(payload?.meta?.notes) ? payload.meta.notes.filter(Boolean) : [];
    const c = payload?.meta?.counts || {};
    const cnt = `rowsFetched=${c.earningsRowsFetched ?? "—"} inRange=${c.earningsRowsInRange ?? "—"} tickersSeen=${c.tickersSeen ?? "—"} eligible=${c.tickersEligible ?? "—"}`;
    setStatus(`No earnings names returned for this range. Try toggling off Engine‑1 filter in Settings. Debug: ${cnt}. ${notes.length ? `Notes: ${notes.slice(0, 2).join(" ")}` : ""}`, false);
  } else {
    setStatus("");
  }

  const headerCells = () => {
    const names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    if (view === "week") return names.slice(0, 5);
    if (view === "month") return names;
    return [];
  };

  const cells = [];
  const headers = headerCells();
  if (headers.length) {
    cells.push(...headers.map((h) => `<div class="calHeadCell">${escapeHtml(h)}</div>`));
  }

  days.forEach((d) => {
    const date = String(d?.date || "");
    const dt = parseIsoDate(date);
    const dow = dt ? dt.toLocaleDateString(undefined, { weekday: "short" }) : "";
    const dayNum = dt ? dt.getDate() : "";
    const isWeekend = dt ? (dt.getDay() === 0 || dt.getDay() === 6) : false;
    const isOutsideMonth = (view === "month" && dt)
      ? (dt.getMonth() !== anchorDt.getMonth() || dt.getFullYear() !== anchorDt.getFullYear())
      : false;

    const evs = (Array.isArray(d?.events) ? d.events : []).filter(shouldShowEvent);
    const earnings = d?.earnings || {};
    const bmo = Array.isArray(earnings?.BMO) ? earnings.BMO : [];
    const amc = Array.isArray(earnings?.AMC) ? earnings.AMC : [];
    const unk = Array.isArray(earnings?.UNK) ? earnings.UNK : [];

    const caps = (view === "month")
      ? _allocMonthCaps({ bmoN: bmo.length, amcN: amc.length, unkN: unk.length }, 8)
      : { BMO: 14, AMC: 14, UNK: 14 };

    const evShown = (view === "month" && evs.length > 2) ? evs.slice(0, 2) : evs;
    const evMore = (view === "month" && evs.length > 2) ? (evs.length - 2) : 0;
    const evHtml = evs.length
      ? `<div class="calEvents">
          ${evShown.map((ev) => `<div class="${badgeForEvent(ev)}" title="${escapeHtml(ev?.title || "")}">${escapeHtml(ev?.short || ev?.title || "")}</div>`).join("")}
          ${evMore > 0 ? `<div class="pill pill--event neutral" title="${escapeHtml(evs.map(e => e?.title).filter(Boolean).join(" · "))}">+${evMore}</div>` : ""}
        </div>`
      : "";

    const grp = (label, rows, cls, cap0) => {
      if (!rows.length) return "";
      const max = Math.max(0, Number(cap0) || 0);
      const shown = rows.slice(0, max);
      const rest = rows.length - shown.length;
      return `
        <div class="calEGroup ${cls}">
          <div class="calEGroupHead">${escapeHtml(label)}</div>
          <div class="calTiles">
            ${shown.map((r) => {
              const tk = String(r?.ticker || "").toUpperCase();
              const src = logoUrlForTicker(tk);
              const img = src
                ? `<img class="calTileLogo" src="${escapeHtml(src)}" alt="${escapeHtml(tk)} logo" loading="lazy" decoding="async" />`
                : "";
              return `<button class="calTile" type="button" data-ticker="${escapeHtml(tk)}" data-date="${escapeHtml(date)}" title="${escapeHtml(tk)}" aria-label="${escapeHtml(tk)}">
                ${img}
                <div class="calTileTicker">${escapeHtml(tk)}</div>
              </button>`;
            }).join("")}
            ${rest > 0 ? `<div class="calTileMore">+${rest}</div>` : ""}
          </div>
        </div>
      `;
    };

    const cellCls = ["calCell", isWeekend ? "isWeekend" : "", isOutsideMonth ? "isOutside" : ""].filter(Boolean).join(" ");

    cells.push(`
      <div class="${cellCls}" data-date="${escapeHtml(date)}">
        <div class="calCellHead">
          <div class="calCellDate"><span class="calDow">${escapeHtml(dow)}</span><span class="calDayNum">${escapeHtml(dayNum)}</span></div>
        </div>
        ${evHtml}
        <div class="calEarnings">
          ${grp("Before Open", bmo, "bmo", caps.BMO)}
          ${grp("After Close", amc, "amc", caps.AMC)}
          ${grp("Other", unk, "unk", caps.UNK)}
        </div>
      </div>
    `);
  });

  grid.innerHTML = cells.join("");

  // Hide broken logos (missing/404) cleanly.
  grid.querySelectorAll(".calTileLogo").forEach((img) => {
    img.addEventListener("error", () => {
      img.classList.add("hidden");
      const btn = img.closest && img.closest(".calTile");
      if (btn) btn.classList.add("calTile--noLogo");
    }, { once: true });
  });

  // ticker click handler (delegated)
  grid.querySelectorAll(".calTile").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const ticker = String(btn.getAttribute("data-ticker") || "").toUpperCase();
      const date = String(btn.getAttribute("data-date") || "");
      if (!ticker) return;
      await openTickerPopover(ticker, date);
    });
  });
}

async function openTickerPopover(ticker, date = "") {
  const popTitle = $("popTitle");
  const popBody = $("popBody");
  const popLink = $("popBreachLink");
  const popLogo = $("popLogo");

  if (popTitle) popTitle.textContent = ticker;
  if (popLogo) {
    const src = logoUrlForTicker(ticker);
    if (src) {
      popLogo.src = src;
      popLogo.classList.remove("hidden");
      popLogo.onerror = () => popLogo.classList.add("hidden");
    } else {
      popLogo.classList.add("hidden");
    }
  }
  if (popBody) {
    const src = state?.lastPayload?.meta?.counts?.snapshotSource || "—";
    const et = state?.lastPayload?.meta?.counts?.snapshotEtDate || "—";
    popBody.innerHTML = `
      <div class="muted" style="margin-bottom:10px;">
        Earnings date: <span class="mono">${escapeHtml(String(date || "—").slice(0, 10))}</span>
      </div>
      <div class="muted" style="margin-bottom:10px;">
        Source: <span class="mono">${escapeHtml(String(src || "—"))}</span> · snapshotET: <span class="mono">${escapeHtml(String(et || "—"))}</span>
      </div>
      <div class="muted" style="margin-bottom:10px;">
        Optional: compute a lightweight “earnings IC rank” for this ticker (on-demand; cached).
      </div>
      <div id="popRankBox" class="miniGrid"></div>
      <div style="display:flex; gap:10px; flex-wrap:wrap; margin-top:10px;">
        <button id="popRankBtn" class="primaryButton" type="button">Compute quick score</button>
        <button id="popRankClear" class="linkButton" type="button">Clear</button>
      </div>
      <div class="finePrint muted" style="margin-top:8px;">
        This is a pre-screen only. Use Engine 1 for the full report (MC + trade builder).
      </div>
    `;
  }

  // Deep link: open Engine 1 with MC enabled + autorun the report.
  if (popLink) {
    const qs = new URLSearchParams({
      ticker: String(ticker || "").toUpperCase(),
      mc: "1",
      mc_cond_regime: "1",
      mc_cond_quarter: "1",
      autorun: "1",
    });
    popLink.href = `/breach?${qs.toString()}`;
  }

  // Wire rank button (on-demand per ticker)
  const box = $("popRankBox");
  const btn = $("popRankBtn");
  const clear = $("popRankClear");
  const renderRank = (r) => {
    if (!box) return;
    if (!r || typeof r !== "object") {
      box.innerHTML = `<div class="muted">—</div>`;
      return;
    }
    const grade = r.grade || "—";
    const score = (r.score100 !== null && r.score100 !== undefined) ? Number(r.score100).toFixed(1) : "—";
    const em = (r.frontWeekEmPct !== null && r.frontWeekEmPct !== undefined) ? `${Number(r.frontWeekEmPct).toFixed(2)}%` : "—";
    const br15 = r?.breachRatePct?.k1_5;
    const br20 = r?.breachRatePct?.k2_0;
    const br15Txt = (br15 !== null && br15 !== undefined) ? `${Number(br15).toFixed(1)}%` : "—";
    const br20Txt = (br20 !== null && br20 !== undefined) ? `${Number(br20).toFixed(1)}%` : "—";
    box.innerHTML = `
      <div class="k">Condor rank</div><div class="v"><span class="pill pill--mini neutral">${escapeHtml(String(grade))}</span> <span class="mono">${escapeHtml(String(score))}</span></div>
      <div class="k">Front-week EM</div><div class="v mono">${escapeHtml(em)}</div>
      <div class="k">Breach rate</div><div class="v mono">k=1.5 ${escapeHtml(br15Txt)} · k=2.0 ${escapeHtml(br20Txt)}</div>
      <div class="k">As-of</div><div class="v mono">${escapeHtml(String(r.asOfDate || "—"))}</div>
    `;
  };

  if (box) renderRank(state.rankCache[String(ticker || "").toUpperCase()] || null);

  if (btn) {
    btn.onclick = async () => {
      const t = String(ticker || "").toUpperCase();
      if (!t) return;
      if (state.rankCache[t]) {
        renderRank(state.rankCache[t]);
        return;
      }
      if (box) box.innerHTML = `<div class="muted">Loading…</div>`;
      try {
        // Keep it lightweight: smaller lookback + fewer events than full Engine 1 default.
        const r = await fetchJson(`/api/condor-rank?ticker=${encodeURIComponent(t)}&n=12&years=5`, { timeoutMs: 45000 });
        state.rankCache[t] = r;
        renderRank(r);
      } catch (e) {
        if (box) box.innerHTML = `<div class="muted">Error: ${escapeHtml(String(e?.message || e))}</div>`;
      }
    };
  }
  if (clear) {
    clear.onclick = () => {
      const t = String(ticker || "").toUpperCase();
      if (t) delete state.rankCache[t];
      renderRank(null);
    };
  }

  openPopover(true);
}

async function refresh() {
  setStatus("Loading…");
  try {
    const payload = await fetchJson(buildCalendarUrl(), { timeoutMs: 60000 });
    render(payload);
    setStatus("");
  } catch (e) {
    setStatus(String(e?.message || e), true);
  }
}

function shiftAnchor(dir) {
  const a = parseIsoDate(state.anchor) || new Date();
  if (state.view === "month") {
    const dt = new Date(a);
    dt.setMonth(dt.getMonth() + dir);
    state.anchor = isoDate(dt);
  } else if (state.view === "week") {
    state.anchor = isoDate(addDays(a, 7 * dir));
  } else {
    state.anchor = isoDate(addDays(a, dir));
  }
}

function init() {
  // Non-blocking: just toggles Engine 2 nav visibility.
  gateNavLinks();

  setView("month");

  $("viewMonth")?.addEventListener("click", () => { setView("month"); refresh(); });
  $("viewWeek")?.addEventListener("click", () => { setView("week"); refresh(); });
  $("viewDay")?.addEventListener("click", () => { setView("day"); refresh(); });

  $("prevBtn")?.addEventListener("click", () => { shiftAnchor(-1); refresh(); });
  $("nextBtn")?.addEventListener("click", () => { shiftAnchor(+1); refresh(); });
  $("todayBtn")?.addEventListener("click", () => { state.anchor = isoDate(new Date()); refresh(); });

  $("settingsBtn")?.addEventListener("click", () => openSettings(true));
  $("settingsCloseBtn")?.addEventListener("click", () => openSettings(false));
  $("settingsCloseX")?.addEventListener("click", () => openSettings(false));
  $("settingsModal")?.addEventListener("click", (ev) => {
    // close when clicking backdrop (not the card)
    if (ev.target && ev.target.id === "settingsModal") openSettings(false);
  });

  $("engine1OnlyToggle")?.addEventListener("change", (ev) => {
    state.engine1Only = !!ev.target.checked;
    refresh();
  });

  const bindLayer = (id, key) => {
    $(id)?.addEventListener("change", (ev) => {
      state.layers[key] = !!ev.target.checked;
      // no API refresh needed; purely client-side filtering
      if (state.lastPayload) render(state.lastPayload);
    });
  };
  bindLayer("evHoliday", "holiday");
  bindLayer("evEarlyClose", "earlyClose");
  bindLayer("evFed", "fed");
  bindLayer("evEcon", "econ");
  bindLayer("evTreasury", "treasury");
  bindLayer("evOpex", "opex");

  $("popClose")?.addEventListener("click", () => openPopover(false));
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      openSettings(false);
      openPopover(false);
    }
  });
  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (t && t.closest && (t.closest("#popover") || t.closest(".calTile"))) return;
    openPopover(false);
  });

  refresh();
}

document.addEventListener("DOMContentLoaded", init);


