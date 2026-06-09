"""Event-study core: turn dated signals into realized, after-cost trade P&L.

This is the piece `backend/backtest_engine.py` never had — a genuine
point-in-time replay. Given a list of ``SignalEvent`` (ticker, the date the
signal was *knowable*, a direction, and a hold horizon), it:

  1. enters ``entry_offset`` trading days after the signal date (default 1 — you
     learn the signal after the close, you trade the next session),
  2. exits ``horizon_days`` trading days later,
  3. computes the directional gross return and subtracts round-trip costs.

Entries/exits are aligned on *actual bars* (trading days), so holidays and
weekends never silently distort the window. If a ticker lacks enough forward
bars (e.g. signal too close to the end of available data, or a delisting), the
event is skipped and counted in ``EventStudyOutcome.skipped`` — surfacing
survivorship/coverage gaps instead of hiding them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from backend.research.cost_model import CostModel
from backend.research.data_provider import PriceProvider, PriceSeries

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignalEvent:
    """A signal to act on.

    direction: +1 = long, -1 = short.
    signal_date: the date the signal became knowable (point-in-time anchor).
    horizon_days: trading days to hold after entry.
    tags: arbitrary labels for cohort bucketing (e.g. {"surprise_bucket": "Q5"}).
    """

    ticker: str
    signal_date: str
    direction: int
    horizon_days: int = 10
    strategy: str = ""
    tags: Dict[str, str] = field(default_factory=dict)
    meta: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.direction not in (-1, 1):
            raise ValueError("direction must be +1 (long) or -1 (short)")
        if self.horizon_days < 1:
            raise ValueError("horizon_days must be >= 1")


@dataclass(frozen=True)
class TradeResult:
    """Realized outcome of one event."""

    ticker: str
    strategy: str
    signal_date: str
    entry_date: str
    exit_date: str
    direction: int
    entry_price: float
    exit_price: float
    gross_return: float
    cost: float
    net_return: float
    holding_days: int
    tags: Dict[str, str] = field(default_factory=dict)

    @property
    def year(self) -> int:
        return int(self.entry_date[:4])

    @property
    def win(self) -> bool:
        return self.net_return > 0


@dataclass
class EventStudyOutcome:
    results: List[TradeResult] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.results)

    @property
    def coverage(self) -> float:
        total = len(self.results) + len(self.skipped)
        return (len(self.results) / total) if total else 0.0


def _evaluate_one(
    ev: SignalEvent,
    series: PriceSeries,
    cost_model: CostModel,
    entry_offset: int,
    price_field: str,
) -> Optional[TradeResult]:
    # Entry: the first session strictly after signal_date, then step forward
    # (entry_offset - 1) more sessions. entry_offset=1 -> next session.
    start_idx = series.index_strictly_after(ev.signal_date)
    if start_idx is None:
        return None
    entry_idx = start_idx + (entry_offset - 1)
    exit_idx = entry_idx + ev.horizon_days

    entry_bar = series.bar_at(entry_idx)
    exit_bar = series.bar_at(exit_idx)
    if entry_bar is None or exit_bar is None:
        return None

    entry_px = entry_bar.price(price_field)
    exit_px = exit_bar.price(price_field)
    if entry_px <= 0 or exit_px <= 0:
        return None

    raw_move = (exit_px - entry_px) / entry_px
    gross = ev.direction * raw_move
    cost = cost_model.round_trip()
    net = gross - cost

    # Holding span in trading days (bars between entry and exit).
    holding_days = exit_idx - entry_idx

    return TradeResult(
        ticker=ev.ticker,
        strategy=ev.strategy,
        signal_date=ev.signal_date,
        entry_date=entry_bar.date,
        exit_date=exit_bar.date,
        direction=ev.direction,
        entry_price=round(entry_px, 6),
        exit_price=round(exit_px, 6),
        gross_return=round(gross, 6),
        cost=round(cost, 6),
        net_return=round(net, 6),
        holding_days=holding_days,
        tags=dict(ev.tags),
    )


def run_event_study(
    events: List[SignalEvent],
    price_provider: PriceProvider,
    *,
    cost_model: Optional[CostModel] = None,
    entry_offset: int = 1,
    price_field: str = "open",
    lookahead_buffer_days: int = 40,
) -> EventStudyOutcome:
    """Replay ``events`` against ``price_provider`` and return realized trades.

    Bars are fetched per ticker for the window [signal_date, signal_date +
    horizon + buffer], so large horizons still resolve. ``lookahead_buffer_days``
    is a *calendar*-day cushion (must exceed the largest horizon expressed in
    trading days; default 40 comfortably covers a ~3-week hold).
    """
    cost_model = cost_model or CostModel()
    outcome = EventStudyOutcome()

    # Group events by ticker to minimize provider round-trips.
    by_ticker: Dict[str, List[SignalEvent]] = {}
    for ev in events:
        by_ticker.setdefault(ev.ticker.upper(), []).append(ev)

    for ticker, evs in by_ticker.items():
        evs.sort(key=lambda e: e.signal_date)
        lo = min(e.signal_date for e in evs)
        max_horizon = max(e.horizon_days for e in evs)
        hi = _add_calendar_days(
            max(e.signal_date for e in evs),
            max_horizon * 2 + lookahead_buffer_days,
        )
        try:
            bars = price_provider.get_bars(ticker, lo, hi)
        except Exception as exc:  # network/coverage failure -> skip, don't crash
            _LOG.warning("price fetch failed for %s: %s", ticker, exc)
            for ev in evs:
                outcome.skipped.append(
                    {"ticker": ticker, "signal_date": ev.signal_date, "reason": "fetch_error"}
                )
            continue

        series = PriceSeries(bars)
        for ev in evs:
            res = _evaluate_one(ev, series, cost_model, entry_offset, price_field)
            if res is None:
                outcome.skipped.append(
                    {
                        "ticker": ticker,
                        "signal_date": ev.signal_date,
                        "reason": "insufficient_forward_bars",
                    }
                )
            else:
                outcome.results.append(res)

    outcome.results.sort(key=lambda r: (r.exit_date, r.ticker))
    return outcome


def _add_calendar_days(date: str, days: int) -> str:
    import datetime as dt

    d = dt.date.fromisoformat(date[:10])
    return (d + dt.timedelta(days=int(days))).isoformat()
