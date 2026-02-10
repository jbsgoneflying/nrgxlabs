/**
 * Engine 5 – Global Lead-Lag UI
 *
 * Fetches pre-computed data from the Engine 5 API endpoints
 * and renders regime state, sector biases, and weekly trade ideas.
 */
(function () {
  "use strict";

  // DOM refs
  const refreshBtn = document.getElementById("refreshBtn");
  const pipelineStatus = document.getElementById("pipelineStatus");
  const resultsEl = document.getElementById("results");
  const emptyEl = document.getElementById("emptySection");

  // Regime
  const regimeBanner = document.getElementById("regimeBanner");
  const regimeLabel = document.getElementById("regimeLabel");
  const regimeScore = document.getElementById("regimeScore");
  const regimeMeta = document.getElementById("regimeMeta");
  const stressBarFill = document.getElementById("stressBarFill");
  const stressBarLabel = document.getElementById("stressBarLabel");
  const componentGrid = document.getElementById("componentGrid");

  // Narrative
  const narrativeText = document.getElementById("narrativeText");
  const narrativeMeta = document.getElementById("narrativeMeta");

  // Index bias
  const indexBiasRow = document.getElementById("indexBiasRow");

  // Sector bias
  const sectorBiasGrid = document.getElementById("sectorBiasGrid");
  const sectorMeta = document.getElementById("sectorMeta");

  // Ideas
  const ideasGrid = document.getElementById("ideasGrid");
  const ideasMeta = document.getElementById("ideasMeta");

  // Suppressions
  const suppressionsSection = document.getElementById("suppressionsSection");
  const suppressionList = document.getElementById("suppressionList");

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function show(el) { el && el.classList.remove("hidden"); }
  function hide(el) { el && el.classList.add("hidden"); }

  function setLoading(on) {
    refreshBtn.disabled = on;
    const spinner = refreshBtn.querySelector(".btnSpinner");
    if (spinner) spinner.style.display = on ? "inline-block" : "none";
  }

  function dirClass(dir) {
    if (dir === "bullish") return "bullish";
    if (dir === "bearish") return "bearish";
    return "neutral";
  }

  function regimeClass(label) {
    const l = (label || "").toLowerCase().replace(/[^a-z]/g, "-");
    return "regime-" + l;
  }

  function stressColor(score) {
    if (score < 30) return "rgba(52, 199, 89, 0.85)";
    if (score < 55) return "rgba(255, 204, 0, 0.85)";
    if (score < 75) return "rgba(255, 149, 0, 0.85)";
    return "rgba(255, 59, 48, 0.85)";
  }

  function fmtPct(v) {
    if (v == null) return "—";
    return Number(v).toFixed(1) + "%";
  }

  function fmtNum(v, decimals) {
    if (v == null) return "—";
    return Number(v).toFixed(decimals != null ? decimals : 2);
  }

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
  }

  function structureLabel(s) {
    return (s || "")
      .replace(/_/g, " ")
      .replace(/\b\w/g, c => c.toUpperCase());
  }

  // ---------------------------------------------------------------------------
  // Render functions
  // ---------------------------------------------------------------------------

  function renderRegime(regime) {
    if (!regime) return;

    const label = regime.label || "Unknown";
    const score = regime.score != null ? regime.score : 0;

    regimeLabel.textContent = label;
    regimeScore.textContent = "Score: " + fmtNum(score, 1) + " / 100";

    // Banner class
    regimeBanner.className = "regimeBanner " + regimeClass(label);

    // Stress bar
    stressBarFill.style.width = score + "%";
    stressBarFill.style.background = stressColor(score);
    stressBarLabel.textContent = fmtNum(score, 0);

    // Meta
    const meta = [];
    if (regime.allowed_structures && regime.allowed_structures.length > 0) {
      meta.push("<span class='regimeMetaItem'><b>Allowed:</b> " + regime.allowed_structures.map(s => esc(structureLabel(s))).join(", ") + "</span>");
    } else {
      meta.push("<span class='regimeMetaItem'><b>Allowed:</b> None (suppressed)</span>");
    }
    meta.push("<span class='regimeMetaItem'><b>Size Modifier:</b> " + fmtNum(regime.position_size_modifier, 2) + "x</span>");
    if (regime.suppression_flags && regime.suppression_flags.length > 0) {
      meta.push("<span class='regimeMetaItem'><b>Flags:</b> " + regime.suppression_flags.map(esc).join(", ") + "</span>");
    }
    regimeMeta.innerHTML = meta.join("");

    // Components
    const comps = regime.components || {};
    const items = [
      { label: "FX Stress", key: "fx_stress" },
      { label: "Yield Stress", key: "yield_stress" },
      { label: "Commodity Stress", key: "commodity_stress" },
      { label: "IV Stress", key: "iv_stress" },
    ];
    componentGrid.innerHTML = items.map(function (it) {
      const val = comps[it.key];
      return "<div class='componentPill'>" +
        "<div class='componentPillLabel'>" + esc(it.label) + "</div>" +
        "<div class='componentPillValue' style='color:" + stressColor(val || 0) + "'>" + fmtNum(val, 1) + "</div>" +
        "</div>";
    }).join("");
  }

  function renderNarrative(summary, week) {
    if (!summary) { narrativeText.textContent = "—"; return; }
    const parts = [];
    if (summary.dominantTheme) parts.push("<b>Theme:</b> " + esc(summary.dominantTheme));
    if (summary.leadersActive != null) parts.push("<b>Active Leaders:</b> " + summary.leadersActive);
    if (summary.leadersConfirming != null) parts.push("<b>Confirming:</b> " + summary.leadersConfirming);
    narrativeText.innerHTML = parts.join(" &middot; ") + (summary.narrative ? "<br/>" + esc(summary.narrative) : "");
    narrativeMeta.textContent = week || "";
  }

  function renderIndexBiases(biases) {
    if (!biases || biases.length === 0) { indexBiasRow.innerHTML = "<span class='muted'>No index bias data</span>"; return; }
    indexBiasRow.innerHTML = biases.map(function (b) {
      return "<div class='indexBiasChip'>" +
        "<span class='idx'>" + esc(b.index) + "</span>" +
        "<span class='dir biasCardDir " + dirClass(b.direction) + "'>" + esc(b.direction) + "</span>" +
        "<span class='conf'>" + b.confidence + "%</span>" +
        (b.note ? "<span style='font-size:11px;color:var(--muted);margin-left:4px;'>" + esc(b.note) + "</span>" : "") +
        "</div>";
    }).join("");
  }

  function renderSectorBiases(biases) {
    if (!biases || biases.length === 0) {
      sectorBiasGrid.innerHTML = "<div class='emptyState'><div class='emptyStateBody'>No sector bias signals available for this period.</div></div>";
      sectorMeta.textContent = "0 sectors";
      return;
    }
    sectorMeta.textContent = biases.length + " sector" + (biases.length !== 1 ? "s" : "");
    sectorBiasGrid.innerHTML = biases.map(function (b) {
      var srcHtml = "";
      if (b.sources && b.sources.length > 0) {
        srcHtml = "<div class='biasCardSources'>" + b.sources.map(esc).join("<br/>") + "</div>";
      }
      return "<div class='biasCard'>" +
        "<div class='biasCardHeader'>" +
          "<div><span class='biasCardSymbol'>" + esc(b.sector) + "</span> <span class='biasCardName'>" + esc(b.name) + "</span></div>" +
          "<span class='biasCardDir " + dirClass(b.direction) + "'>" + esc(b.direction) + "</span>" +
        "</div>" +
        "<div class='biasCardRow'><span class='k'>Confidence</span><span class='v'>" + b.confidence + "%</span></div>" +
        "<div class='biasCardRow'><span class='k'>Vol Bias</span><span class='v'>" + esc(b.volBias || "—") + "</span></div>" +
        srcHtml +
        "</div>";
    }).join("");
  }

  function renderTradeIdeas(ideas) {
    if (!ideas || ideas.length === 0) {
      ideasGrid.innerHTML = "<div class='emptyState'><div class='emptyStateBody'>No trade ideas generated this period. This may be due to insufficient signal strength or regime suppression.</div></div>";
      ideasMeta.textContent = "0 ideas";
      return;
    }
    ideasMeta.textContent = ideas.length + " idea" + (ideas.length !== 1 ? "s" : "");
    ideasGrid.innerHTML = ideas.map(function (idea) {
      var suppBadge = idea.suppressed ? " <span class='suppressedBadge'>Suppressed</span>" : "";
      var cardClass = "ideaCard" + (idea.suppressed ? " suppressed" : "");

      var rows = [];
      rows.push("<div class='ideaCardRow'><span class='k'>Direction</span><span class='v biasCardDir " + dirClass(idea.directionalLean) + "'>" + esc(idea.directionalLean) + "</span></div>");
      rows.push("<div class='ideaCardRow'><span class='k'>Confidence</span><span class='v'>" + idea.confidence + "%</span></div>");
      rows.push("<div class='ideaCardRow'><span class='k'>Regime</span><span class='v'>" + esc(idea.regimeContext) + "</span></div>");
      if (idea.ivRank != null) rows.push("<div class='ideaCardRow'><span class='k'>IV Rank</span><span class='v'>" + fmtPct(idea.ivRank * 100) + "</span></div>");
      if (idea.expectedMove != null) rows.push("<div class='ideaCardRow'><span class='k'>Exp Move</span><span class='v'>" + fmtPct(idea.expectedMove) + "</span></div>");
      if (idea.rocEstimateModel) rows.push("<div class='ideaCardRow'><span class='k'>ROC Est.</span><span class='v'>" + esc(idea.rocEstimateModel) + "</span></div>");
      if (idea.maxRiskEstimate) rows.push("<div class='ideaCardRow'><span class='k'>Max Risk</span><span class='v'>" + esc(idea.maxRiskEstimate) + "</span></div>");

      var notesHtml = "";
      if (idea.notes && idea.notes.length > 0) {
        notesHtml = "<div class='ideaCardNotes'>" + idea.notes.map(esc).join("<br/>") + "</div>";
      }

      var sourceHtml = "";
      if (idea.leadLagSource) {
        sourceHtml = "<div class='biasCardSources'><b>Source:</b> " + esc(idea.leadLagSource) + "</div>";
      }

      return "<div class='" + cardClass + "'>" +
        "<div class='ideaCardHeader'>" +
          "<span class='ideaCardSymbol'>" + esc(idea.symbol) + suppBadge + "</span>" +
          "<span class='ideaCardStructure'>" + esc(structureLabel(idea.structure)) + "</span>" +
        "</div>" +
        "<div class='ideaCardBody'>" + rows.join("") + "</div>" +
        sourceHtml +
        notesHtml +
        "</div>";
    }).join("");
  }

  function renderSuppressions(suppressions) {
    if (!suppressions || suppressions.length === 0) {
      hide(suppressionsSection);
      return;
    }
    show(suppressionsSection);
    suppressionList.innerHTML = suppressions.map(function (s) {
      return "<div class='suppressionItem'>" +
        "<span class='sym'>" + esc(s.symbol) + "</span>" +
        "<span class='reason'>" + esc(s.reason) + "</span>" +
        "</div>";
    }).join("");
  }

  // ---------------------------------------------------------------------------
  // Fetch & render
  // ---------------------------------------------------------------------------

  async function loadData() {
    setLoading(true);
    pipelineStatus.textContent = "Loading...";
    hide(resultsEl);
    hide(emptyEl);

    try {
      const resp = await fetch("/api/engine5/weekly-ideas");

      if (resp.status === 404) {
        show(emptyEl);
        pipelineStatus.textContent = "Engine 5 is disabled or no data available.";
        return;
      }
      if (!resp.ok) {
        const errText = await resp.text();
        pipelineStatus.textContent = "Error: " + resp.status + " — " + errText;
        show(emptyEl);
        return;
      }

      const data = await resp.json();

      // Render regime
      renderRegime(data.regime);

      // Render narrative
      renderNarrative(data.globalSignalSummary, data.week);

      // Render index biases
      renderIndexBiases(data.indexBiases);

      // Render sector biases
      renderSectorBiases(data.sectorBiases);

      // Render trade ideas
      renderTradeIdeas(data.tradeIdeas);

      // Render suppressions
      renderSuppressions(data.suppressions);

      // Pipeline status
      var statusParts = [];
      statusParts.push("Week: " + (data.week || "—"));
      statusParts.push("Generated: " + (data.generatedAt ? new Date(data.generatedAt).toLocaleString() : "—"));
      if (data.pipelineStatus) {
        statusParts.push("Pipeline: " + (data.pipelineStatus.status || "—"));
      }
      pipelineStatus.textContent = statusParts.join("  ·  ");

      show(resultsEl);

    } catch (err) {
      console.error("Engine 5 load error:", err);
      pipelineStatus.textContent = "Network error: " + err.message;
      show(emptyEl);
    } finally {
      setLoading(false);
    }
  }

  // ---------------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------------

  refreshBtn.addEventListener("click", function (e) {
    e.preventDefault();
    loadData();
  });

  // Auto-load on page open
  loadData();
})();
