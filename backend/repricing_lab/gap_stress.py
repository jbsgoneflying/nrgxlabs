"""Trailing cohort gap-stress quantiles for sizing."""
from __future__ import annotations

from typing import List, Optional, Sequence


def overnight_gaps(bars: Sequence[dict], *, side: str = "long") -> List[float]:
    """Adverse overnight gap magnitudes in dollars (positive numbers)."""
    gaps: List[float] = []
    for i in range(1, len(bars)):
        prev = bars[i - 1]
        cur = bars[i]
        prev_c = prev.get("adjusted_close") if prev.get("adjusted_close") is not None else prev.get("close")
        cur_o = cur.get("open")
        if prev_c is None or cur_o is None or float(prev_c) == 0:
            continue
        gap = (float(cur_o) / float(prev_c)) - 1.0
        adverse = (-gap) if side == "long" else gap
        if adverse > 0:
            gaps.append(adverse * float(prev_c))
    return gaps


def quantile(xs: Sequence[float], q: float) -> Optional[float]:
    if not xs:
        return None
    if q < 0 or q > 1:
        raise ValueError("q must be in [0,1]")
    ys = sorted(float(x) for x in xs)
    if len(ys) == 1:
        return ys[0]
    pos = q * (len(ys) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ys) - 1)
    frac = pos - lo
    return ys[lo] * (1 - frac) + ys[hi] * frac


def gap_stress_quantile(
    bars: Sequence[dict],
    *,
    q: float = 0.90,
    side: str = "long",
    lookback: int = 252,
) -> Optional[float]:
    window = bars[-lookback:] if len(bars) > lookback else bars
    gaps = overnight_gaps(window, side=side)
    return quantile(gaps, q)
