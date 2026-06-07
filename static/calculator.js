/**
 * NRGX Labs Calculator
 * Apple-style draggable calculator popup
 */

(function() {
  "use strict";

  let calculatorOpen = false;
  let _calcDragBound = false;

  // ─────────────────────────────────────────────────────────────────────────────
  // Calculator HTML Template
  // ─────────────────────────────────────────────────────────────────────────────

  function getCalculatorHTML() {
    return `
      <div class="calcHeader">
        <div class="calcDragHandle">
          <span class="calcTitle">Calculator</span>
        </div>
        <button class="calcCloseBtn" type="button" aria-label="Close calculator">×</button>
      </div>
      
      <div class="calcBody">
        <div class="calcTabs">
          <button class="calcTab isActive" data-tab="roc">ROC %</button>
          <button class="calcTab" data-tab="more" disabled title="Coming soon">More</button>
        </div>
        
        <div class="calcPanel" data-panel="roc">
          <div class="calcDisplay">
            <div class="calcDisplayLabel">Return on Capital</div>
            <div class="calcDisplayValue" id="calcRocResult">—</div>
          </div>
          
          <div class="calcSection">
            <label class="calcLabel">Iron Condor Wing Size</label>
            <div class="calcWingBtns">
              <button class="calcWingBtn" data-wing="1">$1</button>
              <button class="calcWingBtn" data-wing="2">$2</button>
              <button class="calcWingBtn isActive" data-wing="5">$5</button>
              <button class="calcWingBtn" data-wing="10">$10</button>
              <button class="calcWingBtn" data-wing="20">$20</button>
            </div>
          </div>
          
          <div class="calcSection">
            <label class="calcLabel" for="calcPremium">Premium Collected ($)</label>
            <input 
              type="text" 
              id="calcPremium" 
              class="calcInput" 
              inputmode="decimal" 
              placeholder="0.00"
              autocomplete="off"
            />
          </div>
          
          <div class="calcResults">
            <div class="calcResultRow">
              <span class="calcResultLabel">Wing Width:</span>
              <span class="calcResultValue" id="calcWingDisplay">$5.00</span>
            </div>
            <div class="calcResultRow">
              <span class="calcResultLabel">Max Risk:</span>
              <span class="calcResultValue" id="calcMaxRisk">—</span>
            </div>
            <div class="calcResultRow calcResultRow--highlight">
              <span class="calcResultLabel">ROC %:</span>
              <span class="calcResultValue" id="calcRocPct">—</span>
            </div>
          </div>
          
          <button class="calcClearBtn" type="button">Clear</button>
        </div>
      </div>
    `;
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Create Calculator Element
  // ─────────────────────────────────────────────────────────────────────────────

  function createCalculator() {
    let calc = document.getElementById("ravenCalculator");
    if (calc) return calc;

    calc = document.createElement("div");
    calc.id = "ravenCalculator";
    calc.className = "ravenCalc hidden";
    calc.innerHTML = getCalculatorHTML();
    document.body.appendChild(calc);

    // Bind events
    bindCalculatorEvents(calc);

    return calc;
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Event Bindings
  // ─────────────────────────────────────────────────────────────────────────────

  function bindCalculatorEvents(calc) {
    // Close button
    const closeBtn = calc.querySelector(".calcCloseBtn");
    if (closeBtn) {
      closeBtn.addEventListener("click", closeCalculator);
    }

    // Drag functionality
    if (!_calcDragBound) {
      const header = calc.querySelector(".calcHeader");
      if (header) initDrag(calc, header, { closeSelector: ".calcCloseBtn" });
      _calcDragBound = true;
    }

    // Wing size buttons
    const wingBtns = calc.querySelectorAll(".calcWingBtn");
    wingBtns.forEach(btn => {
      btn.addEventListener("click", () => {
        wingBtns.forEach(b => b.classList.remove("isActive"));
        btn.classList.add("isActive");
        updateWingDisplay();
        calculateROC();
      });
    });

    // Premium input
    const premiumInput = calc.querySelector("#calcPremium");
    if (premiumInput) {
      premiumInput.addEventListener("input", calculateROC);
      premiumInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          calculateROC();
        }
      });
    }

    // Clear button
    const clearBtn = calc.querySelector(".calcClearBtn");
    if (clearBtn) {
      clearBtn.addEventListener("click", clearCalculator);
    }

    // Tab switching
    const tabs = calc.querySelectorAll(".calcTab");
    tabs.forEach(tab => {
      tab.addEventListener("click", () => {
        if (tab.disabled) return;
        tabs.forEach(t => t.classList.remove("isActive"));
        tab.classList.add("isActive");
        // Future: switch panels based on data-tab
      });
    });
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Calculator Logic
  // ─────────────────────────────────────────────────────────────────────────────

  function getSelectedWing() {
    const calc = document.getElementById("ravenCalculator");
    if (!calc) return 5;
    const activeBtn = calc.querySelector(".calcWingBtn.isActive");
    return activeBtn ? parseFloat(activeBtn.dataset.wing) : 5;
  }

  function updateWingDisplay() {
    const wing = getSelectedWing();
    const display = document.getElementById("calcWingDisplay");
    if (display) {
      display.textContent = `$${wing.toFixed(2)}`;
    }
  }

  function calculateROC() {
    const calc = document.getElementById("ravenCalculator");
    if (!calc) return;

    const premiumInput = calc.querySelector("#calcPremium");
    const resultEl = document.getElementById("calcRocResult");
    const maxRiskEl = document.getElementById("calcMaxRisk");
    const rocPctEl = document.getElementById("calcRocPct");

    const premium = parseFloat(premiumInput?.value || "0");
    const wing = getSelectedWing();

    if (isNaN(premium) || premium <= 0) {
      resultEl.textContent = "—";
      maxRiskEl.textContent = "—";
      rocPctEl.textContent = "—";
      return;
    }

    // Iron Condor Max Risk = Wing Width - Premium Collected
    const maxRisk = wing - premium;

    if (maxRisk <= 0) {
      resultEl.textContent = "∞";
      maxRiskEl.textContent = "$0.00";
      rocPctEl.textContent = "∞";
      return;
    }

    // ROC % = (Premium / Max Risk) × 100
    const rocPct = (premium / maxRisk) * 100;

    resultEl.textContent = rocPct.toFixed(2) + "%";
    maxRiskEl.textContent = "$" + maxRisk.toFixed(2);
    rocPctEl.textContent = rocPct.toFixed(2) + "%";
  }

  function clearCalculator() {
    const calc = document.getElementById("ravenCalculator");
    if (!calc) return;

    const premiumInput = calc.querySelector("#calcPremium");
    if (premiumInput) premiumInput.value = "";

    document.getElementById("calcRocResult").textContent = "—";
    document.getElementById("calcMaxRisk").textContent = "—";
    document.getElementById("calcRocPct").textContent = "—";
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Open / Close
  // ─────────────────────────────────────────────────────────────────────────────

  function openCalculator() {
    const calc = createCalculator();
    calc.classList.remove("hidden");
    calculatorOpen = true;

    // Position in bottom-right by default if not already positioned
    if (!calc.style.left || calc.style.left === "auto") {
      calc.style.right = "20px";
      calc.style.bottom = "20px";
      calc.style.left = "auto";
      calc.style.top = "auto";
    }

    // Focus the input
    setTimeout(() => {
      const input = calc.querySelector("#calcPremium");
      if (input) input.focus();
    }, 100);
  }

  function closeCalculator() {
    const calc = document.getElementById("ravenCalculator");
    if (calc) {
      calc.classList.add("hidden");
    }
    calculatorOpen = false;
  }

  function toggleCalculator() {
    if (calculatorOpen) {
      closeCalculator();
    } else {
      openCalculator();
    }
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Add Calculator Button to Page
  // ─────────────────────────────────────────────────────────────────────────────

  function addCalculatorButton() {
    // Check if button already exists
    if (document.getElementById("calcToggleBtn")) return;

    const btn = document.createElement("button");
    btn.id = "calcToggleBtn";
    btn.className = "calcToggleBtn";
    btn.type = "button";
    btn.setAttribute("aria-label", "Open calculator");
    btn.innerHTML = `
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="4" y="2" width="16" height="20" rx="2"/>
        <line x1="8" y1="6" x2="16" y2="6"/>
        <line x1="8" y1="10" x2="8" y2="10.01"/>
        <line x1="12" y1="10" x2="12" y2="10.01"/>
        <line x1="16" y1="10" x2="16" y2="10.01"/>
        <line x1="8" y1="14" x2="8" y2="14.01"/>
        <line x1="12" y1="14" x2="12" y2="14.01"/>
        <line x1="16" y1="14" x2="16" y2="14.01"/>
        <line x1="8" y1="18" x2="8" y2="18.01"/>
        <line x1="12" y1="18" x2="16" y2="18"/>
      </svg>
      <span>Calc</span>
    `;
    btn.addEventListener("click", toggleCalculator);

    document.body.appendChild(btn);
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Keyboard Shortcut
  // ─────────────────────────────────────────────────────────────────────────────

  function handleKeyboard(e) {
    // Cmd/Ctrl + K to toggle calculator
    if ((e.metaKey || e.ctrlKey) && e.key === "k") {
      e.preventDefault();
      toggleCalculator();
    }
    // Escape to close
    if (e.key === "Escape" && calculatorOpen) {
      closeCalculator();
    }
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // Initialize
  // ─────────────────────────────────────────────────────────────────────────────

  function init() {
    addCalculatorButton();
    document.addEventListener("keydown", handleKeyboard);
  }

  // Run on DOM ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Export for external use
  window.RavenCalculator = {
    open: openCalculator,
    close: closeCalculator,
    toggle: toggleCalculator,
  };

})();
