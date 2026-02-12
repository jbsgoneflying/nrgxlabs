/* ── Raven-Tech 2.0 – Command Center JS ─────────────────────────────── */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  /* ── Helpers ───────────────────────────────────────────────── */

  function pillClass(label) {
    const l = (label || "").toLowerCase().replace(/[_-]/g, "");
    if (l === "riskon" || l === "tradable" || l === "supportive") return "pill--green";
    if (l === "riskoff" || l === "suppress" || l === "hostile" || l === "stressed") return "pill--red";
    return "pill--amber";
  }

  function barColor(val) {
    if (val >= 65) return "var(--green)";
    if (val <= 35) return "var(--red)";
    return "var(--amber)";
  }

  function changeArrow(delta) {
    if (delta > 0) return { cls: "ccChange--up", text: "+" + delta.toFixed(1) };
    if (delta < 0) return { cls: "ccChange--down", text: delta.toFixed(1) };
    return { cls: "ccChange--flat", text: "—" };
  }

  /* ── Loading overlay helpers ────────────────────────────────── */

  const overlay = $("#ravenOverlay");
  const progressFill = $("#ravenProgressFill");
  const statusText = $("#ravenStatus");

  function showOverlay() {
    if (overlay) overlay.classList.add("isVisible");
  }
  function hideOverlay() {
    if (overlay) overlay.classList.remove("isVisible");
  }
  function setProgress(pct, msg) {
    if (progressFill) progressFill.style.width = pct + "%";
    if (statusText) statusText.textContent = msg || "";
  }

  function setApiLoading(on) {
    document.body.classList.toggle("isApiLoading", on);
  }

  /* ── Flow Pressure ─────────────────────────────────────────── */

  async function loadFlowPressure() {
    try {
      const res = await fetch("/api/command-center/flow-pressure");
      if (!res.ok) throw new Error(res.statusText);
      const data = await res.json();

      const fp = data.flowPressure || {};
      const score = fp.composite_score != null ? fp.composite_score : "--";
      const label = fp.composite_label || "Neutral";

      $("#fpScore").textContent = typeof score === "number" ? Math.round(score) : score;
      const fpPill = $("#fpPill");
      fpPill.textContent = label;
      fpPill.className = "pill " + pillClass(label);

      // Change indicator from SPX
      const spx = (fp.symbols || {}).SPX || {};
      const change = spx.change_since_prior;
      if (change != null) {
        const a = changeArrow(change);
        const el = $("#fpChange");
        el.className = "ccChange " + a.cls;
        el.textContent = a.text;
      }

      // Sub-bars
      const components = spx.components || {};
      const subBars = $("#fpSubBars");
      subBars.innerHTML = "";
      const labels = {
        dealer_gamma: "Dealer Gamma",
        vol_term_structure: "Vol Term Structure",
        em_richness: "EM Richness",
        liquidity: "Liquidity",
        macro_density: "Macro Density",
      };
      for (const [key, lbl] of Object.entries(labels)) {
        const val = components[key] ?? 50;
        const item = document.createElement("div");
        item.className = "ccSubBarItem";
        item.innerHTML = `
          <span class="label">${lbl}</span>
          <span class="bar"><span class="fill" style="width:${val}%;background:${barColor(val)}"></span></span>
          <span class="val">${Math.round(val)}</span>`;
        subBars.appendChild(item);
      }

      // Regime
      const regime = data.regime || {};
      $("#regimeLabel").textContent = regime.label || "—";
      const regimePill = $("#regimePill");
      if (regime.score != null) {
        regimePill.textContent = "Score: " + regime.score;
        regimePill.className = "pill " + pillClass(regime.label);
      }
      const drivers = [];
      if (regime.fx_stress != null) drivers.push("fx stress: " + regime.fx_stress);
      if (regime.iv_stress != null) drivers.push("iv stress: " + regime.iv_stress);
      $("#regimeDrivers").textContent = drivers.join(" · ");

      // Vol state (Engine 5 volLeadLag uses: global_vol_direction, us_iv_state, vol_lag_state, structure_bias)
      const vol = data.volState || {};
      const volDir = vol.global_vol_direction || vol.direction || vol.vol_direction || "—";
      $("#volDirection").textContent = volDir;
      const volParts = [];
      if (vol.us_iv_state) volParts.push("US IV: " + vol.us_iv_state);
      if (vol.vol_lag_state) volParts.push("Lag: " + vol.vol_lag_state);
      if (vol.structure_bias) volParts.push("Bias: " + vol.structure_bias);
      const volNote = volParts.length ? volParts.join(" · ") : "";
      const volInterp = vol.interpretation || vol.narrative || "";
      $("#volDetails").textContent = [volNote, volInterp].filter(Boolean).join(" — ");
    } catch (e) {
      console.error("Flow pressure load failed:", e);
    }
  }

  /* ── Sequencer ──────────────────────────────────────────────── */

  async function loadSequencer() {
    try {
      const res = await fetch("/api/command-center/sequencer");
      if (!res.ok) throw new Error(res.statusText);
      const data = await res.json();

      const days = data.tradingDays || [];
      const seq = data.sequence || {};
      const timeline = seq.timeline || {};
      const patternMatch = seq.matched_pattern || {};

      const container = $("#seqTimeline");
      container.innerHTML = "";
      const dayLabels = ["MON", "TUE", "WED", "THU", "FRI"];
      days.forEach((d, i) => {
        const events = timeline[d] || [];
        const div = document.createElement("div");
        div.className = "seqDay";
        const chips = events.length
          ? events.map((e) => `<span class="seqChip">${e.label || e.field || "?"}</span>`).join("")
          : '<span style="color:var(--muted);font-size:11px;">—</span>';
        div.innerHTML = `<div class="seqDayLabel">${dayLabels[i] || d}</div>${chips}`;
        container.appendChild(div);
      });

      // Pattern match
      if (patternMatch.label) {
        $("#seqPatternName").textContent = patternMatch.label;
        const conf = patternMatch.confidence;
        const caveat = patternMatch.caveat || "";
        $("#seqPatternConf").textContent =
          (conf != null ? "Confidence: " + conf + "%" : "") +
          (caveat ? " - " + caveat : "");
      }

      // Pattern library
      const patterns = data.patterns || {};
      const list = $("#patternList");
      list.innerHTML = "";
      for (const [key, p] of Object.entries(patterns)) {
        const isMatch = patternMatch.key === key;
        const el = document.createElement("div");
        el.className = "patternItem";
        el.innerHTML = `
          <div class="name">${p.label}${isMatch ? ' <span class="pill pill--green" style="font-size:9px;">MATCH</span>' : ""}</div>
          <div class="desc">${p.description || ""}</div>`;
        list.appendChild(el);
      }
    } catch (e) {
      console.error("Sequencer load failed:", e);
    }
  }

  /* ── Tradable Ideas ─────────────────────────────────────────── */

  async function loadIdeas() {
    try {
      const res = await fetch("/api/command-center/tradable-ideas");
      if (!res.ok) throw new Error(res.statusText);
      const data = await res.json();

      const ideas = data.ideas || [];
      const tbody = $("#ideasBody");
      const emptyEl = $("#ideasEmpty");
      tbody.innerHTML = "";

      if (!ideas.length) {
        emptyEl.style.display = "block";
        return;
      }
      emptyEl.style.display = "none";

      ideas.forEach((idea) => {
        const gate = idea.gate || {};
        const status = gate.status || "TRADABLE";
        const reasons = (gate.reasons || []).map((r) => r.label || r.code || "").join(", ");

        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><strong>${idea.ticker || "—"}</strong></td>
          <td>${idea.engine || "—"}</td>
          <td>${idea.setupType || "—"}</td>
          <td>${idea.direction || "—"}</td>
          <td><span class="pill ${pillClass(status)}">${status}</span>${reasons ? '<br><span style="font-size:10px;color:var(--muted);">' + reasons + "</span>" : ""}</td>
          <td>${idea.score || "—"}</td>
          <td style="max-width:180px;">${idea.whyNow || "—"}</td>
          <td style="max-width:180px;">${idea.whatBreaks || "—"}</td>
        `;
        tbody.appendChild(tr);
      });
    } catch (e) {
      console.error("Ideas load failed:", e);
    }
  }

  /* ── Desk Brief ────────────────────────────────────────────── */

  async function loadDeskBrief() {
    try {
      const res = await fetch("/api/command-center/desk-brief");
      if (!res.ok) throw new Error(res.statusText);
      const data = await res.json();
      const brief = data.brief || {};

      $("#briefMarket").textContent = brief.market_state || "Unavailable";
      $("#briefBias").textContent = brief.weekly_bias || "Unavailable";
      $("#briefRisks").textContent = brief.top_risks || "Unavailable";
    } catch (e) {
      console.error("Desk brief load failed:", e);
      $("#briefMarket").textContent = "Failed to load";
    }
  }

  /* ── Run Command Center ─────────────────────────────────────── */

  let isRunning = false;

  async function runCommandCenter() {
    if (isRunning) return;
    isRunning = true;

    const runBtn = $("#runBtn");
    const statusEl = $("#status");
    const resultsEl = $("#results");

    // UI: loading state
    runBtn.classList.add("isLoading");
    runBtn.disabled = true;
    setApiLoading(true);
    showOverlay();
    setProgress(5, "Bootstrapping engines…");
    statusEl.className = "status isRunning";
    statusEl.textContent = "Initializing engines — this may take a moment…";

    try {
      // Step 1: Kick off engine bootstrap
      setProgress(10, "Starting Engine 5 (regime + vol)…");
      await fetch("/api/command-center/init");

      // Step 2: Poll and load data in stages
      setProgress(20, "Loading Flow Pressure…");
      await loadFlowPressure();

      setProgress(35, "Loading Sequencer…");
      await loadSequencer();

      setProgress(50, "Loading Desk Brief…");
      await loadDeskBrief();

      // Step 3: Wait for Engine 3/4 scans to populate (poll tradable ideas)
      setProgress(60, "Waiting for Engine 3 & 4 scans…");
      let ideasLoaded = false;
      for (let attempt = 0; attempt < 24; attempt++) {
        try {
          const ideasRes = await fetch("/api/command-center/tradable-ideas");
          const ideasData = await ideasRes.json();
          if ((ideasData.ideas || []).length > 0) {
            ideasLoaded = true;
            break;
          }
        } catch (_) { /* ignore */ }

        const pct = 60 + Math.min(attempt * 1.5, 30);
        setProgress(pct, `Scanning universes… (${attempt + 1}/24)`);
        await new Promise((r) => setTimeout(r, 10000));
      }

      // Step 4: Final load of all panels
      setProgress(92, "Refreshing all panels…");
      await Promise.all([
        loadFlowPressure(),
        loadSequencer(),
        loadIdeas(),
        loadDeskBrief(),
      ]);

      setProgress(100, "Done");

      // Show results
      resultsEl.classList.remove("hidden");
      statusEl.className = "status isOk";
      statusEl.textContent = ideasLoaded
        ? "Command Center loaded — all engines reporting."
        : "Command Center loaded — tradable ideas may still be populating.";
    } catch (e) {
      console.error("Command Center run failed:", e);
      statusEl.className = "status isError";
      statusEl.textContent = "Error: " + (e.message || "Unknown error");
    } finally {
      hideOverlay();
      setApiLoading(false);
      runBtn.classList.remove("isLoading");
      runBtn.disabled = false;
      isRunning = false;
    }
  }

  /* ── Init (button-triggered only) ───────────────────────────── */

  function init() {
    const runBtn = $("#runBtn");
    if (runBtn) {
      runBtn.addEventListener("click", (e) => {
        e.preventDefault();
        runCommandCenter();
      });
    }

    // Also allow form submit
    const form = $("#ccForm");
    if (form) {
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        runCommandCenter();
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
