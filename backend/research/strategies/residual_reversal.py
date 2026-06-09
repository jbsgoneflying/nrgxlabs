"""Strategy #4 — Short-horizon cross-sectional residual reversal.

Thesis (the bread-and-butter of stat-arb desks): after stripping out the market
(beta) component, a stock's *residual* return over the last few days tends to
mean-revert over the next week. Go long the most beaten-down residuals, short
the most stretched, hold ~1 week, rebalance.

Construction (all point-in-time as of each rebalance close ``t``):
  1. Estimate each name's beta to the market over a trailing ``beta_window``.
  2. Compute the ``formation_days`` cumulative residual return:
        resid = cum_stock_return - beta * cum_market_return
  3. Rank the cross-section; long the bottom ``top_frac`` (most negative resid,
     expect reversion up), short the top ``top_frac``.
  4. Each selected name becomes a SignalEvent (entry next session, hold
     ``hold_days``).

KNOWN BIASES (surfaced, not hidden):
  * **Survivorship.** If ``universe`` is today's index membership, delisted
    losers are missing and results are optimistic. Use a delisting-aware
    universe for a clean read.
  * **Costs dominate.** This is high-turnover; always run with a realistic
    CostModel. The gross-vs-net gap is the whole story here.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

from backend.research.data_provider import PriceProvider, PriceSeries
from backend.research.event_study import SignalEvent

_LOG = logging.getLogger(__name__)


def _daily_returns(series: PriceSeries, end_idx: int, n: int) -> List[float]:
    """Last ``n`` daily simple returns ending at ``end_idx`` (inclusive)."""
    rets: List[float] = []
    start = end_idx - n + 1
    if start < 1:
        return []
    for i in range(start, end_idx + 1):
        prev = series.bars[i - 1].close
        cur = series.bars[i].close
        if prev > 0:
            rets.append(cur / prev - 1.0)
        else:
            rets.append(0.0)
    return rets


def _ols_beta(stock: List[float], market: List[float]) -> Optional[float]:
    """Slope of stock-returns regressed on market-returns (cov/var)."""
    n = min(len(stock), len(market))
    if n < 10:
        return None
    s = stock[-n:]
    m = market[-n:]
    mbar = sum(m) / n
    sbar = sum(s) / n
    var = sum((x - mbar) ** 2 for x in m)
    if var <= 0:
        return None
    cov = sum((m[i] - mbar) * (s[i] - sbar) for i in range(n))
    return cov / var


def _cum_return(rets: List[float]) -> float:
    acc = 1.0
    for r in rets:
        acc *= (1.0 + r)
    return acc - 1.0


def generate_residual_reversal_events(
    price_provider: PriceProvider,
    universe: Sequence[str],
    market_ticker: str,
    start: str,
    end: str,
    *,
    formation_days: int = 5,
    hold_days: int = 5,
    beta_window: int = 60,
    top_frac: float = 0.1,
    rebalance_every: int = 5,
    history_buffer_days: int = 200,
    min_cross_section: int = 10,
    strategy_name: str = "ResidualReversal",
) -> List[SignalEvent]:
    """Generate long/short residual-reversal signals across ``universe``.

    Bars are fetched from (start - history_buffer_days) so the first rebalance
    already has a full beta window. ``rebalance_every`` is in trading days.
    """
    fetch_start = _sub_calendar_days(start, history_buffer_days)

    # Market series defines the rebalance calendar.
    mkt_bars = price_provider.get_bars(market_ticker, fetch_start, end)
    mkt = PriceSeries(mkt_bars)
    if len(mkt) < beta_window + formation_days + 2:
        _LOG.warning("market series too short for residual reversal")
        return []

    # Pre-load each universe name once.
    series_by_ticker: Dict[str, PriceSeries] = {}
    for t in universe:
        try:
            bars = price_provider.get_bars(t, fetch_start, end)
        except Exception:
            continue
        if bars:
            series_by_ticker[t.upper()] = PriceSeries(bars)

    events: List[SignalEvent] = []

    # Rebalance on market trading days that fall within [start, end].
    first_valid = beta_window + formation_days
    for t_idx in range(first_valid, len(mkt), rebalance_every):
        t_date = mkt.dates[t_idx]
        if t_date < start or t_date > end:
            continue

        mkt_daily = _daily_returns(mkt, t_idx, beta_window)
        mkt_formation = _cum_return(_daily_returns(mkt, t_idx, formation_days))

        scored: List[tuple] = []  # (residual, ticker)
        for ticker, series in series_by_ticker.items():
            # Require the exact rebalance date to exist for this name.
            try:
                i = series.dates.index(t_date)
            except ValueError:
                continue
            if i < beta_window + formation_days:
                continue
            stock_daily = _daily_returns(series, i, beta_window)
            beta = _ols_beta(stock_daily, mkt_daily)
            if beta is None:
                continue
            stock_formation = _cum_return(_daily_returns(series, i, formation_days))
            residual = stock_formation - beta * mkt_formation
            scored.append((residual, ticker))

        if len(scored) < min_cross_section:
            continue

        scored.sort(key=lambda x: x[0])
        k = max(1, int(len(scored) * top_frac))
        longs = scored[:k]      # most negative residual -> expect bounce
        shorts = scored[-k:]    # most positive residual -> expect fade

        for residual, ticker in longs:
            events.append(_mk_event(ticker, t_date, +1, hold_days, strategy_name, "long", residual))
        for residual, ticker in shorts:
            events.append(_mk_event(ticker, t_date, -1, hold_days, strategy_name, "short", residual))

    return events


def _mk_event(ticker, date, direction, hold_days, name, leg, residual):
    return SignalEvent(
        ticker=ticker,
        signal_date=date,
        direction=direction,
        horizon_days=hold_days,
        strategy=name,
        tags={"leg": leg},
        meta={"residual": round(residual, 6)},
    )


def _sub_calendar_days(date: str, days: int) -> str:
    import datetime as dt

    d = dt.date.fromisoformat(date[:10])
    return (d - dt.timedelta(days=int(days))).isoformat()
