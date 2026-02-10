"""Engine 5 – Lead-Lag Detection Module.

Detects statistically meaningful preceding signals from global markets
to US sectors/indices using rolling correlations at multiple lag offsets.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class LeadLagSignal:
    date: str
    leader_symbol: str
    leader_region: str
    follower_symbol: str          # US sector ETF
    lag_days: int                 # 1-5
    correlation_rolling_20d: float
    correlation_rolling_60d: Optional[float]
    signal_strength: float        # 0-100
    direction: str                # bullish | bearish | neutral
    magnitude_z: float            # Leader move z-score vs own history
    confirmation_count: int       # How many other leaders agree

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LeadLagSignal":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------


def _pearson_corr(xs: List[float], ys: List[float]) -> Optional[float]:
    """Pearson correlation coefficient. Returns None if not computable."""
    n = min(len(xs), len(ys))
    if n < 5:
        return None
    x = xs[-n:]
    y = ys[-n:]
    try:
        mx = statistics.mean(x)
        my = statistics.mean(y)
    except statistics.StatisticsError:
        return None

    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    denom_x = math.sqrt(sum((xi - mx) ** 2 for xi in x))
    denom_y = math.sqrt(sum((yi - my) ** 2 for yi in y))

    if denom_x < 1e-12 or denom_y < 1e-12:
        return None
    return num / (denom_x * denom_y)


def _z_score_value(value: float, series: List[float], min_n: int = 10) -> Optional[float]:
    """Z-score of value against series."""
    vals = [v for v in series if v is not None and math.isfinite(v)]
    if len(vals) < min_n:
        return None
    try:
        mu = statistics.mean(vals)
        sd = statistics.stdev(vals)
    except statistics.StatisticsError:
        return None
    if sd < 1e-12:
        return None
    return (value - mu) / sd


def _clamp(lo: float, hi: float, x: float) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Core lead-lag computation
# ---------------------------------------------------------------------------


def compute_lead_lag_signals(
    leader_returns: Dict[str, List[Tuple[str, float]]],
    follower_returns: Dict[str, List[Tuple[str, float]]],
    mapping: Dict[str, List[str]],
    *,
    corr_window: int = 20,
    max_lag_days: int = 5,
    corr_threshold: float = 0.40,
    z_significant: float = 1.50,
    lookback_days: int = 60,
    date: str = "",
) -> List[LeadLagSignal]:
    """Compute lead-lag signals between global leaders and US followers.

    Args:
        leader_returns: {symbol: [(date, return), ...]} sorted by date ascending.
                        Returns are daily log returns.
        follower_returns: {us_symbol: [(date, return), ...]} sorted by date ascending.
                          Not required for correlation-at-lag (we use leader's own
                          lagged return vs US reference index). But if available,
                          used for cross-correlation.
        mapping: {leader_symbol: [us_target_1, us_target_2, ...]}
        corr_window: Rolling window for correlation (trading days).
        max_lag_days: Maximum lag offset to test (1 to max_lag_days).
        corr_threshold: Minimum |correlation| to emit a signal.
        z_significant: Minimum |z| for magnitude to count as significant.
        lookback_days: Days of history to use for z-score computation.
        date: Date label for the output signals.

    Returns:
        List of LeadLagSignal, one per (leader, follower, best_lag) tuple.
    """
    signals: List[LeadLagSignal] = []

    # Pre-extract US reference returns (SPY as generic follower for cross-corr)
    spy_rets = follower_returns.get("SPY", [])
    spy_vals = [r for _, r in spy_rets]

    for leader_sym, leader_series in leader_returns.items():
        targets = mapping.get(leader_sym, [])
        if not targets:
            continue

        leader_vals = [r for _, r in leader_series]
        if len(leader_vals) < corr_window + max_lag_days:
            continue

        # Leader magnitude z-score (today's return vs its own history)
        latest_return = leader_vals[-1] if leader_vals else None
        mag_z = _z_score_value(latest_return, leader_vals[-lookback_days:]) if latest_return is not None else None

        # Determine direction from latest return
        if latest_return is not None:
            if latest_return > 0.001:
                direction = "bullish"
            elif latest_return < -0.001:
                direction = "bearish"
            else:
                direction = "neutral"
        else:
            direction = "neutral"

        # Test correlations at each lag
        best_lag = 1
        best_corr_20d: Optional[float] = None
        best_abs_corr = 0.0

        for lag in range(1, max_lag_days + 1):
            # Lagged leader returns (shifted forward by `lag` days)
            # At lag=1: leader[t-1] predicts follower[t]
            # So we align: leader[:-lag] with spy[lag:]
            if len(leader_vals) < corr_window + lag or len(spy_vals) < corr_window + lag:
                continue

            leader_lagged = leader_vals[-(corr_window + lag):-lag]
            follower_aligned = spy_vals[-corr_window:]

            n = min(len(leader_lagged), len(follower_aligned))
            if n < corr_window:
                continue

            corr = _pearson_corr(leader_lagged[-n:], follower_aligned[-n:])
            if corr is not None and abs(corr) > best_abs_corr:
                best_abs_corr = abs(corr)
                best_corr_20d = corr
                best_lag = lag

        if best_corr_20d is None or abs(best_corr_20d) < corr_threshold:
            continue

        # 60-day correlation at best lag (if enough data)
        corr_60d: Optional[float] = None
        if len(leader_vals) >= 60 + best_lag and len(spy_vals) >= 60 + best_lag:
            leader_60 = leader_vals[-(60 + best_lag):-best_lag]
            follower_60 = spy_vals[-60:]
            n60 = min(len(leader_60), len(follower_60))
            corr_60d = _pearson_corr(leader_60[-n60:], follower_60[-n60:])

        # Compute signal strength (0-100)
        # Components: correlation magnitude (40%), will add confirmation later (30%),
        # magnitude z (20%), consistency (10%)
        corr_score = _clamp(0, 100, (abs(best_corr_20d) / 1.0) * 100)  # |corr| 0-1 -> 0-100
        mag_score = _clamp(0, 100, (abs(mag_z) / 3.0) * 100) if mag_z is not None else 25.0

        # Consistency: check if direction was the same over trailing 5 sessions
        consistency = 0.0
        if len(leader_vals) >= 5:
            recent_5 = leader_vals[-5:]
            same_dir = sum(1 for r in recent_5 if (r > 0) == (latest_return > 0)) if latest_return else 0
            consistency = (same_dir / 5.0) * 100.0

        # Confirmation count will be filled in post-processing
        raw_strength = 0.40 * corr_score + 0.30 * 0.0 + 0.20 * mag_score + 0.10 * consistency
        strength = _clamp(0, 100, raw_strength)

        for target in targets:
            sig = LeadLagSignal(
                date=date,
                leader_symbol=leader_sym,
                leader_region=_region_for_symbol(leader_sym, mapping),
                follower_symbol=target,
                lag_days=best_lag,
                correlation_rolling_20d=round(best_corr_20d, 4),
                correlation_rolling_60d=round(corr_60d, 4) if corr_60d is not None else None,
                signal_strength=round(strength, 1),
                direction=direction,
                magnitude_z=round(mag_z, 4) if mag_z is not None else 0.0,
                confirmation_count=0,  # Filled in post-processing
            )
            signals.append(sig)

    # Post-processing: fill confirmation counts
    _fill_confirmation_counts(signals)

    # Re-score with confirmation
    for sig in signals:
        corr_score = _clamp(0, 100, (abs(sig.correlation_rolling_20d) / 1.0) * 100)
        mag_score = _clamp(0, 100, (abs(sig.magnitude_z) / 3.0) * 100)
        conf_score = _clamp(0, 100, (sig.confirmation_count / max(len(leader_returns) - 1, 1)) * 100)
        # Consistency already baked in; use a placeholder
        consistency_placeholder = 50.0
        strength = 0.40 * corr_score + 0.30 * conf_score + 0.20 * mag_score + 0.10 * consistency_placeholder
        sig.signal_strength = round(_clamp(0, 100, strength), 1)

    return signals


def _fill_confirmation_counts(signals: List[LeadLagSignal]) -> None:
    """For each US target, count how many leaders agree on the same direction."""
    # Group by follower symbol
    by_follower: Dict[str, List[LeadLagSignal]] = {}
    for sig in signals:
        if sig.follower_symbol not in by_follower:
            by_follower[sig.follower_symbol] = []
        by_follower[sig.follower_symbol].append(sig)

    for follower, sigs in by_follower.items():
        # Count unique leaders per direction
        bullish_leaders = set()
        bearish_leaders = set()
        for s in sigs:
            if s.direction == "bullish":
                bullish_leaders.add(s.leader_symbol)
            elif s.direction == "bearish":
                bearish_leaders.add(s.leader_symbol)

        for s in sigs:
            if s.direction == "bullish":
                s.confirmation_count = len(bullish_leaders) - 1  # exclude self
            elif s.direction == "bearish":
                s.confirmation_count = len(bearish_leaders) - 1
            else:
                s.confirmation_count = 0
            s.confirmation_count = max(0, s.confirmation_count)


def _region_for_symbol(symbol: str, mapping: Dict[str, List[str]]) -> str:
    """Infer region from symbol suffix."""
    s = symbol.upper()
    if "INDX" in s:
        if any(x in s for x in ("STOXX", "GDAXI", "FCHI", "FTSE")):
            return "europe"
        if any(x in s for x in ("N225", "HSI")):
            return "asia"
        if "AXJO" in s:
            return "australia"
        if "GSPC" in s:
            return "us"
    if "SHE" in s:
        return "asia"
    if "FOREX" in s:
        return "global"
    return "global"
