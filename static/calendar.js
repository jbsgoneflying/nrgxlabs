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

function openEventPopover(open) {
  const p = $("eventPopover");
  if (!p) return;
  p.classList.toggle("hidden", !open);
}

function _dayFromPayload(dateIso) {
  const days = Array.isArray(state?.lastPayload?.days) ? state.lastPayload.days : [];
  const t = String(dateIso || "").slice(0, 10);
  return days.find((d) => String(d?.date || "").slice(0, 10) === t) || null;
}

function closeExpandPanels() {
  document.querySelectorAll(".calExpandPanel").forEach((p) => p.remove());
  document.querySelectorAll(".calTileMore.isOpen, .calEventMore.isOpen").forEach((b) => b.classList.remove("isOpen"));
}

function _renderExpandTickers(dateIso, groupKey) {
  const d = _dayFromPayload(dateIso);
  const earnings = d?.earnings || {};
  const xs = Array.isArray(earnings?.[groupKey]) ? earnings[groupKey] : [];
  const tickers = xs.map((r) => String(r?.ticker || r || "").toUpperCase()).filter(Boolean);
  if (!tickers.length) return `<div class="muted">—</div>`;
  return `
    <div class="calExpandPills">
      ${tickers.map((tk) => `<button class="calExpandTicker" type="button" data-ticker="${escapeHtml(tk)}" data-date="${escapeHtml(String(dateIso || ""))}">${escapeHtml(tk)}</button>`).join("")}
    </div>
  `;
}

function _renderExpandEvents(dateIso) {
  const d = _dayFromPayload(dateIso);
  const evs = (Array.isArray(d?.events) ? d.events : []).filter(shouldShowEvent);
  if (!evs.length) return `<div class="muted">—</div>`;
  return `
    <div class="calExpandEvents">
      ${evs.map((ev) => {
        const cls = badgeForEvent(ev);
        const evJson = escapeHtml(JSON.stringify(ev || {}));
        const label = escapeHtml(String(ev?.short || ev?.title || "Event"));
        const title = escapeHtml(String(ev?.title || ev?.short || "Event"));
        return `<button class="${cls} calExpandEvent" type="button" data-ev="${evJson}" title="${title}" aria-label="${title}">${label}</button>`;
      }).join("")}
    </div>
  `;
}

function openExpandPanel({ cell, dateIso, kind, groupKey }) {
  if (!cell) return;
  const existing = cell.querySelector(".calExpandPanel");
  const isSame = existing && existing.getAttribute("data-kind") === String(kind || "") && existing.getAttribute("data-group") === String(groupKey || "");
  closeExpandPanels();
  if (isSame) return;

  const title = (kind === "events")
    ? "All events"
    : (groupKey === "BMO") ? "Before Open (BMO)" : (groupKey === "AMC") ? "After Close (AMC)" : "Other (UNK)";

  const bodyHtml = (kind === "events")
    ? _renderExpandEvents(dateIso)
    : _renderExpandTickers(dateIso, groupKey);

  const panel = document.createElement("div");
  panel.className = "calExpandPanel";
  panel.setAttribute("data-kind", String(kind || ""));
  panel.setAttribute("data-group", String(groupKey || ""));
  panel.innerHTML = `
    <div class="calExpandTop">
      <div class="calExpandTitle">${escapeHtml(title)}</div>
      <button class="calExpandHide pill pill--event neutral" type="button">Hide</button>
    </div>
    <div class="calExpandBody">${bodyHtml}</div>
  `;
  cell.appendChild(panel);

  panel.querySelector(".calExpandHide")?.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    closeExpandPanels();
  });

  panel.querySelectorAll(".calExpandTicker").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const tk = String(btn.getAttribute("data-ticker") || "").toUpperCase();
      const dIso = String(btn.getAttribute("data-date") || "");
      if (!tk) return;
      await openTickerPopover(tk, dIso);
    });
  });

  panel.querySelectorAll(".calExpandEvent").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const raw = btn.getAttribute("data-ev") || "{}";
      let eobj = {};
      try { eobj = JSON.parse(String(raw)); } catch { eobj = {}; }
      await openMacroPopover(eobj);
    });
  });
}

