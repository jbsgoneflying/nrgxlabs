from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


def _clamp(lo: float, hi: float, x: float) -> float:
    return max(lo, min(hi, x))


def _normalize_rec(rec: Optional[str]) -> str:
    if not rec:
        return "Avoid"
    r = str(rec)
    if r.startswith("Avoid"):
        return "Avoid"
    return r


def _base_wing_factor(rec: Optional[str]) -> Optional[float]:
    r = _normalize_rec(rec)
    if r == "Tight":
        return 0.5
    if r == "Standard":
        return 1.0
    if r == "Wide":
        return 1.5
    if r == "Avoid":
        # Keep model-driven multipliers available for planning/what-if paths
        # even when recommendation is Avoid (trade gate still controls action).
        return 1.0
    return None


@dataclass(frozen=True)
class WingRecommendation:
    # Tail Asymmetry Score in [-1,+1]; negative => downside tail dominant.
    tas: float
    skew_component: Optional[float]
    history_component: float
    regime_component: float
    quality: str

    structureMode: str
    structureRationale: str

    baseWingMultiple: Optional[float]
    putWingMultiple: Optional[float]
    callWingMultiple: Optional[float]

    recommendationLabel: str
    confidence: str
    rationale: str

    quarterKey: Optional[str]
    quarterRecommendation: Optional[str]

    tradeGate: Optional[str]
    tailMultiplier: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tas": round(self.tas, 3),
            "skew_component": None if self.skew_component is None else round(float(self.skew_component), 3),
            "history_component": round(self.history_component, 3),
            "regime_component": round(self.regime_component, 3),
            "quality": self.quality,
            "structureMode": self.structureMode,
            "structureRationale": self.structureRationale,
            "baseWingMultiple": None if self.baseWingMultiple is None else round(float(self.baseWingMultiple), 2),
            "putWingMultiple": None if self.putWingMultiple is None else round(float(self.putWingMultiple), 2),
            "callWingMultiple": None if self.callWingMultiple is None else round(float(self.callWingMultiple), 2),
            "recommendationLabel": self.recommendationLabel,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "quarterKey": self.quarterKey,
            "quarterRecommendation": self.quarterRecommendation,
            "tradeGate": self.tradeGate,
            "tailMultiplier": self.tailMultiplier,
        }


def compute_wing_recommendation(
    *,
    summary: Dict[str, Any],
    quarters: Dict[str, Any],
    regime: Dict[str, Any],
    current_quarter_key: Optional[str],
    skew_component: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Deterministic wing asymmetry recommendation.

    Phase 2: computed WITHOUT skew dependency by default (skew_component=None).
    """
    up_rate = summary.get("upBreachRatePct")
    down_rate = summary.get("downBreachRatePct")
    avg_up_os = summary.get("avgUpOvershootPct")
    avg_down_os = summary.get("avgDownOvershootPct")

    # History component sign convention:
    # - negative => downside tail dominant
    # - positive => upside tail dominant
    hist_rate_term = 0.0
    if up_rate is not None and down_rate is not None:
        hist_rate_term = _clamp(-1.0, 1.0, (float(up_rate) - float(down_rate)) / 20.0)

    hist_os_term = 0.0
    if avg_up_os is not None and avg_down_os is not None:
        # Scale overshoot asymmetry into [-1,+1] (overshoots are already in percent units).
        hist_os_term = _clamp(-1.0, 1.0, (float(avg_up_os) - float(avg_down_os)) / 200.0)

    history_component = _clamp(-1.0, 1.0, hist_rate_term + hist_os_term)

    label = str(regime.get("label") or "")
    amp = 0.0
    if label == "Elevated":
        amp = 0.10
    elif label == "Stress":
        amp = 0.20
    sign = -1.0 if history_component < 0 else (1.0 if history_component > 0 else 0.0)
    regime_component = sign * amp

    if skew_component is None:
        tas = _clamp(-1.0, 1.0, 0.80 * history_component + 0.20 * regime_component)
        quality = "MISSING"  # skew missing; history+regime only
    else:
        tas = _clamp(-1.0, 1.0, 0.45 * history_component + 0.45 * float(skew_component) + 0.10 * regime_component)
        quality = "OK"

    # Structure mode recommendation
    if abs(tas) < 0.20:
        structure_mode = "AUTO_EQUAL_DELTA"
        structure_rationale = "Low tail asymmetry → keep symmetric construction."
    elif skew_component is not None and abs(float(skew_component)) > 0.35:
        structure_mode = "AUTO_EQUAL_PREMIUM"
        structure_rationale = "Skew is strong → equal-premium can balance tail pricing."
    else:
        structure_mode = "AUTO_EQUAL_DELTA"
        structure_rationale = "Default to equal-delta absent strong skew signal."

    qk = current_quarter_key
    qrec = None
    if qk and isinstance(quarters.get(qk), dict):
        qrec = quarters[qk].get("recommendation")

    base_factor = _base_wing_factor(qrec)
    tail_mult = regime.get("tailMultiplier")
    trade_gate = (regime.get("guidance") or {}).get("tradeGate") if isinstance(regime.get("guidance"), dict) else regime.get("tradeGate")

    base_wing_multiple: Optional[float] = None
    if base_factor is not None and tail_mult is not None:
        try:
            base_wing_multiple = float(base_factor) * float(tail_mult)
        except (TypeError, ValueError):
            base_wing_multiple = None

    # Convert TAS into wing asymmetry multipliers
    rec_label = "SYMMETRIC"
    put_mult = base_wing_multiple
    call_mult = base_wing_multiple
    a = 0.35
    adj = min(a, (abs(tas) * a / 0.6) if 0.6 > 0 else a)
    if abs(tas) < 0.20:
        adj = 0.0

    if base_wing_multiple is not None and adj > 0:
        if tas < 0:
            rec_label = "WIDEN_PUTS_TIGHTEN_CALLS"
            put_mult = base_wing_multiple * (1.0 + adj)
            call_mult = base_wing_multiple * (1.0 - adj)
        elif tas > 0:
            rec_label = "WIDEN_CALLS_TIGHTEN_PUTS"
            put_mult = base_wing_multiple * (1.0 - adj)
            call_mult = base_wing_multiple * (1.0 + adj)

    # Confidence (Phase 2: no skew => never HIGH)
    events_used = summary.get("events_used") or 0
    confidence = "LOW"
    if isinstance(events_used, int) and events_used >= 12:
        confidence = "MED"

    rationale = "Skew unavailable; using directional breach history + regime only."
    if trade_gate == "NO_TRADE":
        # Keep numbers (if computable) but clearly indicate gate.
        rationale = "No Trade (Regime Gate). Wing asymmetry shown for reference only."
        rec_label = "NO_TRADE"

    wr = WingRecommendation(
        tas=tas,
        skew_component=skew_component,
        history_component=history_component,
        regime_component=regime_component,
        quality=quality,
        structureMode=structure_mode,
        structureRationale=structure_rationale,
        baseWingMultiple=base_wing_multiple,
        putWingMultiple=put_mult,
        callWingMultiple=call_mult,
        recommendationLabel=rec_label,
        confidence=confidence,
        rationale=rationale,
        quarterKey=qk,
        quarterRecommendation=qrec,
        tradeGate=trade_gate,
        tailMultiplier=tail_mult,
    )
    return wr.to_dict()

