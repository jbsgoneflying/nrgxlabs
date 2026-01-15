# Engine1 Earnings Hold Risk Extension

## Objective
Build a decision grade earnings hold risk module that quantifies how safe it is to hold short iron condors for additional decay after an earnings announcement.

The system must answer three concrete trading questions:
1. If earnings are out and the stock opens flat, what are the odds it stays inside my expected move bands by the earnings day close.
2. If it stays flat through the earnings day close, what are the odds it remains contained through the next trading day close.
3. How does risk change once the gap is known and volatility has collapsed.

This is not a signal engine. It is a probabilistic risk underwriting layer designed for live capital deployment.

---

## Design Principles
- Regime aware but not regime dependent
- Conditional probabilities over unconditional averages
- Close based risk first, intraday extensions optional
- Explicit baselines and horizons
- Sample size and filter transparency at all times

---

## Core Definitions

### Time Anchors
- Prior Close (PC): close of the trading day before earnings
- Earnings Day Open (EO): official market open on earnings day
- Earnings Day Close (EC): close of earnings day session
- Next Day Close (NC): close of the trading day following earnings

### Expected Move (EM)
- Single consistent EM definition must be used across all metrics
- Preferred: ORATS earnings implied move or nearest horizon ATM straddle based move
- EM must be labeled clearly in outputs as the chosen proxy

### EM Multiples
All breach calculations use k in:
- 1.0x EM
- 1.5x EM
- 2.0x EM

---

## Metric Groups

### Group 1: Unconditional Close Breach Rates
Baseline: Prior Close (PC)

These describe raw historical behavior without conditioning.

Metrics:
- Earnings Day Close Breach
  abs(EC − PC) >= k * EM

- Next Day Close Breach
  abs(NC − PC) >= k * EM

Purpose:
Establish the unconditional risk envelope for holding through earnings and one additional day.

---

### Group 2: Conditional Flat Open Breach Rates

This group answers the real trading question: what happens if the market opens flat.

#### Flat Open Gate
Condition:
abs(EO − PC) <= x * EM

Default:
x = 0.25 EM

This gate defines "earnings came out clean" in probabilistic terms.

#### Metrics
Computed only on events that pass the flat open gate.

- Earnings Day Close Conditional Breach
  abs(EC − PC) >= k * EM

- Next Day Close Conditional Breach
  abs(NC − PC) >= k * EM

Purpose:
Quantify drift risk after a non eventful earnings reaction.

---

### Group 3: Post Event Drift Risk

These metrics rebase risk once information is known.

#### Drift Baselines
- Earnings Intraday Drift
  Baseline: EO
  abs(EC − EO) >= k * EM

- Next Day Drift
  Baseline: EC
  abs(NC − EC) >= k * EM

Purpose:
Measure risk once the gap is observed and implied volatility has collapsed.

---

### Group 4: Optional Strike Aware Risk (Phase 2)

If short strike locations are available:

- Probability EC closes beyond short strikes
- Probability NC closes beyond short strikes

Close based only unless high low data is added later.

---

## Output Schema (Engine1 Payload)

```json
{
  "earnings_hold_risk": {
    "em_source": "ORATS_EARNINGS_IMPLIED",
    "flat_open_gate": 0.25,
    "lookback": "36_events",
    "sample_size": {
      "unconditional": N1,
      "flat_open": N2
    },
    "unconditional": {
      "earnings_close": {
        "1.0": rate,
        "1.5": rate,
        "2.0": rate
      },
      "next_day_close": {
        "1.0": rate,
        "1.5": rate,
        "2.0": rate
      }
    },
    "conditional_flat_open": {
      "earnings_close": {
        "1.0": rate,
        "1.5": rate,
        "2.0": rate
      },
      "next_day_close": {
        "1.0": rate,
        "1.5": rate,
        "2.0": rate
      }
    },
    "drift": {
      "earnings_intraday": {
        "1.0": rate,
        "1.5": rate,
        "2.0": rate
      },
      "next_day": {
        "1.0": rate,
        "1.5": rate,
        "2.0": rate
      }
    }
  }
}
```

---

## Backend Implementation Plan

### Data Layer
File: backend/orats_client.py

Ensure access to:
- prior_close
- earnings_day_open
- earnings_day_close
- next_day_close
- earnings_expected_move

If any field is missing, extend client and normalize timestamps.

---

### Computation Layer
File: backend/expected_move.py

Add helpers:
- compute_breach_rate(events, baseline, target, em, k_values)
- filter_flat_open(events, pc, eo, em, gate)
- compute_drift_rate(events, start, end, em, k_values)

All helpers must return:
- breach_rate
- sample_size

---

### Earnings Logic Integration
File: backend/earnings_logic.py

Steps:
1. Pull historical earnings events for ticker
2. Compute unconditional breach metrics
3. Apply flat open gate and compute conditional metrics
4. Compute drift metrics
5. Package into earnings_hold_risk payload

No trade gating logic belongs here. This is informational risk only.

---

## Testing Strategy

### Unit Tests
File: tests/test_expected_move.py

- Deterministic breach calculations
- Flat open gate correctness
- Drift baseline correctness

### End to End Validation
File: tests/test_end_to_end_mock.py

- Small historical slice with fixed EM values
- Validate sample size integrity
- Validate rates sum logically

---

## UI Card Specification

Card Title:
Earnings Hold Risk

Sections:

1. Earnings Day Close vs Prior Close
- 1.0x EM
- 1.5x EM
- 2.0x EM

2. Next Day Close vs Prior Close
- 1.0x EM
- 1.5x EM
- 2.0x EM

3. Conditional Flat Open (<= 0.25x EM)
- P breach by earnings close
- P breach by next day close

Footer:
- Sample size
- Lookback window
- EM source

---

## Risk Notes
- All metrics are close based and do not capture intraday excursions
- Results must always be read in conjunction with regime context
- Small sample sizes should be visually de emphasized

---

## Final Intent
This module exists to answer one question with institutional clarity:

"If nothing happens on the print, how much edge do I have to keep letting decay work."

If this cannot answer that cleanly, it should not ship.

