/* global window, document, navigator */

// Raven UI Kit: shared helpers for the whole app (no dependencies).
// This is a plain script (not an ES module) so it works with existing static pages.

function $(id) {
  return document.getElementById(id);
}

function escapeHtml(s) {
  const t = String(s ?? "");
  return t
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function fmtPct(v, d = 2) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `${n.toFixed(d)}%`;
}

function fmt0(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(0) : "—";
}

function fmt2(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(2) : "—";
}

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function setHtml(id, html) {
  const el = $(id);
  if (el) el.innerHTML = html;
}

function clamp(x, lo, hi) {
  const n = Number(x);
  if (!Number.isFinite(n)) return lo;
  return Math.max(Number(lo), Math.min(Number(hi), n));
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

async function copyToClipboard(text) {
  const t = String(text ?? "");
  if (!t) return false;
  try {
    await navigator.clipboard.writeText(t);
    return true;
  } catch {
    return false;
  }
}

function initTooltips() {
  // If a page already defines initTooltips (legacy), don't override it.
  // This kit provides a stable delegated tooltip implementation for `.tipWrap`.
  if (typeof window.initTooltips === "function") {
    try { window.initTooltips(); } catch { /* ignore */ }
    return;
  }

  function closeAllTooltips() {
    document.querySelectorAll(".tipWrap.isOpen").forEach((w) => {
      w.classList.remove("isOpen");
      // Also remove lifted state from parent card
      const parentCard = w.closest(".taCard");
      if (parentCard) parentCard.classList.remove("taCard--tipOpen");
      const b = w.querySelector(".tipBtn");
      if (b) b.setAttribute("aria-expanded", "false");
      const p = w.querySelector(".tipPanel");
      if (p) {
        p.style.display = "none";
        p.style.visibility = "";
      }
    });
  }

  function placeFixedTooltip(wrap) {
    if (!wrap?.classList?.contains("tipWrap--fixed")) return;
    const btn = wrap.querySelector(".tipBtn");
    const panel = wrap.querySelector(".tipPanel");
    if (!btn || !panel) return;

    panel.style.visibility = "hidden";
    panel.style.display = "block";

    const br = btn.getBoundingClientRect();
    const pr = panel.getBoundingClientRect();
    const pad = 12;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    let top = br.bottom + 10;
    if (top + pr.height + pad > vh) top = br.top - pr.height - 10;
    top = Math.max(pad, Math.min(top, vh - pr.height - pad));

    let left = br.left + br.width / 2 - pr.width / 2;
    left = Math.max(pad, Math.min(left, vw - pr.width - pad));

    panel.style.top = `${Math.round(top)}px`;
    panel.style.left = `${Math.round(left)}px`;
    panel.style.visibility = "visible";
    panel.style.display = wrap.classList.contains("isOpen") ? "block" : "none";
  }

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
      // Lift parent card above siblings so tooltip isn't clipped
      const parentCard = wrap.closest(".taCard");
      if (parentCard) parentCard.classList.add("taCard--tipOpen");
      btn.setAttribute("aria-expanded", "true");
      placeFixedTooltip(wrap);
    }
  });

  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (t && t.closest && t.closest(".tipWrap")) return;
    closeAllTooltips();
  });

  window.addEventListener("resize", () => {
    document.querySelectorAll(".tipWrap--fixed.isOpen").forEach(placeFixedTooltip);
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeAllTooltips();
  });

  // expose for other scripts (idempotent)
  window.initTooltips = () => {};
}

