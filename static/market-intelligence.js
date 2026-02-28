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

  function showOverlay() { if (overlay) overlay.style.display = "flex"; }
  function hideOverlay() { if (overlay) overlay.style.display = "none"; }

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
  }

  /* ═══════════════════════════════════════════════════════════════════
     Render: Morning Brief
     ═══════════════════════════════════════════════════════════════════ */
  function renderBrief(brief) {
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
     Render: Cross-Asset Stress
     ═══════════════════════════════════════════════════════════════════ */
  function renderStress(dms) {
    var xa = dms.cross_asset_stress || {};
    var readings = xa.readings || [];

    if (readings.length === 0) {
      stressGrid.innerHTML = '<div class="miEmpty">No cross-asset data available.</div>';
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

  var _dragState = { isDragging: false, offsetX: 0, offsetY: 0 };

  function startInsightDrag(e) {
    if (!insightPopup) return;
    if (e.target.closest(".miInsightClose")) return;
    _dragState.isDragging = true;
    insightPopup.classList.add("isDragging");
    var clientX = e.touches ? e.touches[0].clientX : e.clientX;
    var clientY = e.touches ? e.touches[0].clientY : e.clientY;
    var rect = insightPopup.getBoundingClientRect();
    _dragState.offsetX = clientX - rect.left;
    _dragState.offsetY = clientY - rect.top;
    e.preventDefault();
  }

  function doInsightDrag(e) {
    if (!_dragState.isDragging || !insightPopup) return;
    var clientX = e.touches ? e.touches[0].clientX : e.clientX;
    var clientY = e.touches ? e.touches[0].clientY : e.clientY;
    var newX = Math.max(0, Math.min(clientX - _dragState.offsetX, window.innerWidth - insightPopup.offsetWidth));
    var newY = Math.max(0, Math.min(clientY - _dragState.offsetY, window.innerHeight - insightPopup.offsetHeight));
    insightPopup.style.left = newX + "px";
    insightPopup.style.top = newY + "px";
  }

  function endInsightDrag() {
    if (!_dragState.isDragging) return;
    if (insightPopup) insightPopup.classList.remove("isDragging");
    _dragState.isDragging = false;
  }

  document.addEventListener("mousemove", doInsightDrag);
  document.addEventListener("mouseup", endInsightDrag);
  document.addEventListener("touchmove", doInsightDrag, { passive: false });
  document.addEventListener("touchend", endInsightDrag);

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

    insightHeader.removeEventListener("mousedown", startInsightDrag);
    insightHeader.removeEventListener("touchstart", startInsightDrag);
    insightHeader.addEventListener("mousedown", startInsightDrag);
    insightHeader.addEventListener("touchstart", startInsightDrag, { passive: false });
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
      fetchCardInsight("regime", cardData, "pattern:" + key, [
        { key: "what_regime_tells_us", title: "What This Pattern Tells Us" },
        { key: "engine_implications",  title: "How the Desk Should Think About It" },
        { key: "regime_context",       title: "When This Pattern Works / Fails" },
        { key: "desk_takeaway",        title: "Desk Takeaway" },
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
