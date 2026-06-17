/* Earnings Drift (PEAD) — Engine 18 dashboard.
   Fetches /api/engine18 and renders candidate cards (surprise bucket, quality
   quintile, sizing tier, entry/exit dates, expected-edge stats), the desk
   tracker (track entry / close), the informational options expression card,
   the rolling-edge validation banner, the narrative desk note, and the manual
   on-demand PEAD profile (single ticker through the same validated pipeline). */
(function () {
  "use strict";

  var state = { payload: null, trades: [], options: {} };

  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]; }); }
  function num(v, d) { d = (d == null ? 1 : d); return (v == null ? "—" : (Math.round(v * Math.pow(10, d)) / Math.pow(10, d)).toFixed(d)); }
  function pct(v, d) { return v == null ? "—" : ((v >= 0 ? "+" : "") + num(v * 100, d == null ? 1 : d) + "%"); }
  function money(v) {
    if (v == null) return "—";
    if (v >= 1e9) return "$" + num(v / 1e9, 1) + "B";
    if (v >= 1e6) return "$" + num(v / 1e6, 0) + "M";
    return "$" + num(v, 0);
  }

  function setStatus(msg, isError) {
    var el = $("edStatus");
    el.textContent = msg || "";
    el.style.color = isError ? "#b02018" : "rgba(11,11,15,0.5)";
  }

  /* ── Validation / rolling-edge banner ── */
  function renderValidation(p) {
    var el = $("edValidation");
    var v = p.validation;
    if (!v || v.rolling6mAvgNetPct == null) { el.style.display = "none"; return; }
    var degraded = !!v.degraded;
    el.className = "edValidation" + (degraded ? " edValidation--degraded" : "");
    var flag = degraded
      ? '<span class="edDegradedFlag">Degraded</span>'
      : '<span class="edHealthyFlag">Edge intact</span>';
    el.innerHTML = flag +
      "<span>Rolling 6-mo avg net <b>" + pct(v.rolling6mAvgNetPct / 100, 2) + "</b>/trade" +
      (v.n6m != null ? " over <b>" + v.n6m + "</b> signals" : "") + "</span>" +
      (v.rolling12mAvgNetPct != null ? '<span class="edMuted">12-mo ' + pct(v.rolling12mAvgNetPct / 100, 2) + "/trade" + (v.n12m != null ? " (n=" + v.n12m + ")" : "") + "</span>" : "") +
      (v.asOf ? '<span class="edMuted">validated ' + esc(String(v.asOf).slice(0, 10)) + "</span>" : "") +
      (degraded ? "<span><b>Do not add new entries</b> until the desk reviews the edge.</span>" : "");
    el.style.display = "flex";
  }

  /* ── Candidate cards ── */
  function expectedLine(c) {
    var e = c.expected || {};
    var parts = [];
    if (e.bucketAvgNetPct != null) {
      parts.push("bucket <b>" + (e.bucketAvgNetPct >= 0 ? "+" : "") + num(e.bucketAvgNetPct, 2) + "%/trade</b>" +
        (e.bucketHitRate != null ? " @ " + num(e.bucketHitRate * 100, 0) + "% hit" : "") +
        (e.bucketN != null ? " (n=" + e.bucketN + ")" : ""));
    }
    if (e.qualityAvgNetPct != null) {
      parts.push("quality cohort <b>+" + num(e.qualityAvgNetPct, 2) + "%/trade</b>" +
        (e.qualityHitRate != null ? " @ " + num(e.qualityHitRate * 100, 0) + "% hit" : ""));
    }
    if (!parts.length) return "";
    return '<div class="edExpected">Validated cohort: ' + parts.join(" · ") + "</div>";
  }

  function optionsBlock(c) {
    var opt = state.options[c.ticker];
    if (opt === undefined) return "";  // not fetched (not full-size)
    if (opt === null) return '<div class="edOpt"><div class="edOptHead">Options expression</div><div class="edMuted">Live chain unavailable — express via equity.</div></div>';
    return '<div class="edOpt">' +
      '<div class="edOptHead" data-insight="options_expression">Options expression (informational)</div>' +
      '<div class="edOptBody">' +
        esc(opt.structure) + " · exp <b>" + esc(opt.expiry) + "</b> (" + num(opt.dte, 0) + "d)<br/>" +
        "long " + num(opt.longStrike, 2) + "C (~" + num((opt.longDelta || 0) * 100, 0) + "Δ) / short " +
        num(opt.shortStrike, 2) + "C (~" + num((opt.shortDelta || 0) * 100, 0) + "Δ)<br/>" +
        "debit ~<b>" + num(opt.debit, 2) + "</b> · width " + num(opt.width, 2) +
        (opt.rewardRisk != null ? " · R/R " + num(opt.rewardRisk, 1) + ":1" : "") +
      "</div>" +
      '<div class="edOptDisclaimer">' + esc(opt.disclaimer || "Informational — options expression not backtested.") + "</div>" +
    "</div>";
  }

  /* Deterministic desk verdict. Prefer the backend's value; fall back to the
     same rule so the card stays correct even against an older API. */
  function decisionFor(c) {
    if (c.decision) return c.decision;
    if (c.sizing === "full" || c.sizing === "half") return c.entry_status === "late" ? "CAUTION" : "GO";
    return "NO_GO";
  }

  var DECISION_META = {
    GO:      { label: "GO",      cls: "go",      sub: "Commit capital — enter LONG at the open" },
    NO_GO:   { label: "NO GO",   cls: "nogo",    sub: "Do not commit capital — signal does not clear the bar" },
    CAUTION: { label: "CAUTION", cls: "caution", sub: "Validated entry has passed — desk review before sizing" },
  };

  function planRow(label, value, sub) {
    return '<div class="edPlanRow"><span class="edPlanL">' + label + "</span>" +
      '<span class="edPlanV">' + value + (sub ? ' <span class="edMuted">' + sub + "</span>" : "") + "</span></div>";
  }

  function candidateCard(c) {
    var rep = c.report || {};
    var g = c.grade || {};
    var decision = decisionFor(c);
    var dm = DECISION_META[decision] || DECISION_META.NO_GO;
    var cardCls = "edCard edCard--" + dm.cls + (c.sizing === "full" ? " edCard--full" : (c.sizing === "pass" ? " edCard--pass" : ""));
    var bucketLabel = c.bucket === "beat_large" ? "Large beat" : "Small beat";
    var sizingLabel = c.sizing === "full" ? "FULL SIZE" : (c.sizing === "half" ? "HALF SIZE" : "PASS");
    var sizeSub = c.sizing === "full" ? "full position" : (c.sizing === "half" ? "half position" : "no allocation");
    var gradeSrc = g.source === "llm" ? "LLM" : (g.source === "heuristic" ? "heuristic" : "no transcript");
    var confidence = c.confidence || (c.sizing === "full" ? "High" : (c.sizing === "half" ? "Moderate" : "Low"));
    var entryRef = c.last_close != null ? "ref ~$" + num(c.last_close, 2) : "no ref px";
    var manualPill = c.origin === "manual"
      ? '<span class="edPill edPill--manual" title="On-demand profile — EPS source: ' + esc((c.eps_source || "").toUpperCase()) + '">MANUAL</span>'
      : "";
    var lateChip = c.entry_status === "late"
      ? '<div class="edLateChip">⚠ ' + c.days_late + " trading day" + (c.days_late === 1 ? "" : "s") +
        " past the validated entry (" + esc(c.entry_date) + " open). Mid-drift entries were never backtested — expected stats below do NOT apply.</div>"
      : "";

    var plan =
      '<div class="edPlan">' +
        '<div class="edPlanHead">Trade plan</div>' +
        planRow("Decision", '<b class="edDec--' + dm.cls + '">' + dm.label + "</b>", dm.sub) +
        planRow("Direction", "LONG", "PEAD long-only") +
        planRow("Size", sizingLabel, sizeSub) +
        planRow("Entry", esc(c.entry_date || "—") + " open", entryRef) +
        planRow("Hold", (c.hold_days || 10) + " trading days", "→ exit " + esc(c.exit_date || "—")) +
        planRow("Confidence", esc(confidence), esc(g.quintile || "—") + " quality · " + num(g.score, 2)) +
      "</div>";

    return '<div class="' + cardCls + '">' +
      '<div class="edCardHead">' +
        '<span class="edTicker">' + esc(c.ticker) + "</span>" +
        '<span class="edDecBadge edDecBadge--' + dm.cls + '">' + dm.label + "</span>" +
        '<span class="edCardMeta">' + esc(rep.report_date || "") + (rep.timing ? " · " + esc(rep.timing).toUpperCase() : "") + "</span>" +
      "</div>" +
      '<div class="edCardPills">' +
        '<span class="edPill edPill--' + esc(c.bucket) + '">' + bucketLabel + "</span>" +
        '<span class="edPill edPill--q">' + esc(g.quintile || "—") + "</span>" +
        '<span class="edPill edPill--' + esc(c.sizing) + '">' + sizingLabel + "</span>" +
        manualPill +
      "</div>" +
      lateChip +
      plan +
      '<details class="edDetail"><summary>Signal detail</summary>' +
      '<div class="edRows">' +
        '<div class="edRow"><span class="edRowL">EPS surprise</span><span class="edRowV ' + ((rep.surprise_pct || 0) >= 0 ? "edPos" : "") + '">' + pct(rep.surprise_pct) + "</span></div>" +
        '<div class="edRow"><span class="edRowL">Actual / est</span><span class="edRowV">' + num(rep.actual_eps, 2) + " / " + num(rep.estimate_eps, 2) + "</span></div>" +
        '<div class="edRow"><span class="edRowL">Quality score</span><span class="edRowV">' + num(g.score, 2) + ' <span class="edMuted">(' + gradeSrc + ")</span></span></div>" +
        '<div class="edRow"><span class="edRowL">ADV</span><span class="edRowV">' + money(c.adv_usd) + "</span></div>" +
      "</div>" +
      (g.rationale ? '<div class="edRationale">' + esc(g.rationale) + "</div>" : "") +
      expectedLine(c) +
      optionsBlock(c) +
      "</details>" +
      '<div class="edCardActions">' +
        '<button class="edBtn edBtn--sm edBtn--' + dm.cls + '" data-track="' + esc(c.ticker) + '">Log trade</button>' +
        '<button class="edBtn edBtn--sm" data-evidence="' + esc(c.ticker) + '">Evidence</button>' +
      "</div>" +
    "</div>";
  }

  function renderCards(p) {
    var wrap = $("edCards");
    var empty = $("edEmpty");
    var cands = p.candidates || [];
    $("edCandSub").textContent = cands.length ? (cands.length + " candidate(s) · sorted by sizing then surprise") : "";
    if (!cands.length) {
      wrap.innerHTML = "";
      empty.style.display = "block";
      empty.textContent = p.note || "No qualifying beats in the scan window. The cron rebuilds every weekday at 7:45 ET.";
      return;
    }
    empty.style.display = "none";
    wrap.innerHTML = cands.map(candidateCard).join("");
    Array.prototype.forEach.call(wrap.querySelectorAll("[data-track]"), function (btn) {
      btn.addEventListener("click", function () { trackEntry(btn.getAttribute("data-track")); });
    });
    Array.prototype.forEach.call(wrap.querySelectorAll("[data-evidence]"), function (btn) {
      btn.addEventListener("click", function () { showEvidence(btn.getAttribute("data-evidence")); });
    });
  }

  /* Fetch options expressions for full-size candidates only (informational). */
  function loadOptions(p) {
    var fulls = (p.candidates || []).filter(function (c) { return c.sizing === "full"; });
    fulls.forEach(function (c) {
      if (state.options[c.ticker] !== undefined) return;
      fetch("/api/engine18/options/" + encodeURIComponent(c.ticker))
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (doc) {
          state.options[c.ticker] = (doc && doc.available) ? doc.card : null;
          renderCards(state.payload);
        })
        .catch(function () { state.options[c.ticker] = null; renderCards(state.payload); });
    });
  }

  /* ── Tracker ── */
  function findCandidate(ticker) {
    return ((state.payload || {}).candidates || []).find(function (c) { return c.ticker === ticker; }) || null;
  }

  function trackEntry(ticker) {
    var c = findCandidate(ticker);
    if (!c) return;
    var decision = decisionFor(c);

    // The desk can log any candidate for the record — but committing capital to
    // a NO GO / CAUTION signal is a deliberate manual override, so confirm it.
    var override = decision !== "GO";
    if (override) {
      var why = decision === "NO_GO"
        ? ticker + " is a NO GO (sizing PASS — outside the validated edge)."
        : ticker + " is CAUTION (validated entry has passed; mid-drift entry was never backtested).";
      if (!window.confirm(why + "\n\nLog this as a MANUAL OVERRIDE trade anyway?")) return;
    }

    var px = window.prompt("Entry price for " + ticker + " (fill at " + c.entry_date + " open):", c.last_close != null ? String(c.last_close) : "");
    if (px === null) return;
    var body = {
      ticker: ticker,
      entryDate: c.entry_date,
      plannedExitDate: c.exit_date,
      holdDays: c.hold_days,
      entryPrice: px === "" ? null : parseFloat(px),
      sizing: c.sizing,
      decision: decision,
      mode: override ? "manual_override" : "tracked",
      notes: override ? ("Manual override of " + decision + " verdict.") : "",
      signalSnapshot: c,
    };
    fetch("/api/engine18/trade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function () { setStatus("Logged " + ticker + " drift trade" + (override ? " (manual override)" : "") + "."); loadTrades(); })
      .catch(function (err) { setStatus("Failed to log trade: " + err.message, true); });
  }

  function closeTrade(tradeId, ticker) {
    var px = window.prompt("Exit price for " + ticker + ":");
    if (px === null) return;
    fetch("/api/engine18/trade/" + encodeURIComponent(tradeId) + "/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exitPrice: px === "" ? null : parseFloat(px), reason: "planned_exit" }),
    })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function () { setStatus("Closed " + ticker + "."); loadTrades(); })
      .catch(function (err) { setStatus("Failed to close trade: " + err.message, true); });
  }

  function renderTrades() {
    var body = $("edTrackBody");
    var empty = $("edTrackEmpty");
    var trades = state.trades || [];
    $("edTrackSub").textContent = trades.length
      ? (trades.filter(function (t) { return t.status === "active"; }).length + " open · " + trades.length + " total")
      : "";
    body.innerHTML = "";
    if (!trades.length) { empty.style.display = "block"; return; }
    empty.style.display = "none";
    var today = new Date().toISOString().slice(0, 10);
    trades.forEach(function (t) {
      var out = t.outcome || {};
      var exitDue = t.status === "active" && t.plannedExitDate && t.plannedExitDate <= today;
      var tr = document.createElement("tr");
      var overrideTag = t.mode === "manual_override"
        ? ' <span class="edPill edPill--manual" title="Logged against the desk verdict">OVERRIDE</span>'
        : "";
      tr.innerHTML =
        "<td><b>" + esc(t.ticker) + "</b>" + (t.sizing ? ' <span class="edMuted">' + esc(t.sizing) + "</span>" : "") + overrideTag + "</td>" +
        '<td><span class="edStatus--' + esc(t.status) + '">' + esc(t.status) + "</span></td>" +
        "<td>" + esc(t.entryDate || "—") + "</td>" +
        "<td>" + (exitDue ? '<span class="edExitDue" title="Past the validated 10-trading-day hold — exit now">' + esc(t.plannedExitDate) + " ⚠</span>" : esc(t.plannedExitDate || "—")) + "</td>" +
        '<td class="edNum">' + num(t.entryPrice, 2) + "</td>" +
        '<td class="edNum">' + num(out.exitPrice, 2) + "</td>" +
        '<td class="edNum">' + (out.returnPct != null ? pct(out.returnPct, 2) : "—") + "</td>" +
        "<td>" + (t.status === "active" ? '<button class="edBtn edBtn--sm" data-close="' + esc(t.tradeId) + '" data-tk="' + esc(t.ticker) + '">Close</button>' : "") + "</td>";
      body.appendChild(tr);
    });
    Array.prototype.forEach.call(body.querySelectorAll("[data-close]"), function (btn) {
      btn.addEventListener("click", function () { closeTrade(btn.getAttribute("data-close"), btn.getAttribute("data-tk")); });
    });
  }

  function loadTrades() {
    fetch("/api/engine18/trades")
      .then(function (r) { return r.ok ? r.json() : { trades: [] }; })
      .then(function (doc) { state.trades = doc.trades || []; renderTrades(); })
      .catch(function () { state.trades = []; renderTrades(); });
  }

  /* ── Evidence drill-down (simple alert-style modal-free view) ── */
  function showEvidence(ticker) {
    fetch("/api/engine18/evidence/" + encodeURIComponent(ticker))
      .then(function (r) { return r.json(); })
      .then(function (doc) {
        if (!doc.found) { setStatus("No stored evidence for " + ticker + ".", true); return; }
        var ev = doc.evidence || {};
        var g = ev.grade || {};
        var lines = [
          ticker + " — evidence",
          "Report: " + JSON.stringify(ev.report || {}),
          "LLM score: " + (g.source === "llm" ? g.score : "n/a") + " · heuristic: " + g.heuristic_score + " · quintile " + g.quintile,
          "Rationale: " + (g.rationale || "—"),
          "Transcript: " + (ev.transcriptChars || 0) + " chars",
          "",
          (ev.transcriptExcerpt || "").slice(0, 800),
        ];
        window.alert(lines.join("\n"));
      })
      .catch(function () { setStatus("Failed to load evidence for " + ticker + ".", true); });
  }

  /* ── Advisor ── */
  function generateAdvisor() {
    var btn = $("edAdvisorBtn");
    var el = $("edAdvisor");
    btn.disabled = true;
    el.style.display = "block";
    el.textContent = "Generating desk note…";
    fetch("/api/engine18/advisor", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scanPayload: state.payload }),
    })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (doc) {
        var a = doc.advisor || {};
        el.textContent = a.narrative || "No note generated.";
        if (a._source === "fallback") el.textContent += "\n\n(" + (a._fallback_reason || "LLM unavailable") + ")";
      })
      .catch(function (err) { el.textContent = "Desk note failed: " + err.message; })
      .then(function () { btn.disabled = false; });
  }

  /* ── Manual profile ── */
  function setVerdict(kind, html) {
    var el = $("edProfileVerdict");
    el.className = "edVerdict" + (kind ? " edVerdict--" + kind : "");
    el.innerHTML = html || "";
  }

  function runProfile() {
    var ticker = ($("edProfileTicker").value || "").trim().toUpperCase();
    if (!ticker) { setVerdict("err", "Enter a ticker first."); return; }
    var btn = $("edProfileRun");
    var body = { ticker: ticker };
    var actual = $("edOvActual").value, est = $("edOvEstimate").value;
    if (actual !== "") body.actual_eps = parseFloat(actual);
    if (est !== "") body.estimate_eps = parseFloat(est);
    if ($("edOvDate").value) body.report_date = $("edOvDate").value;
    if ($("edOvTiming").value) body.timing = $("edOvTiming").value;

    btn.disabled = true;
    setVerdict("no", "Profiling " + esc(ticker) + " — report lookup, transcript, quality grade…");
    fetch("/api/engine18/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) {
        return r.json().then(function (doc) {
          if (!r.ok) throw new Error((doc && doc.detail) || ("HTTP " + r.status));
          return doc;
        });
      })
      .then(function (doc) {
        if (doc.verdict === "candidate") {
          var c = doc.candidate || {};
          var late = c.entry_status === "late";
          var decision = decisionFor(c);
          var dm = DECISION_META[decision] || DECISION_META.NO_GO;
          setVerdict(decision === "GO" ? "ok" : "no",
            '<b class="edDec--' + dm.cls + '">' + dm.label + "</b> — " + esc(ticker) + " · " +
            (c.bucket === "beat_large" ? "large beat" : "small beat") +
            ", quality " + esc((c.grade || {}).quintile || "?") +
            ", <b>" + esc(c.sizing || "?").toUpperCase() + " size</b> (LONG)" +
            " · entry " + esc(c.entry_date) + " open · exit " + esc(c.exit_date) +
            " · EPS source: " + esc((doc.source || "").toUpperCase()) +
            (late ? "<br/><b>⚠ " + esc(doc.warning || "Late entry — validated entry point has passed.") + "</b>" : "") +
            "<br/>Added to the candidate list below (tagged MANUAL).");
          load();
        } else {
          var label = doc.verdict === "no_report" ? "No report found"
            : doc.verdict === "illiquid" ? esc(ticker) + " is below the liquidity floor"
            : esc(ticker) + " — not tradable";
          setVerdict("no", "<b>" + label + ".</b> " + esc(doc.reason || ""));
        }
      })
      .catch(function (err) { setVerdict("err", "Profile failed: " + esc(err.message)); })
      .then(function () { btn.disabled = false; });
  }

  /* ── Banner / status / load ── */
  function renderBanner(p) {
    var s = p.summary || {};
    $("edCandidates").textContent = s.candidates != null ? s.candidates : 0;
    $("edActionable").textContent = s.actionable != null ? s.actionable : 0;
    $("edFull").textContent = s.fullSize != null ? s.fullSize : 0;
    $("edHalf").textContent = s.halfSize != null ? s.halfSize : 0;
  }

  function render(p) {
    state.payload = p;
    renderBanner(p);
    renderValidation(p);
    renderCards(p);
    loadOptions(p);
    var when = p.asOf ? new Date(p.asOf).toLocaleString() : "";
    var meta = p.meta || {};
    var regime = meta.regimeContext ? " · regime: " + meta.regimeContext + " (context only)" : "";
    setStatus((p.cached ? "Cached scan" : "Scan") + (when ? " · " + when : "") + regime + (p.note ? " · " + p.note : ""));
    if (window.RavenChat) RavenChat.setEngineContext("engine18", p);
  }

  function load() {
    var btn = $("edRefresh");
    btn.disabled = true;
    setStatus("Loading scan…");
    fetch("/api/engine18")
      .then(function (res) {
        if (res.status === 404) throw new Error("Engine 18 (Earnings Drift) is disabled on this deployment.");
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (p) { render(p); })
      .catch(function (err) { setStatus(err.message || "Failed to load scan.", true); })
      .then(function () { btn.disabled = false; });
  }

  /* Rebuild: one calendar call + a handful of transcript LLM grades — short
     enough to run detached and poll status every few seconds. */
  function rebuild() {
    var btn = $("edRefresh");
    btn.disabled = true;
    setStatus("Starting background rebuild (calendar + transcripts + LLM grades)…");
    fetch("/api/engine18/refresh?background=true", { method: "POST" })
      .then(function (res) {
        if (res.status === 404) throw new Error("Engine 18 (Earnings Drift) is disabled on this deployment.");
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function () { pollRebuild(0); })
      .catch(function (err) { setStatus(err.message || "Failed to start rebuild.", true); btn.disabled = false; });
  }

  function pollRebuild(tries) {
    if (tries > 80) {  // ~7 min safety cap
      setStatus("Rebuild still running — reload shortly to see the fresh scan.");
      $("edRefresh").disabled = false;
      return;
    }
    fetch("/api/engine18/status")
      .then(function (r) { return r.json(); })
      .then(function (s) {
        var lr = s.lastRun || {};
        if (s.backgroundRunning || (lr.ok === false && !lr.finishedAt) || (lr.startedAt && !lr.finishedAt)) {
          setStatus("Rebuilding scan… (" + Math.round(tries * 5) + "s elapsed)");
          setTimeout(function () { pollRebuild(tries + 1); }, 5000);
          return;
        }
        if (lr.ok === false && lr.error) {
          setStatus("Rebuild failed: " + lr.error, true);
          $("edRefresh").disabled = false;
          return;
        }
        setStatus("Rebuild complete — loading fresh scan…");
        load();
      })
      .catch(function () { setTimeout(function () { pollRebuild(tries + 1); }, 5000); });
  }

  function init() {
    $("edRefresh").addEventListener("click", rebuild);
    $("edAdvisorBtn").addEventListener("click", generateAdvisor);
    $("edProfileRun").addEventListener("click", runProfile);
    $("edProfileTicker").addEventListener("keydown", function (e) { if (e.key === "Enter") runProfile(); });
    $("edProfileToggle").addEventListener("click", function () {
      $("edOverrides").classList.toggle("open");
    });
    load();
    loadTrades();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