function initInfoTips() {
  // Click-to-open popover for legacy `.info` icons that only had a title attribute.
  // Uses the same `.taTipPop` styling as the TA panel so behavior is consistent on touch devices.
  if (window.__RavenInfoTipsInit) return;
  window.__RavenInfoTipsInit = true;

  let tipEl = null;
  let lastAnchor = null;

  const closeTip = () => {
    if (tipEl && tipEl.parentNode) tipEl.parentNode.removeChild(tipEl);
    tipEl = null;
    lastAnchor = null;
  };

  const openTip = (anchor, text) => {
    closeTip();
    const msg = String(text || "").trim();
    if (!msg) return;
    lastAnchor = anchor;
    tipEl = document.createElement("div");
    tipEl.className = "taTipPop taGlass";
    tipEl.setAttribute("role", "tooltip");
    const body = document.createElement("div");
    body.className = "taTipPopBody";
    body.style.whiteSpace = "pre-wrap";
    body.textContent = msg;
    tipEl.appendChild(body);
    document.body.appendChild(tipEl);

    const r = anchor.getBoundingClientRect();
    const pad = 10;
    const left = Math.max(pad, Math.min(window.innerWidth - pad, r.left + r.width / 2));
    const top = Math.max(pad, r.bottom + 8);
    tipEl.style.left = `${left}px`;
    tipEl.style.top = `${top}px`;
    tipEl.style.transform = "translateX(-50%)";
  };

  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (!(t && t.closest)) return;
    if (t.closest(".taTipPop")) return;
    const info = t.closest(".info");
    if (!info) return;
    const title = info.getAttribute("title") || info.getAttribute("data-tip") || "";
    if (!title) return;
    ev.preventDefault();
    ev.stopPropagation();
    if (lastAnchor === info && tipEl) closeTip();
    else openTip(info, title);
  });

  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (t && t.closest && (t.closest(".taTipPop") || t.closest(".info"))) return;
    closeTip();
  });

  document.addEventListener("scroll", closeTip, { passive: true });
  window.addEventListener("resize", closeTip);
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeTip(); });
}

// ---------------------------------------------------------------------------
// Engine tab title helper.
//
// Desk often has 6-10 engine tabs open simultaneously during the trading day —
// without a ticker hint, they all collapse to "Engine 1…" / "Engine 15…" in
// Chrome's tab strip and become indistinguishable. This helper rewrites
// `<title>` to `E{N} · {TICKER} — {Short Name}` (e.g. `E15 · GOOGL — Earnings
// IC Scenario`) so the engine code + ticker stay visible even when the tab is
// truncated to ~10-15 chars.
//
// Original title pattern expected: `Engine N — Short Name | NRGX Labs`
// (Was `| Raven-Tech.co` pre-rebrand; the regex below ignores the suffix.)
// If the page's title doesn't match, this is a silent no-op (so pages that
// manage their own dynamic title — e.g. SPX page rotating SPX/SPY/QQQ — are
// not stomped on).
// ---------------------------------------------------------------------------

let _ravenOriginalTitle = null;
let _ravenTitleParts = null; // { engineNum, shortName } | null (if pattern didn't match)

function _parseEngineTitle(title) {
  // Match "Engine 15 — Earnings IC Scenario | NRGX Labs"
  // (Both old `Raven-Tech.co` and new `NRGX Labs` suffixes are accepted —
  // the trailing `(?:\s*\|.*)?$` swallows any brand suffix.)
  // Em-dash (U+2014) is the standard separator across the app's titles.
  const m = String(title || "").match(/^Engine\s+(\d+)\s+[—–-]\s+([^|]+?)(?:\s*\|.*)?$/);
  if (!m) return null;
  return { engineNum: m[1], shortName: m[2].trim() };
}

function setEngineTabTitle(ticker) {
  if (_ravenOriginalTitle == null) {
    _ravenOriginalTitle = document.title;
    _ravenTitleParts = _parseEngineTitle(_ravenOriginalTitle);
  }
  if (!_ravenTitleParts) return; // page doesn't follow the engine title pattern
  const t = String(ticker || "").trim().toUpperCase();
  const { engineNum, shortName } = _ravenTitleParts;
  if (t) {
    document.title = `E${engineNum} · ${t} — ${shortName}`;
  } else {
    document.title = _ravenOriginalTitle;
  }
}