function closeAllTooltips() {
  document.querySelectorAll(".tipWrap.isOpen").forEach((w) => {
    w.classList.remove("isOpen");
    const b = w.querySelector(".tipBtn");
    if (b) b.setAttribute("aria-expanded", "false");
    const p = w.querySelector(".tipPanel");
    if (p) {
      p.style.display = "none";
      p.style.visibility = "";
    }
  });
}

function _placeFixedTooltip(wrap) {
  if (!wrap || !wrap.classList || !wrap.classList.contains("tipWrap--fixed")) return;
  const btn = wrap.querySelector(".tipBtn");
  const panel = wrap.querySelector(".tipPanel");
  if (!btn || !panel) return;

  // Temporarily show to measure.
  panel.style.visibility = "hidden";
  panel.style.display = "block";

  const br = btn.getBoundingClientRect();
  const pr = panel.getBoundingClientRect();
  const pad = 12;
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  // Prefer below button; if it would clip bottom, place above.
  let top = br.bottom + 10;
  if (top + pr.height + pad > vh) top = br.top - pr.height - 10;
  top = Math.max(pad, Math.min(top, vh - pr.height - pad));

  // Center-align over the button, clamped to viewport.
  let left = br.left + br.width / 2 - pr.width / 2;
  left = Math.max(pad, Math.min(left, vw - pr.width - pad));

  panel.style.top = `${Math.round(top)}px`;
  panel.style.left = `${Math.round(left)}px`;
  panel.style.visibility = "visible";
  // Allow CSS to control visibility; we only keep fixed top/left.
  panel.style.display = wrap.classList.contains("isOpen") ? "block" : "none";
}

function initTooltips() {
  // Delegated tooltips so dynamically injected rows also work.
  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (!(t && t.closest)) return;
    const btn = t.closest(".tipBtn");
    if (!btn) return;
    const wrap = btn.closest(".tipWrap");
    if (!wrap) return;
    ev.preventDefault();
    ev.stopPropagation();

    const isOpen = wrap.classList.contains("isOpen");
    closeAllTooltips();
    if (!isOpen) {
      wrap.classList.add("isOpen");
      btn.setAttribute("aria-expanded", "true");
      _placeFixedTooltip(wrap);
    }
  });

  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (t && t.closest && t.closest(".tipWrap")) return;
    closeAllTooltips();
  });

  window.addEventListener("resize", () => {
    // Reposition any open fixed tooltips
    document.querySelectorAll(".tipWrap--fixed.isOpen").forEach(_placeFixedTooltip);
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeAllTooltips();
  });
}

