"""Aggregate trade outcomes into edge statistics.

Given a list of ``TradeResult`` we compute the metrics a desk actually uses to
judge an edge: average and median net return per trade, hit rate, dispersion,
a t-stat (is the mean distinguishable from zero?), an annualized Sharpe proxy,
and a max-drawdown estimate from the sequential equity curve.

Caveats stated honestly:
  * **Sharpe** is annualized from per-trade stats using the *average* holding
    period (periods/yr = 252 / avg_holding_days). It is a proxy, not a true
    daily-marked Sharpe.
  * **Max drawdown** compounds trades one-at-a-time ordered by exit date. Real
    overlapping books differ; treat it as a relative ranking signal.
  * The **t-stat** assumes roughly independent trades. Overlapping windows on
    correlated names inflate significance — discount accordingly.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List

from backend.research.event_study import TradeResult


@dataclass(frozen=True)
class CohortStats:
    n: int = 0
    avg_net_return: float = 0.0
    median_net_return: float = 0.0
    avg_gross_return: float = 0.0
    hit_rate: float = 0.0
    std_net_return: float = 0.0
    t_stat: float = 0.0
    sharpe_annualized: float = 0.0
    avg_holding_days: float = 0.0
    total_compounded_return: float = 0.0
    max_drawdown: float = 0.0
    best: float = 0.0
    worst: float = 0.0
    avg_cost_drag: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _median(xs: List[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def _sample_std(xs: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)


def _max_drawdown(net_returns_in_order: List[float]) -> float:
    """Max peak-to-trough drawdown of a one-at-a-time compounded equity curve.

    Returned as a positive fraction (0.20 == a 20% drawdown).
    """
    if not net_returns_in_order:
        return 0.0
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in net_returns_in_order:
        equity *= (1.0 + r)
        peak = max(peak, equity)
        if peak > 0:
            dd = (peak - equity) / peak
            max_dd = max(max_dd, dd)
    return max_dd


def summarize(results: List[TradeResult]) -> CohortStats:
    if not results:
        return CohortStats()

    net = [r.net_return for r in results]
    gross = [r.gross_return for r in results]
    holds = [float(r.holding_days) for r in results if r.holding_days > 0]
    costs = [r.cost for r in results]
    n = len(net)

    avg = _mean(net)
    std = _sample_std(net)
    t_stat = (avg / (std / math.sqrt(n))) if (std > 0 and n > 1) else 0.0

    avg_hold = _mean(holds) if holds else 0.0
    if std > 0 and avg_hold > 0:
        periods_per_year = 252.0 / avg_hold
        sharpe = (avg / std) * math.sqrt(periods_per_year)
    else:
        sharpe = 0.0

    # Equity curve ordered by exit date for the drawdown estimate.
    ordered = [r.net_return for r in sorted(results, key=lambda r: r.exit_date)]
    compounded = 1.0
    for r in ordered:
        compounded *= (1.0 + r)

    return CohortStats(
        n=n,
        avg_net_return=round(avg, 6),
        median_net_return=round(_median(net), 6),
        avg_gross_return=round(_mean(gross), 6),
        hit_rate=round(sum(1 for x in net if x > 0) / n, 4),
        std_net_return=round(std, 6),
        t_stat=round(t_stat, 3),
        sharpe_annualized=round(sharpe, 3),
        avg_holding_days=round(avg_hold, 2),
        total_compounded_return=round(compounded - 1.0, 6),
        max_drawdown=round(_max_drawdown(ordered), 6),
        best=round(max(net), 6),
        worst=round(min(net), 6),
        avg_cost_drag=round(_mean(costs), 6),
    )


def group_by(
    results: List[TradeResult],
    key_fn: Callable[[TradeResult], str],
) -> Dict[str, CohortStats]:
    """Bucket results by an arbitrary key and summarize each bucket."""
    buckets: Dict[str, List[TradeResult]] = {}
    for r in results:
        buckets.setdefault(key_fn(r), []).append(r)
    return {k: summarize(v) for k, v in sorted(buckets.items())}


def group_by_tag(results: List[TradeResult], tag: str, default: str = "NA") -> Dict[str, CohortStats]:
    return group_by(results, lambda r: r.tags.get(tag, default))
