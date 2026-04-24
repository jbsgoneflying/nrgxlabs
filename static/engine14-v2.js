/* =============================================================
   Engine 14 v2 — IC Scenario Command Deck (additive layer)

   Loads on top of ic-scenario.js (the legacy scenario form
   stays wired; nothing it does is disrupted). Adds:

   - A ranked Wing Decision Console card above the form, calling
     POST /api/ic-scenario/wing-console and painting a 12-row
     placements table + EM/wing slider tuner.
   - "Simulate This Pick" handoff that pre-fills the scenario
     form and triggers its submit so the existing drilldown
     flow runs against the clicked placement.
   - MI v2 Regime, MC Reading, and MAE Pool drilldown cards
     populated from the /wing-console response.
   - An always-on E14-native advisor button post-scenario that
     posts to /api/ic-scenario/advisor and paints the narrative.

   This file is loaded AFTER ic-scenario.js so it can piggyback
   on the existing escapeHtml helpers via a minimal inline copy.
   ============================================================= */

(function () {
  "use strict";

  const State = {
    entryDate:   "",
    expiryDate:  "",
    asOfDate:    "",
    lastDeck:    null,
    selectedIndex: 0,
  };

  const SOURCE_LABELS = {
    desk_default:  "Desk default",
    user_override: "Override",
    unknown:       "",
  };

  // ---------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtPct(x, digits) {
    if (x === null || x === undefined) return "—";
    var n = Number(x);
    if (!Number.isFinite(n)) return "—";
    return n.toFixed(digits == null ? 1 : digits) + "%";
  }

  function todayIso() {
    var d = new Date();
    return d.toISOString().slice(0, 10);
  }

  function addBusinessDays(iso, n) {
    var d = new Date(iso + "T00:00:00");
    var added = 0;
    while (added < n) {
      d.setDate(d.getDate() + 1);
      var day = d.getDay();
      if (day !== 0 && day !== 6) added++;
    }
    return d.toISOString().slice(0, 10);
  }

  function setSourceChip(source) {
    var chip = $("e14EventSourceChip");
    if (!chip) return;
    var s = String(source || "unknown").toLowerCase();
    var cls = "e1SourceChip--unknown";
    if (s === "user_override") cls = "e1SourceChip--user_override";
    else if (s === "desk_default") cls = "e1SourceChip--orats_cores";
    chip.className = "e1SourceChip " + cls;
    var label = SOURCE_LABELS[s] || "";
    chip.textContent = label;
    chip.title = label ? ("Scenario source: " + label.toLowerCase()) : "";
  }

  function scoreColor(score) {
    if (score >= 75) return "e14MetricGood";
    if (score >= 55) return "e14MetricMed";
    return "e14MetricRisky";
  }

  // ---------------------------------------------------------------
  // Wing Console fetcher + painter
  // ---------------------------------------------------------------

  async function fetchAndPaintWingConsole() {
    var host = $("e14WingConsole");
    if (!host) return;
    host.innerHTML = '<div class="e14ConsoleWarnings">Scoring wing placements…</div>';

    // v2: Command Deck pattern — read the single unified form at the
    // top of the page. Falls back to today+4biz days only if the desk
    // somehow triggered this path without filling the form (shouldn't
    // happen since Calculate is gated on required fields).
    var entryEl  = $("entryDate");
    var expiryEl = $("expiry");
    var entryDate  = (entryEl  && entryEl.value)  || todayIso();
    var expiryDate = (expiryEl && expiryEl.value) || addBusinessDays(entryDate, 4);

    if (entryEl  && !entryEl.value)  entryEl.value  = entryDate;
    if (expiryEl && !expiryEl.value) expiryEl.value = expiryDate;

    State.entryDate  = entryDate;
    State.expiryDate = expiryDate;
    State.asOfDate   = todayIso();

    setSourceChip("desk_default");

    try {
      var resp = await fetch("/api/ic-scenario/wing-console", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          underlying:  "SPX",
          entry_date:  entryDate,
          expiry_date: expiryDate,
        }),
      });
      if (!resp.ok) {
        var txt = await resp.text();
        throw new Error("wing-console " + resp.status + ": " + txt.slice(0, 300));
      }
      var deck = await resp.json();
      State.lastDeck = deck;
      paintWingConsole(host, deck);
      paintMiV2(deck);
      paintMcReading(deck);
      paintMaeDistribution(deck);
    } catch (err) {
      host.innerHTML = '<div class="e14ConsoleWarnings">Wing Console unavailable: ' +
        esc(err && err.message ? err.message : String(err)) + "</div>";
    }
  }

  function paintWingConsole(host, deck) {
    var wc = deck && deck.wingConsole;
    if (!wc || !Array.isArray(wc.placements) || !wc.placements.length) {
      host.innerHTML = '<div class="e14ConsoleWarnings">' +
        esc(((wc && wc.warnings) || ["No placements returned."]).join(" · ")) +
        "</div>";
      return;
    }
    var top = wc.placements.slice(0, 8);
    var subtitle = [
      "Entry <strong>" + esc(wc.entry_date) + "</strong>",
      "Expiry <strong>" + esc(wc.expiry_date) + "</strong>",
      "Spot <strong>" + (wc.spot != null ? Number(wc.spot).toFixed(2) : "—") + "</strong>",
      "1σ EM <strong>" + fmtPct(wc.em_pct, 2) + "</strong>",
      "Regime <strong>" + esc(wc.regime_label || "—") + "</strong>" +
        (wc.regime_mi_v2 && wc.regime_mi_v2.label
          ? " · HMM <strong>" + esc(wc.regime_mi_v2.label) + "</strong>" : ""),
      "MC <strong>" + ((deck.mcResults && deck.mcResults.n_sims) || 0) + "</strong> sims" +
        (deck.mcResults && deck.mcResults.conditioning_used
          ? " · " + esc(deck.mcResults.conditioning_used) : ""),
    ].map(function (s) { return "<span>" + s + "</span>"; }).join("");

    var warningsHtml = Array.isArray(wc.warnings) && wc.warnings.length
      ? '<div class="e14ConsoleWarnings">' + wc.warnings.map(esc).join(" · ") + "</div>"
      : "";

    var rows = top.map(function (p, i) {
      var scoreClass = scoreColor(Number(p.composite_score || 0));
      var maeClass   = (p.mae_p95_vs_wing || 0) >= 0.9 ? "e14MetricRisky"
                     : (p.mae_p95_vs_wing || 0) >= 0.5 ? "e14MetricMed" : "e14MetricGood";
      var breachClass = (p.breach_close_prob || 0) >= 0.2 ? "e14MetricRisky"
                      : (p.breach_close_prob || 0) >= 0.1 ? "e14MetricMed" : "e14MetricGood";
      var touchClass  = (p.touch_intraweek_prob || 0) >= 0.3 ? "e14MetricRisky"
                      : (p.touch_intraweek_prob || 0) >= 0.15 ? "e14MetricMed" : "e14MetricGood";
      return '' +
        '<tr class="e14PlacementRow ' + (i === 0 ? "e14PlacementRow--top" : "") + '" data-index="' + i + '">' +
          '<td class="e14RankCell">' + (i + 1) + (i === 0 ? '<span class="e14StarTop">★</span>' : "") + "</td>" +
          "<td>" + Number(p.em_mult).toFixed(2) + "</td>" +
          "<td>" + Number(p.wing_pts).toFixed(0) + "</td>" +
          "<td>" + Number(p.short_put_strike).toFixed(0) + " / " + Number(p.short_call_strike).toFixed(0) + "</td>" +
          "<td>$" + Number(p.credit_dollars).toFixed(0) + "</td>" +
          '<td class="' + breachClass + '">' + fmtPct((p.breach_close_prob || 0) * 100) + "</td>" +
          '<td class="' + touchClass + '">' + fmtPct((p.touch_intraweek_prob || 0) * 100) + "</td>" +
          '<td class="' + maeClass + '">' + fmtPct((p.mae_p95_vs_wing || 0) * 100) + "</td>" +
          "<td>" + fmtPct(p.theta_capture_pct) + "</td>" +
          '<td class="e14ScoreCell ' + scoreClass + '">' + Number(p.composite_score).toFixed(1) + "</td>" +
          "<td>" + esc(p.confidence || "—") + "</td>" +
        "</tr>";
    }).join("");

    var topP = top[0] || { em_mult: 1.25, wing_pts: 10 };

    host.innerHTML = '' +
      '<div class="e14Console">' +
        '<div class="e14ConsoleHeader">' +
          "<div>" +
            '<h3 class="e14ConsoleTitle">Ranked weekly-IC placements</h3>' +
            '<div class="e14ConsoleSubtitle">' + subtitle + "</div>" +
          "</div>" +
        "</div>" +
        warningsHtml +
        '<table class="e14PlacementTable"><thead><tr>' +
          "<th>#</th><th>EM×</th><th>Wings (pts)</th>" +
          "<th>P short / C short</th><th>Credit ($)</th>" +
          "<th>Brch close</th><th>Touch intraweek</th>" +
          "<th>MAE p95 (% wing)</th><th>Theta cap</th>" +
          "<th>Score</th><th>Conf.</th>" +
        "</tr></thead><tbody>" + rows + "</tbody></table>" +

        '<div class="e14Tuner">' +
          '<div class="e14TunerField">' +
            '<label for="e14TunerEm">EM multiple <span id="e14TunerEmValue" class="e14TunerValue">' + Number(topP.em_mult).toFixed(2) + "</span></label>" +
            '<input id="e14TunerEm" type="range" min="0.75" max="2.5" step="0.05" value="' + Number(topP.em_mult).toFixed(2) + '" />' +
          "</div>" +
          '<div class="e14TunerField">' +
            '<label for="e14TunerWp">Wing width (pts) <span id="e14TunerWpValue" class="e14TunerValue">' + Number(topP.wing_pts).toFixed(0) + "</span></label>" +
            '<input id="e14TunerWp" type="range" min="2" max="30" step="1" value="' + Number(topP.wing_pts).toFixed(0) + '" />' +
          "</div>" +
          '<div class="e14TunerScoreBox">' +
            '<div>Custom placement score: <strong id="e14TunerScore">—</strong></div>' +
            '<div style="font-size:11px;color:var(--muted,#9aa0a6)" id="e14TunerScoreNote">snap to nearest grid</div>' +
          "</div>" +
        "</div>" +

        '<div class="e14ConsoleActions">' +
          '<button type="button" id="e14SimPickBtn" class="e14ConsoleActions--primary">Simulate This Pick</button>' +
          '<button type="button" id="e14LogTradeBtn">Log Trade as Open</button>' +
          '<button type="button" id="e14RunAdvisorBtn">Run Advisor</button>' +
          '<button type="button" id="e14ExportBtn">Export JSON</button>' +
        "</div>" +
      "</div>";

    // Row-click selection so the Log / Simulate buttons know which
    // placement the desk actually clicked (default = top rank).
    var rowsEl = host.querySelector(".e14PlacementTable tbody");
    if (rowsEl) {
      rowsEl.querySelectorAll(".e14PlacementRow").forEach(function (row) {
        row.addEventListener("click", function () {
          var idx = Number(row.getAttribute("data-index") || 0);
          State.selectedIndex = idx;
          rowsEl.querySelectorAll(".e14PlacementRow").forEach(function (r) {
            r.classList.remove("e14PlacementRow--selected");
          });
          row.classList.add("e14PlacementRow--selected");
        });
      });
    }

    wireTuner(deck);
    wireDeckActions(deck);
  }

  function wireTuner(deck) {
    var emEl = $("e14TunerEm");
    var wpEl = $("e14TunerWp");
    var emV = $("e14TunerEmValue");
    var wpV = $("e14TunerWpValue");
    var scoreEl = $("e14TunerScore");
    var noteEl  = $("e14TunerScoreNote");
    if (!emEl || !wpEl) return;

    var placements = (deck && deck.wingConsole && deck.wingConsole.placements) || [];
    var seq = 0;
    var debounceTimer = null;
    var DEBOUNCE_MS = 220;

    function nearest(em, wp) {
      var best = null, bestDist = Infinity;
      for (var i = 0; i < placements.length; i++) {
        var p = placements[i];
        var d = Math.pow(p.em_mult - em, 2) + Math.pow((p.wing_pts - wp) / 5, 2);
        if (d < bestDist) { best = p; bestDist = d; }
      }
      return best;
    }

    function paint(p, tag) {
      if (!p || !scoreEl) return;
      scoreEl.textContent = Number(p.composite_score).toFixed(1);
      scoreEl.className = scoreColor(Number(p.composite_score));
      if (noteEl) {
        noteEl.textContent = tag +
          " · brch " + fmtPct((p.breach_close_prob || 0) * 100) +
          " · touch " + fmtPct((p.touch_intraweek_prob || 0) * 100) +
          " · credit $" + Number(p.credit_dollars || 0).toFixed(0);
      }
    }

    async function fetchExact(em, wp) {
      var mySeq = ++seq;
      try {
        var resp = await fetch("/api/ic-scenario/wing-console/score-placement", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            underlying:  "SPX",
            entry_date:  State.entryDate,
            expiry_date: State.expiryDate,
            as_of_date:  State.asOfDate,
            em_mult:     em,
            wing_pts:    wp,
          }),
        });
        if (!resp.ok) return;
        var body = await resp.json();
        if (mySeq !== seq) return;
        paint(body.placement, "exact");
      } catch (err) { /* ignore */ }
    }

    function recompute() {
      var em = Number(emEl.value);
      var wp = Number(wpEl.value);
      if (emV) emV.textContent = em.toFixed(2);
      if (wpV) wpV.textContent = wp.toFixed(0);
      var near = nearest(em, wp);
      if (near) paint(near, "grid ~ EM " + Number(near.em_mult).toFixed(2) + " / " +
        Number(near.wing_pts).toFixed(0) + "pt");
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () { fetchExact(em, wp); }, DEBOUNCE_MS);
    }
    emEl.addEventListener("input", recompute);
    wpEl.addEventListener("input", recompute);
    recompute();
  }

  function wireDeckActions(deck) {
    var exportBtn = $("e14ExportBtn");
    if (exportBtn) {
      exportBtn.addEventListener("click", function () {
        var blob = new Blob([JSON.stringify(deck, null, 2)], { type: "application/json" });
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = "e14-wing-console-" +
          ((deck.wingConsole || {}).entry_date || "now") + ".json";
        a.click();
        setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      });
    }
    var simBtn = $("e14SimPickBtn");
    if (simBtn) simBtn.addEventListener("click", simulateTopPick);

    var advBtn = $("e14RunAdvisorBtn");
    if (advBtn) advBtn.addEventListener("click", runAdvisor);

    var logBtn = $("e14LogTradeBtn");
    if (logBtn) logBtn.addEventListener("click", function () { showLogModal(deck); });
  }

  // -------------------------------------------------------------
  // Adjust & Log modal (v2 parallel to E1/E2 adjust-log flows)
  // -------------------------------------------------------------

  function showLogModal(deck) {
    var wc = (deck && deck.wingConsole) || {};
    var placements = wc.placements || [];
    var idx = State.selectedIndex || 0;
    var p = placements[idx];
    if (!p) {
      alert("No placement selected.");
      return;
    }

    var entry = wc.entry_date || State.entryDate;
    var expiry = wc.expiry_date || State.expiryDate;
    var defCredit = Number(p.credit_dollars || 0) / 100.0;
    var defSP = p.short_put_strike != null ? p.short_put_strike : "";
    var defLP = p.long_put_strike  != null ? p.long_put_strike  : "";
    var defSC = p.short_call_strike != null ? p.short_call_strike : "";
    var defLC = p.long_call_strike  != null ? p.long_call_strike  : "";

    var overlay = document.createElement("div");
    overlay.id = "e14LogOverlay";
    overlay.style.cssText = "position:fixed;inset:0;z-index:10001;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center";
    overlay.innerHTML = '<div style="background:var(--bg,#121418);color:var(--text,#e6e6e6);border-radius:12px;padding:24px;width:480px;max-width:90vw;max-height:90vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.4)">' +
      '<h3 style="margin:0 0 6px">Adjust &amp; Log Trade — SPX</h3>' +
      '<div style="font-size:11px;opacity:.6;margin-bottom:14px">' +
        'Seeded from Wing Console · rank ' + (idx + 1) +
        ' · composite ' + Number(p.composite_score || 0).toFixed(1) +
        ' · ' + esc(p.confidence || "—") + ' confidence' +
      '</div>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:13px">' +
        _field("Entry Date",      "e14LogEntry",  entry,  "date") +
        _field("Expiry",          "e14LogExpiry", expiry, "date") +
        _field("Long Put",        "e14LogLP",     defLP,  "number", "", "", "0.5") +
        _field("Short Put",       "e14LogSP",     defSP,  "number", "", "", "0.5") +
        _field("Short Call",      "e14LogSC",     defSC,  "number", "", "", "0.5") +
        _field("Long Call",       "e14LogLC",     defLC,  "number", "", "", "0.5") +
        _field("Entry Credit ($)","e14LogCredit", defCredit.toFixed(2), "number", "0", "9999", "0.01") +
      '</div>' +
      '<div style="margin-top:12px"><label style="font-size:12px;opacity:.7">Notes (optional)</label>' +
      '<textarea id="e14LogNotes" rows="2" style="width:100%;font-size:12px;padding:6px;border-radius:6px;border:1px solid var(--border,rgba(255,255,255,.12));background:var(--surface-2,#1a1d24);color:inherit;margin-top:4px" placeholder="Fill price, rationale, anything worth pinning to the journal…"></textarea></div>' +
      '<div style="display:flex;gap:12px;margin-top:20px;justify-content:flex-end">' +
        '<button id="e14LogCancel" style="padding:8px 20px;font-size:12px;border-radius:6px;border:1px solid var(--border,rgba(255,255,255,.12));background:none;color:inherit;cursor:pointer">Cancel</button>' +
        '<button id="e14LogSubmit" class="e14ConsoleActions--primary" style="padding:8px 20px;font-size:12px;font-weight:600;border-radius:6px;border:1px solid rgba(60,212,169,0.45);background:rgba(60,212,169,0.12);color:#3cd4a9;cursor:pointer">Log Trade as Open</button>' +
      '</div></div>';

    document.body.appendChild(overlay);
    $("e14LogCancel").addEventListener("click", function () { overlay.remove(); });
    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) overlay.remove();
    });

    $("e14LogSubmit").addEventListener("click", async function () {
      var entryVal = $("e14LogEntry").value || entry;
      var expVal   = $("e14LogExpiry").value || expiry;
      var lp = parseFloat($("e14LogLP").value) || null;
      var sp = parseFloat($("e14LogSP").value) || null;
      var sc = parseFloat($("e14LogSC").value) || null;
      var lc = parseFloat($("e14LogLC").value) || null;
      var creditVal = parseFloat($("e14LogCredit").value) || 0;
      var notes = ($("e14LogNotes").value || "").trim();

      if (!(lp && sp && sc && lc)) {
        alert("All four strikes (long put / short put / short call / long call) are required.");
        return;
      }
      if (!(lp < sp && sp < sc && sc < lc)) {
        alert("Strikes must satisfy: longPut < shortPut < shortCall < longCall.");
        return;
      }
      if (creditVal <= 0) {
        alert("Entry credit must be positive.");
        return;
      }

      // Re-run the scenario with the adjusted strikes/credit so the
      // journal carries a full replay context. Then journal the trade.
      var scenarioBody = {
        underlying: "SPX",
        entryDate: entryVal,
        expiry:    expVal,
        longPut:   lp,
        shortPut:  sp,
        shortCall: sc,
        longCall:  lc,
        creditReceived: creditVal,
        seasonMode: "none",
        wingConsoleCacheKey: (deck && deck.wingConsole && deck.wingConsole.cache_key) || null,
        placementRank:       idx,
        sourceChip:          (State.selectedIndex !== 0 ? "user_override" : "desk_default"),
      };

      try {
        // Run the scenario (re-prices the adjusted wings so the journal
        // entry has the full distribution + sizing + greeks).
        var simResp = await fetch("/api/ic-scenario", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(scenarioBody),
        });
        if (!simResp.ok) {
          var t1 = await simResp.text();
          throw new Error("scenario " + simResp.status + ": " + t1.slice(0, 200));
        }
        var scenario = await simResp.json();

        // Journal the trade with the just-computed scenario attached.
        // The endpoint expects `request` (original form submission) +
        // `scenario` (full /api/ic-scenario payload); extra keys are
        // preserved verbatim on the trade record for later review.
        var journalResp = await fetch("/api/ic-scenario/journal", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            request:         scenarioBody,
            scenario:        scenario,
            note:            notes || "Logged from E14 Wing Console.",
          }),
        });
        if (!journalResp.ok) {
          var t2 = await journalResp.text();
          throw new Error("journal " + journalResp.status + ": " + t2.slice(0, 200));
        }
        var journalResult = await journalResp.json();
        overlay.remove();
        alert("Trade logged as open.\nTrade ID: " + (journalResult.tradeId || journalResult.trade_id || "ok"));
        if (typeof loadActiveTrades === "function") loadActiveTrades();
      } catch (err) {
        alert("Failed to log trade: " + (err && err.message ? err.message : String(err)));
      }
    });
  }

  function _field(label, id, defVal, type, min, max, step) {
    var html = '<div><label for="' + id + '" style="font-size:11px;opacity:.7;display:block;margin-bottom:3px">' + label + '</label>';
    html += '<input id="' + id + '" type="' + (type || "text") + '" value="' + (defVal != null ? esc(defVal) : "") + '"' +
      (min ? ' min="' + min + '"' : '') + (max ? ' max="' + max + '"' : '') + (step ? ' step="' + step + '"' : '') +
      ' style="width:100%;padding:6px;font-size:12px;border-radius:6px;border:1px solid var(--border,rgba(255,255,255,.12));background:var(--surface-2,#1a1d24);color:inherit">';
    html += '</div>';
    return html;
  }

  function simulateTopPick() {
    var deck = State.lastDeck;
    if (!deck || !deck.wingConsole) return;
    var p = (deck.wingConsole.placements || [])[State.selectedIndex || 0];
    if (!p) return;

    var sp  = $("shortPut"),  lp = $("longPut"),
        sc  = $("shortCall"), lc = $("longCall"),
        cr  = $("creditReceived"),
        ed  = $("entryDate"),
        xp  = $("expiry");
    if (sp) sp.value = Number(p.short_put_strike).toFixed(0);
    if (lp) lp.value = Number(p.long_put_strike).toFixed(0);
    if (sc) sc.value = Number(p.short_call_strike).toFixed(0);
    if (lc) lc.value = Number(p.long_call_strike).toFixed(0);
    if (cr) cr.value = Number(p.credit_dollars / 100.0).toFixed(2);
    if (ed) ed.value = State.entryDate;
    if (xp) xp.value = State.expiryDate;

    // Trigger the existing scenario form handler so the drilldown renders.
    var form = $("icForm");
    if (form) {
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else {
        var evt = new Event("submit", { bubbles: true, cancelable: true });
        form.dispatchEvent(evt);
      }
    }
    setSourceChip("user_override");
  }

  // ---------------------------------------------------------------
  // MI v2 / MC / MAE card painters
  // ---------------------------------------------------------------

  function paintMiV2(deck) {
    var sec  = $("e14RegimeMiV2Section");
    var body = $("e14RegimeMiV2Body");
    if (!sec || !body) return;
    var r = (deck && deck.regime && deck.regime.mi_v2) || null;
    if (!r) { sec.style.display = "none"; return; }
    sec.style.display = "";
    var probs = r.probabilities || {};
    var bars = Object.keys(probs).map(function (label) {
      var pct = Math.max(0, Math.min(1, Number(probs[label]) || 0));
      return '' +
        '<div class="e14RegimeMiV2">' +
          '<span style="min-width:110px">' + esc(label) + "</span>" +
          '<span class="e14RegimeMiV2Bar" style="width:' + Math.max(24, Math.round(pct * 180)) + 'px">' +
            '<span class="e14RegimeMiV2Fill" style="width:' + (pct * 100).toFixed(0) + '%"></span>' +
          "</span>" +
          "<span>" + (pct * 100).toFixed(1) + "%</span>" +
        "</div>";
    }).join("");
    body.innerHTML = '' +
      '<div style="padding:12px 16px">' +
        '<div class="muted" style="font-size:12px;margin-bottom:6px">' +
          "label: <strong>" + esc(r.label || "—") + "</strong> · vol_state: <strong>" +
          esc(typeof r.vol_state === "string" ? r.vol_state : JSON.stringify(r.vol_state || "—")) +
          "</strong> · source: <strong>" + esc(r.source || "—") + "</strong>" +
        "</div>" +
        bars +
      "</div>";
  }

  function paintMcReading(deck) {
    var sec  = $("e14McReadingSection");
    var body = $("e14McReadingBody");
    if (!sec || !body) return;
    var mc = deck && deck.mcResults;
    if (!mc || !mc.n_sims) { sec.style.display = "none"; return; }
    sec.style.display = "";
    body.innerHTML = '' +
      '<div style="padding:12px 16px">' +
        '<div class="muted" style="font-size:12px;margin-bottom:6px">' +
          "n_sims: <strong>" + mc.n_sims + "</strong> · mode: <strong>" + esc(mc.mode) +
          "</strong> · conditioning: <strong>" + esc(mc.conditioning_used) +
          "</strong> · pool used/total: <strong>" + mc.pool_size_used + "/" + mc.pool_size_total + "</strong>" +
        "</div>" +
        (Array.isArray(mc.notes) && mc.notes.length
          ? '<div class="muted" style="font-size:11px">' + mc.notes.map(esc).join(" · ") + "</div>"
          : "") +
      "</div>";
  }

  function paintMaeDistribution(deck) {
    var sec  = $("e14MaeDistributionSection");
    var body = $("e14MaeDistributionBody");
    if (!sec || !body) return;
    var m = deck && deck.maeDistribution;
    if (!m || !m.n) { sec.style.display = "none"; return; }
    sec.style.display = "";
    body.innerHTML = '' +
      '<div style="padding:12px 16px">' +
        '<div class="muted" style="font-size:12px;margin-bottom:8px">' +
          "n=<strong>" + m.n + "</strong> weeks · source: <strong>" + esc(m.source) + "</strong>" +
        "</div>" +
        '<div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(120px, 1fr));gap:8px">' +
          '<div class="e14Card"><div class="e14CardLabel">p50</div><div class="e14CardValue">' + fmtPct(m.p50, 2) + "</div></div>" +
          '<div class="e14Card"><div class="e14CardLabel">p75</div><div class="e14CardValue">' + fmtPct(m.p75, 2) + "</div></div>" +
          '<div class="e14Card"><div class="e14CardLabel">p90</div><div class="e14CardValue">' + fmtPct(m.p90, 2) + "</div></div>" +
          '<div class="e14Card"><div class="e14CardLabel">p95</div><div class="e14CardValue">' + fmtPct(m.p95, 2) + "</div></div>" +
          '<div class="e14Card"><div class="e14CardLabel">max</div><div class="e14CardValue">' + fmtPct(m.max, 2) + "</div></div>" +
        "</div>" +
      "</div>";
  }

  // ---------------------------------------------------------------
  // Advisor
  // ---------------------------------------------------------------

  async function runAdvisor() {
    var sec = $("e14AdvisorSection");
    var body = $("e14AdvisorBody");
    if (!sec || !body) return;
    sec.style.display = "";
    body.innerHTML = '<div class="muted" style="padding:12px">Calling advisor…</div>';

    // Prefer a scenario that has been run from the form; fall back to
    // running the advisor from a fresh /request using the form values.
    var formBody = readFormBody();
    try {
      var resp = await fetch("/api/ic-scenario/advisor", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request: formBody }),
      });
      if (!resp.ok) {
        var txt = await resp.text();
        throw new Error("advisor " + resp.status + ": " + txt.slice(0, 300));
      }
      var out = await resp.json();
      paintAdvisor(body, out);
    } catch (err) {
      body.innerHTML = '<div class="e14ConsoleWarnings">Advisor unavailable: ' +
        esc(err && err.message ? err.message : String(err)) + "</div>";
    }
  }

  function readFormBody() {
    function num(id) {
      var v = $(id);
      return v && v.value ? parseFloat(v.value) : null;
    }
    function s(id) {
      var v = $(id);
      return v && v.value ? String(v.value) : "";
    }
    return {
      underlying:      "SPX",
      entryDate:       s("entryDate"),
      expiry:          s("expiry"),
      shortPut:        num("shortPut"),
      longPut:         num("longPut"),
      shortCall:       num("shortCall"),
      longCall:        num("longCall"),
      creditReceived:  num("creditReceived"),
      profitTargetPct: num("profitTargetPct"),
      stopLossPct:     num("stopLossPct"),
      seasonMode:      s("seasonMode") || "none",
    };
  }

  function paintAdvisor(host, out) {
    var adv = out && out.advisor;
    if (!adv) {
      host.innerHTML = '<div class="e14ConsoleWarnings">Advisor returned empty response.</div>';
      return;
    }
    var verdictClass = "e14ScoreCell";
    if (adv.verdict === "PASS") verdictClass = "e14ScoreCell--low";
    else if (adv.verdict === "HOLD") verdictClass = "e14ScoreCell--med";

    var risks = Array.isArray(adv.risks) ? adv.risks.map(function (r) {
      return "<li>" + esc(r) + "</li>";
    }).join("") : "";
    var keyPts = Array.isArray(adv.keyPoints) ? adv.keyPoints.map(function (r) {
      return "<li>" + esc(r) + "</li>";
    }).join("") : "";
    var adjs = Array.isArray(adv.suggestedAdjustments) ? adv.suggestedAdjustments.map(function (r) {
      return "<li>" + esc(r) + "</li>";
    }).join("") : "";

    host.innerHTML = '' +
      '<div style="padding:14px 16px">' +
        '<div style="display:flex; gap:12px; align-items:baseline; flex-wrap:wrap">' +
          '<span style="font-size:22px; font-weight:800; letter-spacing:0.04em" class="' + verdictClass + '">' + esc(adv.verdict) + "</span>" +
          '<span class="muted" style="font-size:12px">confidence: <strong>' + Number(adv.confidence * 100).toFixed(0) + "%</strong> · stance: <strong>" + esc(adv.stance) + "</strong> · source: <strong>" + esc(adv._source) + "</strong></span>" +
        "</div>" +
        '<div class="e14AdvisorNarrative" style="margin-top:8px">' + esc(adv.narrative || "") + "</div>" +
        (keyPts ? '<div style="margin-top:8px"><strong>Key points</strong><ul style="margin:4px 0 0 20px">' + keyPts + "</ul></div>" : "") +
        (risks ? '<div style="margin-top:8px"><strong>Risks</strong><ul style="margin:4px 0 0 20px">' + risks + "</ul></div>" : "") +
        (adjs ? '<div style="margin-top:8px"><strong>Suggested adjustments</strong><ul style="margin:4px 0 0 20px">' + adjs + "</ul></div>" : "") +
        (adv.deskNote ? '<div class="muted" style="margin-top:8px;font-size:12px"><strong>Desk note:</strong> ' + esc(adv.deskNote) + "</div>" : "") +
        (adv.plannedExitNote ? '<div class="muted" style="margin-top:4px;font-size:12px"><strong>Exit:</strong> ' + esc(adv.plannedExitNote) + "</div>" : "") +
      "</div>";
  }

  // ---------------------------------------------------------------
  // Boot: run Wing Console on page load + when form fields change.
  // ---------------------------------------------------------------

  function boot() {
    // v2: Honor URL-param handoff from upstream engines. The
    // /ic-scenario page can be opened from E2's "Simulate in E14"
    // button carrying entry_date, expiry, and a specific 4-strike
    // structure (plus optional credit). When those params are
    // present we pre-fill the scenario form and trigger submit so
    // the desk lands directly on the replay drilldown + Wing Console.
    var handoff = parseHandoffQuery();
    if (handoff && handoff.valid) {
      applyHandoffToForm(handoff);
      showHandoffBanner(handoff);
      triggerIcFormSubmit();
      loadActiveTrades();
      return;
    }

    // Default path: NO auto-run. The desk fills entry/expiry + 4
    // strikes + credit in #icForm, then clicks Calculate. The
    // capture-phase submit listener below fires the Wing Console +
    // Active Trades alongside ic-scenario.js's own submit handler
    // (which runs the scenario drilldown).
    var form = $("icForm");
    if (form) {
      form.addEventListener("submit", function () {
        fetchAndPaintWingConsole();
        loadActiveTrades();
      }, { capture: true });
    }
  }

  // ---------------------------------------------------------------
  // URL handoff (E2 -> E14 "Simulate in E14" button, etc.)
  // ---------------------------------------------------------------

  function parseHandoffQuery() {
    try {
      var q = new URLSearchParams(window.location.search);
      var entry = (q.get("entry_date") || q.get("entryDate") || "").trim();
      var exp   = (q.get("expiry") || q.get("expiryDate") || "").trim();
      var sp    = parseFloat(q.get("short_put") || q.get("shortPut") || "");
      var lp    = parseFloat(q.get("long_put")  || q.get("longPut")  || "");
      var sc    = parseFloat(q.get("short_call") || q.get("shortCall") || "");
      var lc    = parseFloat(q.get("long_call")  || q.get("longCall")  || "");
      var credit = parseFloat(q.get("credit") || q.get("creditReceived") || "");
      var src   = (q.get("src") || "").toLowerCase();
      var rank  = q.get("rank");
      var composite = q.get("composite");
      var valid = Boolean(
        entry && exp &&
        Number.isFinite(sp) && Number.isFinite(lp) &&
        Number.isFinite(sc) && Number.isFinite(lc) &&
        lp < sp && sp < sc && sc < lc
      );
      return {
        valid:      valid,
        entryDate:  entry,
        expiry:     exp,
        shortPut:   sp,
        longPut:    lp,
        shortCall:  sc,
        longCall:   lc,
        credit:     Number.isFinite(credit) ? credit : 0,
        src:        src,
        rank:       rank,
        composite:  composite,
      };
    } catch (e) {
      return { valid: false };
    }
  }

  function applyHandoffToForm(h) {
    var assign = function (id, v) {
      var el = $(id);
      if (el && v != null && v !== "") el.value = v;
    };
    assign("entryDate", h.entryDate);
    assign("expiry",    h.expiry);
    assign("longPut",   h.longPut);
    assign("shortPut",  h.shortPut);
    assign("shortCall", h.shortCall);
    assign("longCall",  h.longCall);
    if (h.credit > 0) assign("creditReceived", h.credit.toFixed(2));
  }

  function showHandoffBanner(h) {
    var banner = $("banner");
    if (!banner) return;
    var rankLabel = h.rank != null ? (" rank " + (parseInt(h.rank, 10) + 1)) : "";
    var srcLabel = h.src === "e2" ? "E2 SPX IC Command Deck" : "upstream engine";
    banner.textContent =
      "Scenario from " + srcLabel + rankLabel + " handoff: entry " +
      h.entryDate + ", expiry " + h.expiry + ", strikes " +
      h.longPut + "/" + h.shortPut + " put  " +
      h.shortCall + "/" + h.longCall + " call" +
      (h.credit > 0 ? (" @ $" + h.credit.toFixed(2)) : "") +
      (h.composite ? (" · composite " + Number(h.composite).toFixed(1)) : "");
    banner.style.display = "";
    banner.classList.remove("error");
  }

  function triggerIcFormSubmit() {
    var form = $("icForm");
    if (!form) return;
    // Let ic-scenario.js's submit handler take over; that path
    // already runs the scenario + renders the drilldown.
    if (typeof form.requestSubmit === "function") form.requestSubmit();
    else form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  }

  // -------------------------------------------------------------
  // Active Trades — lists E14-source open positions from E2 store.
  // -------------------------------------------------------------

  async function loadActiveTrades() {
    var sec = $("e14ActiveTradesSection");
    var host = $("e14ActiveTradesBody");
    if (!sec || !host) return;
    try {
      var resp = await fetch("/api/spx-ic/trades");
      if (!resp.ok) {
        sec.style.display = "none";
        return;
      }
      var data = await resp.json();
      var all = data.trades || [];
      // Filter to E14-source trades (logged from this page).
      var trades = all.filter(function (t) { return t.source === "engine14"; });
      if (!trades.length) {
        sec.style.display = "none";
        return;
      }
      sec.style.display = "";
      host.innerHTML = trades.map(renderTradeCard).join("");
      wireTradeCards();
    } catch (err) {
      // Silent fail; section stays hidden.
    }
  }

  function renderTradeCard(t) {
    var entry = t.entry || {};
    var tracking = t.tracking || {};
    var status = tracking.deterministicStatus || "on_track";
    var lastCheckin = (t.checkIns || []).slice(-1)[0];
    var narrative = "";
    if (lastCheckin) {
      if (lastCheckin.headline) narrative = esc(lastCheckin.headline);
      if (lastCheckin.recommendation) narrative += '<div style="opacity:.8;margin-top:4px">' + esc(lastCheckin.recommendation) + '</div>';
    }
    var statusColor = "#7db6ff";
    if (status === "exit") statusColor = "#ff7a7a";
    else if (status === "adjust") statusColor = "#f3a847";
    else if (status === "on_track") statusColor = "#3cd4a9";

    return '' +
      '<div style="padding:12px 16px;border-bottom:1px solid rgba(255,255,255,0.05)">' +
        '<div style="display:flex;gap:12px;align-items:baseline;flex-wrap:wrap;margin-bottom:6px">' +
          '<strong style="font-size:13px">' + esc(entry.underlying || "SPX") + ' IC · ' + esc(String(entry.wingWidth || "—")) + 'pt wings</strong>' +
          '<span style="font-size:10px;padding:2px 6px;border-radius:3px;background:#0a84ff;color:#fff;font-weight:600">E14</span>' +
          '<span style="font-size:11px;padding:2px 8px;border-radius:4px;background:' + statusColor + '22;color:' + statusColor + ';text-transform:uppercase;letter-spacing:0.4px">' + esc(status) + '</span>' +
          '<span class="muted" style="font-size:11px">Logged ' + esc(String(t.loggedAt || "").slice(0, 16).replace("T", " ")) + '</span>' +
        '</div>' +
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:6px;font-size:11px;margin-bottom:8px">' +
          '<div><span class="muted">Short Put</span> <strong>' + esc(String(entry.shortPutStrike || "—")) + '</strong></div>' +
          '<div><span class="muted">Short Call</span> <strong>' + esc(String(entry.shortCallStrike || "—")) + '</strong></div>' +
          '<div><span class="muted">Credit</span> <strong>$' + esc(String(entry.entryCredit || "—")) + '</strong></div>' +
          '<div><span class="muted">Spot</span> <strong>' + (tracking.currentSpot ? Number(tracking.currentSpot).toFixed(2) : "—") + '</strong></div>' +
          '<div><span class="muted">DTE</span> <strong>' + (tracking.dte != null ? tracking.dte : "—") + '</strong></div>' +
          '<div><span class="muted">Put / Call dist</span> <strong>' + (tracking.distPutPct != null ? tracking.distPutPct + '%' : '—') + ' / ' + (tracking.distCallPct != null ? tracking.distCallPct + '%' : '—') + '</strong></div>' +
        '</div>' +
        '<div id="e14ReviewPanel-' + esc(t.tradeId) + '" class="e14ReviewPanel" style="display:' + (narrative ? '' : 'none') + ';padding:8px 10px;border-radius:6px;background:rgba(125,182,255,0.06);border:1px solid rgba(125,182,255,0.2);font-size:11px;margin-bottom:8px">' + narrative + '</div>' +
        '<div style="display:flex;gap:8px;flex-wrap:wrap">' +
          '<button class="e14ReviewBtn e14ConsoleActions--primary" data-trade-id="' + esc(t.tradeId) + '" style="padding:4px 12px;font-size:11px;border-radius:6px;border:1px solid rgba(125,182,255,0.45);background:rgba(125,182,255,0.12);color:#7db6ff;cursor:pointer;font-weight:600">Run Live Check-In</button>' +
          '<button class="e14CloseBtn" data-trade-id="' + esc(t.tradeId) + '" style="padding:4px 12px;font-size:11px;border-radius:6px;border:1px solid rgba(255,255,255,0.12);background:none;color:inherit;cursor:pointer">Close Trade</button>' +
        '</div>' +
      '</div>';
  }

  function wireTradeCards() {
    document.querySelectorAll(".e14ReviewBtn").forEach(function (btn) {
      btn.addEventListener("click", function () { runReview(btn.dataset.tradeId); });
    });
    document.querySelectorAll(".e14CloseBtn").forEach(function (btn) {
      btn.addEventListener("click", function () { closeTrade(btn.dataset.tradeId); });
    });
  }

  async function runReview(tradeId) {
    var panel = document.getElementById("e14ReviewPanel-" + tradeId);
    if (!panel) return;
    panel.style.display = "";
    panel.innerHTML = '<div class="muted">Running check-in…</div>';
    try {
      // Reuse E2's checkin endpoint — E14 trades live in the same store
      // (via backend.engine2_trades.log_trade), so tracking + analysis
      // resolve cleanly for both sources.
      var resp = await fetch("/api/spx-ic/trade/" + encodeURIComponent(tradeId) + "/checkin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (!resp.ok) {
        var txt = await resp.text();
        throw new Error("HTTP " + resp.status + ": " + txt.slice(0, 200));
      }
      var body = await resp.json();
      paintReview(panel, body);
    } catch (err) {
      panel.innerHTML = '<div style="color:#ff7a7a">Check-in failed: ' +
        esc(err && err.message ? err.message : String(err)) + "</div>";
    }
  }

  function paintReview(panel, body) {
    var a = (body && body.analysis) || {};
    var tr = (body && body.tracking) || {};
    var status = a.status || tr.deterministicStatus || "—";
    var color = "#7db6ff";
    if (status === "exit") color = "#ff7a7a";
    else if (status === "adjust") color = "#f3a847";
    else if (status === "on_track") color = "#3cd4a9";

    panel.innerHTML = '' +
      '<div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:6px">' +
        '<strong style="color:' + color + ';font-size:13px;text-transform:uppercase">' + esc(status) + '</strong>' +
        '<span class="muted">Spot $' + (body.currentSpot != null ? Number(body.currentSpot).toFixed(2) : "—") + '</span>' +
      '</div>' +
      (a.headline ? '<div style="font-weight:600;margin-bottom:4px">' + esc(a.headline) + '</div>' : '') +
      (a.recommendation ? '<div style="margin-bottom:4px">' + esc(a.recommendation) + '</div>' : '') +
      (a.spotAnalysis ? '<div class="muted">' + esc(a.spotAnalysis) + '</div>' : '') +
      (a.regimeDrift ? '<div class="muted" style="margin-top:4px">Regime: ' + esc(a.regimeDrift) + '</div>' : '') +
      (a.adjustmentIfNeeded ? '<div style="margin-top:4px;padding:6px 8px;border-left:3px solid #f3a847;background:rgba(243,168,71,0.08)">' + esc(a.adjustmentIfNeeded) + '</div>' : '') +
      (a.deskNote ? '<div class="muted" style="margin-top:6px"><em>Desk note:</em> ' + esc(a.deskNote) + '</div>' : '');
  }

  async function closeTrade(tradeId) {
    if (!confirm("Close trade " + tradeId + "?")) return;
    try {
      var resp = await fetch("/api/spx-ic/trade/" + encodeURIComponent(tradeId) + "/close", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ closeReason: "manual" }),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      loadActiveTrades();
    } catch (err) {
      alert("Close failed: " + (err && err.message ? err.message : String(err)));
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
