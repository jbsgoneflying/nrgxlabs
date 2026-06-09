"""Data access abstraction for the research harness.

Everything downstream (event study, strategies, stats) depends only on these
small Protocols and plain dataclasses — never on a concrete API client. That
keeps the harness:
  * unit-testable offline (use the ``InMemory*`` providers, zero network), and
  * swappable to live data (see ``live_providers``) without touching logic.

All dates are ISO ``YYYY-MM-DD`` strings. Prices are split/dividend-adjusted
where the upstream provides it (EODHD ``adjusted_close``); otherwise raw close.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Sequence


# ---------------------------------------------------------------------------
# Core records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PriceBar:
    """One daily OHLCV bar. ``close`` is the adjusted close when available."""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def price(self, field_name: str) -> float:
        v = getattr(self, field_name, None)
        if v is None:
            raise KeyError(f"PriceBar has no field {field_name!r}")
        return float(v)


@dataclass(frozen=True)
class EarningsEvent:
    """A historical earnings report with surprise inputs.

    ``actual_eps``/``estimate_eps`` may be ``None`` when the upstream lacks them;
    strategy code must guard for that.
    """

    ticker: str
    report_date: str            # date the result was reported (YYYY-MM-DD)
    timing: str = "amc"         # "bmo" | "amc" | "during" | "" (unknown)
    actual_eps: Optional[float] = None
    estimate_eps: Optional[float] = None
    actual_revenue: Optional[float] = None
    estimate_revenue: Optional[float] = None

    def eps_surprise_pct(self) -> Optional[float]:
        """Standardized EPS surprise: (actual - est) / |est|.

        Returns ``None`` when inputs are missing or the estimate is ~zero (the
        ratio explodes and is meaningless there).
        """
        a, e = self.actual_eps, self.estimate_eps
        if a is None or e is None:
            return None
        if abs(e) < 1e-9:
            return None
        return (a - e) / abs(e)

    def revenue_surprise_pct(self) -> Optional[float]:
        a, e = self.actual_revenue, self.estimate_revenue
        if a is None or e is None or abs(e) < 1e-9:
            return None
        return (a - e) / abs(e)


@dataclass(frozen=True)
class OptionQuote:
    """One option contract quote on a given trade date."""

    expiry: str
    strike: float
    right: str   # "C" | "P"
    mid: float
    dte: int = 0
    delta: float = 0.0
    iv: float = 0.0


@dataclass(frozen=True)
class InsiderTxn:
    """A single SEC Form 4 insider transaction."""

    ticker: str
    filing_date: str            # when the form hit EDGAR (the knowable date)
    trade_date: str             # when the trade occurred
    owner: str
    side: str                   # "buy" | "sell"
    shares: float = 0.0
    price: float = 0.0

    def dollar_value(self) -> float:
        return abs(self.shares) * abs(self.price)


# ---------------------------------------------------------------------------
# Provider protocols
# ---------------------------------------------------------------------------

class PriceProvider(Protocol):
    def get_bars(self, ticker: str, start: str, end: str) -> List[PriceBar]:
        """Return date-sorted bars for ``ticker`` within [start, end] inclusive."""
        ...


class EarningsProvider(Protocol):
    def get_events(self, ticker: str, start: str, end: str) -> List[EarningsEvent]:
        ...


class InsiderProvider(Protocol):
    def get_transactions(self, ticker: str, start: str, end: str) -> List[InsiderTxn]:
        ...


class ChainProvider(Protocol):
    def get_chain(self, ticker: str, trade_date: str) -> List[OptionQuote]:
        """Return option quotes for ``ticker`` as of ``trade_date``."""
        ...


# ---------------------------------------------------------------------------
# PriceSeries — fast trading-day navigation over a single ticker's bars
# ---------------------------------------------------------------------------

class PriceSeries:
    """Indexed view of one ticker's bars supporting trading-day arithmetic.

    The harness aligns entries/exits by *trading days* (actual bars), never by
    calendar days, so weekends/holidays never silently shift a hold window.
    """

    def __init__(self, bars: Sequence[PriceBar]) -> None:
        clean = [b for b in bars if b is not None]
        clean.sort(key=lambda b: b.date)
        # de-dup by date, keeping the last occurrence
        seen: Dict[str, PriceBar] = {}
        for b in clean:
            seen[b.date] = b
        self.bars: List[PriceBar] = [seen[d] for d in sorted(seen.keys())]
        self.dates: List[str] = [b.date for b in self.bars]

    def __len__(self) -> int:
        return len(self.bars)

    def index_on_or_after(self, date: str) -> Optional[int]:
        """First bar index with date >= ``date`` (None if none exists)."""
        i = bisect.bisect_left(self.dates, date)
        return i if i < len(self.dates) else None

    def index_strictly_after(self, date: str) -> Optional[int]:
        """First bar index with date > ``date`` (None if none exists)."""
        i = bisect.bisect_right(self.dates, date)
        return i if i < len(self.dates) else None

    def bar_at(self, idx: int) -> Optional[PriceBar]:
        if 0 <= idx < len(self.bars):
            return self.bars[idx]
        return None


# ---------------------------------------------------------------------------
# In-memory providers (for tests / synthetic demos — zero network)
# ---------------------------------------------------------------------------

@dataclass
class InMemoryPriceProvider:
    """PriceProvider backed by a dict of ticker -> list[PriceBar]."""

    data: Dict[str, List[PriceBar]] = field(default_factory=dict)

    def add(self, ticker: str, bars: Sequence[PriceBar]) -> None:
        self.data[ticker.upper()] = sorted(bars, key=lambda b: b.date)

    def get_bars(self, ticker: str, start: str, end: str) -> List[PriceBar]:
        bars = self.data.get(ticker.upper(), [])
        return [b for b in bars if start <= b.date <= end]


@dataclass
class InMemoryEarningsProvider:
    data: Dict[str, List[EarningsEvent]] = field(default_factory=dict)

    def add(self, ticker: str, events: Sequence[EarningsEvent]) -> None:
        self.data[ticker.upper()] = sorted(events, key=lambda e: e.report_date)

    def get_events(self, ticker: str, start: str, end: str) -> List[EarningsEvent]:
        evs = self.data.get(ticker.upper(), [])
        return [e for e in evs if start <= e.report_date <= end]


@dataclass
class InMemoryInsiderProvider:
    data: Dict[str, List[InsiderTxn]] = field(default_factory=dict)

    def add(self, ticker: str, txns: Sequence[InsiderTxn]) -> None:
        self.data[ticker.upper()] = sorted(txns, key=lambda t: t.filing_date)

    def get_transactions(self, ticker: str, start: str, end: str) -> List[InsiderTxn]:
        txns = self.data.get(ticker.upper(), [])
        return [t for t in txns if start <= t.filing_date <= end]


@dataclass
class InMemoryChainProvider:
    """ChainProvider backed by a dict of (ticker, trade_date) -> list[OptionQuote]."""

    data: Dict[tuple, List[OptionQuote]] = field(default_factory=dict)

    def add(self, ticker: str, trade_date: str, quotes: Sequence[OptionQuote]) -> None:
        self.data[(ticker.upper(), trade_date)] = list(quotes)

    def get_chain(self, ticker: str, trade_date: str) -> List[OptionQuote]:
        return list(self.data.get((ticker.upper(), trade_date), []))
