"""Strategy #3 — Smart-money insider (Form 4) cluster drift.

Thesis: a single insider buy is noise; a *cluster* of distinct insiders buying
meaningful dollar amounts in a short window precedes multi-week drift. Retail
reacts to the headline ("an insider bought"); the edge is in cluster strength.

Construction (point-in-time on the EDGAR filing date):
  * Slide over buy filings; when, within a trailing ``window_days``, at least
    ``min_distinct_buyers`` distinct owners have bought and net buy dollars clear
    ``min_net_dollars``, emit a long signal anchored to that filing date.
  * A ``cooldown`` (defaults to the window) prevents the same cluster from
    emitting repeatedly on consecutive days.

Equity-only (no options pricing), so the event study handles P&L directly.
"""
from __future__ import annotations

from typing import List, Sequence

from backend.research.data_provider import InsiderProvider, InsiderTxn
from backend.research.event_study import SignalEvent


def _cluster_bucket(n_buyers: int) -> str:
    if n_buyers >= 4:
        return "cluster_4plus"
    if n_buyers == 3:
        return "cluster_3"
    return "cluster_2"


def _days_between(a: str, b: str) -> int:
    import datetime as dt

    return abs((dt.date.fromisoformat(a[:10]) - dt.date.fromisoformat(b[:10])).days)


def generate_insider_cluster_events(
    insider_provider: InsiderProvider,
    tickers: Sequence[str],
    start: str,
    end: str,
    *,
    min_distinct_buyers: int = 2,
    window_days: int = 7,
    min_net_dollars: float = 100_000.0,
    horizon_days: int = 10,
    strategy_name: str = "InsiderCluster",
) -> List[SignalEvent]:
    events: List[SignalEvent] = []
    for ticker in tickers:
        try:
            txns = insider_provider.get_transactions(ticker, start, end)
        except Exception:
            continue
        buys: List[InsiderTxn] = sorted(
            [t for t in txns if t.side == "buy"], key=lambda t: t.filing_date
        )
        if len(buys) < min_distinct_buyers:
            continue

        last_emit: str | None = None
        for j in range(len(buys)):
            anchor = buys[j].filing_date
            # Trailing window ending at this filing.
            window = [b for b in buys if 0 <= _days_between(anchor, b.filing_date) <= window_days and b.filing_date <= anchor]
            distinct = {b.owner for b in window}
            net = sum(b.dollar_value() for b in window)
            if len(distinct) >= min_distinct_buyers and net >= min_net_dollars:
                if last_emit is not None and _days_between(anchor, last_emit) <= window_days:
                    continue  # cooldown: same cluster
                events.append(
                    SignalEvent(
                        ticker=ticker.upper(),
                        signal_date=anchor,
                        direction=1,
                        horizon_days=horizon_days,
                        strategy=strategy_name,
                        tags={"cluster_bucket": _cluster_bucket(len(distinct))},
                        meta={"distinct_buyers": float(len(distinct)), "net_dollars": round(net, 2)},
                    )
                )
                last_emit = anchor
    return events