function _li(lines) {
  const xs = Array.isArray(lines) ? lines.filter(Boolean) : [];
  if (!xs.length) return `<div class="muted">—</div>`;
  return `<ul>${xs.map(x => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>`;
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
  // Month view goal: show as many logos as space allows.
  // With 36px logo tiles, totalCap=8 fits comfortably in each day cell.
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
  // Force FMP live data until ORATS snapshot has better coverage
  params.set("forceFmp", "1");
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

  // --- Scan panel (instrument-style summary, TA-cohesive) ---
  const scan = $("calendarScanSection");
  if (scan) {
    const total = days.reduce((acc, d) => {
      const e = d?.earnings || {};
      const b = Array.isArray(e?.BMO) ? e.BMO.length : 0;
      const a = Array.isArray(e?.AMC) ? e.AMC.length : 0;
      const u = Array.isArray(e?.UNK) ? e.UNK.length : 0;
      return { BMO: acc.BMO + b, AMC: acc.AMC + a, UNK: acc.UNK + u };
    }, { BMO: 0, AMC: 0, UNK: 0 });
    const earnTotal = total.BMO + total.AMC + total.UNK;

    const evAll = days.flatMap((d) => Array.isArray(d?.events) ? d.events : []).filter(shouldShowEvent);
    const evKinds = evAll.reduce((m, ev) => {
      const k = String(ev?.kind || "").toUpperCase();
      m[k] = (m[k] || 0) + 1;
      return m;
    }, {});
    const hiMacro = (evKinds.FED || 0) + (evKinds.ECON || 0);
    const holidayN = (evKinds.HOLIDAY || 0);
    const earlyCloseN = (evKinds.EARLY_CLOSE || 0);
    const opexN = (evKinds.OPEX || 0);

    const chips = [];
    chips.push(state.engine1Only ? "Engine‑1 eligible only" : "All names");
    chips.push(view === "month" ? "Month view" : view === "week" ? "Week view" : "Day view");
    if (hiMacro > 0) chips.push(`Macro events: ${hiMacro}`);
    else chips.push("Macro: quiet");
    const chipHtml = chips.slice(0, 3).map((c) => `<span class="taChip">${escapeHtml(c)}</span>`).join("");

    const asOf = payload?.meta?.generatedAt || "—";
    const range = `${isoDate(start) || "—"} → ${isoDate(end) || "—"}`;

    scan.innerHTML = `
      <div class="taPanel">
        <div class="taHeader">
          <div class="taHeaderRow">
            <div class="taHeaderTitle">Calendar — Scan</div>
            <div class="taHeaderMeta">Range: ${escapeHtml(range)} • asOf: ${escapeHtml(asOf)}</div>
          </div>
          <div class="taHeaderRow taHeaderRow--sub">
            <div class="taBiasPill taBiasPill--neu">PLANNING VIEW</div>
            <div class="taChips">${chipHtml}</div>
          </div>
        </div>

        <div class="taGrid taGrid--4" aria-label="Calendar instrument cards">
          <div class="taCard">
            <div class="taCardTop">
              <div class="taCardTitle">
                Earnings in range
                <span class="tipWrap">
                  <button class="tipBtn" type="button" aria-label="Earnings count help" aria-expanded="false">i</button>
                  <div class="tipPanel" role="tooltip">
                    <div class="tipTitle">Earnings count (BMO/AMC/UNK)</div>
                    <div class="tipBody">
                      <p>Counts earnings in the currently visible range. Use BMO vs AMC split to plan event windows.</p>
                    </div>
                  </div>
                </span>
              </div>
            </div>
            <div class="taCardState mono">${escapeHtml(String(earnTotal))}</div>
            <div class="taCardInterp">BMO ${escapeHtml(String(total.BMO))} · AMC ${escapeHtml(String(total.AMC))} · UNK ${escapeHtml(String(total.UNK))}</div>
          </div>

          <div class="taCard">
            <div class="taCardTop">
              <div class="taCardTitle">High-impact macro</div>
            </div>
            <div class="taCardState mono">${escapeHtml(String(hiMacro))}</div>
            <div class="taCardInterp">FED ${escapeHtml(String(evKinds.FED || 0))} · ECON ${escapeHtml(String(evKinds.ECON || 0))}</div>
          </div>

          <div class="taCard">
            <div class="taCardTop">
              <div class="taCardTitle">Market structure</div>
            </div>
            <div class="taCardState mono">${escapeHtml(String(holidayN + earlyCloseN + opexN))}</div>
            <div class="taCardInterp">Holiday ${escapeHtml(String(holidayN))} · Early close ${escapeHtml(String(earlyCloseN))} · OpEx ${escapeHtml(String(opexN))}</div>
          </div>

          <div class="taCard">
            <div class="taCardTop">
              <div class="taCardTitle">Universe filter</div>
            </div>
            <div class="taCardState">${state.engine1Only ? "Engine‑1 eligible" : "All names"}</div>
            <div class="taCardInterp">Change in Settings; affects what appears on the grid.</div>
          </div>
        </div>
      </div>
    `;
  }

  const title = $("rangeTitle");
  const sub = $("rangeSub");
  if (title) title.textContent = fmtRangeTitle(view, start, end, anchorDt);
  if (sub) {
    const c = payload?.meta?.counts || {};
    const source = c?.earningsSource ? String(c.earningsSource) : (c?.earningsSnapshotKind ? String(c.earningsSnapshotKind) : "");
    const snapDate = c?.snapshotEtDate ? String(c.snapshotEtDate) : "";
    const uniMode = c?.universeMode ? String(c.universeMode) : (c?.snapshotUniverseMode ? String(c.snapshotUniverseMode) : "");
    const uniSize = (c?.universeSize !== null && c?.universeSize !== undefined) ? String(c.universeSize) : (
      (c?.snapshotUniverseSize !== null && c?.snapshotUniverseSize !== undefined) ? String(c.snapshotUniverseSize) : ""
    );
    const fb = (c?.earningsFallbackUsed === true) ? "fallback" : "";
    const bits = [
      source ? `earnings=${source}${fb ? `(${fb})` : ""}` : "",
      snapDate ? `etDate=${snapDate}` : "",
      (uniMode || uniSize) ? `universe=${uniMode || "—"}${uniSize ? `:${uniSize}` : ""}` : "",
    ].filter(Boolean);
    const srcTxt = bits.length ? ` · ${bits.join(" · ")}` : "";
    sub.textContent = `asOf ${payload?.meta?.generatedAt || "—"} · ${payload?.meta?.engine1Only ? "Engine‑1 eligible" : "All names"}${srcTxt}`;
  }

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
    // Month view: trading week only (hide weekend columns for more screen room).
    if (view === "month") return names.slice(0, 5);
    return [];
  };

  const cells = [];
  const headers = headerCells();
  if (headers.length) {
    cells.push(...headers.map((h) => `<div class="calHeadCell">${escapeHtml(h)}</div>`));
  }

  const daysToRender = (view === "month")
    ? days.filter((d) => {
      const dt = parseIsoDate(d?.date);
      if (!dt) return true;
      const wd = dt.getDay(); // Sun=0..Sat=6
      return wd !== 0 && wd !== 6; // Mon-Fri only
    })
    : days;

  daysToRender.forEach((d) => {
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

    // With smaller 36px logo tiles, we can show more per day
    const caps = (view === "month")
      ? _allocMonthCaps({ bmoN: bmo.length, amcN: amc.length, unkN: unk.length }, 8)
      : { BMO: 20, AMC: 20, UNK: 20 };

    const evShown = (view === "month" && evs.length > 2) ? evs.slice(0, 2) : evs;
    const evMore = (view === "month" && evs.length > 2) ? (evs.length - 2) : 0;
    const evHtml = evs.length
      ? `<div class="calEvents">
          ${evShown.map((ev) => {
            const cls = badgeForEvent(ev);
            const evJson = escapeHtml(JSON.stringify(ev || {}));
            const label = escapeHtml(ev?.short || ev?.title || "");
            const title = escapeHtml(ev?.title || "");
            // Use a button so we can open a macro-event popover on click.
            return `<button class="${cls} calEventPill" type="button" data-ev="${evJson}" title="${title}" aria-label="${title}">${label}</button>`;
          }).join("")}
          ${evMore > 0 ? `<button class="pill pill--event neutral calEventMore" type="button" data-date="${escapeHtml(date)}" title="Show all events">+${evMore}</button>` : ""}
        </div>`
      : "";

    // Helper to get a color index for ticker (for fallback initials)
    const getColorIndex = (tk) => ((tk.charCodeAt(0) || 0) % 5) + 1;
    
    const grp = (label, key, rows, cls, cap0) => {
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
              const colorIdx = getColorIndex(tk);
              // Logo-only tile with tooltip via data-ticker attribute
              return `<button class="calTile" type="button" data-ticker="${escapeHtml(tk)}" data-date="${escapeHtml(date)}" data-color="${colorIdx}" aria-label="${escapeHtml(tk)}">
                <img class="calTileLogo" src="${escapeHtml(src || '')}" alt="" loading="lazy" decoding="async" data-ticker="${escapeHtml(tk)}" />
              </button>`;
            }).join("")}
            ${rest > 0 ? `<button class="calTileMore" type="button" data-date="${escapeHtml(date)}" data-group="${escapeHtml(key)}" title="Show all tickers">+${rest}</button>` : ""}
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
          ${grp("Before Open", "BMO", bmo, "bmo", caps.BMO)}
          ${grp("After Close", "AMC", amc, "amc", caps.AMC)}
          ${grp("Other", "UNK", unk, "unk", caps.UNK)}
        </div>
      </div>
    `);
  });

  grid.innerHTML = cells.join("");

  // Handle broken logos (missing/404) - show ticker initial fallback
  grid.querySelectorAll(".calTileLogo").forEach((img) => {
    img.addEventListener("error", () => {
      const btn = img.closest && img.closest(".calTile");
      const ticker = img.getAttribute("data-ticker") || btn?.getAttribute("data-ticker") || "?";
      const colorIdx = btn?.getAttribute("data-color") || "1";
      const initial = ticker.charAt(0).toUpperCase();
      // Replace logo with colored initial
      if (btn) {
        img.remove();
        const fallback = document.createElement("div");
        fallback.className = "calTileInitial";
        fallback.setAttribute("data-color", colorIdx);
        fallback.textContent = initial;
        btn.appendChild(fallback);
      }
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

  // Macro event click handler
  grid.querySelectorAll(".calEventPill").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const raw = btn.getAttribute("data-ev") || "{}";
      let ev = {};
      try { ev = JSON.parse(String(raw)); } catch { ev = {}; }
      await openMacroPopover(ev);
    });
  });

  // +N (tickers) click → expand overlay within the day cell (keeps cell height fixed)
  grid.querySelectorAll(".calTileMore").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const cell = btn.closest(".calCell");
      const date = String(btn.getAttribute("data-date") || cell?.getAttribute("data-date") || "");
      const groupKey = String(btn.getAttribute("data-group") || "");
      if (!cell || !date || !groupKey) return;
      btn.classList.add("isOpen");
      openExpandPanel({ cell, dateIso: date, kind: "earnings", groupKey });
    });
  });

  // +N (events) click → expand overlay within the day cell (keeps cell height fixed)
  grid.querySelectorAll(".calEventMore").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const cell = btn.closest(".calCell");
      const date = String(btn.getAttribute("data-date") || cell?.getAttribute("data-date") || "");
      if (!cell || !date) return;
      btn.classList.add("isOpen");
      openExpandPanel({ cell, dateIso: date, kind: "events", groupKey: "" });
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
    popBody.innerHTML = `
      <div class="finePrint muted" style="margin-bottom:10px;">
        Quick score is on-demand (no per-ticker calendar calls).
      </div>
      <div id="popRankBox" class="miniGrid"></div>
      <div style="display:flex; gap:10px; flex-wrap:wrap; margin-top:10px;">
        <button id="popRankBtn" class="primaryButton" type="button">Compute quick score</button>
        <button id="popRankClear" class="linkButton" type="button">Clear</button>
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
    const br10 = r?.breachRatePct?.k1_0;
    const br15 = r?.breachRatePct?.k1_5;
    const br20 = r?.breachRatePct?.k2_0;
    const eu = r?.eventsUsed;
    const euTxt = (eu !== null && eu !== undefined) ? String(eu) : "—";
    const br10Txt = (br10 !== null && br10 !== undefined) ? `${Number(br10).toFixed(1)}%` : "—";
    const br15Txt = (br15 !== null && br15 !== undefined) ? `${Number(br15).toFixed(1)}%` : "—";
    const br20Txt = (br20 !== null && br20 !== undefined) ? `${Number(br20).toFixed(1)}%` : "—";
    box.innerHTML = `
      <div class="k">Condor rank</div><div class="v"><span class="pill pill--mini neutral">${escapeHtml(String(grade))}</span> <span class="mono">${escapeHtml(String(score))}</span></div>
      <div class="k">Front-week EM</div><div class="v mono">${escapeHtml(em)}</div>
      <div class="k">Breach rate</div><div class="v mono">k=1.0 ${escapeHtml(br10Txt)} · k=1.5 ${escapeHtml(br15Txt)} · k=2.0 ${escapeHtml(br20Txt)}</div>
      <div class="k">Events used</div><div class="v mono">${escapeHtml(euTxt)}</div>
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

async function openMacroPopover(ev) {
  const title = $("evTitle");
  const meta = $("evMeta");
  const nums = $("evNumbers");
  const desk = $("evDesk");
  const watch = $("evWatch");
  const stats = $("evStats");

  const t = String(ev?.title || ev?.short || "Macro event");
  if (title) title.textContent = t;

  const imp = ev?.importance;
  const timeEt = ev?.timeEt;
  const kind = String(ev?.kind || "—");
  const key = String(ev?.key || "").toUpperCase();

  if (meta) {
    const impTxt = (imp !== null && imp !== undefined) ? `importance=${String(imp)}` : "importance=—";
    const timeTxt = timeEt ? `time=${String(timeEt)}` : "time=—";
    const keyTxt = key ? `key=${key}` : "key=—";
    meta.textContent = `${kind} · ${keyTxt} · ${timeTxt} · ${impTxt}`;
  }

  const f = ev?.forecast;
  const p = ev?.previous;
  const a = ev?.actual;
  const unit = ev?.unit ? String(ev.unit) : "";
  const fmt = (x) => (x === null || x === undefined || Number.isNaN(Number(x))) ? "—" : `${Number(x)}${unit ? " " + unit : ""}`;
  if (nums) {
    nums.innerHTML = `
      <div class="k">Forecast</div><div class="v mono">${escapeHtml(fmt(f))}</div>
      <div class="k">Previous</div><div class="v mono">${escapeHtml(fmt(p))}</div>
      <div class="k">Actual</div><div class="v mono">${escapeHtml(fmt(a))}</div>
    `;
  }

  const pb = ev?.playbook || null;
  if (desk) desk.innerHTML = _li(pb?.deskView);
  if (watch) watch.innerHTML = _li(pb?.watch);

  if (stats) stats.innerHTML = `<div class="muted">Loading…</div>`;
  if (stats && key) {
    try {
      const s = await fetchJson(`/api/macro-event-stats?key=${encodeURIComponent(key)}`, { timeoutMs: 45000 });
      if (!s?.enabled) {
        stats.innerHTML = `<div class="muted">${escapeHtml((Array.isArray(s?.notes) && s.notes.length) ? s.notes[0] : "Stats unavailable.")}</div>`;
      } else {
        const ed = s?.spy?.eventDayCloseToClose || {};
        const nd = s?.spy?.nextDayCloseToClose || {};
        const pd = s?.spy?.priorDayCloseToClose || {};
        const spot = (s.spySpotClose !== null && s.spySpotClose !== undefined) ? Number(s.spySpotClose) : null;
        const fmtPct = (x) => (x === null || x === undefined) ? "—" : `${Number(x).toFixed(2)}%`;
        const fmtPts = (x) => (x === null || x === undefined) ? "—" : `${Number(x).toFixed(2)} pts`;
        const fmtBand = (pts, pct) => `${fmtPts(pts)} (${fmtPct(pct)})`;
        const tip = (label, text) => `
          <span class="tipWrap tipWrap--fixed" style="margin-left:8px;">
            <button class="tipBtn" type="button" aria-label="${escapeHtml(label)} help" aria-expanded="false">i</button>
            <div class="tipPanel" role="tooltip">
              <div class="tipTitle">${escapeHtml(label)}</div>
              <div class="tipBody">${text}</div>
            </div>
          </span>
        `;
        stats.innerHTML = `
          <div class="k">SPY close used${tip("SPY close used", "<p>This is the <b>SPY last close</b> used to convert % moves into <b>price points</b>.</p><p>Points = (abs% / 100) × spot close.</p>")}</div>
          <div class="v mono">${escapeHtml(spot === null || Number.isNaN(spot) ? "—" : spot.toFixed(2))}</div>

          <div class="k">Events used${tip("Events used", "<p>How many historical event occurrences were found and could be matched to valid SPY trading-day closes.</p><p><b>Higher n</b> = more stable stats. Small n can be noisy.</p>")}</div>
          <div class="v mono">${escapeHtml(String(s.eventsUsed ?? "—"))}</div>

          <div class="k">Event day |median|${tip("Event day |median|", "<p>Typical <b>absolute</b> SPY close→close move on the event day (vs prior trading day close).</p><p>Use as a baseline for expected movement risk.</p>")}</div>
          <div class="v mono">${escapeHtml(fmtBand(ed.medianAbsPts, ed.medianAbsPct))}</div>

          <div class="k">Event day p90 |abs|${tip("Event day p90 |abs|", "<p>A tail-ish reference: 90% of matched events had an <b>absolute</b> move at or below this level on event day.</p><p><b>Desk use</b>: sanity-check wing width / size so the structure survives an outsized move.</p>")}</div>
          <div class="v mono">${escapeHtml(fmtBand(ed.p90AbsPts, ed.p90AbsPct))}</div>

          <div class="k">Next day |median|${tip("Next day |median|", "<p>Typical <b>absolute</b> SPY close→close move on the day <b>after</b> the event (follow-through risk).</p><p>Helps gauge whether the event tends to extend or mean-revert.</p>")}</div>
          <div class="v mono">${escapeHtml(fmtBand(nd.medianAbsPts, nd.medianAbsPct))}</div>

          <div class="k">Next day p90 |abs|${tip("Next day p90 |abs|", "<p>90th percentile <b>absolute</b> follow-through move the day after.</p><p>Useful for risk planning if you intend to hold positions beyond the event day.</p>")}</div>
          <div class="v mono">${escapeHtml(fmtBand(nd.p90AbsPts, nd.p90AbsPct))}</div>

          <div class="k">Prior day |median|${tip("Prior day |median|", "<p>Typical <b>absolute</b> SPY close→close move on the day before the event (pre-positioning / drift proxy).</p><p>Not causal—just context.</p>")}</div>
          <div class="v mono">${escapeHtml(fmtBand(pd.medianAbsPts, pd.medianAbsPct))}</div>
        `;
      }
    } catch (e) {
      stats.innerHTML = `<div class="muted">Error: ${escapeHtml(String(e?.message || e))}</div>`;
    }
  }

  openEventPopover(true);
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
    // Month shifting: clamp the day-of-month to avoid JS Date rollover
    // (e.g. Jan 31 + 1 month => Mar 3 because Feb doesn't have 31).
    const dt = new Date(a);
    const origDay = dt.getDate();
    dt.setDate(1); // safe pivot day
    dt.setMonth(dt.getMonth() + dir);
    const y = dt.getFullYear();
    const m = dt.getMonth(); // 0-based
    const lastDay = new Date(y, m + 1, 0).getDate();
    dt.setDate(Math.min(origDay, lastDay));
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
  initTooltips();
  try { window.RavenUI?.initInfoTips?.(); } catch { /* ignore */ }

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
  $("evClose")?.addEventListener("click", () => openEventPopover(false));
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      openSettings(false);
      openPopover(false);
      openEventPopover(false);
      closeExpandPanels();
    }
  });
  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (t && t.closest && (t.closest("#popover") || t.closest("#eventPopover") || t.closest(".calTile") || t.closest(".calEventPill") || t.closest(".calTileMore") || t.closest(".calEventMore") || t.closest(".calExpandPanel"))) return;
    openPopover(false);
    openEventPopover(false);
    closeExpandPanels();
  });

  refresh();
}

document.addEventListener("DOMContentLoaded", init);


