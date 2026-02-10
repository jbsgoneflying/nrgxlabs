# Raven Tech – Global Lead–Lag Engine  
**Weekly Income Intelligence System (EOD-Driven)**

---

## 1. Purpose and Philosophy

The Raven Tech Global Lead–Lag Engine is an **idea-generation and correlation intelligence system**, not an auto-trading platform.

Its purpose is to:
- Ingest **global end-of-day (EOD)** market data
- Detect **lead–lag relationships** across regions, assets, and regimes
- Translate those signals into **probabilistic weekly biases**
- Support **US options income harvesting** decisions
- Remove intraday emotion and discretionary noise

The engine generates **plans, confidence scores, and trade ideas**, not executions.

---

## 2. Core Design Principles

- **EOD only**: No intraday data required or consumed
- **Weekly horizon**: Signals target 3–7 trading day outcomes
- **No auto-trading**: Human execution remains external
- **Regime-aware**: Signals suppressed in unstable conditions
- **Options-first monetization**: Designed around weekly income strategies
- **Emotion minimization**: All planning occurs outside market hours

---

## 3. Data Stack

### 3.1 EODHD – All-In-One Plan (Global Context Layer)
Used for global market truth and macro regime awareness.

**Data consumed**
- Global equity indexes  
  - Europe (STOXX, DAX, CAC, FTSE proxies)
  - Asia (Nikkei, Hang Seng, Shanghai)
  - Australia (ASX)
- FX spot (EOD)  
  - EURUSD, USDJPY, AUDUSD
- Commodities  
  - Oil, Copper, Gold
- Sovereign yields  
  - US 2Y, US 10Y
  - German Bund 10Y
  - Japan JGB 10Y
- Macroeconomic series (selective)

**Role**
- “What happened while the US was asleep?”
- Global risk tone
- Macro stress confirmation

---

### 3.2 ORATS LiveData API (Options Intelligence Layer)
Used for US options monetization and structure selection.

**Data consumed**
- Options chains
- Implied volatility
- Skew and term structure
- Expected move metrics
- Underlying OHLCV where needed
- Event awareness (earnings proximity)

**Role**
- Translate directional and volatility bias into **income-oriented structures**
- Maintain probabilistic framing and defined risk logic

---

### 3.3 Benzinga API (News and Event Filter Layer)
Used only as a **contextual and suppression layer**, not for prediction.

**Data consumed**
- High-impact macro headlines
- Earnings announcements
- Major geopolitical or policy events

**Role**
- Flag weeks or symbols where statistical signals may be overridden by event risk
- Add human-readable context to Raven outputs

---

## 4. Engine Architecture

Global Markets (EODHD)
↓
Lead–Lag Detection
↓
Regime Classification
↓
Translation Engine
↓
Options Structure Mapping (ORATS)
↓
Weekly Idea Output (Raven)


---

## 5. Engine Modules

### 5.1 Global Market Intake Module
**Runs nightly after global markets close**

- Pull EOD prices for all configured global assets
- Normalize returns:
  - Local currency
  - USD-converted
  - Volatility-adjusted (z-scores)
- Store session-aware timestamps

**Output**
- Clean global return vectors
- Cross-asset movement summaries

---

### 5.2 Lead–Lag Detection Module
Identifies statistically meaningful **preceding signals**.

**Features**
- Rolling correlations with lags (1–5 days)
- Cross-market confirmation counts
- Sector-to-sector mapping
- Magnitude vs historical distribution

**Examples**
- European banks strength → US financials bias
- Japan semis → US semiconductor ETFs
- Australia materials → US materials and energy

**Output**
- Lead–lag signal strength per US sector or index
- Confidence score (0–100)

---

### 5.3 Regime Classification Engine
Determines whether signals should be acted on or suppressed.

**Inputs**
- FX behavior (risk vs funding currencies)
- Sovereign yield direction and slope
- Commodity stress signals
- Volatility proxies from ORATS

**Regimes**
- Risk-On
- Risk-Off
- Transitional
- Stressed

**Output**
- Regime label
- Allowed trade types
- Position size modifiers
- Suppression flags

---

### 5.4 Translation Engine (Global → US)
Maps global signals into **tradable US expressions**.

**Mapping logic**
- Global region → US sector ETF
- Macro move → index bias
- FX move → volatility posture

**Examples**
- Europe industrial strength → XLI bias
- Yen strength + falling yields → defensive posture
- Commodity rally → XLE/XLB tilt

**Output**
- Directional bias (bullish, bearish, neutral)
- Volatility bias (expand, contract, neutral)

---

### 5.5 Options Structure Mapping Engine (ORATS)
Converts bias into **income-oriented ideas**.

**Structures**
- Put credit spreads
- Call credit spreads
- Skewed iron condors
- Calendars and diagonals when appropriate

**Selection logic**
- Expected move vs strike distance
- IV rank and skew
- Regime-based structure permissions
- Earnings and event filters

**Output**
- Trade idea templates
- Probabilistic framing
- Max risk and ROC estimates

---

### 5.6 News and Event Filter (Benzinga)
Final safety layer before output.

**Functions**
- Flag symbols with earnings inside window
- Highlight macro event weeks
- Annotate unusual news-driven risk

**Output**
- Warnings
- Confidence adjustments
- Human-readable context

---

## 6. Weekly Output Format (Raven UI)

Each cycle produces:

- **Market Regime Summary**
- **Global Signal Dashboard**
- **US Sector Bias Table**
- **Index Bias (SPY / QQQ / IWM)**
- **Options Income Ideas**
  - Structure type
  - Directional lean
  - Confidence score
  - Notes and suppressions

No execution. No automation. No alerts that require reaction.

---

## 7. Operating Cadence

### Nightly
- Ingest data
- Update signals
- Recalculate regime

### Weekly (Primary)
- Generate trade ideas
- Review confidence
- Plan entries

### Market Hours
- Market “tags in” or does not
- No signal recalculation
- No emotional intervention

---

## 8. What This Engine Is Not

- Not a scalping system
- Not an intraday trading tool
- Not a prediction engine
- Not an auto-execution platform

It is a **decision-support and planning system** for disciplined, repeatable income strategies.

---

## 9. Success Criteria

The Lead–Lag Engine is successful if it:
- Improves ROC consistency
- Reduces drawdowns through suppression
- Increases confidence in weekly positioning
- Removes emotional interference
- Scales in complexity without increasing stress

---

## 10. One-Sentence Summary

The Raven Tech Lead–Lag Engine uses global EOD intelligence to inform disciplined, regime-aware weekly options income ideas without requiring intraday decisions or automated execution.
