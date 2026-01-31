/**
 * Position Size Calculator
 * Apple-style draggable calculator for building trades around entry/stop/target
 * Used by Engine 3 (Red Dog) and Engine 4 (Ichimoku)
 */

(function() {
  "use strict";

  let posCalcOpen = false;
  let currentSignal = null;
  let dragState = { isDragging: false, offsetX: 0, offsetY: 0 };

  // Persist account settings in localStorage
  const STORAGE_KEY = "ravenPosCalcSettings";

  function loadSettings() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) return JSON.parse(saved);
    } catch (e) { /* ignore */ }
    return { accountValue: 100000, riskPct: 1.0 };
  }

  function saveSettings(accountValue, riskPct) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ accountValue, riskPct }));
    } catch (e) { /* ignore */ }
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Calculator HTML Template
  // ─────────────────────────────────────────────────────────────────────────────

  function getCalculatorHTML(signal) {
    const ticker = signal?.ticker || "—";
    const direction = signal?.direction || "bullish";
    const entry = signal?.levels?.entryTrigger || signal?.entry || 0;
    const stop = signal?.levels?.stopLoss || signal?.stop || 0;
    // For Engine 3 (Red Dog mean reversion), prefer targetSma20 over target1 (which is just 1R)
    // This gives a more realistic risk:reward for mean reversion trades
    const target1Raw = signal?.levels?.target1 || signal?.target1 || 0;
    const targetSma20 = signal?.levels?.targetSma20 || 0;
    const target1 = (targetSma20 > 0 && targetSma20 !== target1Raw) ? targetSma20 : target1Raw;
    const dirClass = direction === "bullish" ? "bullish" : "bearish";
    const dirLabel = direction === "bullish" ? "LONG" : "SHORT";

    const settings = loadSettings();

    return `
      <div class="posCalcHeader">
        <div class="posCalcDragHandle">
          <span class="posCalcTitle">Position Calculator</span>
        </div>
        <button class="posCalcCloseBtn" type="button" aria-label="Close calculator">×</button>
      </div>
      
      <div class="posCalcBody">
        <!-- Signal Info -->
        <div class="posCalcSignalInfo">
          <div class="posCalcTicker">${ticker}</div>
          <span class="posCalcDirection ${dirClass}">${dirLabel}</span>
        </div>

        <!-- Trade Levels (Read-only) -->
        <div class="posCalcLevels">
          <div class="posCalcLevel">
            <span class="posCalcLevelLabel">Entry</span>
            <span class="posCalcLevelValue" id="posCalcEntry">$${formatPrice(entry)}</span>
          </div>
          <div class="posCalcLevel">
            <span class="posCalcLevelLabel">Stop</span>
            <span class="posCalcLevelValue posCalcStop" id="posCalcStop">$${formatPrice(stop)}</span>
          </div>
          <div class="posCalcLevel">
            <span class="posCalcLevelLabel">Target 1</span>
            <span class="posCalcLevelValue posCalcTarget" id="posCalcTarget">$${formatPrice(target1)}</span>
          </div>
        </div>

        <!-- Account Inputs -->
        <div class="posCalcSection">
          <label class="posCalcLabel" for="posCalcAccount">Account Value ($)</label>
          <input 
            type="text" 
            id="posCalcAccount" 
            class="posCalcInput" 
            inputmode="decimal" 
            placeholder="100,000"
            value="${formatNumber(settings.accountValue)}"
            autocomplete="off"
          />
        </div>

        <div class="posCalcSection">
          <label class="posCalcLabel">Risk Per Trade (%)</label>
          <div class="posCalcRiskBtns">
            <button class="posCalcRiskBtn ${settings.riskPct === 0.5 ? 'isActive' : ''}" data-risk="0.5">0.5%</button>
            <button class="posCalcRiskBtn ${settings.riskPct === 1.0 ? 'isActive' : ''}" data-risk="1.0">1%</button>
            <button class="posCalcRiskBtn ${settings.riskPct === 1.5 ? 'isActive' : ''}" data-risk="1.5">1.5%</button>
            <button class="posCalcRiskBtn ${settings.riskPct === 2.0 ? 'isActive' : ''}" data-risk="2.0">2%</button>
          </div>
        </div>

        <!-- Results Display -->
        <div class="posCalcDisplay">
          <div class="posCalcDisplayLabel">Shares to Buy</div>
          <div class="posCalcDisplayValue" id="posCalcShares">—</div>
        </div>

        <div class="posCalcResults">
          <div class="posCalcResultRow">
            <span class="posCalcResultLabel">Risk per Share</span>
            <span class="posCalcResultValue" id="posCalcRiskPerShare">—</span>
          </div>
          <div class="posCalcResultRow">
            <span class="posCalcResultLabel">Amount at Risk</span>
            <span class="posCalcResultValue posCalcRiskValue" id="posCalcAmountAtRisk">—</span>
          </div>
          <div class="posCalcResultRow">
            <span class="posCalcResultLabel">Total Trade Value</span>
            <span class="posCalcResultValue" id="posCalcTradeValue">—</span>
          </div>
          <div class="posCalcResultRow posCalcResultRow--highlight">
            <span class="posCalcResultLabel">Profit at Target 1</span>
            <span class="posCalcResultValue" id="posCalcProfit">—</span>
          </div>
          <div class="posCalcResultRow">
            <span class="posCalcResultLabel">Risk:Reward</span>
            <span class="posCalcResultValue" id="posCalcRR">—</span>
          </div>
        </div>

        <button class="posCalcAnalyzeBtn" type="button" id="posCalcAnalyzeBtn">
          Open Full Analysis →
        </button>
      </div>
    `;
  }

  function formatPrice(n) {
    const num = Number(n);
    if (!Number.isFinite(num)) return "—";
    return num.toFixed(2);
  }

  function formatNumber(n) {
    const num = Number(n);
    if (!Number.isFinite(num)) return "";
    return num.toLocaleString("en-US", { maximumFractionDigits: 0 });
  }

  function parseNumber(str) {
    if (!str) return NaN;
    // Remove commas and parse
    return parseFloat(str.replace(/,/g, ""));
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Create Calculator Element
  // ─────────────────────────────────────────────────────────────────────────────

  function createCalculator(signal) {
    let calc = document.getElementById("positionCalculator");
    if (calc) {
      calc.innerHTML = getCalculatorHTML(signal);
    } else {
      calc = document.createElement("div");
      calc.id = "positionCalculator";
      calc.className = "posCalc hidden";
      calc.innerHTML = getCalculatorHTML(signal);
      document.body.appendChild(calc);
    }

    currentSignal = signal;
    bindCalculatorEvents(calc);
    return calc;
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Event Bindings
  // ─────────────────────────────────────────────────────────────────────────────

  function bindCalculatorEvents(calc) {
    // Close button
    const closeBtn = calc.querySelector(".posCalcCloseBtn");
    if (closeBtn) {
      closeBtn.addEventListener("click", closeCalculator);
    }

    // Drag functionality
    const header = calc.querySelector(".posCalcHeader");
    if (header) {
      header.addEventListener("mousedown", startDrag);
      header.addEventListener("touchstart", startDrag, { passive: false });
    }

    // Risk buttons
    const riskBtns = calc.querySelectorAll(".posCalcRiskBtn");
    riskBtns.forEach(btn => {
      btn.addEventListener("click", () => {
        riskBtns.forEach(b => b.classList.remove("isActive"));
        btn.classList.add("isActive");
        calculatePosition();
      });
    });

    // Account input
    const accountInput = calc.querySelector("#posCalcAccount");
    if (accountInput) {
      accountInput.addEventListener("input", calculatePosition);
      accountInput.addEventListener("blur", () => {
        // Format on blur
        const val = parseNumber(accountInput.value);
        if (Number.isFinite(val)) {
          accountInput.value = formatNumber(val);
        }
      });
      accountInput.addEventListener("focus", () => {
        // Remove formatting on focus for easier editing
        const val = parseNumber(accountInput.value);
        if (Number.isFinite(val)) {
          accountInput.value = val;
        }
      });
    }

    // Analyze button (open Engine 1)
    const analyzeBtn = calc.querySelector("#posCalcAnalyzeBtn");
    if (analyzeBtn) {
      analyzeBtn.addEventListener("click", () => {
        if (currentSignal?.ticker) {
          const url = `/breach?ticker=${encodeURIComponent(currentSignal.ticker)}&k=1.5&mc=1&autorun=1`;
          window.open(url, "_blank");
        }
      });
    }
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Drag Logic
  // ─────────────────────────────────────────────────────────────────────────────

  function startDrag(e) {
    const calc = document.getElementById("positionCalculator");
    if (!calc) return;
    if (e.target.closest(".posCalcCloseBtn")) return;

    dragState.isDragging = true;
    calc.classList.add("isDragging");

    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    const clientY = e.touches ? e.touches[0].clientY : e.clientY;

    const rect = calc.getBoundingClientRect();
    dragState.offsetX = clientX - rect.left;
    dragState.offsetY = clientY - rect.top;

    e.preventDefault();
  }

  function onDrag(e) {
    if (!dragState.isDragging) return;

    const calc = document.getElementById("positionCalculator");
    if (!calc) return;

    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    const clientY = e.touches ? e.touches[0].clientY : e.clientY;

    let newX = clientX - dragState.offsetX;
    let newY = clientY - dragState.offsetY;

    const maxX = window.innerWidth - calc.offsetWidth;
    const maxY = window.innerHeight - calc.offsetHeight;
    newX = Math.max(0, Math.min(newX, maxX));
    newY = Math.max(0, Math.min(newY, maxY));

    calc.style.left = newX + "px";
    calc.style.top = newY + "px";
    calc.style.right = "auto";
    calc.style.bottom = "auto";

    e.preventDefault();
  }

  function endDrag() {
    if (!dragState.isDragging) return;
    dragState.isDragging = false;

    const calc = document.getElementById("positionCalculator");
    if (calc) calc.classList.remove("isDragging");
  }

  // Global drag listeners
  document.addEventListener("mousemove", onDrag);
  document.addEventListener("touchmove", onDrag, { passive: false });
  document.addEventListener("mouseup", endDrag);
  document.addEventListener("touchend", endDrag);

  // ─────────────────────────────────────────────────────────────────────────────
  // Calculator Logic
  // ─────────────────────────────────────────────────────────────────────────────

  function getSelectedRisk() {
    const calc = document.getElementById("positionCalculator");
    if (!calc) return 1.0;
    const activeBtn = calc.querySelector(".posCalcRiskBtn.isActive");
    return activeBtn ? parseFloat(activeBtn.dataset.risk) : 1.0;
  }

  function calculatePosition() {
    const calc = document.getElementById("positionCalculator");
    if (!calc || !currentSignal) return;

    const accountInput = calc.querySelector("#posCalcAccount");
    const accountValue = parseNumber(accountInput?.value || "0");
    const riskPct = getSelectedRisk();

    // Save settings
    if (Number.isFinite(accountValue) && accountValue > 0) {
      saveSettings(accountValue, riskPct);
    }

    // Get trade levels from signal
    const entry = currentSignal?.levels?.entryTrigger || currentSignal?.entry || 0;
    const stop = currentSignal?.levels?.stopLoss || currentSignal?.stop || 0;
    // For Engine 3 (Red Dog mean reversion), prefer targetSma20 over target1 (which is just 1R)
    const target1Raw = currentSignal?.levels?.target1 || currentSignal?.target1 || 0;
    const targetSma20 = currentSignal?.levels?.targetSma20 || 0;
    const target1 = (targetSma20 > 0 && targetSma20 !== target1Raw) ? targetSma20 : target1Raw;
    const direction = currentSignal?.direction || "bullish";

    // Calculate risk per share
    let riskPerShare;
    if (direction === "bullish") {
      riskPerShare = entry - stop;
    } else {
      riskPerShare = stop - entry;
    }

    // Get DOM elements
    const sharesEl = document.getElementById("posCalcShares");
    const riskPerShareEl = document.getElementById("posCalcRiskPerShare");
    const amountAtRiskEl = document.getElementById("posCalcAmountAtRisk");
    const tradeValueEl = document.getElementById("posCalcTradeValue");
    const profitEl = document.getElementById("posCalcProfit");
    const rrEl = document.getElementById("posCalcRR");

    // Validate inputs
    if (!Number.isFinite(accountValue) || accountValue <= 0 || 
        !Number.isFinite(riskPerShare) || riskPerShare <= 0 ||
        !Number.isFinite(entry) || entry <= 0) {
      sharesEl.textContent = "—";
      riskPerShareEl.textContent = "—";
      amountAtRiskEl.textContent = "—";
      tradeValueEl.textContent = "—";
      profitEl.textContent = "—";
      rrEl.textContent = "—";
      return;
    }

    // Calculate max risk amount
    const maxRiskAmount = accountValue * (riskPct / 100);

    // Calculate shares (floor to whole shares)
    const shares = Math.floor(maxRiskAmount / riskPerShare);

    if (shares <= 0) {
      sharesEl.textContent = "0";
      riskPerShareEl.textContent = "$" + riskPerShare.toFixed(2);
      amountAtRiskEl.textContent = "—";
      tradeValueEl.textContent = "—";
      profitEl.textContent = "—";
      rrEl.textContent = "—";
      return;
    }

    // Calculate actual amounts
    const actualRisk = shares * riskPerShare;
    const tradeValue = shares * entry;

    // Calculate profit at target1
    let profitPerShare;
    if (direction === "bullish") {
      profitPerShare = target1 - entry;
    } else {
      profitPerShare = entry - target1;
    }
    const totalProfit = shares * profitPerShare;

    // Calculate risk:reward ratio
    const rrRatio = profitPerShare > 0 ? (profitPerShare / riskPerShare) : 0;

    // Update display
    sharesEl.textContent = shares.toLocaleString("en-US");
    riskPerShareEl.textContent = "$" + riskPerShare.toFixed(2);
    amountAtRiskEl.textContent = "$" + actualRisk.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    tradeValueEl.textContent = "$" + tradeValue.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    
    if (totalProfit > 0) {
      profitEl.textContent = "+$" + totalProfit.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    } else {
      profitEl.textContent = "$" + totalProfit.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    
    rrEl.textContent = "1:" + rrRatio.toFixed(2);
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Open / Close
  // ─────────────────────────────────────────────────────────────────────────────

  function openCalculator(signal, event) {
    const calc = createCalculator(signal);
    calc.classList.remove("hidden");
    posCalcOpen = true;

    // Position near click or center of screen
    if (event) {
      const clickX = event.clientX || (event.touches ? event.touches[0].clientX : window.innerWidth / 2);
      const clickY = event.clientY || (event.touches ? event.touches[0].clientY : window.innerHeight / 2);
      
      // Position to the right of click, or left if not enough room
      let posX = clickX + 20;
      let posY = clickY - 100;

      // Keep within viewport
      const calcWidth = 320;
      const calcHeight = 500;
      
      if (posX + calcWidth > window.innerWidth - 20) {
        posX = clickX - calcWidth - 20;
      }
      if (posX < 20) posX = 20;
      
      if (posY + calcHeight > window.innerHeight - 20) {
        posY = window.innerHeight - calcHeight - 20;
      }
      if (posY < 20) posY = 20;

      calc.style.left = posX + "px";
      calc.style.top = posY + "px";
      calc.style.right = "auto";
      calc.style.bottom = "auto";
    } else {
      // Center in viewport
      calc.style.left = "50%";
      calc.style.top = "50%";
      calc.style.transform = "translate(-50%, -50%)";
    }

    // Calculate initial position
    setTimeout(calculatePosition, 50);
  }

  function closeCalculator() {
    const calc = document.getElementById("positionCalculator");
    if (calc) {
      calc.classList.add("hidden");
      calc.style.transform = "";
    }
    posCalcOpen = false;
    currentSignal = null;
  }

  // Close on Escape
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && posCalcOpen) {
      closeCalculator();
    }
  });

  // Close when clicking outside
  document.addEventListener("click", (e) => {
    if (!posCalcOpen) return;
    const calc = document.getElementById("positionCalculator");
    if (calc && !calc.contains(e.target) && !e.target.closest(".signalCard")) {
      closeCalculator();
    }
  });

  // ─────────────────────────────────────────────────────────────────────────────
  // Export for external use
  // ─────────────────────────────────────────────────────────────────────────────

  window.PositionCalculator = {
    open: openCalculator,
    close: closeCalculator,
    isOpen: () => posCalcOpen,
  };

})();
