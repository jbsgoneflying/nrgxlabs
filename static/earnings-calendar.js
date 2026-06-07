/* ── NRGX Labs  ·  Earnings Calendar (EODHD-only) ───────────────────
   Month / week views of mega-cap ($100 B+) earnings with compare workflow.
   ──────────────────────────────────────────────────────────────────── */
(function () {
  "use strict";

  // ── State ──────────────────────────────────────────────────────────
  var currentView = "month"; // "month" | "week"
  var anchorDate = todayISO();
  var calData = null; // last API response
  var selected = new Set(); // selected tickers (max 10)
  var MAX_SELECT = 10;
  var WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  // ── DOM refs ───────────────────────────────────────────────────────
  var body = document.getElementById("calBody");
  var label = document.getElementById("periodLabel");
  var prevBtn = document.getElementById("prevBtn");
  var nextBtn = document.getElementById("nextBtn");
  var compareBtn = document.getElementById("compareBtn");
  var selBar = document.getElementById("selectionBar");
  var selChips = document.getElementById("selChips");
  var selCount = document.getElementById("selCount");
  var selCompareBtn = document.getElementById("selCompareBtn");

  // ── Helpers ────────────────────────────────────────────────────────
  function todayISO() {
    return new Date().toISOString().slice(0, 10);
  }

  function parseDate(s) {
    var p = s.split("-");
    return new Date(+p[0], +p[1] - 1, +p[2]);
  }

  function fmtDate(d) {
    var y = d.getFullYear();
    var m = String(d.getMonth() + 1).padStart(2, "0");
    var dd = String(d.getDate()).padStart(2, "0");
    return y + "-" + m + "-" + dd;
  }

  function addDays(iso, n) {
    var d = parseDate(iso);
    d.setDate(d.getDate() + n);
    return fmtDate(d);
  }

  function fmtMarketCap(v) {
    if (!v || v <= 0) return "";
    if (v >= 1e12) return "$" + (v / 1e12).toFixed(1) + "T";
    if (v >= 1e9) return "$" + (v / 1e9).toFixed(0) + "B";
    return "$" + (v / 1e6).toFixed(0) + "M";
  }

  /** Deterministic colour from ticker string for fallback logos */
  function tickerColor(t) {
    var h = 0;
    for (var i = 0; i < t.length; i++) h = (h * 31 + t.charCodeAt(i)) & 0xffffff;
    var hue = h % 360;
    return "hsl(" + hue + ",52%,46%)";
  }

  /** Build an <img> with onerror fallback to styled initials */
  function logoHTML(ticker, logoUrl, size) {
    var sz = size || 20;
    var rad = sz <= 24 ? 5 : 10;
    var fs = sz <= 24 ? 9 : 13;
    var initials = ticker.slice(0, 2);
    var bg = tickerColor(ticker);
    var cls = sz <= 24 ? "monthPillLogo" : "weekCardLogo";
    var fbCls = sz <= 24 ? "monthPillFallback" : "weekCardFallback";
    return (
      '<img class="' + cls + '" src="' + logoUrl + '" alt="" width="' + sz + '" height="' + sz + '" ' +
      'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\';" />' +
      '<div class="' + fbCls + '" style="display:none;width:' + sz + 'px;height:' + sz + 'px;border-radius:' +
      rad + 'px;font-size:' + fs + 'px;background:' + bg + ';">' + initials + '</div>'
    );
  }

  function timingBadge(tl) {
    return '<span class="weekCardBadge timing-' + tl + '">' +
      (tl === "BMO" ? "&#9788; " : tl === "AMC" ? "&#9789; " : "&#9201; ") +
      tl + '</span>';
  }

  function surpriseHTML(entry) {
    if (entry.actual == null || entry.estimate == null) return "";
    var diff = entry.actual - entry.estimate;
    if (diff > 0) return '<span class="weekCardSurprise surprise-beat">+' + diff.toFixed(2) + " Beat</span>";
    if (diff < 0) return '<span class="weekCardSurprise surprise-miss">' + diff.toFixed(2) + " Miss</span>";
    return '<span class="weekCardSurprise" style="background:var(--hover);">In-line</span>';
  }

  // ── Selection logic ────────────────────────────────────────────────
  function toggleSelect(ticker) {
    if (selected.has(ticker)) {
      selected.delete(ticker);
    } else {
      if (selected.size >= MAX_SELECT) return;
      selected.add(ticker);
    }
    refreshSelection();
    refreshSelectedStyles();
  }

  function removeSelect(ticker) {
    selected.delete(ticker);
    refreshSelection();
    refreshSelectedStyles();
  }

  function refreshSelection() {
    var count = selected.size;
    // Selection bar visibility
    if (count > 0) {
      selBar.classList.add("visible");
    } else {
      selBar.classList.remove("visible");
    }
    // Count
    selCount.textContent = count + "/" + MAX_SELECT;
    // Compare button in control bar
    if (count > 0) {
      compareBtn.classList.add("enabled");
    } else {
      compareBtn.classList.remove("enabled");
    }
    // Chips
    selChips.innerHTML = "";
    selected.forEach(function (t) {
      var chip = document.createElement("span");
      chip.className = "selChip";
      chip.innerHTML = t + ' <span class="selChipX" data-ticker="' + t + '">&times;</span>';
      selChips.appendChild(chip);
    });
    // Chip remove handlers
    selChips.querySelectorAll(".selChipX").forEach(function (el) {
      el.addEventListener("click", function (e) {
        e.stopPropagation();
        removeSelect(el.dataset.ticker);
      });
    });
  }

  function refreshSelectedStyles() {
    document.querySelectorAll("[data-ticker-sel]").forEach(function (el) {
      var t = el.dataset.tickerSel;
      if (selected.has(t)) {
        el.classList.add("selected");
      } else {
        el.classList.remove("selected");
      }
      // Update checkbox in week view
      var chk = el.querySelector(".weekCardCheck");
      if (chk) {
        chk.innerHTML = selected.has(t) ? '<svg width="14" height="14" viewBox="0 0 14 14"><polyline points="3,7 6,10 11,4" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>' : "";
      }
    });
  }

  function goToCompare() {
    if (selected.size === 0) return;
    var tickers = Array.from(selected).join(",");
    window.location.href = "/compare?tickers=" + encodeURIComponent(tickers) + "&k=1.5";
  }

  // ── Fetch ──────────────────────────────────────────────────────────
  function fetchCalendar() {
    body.innerHTML = '<div class="calLoading">Loading earnings data...</div>';
    var url = "/api/earnings-calendar?view=" + currentView + "&anchor=" + anchorDate;
    fetch(url)
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        calData = data;
        label.textContent = data.label || "";
        render();
      })
      .catch(function (err) {
        body.innerHTML = '<div class="calEmpty">Failed to load calendar: ' + err.message + "</div>";
      });
  }

  // ── Render (dispatcher) ────────────────────────────────────────────
  function render() {
    if (!calData) return;
    if (currentView === "week") {
      renderWeek();
    } else {
      renderMonth();
    }
    refreshSelectedStyles();
  }

  // ── Month Render ───────────────────────────────────────────────────
  function renderMonth() {
    var days = calData.days || {};
    var startISO = calData.start;
    var endISO = calData.end;
    var anchorD = parseDate(calData.anchor);
    var anchorMonth = anchorD.getMonth();
    var todayStr = todayISO();

    var html = '<div class="monthGrid">';
    // Day-of-week headers
    for (var i = 0; i < 7; i++) {
      html += '<div class="monthDayHeader">' + WEEKDAYS[i] + "</div>";
    }

    // Cells
    var cur = startISO;
    while (cur <= endISO) {
      var d = parseDate(cur);
      var isOutside = d.getMonth() !== anchorMonth;
      var isToday = cur === todayStr;
      var cls = "monthCell" + (isOutside ? " outside" : "") + (isToday ? " today" : "");
      html += '<div class="' + cls + '">';
      html += '<div class="monthCellDate' + (isToday ? " today" : "") + '">' + d.getDate() + "</div>";

      var entries = days[cur] || [];
      html += '<div class="monthCellEarnings">';
      var showCount = Math.min(entries.length, 5);
      for (var j = 0; j < showCount; j++) {
        var e = entries[j];
        html += monthPillHTML(e);
      }
      if (entries.length > 5) {
        html += '<div class="monthMore" data-date="' + cur + '">+' + (entries.length - 5) + " more</div>";
      }
      html += "</div></div>";
      cur = addDays(cur, 1);
    }

    html += "</div>";
    body.innerHTML = html;

    // Pill click handlers
    body.querySelectorAll(".monthPill").forEach(function (el) {
      el.addEventListener("click", function () {
        toggleSelect(el.dataset.tickerSel);
      });
    });
    // "More" expand handlers
    body.querySelectorAll(".monthMore").forEach(function (el) {
      el.addEventListener("click", function () {
        var dt = el.dataset.date;
        var entries = (calData.days || {})[dt] || [];
        // Replace the monthMore with remaining pills
        var frag = document.createDocumentFragment();
        for (var k = 5; k < entries.length; k++) {
          var pill = document.createElement("div");
          pill.innerHTML = monthPillHTML(entries[k]);
          var node = pill.firstElementChild;
          node.addEventListener("click", (function(ticker) {
            return function () { toggleSelect(ticker); };
          })(entries[k].ticker));
          frag.appendChild(node);
        }
        el.parentNode.replaceChild(frag, el);
        refreshSelectedStyles();
      });
    });
  }

  function monthPillHTML(e) {
    return (
      '<div class="monthPill" data-ticker-sel="' + e.ticker + '">' +
      logoHTML(e.ticker, e.logo_url, 20) +
      '<span class="monthPillTicker">' + e.ticker + "</span>" +
      '<span class="monthPillTiming timing-' + e.timing_label + '">' + e.timing_label + "</span>" +
      "</div>"
    );
  }

  // ── Week Render ────────────────────────────────────────────────────
  function renderWeek() {
    var days = calData.days || {};
    var startISO = calData.start;
    var todayStr = todayISO();

    var html = '<div class="weekGrid">';
    for (var i = 0; i < 5; i++) {
      // Mon through Fri
      var dayISO = addDays(startISO, i);
      var d = parseDate(dayISO);
      var isToday = dayISO === todayStr;
      var dayLabel = WEEKDAYS[i] + ", " + d.toLocaleDateString("en-US", { month: "short", day: "numeric" });

      html += '<div class="weekCol">';
      html += '<div class="weekColHeader' + (isToday ? " today" : "") + '">' + dayLabel + "</div>";
      html += '<div class="weekColBody">';

      var entries = days[dayISO] || [];
      if (entries.length === 0) {
        html += '<div style="color:var(--muted2);font-size:12px;padding:12px 0;text-align:center;">No earnings</div>';
      }
      for (var j = 0; j < entries.length; j++) {
        html += weekCardHTML(entries[j]);
      }

      html += "</div></div>";
    }
    html += "</div>";
    body.innerHTML = html;

    // Card click handlers
    body.querySelectorAll(".weekCard").forEach(function (el) {
      el.addEventListener("click", function () {
        toggleSelect(el.dataset.tickerSel);
      });
    });
  }

  function weekCardHTML(e) {
    var epsStr = "";
    if (e.estimate != null) {
      epsStr = '<span class="weekCardEps">Est: $' + e.estimate.toFixed(2) + "</span>";
    }
    return (
      '<div class="weekCard" data-ticker-sel="' + e.ticker + '">' +
      '<div class="weekCardTop">' +
      logoHTML(e.ticker, e.logo_url, 36) +
      '<div class="weekCardInfo">' +
      '<div class="weekCardName">' + e.name + "</div>" +
      '<div class="weekCardTicker">' + e.ticker + "</div>" +
      "</div>" +
      '<div class="weekCardCheck"></div>' +
      "</div>" +
      '<div class="weekCardMeta">' +
      timingBadge(e.timing_label) +
      epsStr +
      surpriseHTML(e) +
      '<span class="weekCardMcap">' + fmtMarketCap(e.market_cap) + "</span>" +
      "</div>" +
      "</div>"
    );
  }

  // ── Navigation ─────────────────────────────────────────────────────
  function navigate(direction) {
    var d = parseDate(anchorDate);
    if (currentView === "month") {
      d.setMonth(d.getMonth() + direction);
    } else {
      d.setDate(d.getDate() + 7 * direction);
    }
    anchorDate = fmtDate(d);
    fetchCalendar();
  }

  function setView(v) {
    if (v === currentView) return;
    currentView = v;
    document.querySelectorAll(".calViewBtn").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.view === v);
    });
    fetchCalendar();
  }

  // ── Init ───────────────────────────────────────────────────────────
  prevBtn.addEventListener("click", function () { navigate(-1); });
  nextBtn.addEventListener("click", function () { navigate(1); });
  compareBtn.addEventListener("click", goToCompare);
  selCompareBtn.addEventListener("click", goToCompare);
  document.querySelectorAll(".calViewBtn").forEach(function (btn) {
    btn.addEventListener("click", function () { setView(btn.dataset.view); });
  });

  // Keyboard nav
  document.addEventListener("keydown", function (e) {
    if (e.key === "ArrowLeft") navigate(-1);
    if (e.key === "ArrowRight") navigate(1);
  });

  fetchCalendar();
})();
