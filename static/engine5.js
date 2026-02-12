/**
 * Engine 5 – Global Lead-Lag UI
 *
 * Fetches pre-computed data from the Engine 5 API endpoints
 * and renders regime state, sector biases, and weekly trade ideas.
 */
(function () {
  "use strict";

  // DOM refs
  const snapshotBtn = document.getElementById("snapshotBtn");
  const runUpdateBtn = document.getElementById("runUpdateBtn");
  const pipelineStatus = document.getElementById("pipelineStatus");
  const resultsEl = document.getElementById("results");
  const emptyEl = document.getElementById("emptySection");

  // Snapshot metadata strip
  const snapshotMetaEl = document.getElementById("snapshotMeta");
  const snapshotGradeEl = document.getElementById("snapshotGrade");
  const snapshotCreatedEl = document.getElementById("snapshotCreated");
  const snapshotAsofEl = document.getElementById("snapshotAsof");
  const snapshotWarningEl = document.getElementById("snapshotWarning");

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

  // Vol Lead-Lag
  const volLeadLagSection = document.getElementById("volLeadLagSection");
  const volScoreBarFill = document.getElementById("volScoreBarFill");
  const volScoreVal = document.getElementById("volScoreVal");
  const volPillRow = document.getElementById("volPillRow");
  const volBiasText = document.getElementById("volBiasText");

  // Transition triggers
  const triggerPanel = document.getElementById("triggerPanel");
  const triggerDriverRow = document.getElementById("triggerDriverRow");
  const triggerFlipRow = document.getElementById("triggerFlipRow");
  const triggerProximity = document.getElementById("triggerProximity");
  const triggerBoundary = document.getElementById("triggerBoundary");

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function show(el) { el && el.classList.remove("hidden"); }
  function hide(el) { el && el.classList.add("hidden"); }

  function setLoading(on, btn) {
    if (btn) {
      btn.disabled = on;
      var spinner = btn.querySelector(".btnSpinner");
      if (spinner) spinner.style.display = on ? "inline-block" : "none";
    }
    // Disable both buttons during any operation
    if (snapshotBtn) snapshotBtn.disabled = on;
    if (runUpdateBtn) runUpdateBtn.disabled = on;
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

  function renderTransitionTriggers(regime) {
    var tt = regime && regime.transitionTriggers;
    if (!tt) { hide(triggerPanel); return; }
    show(triggerPanel);

    // Top drivers
    var drivers = tt.top_drivers || [];
    triggerDriverRow.innerHTML = drivers.map(function (d) {
      return "<div class='triggerDriverPill'>" +
        "<span>" + esc(d.name) + "</span>" +
        "<span class='driverVal' style='color:" + stressColor(d.value || 0) + "'>" + fmtNum(d.value, 1) + "</span>" +
        "</div>";
    }).join("");

    // Flip conditions
    var flipUp = tt.flip_up_conditions || [];
    var flipDown = tt.flip_down_conditions || [];
    var flipHtml = "";
    if (flipUp.length > 0) {
      flipHtml += "<div class='triggerFlipBox flip-up'>" +
        "<div class='triggerFlipBoxTitle'>Flip Up</div>" +
        "<ul>" + flipUp.map(function (c) { return "<li>" + esc(c) + "</li>"; }).join("") + "</ul>" +
        "</div>";
    }
    if (flipDown.length > 0) {
      flipHtml += "<div class='triggerFlipBox flip-down'>" +
        "<div class='triggerFlipBoxTitle'>Flip Down</div>" +
        "<ul>" + flipDown.map(function (c) { return "<li>" + esc(c) + "</li>"; }).join("") + "</ul>" +
        "</div>";
    }
    triggerFlipRow.innerHTML = flipHtml;

    // Proximity flags
    var flags = tt.proximity_flags || [];
    if (flags.length > 0) {
      triggerProximity.innerHTML = flags.map(function (f) {
        var cls = f.replace(/_/g, "-");
        var label = f.replace(/_/g, " ").replace(/\b\w/g, function (c) { return c.toUpperCase(); });
        return "<span class='proximityTag " + cls + "'>" + esc(label) + "</span>";
      }).join("");
    } else {
      triggerProximity.innerHTML = "";
    }

    // Boundary distances
    var dists = tt.boundary_distances || {};
    var distKeys = Object.keys(dists);
    if (distKeys.length > 0) {
      triggerBoundary.innerHTML = distKeys.map(function (k) {
        var v = dists[k];
        var label = k.replace(/_/g, " ").replace(/\b\w/g, function (c) { return c.toUpperCase(); });
        var sign = v >= 0 ? "+" : "";
        return "<div class='boundaryDistItem'>" +
          "<span class='bdLabel'>" + esc(label) + ":</span> " +
          "<span class='bdVal'>" + sign + fmtNum(v, 1) + "</span>" +
          "</div>";
      }).join("");
    } else {
      triggerBoundary.innerHTML = "";
    }
  }

  function volStateClass(state) {
    if (state === "UNDERPRICED_RISK") return "vol-underpriced";
    if (state === "OVERPRICED_RISK") return "vol-overpriced";
    if (state === "CONFIRMED_STRESS") return "vol-confirmed";
    return "vol-normal";
  }

  function volStateLabel(state) {
    if (state === "UNDERPRICED_RISK") return "UNDERPRICED RISK";
    if (state === "OVERPRICED_RISK") return "OVERPRICED RISK";
    if (state === "CONFIRMED_STRESS") return "CONFIRMED STRESS";
    return "NORMAL";
  }

  function ivStateClass(state) {
    if (state === "LOW") return "iv-low";
    if (state === "HIGH") return "iv-high";
    return "iv-neutral";
  }

  function volScoreColor(score) {
    if (score > 0.75) return "rgba(255, 59, 48, 0.85)";
    if (score > 0.4) return "rgba(255, 149, 0, 0.85)";
    if (score < -0.75) return "rgba(52, 199, 89, 0.85)";
    if (score < -0.4) return "rgba(52, 199, 89, 0.65)";
    return "rgba(11, 11, 15, 0.25)";
  }

  function renderVolLeadLag(data) {
    var vll = data && data.volLeadLag;
    if (!vll) { hide(volLeadLagSection); return; }
    show(volLeadLagSection);

    var score = vll.global_vol_score || 0;
    var direction = vll.global_vol_direction || "flat";
    var usState = vll.us_iv_state || "NEUTRAL";
    var lagState = vll.vol_lag_state || "NORMAL";
    var suppressed = vll.suppressed || false;
    var bias = vll.structure_bias || "";
    var swMult = vll.strike_width_multiplier || 1.0;
    var szMult = vll.vol_size_multiplier || 1.0;
    var components = vll.components || {};

    // Score bar: centered at 50%, extend left (negative/compressing) or right (positive/expanding)
    var pct = Math.min(Math.abs(score) / 3.0 * 50, 50); // 0-50% half-width
    var color = volScoreColor(score);
    if (score >= 0) {
      volScoreBarFill.style.left = "50%";
      volScoreBarFill.style.width = pct + "%";
    } else {
      volScoreBarFill.style.left = (50 - pct) + "%";
      volScoreBarFill.style.width = pct + "%";
    }
    volScoreBarFill.style.background = color;
    volScoreVal.textContent = (score >= 0 ? "+" : "") + fmtNum(score, 2);
    volScoreVal.style.color = color;

    // Pills row: US IV State + Vol Lag State + modifiers
    var pills = [];
    pills.push("<span class='volStateBadge " + (suppressed ? "vol-suppressed" : volStateClass(lagState)) + "'>" +
      (suppressed ? "SUPPRESSED" : volStateLabel(lagState)) + "</span>");
    pills.push("<span class='volPill'>US IV: <span class='volPillVal volIvPill " + ivStateClass(usState) + "'>" + esc(usState) + "</span></span>");
    pills.push("<span class='volPill'>Direction: <span class='volPillVal'>" + esc(direction) + "</span></span>");
    if (swMult !== 1.0) {
      pills.push("<span class='volPill'>Strike Width: <span class='volPillVal'>" + fmtNum(swMult, 2) + "x</span></span>");
    }
    if (szMult !== 1.0) {
      pills.push("<span class='volPill'>Size Modifier: <span class='volPillVal'>" + fmtNum(szMult, 2) + "x</span></span>");
    }

    // Component z-scores
    var compKeys = Object.keys(components);
    if (compKeys.length > 0) {
      compKeys.forEach(function (k) {
        var z = components[k];
        pills.push("<span class='volPill'>" + esc(k) + ": <span class='volPillVal' style='color:" + volScoreColor(z) + "'>" +
          (z >= 0 ? "+" : "") + fmtNum(z, 2) + "</span></span>");
      });
    }
    volPillRow.innerHTML = pills.join("");

    // Bias text
    if (suppressed && vll.suppression_reason) {
      volBiasText.textContent = vll.suppression_reason;
    } else if (bias) {
      volBiasText.textContent = bias;
    } else {
      volBiasText.textContent = "";
    }
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

  function invBadgeClass(status) {
    if (status === "HARD") return "inv-hard";
    if (status === "SOFT") return "inv-soft";
    return "inv-valid";
  }

  function invActionsClass(status) {
    if (status === "HARD") return "inv-actions-hard";
    if (status === "SOFT") return "inv-actions-soft";
    return "inv-actions-valid";
  }

  function renderTradeIdeas(ideas) {
    if (!ideas || ideas.length === 0) {
      ideasGrid.innerHTML = "<div class='emptyState'><div class='emptyStateBody'>No trade ideas generated this period. This may be due to insufficient signal strength or regime suppression.</div></div>";
      ideasMeta.textContent = "0 ideas";
      return;
    }
    ideasMeta.textContent = ideas.length + " idea" + (ideas.length !== 1 ? "s" : "");
    ideasGrid.innerHTML = ideas.map(function (idea) {
      var invStatus = idea.invalidationStatus || "VALID";
      var ideaVolState = idea.volLagState || null;
      var suppBadge = idea.suppressed ? " <span class='suppressedBadge'>Suppressed</span>" : "";
      var invBadge = " <span class='invBadge " + invBadgeClass(invStatus) + "'>" + esc(invStatus) + "</span>";
      var volBadge = (ideaVolState && ideaVolState !== "NORMAL") ?
        " <span class='volStateBadge " + volStateClass(ideaVolState) + "' style='font-size:9px;padding:2px 7px;'>" + volStateLabel(ideaVolState) + "</span>" : "";
      var cardClass = "ideaCard" + (idea.suppressed ? " suppressed" : "") + (invStatus === "HARD" ? " inv-hard-card" : "");

      var rows = [];
      rows.push("<div class='ideaCardRow'><span class='k'>Direction</span><span class='v biasCardDir " + dirClass(idea.directionalLean) + "'>" + esc(idea.directionalLean) + "</span></div>");
      rows.push("<div class='ideaCardRow'><span class='k'>Confidence</span><span class='v'>" + idea.confidence + "%</span></div>");
      rows.push("<div class='ideaCardRow'><span class='k'>Regime</span><span class='v'>" + esc(idea.regimeContext) + "</span></div>");
      if (idea.sourceDriver) rows.push("<div class='ideaCardRow'><span class='k'>Driver</span><span class='v'>" + esc(idea.sourceDriver) + "</span></div>");
      if (idea.ivRank != null) rows.push("<div class='ideaCardRow'><span class='k'>IV Rank</span><span class='v'>" + fmtPct(idea.ivRank * 100) + "</span></div>");
      if (idea.expectedMove != null) rows.push("<div class='ideaCardRow'><span class='k'>Exp Move</span><span class='v'>" + fmtPct(idea.expectedMove) + "</span></div>");
      if (idea.rocEstimateModel) rows.push("<div class='ideaCardRow'><span class='k'>ROC Est.</span><span class='v'>" + esc(idea.rocEstimateModel) + "</span></div>");
      if (idea.maxRiskEstimate) rows.push("<div class='ideaCardRow'><span class='k'>Max Risk</span><span class='v'>" + esc(idea.maxRiskEstimate) + "</span></div>");
      if (idea.strikeWidthMultiplier != null && idea.strikeWidthMultiplier !== 1.0) rows.push("<div class='ideaCardRow'><span class='k'>Strike Width</span><span class='v'>" + fmtNum(idea.strikeWidthMultiplier, 2) + "x</span></div>");
      if (idea.volSizeMultiplier != null && idea.volSizeMultiplier !== 1.0) rows.push("<div class='ideaCardRow'><span class='k'>Vol Size Mod</span><span class='v'>" + fmtNum(idea.volSizeMultiplier, 2) + "x</span></div>");

      // Invalidation rules section
      var invHtml = "";
      var hasInvData = idea.invalidationPriceLevel != null || idea.invalidationDeltaThreshold != null || idea.invalidationDriverRule;
      if (hasInvData) {
        var invRows = [];
        if (idea.invalidationPriceLevel != null) {
          var distStr = idea.invalidationPriceDistancePct != null ? " (" + fmtPct(idea.invalidationPriceDistancePct * 100) + " away)" : "";
          invRows.push("<div class='invRuleRow'><span class='invRuleLabel'>Price</span><span class='invRuleVal'>Invalidate if close " +
            (idea.directionalLean === "bearish" ? ">= " : "<= ") +
            fmtNum(idea.invalidationPriceLevel, 2) + distStr + "</span></div>");
        }
        if (idea.invalidationDeltaThreshold != null) {
          invRows.push("<div class='invRuleRow'><span class='invRuleLabel'>Delta</span><span class='invRuleVal'>Invalidate if |delta| >= " +
            fmtNum(idea.invalidationDeltaThreshold, 2) + "</span></div>");
        }
        if (idea.invalidationDriverRule) {
          invRows.push("<div class='invRuleRow'><span class='invRuleLabel'>Driver</span><span class='invRuleVal'>" + esc(idea.invalidationDriverRule) + "</span></div>");
        }
        if (idea.invalidationTestsTriggered && idea.invalidationTestsTriggered.length > 0) {
          invRows.push("<div class='invRuleRow'><span class='invRuleLabel'>Fired</span><span class='invRuleVal'>" +
            idea.invalidationTestsTriggered.map(esc).join(", ") + "</span></div>");
        }
        invHtml = "<div class='invRulesSection'>" + invRows.join("") + "</div>";
      }

      // Action guidance
      var actionsHtml = "";
      if (idea.invalidationActions && idea.invalidationActions.length > 0) {
        actionsHtml = "<div class='invActionsRow " + invActionsClass(invStatus) + "'>" +
          idea.invalidationActions.map(esc).join(" ") + "</div>";
      }

      var notesHtml = "";
      var allNotes = (idea.notes && idea.notes.length > 0) ? idea.notes.slice() : [];
      if (idea.structureBiasReason && ideaVolState && ideaVolState !== "NORMAL") {
        allNotes.push("Vol: " + idea.structureBiasReason);
      }
      if (allNotes.length > 0) {
        notesHtml = "<div class='ideaCardNotes'>" + allNotes.map(esc).join("<br/>") + "</div>";
      }

      var sourceHtml = "";
      if (idea.leadLagSource) {
        sourceHtml = "<div class='biasCardSources'><b>Source:</b> " + esc(idea.leadLagSource) + "</div>";
      }

      return "<div class='" + cardClass + "'>" +
        "<div class='ideaCardHeader'>" +
          "<span class='ideaCardSymbol'>" + esc(idea.symbol) + suppBadge + invBadge + volBadge + "</span>" +
          "<span class='ideaCardStructure'>" + esc(structureLabel(idea.structure)) + "</span>" +
        "</div>" +
        "<div class='ideaCardBody'>" + rows.join("") + "</div>" +
        invHtml +
        actionsHtml +
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

  // ---------------------------------------------------------------------------
  // Render results from API response
  // ---------------------------------------------------------------------------

  function renderSnapshotMeta(meta) {
    if (!meta || !snapshotMetaEl) return;
    show(snapshotMetaEl);

    // Grade badge
    var grade = meta.grade || "C";
    var gradeLabel = meta.gradeLabel || "";
    snapshotGradeEl.className = "snapshotGradeBadge grade-" + grade;
    snapshotGradeEl.textContent = "Grade " + grade + (gradeLabel ? " — " + gradeLabel : "");

    // Created time
    if (meta.createdAt) {
      try {
        var d = new Date(meta.createdAt);
        snapshotCreatedEl.textContent = "Built " + d.toLocaleString(undefined, {
          month: "short", day: "numeric", hour: "numeric", minute: "2-digit", hour12: true
        });
      } catch (_) {
        snapshotCreatedEl.textContent = "Built " + meta.createdAt;
      }
    } else {
      snapshotCreatedEl.textContent = "";
    }

    // As-of dates
    var asof = meta.asofDates || {};
    var parts = [];
    if (asof.us) parts.push("US: " + asof.us);
    if (asof.eu) parts.push("EU: " + asof.eu);
    if (asof.asia) parts.push("Asia: " + asof.asia);
    if (asof.au) parts.push("AU: " + asof.au);
    snapshotAsofEl.textContent = parts.join(" | ");

    // Warning
    if (meta.warning) {
      snapshotWarningEl.textContent = meta.warning;
      show(snapshotWarningEl);
    } else {
      hide(snapshotWarningEl);
    }
  }

  function renderAll(data) {
    // Render snapshot metadata strip if present
    if (data.meta) {
      renderSnapshotMeta(data.meta);
    }

    renderRegime(data.regime);
    renderTransitionTriggers(data.regime);
    renderVolLeadLag(data);
    renderNarrative(data.globalSignalSummary, data.week);
    renderIndexBiases(data.indexBiases);
    renderSectorBiases(data.sectorBiases);
    renderTradeIdeas(data.tradeIdeas);
    renderSuppressions(data.suppressions);

    var statusParts = [];
    statusParts.push("Week: " + (data.week || "—"));
    if (data.meta && data.meta.snapshotId) {
      statusParts.push("Snapshot: " + data.meta.snapshotId);
    }
    if (data.generatedAt) {
      statusParts.push("Generated: " + new Date(data.generatedAt).toLocaleString());
    }
    pipelineStatus.textContent = statusParts.join("  ·  ");
    show(resultsEl);
  }

  // ---------------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------------

  // Track last-rendered snapshot metadata for comparison after "Run Update"
  var lastRenderedMeta = null;

  // ---------------------------------------------------------------------------
  // Comparison banner: when a new "Run Update" snapshot is weaker, offer
  // a one-click path back to the best historical snapshot.
  // ---------------------------------------------------------------------------

  var comparisonBanner = null;

  function ensureComparisonBanner() {
    if (comparisonBanner) return comparisonBanner;
    comparisonBanner = document.createElement("div");
    comparisonBanner.className = "snapshotMeta";
    comparisonBanner.style.cssText =
      "background:rgba(255,149,0,0.08);border-color:rgba(255,149,0,0.25);margin-top:8px;display:none;align-items:center;gap:10px;flex-wrap:wrap;";
    // Insert after snapshotMeta
    if (snapshotMetaEl && snapshotMetaEl.parentNode) {
      snapshotMetaEl.parentNode.insertBefore(comparisonBanner, snapshotMetaEl.nextSibling);
    }
    return comparisonBanner;
  }

  function showComparisonBanner(bestMeta) {
    var banner = ensureComparisonBanner();
    var bestIdeas = bestMeta.tradeIdeasCount || 0;
    var bestGrade = bestMeta.grade || "?";
    var bestId = bestMeta.snapshotId || "unknown";
    var bestCreated = bestMeta.createdAt
      ? new Date(bestMeta.createdAt).toLocaleString()
      : "unknown";

    banner.innerHTML =
      '<span style="font-size:11px;font-weight:700;color:#995c00;">This run has fewer results than the best snapshot on file.</span>' +
      '<span class="snapshotMetaSep">|</span>' +
      '<span style="font-size:11px;color:var(--muted);">Best: <b>Grade ' + esc(bestGrade) + '</b> · ' +
      bestIdeas + ' ideas · ' + esc(bestCreated) + '</span>' +
      '<button id="loadBestBtn" class="primaryButton" type="button" ' +
      'style="font-size:11px;padding:4px 12px;margin-left:auto;">Load Best Snapshot</button>';

    banner.style.display = "flex";

    var loadBestBtn = document.getElementById("loadBestBtn");
    if (loadBestBtn) {
      loadBestBtn.addEventListener("click", function (e) {
        e.preventDefault();
        banner.style.display = "none";
        getSnapshot("best");
      });
    }
  }

  function hideComparisonBanner() {
    if (comparisonBanner) comparisonBanner.style.display = "none";
  }

  // ---------------------------------------------------------------------------
  // Enhanced getSnapshot with comparison logic
  // ---------------------------------------------------------------------------

  var _originalRenderAll = renderAll;
  renderAll = function (data) {
    lastRenderedMeta = data.meta || null;
    _originalRenderAll(data);
  };

  async function getSnapshot(view) {
    var btn = (view === "run") ? runUpdateBtn : snapshotBtn;
    var isRun = (view === "run");

    // Remember the best snapshot's meta before running update
    var priorBestMeta = (isRun && lastRenderedMeta) ? Object.assign({}, lastRenderedMeta) : null;

    hideComparisonBanner();
    setLoading(true, btn);
    hide(resultsEl);
    hide(emptyEl);
    hide(snapshotMetaEl);
    pipelineStatus.textContent = isRun ? "Running pipeline..." : "Loading snapshot...";

    // For explicit run or if request takes long, show the Raven loading overlay
    if (isRun && window.RavenLoading) {
      window.RavenLoading.show({
        status: "Running full pipeline...",
        expectedLoadMs: 30000,
        clearResults: false,
      });
    }

    // Start a timer so if the request takes > 2s we show loading overlay
    var slowTimer = null;
    if (!isRun) {
      slowTimer = setTimeout(function () {
        if (window.RavenLoading) {
          window.RavenLoading.show({
            status: "Bootstrapping pipeline (first run)...",
            expectedLoadMs: 30000,
            clearResults: false,
          });
        }
      }, 2000);
    }

    try {
      var url = "/api/engine5/weekly-ideas";
      if (view && view !== "best") {
        url += "?view=" + encodeURIComponent(view);
      }

      var resp = await fetch(url);

      // Clear the slow timer
      if (slowTimer) clearTimeout(slowTimer);

      if (resp.status === 404) {
        if (window.RavenLoading) window.RavenLoading.hide();
        show(emptyEl);
        pipelineStatus.textContent = "Engine 5 is not enabled or no data available.";
        return;
      }

      if (!resp.ok) {
        if (window.RavenLoading) window.RavenLoading.hide();
        var errText = await resp.text();
        pipelineStatus.textContent = "Error: " + resp.status + " — " + errText;
        show(emptyEl);
        return;
      }

      if (window.RavenLoading) {
        window.RavenLoading.setProgress(95, "Rendering results...");
      }

      var data = await resp.json();
      renderAll(data);

      // After "Run Update", compare with best snapshot and show banner if worse
      if (isRun && data.meta) {
        var newIdeas = (data.tradeIdeas || []).length;
        var newGrade = data.meta.grade || "C";

        // Check the embedded bestSnapshotMeta from the API (if present)
        var bestMeta = data.meta.bestSnapshotMeta || priorBestMeta;
        if (bestMeta) {
          var bestIdeas = bestMeta.tradeIdeasCount || 0;
          var bestGrade = bestMeta.grade || "C";

          // Show banner if new snapshot has fewer ideas or worse grade
          var newGradeVal = newGrade === "A" ? 3 : (newGrade === "B" ? 2 : 1);
          var bestGradeVal = bestGrade === "A" ? 3 : (bestGrade === "B" ? 2 : 1);

          if ((bestIdeas > newIdeas && bestIdeas > 0) || (bestGradeVal > newGradeVal && bestIdeas > newIdeas)) {
            showComparisonBanner(bestMeta);
          }
        }
      }

    } catch (err) {
      if (slowTimer) clearTimeout(slowTimer);
      console.error("Engine 5 load error:", err);
      pipelineStatus.textContent = "Network error: " + err.message;
      show(emptyEl);
    } finally {
      if (window.RavenLoading) window.RavenLoading.hide();
      setLoading(false, btn);
    }
  }

  // ---------------------------------------------------------------------------
  // Init — auto-load best snapshot on page open
  // ---------------------------------------------------------------------------

  if (snapshotBtn) {
    snapshotBtn.addEventListener("click", function (e) {
      e.preventDefault();
      getSnapshot("best");
    });
  }

  if (runUpdateBtn) {
    runUpdateBtn.addEventListener("click", function (e) {
      e.preventDefault();
      getSnapshot("run");
    });
  }

  // Auto-load best snapshot on page open so the user immediately sees the
  // best available analysis without having to click anything.
  getSnapshot("best");
})();
