"""Per-ticker tape-driven intraday crush estimator.

The legacy Engine 15 blend at the planned-exit boundary used a hard-coded
``ENGINE15_INTRADAY_CRUSH_FACTOR`` (0.80) to approximate the ~10:30 AM
realized P&L from the EOD ORATS chain. Different tickers collapse IV at
very different speeds — low-beta industrials often finish most of the
crush overnight (factor ~ 0.6), while speculative names can keep moving
into the first hour of trade (factor ~ 0.95+).

This module computes an **empirical** per-ticker crush factor from the
historical event pool. For each analogue window with a cached chain on
the entry day and the first post-earnings day, we compute the ratio:

    factor = |PnL(entry_day_close)| / |PnL(post_earn_close)|

clipped to ``[0, 1.2]``. Higher factor => more of the close->close move
has already played out by entry-day close (which, for a short IC,
maps to 'more of the crush is visible at the EOD mark').

Returns a :class:`CrushReading` with the median factor + sample size
+ source tag. Fallback to the config default (0.80) when sample < 3.

Output also carries a few diagnostic fields that the tooltip can
surface: ``p25`` / ``p75`` span, ``fallback_reason`` if thin.
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

LOG = logging.getLogger("engine15.intraday_crush")


@dataclass
class CrushReading:
    """Aggregate crush factor + diagnostics."""

    factor:           float = 0.80
    n_events:         int = 0
    source:           str = "fixed"       # "empirical" | "fixed" | "mixed"
    p25:              Optional[float] = None
    p50:              Optional[float] = None
    p75:              Optional[float] = None
    fallback_reason:  str = ""
    notes:            List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return float(statistics.median(values))


def _pctile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    k = (pct / 100.0) * (len(xs) - 1)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return float(xs[lo])
    frac = k - lo
    return float(xs[lo] + (xs[hi] - xs[lo]) * frac)


def compute_crush_factor(
    *,
    paths: Iterable[Any],
    fallback: float = 0.80,
    min_sample: int = 3,
    clip_lo: float = 0.05,
    clip_hi: float = 1.20,
) -> CrushReading:
    """Estimate the intraday crush factor from an analogue-path pool.

    Each path is expected to carry a ``daily_pnl_pct`` iterable of
    ``(dte_remaining, pnl_pct)`` tuples (the same shape that
    :class:`backend.engine14.simulator.AnaloguePath` emits). We take the
    **entry-day** PnL (first row, largest dte_remaining) and the
    **planned-exit-day** PnL (last row) and compute the magnitude ratio
    ``|entry_pnl| / |exit_pnl|``. Clipped + aggregated to a per-ticker
    median.

    Semantics of the factor in the replay adapter:

      final_pnl = entry_pnl + factor * (close_pnl - entry_pnl)

    Factor=1.0 means "close-to-close is fully realized by morning"
    (worst case for the IC seller on a bad gap). Factor=0.0 means
    "the whole move retraces by morning" (best case; rare). The pool-
    median reported here is the desk's best read on this ticker's
    actual behavior.
    """
    ratios: List[float] = []
    for p in paths or []:
        daily = getattr(p, "daily_pnl_pct", None) or []
        if len(daily) < 2:
            continue
        try:
            # Accept either tuple/list (dte, pnl) or dict-ish.
            first = daily[0]
            last = daily[-1]
            e_pnl = _safe_float(first[1] if not isinstance(first, dict) else first.get("pnl_pct"))
            x_pnl = _safe_float(last[1]  if not isinstance(last,  dict) else last.get("pnl_pct"))
        except Exception:
            continue
        if e_pnl is None or x_pnl is None:
            continue
        # Both legs must be meaningfully non-zero to avoid divide-by-zero
        # and amplify-by-nothing artefacts.
        if abs(x_pnl) < 5.0:
            # < 5% absolute move at exit -> noise floor; skip.
            continue
        ratio = abs(e_pnl) / abs(x_pnl)
        ratio = max(clip_lo, min(clip_hi, ratio))
        ratios.append(ratio)

    n = len(ratios)
    if n < int(min_sample):
        r = CrushReading(
            factor=float(fallback),
            n_events=n,
            source="fixed",
            fallback_reason=f"sample {n} < min {min_sample}",
        )
        r.notes.append(
            f"falling back to fixed ENGINE15_INTRADAY_CRUSH_FACTOR={fallback:.2f}"
        )
        return r

    med = _median(ratios)
    p25 = _pctile(ratios, 25.0)
    p75 = _pctile(ratios, 75.0)

    return CrushReading(
        factor=round(float(med if med is not None else fallback), 4),
        n_events=n,
        source="empirical",
        p25=None if p25 is None else round(p25, 4),
        p50=None if med is None else round(med, 4),
        p75=None if p75 is None else round(p75, 4),
        notes=[f"empirical median of {n} analogue events"],
    )


__all__ = ["CrushReading", "compute_crush_factor"]
