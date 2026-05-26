/* ═══════════════════════════════════════════════════════════════════════
   Raven-Tech · Market Intelligence
   Front Layer: synthesizes all engines into a daily roadmap.
   Read-only. No trade recommendations.
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  /* ── DOM refs ─────────────────────────────────────── */
  var refreshBtn       = document.getElementById("miRefreshBtn");
  var refreshBanner    = document.getElementById("miRefreshBanner");
  var overlay          = document.getElementById("ravenOverlay");
  var progressFill     = document.getElementById("ravenProgressFill");
  var statusLabel      = document.getElementById("ravenStatus");

  // Layout rows
  var topRow           = document.getElementById("miTopRow");
  var briefRow         = document.getElementById("miBriefRow");
  var bottomGrid       = document.getElementById("miBottomGrid");
  var asymRow          = document.getElementById("miAsymRow");
  var asymCard         = document.getElementById("miAsymCard");
  var diffPanel        = document.getElementById("miDiffPanel");

  // Card internals
  var regimeScore      = document.getElementById("miRegimeScore");
  var regimeLabel      = document.getElementById("miRegimeLabel");
  // regimeTs removed — timestamp no longer shown in hero card
  var engineGates      = document.getElementById("miEngineGates");
  var volState         = document.getElementById("miVolState");
  var briefCard        = document.getElementById("miBriefCard");
  var briefTs          = document.getElementById("miBriefTs");
  var briefStandDown   = document.getElementById("miBriefStandDown");
  var briefContent     = document.getElementById("miBriefContent");
  var themesContainer  = document.getElementById("miThemes");
  var stressGrid       = document.getElementById("miStressGrid");
  var asymContent      = document.getElementById("miAsymContent");
  var diffContent      = document.getElementById("miDiffContent");
  var patternList      = document.getElementById("miPatternList");
  var patternMatch     = document.getElementById("miPatternMatch");

  var showDiff = true;

  // Refs for the Desk Insight v2 section dividers (optional — the page
  // functions without them if desk-insight.js isn't loaded).
  var regimeSection = document.getElementById("miRegimeSection");
  var briefSection  = document.getElementById("miBriefSection");
  var bottomSection = document.getElementById("miBottomSection");
  var diffSection   = document.getElementById("miDiffSection");

  // Expose a lightweight context that the shared Desk Insight binding
  // in the page HTML reads via getCardData / getScenarioContext.
  var miContext = window.__RAVEN_MI_CONTEXT = window.__RAVEN_MI_CONTEXT || {};

  function syncDeskInsightSections() {
    if (regimeSection && topRow)      regimeSection.style.display = topRow.style.display === "grid"      ? "flex" : "none";
    if (briefSection  && briefRow)    briefSection.style.display  = briefRow.style.display  === "grid"   ? "flex" : "none";
    if (bottomSection && bottomGrid)  bottomSection.style.display = bottomGrid.style.display === "grid"  ? "flex" : "none";
    if (diffSection   && diffPanel)   diffSection.style.display   = diffPanel.style.display  === "grid"  ? "flex" : "none";
    if (window.DeskInsight) window.DeskInsight.refresh();
  }

  /* ── Helpers ──────────────────────────────────────── */
  function pillClass(label) {
    var l = (label || "").toLowerCase().replace(/[^a-z]/g, "");
    if (l === "riskon")     return "pill pill--green";
    if (l === "riskoff")    return "pill pill--red";
    if (l === "stressed")   return "pill pill--red";
    if (l === "neutral" || l === "transitional") return "pill pill--amber";
    return "pill pill--blue";
  }

  function gatePill(status) {
    var s = (status || "").toLowerCase();
    if (s === "allowed")    return '<span class="pill pill--green">' + status + '</span>';
    if (s === "selective" || s === "watch" || s === "reduced") return '<span class="pill pill--amber">' + status + '</span>';
    if (s === "suppressed") return '<span class="pill pill--red">' + status + '</span>';
    return '<span class="pill pill--blue">' + status + '</span>';
  }

  function accelArrow(accel) {
    if (accel === "rising")  return '<span class="accel--rising">&#x25B2; Rising</span>';
    if (accel === "falling") return '<span class="accel--falling">&#x25BC; Falling</span>';
    return '<span class="accel--stable">&#x25AC; Stable</span>';
  }

  function stressColor(score) {
    if (score >= 70) return "var(--red)";
    if (score >= 55) return "var(--amber)";
    if (score <= 35) return "var(--green)";
    return "var(--blue)";
  }

  function themeBarColor(intensity) {
    if (intensity >= 70) return "var(--red)";
    if (intensity >= 40) return "var(--amber)";
    return "var(--green)";
  }

  function fmt(ts) {
    if (!ts) return "";
    try { return new Date(ts).toLocaleString(); } catch (e) { return ts; }
  }

  function setProgress(pct, msg) {
    if (progressFill) progressFill.style.width = pct + "%";
    if (statusLabel) statusLabel.textContent = msg;
  }

  function showOverlay() { if (overlay) overlay.classList.add("isVisible"); }
  function hideOverlay() { if (overlay) overlay.classList.remove("isVisible"); }

  function fetchJSON(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  /* ═══════════════════════════════════════════════════════════════════
     Render: Regime
     ═══════════════════════════════════════════════════════════════════ */
  function _heroLabelClass(state) {
    var s = (state || "").toLowerCase().replace(/[^a-z]/g, "");
    if (s === "riskon")       return "miHeroLabel--riskon";
    if (s === "riskoff")      return "miHeroLabel--riskoff";
    if (s === "stressed")     return "miHeroLabel--stressed";
    if (s === "transitional") return "miHeroLabel--transitional";
    if (s === "neutral")      return "miHeroLabel--neutral";
    return "miHeroLabel--default";
  }

  function _barColor(score) {
    if (score >= 70) return "var(--red, #ff3b30)";
    if (score >= 50) return "var(--amber, #ff9f0a)";
    if (score >= 30) return "var(--blue, #007aff)";
    return "var(--green, #34c759)";
  }

  function _gateDotClass(status) {
    var s = (status || "").toLowerCase();
    if (s === "allowed")   return "miHeroGateDot--green";
    if (s === "suppressed") return "miHeroGateDot--red";
    return "miHeroGateDot--amber";
  }

  function renderDMS(dms) {
    _lastDms = dms;
    topRow.style.display = "grid";

    // ── Regime card ──
    var regime = dms.regime || {};
    var rScore = Number(regime.score || 0);
    regimeScore.textContent = rScore.toFixed(0);

    regimeLabel.textContent = regime.state || "--";
    regimeLabel.className = "miHeroLabel " + _heroLabelClass(regime.state);

    // Update the shared Desk Insight context so tooltip fetches can
    // reference live regime / vol state / engine gates.
    miContext.regime        = regime;
    miContext.regimeLabel   = regime.state;
    miContext.regimeScore   = rScore;
    miContext.volState      = dms.vol_state;
    miContext.engineGates   = dms.engine_gates;
    miContext.crossAsset    = dms.cross_asset;
    syncDeskInsightSections();

    var regimeBar = document.getElementById("miRegimeBar");
    if (regimeBar) {
      regimeBar.style.width = Math.min(rScore, 100) + "%";
      regimeBar.style.background = _barColor(rScore);
    }

    // Engine gates as compact chips with colored dots
    var gates = dms.engine_gates || {};
    var gateNames = { earnings: "Earnings", red_dog: "Red Dog", ichimoku: "Ichimoku", index_income: "Index Income" };
    var gatesHtml = "";
    for (var gk in gateNames) {
      if (gates[gk] !== undefined) {
        var dotCls = _gateDotClass(gates[gk]);
        gatesHtml += '<span class="miHeroGate"><span class="miHeroGateDot ' + dotCls + '"></span>' +
          gateNames[gk] + ': <b>' + gates[gk] + '</b></span>';
      }
    }
    engineGates.innerHTML = gatesHtml;

    // Vol state as clean chips
    var vs = dms.vol_state || {};
    volState.innerHTML =
      '<span class="miHeroChip"><b>Term</b> ' + (vs.term_structure || "--") + '</span>' +
      '<span class="miHeroChip"><b>Skew</b> ' + (vs.skew || "--") + '</span>' +
      '<span class="miHeroChip"><b>Level</b> ' + (vs.level || 0).toFixed(1) + '</span>';

    // Show regime data source and staleness
    var src = dms.regime_source || {};
    var srcEl = document.getElementById("miRegimeSource");
    if (srcEl && src.snapshot_generated_at) {
      var snapTime = fmt(src.snapshot_generated_at);
      var grade = src.snapshot_grade ? " · Grade " + src.snapshot_grade : "";
      srcEl.textContent = "Snapshot: " + snapTime + grade;
      srcEl.style.display = "block";
      var snapDate = new Date(src.snapshot_generated_at);
      var ageMs = Date.now() - snapDate.getTime();
      if (ageMs > 60 * 60 * 1000) {
        srcEl.style.color = "#995c00";
        srcEl.textContent += " (stale — " + Math.round(ageMs / 60000) + "m ago)";
      }
    }

    // ── MI v2 augmentation ──────────────────────────────────────
    _renderMiV2(dms);
  }

  /* ──────────────────────────────────────────────────────────────
     MI v2 renderers (probability bars, factor stack, diff, banner)
     ────────────────────────────────────────────────────────────── */

  function _pct(n) {
    var x = Number(n) || 0;
    return Math.max(0, Math.min(100, Math.round(x * 100))) + "%";
  }

  function _renderMiV2(dms) {
    var probs = (dms.regime || {}).probs || null;
    var probsEl = document.getElementById("miRegimeProbs");
    var chipsEl = document.getElementById("miRegimeChips");
    if (probs && probsEl && chipsEl) {
      var ro = Number(probs.risk_on || 0);
      var tr = Number(probs.transitional || 0);
      var st = Number(probs.stressed || 0);
      var roEl = document.getElementById("miProbRiskOn");
      var trEl = document.getElementById("miProbTransitional");
      var stEl = document.getElementById("miProbStressed");
      if (roEl) roEl.style.width = (ro * 100).toFixed(0) + "%";
      if (trEl) trEl.style.width = (tr * 100).toFixed(0) + "%";
      if (stEl) stEl.style.width = (st * 100).toFixed(0) + "%";
      var roV = document.getElementById("miProbRiskOnVal");
      var trV = document.getElementById("miProbTransitionalVal");
      var stV = document.getElementById("miProbStressedVal");
      if (roV) roV.textContent = _pct(ro);
      if (trV) trV.textContent = _pct(tr);
      if (stV) stV.textContent = _pct(st);
      probsEl.style.display = "flex";

      var conf  = (dms.regime || {}).confidence;
      var trisk = (dms.regime || {}).transition_risk_1d;
      var anom  = (dms.regime || {}).anomaly_score;
      var confChip   = document.getElementById("miRegimeConfidenceChip");
      var triskChip  = document.getElementById("miRegimeTransitionChip");
      var anomChip   = document.getElementById("miRegimeAnomalyChip");
      if (confChip)  confChip.textContent  = "conf " + (conf  != null ? _pct(conf)  : "—");
      if (triskChip) triskChip.textContent = "flip risk " + (trisk != null ? _pct(trisk) : "—");
      if (anomChip)  anomChip.textContent  = "anomaly " + (anom  != null ? Number(anom).toFixed(2) : "—");
      chipsEl.style.display = "flex";
    } else if (probsEl) {
      probsEl.style.display = "none";
    }

    // Data-quality banner.
    var dq = ((dms.regime || {}).data_quality) || {};
    var banner = document.getElementById("miDataQualityBanner");
    if (banner) {
      if (dq.insufficient) {
        banner.className = "miDataQualityBanner miDQ--insufficient";
        banner.textContent = "Insufficient factor data — regime served from legacy fallback. " +
          "Missing factors: " + ((dq.missing || []).join(", ") || "(none)") + ".";
        banner.style.display = "block";
      } else if ((dq.missing || []).length > 0 || (dq.stale || []).length > 0) {
        banner.className = "miDataQualityBanner";
        banner.textContent = "Partial data: " +
          (dq.missing || []).length + " missing, " +
          (dq.stale || []).length + " stale of 8 factors.";
        banner.style.display = "block";
      } else {
        banner.style.display = "none";
      }
    }

    // Factor Stack card.
    _renderFactorStack(dms);
    _renderDiffPanelV2();
  }

  function _renderFactorStack(dms) {
    var fr = (dms.regime || {}).factor_readings || {};
    var contribs = (dms.regime || {}).factor_contributions || {};
    var rowEl = document.getElementById("miFactorRow");
    var sectionEl = document.getElementById("miFactorSection");
    var stackEl = document.getElementById("miFactorStack");
    if (!stackEl) return;
    var keys = Object.keys(fr);
    if (!keys.length) {
      if (rowEl) rowEl.style.display = "none";
      if (sectionEl) sectionEl.style.display = "none";
      return;
    }
    // Canonical order matches FACTOR_KEYS in backend/market_intel/factors.py.
    var ORDER = [
      "rv_spx_20d", "vix_term_slope", "credit_hyg_lqd", "dxy_drift",
      "commodity_stress", "btc_decoupling", "dealer_gamma", "breadth_proxy",
    ];
    var html = "";
    ORDER.forEach(function (k) {
      var r = fr[k];
      if (!r) return;
      var z = Number(r.z || 0);
      var quality = String(r.quality || "MISSING");
      var label = String(r.label || k);
      var asof = String(r.as_of || "");
      var contrib = contribs[k];
      // Bar position: z in [-4,4] -> 0..100% (50% = z=0 midline)
      var barPctWidth = Math.min(100, Math.abs(z) / 4 * 50);  // half-width
      var barCls = z >= 0 ? "miFactorBar--pos" : "miFactorBar--neg";
      var barLeft = z >= 0 ? 50 : (50 - barPctWidth);
      var qCls = "miFactorChip--missing";
      if (quality === "OK") qCls = "miFactorChip--ok";
      else if (quality === "STALE") qCls = "miFactorChip--stale";
      var contribTxt = (contrib != null && quality !== "MISSING")
        ? " · " + (Number(contrib) >= 0 ? "+" : "") + Number(contrib).toFixed(2)
        : "";
      html += '<div class="miFactorRow" title="' +
              (asof ? "as of " + asof : "") + (r.note ? " · " + r.note : "") +
              '">' +
        '<span class="miFactorLabel">' + label + '</span>' +
        '<div class="miFactorBarWrap">' +
          '<div class="miFactorBarMidline"></div>' +
          '<div class="miFactorBar ' + barCls + '" style="left:' + barLeft + '%;width:' + barPctWidth + '%;"></div>' +
        '</div>' +
        '<span class="miFactorZ">' + (z >= 0 ? "+" : "") + z.toFixed(2) + "σ" + contribTxt + '</span>' +
        '<span class="miFactorChip ' + qCls + '">' + quality + '</span>' +
      '</div>';
    });
    stackEl.innerHTML = html;
    if (rowEl) rowEl.style.display = "grid";
    if (sectionEl) sectionEl.style.display = "flex";
    if (window.DeskInsight) window.DeskInsight.refresh();
  }

  function _renderDiffPanelV2() {
    var sectionEl = document.getElementById("miDiffSectionV2");
    var rowEl = document.getElementById("miDiffRow");
    var panelEl = document.getElementById("miDiffPanelV2");
    var tsEl = document.getElementById("miDiffTs");
    if (!panelEl) return;
    fetch("/api/market-intel/diff", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || (!d.has_changes && !d.headline_summary)) {
          if (rowEl) rowEl.style.display = "none";
          if (sectionEl) sectionEl.style.display = "none";
          return;
        }
        var html = "";
        var headlineCls = d.regime_flip_is_material
          ? "miDiffHeadline miDiffHeadline--material"
          : "miDiffHeadline";
        html += '<div class="' + headlineCls + '">' +
                (d.headline_summary || "Quiet tape") + '</div>';
        if (Array.isArray(d.top_factor_moves) && d.top_factor_moves.length) {
          html += '<div class="miDiffSubsection">';
          html += '<div class="miDiffSubLabel">Top factor moves</div>';
          d.top_factor_moves.forEach(function (m) {
            var dz = Number(m.delta_z || 0);
            var sign = dz >= 0 ? "+" : "";
            var cls = dz >= 0 ? "miDiffDelta--up" : "miDiffDelta--down";
            html += '<div class="miDiffMoveRow">' +
              '<strong>' + (m.label || m.key) + '</strong>' +
              ' &middot; ' +
              '<span class="miDiffDelta ' + cls + '">' + sign + dz.toFixed(2) + 'σ</span>' +
              ' (was ' + Number(m.z_yesterday || 0).toFixed(2) +
              ', now ' + Number(m.z_today || 0).toFixed(2) + ')' +
            '</div>';
          });
          html += '</div>';
        }
        if (Array.isArray(d.engine_gate_changes) && d.engine_gate_changes.length) {
          html += '<div class="miDiffSubsection">';
          html += '<div class="miDiffSubLabel">Engine gate changes</div>';
          d.engine_gate_changes.forEach(function (g) {
            html += '<div class="miDiffGateRow"><strong>' + g.engine + '</strong>: ' +
              g.from_state + " → <strong>" + g.to_state + "</strong></div>";
          });
          html += '</div>';
        }
        if (d.regime_threshold_proximity && d.regime_threshold_proximity.p_stressed_today != null) {
          var prox = d.regime_threshold_proximity;
          html += '<div class="miDiffSubsection">';
          html += '<div class="miDiffSubLabel">Regime flip proximity</div>';
          html += '<div class="miDiffMoveRow">P(stressed) today = <strong>' +
                  (prox.p_stressed_today * 100).toFixed(1) + '%</strong>; ' +
                  'distance to flip = ' + (prox.distance_to_flip * 100).toFixed(1) + 'pp.' +
                  (prox.crossed_flip ? ' <strong>CROSSED 0.5 OVERNIGHT.</strong>' : '') +
                  '</div>';
          html += '</div>';
        }
        panelEl.innerHTML = html;
        if (tsEl && d.to_date) tsEl.textContent = d.from_date + " → " + d.to_date;
        if (rowEl) rowEl.style.display = "grid";
        if (sectionEl) sectionEl.style.display = "flex";
        if (window.DeskInsight) window.DeskInsight.refresh();
      })
      .catch(function () { /* swallow — silent if endpoint disabled */ });
  }

  /* ═══════════════════════════════════════════════════════════════════
     Render: Morning Brief
     ═══════════════════════════════════════════════════════════════════ */
  function renderBrief(brief) {
    miContext.morningBrief = brief;
    syncDeskInsightSections();
    briefRow.style.display = "grid";
    var isFallback = brief._source === "fallback";
    briefTs.textContent = fmt(brief._generated_at) + (isFallback ? " (fallback)" : "");

    if (isFallback && brief._fallback_reason) {
      briefStandDown.innerHTML = '<div class="miStandDown" style="background:rgba(255,149,0,0.06);color:#995c00;border:1px solid rgba(255,149,0,0.14);">' +
        '<b>LLM unavailable:</b> ' + brief._fallback_reason +
        '<br><small>Showing placeholder text. Fix the issue and click Refresh Live Data.</small></div>';
      briefContent.innerHTML = "";
      return;
    }

    var sd = brief.stand_down || "";
    if (sd && sd.toLowerCase().indexOf("no stand-down") === -1 && sd.toLowerCase().indexOf("not required") === -1) {
      briefStandDown.innerHTML = '<div class="miStandDown">' + sd + '</div>';
    } else {
      briefStandDown.innerHTML = '<div class="miStandDown miStandDown--clear">No stand-down required.</div>';
    }

    var fields = [
      { key: "market_posture",       label: "Market Posture",      cls: "posture", icon: "P" },
      { key: "changes_vs_yesterday", label: "What Changed",        cls: "changes", icon: "D" },
      { key: "active_themes",        label: "Active Themes",       cls: "themes",  icon: "T" },
      { key: "cross_asset_signals",  label: "Cross-Asset Signals", cls: "cross",   icon: "X" },
      { key: "engine_alignment",     label: "Engine Alignment",    cls: "engines", icon: "E" },
      { key: "watch_list",           label: "Watch List",          cls: "watch",   icon: "W" },
    ];

    var html = "";
    fields.forEach(function (f) {
      var val = brief[f.key] || "";
      if (typeof val === "object") val = JSON.stringify(val);

      // Strip any leftover bracket citations like [field.name]
      val = String(val).replace(/\[[\w.]+\]/g, "").replace(/\s{2,}/g, " ").trim();

      var labelHtml = '<span class="miBriefIcon miBriefIcon--' + f.cls + '">' + f.icon + '</span>' +
                      '<span class="miBriefLabel--' + f.cls + '">' + f.label + '</span>';

      var bodyHtml;
      if (f.key === "watch_list" && val && val.toLowerCase() !== "none" && val.toLowerCase() !== "nothing flagged.") {
        var pills = val.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
        bodyHtml = '<div class="miBriefWatchPills">' +
          pills.map(function (p) { return '<span class="miBriefWatchPill">' + p + '</span>'; }).join("") +
          '</div>';
      } else {
        bodyHtml = '<div class="miBriefText">' + val + '</div>';
      }

      html += '<div class="miBriefItem"><div class="miBriefLabel">' + labelHtml + '</div>' + bodyHtml + '</div>';
    });
    briefContent.innerHTML = html;
  }

  /* ═══════════════════════════════════════════════════════════════════
     Render: Pattern Library
     ═══════════════════════════════════════════════════════════════════ */
  var _lastPatterns = {};
  var _lastPatternMatch = {};

  function renderPatterns(patterns, matched) {
    if (!patternList) return;

    // Matched pattern summary
    if (patternMatch) {
      if (matched.label) {
        var guidanceParts = [];
        var favored = matched.favored_play_types || [];
        if (favored.length > 0) {
          guidanceParts.push("Favored: " + favored.map(function (f) { return f.replace(/_/g, " "); }).join(", "));
        }
        if (matched.primary_risk) {
          guidanceParts.push("Risk: " + matched.primary_risk);
        }
        patternMatch.innerHTML =
          '<div style="padding:10px 12px;border-radius:10px;background:rgba(52,199,89,0.06);border:1px solid rgba(52,199,89,0.14);">' +
          '<div style="font-size:13px;font-weight:800;">' + matched.label +
          ' <span class="pill pill--green" style="font-size:9px;margin-left:4px;">ACTIVE MATCH</span></div>' +
          (matched.confidence != null ? '<div class="miPatternConfidence">Confidence: ' + matched.confidence + '%</div>' : '') +
          (guidanceParts.length ? '<div class="miPatternGuidance">' + guidanceParts.join(' &middot; ') + '</div>' : '') +
          '</div>';
      } else {
        patternMatch.innerHTML =
          '<div style="padding:8px 12px;border-radius:10px;background:var(--hover);font-size:11px;color:var(--muted);">' +
          'No pattern matched this week. Events populate as regime and vol states change.</div>';
      }
    }

    // Pattern list
    var html = '';
    for (var key in patterns) {
      var p = patterns[key];
      var isMatch = matched.key === key;
      html += '<div class="miPatternItem' + (isMatch ? ' miPatternItem--match' : '') + '" data-pattern-key="' + key + '" title="Click for desk insight">' +
        '<div class="miPatternName">' + (p.label || key) +
        (isMatch ? ' <span class="pill pill--green" style="font-size:9px;">MATCH</span>' : '') +
        '</div>' +
        '<div class="miPatternDesc">' + (p.description || "") + '</div>' +
      '</div>';
    }
    patternList.innerHTML = html;
  }

  /* ═══════════════════════════════════════════════════════════════════
     Render: Active Themes
     ═══════════════════════════════════════════════════════════════════ */
  function renderThemes(dms) {
    var themes = dms.news_themes || [];
    if (themes.length === 0) {
      themesContainer.innerHTML = '<div class="miEmpty">No active themes detected.</div>';
      return;
    }

    var active = themes.filter(function (t) { return (t.intensity || 0) > 0; });
    if (active.length === 0) {
      themesContainer.innerHTML = '<div class="miEmpty">All themes below threshold.</div>';
      return;
    }

    var html = "";
    active.forEach(function (t, idx) {
      var barColor = themeBarColor(t.intensity || 0);
      html += '<div class="miThemeCard" data-theme-idx="' + idx + '" title="Click for desk insight">' +
        '<div class="miThemeHeader">' +
          '<span class="miThemeName">' + (t.theme || "") + '</span>' +
          '<span class="miThemeIntensity" style="color:' + barColor + ';">' + (t.intensity || 0).toFixed(0) + '/100</span>' +
        '</div>' +
        '<div class="miThemeBar"><div class="miThemeBarFill" style="width:' + (t.intensity || 0) + '%;background:' + barColor + ';"></div></div>' +
        '<div class="miThemeMeta">' +
          accelArrow(t.acceleration || "stable") +
          ' &middot; ' + (t.persistence_days || 0) + 'd persistent' +
          ' &middot; ' + (t.keyword_hits || 0) + ' hits' +
        '</div>' +
        '<div class="miThemeMeta">Sectors: ' + (t.affected_sectors || []).join(", ") + '</div>' +
      '</div>';
    });
    themesContainer.innerHTML = html;
  }

  /* ═══════════════════════════════════════════════════════════════════
     Render: Market Stress (cross-asset readings)
     ═══════════════════════════════════════════════════════════════════ */
  function renderStress(dms) {
    var xa = dms.cross_asset_stress || {};
    var readings = xa.readings || [];

    if (readings.length === 0) {
      stressGrid.innerHTML = '<div class="miEmpty">No market stress data available.</div>';
      return;
    }

    var html = '';
    html += '<div class="miStressItem" style="grid-column:1/-1;border-left:3px solid ' + stressColor(xa.composite_score || 50) + ';" title="Click for desk insight">' +
      '<div class="miStressName">COMPOSITE</div>' +
      '<span class="miStressScore" style="color:' + stressColor(xa.composite_score || 50) + ';">' + (xa.composite_score || 50).toFixed(0) + '</span>' +
      ' <span class="' + pillClass(xa.composite_label) + '">' + (xa.composite_label || "Neutral") + '</span>' +
    '</div>';

    _lastReadings = readings;

    readings.forEach(function (r, idx) {
      var sc = r.stress_score || 50;
      var relCls = "";
      if (r.equity_relationship === "confirming") relCls = "miStressRel--confirming";
      if (r.equity_relationship === "diverging") relCls = "miStressRel--diverging";

      html += '<div class="miStressItem miStressCard" data-reading-idx="' + idx + '" title="Click for desk insight">' +
        '<div class="miStressName">' + (r.name || r.symbol || "") + '</div>' +
        '<div><span class="miStressScore" style="color:' + stressColor(sc) + ';">' + sc.toFixed(0) + '</span>' +
        ' <span class="miStressDir">' + (r.direction || "flat") + '</span></div>' +
        '<div class="miStressChange">' + ((r.change_vs_prior || 0) >= 0 ? "+" : "") + (r.change_vs_prior || 0).toFixed(2) + '%</div>' +
        '<div class="miStressRel ' + relCls + '">' + (r.equity_relationship || "neutral") + ' vs equities</div>' +
      '</div>';
    });

    stressGrid.innerHTML = html;
  }

  /* ═══════════════════════════════════════════════════════════════════
     Render: Asymmetry Radar
     ═══════════════════════════════════════════════════════════════════ */
  function renderAsymmetry(dms) {
    var signals = dms.asymmetry_signals || [];

    if (signals.length === 0) {
      asymRow.style.display = "grid";
      asymContent.innerHTML = '<div class="miEmpty">No asymmetries detected. All clear.</div>';
      return;
    }

    asymRow.style.display = "grid";
    var html = "";
    signals.forEach(function (s, idx) {
      html += '<div class="miAsymCard" data-asym-idx="' + idx + '" title="Click for desk insight">' +
        '<div class="miAsymType">' + (s.type || "").replace(/_/g, " ") + '</div>' +
        '<div class="miAsymDesc">' + (s.description || "") + '</div>' +
        '<div class="miAsymAction">' + (s.action || "Monitor only.") + '</div>' +
        '<div style="font-size:10px;color:var(--muted);margin-top:4px;">Sources: ' + (s.sources || []).join(", ") + '</div>' +
      '</div>';
    });
    asymContent.innerHTML = html;
  }

  /* ═══════════════════════════════════════════════════════════════════
     Render: Diff
     ═══════════════════════════════════════════════════════════════════ */
  function renderDiff(diffData) {
    miContext.diff = diffData;
    syncDeskInsightSections();
    _lastDiff = diffData || {};
    diffPanel.style.display = showDiff ? "grid" : "none";
    if (!showDiff) return;

    if (!diffData || !diffData.has_changes) {
      diffContent.innerHTML = '<div class="miEmpty">No changes detected between ' + (diffData.from_date || "yesterday") + ' and ' + (diffData.to_date || "today") + '.</div>';
      return;
    }

    var changes = diffData.changes || {};
    var html = '<div style="font-size:12px;margin-bottom:8px;">Comparing <b>' + diffData.from_date + '</b> to <b>' + diffData.to_date + '</b></div>';

    for (var section in changes) {
      html += '<div style="margin-bottom:8px;"><span style="font-weight:800;font-size:11px;text-transform:uppercase;">' + section + '</span>';
      var sectionChanges = changes[section];
      if (typeof sectionChanges === "object" && sectionChanges !== null) {
        for (var field in sectionChanges) {
          var ch = sectionChanges[field];
          if (ch && typeof ch === "object" && ch.old !== undefined) {
            html += '<div style="font-size:11px;padding-left:12px;"><span style="color:var(--muted);">' + field + ':</span> <span style="color:var(--red);">' + JSON.stringify(ch.old) + '</span> &rarr; <span style="color:var(--green);">' + JSON.stringify(ch["new"]) + '</span></div>';
          }
        }
      }
      html += '</div>';
    }
    diffContent.innerHTML = html;
  }

  /* ═══════════════════════════════════════════════════════════════════
     Main load sequence
     ═══════════════════════════════════════════════════════════════════ */
  function loadAll() {
    showOverlay();
    setProgress(5, "Fetching DailyMarketState...");

    fetchJSON("/api/front-layer/daily-market-state")
      .then(function (dms) {
        setProgress(30, "Rendering market state...");
        renderDMS(dms);
        renderThemes(dms);
        renderStress(dms);
        renderAsymmetry(dms);
        bottomGrid.style.display = "grid";

        if (refreshBanner && dms.generated_at) {
          var regime = dms.regime || {};
          var src = dms.regime_source || {};
          var xa = dms.cross_asset_stress || {};
          var parts = [
            '<b>Live data refreshed</b> at ' + fmt(dms.generated_at),
            'Regime: <b>' + (regime.state || '?') + ' (' + (regime.score || 0).toFixed(0) + ')</b>',
            'Cross-asset: ' + (xa.composite_score ? xa.composite_score.toFixed(0) : '?'),
            (dms.news_themes || []).length + ' themes',
            src.snapshot_grade ? 'Brief generated (Grade ' + src.snapshot_grade + ')' : ''
          ].filter(Boolean);
          refreshBanner.className = "miRefreshBanner miRefreshBanner--ok";
          refreshBanner.innerHTML = parts.join(' &middot; ');
          refreshBanner.style.display = "block";
        }

        setProgress(50, "Generating Morning Brief...");
        return fetchJSON("/api/front-layer/morning-brief").then(function (brief) {
          renderBrief(brief);
          setProgress(70, "Loading pattern library...");
          return fetchJSON("/api/front-layer/patterns");
        }).then(function (patternData) {
          _lastPatterns = patternData.templates || {};
          _lastPatternMatch = patternData.matched || {};
          renderPatterns(_lastPatterns, _lastPatternMatch);
          setProgress(85, "Checking day-over-day changes...");
          return fetchJSON("/api/front-layer/diff");
        }).then(function (diffData) {
          renderDiff(diffData);
          setProgress(100, "Market Intelligence loaded.");
        });
      })
      .catch(function (err) {
        console.error("Market Intelligence load error:", err);
        setProgress(100, "Error: " + err.message);
      })
      .finally(function () {
        setTimeout(hideOverlay, 600);
      });
  }

  /* ═══════════════════════════════════════════════════════════════════
     Refresh Live Data
     ═══════════════════════════════════════════════════════════════════ */
  function refreshLiveData() {
    if (!refreshBtn) return;
    _insightCache = {};
    refreshBtn.disabled = true;
    refreshBtn.classList.add("isRefreshing");
    refreshBtn.textContent = "Refreshing...";
    if (refreshBanner) {
      refreshBanner.className = "miRefreshBanner";
      refreshBanner.style.display = "block";
      refreshBanner.textContent = "Pulling fresh data from all engines and data sources...";
    }

    fetch("/api/front-layer/refresh", { method: "POST" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (result) {
        if (refreshBanner) {
          var ts = result.refreshed_at ? fmt(result.refreshed_at) : "now";
          var llm = result.llm || {};
          var fallbackReason = llm.fallback_reason || llm.brief_error || "";
          var briefStatus = result.brief_regenerated
            ? '<span style="color:#1b8a3e;">Brief generated (LLM)</span>'
            : '<span style="color:#995c00;">Brief fallback' +
              (fallbackReason ? ' — ' + fallbackReason : (llm.openai_key_set ? ' — check logs' : ' — no OpenAI key')) +
              '</span>';
          refreshBanner.className = "miRefreshBanner miRefreshBanner--ok";
          refreshBanner.innerHTML =
            '<b>Live data refreshed</b> at ' + ts +
            ' &middot; Regime: ' + ((result.regime || {}).state || "?") +
            ' (' + ((result.regime || {}).score || 0).toFixed(0) + ')' +
            ' &middot; Cross-asset: ' + (result.cross_asset_readings || 0) + ' readings' +
            ' &middot; ' + (result.theme_count || 0) + ' themes' +
            ' &middot; ' + briefStatus;
        }

        return fetchJSON("/api/front-layer/daily-market-state").then(function (dms) {
          renderDMS(dms);
          renderThemes(dms);
          renderStress(dms);
          renderAsymmetry(dms);
          topRow.style.display = "grid";
          bottomGrid.style.display = "grid";

          return fetchJSON("/api/front-layer/morning-brief");
        }).then(function (brief) {
          renderBrief(brief);
          return fetchJSON("/api/front-layer/patterns");
        }).then(function (patternData) {
          _lastPatterns = patternData.templates || {};
          _lastPatternMatch = patternData.matched || {};
          renderPatterns(_lastPatterns, _lastPatternMatch);
        });
      })
      .catch(function (err) {
        console.error("Refresh error:", err);
        if (refreshBanner) {
          refreshBanner.className = "miRefreshBanner";
          refreshBanner.style.display = "block";
          refreshBanner.textContent = "Refresh failed: " + err.message;
        }
      })
      .finally(function () {
        refreshBtn.disabled = false;
        refreshBtn.classList.remove("isRefreshing");
        refreshBtn.textContent = "Refresh Live Data";
      });
  }

  /* ── Event bindings ────────────────────────────── */
  if (refreshBtn)  refreshBtn.addEventListener("click", refreshLiveData);

  /* ═══════════════════════════════════════════════════════════════════
     Desk Insight Popup (dark draggable)
     ═══════════════════════════════════════════════════════════════════ */
  var _lastReadings = [];
  var _lastDms = {};
  var _lastDiff = {};
  var _insightCache = {};

  var insightPopup  = document.getElementById("miInsightPopup");
  var insightHeader = document.getElementById("miInsightHeader");
  var insightTitle  = document.getElementById("miInsightTitle");
  var insightClose  = document.getElementById("miInsightClose");
  var insightBody   = document.getElementById("miInsightBody");

  initDrag(insightPopup, insightHeader, { closeSelector: ".miInsightClose" });

  function openPopup(title, clickEvent) {
    if (!insightPopup) return;
    insightTitle.textContent = title;
    insightBody.innerHTML =
      '<div class="miInsightLoading">' +
      '<span class="miInsightDot"></span><span class="miInsightDot"></span><span class="miInsightDot"></span>' +
      '<br>Generating desk insight...</div>';

    var posX = (clickEvent ? clickEvent.clientX : window.innerWidth / 2) + 20;
    var posY = (clickEvent ? clickEvent.clientY : window.innerHeight / 2) - 120;
    if (posX + 420 > window.innerWidth) posX = (clickEvent ? clickEvent.clientX : 200) - 420;
    posX = Math.max(16, posX);
    posY = Math.max(16, posY);
    if (posY + 400 > window.innerHeight) posY = window.innerHeight - 420;
    insightPopup.style.left = posX + "px";
    insightPopup.style.top = posY + "px";
    insightPopup.style.display = "block";
  }

  function hideInsightPopup() { if (insightPopup) insightPopup.style.display = "none"; }
  if (insightClose) insightClose.addEventListener("click", hideInsightPopup);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hideInsightPopup(); });

  function renderSourceFooter(insight) {
    if (insight._source === "fallback" && insight._fallback_reason) {
      return '<div class="miInsightSource" style="color:rgba(255,180,100,0.6);">Fallback: ' + insight._fallback_reason + '</div>';
    }
    if (insight._source === "llm") {
      return '<div class="miInsightSource">Generated by LLM &middot; Read-only &middot; Not a trade recommendation</div>';
    }
    return '';
  }

  function renderSections(insight, sections) {
    var html = '';
    sections.forEach(function (s) {
      var val = insight[s.key] || "";
      if (!val) return;
      var isDesk = s.key === "desk_takeaway";
      html += '<div class="miInsightSection">' +
        '<div class="miInsightSectionTitle">' + s.title + '</div>' +
        '<div class="miInsightText"' + (isDesk ? ' style="font-weight:700;color:rgba(255,255,255,0.95);"' : '') + '>' + val + '</div>' +
      '</div>';
    });
    return html;
  }

  function fetchCardInsight(cardType, cardData, cacheKey, sections, metaHtml) {
    if (_insightCache[cacheKey]) {
      insightBody.innerHTML = (metaHtml || '') + renderSections(_insightCache[cacheKey], sections) + renderSourceFooter(_insightCache[cacheKey]);
      return;
    }
    fetch("/api/front-layer/card-insight", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ card_type: cardType, card_data: cardData }),
    })
    .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(function (insight) {
      _insightCache[cacheKey] = insight;
      insightBody.innerHTML = (metaHtml || '') + renderSections(insight, sections) + renderSourceFooter(insight);
    })
    .catch(function (err) {
      insightBody.innerHTML = '<div class="miInsightText" style="color:rgba(255,200,150,0.8);">Insight unavailable: ' + err.message + '</div>';
    });
  }

  /* ── 1. Asset stress cards ─────────────────────────── */
  if (stressGrid) {
    stressGrid.addEventListener("click", function (e) {
      var card = e.target.closest(".miStressCard");
      if (card) {
        var idx = parseInt(card.getAttribute("data-reading-idx"), 10);
        if (isNaN(idx) || !_lastReadings[idx]) return;
        var reading = _lastReadings[idx];
        var name = reading.name || reading.symbol || "Asset";
        openPopup(name + " — Desk Insight", e);
        var cacheKey = "asset:" + name;
        if (_insightCache[cacheKey]) {
          renderAssetInsightBody(_insightCache[cacheKey], reading);
          return;
        }
        fetch("/api/front-layer/asset-insight", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ asset: reading }),
        })
        .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
        .then(function (insight) { _insightCache[cacheKey] = insight; renderAssetInsightBody(insight, reading); })
        .catch(function (err) { insightBody.innerHTML = '<div class="miInsightText" style="color:rgba(255,200,150,0.8);">Insight unavailable: ' + err.message + '</div>'; });
        return;
      }

      // Composite row click
      var item = e.target.closest(".miStressItem");
      if (!item || item.classList.contains("miStressCard")) return;
      var nameEl = item.querySelector(".miStressName");
      if (!nameEl || nameEl.textContent !== "COMPOSITE") return;
      var xa = _lastDms.cross_asset_stress || {};
      openPopup("Composite Stress — Desk Insight", e);
      var sc = (xa.composite_score || 50).toFixed(0);
      var metaHtml = '<div class="miInsightMeta">' +
        '<div class="miInsightMetaItem">Composite<br><span class="miInsightMetaValue" style="color:' + stressColor(parseFloat(sc)) + ';">' + sc + '/100</span></div>' +
        '<div class="miInsightMetaItem">Label<br><span class="miInsightMetaValue">' + (xa.composite_label || "Neutral") + '</span></div>' +
        '<div class="miInsightMetaItem">Readings<br><span class="miInsightMetaValue">' + (xa.readings || []).length + ' assets</span></div>' +
        '<div class="miInsightMetaItem">Date<br><span class="miInsightMetaValue">' + (_lastDms.date || "--") + '</span></div></div>';
      fetchCardInsight("composite", xa, "composite", [
        { key: "what_its_telling_us", title: "What the Composite Is Telling Us" },
        { key: "key_drivers",         title: "Key Drivers" },
        { key: "historical_context",  title: "Historical Context" },
        { key: "desk_takeaway",       title: "Desk Takeaway" },
      ], metaHtml);
    });
  }

  function renderAssetInsightBody(insight, reading) {
    var sc = (reading.stress_score || 50).toFixed(0);
    var html = '<div class="miInsightMeta">' +
      '<div class="miInsightMetaItem">Stress Score<br><span class="miInsightMetaValue" style="color:' + stressColor(parseFloat(sc)) + ';">' + sc + '/100</span></div>' +
      '<div class="miInsightMetaItem">Direction<br><span class="miInsightMetaValue">' + (reading.direction || "flat") + ' (' + ((reading.change_vs_prior||0)>=0?"+":"") + (reading.change_vs_prior||0).toFixed(2) + '%)</span></div>' +
      '<div class="miInsightMetaItem">vs Equities<br><span class="miInsightMetaValue">' + (reading.equity_relationship || "neutral") + '</span></div>' +
      '<div class="miInsightMetaItem">Asset Class<br><span class="miInsightMetaValue">' + (reading.asset_class || "--") + '</span></div></div>';
    html += renderSections(insight, [
      { key: "what_its_telling_us", title: "What This Asset Is Telling Us" },
      { key: "why_it_matters",      title: "Why It Matters for Equities" },
      { key: "context",             title: "Context" },
      { key: "desk_takeaway",       title: "Desk Takeaway" },
    ]);
    html += renderSourceFooter(insight);
    insightBody.innerHTML = html;
  }

  /* ── 2. Theme cards ────────────────────────────────── */
  if (themesContainer) {
    themesContainer.addEventListener("click", function (e) {
      var card = e.target.closest(".miThemeCard");
      if (!card) return;
      var idx = parseInt(card.getAttribute("data-theme-idx"), 10);
      var themes = (_lastDms.news_themes || []).filter(function (t) { return (t.intensity || 0) > 0; });
      if (isNaN(idx) || !themes[idx]) return;
      var t = themes[idx];
      openPopup((t.theme || "Theme") + " — Desk Insight", e);
      var metaHtml = '<div class="miInsightMeta">' +
        '<div class="miInsightMetaItem">Intensity<br><span class="miInsightMetaValue" style="color:' + themeBarColor(t.intensity || 0) + ';">' + (t.intensity || 0).toFixed(0) + '/100</span></div>' +
        '<div class="miInsightMetaItem">Acceleration<br><span class="miInsightMetaValue">' + (t.acceleration || "stable") + '</span></div>' +
        '<div class="miInsightMetaItem">Persistence<br><span class="miInsightMetaValue">' + (t.persistence_days || 0) + ' days</span></div>' +
        '<div class="miInsightMetaItem">Hits<br><span class="miInsightMetaValue">' + (t.keyword_hits || 0) + '</span></div></div>';
      fetchCardInsight("theme", t, "theme:" + t.theme, [
        { key: "what_this_theme_means", title: "What This Theme Means" },
        { key: "market_impact",         title: "Market Impact" },
        { key: "momentum_read",         title: "Momentum Read" },
        { key: "desk_takeaway",         title: "Desk Takeaway" },
      ], metaHtml);
    });
  }

  /* ── 3. Regime card ────────────────────────────────── */
  var regimeCard = document.getElementById("miRegimeCard");
  if (regimeCard) {
    regimeCard.addEventListener("click", function (e) {
      if (e.target.closest("button")) return;
      var r = _lastDms.regime || {};
      openPopup("Regime State — Desk Insight", e);
      var metaHtml = '<div class="miInsightMeta">' +
        '<div class="miInsightMetaItem">Regime Score<br><span class="miInsightMetaValue">' + (r.score || 0).toFixed(0) + '</span></div>' +
        '<div class="miInsightMetaItem">State<br><span class="miInsightMetaValue">' + (r.state || "--") + '</span></div>' +
        '<div class="miInsightMetaItem">Vol Term<br><span class="miInsightMetaValue">' + ((_lastDms.vol_state || {}).term_structure || "--") + '</span></div>' +
        '<div class="miInsightMetaItem">Vol Skew<br><span class="miInsightMetaValue">' + ((_lastDms.vol_state || {}).skew || "--") + '</span></div></div>';
      fetchCardInsight("regime", { regime: r, engine_gates: _lastDms.engine_gates || {}, vol_state: _lastDms.vol_state || {} }, "regime", [
        { key: "what_regime_tells_us", title: "What the Regime Is Telling Us" },
        { key: "engine_implications",  title: "Engine Implications" },
        { key: "regime_context",       title: "Regime Context" },
        { key: "desk_takeaway",        title: "Desk Takeaway" },
      ], metaHtml);
    });
  }

  /* ── 4. Asymmetry cards ────────────────────────────── */
  if (asymContent) {
    asymContent.addEventListener("click", function (e) {
      var card = e.target.closest(".miAsymCard");
      if (!card) return;
      var idx = parseInt(card.getAttribute("data-asym-idx"), 10);
      var signals = (_lastDms.asymmetry_signals || []);
      if (isNaN(idx) || !signals[idx]) return;
      var s = signals[idx];
      var label = (s.type || "").replace(/_/g, " ");
      openPopup("Asymmetry: " + label + " — Desk Insight", e);
      var metaHtml = '<div class="miInsightMeta">' +
        '<div class="miInsightMetaItem">Type<br><span class="miInsightMetaValue" style="text-transform:capitalize;">' + label + '</span></div>' +
        '<div class="miInsightMetaItem">Severity<br><span class="miInsightMetaValue" style="color:var(--amber);">' + (s.severity || "--") + '</span></div>' +
        '<div class="miInsightMetaItem">Action<br><span class="miInsightMetaValue" style="font-size:11px;">' + (s.action || "Monitor only") + '</span></div>' +
        '<div class="miInsightMetaItem">Sources<br><span class="miInsightMetaValue" style="font-size:10px;">' + (s.sources || []).join(", ") + '</span></div></div>';
      fetchCardInsight("asymmetry", s, "asym:" + s.type, [
        { key: "what_this_means", title: "What This Asymmetry Means" },
        { key: "why_it_matters",  title: "Why It Matters" },
        { key: "what_to_watch",   title: "What to Watch" },
        { key: "desk_takeaway",   title: "Desk Takeaway" },
      ], metaHtml);
    });
  }

  /* ── 5. Diff panel ─────────────────────────────────── */
  if (diffContent) {
    diffContent.addEventListener("click", function (e) {
      if (!_lastDiff || !_lastDiff.has_changes) return;
      openPopup("Day-over-Day Changes — Desk Insight", e);
      var metaHtml = '<div class="miInsightMeta">' +
        '<div class="miInsightMetaItem">From<br><span class="miInsightMetaValue">' + (_lastDiff.from_date || "?") + '</span></div>' +
        '<div class="miInsightMetaItem">To<br><span class="miInsightMetaValue">' + (_lastDiff.to_date || "?") + '</span></div>' +
        '<div class="miInsightMetaItem">Sections Changed<br><span class="miInsightMetaValue">' + Object.keys(_lastDiff.changes || {}).length + '</span></div>' +
        '<div class="miInsightMetaItem">Regime<br><span class="miInsightMetaValue">' + ((_lastDms.regime || {}).state || "--") + '</span></div></div>';
      fetchCardInsight("diff", _lastDiff, "diff:" + _lastDiff.to_date, [
        { key: "what_changed",       title: "What Changed" },
        { key: "significance",       title: "Significance" },
        { key: "cascading_effects",  title: "Cascading Effects" },
        { key: "desk_takeaway",      title: "Desk Takeaway" },
      ], metaHtml);
    });
  }

  /* ── 6. Pattern Library cards ──────────────────────── */
  if (patternList) {
    patternList.addEventListener("click", function (e) {
      var item = e.target.closest(".miPatternItem");
      if (!item) return;
      var key = item.getAttribute("data-pattern-key");
      if (!key || !_lastPatterns[key]) return;
      var p = _lastPatterns[key];
      var isMatch = _lastPatternMatch.key === key;
      openPopup((p.label || key) + " — Pattern Insight", e);
      var metaHtml = '<div class="miInsightMeta">' +
        '<div class="miInsightMetaItem">Pattern<br><span class="miInsightMetaValue">' + (p.label || key) + '</span></div>' +
        '<div class="miInsightMetaItem">Status<br><span class="miInsightMetaValue" style="color:' + (isMatch ? 'var(--green)' : 'var(--muted)') + ';">' + (isMatch ? 'ACTIVE MATCH' : 'Not Matched') + '</span></div>' +
        '<div class="miInsightMetaItem">Regime<br><span class="miInsightMetaValue">' + ((_lastDms.regime || {}).state || "--") + '</span></div></div>';
      var cardData = {
        pattern_key: key, label: p.label, description: p.description,
        is_match: isMatch, confidence: _lastPatternMatch.confidence || 0,
        favored_play_types: _lastPatternMatch.favored_play_types || [],
        primary_risk: _lastPatternMatch.primary_risk || "",
      };
      fetchCardInsight("pattern_match", cardData, "pattern:" + key, [
        { key: "pattern_mechanics",      title: "Pattern Mechanics" },
        { key: "why_it_matched",         title: "Why It Matched" },
        { key: "what_typically_happens", title: "What Typically Happens" },
        { key: "what_invalidates_it",    title: "What Invalidates It" },
        { key: "desk_takeaway",          title: "Desk Takeaway" },
      ], metaHtml);
    });
  }

  /* ═══════════════════════════════════════════════════════════════════
     Backfill status check
     ═══════════════════════════════════════════════════════════════════ */
  var backfillStatus = document.getElementById("miBackfillStatus");

  function checkBackfillStatus() {
    if (!backfillStatus) return;
    fetchJSON("/api/front-layer/backfill-status")
      .then(function (data) {
        backfillStatus.style.display = "flex";
        if (data.seeded) {
          var range = data.date_range || {};
          var crossAssetDays = (data.days || []).filter(function (d) { return d.has_cross_asset; }).length;
          var themeDays = (data.days || []).filter(function (d) { return d.has_themes; }).length;
          backfillStatus.className = "miBackfillStatus miBackfillStatus--seeded";
          backfillStatus.innerHTML =
            '<span class="miBackfillDot miBackfillDot--green"></span>' +
            '<span><b>Historical data seeded</b> &middot; ' +
            data.snapshot_count + ' snapshots (' +
            (range.earliest || "?") + ' to ' + (range.latest || "?") + ')' +
            ' &middot; Cross-asset: ' + crossAssetDays + 'd' +
            ' &middot; Themes: ' + themeDays + 'd</span>';
        } else {
          backfillStatus.className = "miBackfillStatus miBackfillStatus--empty";
          backfillStatus.innerHTML =
            '<span class="miBackfillDot miBackfillDot--amber"></span>' +
            '<span><b>No historical data</b> &middot; ' +
            'Run <code>python3 scripts/backfill_front_layer.py</code> to seed history.</span>';
        }
      })
      .catch(function () { /* non-critical */ });
  }

  checkBackfillStatus();

  /* ── Auto-load on page open ────────────────────── */
  loadAll();

})();
