/* ── Raven-Tech Market Intelligence ─────────────────────────────────
   Front Layer: synthesizes all engines into a daily roadmap.
   Read-only. No trade recommendations.
   ──────────────────────────────────────────────────────────────────── */
(function () {
  "use strict";

  /* ── DOM refs ─────────────────────────────────────── */
  var runBtn           = document.getElementById("miRunBtn");
  var refreshBtn       = document.getElementById("miRefreshBtn");
  var refreshBanner    = document.getElementById("miRefreshBanner");
  var diffToggle       = document.getElementById("miDiffToggle");
  var overlay          = document.getElementById("ravenOverlay");
  var progressFill     = document.getElementById("ravenProgressFill");
  var statusLabel      = document.getElementById("ravenStatus");

  var topRow           = document.getElementById("miTopRow");
  var briefCard        = document.getElementById("miBriefCard");
  var roadmapCard      = document.getElementById("miRoadmapCard");
  var bottomGrid       = document.getElementById("miBottomGrid");
  var asymCard         = document.getElementById("miAsymCard");
  var diffPanel        = document.getElementById("miDiffPanel");

  var regimeScore      = document.getElementById("miRegimeScore");
  var regimeLabel      = document.getElementById("miRegimeLabel");
  var regimeTs         = document.getElementById("miRegimeTs");
  var engineGates      = document.getElementById("miEngineGates");
  var fpScore          = document.getElementById("miFpScore");
  var fpLabel          = document.getElementById("miFpLabel");
  var volState         = document.getElementById("miVolState");
  var briefTs          = document.getElementById("miBriefTs");
  var briefStandDown   = document.getElementById("miBriefStandDown");
  var briefContent     = document.getElementById("miBriefContent");
  var roadmapTs        = document.getElementById("miRoadmapTs");
  var roadmapContent   = document.getElementById("miRoadmapContent");
  var themesContainer  = document.getElementById("miThemes");
  var stressGrid       = document.getElementById("miStressGrid");
  var asymContent      = document.getElementById("miAsymContent");
  var diffContent      = document.getElementById("miDiffContent");

  var showDiff = false;

  /* ── Helpers ──────────────────────────────────────── */
  function h(tag, cls, html) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (html !== undefined) el.innerHTML = html;
    return el;
  }

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

  /* ── Data fetch ──────────────────────────────────── */
  function fetchJSON(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  /* ── Render: Regime + Flow Pressure ─────────────── */
  function renderDMS(dms) {
    topRow.style.display = "grid";

    var regime = dms.regime || {};
    regimeScore.textContent = (regime.score || 0).toFixed(0);
    regimeLabel.textContent = regime.state || "--";
    regimeLabel.className = "miRegimeBig " + pillClass(regime.state).replace("pill ", "");
    regimeTs.textContent = fmt(dms.generated_at);

    // Engine gates
    var gates = dms.engine_gates || {};
    var gatesHtml = "";
    var gateNames = { earnings: "Earnings", red_dog: "Red Dog", ichimoku: "Ichimoku", index_income: "Index Income" };
    for (var gk in gateNames) {
      if (gates[gk] !== undefined) {
        gatesHtml += '<span style="margin-right:8px;font-size:11px;">' + gateNames[gk] + ': ' + gatePill(gates[gk]) + '</span>';
      }
    }
    engineGates.innerHTML = gatesHtml;

    // Flow pressure
    var fp = dms.flow_pressure || {};
    fpScore.textContent = (fp.score || 0).toFixed(0);
    fpLabel.textContent = fp.state || "--";

    // Vol state
    var vs = dms.vol_state || {};
    volState.innerHTML =
      '<span style="font-weight:700;">Term:</span> ' + (vs.term_structure || "--") +
      ' &nbsp; <span style="font-weight:700;">Skew:</span> ' + (vs.skew || "--") +
      ' &nbsp; <span style="font-weight:700;">Level:</span> ' + ((vs.level || 0).toFixed(1));
  }

  /* ── Render: Morning Brief ─────────────────────── */
  function renderBrief(brief) {
    briefCard.style.display = "block";
    var isFallback = brief._source === "fallback";
    briefTs.textContent = fmt(brief._generated_at) + (isFallback ? " (fallback)" : "");

    // If fallback, show the reason prominently
    if (isFallback && brief._fallback_reason) {
      briefStandDown.innerHTML = '<div class="miStandDown" style="background:rgba(255,149,0,0.08);color:#995c00;">' +
        '<b>LLM unavailable:</b> ' + brief._fallback_reason +
        '<br><small>Showing placeholder text. Fix the issue and click Refresh Live Data.</small></div>';
      briefContent.innerHTML = "";
      return;
    }

    // Stand-down banner
    var sd = brief.stand_down || "";
    if (sd && sd.toLowerCase().indexOf("no stand-down") === -1 && sd.toLowerCase().indexOf("not required") === -1) {
      briefStandDown.innerHTML = '<div class="miStandDown">' + sd + '</div>';
    } else {
      briefStandDown.innerHTML = '<div class="miStandDown miStandDown--clear">No stand-down required.</div>';
    }

    var fields = [
      { key: "market_posture",     label: "Market Posture" },
      { key: "changes_vs_yesterday", label: "What Changed" },
      { key: "active_themes",     label: "Active Themes" },
      { key: "cross_asset_signals", label: "Cross-Asset Signals" },
      { key: "engine_alignment",   label: "Engine Alignment" },
      { key: "watch_list",         label: "Watch List" },
    ];

    var html = "";
    fields.forEach(function (f) {
      var val = brief[f.key] || "";
      if (typeof val === "object") val = JSON.stringify(val);
      html += '<div class="miBriefItem"><div class="miBriefLabel">' + f.label + '</div><div class="miBriefText">' + val + '</div></div>';
    });
    briefContent.innerHTML = html;
  }

  /* ── Render: Weekly Roadmap ────────────────────── */
  function renderRoadmap(roadmap) {
    roadmapCard.style.display = "block";
    var isFallback = roadmap._source === "fallback";
    roadmapTs.textContent = fmt(roadmap._generated_at) + (isFallback ? " (fallback)" : "");

    // If fallback, show the reason instead of placeholder text
    if (isFallback && roadmap._fallback_reason) {
      roadmapContent.innerHTML = '<div style="padding:8px 0;color:#995c00;font-size:12px;">' +
        '<b>LLM unavailable:</b> ' + roadmap._fallback_reason + '</div>';
      return;
    }

    var sections = [
      { key: "regime_flow_summary", label: "Regime & Flow Summary" },
      { key: "expected_pattern",    label: "Expected Pattern" },
      { key: "engine_behaviors",    label: "Engine Behaviors" },
      { key: "asymmetry_radar",     label: "Asymmetry Radar" },
      { key: "break_the_plan",      label: "What Would Break the Plan" },
    ];

    var html = "";
    sections.forEach(function (s) {
      var val = roadmap[s.key] || "";
      if (typeof val === "object") val = JSON.stringify(val);
      html += '<div class="miRoadmapSection">' +
        '<div class="miRoadmapHead">' + s.label + '</div>' +
        '<div class="miRoadmapBody">' + val + '</div></div>';
    });

    // High risk days
    var hrd = roadmap.high_risk_days || [];
    if (hrd.length > 0) {
      html += '<div class="miRoadmapSection"><div class="miRoadmapHead">High-Risk Days</div><div class="miRoadmapBody"><ul class="miRoadmapList">';
      hrd.forEach(function (d) { html += "<li>" + d + "</li>"; });
      html += "</ul></div></div>";
    }

    // Earnings focus
    var ef = roadmap.earnings_focus || [];
    if (ef.length > 0) {
      html += '<div class="miRoadmapSection"><div class="miRoadmapHead">Earnings Focus (max 2)</div><div class="miRoadmapBody">';
      ef.forEach(function (t) {
        html += '<span class="pill pill--blue" style="margin-right:6px;">' + t + '</span>';
      });
      html += "</div></div>";
    }

    roadmapContent.innerHTML = html;

    if (roadmap._source === "fallback") {
      roadmapTs.textContent += " (fallback)";
    }
  }

  /* ── Render: Active Themes ─────────────────────── */
  function renderThemes(dms) {
    var themes = dms.news_themes || [];
    if (themes.length === 0) {
      themesContainer.innerHTML = '<div class="miEmpty">No active themes detected.</div>';
      return;
    }

    // Show only themes with intensity > 0
    var active = themes.filter(function (t) { return (t.intensity || 0) > 0; });
    if (active.length === 0) {
      themesContainer.innerHTML = '<div class="miEmpty">All themes below threshold.</div>';
      return;
    }

    var html = "";
    active.forEach(function (t) {
      var barColor = themeBarColor(t.intensity || 0);
      html += '<div class="miThemeCard">' +
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

  /* ── Render: Cross-Asset Stress ────────────────── */
  function renderStress(dms) {
    var xa = dms.cross_asset_stress || {};
    var readings = xa.readings || [];

    if (readings.length === 0) {
      stressGrid.innerHTML = '<div class="miEmpty">No cross-asset data available.</div>';
      return;
    }

    var html = '';
    // Composite header
    html += '<div class="miStressItem" style="grid-column:1/-1;border-left:3px solid ' + stressColor(xa.composite_score || 50) + ';">' +
      '<div class="miStressName">COMPOSITE</div>' +
      '<span class="miStressScore" style="color:' + stressColor(xa.composite_score || 50) + ';">' + (xa.composite_score || 50).toFixed(0) + '</span>' +
      ' <span class="' + pillClass(xa.composite_label) + '">' + (xa.composite_label || "Neutral") + '</span>' +
    '</div>';

    readings.forEach(function (r) {
      var sc = r.stress_score || 50;
      var relCls = "";
      if (r.equity_relationship === "confirming") relCls = "miStressRel--confirming";
      if (r.equity_relationship === "diverging") relCls = "miStressRel--diverging";

      html += '<div class="miStressItem">' +
        '<div class="miStressName">' + (r.name || r.symbol || "") + '</div>' +
        '<div><span class="miStressScore" style="color:' + stressColor(sc) + ';">' + sc.toFixed(0) + '</span>' +
        ' <span class="miStressDir">' + (r.direction || "flat") + '</span></div>' +
        '<div class="miStressChange">' + ((r.change_vs_prior || 0) >= 0 ? "+" : "") + (r.change_vs_prior || 0).toFixed(2) + '%</div>' +
        '<div class="miStressRel ' + relCls + '">' + (r.equity_relationship || "neutral") + ' vs equities</div>' +
      '</div>';
    });

    stressGrid.innerHTML = html;
  }

  /* ── Render: Asymmetry Radar ───────────────────── */
  function renderAsymmetry(dms) {
    var signals = dms.asymmetry_signals || [];

    if (signals.length === 0) {
      asymCard.style.display = "block";
      asymContent.innerHTML = '<div class="miEmpty">No asymmetries detected. All clear.</div>';
      return;
    }

    asymCard.style.display = "block";
    var html = "";
    signals.forEach(function (s) {
      html += '<div class="miAsymCard">' +
        '<div class="miAsymType">' + (s.type || "").replace(/_/g, " ") + '</div>' +
        '<div class="miAsymDesc">' + (s.description || "") + '</div>' +
        '<div class="miAsymAction">' + (s.action || "Monitor only.") + '</div>' +
        '<div style="font-size:10px;color:var(--muted);margin-top:4px;">Sources: ' + (s.sources || []).join(", ") + '</div>' +
      '</div>';
    });
    asymContent.innerHTML = html;
  }

  /* ── Render: Diff ──────────────────────────────── */
  function renderDiff(diffData) {
    diffPanel.style.display = showDiff ? "block" : "none";
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

  /* ── Main load sequence ────────────────────────── */
  function loadAll() {
    showOverlay();
    setProgress(5, "Fetching DailyMarketState...");
    runBtn.disabled = true;

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
          setProgress(70, "Loading Weekly Roadmap...");
          return fetchJSON("/api/front-layer/weekly-roadmap");
        }).then(function (roadmap) {
          renderRoadmap(roadmap);
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
        runBtn.disabled = false;
      });
  }

  /* ── Refresh Live Data ──────────────────────────── */
  function refreshLiveData() {
    if (!refreshBtn) return;
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
        // Show success banner
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
            ' &middot; Flow: ' + ((result.flow_pressure || {}).state || "?") +
            ' &middot; Cross-asset: ' + (result.cross_asset_readings || 0) + ' readings' +
            ' &middot; ' + (result.theme_count || 0) + ' themes' +
            ' &middot; ' + briefStatus;
        }

        // Now reload all panels with the fresh data
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
  if (runBtn) {
    runBtn.addEventListener("click", loadAll);
  }

  if (refreshBtn) {
    refreshBtn.addEventListener("click", refreshLiveData);
  }

  if (diffToggle) {
    diffToggle.addEventListener("click", function () {
      showDiff = !showDiff;
      diffToggle.textContent = showDiff ? "Hide Changes" : "Show Changes";
      if (showDiff && diffPanel.style.display === "none") {
        fetchJSON("/api/front-layer/diff").then(renderDiff).catch(function () {});
      }
      diffPanel.style.display = showDiff ? "block" : "none";
    });
  }

  /* ── Backfill status check ──────────────────────── */
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
            ' &middot; Themes: ' + themeDays + 'd' +
            '</span>';
        } else {
          backfillStatus.className = "miBackfillStatus miBackfillStatus--empty";
          backfillStatus.innerHTML =
            '<span class="miBackfillDot miBackfillDot--amber"></span>' +
            '<span><b>No historical data</b> &middot; ' +
            'Run <code>python3 scripts/backfill_front_layer.py</code> to seed 14 days of cross-asset and theme history for richer insights.</span>';
        }
      })
      .catch(function () {
        // Silently ignore – non-critical
      });
  }

  // Check backfill status on page load
  checkBackfillStatus();

  /* ── Auto-load on page open ────────────────────── */
  hideOverlay();

})();
