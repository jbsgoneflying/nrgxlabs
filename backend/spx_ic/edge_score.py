"""Engine 2 — SPX/SPY Iron Condor Edge Score.

Deterministic composite 0–100 score that quantifies "where is the edge"
for the current weekly (or flex) iron condor BEFORE the LLM runs. This
is the SPX analogue of :mod:`backend.e1_vrp_engine` ``compute_vrp_score``
for the earnings (E1) advisor.

The score is built from six components the engine already computes:

  * regimeAlignment    (25%) — calmer regime = more edge
  * macroProximity     (20%) — further from macro shock window = more edge
  * volPressure        (15%) — paid premium (BID) without spiking = harvestable
  * dealerGamma        (15%) — positive dealer gamma dampens moves
  * newsGate           (15%) — quiet news flow = clean IV harvest
  * breachAtPreferredEm (10%) — historical breach % at preferred EM

All inputs come from the existing Engine 2 / Engine 2b payload — no new
data sources are required. The output schema mirrors ``vrpAnalysis`` so
the frontend can reuse the E1 scorecard layout.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _round1(v: Optional[float]) -> Optional[float]:
    return round(v, 1) if v is not None else None


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Component scorers (each returns a 0–100 score)
# ---------------------------------------------------------------------------

def _score_regime(regime_score_raw: Optional[float], regime_bucket: str) -> float:
    """Score regime alignment. Lower regime score = more edge for IC sellers.

    Accepts either 0–100 scaled (overlay) or 0–1 scaled scores; normalises.
    """
    bucket = (regime_bucket or "").upper()
    rs = _f(regime_score_raw)
    if rs is not None:
        rs01 = rs if rs <= 1.0 else rs / 100.0
        # rs01 = 0.0 → 100, rs01 = 1.0 → 0
        score = _clamp((1.0 - rs01) * 100.0)
    else:
        score = 50.0
    # Bucket overlay: hard floors for stressed regimes regardless of score
    if bucket == "NO_TRADE":
        score = min(score, 15.0)
    elif bucket == "ELEVATED":
        score = min(score, 35.0)
    elif bucket == "LOW":
        score = max(score, 60.0)
    return _clamp(score)


def _score_macro(macro_multiplier: Optional[float]) -> float:
    """Score macro proximity. multiplier=1.0 → 100, =2.0 → 0."""
    m = _f(macro_multiplier)
    if m is None:
        return 50.0
    score = _clamp(100.0 - (m - 1.0) * 100.0)
    return score


def _score_vol_pressure(vol_state: str) -> float:
    """Score vol pressure state for IC sellers.

    BID: IV > RV — desk is paid for variance. Best for selling premium.
    NEUTRAL: balanced.
    ASK: IV < RV — premium thin, low edge.
    SPIKING: vol exploding — premium high but realized risk just as high.
    """
    s = (vol_state or "").upper()
    return {
        "BID": 75.0,
        "NEUTRAL": 60.0,
        "ASK": 40.0,
        "SPIKING": 20.0,
    }.get(s, 50.0)


def _score_dealer_gamma(sign: str) -> float:
    """Score dealer gamma context. Positive = dampened moves = more edge."""
    s = (sign or "").lower()
    return {
        "positive": 78.0,
        "long": 78.0,
        "neutral": 55.0,
        "unknown": 50.0,
        "negative": 22.0,
        "short": 22.0,
    }.get(s, 50.0)


def _score_news_gate(news_gate: Optional[Dict[str, Any]]) -> float:
    """Score news gate. Quieter news = more edge.

    Uses ``maxAdjustedIntensity`` (0–100). 0 → 100, 80+ → 0.
    """
    if not isinstance(news_gate, dict) or not news_gate:
        return 60.0  # benign default when DMS unavailable
    adj = _f(news_gate.get("maxAdjustedIntensity")) or 0.0
    score = _clamp(100.0 - adj * 1.25)
    return score


def _score_breach(em_breach_summary: Optional[Dict[str, Any]], preferred_em: Optional[float]) -> tuple[float, Optional[float], Optional[float]]:
    """Score historical breach % at the preferred EM.

    Returns (score, breach_pct_used, em_used). Breach 0% → 100, 25% → 0.
    Falls back to lowest breach in the grid if preferred EM is missing.
    """
    if not isinstance(em_breach_summary, dict) or not em_breach_summary:
        return 50.0, None, None
    breach_pct: Optional[float] = None
    em_used: Optional[float] = preferred_em
    if preferred_em is not None:
        breach_pct = _f(em_breach_summary.get(str(preferred_em))) or _f(em_breach_summary.get(f"{preferred_em:.1f}"))
    if breach_pct is None:
        # Fall back to lowest breach % across the grid
        candidates: List[tuple[float, float]] = []
        for k, v in em_breach_summary.items():
            fv = _f(v)
            if fv is None:
                continue
            try:
                fk = float(k)
            except (TypeError, ValueError):
                continue
            candidates.append((fk, fv))
        if candidates:
            em_used, breach_pct = min(candidates, key=lambda t: t[1])
    if breach_pct is None:
        return 50.0, None, None
    score = _clamp(100.0 - breach_pct * 4.0)
    return score, breach_pct, em_used


# ---------------------------------------------------------------------------
# Composite Edge Score
# ---------------------------------------------------------------------------

def compute_edge_score(
    *,
    regime_score: Optional[float] = None,
    regime_bucket: str = "MODERATE",
    macro_multiplier: Optional[float] = 1.0,
    vol_pressure_state: str = "NEUTRAL",
    dealer_gamma_sign: str = "unknown",
    news_gate: Optional[Dict[str, Any]] = None,
    em_breach_summary: Optional[Dict[str, Any]] = None,
    preferred_em: Optional[float] = None,
) -> Dict[str, Any]:
    """Build the deterministic Edge Score block for the E2 advisor card.

    Returns a dict with shape mirroring E1 ``vrpAnalysis``:
        {
          "edgeScore": float 0-100,
          "components": {regimeAlignment, macroProximity, volPressure,
                         dealerGamma, newsGate, breachAtPreferredEm},
          "label": "STRONG" | "MODERATE" | "WEAK" | "AVOID",
          "flags": [string, ...],   # active risk flags
          "confidence": "HIGH" | "MED" | "LOW",
          "preferredEm": float | None,
          "breachAtPreferredEmPct": float | None,
        }
    """
    flags: List[str] = []

    regime = _score_regime(regime_score, regime_bucket)
    macro = _score_macro(macro_multiplier)
    vol = _score_vol_pressure(vol_pressure_state)
    gamma = _score_dealer_gamma(dealer_gamma_sign)
    news = _score_news_gate(news_gate)
    breach, breach_pct, em_used = _score_breach(em_breach_summary, preferred_em)

    # Flags surface dimensions that are dragging the score down
    bucket = (regime_bucket or "").upper()
    if bucket == "NO_TRADE":
        flags.append("regime_no_trade")
    elif bucket == "ELEVATED":
        flags.append("regime_elevated")
    if _f(macro_multiplier) is not None and float(macro_multiplier) >= 1.5:
        flags.append("macro_in_window")
    vp = (vol_pressure_state or "").upper()
    if vp == "SPIKING":
        flags.append("vol_spiking")
    elif vp == "ASK":
        flags.append("vol_compressed")
    if (dealer_gamma_sign or "").lower() in ("negative", "short"):
        flags.append("negative_dealer_gamma")
    if isinstance(news_gate, dict):
        gate = str(news_gate.get("gate", "ok")).lower()
        if gate in ("elevated", "block"):
            flags.append(f"news_gate_{gate}")
    if breach_pct is not None and breach_pct >= 25.0:
        flags.append("breach_high_at_preferred_em")

    composite = (
        regime * 0.25
        + macro * 0.20
        + vol * 0.15
        + gamma * 0.15
        + news * 0.15
        + breach * 0.10
    )
    score = round(_clamp(composite), 1)

    # Label thresholds — STRONG is reserved for clean setups
    if score >= 70 and not flags:
        label = "STRONG"
    elif score >= 70:
        label = "MODERATE"  # high score but at least one risk flag → moderate
    elif score >= 50:
        label = "MODERATE"
    elif score >= 30:
        label = "WEAK"
    else:
        label = "AVOID"

    # Confidence — based on how many real signals we got (vs neutral defaults)
    real_signals = 0
    if _f(regime_score) is not None or bucket:
        real_signals += 1
    if _f(macro_multiplier) is not None:
        real_signals += 1
    if (vol_pressure_state or "").upper() not in ("", "NEUTRAL", "UNKNOWN"):
        real_signals += 1
    if (dealer_gamma_sign or "").lower() not in ("", "unknown"):
        real_signals += 1
    if isinstance(news_gate, dict) and news_gate.get("maxAdjustedIntensity") is not None:
        real_signals += 1
    if breach_pct is not None:
        real_signals += 1
    if real_signals >= 5:
        confidence = "HIGH"
    elif real_signals >= 3:
        confidence = "MED"
    else:
        confidence = "LOW"

    return {
        "edgeScore": score,
        "components": {
            "regimeAlignment": _round1(regime),
            "macroProximity": _round1(macro),
            "volPressure": _round1(vol),
            "dealerGamma": _round1(gamma),
            "newsGate": _round1(news),
            "breachAtPreferredEm": _round1(breach),
        },
        "label": label,
        "flags": flags,
        "confidence": confidence,
        "preferredEm": em_used,
        "breachAtPreferredEmPct": _round1(breach_pct),
        "inputs": {
            "regimeScore": _round1(_f(regime_score)) if _f(regime_score) is not None else None,
            "regimeBucket": bucket or None,
            "macroMultiplier": _round1(_f(macro_multiplier)) if _f(macro_multiplier) is not None else None,
            "volPressureState": (vol_pressure_state or "").upper() or None,
            "dealerGammaSign": (dealer_gamma_sign or "").lower() or None,
            "newsGate": news_gate if isinstance(news_gate, dict) else None,
        },
    }


__all__ = ["compute_edge_score"]