// Expose a single global namespace
window.RavenUI = {
  $,
  escapeHtml,
  fmtPct,
  fmt0,
  fmt2,
  setText,
  setHtml,
  clamp,
  fetchJson,
  copyToClipboard,
  initTooltips,
  initInfoTips,
  installGlobalApiLoading,
  setEngineTabTitle,
};

// ---------------------------------------------------------------------------
// Global API loading bar (desk usability): shows a subtle top progress bar for
// in-flight `/api/*` calls. Centralized via fetch wrapper so all pages benefit.
// ---------------------------------------------------------------------------

function installGlobalApiLoading({
  apiPrefix = "/api/",
  showDelayMs = 150,
} = {}) {
  // Idempotent install.
  if (window.__RavenApiLoadingInstalled) return;
  window.__RavenApiLoadingInstalled = true;

  const state = {
    inflight: 0,
    showTimer: null,
    lastClickEl: null,
  };

  const ensureBar = () => {
    if (document.getElementById("ravenTopLoader")) return;
    if (!document.body) return;
    const bar = document.createElement("div");
    bar.id = "ravenTopLoader";
    bar.setAttribute("aria-hidden", "true");
    document.body.appendChild(bar);
  };

  const isApiUrl = (input) => {
    try {
      if (!input) return false;
      // input can be Request | string | URL
      const raw = (typeof input === "string") ? input : (input?.url || String(input));
      const s = String(raw || "");
      if (s.startsWith(apiPrefix)) return true;
      if (s.startsWith("http://") || s.startsWith("https://")) {
        const u = new URL(s, window.location.href);
        // Only consider same-origin API calls.
        if (u.origin !== window.location.origin) return false;
        return u.pathname.startsWith(apiPrefix);
      }
      return false;
    } catch {
      return false;
    }
  };

  const setTempDisabled = (el, on) => {
    if (!el || !el.classList) return;
    const tag = String(el.tagName || "").toUpperCase();
    if (tag === "BUTTON") {
      if (on) {
        if (!el.hasAttribute("data-raven-prev-disabled")) {
          el.setAttribute("data-raven-prev-disabled", el.disabled ? "1" : "0");
        }
        el.disabled = true;
      } else {
        const prev = el.getAttribute("data-raven-prev-disabled");
        if (prev !== null) {
          el.disabled = prev === "1";
          el.removeAttribute("data-raven-prev-disabled");
        } else {
          // If we didn't record, leave as-is.
        }
      }
      return;
    }
    if (tag === "A") {
      if (on) {
        el.classList.add("ravenTempDisabled");
        el.setAttribute("aria-disabled", "true");
      } else {
        el.classList.remove("ravenTempDisabled");
        el.removeAttribute("aria-disabled");
      }
    }
  };

  const show = () => {
    ensureBar();
    document.documentElement.classList.add("isApiLoading");
    setTempDisabled(state.lastClickEl, true);
  };

  const hide = () => {
    document.documentElement.classList.remove("isApiLoading");
    setTempDisabled(state.lastClickEl, false);
  };

  const inc = () => {
    state.inflight += 1;
    if (state.inflight === 1) {
      // Avoid flicker for fast calls.
      if (state.showTimer) clearTimeout(state.showTimer);
      state.showTimer = setTimeout(() => {
        state.showTimer = null;
        if (state.inflight > 0) show();
      }, Math.max(0, Number(showDelayMs) || 0));
    }
  };

  const dec = () => {
    state.inflight = Math.max(0, state.inflight - 1);
    if (state.inflight === 0) {
      if (state.showTimer) {
        clearTimeout(state.showTimer);
        state.showTimer = null;
      }
      hide();
    }
  };

  // Track the most recently clicked button/link so we can temporarily disable it
  // while API calls are running (prevents spam-clicking).
  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (!(t && t.closest)) return;
    const el = t.closest("button, a");
    if (!el) return;
    state.lastClickEl = el;
  }, { capture: true });

  // Wrap fetch once and count `/api/*` in-flight calls.
  if (!window.__RavenFetchWrapped && typeof window.fetch === "function") {
    window.__RavenFetchWrapped = true;
    const origFetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const track = isApiUrl(input);
      if (track) inc();
      try {
        return await origFetch(input, init);
      } finally {
        if (track) dec();
      }
    };
  }

  // Ensure bar exists once body is available.
  if (document.body) ensureBar();
  else document.addEventListener("DOMContentLoaded", ensureBar, { once: true });
}

