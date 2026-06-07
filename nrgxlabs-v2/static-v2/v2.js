/* NRGX Labs v2 — minimal client glue.
 *
 * Phase 0:
 *   - Animate brain progress bars + sync the pct chip
 *   - Pull /api/v2/version for the regime chip + section tag
 *   - Wire the mobile drawer (hamburger + overlay)
 *   - Pull /api/v2/counterfactual/recent for the live ticker
 *
 * No bundlers, no frameworks. Stays this lean until we genuinely need more. */

(function () {
  "use strict";

  function $(sel, root)  { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

  // ── Animate brain bars + populate the % chip ──────────────
  // Each tile may declare data-progress (hardcoded baseline) and/or
  // data-live-source (an API path to query for a live "n / target" reading).
  // Tiles with data-live-source override the baseline once the API responds.
  function animateBrainBars() {
    $$(".v2BrainBarFill").forEach(function (el) {
      var pct = parseFloat(el.dataset.progress || "0");
      var clamped = Math.max(14, Math.min(100, pct));
      requestAnimationFrame(function () { el.style.width = clamped + "%"; });
    });
    $$(".v2BrainBarPct").forEach(function (el) {
      var n = parseFloat(el.dataset.pctOf || "0");
      el.textContent = (Math.round(n)) + "%";
    });
  }

  // Updates a tile's bar + chip + caption with a live measurement.
  function updateBrainTile(moduleId, pct, captionText) {
    var tile = document.querySelector('[data-brain-module="' + moduleId + '"]');
    if (!tile) return;
    var bar = tile.querySelector(".v2BrainBarFill");
    var chip = tile.querySelector(".v2BrainBarPct");
    var caption = tile.querySelector(".v2BrainTileLive");
    var clamped = Math.max(14, Math.min(100, pct));
    if (bar) bar.style.width = clamped + "%";
    if (chip) chip.textContent = Math.round(pct) + "%";
    if (caption && captionText) caption.textContent = captionText;
    tile.classList.add("is-live");
  }

  // Pull /api/v2/conformal/list and update the conformal tile with the
  // sum of n_calibration across all (engine, metric) calibrators.
  // Target window for "100% online" is 200 observations — enough for
  // tight intervals at α=0.1 (Lei et al. recommend n≥100, we want headroom).
  async function refreshConformalTile() {
    var TARGET_N = 200;
    try {
      var res = await fetch("/api/v2/conformal/list", { credentials: "include" });
      if (!res.ok) return;
      var data = await res.json();
      var cals = data.calibrators || [];
      var total = cals.reduce(function (acc, c) { return acc + (c.n || 0); }, 0);
      var pct = Math.min(100, (total / TARGET_N) * 100);
      var caption;
      if (cals.length === 0) {
        caption = "no calibrators yet — POST /api/v2/conformal/mirror to backfill";
      } else {
        caption = total + " observations · " + cals.length + " calibrator" + (cals.length === 1 ? "" : "s");
      }
      updateBrainTile("conformal_calibrator", pct, caption);
    } catch (err) { /* ignore — keep baseline */ }
  }

  // Pull /api/v2/analogues/stats and update the analogue tile with a
  // composite reading: indexed-trade count vs target + ticker diversity.
  // Target index size for "100% online" is 1000 trades.
  async function refreshAnalogueTile() {
    var TARGET_N = 1000;
    try {
      var res = await fetch("/api/v2/analogues/stats", { credentials: "include" });
      if (!res.ok) return;
      var data = await res.json();
      var indexes = data.indexes || [];
      var total = indexes.reduce(function (acc, i) { return acc + (i.n_indexed || 0); }, 0);
      var tickers = new Set();
      indexes.forEach(function (i) {
        (i.tickers || []).forEach(function (t) { tickers.add(t.ticker); });
      });
      var pct = Math.min(100, (total / TARGET_N) * 100);
      var caption;
      if (indexes.length === 0) {
        caption = "no index built — POST /api/v2/analogues/build to index v1 trades";
      } else {
        caption = total + " trades indexed · " + tickers.size + " tickers · " + indexes.length + " engine" + (indexes.length === 1 ? "" : "s");
      }
      updateBrainTile("contrastive_analogues", pct, caption);
    } catch (err) { /* ignore — keep baseline */ }
  }

  async function refreshPathTile() {
    var TARGET_N = 500;  // 500 returns aggregated across corpora to fill bar
    try {
      var res = await fetch("/api/v2/paths/stats", { credentials: "include" });
      if (!res.ok) return;
      var data = await res.json();
      var tickers = (data && data.tickers) || [];
      var n = (data && data.n_total) || 0;
      var pct = Math.min(100, (n / TARGET_N) * 100);
      var caption;
      if (n === 0) {
        caption = "no return corpus — POST /api/v2/paths/corpus/save with daily log-returns";
      } else {
        var t = tickers.length;
        caption = n + " returns indexed · " + t + " ticker" + (t === 1 ? "" : "s");
      }
      updateBrainTile("path_generator", pct, caption);
    } catch (err) { /* ignore — keep baseline */ }
  }

  async function refreshCommitteeTile() {
    var TARGET_N = 25;  // 25 deliberations to fill the bar
    try {
      var res = await fetch("/api/v2/committee/recent?n=100", { credentials: "include" });
      if (!res.ok) return;
      var data = await res.json();
      var entries = (data && data.entries) || [];
      var n = entries.length;
      var counts = { approve: 0, monitor: 0, decline: 0 };
      entries.forEach(function (e) {
        if (counts.hasOwnProperty(e.decision)) counts[e.decision] += 1;
      });
      var pct = Math.min(100, (n / TARGET_N) * 100);
      var caption;
      if (n === 0) {
        caption = "no deliberations yet — POST /api/v2/committee/deliberate with a setup";
      } else {
        caption = n + " deliberation" + (n === 1 ? "" : "s") + " · "
                + counts.approve + " approve · "
                + counts.monitor + " monitor · "
                + counts.decline + " decline";
      }
      updateBrainTile("agent_committee", pct, caption);
    } catch (err) { /* ignore — keep baseline */ }
  }

  async function refreshRegimeTile() {
    var TARGET_N = 90;  // 90 days = ~one quarter of DMS snapshots
    try {
      var res = await fetch("/api/v2/regime/stats", { credentials: "include" });
      if (!res.ok) return;
      var data = await res.json();
      var n = data.n_indexed || 0;
      var pct = Math.min(100, (n / TARGET_N) * 100);
      var caption;
      if (n === 0) {
        caption = "no index built — POST /api/v2/regime/build to ingest v1 DMS history";
      } else {
        var dist = data.label_distribution || {};
        var parts = [];
        ["Risk-On", "Transitional", "Risk-Off", "Stressed"].forEach(function (k) {
          if (dist[k]) parts.push(dist[k] + " " + k);
        });
        caption = n + " days indexed · " + (parts.length ? parts.join(" · ") : "no labels yet");
      }
      updateBrainTile("regime_encoder", pct, caption);
    } catch (err) { /* ignore — keep baseline */ }
  }

  // ── Mobile drawer ─────────────────────────────────────────
  function wireDrawer() {
    var btn  = $("#v2MenuToggle");
    var ovl  = $("#v2NavOverlay");
    var side = $("#v2Sidebar");
    if (!btn) return;

    function open() {
      document.body.classList.add("v2NavOpen");
      btn.setAttribute("aria-expanded", "true");
      if (ovl) ovl.setAttribute("aria-hidden", "false");
    }
    function close() {
      document.body.classList.remove("v2NavOpen");
      btn.setAttribute("aria-expanded", "false");
      if (ovl) ovl.setAttribute("aria-hidden", "true");
    }
    function toggle() {
      if (document.body.classList.contains("v2NavOpen")) close(); else open();
    }

    btn.addEventListener("click", toggle);
    if (ovl) ovl.addEventListener("click", close);

    // Tapping a nav link inside the drawer should close it on mobile.
    if (side) {
      side.addEventListener("click", function (ev) {
        var link = ev.target.closest("a");
        if (link) close();
      });
    }

    // Esc closes the drawer.
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && document.body.classList.contains("v2NavOpen")) close();
    });

    // If the viewport grows back past mobile, ensure we close the drawer
    // so the persistent sidebar layout doesn't end up with stale state.
    var mq = window.matchMedia("(min-width: 901px)");
    if (mq.addEventListener) mq.addEventListener("change", function (e) { if (e.matches) close(); });
  }

  // ── /api/v2/version → regime chip + section tag ───────────
  async function loadVersion() {
    var chip = $("#v2RegimeChipText");
    var tag  = $("#v2BrainTag");
    try {
      var res = await fetch("/api/v2/version", { credentials: "include" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      var data = await res.json();
      var v = data.version || "";
      var enabled = Object.values(data.foundation || {}).filter(Boolean).length;
      var total   = Object.keys(data.foundation || {}).length || 6;
      if (chip) chip.textContent = "v2." + v + " · brain " + enabled + "/" + total;
      if (tag)  tag.textContent  = "layer 1 · " + enabled + "/" + total + " modules online";
    } catch (err) {
      if (chip) chip.textContent = "v2 · standalone";
      if (tag)  tag.textContent  = "layer 1 · phase 0";
    }
  }

  // ── /api/v2/counterfactual/recent → live ticker ───────────
  async function loadTicker() {
    var inner = $(".v2TickerInner");
    if (!inner) return;
    try {
      var res = await fetch("/api/v2/counterfactual/recent?n=24", { credentials: "include" });
      if (!res.ok) return;
      var data = await res.json();
      var entries = (data && data.entries) || [];
      if (!entries.length) return;

      // Replace the placeholder content with real entries; duplicate the
      // list so the marquee animation loops seamlessly.
      var rendered = entries.map(renderEntry).join("");
      inner.innerHTML = rendered + rendered;
    } catch (err) { /* keep placeholder */ }
  }

  function renderEntry(e) {
    var ts = (e.ts || "").slice(11, 16) || "--:--";
    var engine = (e.engine || "?").toUpperCase();
    var agree = !!e.agree;
    var kindCls = agree ? "v2TickerKind v2TickerKind--em" : "v2TickerKind v2TickerKind--mag";
    var verdict = agree ? "agree" : "DISAGREE";
    var note = e.delta_summary
      ? ' <span class="v2TickerNote">"' + escapeHtml(e.delta_summary).slice(0, 64) + '"</span>'
      : "";
    return ''
      + '<span class="v2TickerItem">'
      +   '<span class="' + kindCls + '">[' + ts + ']</span> '
      +   '<span class="v2TickerEng">' + escapeHtml(engine) + '</span> '
      +   verdict
      +   note
      + '</span>';
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ── Streaming caret on the hero (subtle, ambient) ─────────
  function ambientStream() {
    var title = $(".v2HeroTitle em");
    if (!title || title.dataset.streamed === "1") return;
    title.dataset.streamed = "1";
    var caret = document.createElement("span");
    caret.className = "v2StreamCaret";
    caret.setAttribute("aria-hidden", "true");
    title.parentNode.appendChild(caret);
  }

  document.addEventListener("DOMContentLoaded", function () {
    animateBrainBars();
    wireDrawer();
    loadVersion();
    loadTicker();
    refreshConformalTile();
    refreshAnalogueTile();
    refreshRegimeTile();
    refreshCommitteeTile();
    refreshPathTile();
    ambientStream();
  });
})();
