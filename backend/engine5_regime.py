"""Engine 5 – Global Regime Classification Engine.

4-factor stress scoring: higher score = more stress.
- FX stress (30%): funding currency bid = stress
- Yield stress (25%): flattening/inversion = stress
- Commodity stress (20%): risk-off commodity pattern = stress
- IV stress (25%): high IV rank = stress

Labels:
- score < 30: Risk-On (full structures, 1.0x size)
- 30-55: Transitional (credit spreads, 0.75x)
- 55-75: Risk-Off (directional spreads only, 0.50x)
- >= 75: Stressed (suppression active)
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class GlobalRegime:
    date: str
    label: str                                  # Risk-On | Risk-Off | Transitional | Stressed
    score: float                                # 0-100 composite (higher = more stress)
    components: Dict[str, float] = field(default_factory=dict)
    allowed_structures: List[str] = field(default_factory=list)
    position_size_modifier: float = 1.0
    suppression_flags: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GlobalRegime":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALL_STRUCTURES = [
    "put_credit_spread",
    "call_credit_spread",
    "iron_condor",
    "calendar",
    "diagonal",
]

DIRECTIONAL_ONLY = [
    "put_credit_spread",
    "call_credit_spread",
]

CREDIT_SPREADS = [
    "put_credit_spread",
    "call_credit_spread",
    "iron_condor",
]


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------


def _clamp(lo: float, hi: float, x: float) -> float:
    return max(lo, min(hi, x))


def _percentile_rank(x: float, xs: List[float]) -> Optional[float]:
    """Percentile rank in [0, 1] using <= comparison."""
    vals = [v for v in xs if v is not None and isinstance(v, (int, float)) and math.isfinite(v)]
    if not vals:
        return None
    c = sum(1 for v in vals if v <= x)
    return c / len(vals)


def _safe_mean(xs: List[float]) -> Optional[float]:
    vals = [v for v in xs if v is not None and math.isfinite(v)]
    if not vals:
        return None
    return statistics.mean(vals)


# ---------------------------------------------------------------------------
# Factor computations
# ---------------------------------------------------------------------------


def _compute_fx_stress(
    audusd_returns_5d: List[float],
    usdjpy_returns_5d: List[float],
    fx_stress_history_252d: List[float],
) -> float:
    """FX stress: falling AUDUSD + rising USDJPY = stress (higher score).

    Composite: -mean(AUDUSD 5d returns) + mean(USDJPY 5d returns).
    Positive composite = stress. Percentile-ranked against 252d history.
    Returns 0-100.
    """
    aud_mean = _safe_mean(audusd_returns_5d) or 0.0
    jpy_mean = _safe_mean(usdjpy_returns_5d) or 0.0

    # Stress metric: when AUD falls (negative return) and JPY rises vs USD
    # USDJPY rising = JPY weakening = risk-on, USDJPY falling = JPY strengthening = stress
    # So stress = -AUDUSD_return + (-USDJPY_return) = -AUDUSD - USDJPY
    # Wait: USDJPY falling means JPY strengthening (funding currency bid) = stress
    # So stress = -AUDUSD_return - USDJPY_return
    stress_metric = -aud_mean - jpy_mean

    if fx_stress_history_252d:
        pct = _percentile_rank(stress_metric, fx_stress_history_252d)
        if pct is not None:
            return round(_clamp(0, 100, pct * 100), 2)

    # Fallback: map metric directly to 0-100 scale
    return round(_clamp(0, 100, 50 + stress_metric * 5000), 2)


def _compute_yield_stress(
    slope_2s10s: float,
    slope_5d_change: float,
    yield_stress_history_252d: List[float],
) -> float:
    """Yield stress: flattening/inversion = stress (higher score).

    Composite: -slope_2s10s + abs(slope_5d_change if flattening).
    Negative slope = inversion = high stress.
    Percentile-ranked against 252d history.
    Returns 0-100.
    """
    # More negative slope = more stress. Flattening (negative change) = more stress.
    stress_metric = -slope_2s10s
    if slope_5d_change < 0:
        stress_metric += abs(slope_5d_change) * 0.5  # Extra stress for active flattening

    if yield_stress_history_252d:
        pct = _percentile_rank(stress_metric, yield_stress_history_252d)
        if pct is not None:
            return round(_clamp(0, 100, pct * 100), 2)

    # Fallback heuristic
    if slope_2s10s < -0.5:
        return 90.0
    elif slope_2s10s < 0:
        return 70.0
    elif slope_2s10s < 0.5:
        return 50.0
    elif slope_2s10s < 1.0:
        return 30.0
    return 15.0


def _compute_commodity_stress(
    oil_returns_5d: List[float],
    copper_returns_5d: List[float],
    gold_returns_5d: List[float],
    commodity_stress_history_252d: List[float],
) -> float:
    """Commodity stress: declining oil+copper with rising gold = stress.

    Composite: -mean(oil) - mean(copper) + mean(gold).
    Positive = stress pattern. Percentile-ranked.
    Returns 0-100.
    """
    oil_mean = _safe_mean(oil_returns_5d) or 0.0
    copper_mean = _safe_mean(copper_returns_5d) or 0.0
    gold_mean = _safe_mean(gold_returns_5d) or 0.0

    stress_metric = -oil_mean - copper_mean + gold_mean

    if commodity_stress_history_252d:
        pct = _percentile_rank(stress_metric, commodity_stress_history_252d)
        if pct is not None:
            return round(_clamp(0, 100, pct * 100), 2)

    return round(_clamp(0, 100, 50 + stress_metric * 3000), 2)


def _compute_iv_stress(
    spy_iv_rank: Optional[float],
) -> float:
    """IV stress: high IV rank = stress. Already oriented correctly.

    spy_iv_rank: 0.0 to 1.0 (percentile). Directly maps to 0-100.
    Returns 0-100.
    """
    if spy_iv_rank is None:
        return 50.0  # Neutral if unavailable
    return round(_clamp(0, 100, spy_iv_rank * 100), 2)


# ---------------------------------------------------------------------------
# Regime classification
# ---------------------------------------------------------------------------


def classify_regime(
    *,
    date: str,
    fx_stress: float,
    yield_stress: float,
    commodity_stress: float,
    iv_stress: float,
    yield_snapshot: Optional[dict] = None,
    stressed_threshold: float = 75.0,
    risk_off_threshold: float = 55.0,
    transitional_threshold: float = 30.0,
) -> GlobalRegime:
    """Classify the global regime from four stress factor scores.

    All inputs are 0-100, higher = more stress.
    """
    score = (
        0.30 * fx_stress
        + 0.25 * yield_stress
        + 0.20 * commodity_stress
        + 0.25 * iv_stress
    )
    score = round(_clamp(0, 100, score), 2)

    if score >= stressed_threshold:
        label = "Stressed"
        allowed = []
        size_mod = 0.0
    elif score >= risk_off_threshold:
        label = "Risk-Off"
        allowed = list(DIRECTIONAL_ONLY)
        size_mod = 0.50
    elif score >= transitional_threshold:
        label = "Transitional"
        allowed = list(CREDIT_SPREADS)
        size_mod = 0.75
    else:
        label = "Risk-On"
        allowed = list(ALL_STRUCTURES)
        size_mod = 1.0

    # Suppression flags
    flags: List[str] = []
    if yield_snapshot:
        slope = yield_snapshot.get("us_2s10s_slope")
        if slope is not None and slope < -0.5:
            flags.append("yield_inversion_deepening")
    if fx_stress >= 85:
        flags.append("fx_dislocation")
    if iv_stress >= 90:
        flags.append("iv_extreme")
    if commodity_stress >= 85:
        flags.append("commodity_stress_extreme")

    return GlobalRegime(
        date=date,
        label=label,
        score=score,
        components={
            "fx_stress": round(fx_stress, 2),
            "yield_stress": round(yield_stress, 2),
            "commodity_stress": round(commodity_stress, 2),
            "iv_stress": round(iv_stress, 2),
        },
        allowed_structures=allowed,
        position_size_modifier=size_mod,
        suppression_flags=flags,
    )


# ---------------------------------------------------------------------------
# High-level entry point
# ---------------------------------------------------------------------------


def compute_regime_from_bars(
    *,
    date: str,
    bars_history: Dict[str, List[dict]],
    yield_snapshots: List[dict],
    spy_iv_rank: Optional[float] = None,
    stress_histories: Optional[Dict[str, List[float]]] = None,
    stressed_threshold: float = 75.0,
    risk_off_threshold: float = 55.0,
    transitional_threshold: float = 30.0,
) -> GlobalRegime:
    """Compute regime from raw bar history and yield snapshots.

    Args:
        bars_history: {symbol: [{"date": ..., "return_1d_local": ...}, ...]}
        yield_snapshots: List of yield snapshot dicts, sorted by date.
        spy_iv_rank: Current SPY IV rank from ORATS (0-1).
        stress_histories: Optional pre-computed 252d histories for each stress metric.
    """
    histories = stress_histories or {}

    # Extract 5-day return series for FX
    audusd_rets = _extract_recent_returns(bars_history.get("AUDUSD.FOREX", []), 5)
    usdjpy_rets = _extract_recent_returns(bars_history.get("USDJPY.FOREX", []), 5)
    fx_stress = _compute_fx_stress(
        audusd_rets, usdjpy_rets,
        histories.get("fx_stress", []),
    )

    # Yield stress
    latest_yield = yield_snapshots[-1] if yield_snapshots else {}
    slope = latest_yield.get("us_2s10s_slope", 0.0) or 0.0
    # 5-day slope change
    slope_5d_ago = 0.0
    if len(yield_snapshots) >= 6:
        slope_5d_ago = yield_snapshots[-6].get("us_2s10s_slope", 0.0) or 0.0
    slope_change = slope - slope_5d_ago

    yield_stress = _compute_yield_stress(
        slope, slope_change,
        histories.get("yield_stress", []),
    )

    # Commodity stress
    oil_rets = _extract_recent_returns(bars_history.get("USO.US", []), 5)
    copper_rets = _extract_recent_returns(bars_history.get("CPER.US", []), 5)
    gold_rets = _extract_recent_returns(bars_history.get("GLD.US", []), 5)
    commodity_stress = _compute_commodity_stress(
        oil_rets, copper_rets, gold_rets,
        histories.get("commodity_stress", []),
    )

    # IV stress
    iv_stress = _compute_iv_stress(spy_iv_rank)

    return classify_regime(
        date=date,
        fx_stress=fx_stress,
        yield_stress=yield_stress,
        commodity_stress=commodity_stress,
        iv_stress=iv_stress,
        yield_snapshot=latest_yield,
        stressed_threshold=stressed_threshold,
        risk_off_threshold=risk_off_threshold,
        transitional_threshold=transitional_threshold,
    )


def _extract_recent_returns(bars: List[dict], n: int) -> List[float]:
    """Extract the last n return_1d_local values from a bar history."""
    sorted_bars = sorted(bars, key=lambda b: str(b.get("date", "")))
    returns = []
    for b in sorted_bars:
        r = b.get("return_1d_local")
        if r is not None:
            try:
                returns.append(float(r))
            except (TypeError, ValueError):
                pass
    return returns[-n:] if returns else []