// Auto-install so all pages get consistent loading UX.
try { installGlobalApiLoading(); } catch { /* ignore */ }

// ---------------------------------------------------------------------------
// Nav Button Tooltips: Fixed positioning to escape header stacking context
// ---------------------------------------------------------------------------

(function initNavTooltips() {
  let tooltip = null;
  let currentBtn = null;
  let hideTimeout = null;
  
  function create() {
    if (tooltip) return tooltip;
    tooltip = document.createElement("div");
    tooltip.className = "navTooltip";
    tooltip.setAttribute("role", "tooltip");
    document.body.appendChild(tooltip);
    return tooltip;
  }
  
  function show(btn) {
    if (!btn) return;
    const text = btn.getAttribute("data-tooltip");
    if (!text) return;
    
    create();
    currentBtn = btn;
    tooltip.textContent = text;
    
    // Clear any pending hide
    if (hideTimeout) {
      clearTimeout(hideTimeout);
      hideTimeout = null;
    }
    
    // Position below button, centered
    const rect = btn.getBoundingClientRect();
    const tooltipWidth = Math.min(340, Math.max(280, text.length * 7));
    
    let left = rect.left + rect.width / 2 - tooltipWidth / 2;
    // Keep within viewport
    left = Math.max(10, Math.min(left, window.innerWidth - tooltipWidth - 10));
    
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${rect.bottom + 8}px`;
    tooltip.style.width = `${tooltipWidth}px`;
    
    // Adjust arrow position
    const arrowOffset = rect.left + rect.width / 2 - left;
    tooltip.style.setProperty("--arrow-left", `${arrowOffset}px`);
    
    // Show with animation
    requestAnimationFrame(() => {
      tooltip.classList.add("isVisible");
    });
  }
  
  function hide() {
    if (!tooltip) return;
    tooltip.classList.remove("isVisible");
    currentBtn = null;
  }
  
  // Delegate hover events
  document.addEventListener("mouseenter", (e) => {
    const btn = e.target.closest?.(".navBtn[data-tooltip]");
    if (btn) show(btn);
  }, true);
  
  document.addEventListener("mouseleave", (e) => {
    const btn = e.target.closest?.(".navBtn[data-tooltip]");
    if (btn && btn === currentBtn) {
      // Small delay to prevent flicker
      hideTimeout = setTimeout(hide, 50);
    }
  }, true);
})();

// ---------------------------------------------------------------------------
// Raven Loading Overlay: Full-screen loading with spinning logo and progress
// ---------------------------------------------------------------------------

window.RavenLoading = (function() {
  "use strict";

  let overlay = null;
  let progressFill = null;
  let statusEl = null;
  let isVisible = false;
  
  // Auto-progress state
  let autoProgressInterval = null;
  let currentProgress = 0;
  let startTime = 0;
  
  // Default expected load time in milliseconds (45 seconds)
  let expectedLoadMs = 45000;
  // Progress ceiling before completion (don't go past 92% until actually done)
  const PROGRESS_CEILING = 92;
  // Update interval in ms
  const UPDATE_INTERVAL = 250;

  function create() {
    if (overlay) return overlay;

    overlay = document.createElement("div");
    overlay.className = "ravenLoadingOverlay";
    overlay.setAttribute("role", "progressbar");
    overlay.setAttribute("aria-valuemin", "0");
    overlay.setAttribute("aria-valuemax", "100");
    overlay.innerHTML = `
      <img src="/static/NRGX-Mark.png" class="ravenLoadingLogo" alt="" aria-hidden="true" />
      <div class="ravenLoadingProgress">
        <div class="ravenLoadingProgressFill"></div>
      </div>
      <div class="ravenLoadingStatus">Loading...</div>
    `;
    document.body.appendChild(overlay);

    progressFill = overlay.querySelector(".ravenLoadingProgressFill");
    statusEl = overlay.querySelector(".ravenLoadingStatus");

    return overlay;
  }
  
  /**
   * Stop auto-progress animation
   */
  function stopAutoProgress() {
    if (autoProgressInterval) {
      clearInterval(autoProgressInterval);
      autoProgressInterval = null;
    }
  }
  
  /**
   * Start auto-progress animation
   * Linear progress over expected time, capped at 92%
   */
  function startAutoProgress() {
    stopAutoProgress();
    currentProgress = 0;
    startTime = Date.now();
    
    autoProgressInterval = setInterval(() => {
      if (!isVisible) {
        stopAutoProgress();
        return;
      }
      
      // Calculate linear progress based on elapsed time
      const elapsed = Date.now() - startTime;
      const linearProgress = (elapsed / expectedLoadMs) * PROGRESS_CEILING;
      
      // Cap at ceiling - if load takes longer than expected, stay at 92%
      currentProgress = Math.min(PROGRESS_CEILING, linearProgress);
      
      if (progressFill) {
        progressFill.style.width = `${currentProgress}%`;
      }
      
      overlay.setAttribute("aria-valuenow", String(Math.round(currentProgress)));
      
      // Stop interval once we hit ceiling (will complete when hide() is called)
      if (currentProgress >= PROGRESS_CEILING) {
        stopAutoProgress();
      }
    }, UPDATE_INTERVAL);
  }

  /**
   * Show the loading overlay
   * @param {Object} options
   * @param {string} options.status - Initial status message
   * @param {boolean} options.clearResults - Whether to clear #results content (default: true)
   * @param {boolean} options.autoProgress - Enable auto-progress animation (default: true)
   * @param {number} options.expectedLoadMs - Expected load time in ms (default: 45000)
   */
  function show(options = {}) {
    create();
    
    // Set expected load time if provided
    if (options.expectedLoadMs && options.expectedLoadMs > 0) {
      expectedLoadMs = options.expectedLoadMs;
    }

    // Clear previous results if specified (default: true)
    if (options.clearResults !== false) {
      const resultsEl = document.getElementById("results");
      if (resultsEl) {
        resultsEl.classList.add("hidden");
        // Clear grid content to prevent flash of old data on next show
        // Exclude statsGrid which has static structure with dynamic values
        const grids = resultsEl.querySelectorAll("[id$='Grid']:not(#statsGrid), [id$='Summary'], [id$='List']");
        grids.forEach(g => { g.innerHTML = ""; });
      }
    }

    // Reset progress
    currentProgress = 0;
    startTime = Date.now();
    if (progressFill) {
      progressFill.style.transition = "none";
      progressFill.style.width = "0%";
      // Force reflow then restore transition
      void progressFill.offsetWidth;
      progressFill.style.transition = "width 0.25s linear";
    }

    // Set initial status
    if (statusEl) {
      statusEl.textContent = options.status || "Loading...";
    }

    // Update ARIA
    overlay.setAttribute("aria-valuenow", "0");

    // Show overlay
    isVisible = true;
    overlay.classList.add("isVisible");
    
    // Start auto-progress (default: true)
    if (options.autoProgress !== false) {
      startAutoProgress();
    }
  }

  /**
   * Update status message (progress bar auto-animates based on time)
   * @param {number} percent - Ignored (kept for API compatibility)
   * @param {string} status - Status message to display
   */
  function setProgress(percent, status) {
    if (!overlay) return;

    // Only update status text - progress bar animates automatically
    if (status && statusEl) {
      statusEl.textContent = status;
    }
  }

  /**
   * Hide the loading overlay
   */
  function hide() {
    if (!overlay || !isVisible) return;
    
    // Stop auto-progress
    stopAutoProgress();

    // Set to 100% first for completion feedback
    currentProgress = 100;
    if (progressFill) {
      progressFill.style.width = "100%";
    }

    // Delay hide slightly so user sees completion
    setTimeout(() => {
      isVisible = false;
      overlay.classList.remove("isVisible");
    }, 200);
  }

  /**
   * Check if overlay is currently visible
   */
  function visible() {
    return isVisible;
  }

  return {
    show,
    hide,
    setProgress,
    isVisible: visible,
  };
})();

/**
 * Make a popup element draggable by its header.
 * Supports mouse + touch, constrains to viewport, adds/removes .isDragging.
 *
 * @param {HTMLElement} popupEl   – the popup container to reposition
 * @param {HTMLElement} headerEl  – the drag handle (usually the popup header bar)
 * @param {object}      [opts]
 * @param {string}      [opts.closeSelector]  – CSS selector; clicks on this skip drag
 * @param {boolean}     [opts.constrain=true] – keep popup within viewport
 */
function initDrag(popupEl, headerEl, opts) {
  if (!popupEl || !headerEl) return;
  const o = Object.assign({ constrain: true }, opts);
  let dragging = false, offsetX = 0, offsetY = 0;

  function pointer(e) {
    const t = e.touches ? e.touches[0] : e;
    return { x: t.clientX, y: t.clientY };
  }

  function onDown(e) {
    if (o.closeSelector && e.target.closest(o.closeSelector)) return;
    dragging = true;
    popupEl.classList.add("isDragging");
    const p = pointer(e);
    const r = popupEl.getBoundingClientRect();
    offsetX = p.x - r.left;
    offsetY = p.y - r.top;
    e.preventDefault();
  }

  function onMove(e) {
    if (!dragging) return;
    const p = pointer(e);
    let x = p.x - offsetX;
    let y = p.y - offsetY;
    if (o.constrain) {
      x = Math.max(0, Math.min(x, window.innerWidth - popupEl.offsetWidth));
      y = Math.max(0, Math.min(y, window.innerHeight - popupEl.offsetHeight));
    }
    popupEl.style.left = x + "px";
    popupEl.style.top = y + "px";
    popupEl.style.right = "auto";
    popupEl.style.bottom = "auto";
  }

  function onUp() {
    if (!dragging) return;
    dragging = false;
    popupEl.classList.remove("isDragging");
  }

  headerEl.addEventListener("mousedown", onDown);
  headerEl.addEventListener("touchstart", onDown, { passive: false });
  document.addEventListener("mousemove", onMove);
  document.addEventListener("touchmove", onMove, { passive: false });
  document.addEventListener("mouseup", onUp);
  document.addEventListener("touchend", onUp);
}

/**
 * Reusable card-insight popup (Pattern A) used by Engines 1-5.
 *
 * @param {object} opts
 * @param {HTMLElement} opts.popupEl   – popup container
 * @param {HTMLElement} opts.titleEl   – title text element
 * @param {HTMLElement} opts.bodyEl    – body content element
 * @param {string}      opts.prefix    – CSS class prefix (e.g. "e1Insight")
 * @param {object}      [opts.labels]  – key → display-label map
 * @param {boolean}     [opts.renderMeta] – render _meta block (Engine 5 style)
 */
function InsightPopup(opts) {
  this.popupEl = opts.popupEl;
  this.titleEl = opts.titleEl;
  this.bodyEl  = opts.bodyEl;
  this.pfx     = opts.prefix;
  this.labels  = opts.labels || {};
  this.meta    = !!opts.renderMeta;
  this._cache  = {};
  this._esc    = typeof escapeHtml === "function" ? escapeHtml : function (s) { return String(s ?? ""); };
  this.onResult = opts.onResult || null;
}

InsightPopup.prototype.open = function (title, x, y) {
  this.titleEl.textContent = title;
  this.bodyEl.innerHTML =
    "<div class='" + this.pfx + "Loading'><span class='" + this.pfx + "Dot'></span>" +
    "<span class='" + this.pfx + "Dot'></span><span class='" + this.pfx + "Dot'></span>" +
    "<br>Generating desk insight\u2026</div>";
  this.popupEl.style.left = Math.min(x, window.innerWidth - 460) + "px";
  this.popupEl.style.top  = Math.min(y, window.innerHeight - 300) + "px";
  this.popupEl.style.display = "block";
};

InsightPopup.prototype.render = function (data) {
  var esc = this._esc;
  if (!data) { this.bodyEl.innerHTML = "<div class='" + this.pfx + "Loading'>No insight data.</div>"; return; }
  var html = "";
  if (data._fallback_reason) {
    html += "<div style='background:rgba(255,107,107,.15);border:1px solid rgba(255,107,107,.3);border-radius:8px;padding:10px 12px;margin-bottom:14px;font-size:11px;color:#ff6b6b;'>" +
      esc(data._fallback_reason) + "</div>";
  }
  if (this.meta && data._meta) {
    html += "<div class='" + this.pfx + "Meta'>";
    for (var mk in data._meta) {
      html += "<div class='" + this.pfx + "MetaItem'><div style='font-size:10px;text-transform:uppercase;letter-spacing:0.5px;color:rgba(255,255,255,0.45);'>" +
        esc(mk.replace(/_/g, " ")) + "</div><div class='" + this.pfx + "MetaValue'>" + esc(String(data._meta[mk])) + "</div></div>";
    }
    html += "</div>";
  }
  var skip = new Set(["_source", "_meta", "_card_type", "_fallback_reason"]);
  for (var key in data) {
    if (skip.has(key)) continue;
    var label = this.labels[key] || key.replace(/_/g, " ").replace(/\b\w/g, function (c) { return c.toUpperCase(); });
    var isDesk = key === "desk_takeaway" || key === "the_call";
    html += "<div class='" + this.pfx + "Section'><div class='" + this.pfx + "SectionTitle'>" + esc(label) +
      "</div><div class='" + this.pfx + "Text'" + (isDesk ? " style='color:#34c759;font-weight:600;'" : "") +
      ">" + esc(String(data[key])) + "</div></div>";
  }
  if (data._source) html += "<div class='" + this.pfx + "Source'>Source: " + esc(data._source) + "</div>";
  this.bodyEl.innerHTML = html;
};

InsightPopup.prototype.fetch = function (cardType, cardData, title, x, y, ctx) {
  var self = this;
  var cacheKey = cardType + ":" + JSON.stringify(cardData).substring(0, 100);
  if (self._cache[cacheKey]) { self.open(title, x, y); self.render(self._cache[cacheKey]); return; }
  self.open(title, x, y);
  var esc = self._esc;

  window.fetch("/api/front-layer/card-insight", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ card_type: cardType, card_data: cardData, dms_summary: ctx || {} }),
  })
  .then(function (r) { return r.json(); })
  .then(function (resp) {
    if (resp.error || resp.detail) {
      self.bodyEl.innerHTML = "<div class='" + self.pfx + "Loading' style='color:#ff6b6b;'>Error: " + esc(resp.error || resp.detail || "Unknown") + "</div>";
      return;
    }
    self._cache[cacheKey] = resp;
    self.render(resp);
    if (typeof self.onResult === "function") self.onResult(cardType, resp);
  })
  .catch(function () {
    self.bodyEl.innerHTML = "<div class='" + self.pfx + "Loading' style='color:#ff6b6b;'>Failed to load insight.</div>";
  });
};

InsightPopup.prototype.clearCache = function () { this._cache = {}; };

