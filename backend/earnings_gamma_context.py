"""NRGX Labs 2.0 – Earnings Gamma Context for Engine 1.

Overlays dealer gamma analysis on earnings events to classify
post-earnings price-path risk:
  - Pin Risk          (supportive gamma + near a major strike)
  - Snap Risk         (hostile gamma + near a major strike)
  - Trend Continuation (supportive gamma + far from strikes)
  - Gap and Drift     (fragile gamma + skewed)

Adds three columns to the Earnings Rank Table:
  1. gammaContext     – Supportive | Fragile | Hostile
  2. pinZoneProximity – near | medium | far
  3. skewRisk         – downside_heavy | balanced | upside_heavy
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class EarningsGammaContext:
    ticker: str = ""
    as_of_date: str = ""
    # Computed fields
    gamma_context: str = "Fragile"         # Supportive | Fragile | Hostile
    gamma_context_score: float = 50.0      # 0-100
    pin_zone_proximity: str = "far"        # near | medium | far
    pin_zone_distance_pct: float = 999.0   # % distance to nearest gamma wall
    skew_risk: str = "balanced"            # downside_heavy | balanced | upside_heavy
    skew_risk_score: float = 0.0           # -1 to +1
    # Supporting data
    key_gamma_strikes: List[dict] = field(default_factory=list)
    expected_move_band: Dict[str, float] = field(default_factory=dict)
    net_gex_sign: str = ""
    magnitude_bucket: str = ""
    # Composite
    path_risk_label: str = "Gap and Drift"
    path_risk_rationale: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def compute_earnings_gamma_context(
    *,
    ticker: str,
    as_of_date: str = "",
    dealer_gamma: Optional[dict] = None,
    tail_ignition: Optional[dict] = None,
    spot: Optional[float] = None,
    implied_move_pct: Optional[float] = None,
) -> EarningsGammaContext:
    """Compute earnings gamma context from existing dealer gamma + tail ignition data.

    Args:
        dealer_gamma:  Output of compute_dealer_gamma_context()
        tail_ignition: Output of compute_tail_ignition()
        spot:          Current spot price
        implied_move_pct: Current implied move in percent
    """
    dg = dealer_gamma if isinstance(dealer_gamma, dict) else {}
    ti = tail_ignition if isinstance(tail_ignition, dict) else {}

    # --- Extract dealer gamma fields ---
    net_sign = str(dg.get("netGammaSign") or "").lower()
    mag_bucket = str(dg.get("magnitudeBucket") or "").lower()
    top_strikes = dg.get("topGammaStrikes") or []
    if not isinstance(top_strikes, list):
        top_strikes = []

    # --- Gamma context classification ---
    if net_sign == "positive" and mag_bucket in ("medium", "high"):
        gamma_context = "Supportive"
        gamma_score = 75.0 if mag_bucket == "high" else 65.0
    elif net_sign == "positive" and mag_bucket == "low":
        gamma_context = "Fragile"
        gamma_score = 50.0
    elif net_sign == "negative":
        gamma_context = "Hostile"
        gamma_score = 25.0 if mag_bucket in ("medium", "high") else 35.0
    else:
        gamma_context = "Fragile"
        gamma_score = 50.0

    # Check proximity to gamma flip (from tail ignition)
    flip_dist_pct = _to_float(ti.get("flipDistancePct"))
    if flip_dist_pct is not None and flip_dist_pct < 2.0:
        # Near gamma flip degrades supportive to fragile
        if gamma_context == "Supportive":
            gamma_context = "Fragile"
            gamma_score = max(40.0, gamma_score - 15.0)

    # --- Pin zone proximity ---
    s = _to_float(spot) or _to_float(dg.get("spot"))
    min_dist_pct = 999.0
    key_strikes_out = []

    if s and s > 0:
        for ts in top_strikes[:5]:
            if not isinstance(ts, dict):
                continue
            k = _to_float(ts.get("strike"))
            if k is None or k <= 0:
                continue
            dist_pct = abs(s - k) / s * 100.0
            min_dist_pct = min(min_dist_pct, dist_pct)
            key_strikes_out.append({
                "strike": k,
                "side": ts.get("side", ""),
                "gex": ts.get("gex"),
                "distancePct": round(dist_pct, 2),
            })

    if min_dist_pct <= 1.0:
        pin_zone = "near"
    elif min_dist_pct <= 3.0:
        pin_zone = "medium"
    else:
        pin_zone = "far"

    # --- Skew risk ---
    down_score = 50
    up_score = 50
    down_data = ti.get("down") if isinstance(ti.get("down"), dict) else {}
    up_data = ti.get("up") if isinstance(ti.get("up"), dict) else {}
    ds = _to_float(down_data.get("score"))
    us = _to_float(up_data.get("score"))
    if ds is not None:
        down_score = ds
    if us is not None:
        up_score = us

    skew_diff = down_score - up_score
    if skew_diff > 20:
        skew_risk = "downside_heavy"
        skew_score = -1.0 * min(1.0, skew_diff / 50.0)
    elif skew_diff < -20:
        skew_risk = "upside_heavy"
        skew_score = 1.0 * min(1.0, abs(skew_diff) / 50.0)
    else:
        skew_risk = "balanced"
        skew_score = 0.0

    # --- Expected move band ---
    em_band = {}
    if s and implied_move_pct:
        em_abs = s * (implied_move_pct / 100.0)
        em_band = {
            "low": round(s - em_abs, 2),
            "high": round(s + em_abs, 2),
            "impliedMovePct": implied_move_pct,
        }

    # --- Path risk label (composite) ---
    if gamma_context == "Supportive" and pin_zone == "near":
        path_label = "Pin Risk"
        path_rationale = "Positive dealer gamma near a major strike creates pinning pressure post-earnings."
    elif gamma_context == "Hostile" and pin_zone == "near":
        path_label = "Snap Risk"
        path_rationale = "Negative dealer gamma near a major strike creates snap-through risk post-earnings."
    elif gamma_context == "Supportive" and pin_zone == "far":
        path_label = "Trend Continuation"
        path_rationale = "Positive dealer gamma with no nearby walls supports orderly post-earnings trending."
    elif gamma_context == "Fragile" and skew_risk != "balanced":
        path_label = "Gap and Drift"
        path_rationale = f"Fragile gamma with {skew_risk.replace('_', ' ')} skew suggests gap and drift risk."
    elif gamma_context == "Hostile":
        path_label = "Snap Risk"
        path_rationale = "Negative dealer gamma increases post-earnings volatility and snap-through risk."
    else:
        path_label = "Trend Continuation"
        path_rationale = "Gamma positioning is neutral; post-earnings price action likely follows trend."

    return EarningsGammaContext(
        ticker=ticker,
        as_of_date=as_of_date,
        gamma_context=gamma_context,
        gamma_context_score=round(gamma_score, 1),
        pin_zone_proximity=pin_zone,
        pin_zone_distance_pct=round(min_dist_pct, 2),
        skew_risk=skew_risk,
        skew_risk_score=round(skew_score, 3),
        key_gamma_strikes=key_strikes_out[:5],
        expected_move_band=em_band,
        net_gex_sign=net_sign,
        magnitude_bucket=mag_bucket,
        path_risk_label=path_label,
        path_risk_rationale=path_rationale,
    )
