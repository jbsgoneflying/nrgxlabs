"""Deterministic synthetic dataset for demonstrating the harness offline.

This exists so the *full* bake-off pipeline (signal gen -> event study -> report
-> scorecard) can be run and validated with ZERO network/API quota. It builds a
random-walk price world and injects two known edges plus one no-edge control:

  * post-earnings drift in the direction of the EPS surprise (PEAD edge),
  * post-insider-cluster upward drift (insider edge),
  * random signals with no injected drift (control -> should look dead).

It is NOT a substitute for the live bake-off. Real numbers require running the
CLI's live subcommands with API keys. The synthetic numbers only prove the
plumbing is correct and show the desk the output format.
"""
from __future__ import annotations

import datetime as dt
import random
from typing import Dict, List, Tuple

from backend.research.data_provider import (
    EarningsEvent,
    InMemoryEarningsProvider,
    InMemoryInsiderProvider,
    InMemoryPriceProvider,
    InsiderTxn,
    PriceBar,
)


def _business_days(start: str, end: str) -> List[str]:
    out: List[str] = []
    d = dt.date.fromisoformat(start)
    last = dt.date.fromisoformat(end)
    while d <= last:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def build_synthetic_dataset(
    *,
    seed: int = 7,
    n_tickers: int = 40,
    start: str = "2021-01-01",
    end: str = "2025-12-31",
    pead_drift_per_day: float = 0.004,
    insider_drift_per_day: float = 0.003,
    drift_days: int = 10,
) -> Tuple[InMemoryPriceProvider, InMemoryEarningsProvider, InMemoryInsiderProvider, Dict[str, list]]:
    """Return (price, earnings, insider) providers + a dict of injected events."""
    rng = random.Random(seed)
    cal = _business_days(start, end)
    ncal = len(cal)

    price = InMemoryPriceProvider()
    earnings = InMemoryEarningsProvider()
    insider = InMemoryInsiderProvider()
    injected: Dict[str, list] = {"pead": [], "insider": []}

    for k in range(n_tickers):
        ticker = f"SYN{k:02d}"
        rets = [rng.gauss(0.0003, 0.015) for _ in range(ncal)]
        ticker_earnings: List[EarningsEvent] = []
        ticker_insiders: List[InsiderTxn] = []

        # Quarterly earnings (~63 trading days apart) with injected drift.
        e = 70 + rng.randint(0, 20)
        while e < ncal - drift_days - 2:
            surprise = rng.uniform(-0.4, 0.4)
            sign = 1.0 if surprise >= 0 else -1.0
            for d in range(1, drift_days + 1):
                rets[e + d] += sign * pead_drift_per_day
            est = 1.00
            actual = round(est * (1.0 + surprise), 4)
            ticker_earnings.append(
                EarningsEvent(ticker, cal[e], "amc", actual_eps=actual, estimate_eps=est)
            )
            injected["pead"].append((ticker, cal[e], round(surprise, 4)))
            e += 63 + rng.randint(-3, 3)

        # A couple of insider clusters per ~half the names, with upward drift.
        if k % 2 == 0:
            for _ in range(2):
                c = rng.randint(80, ncal - drift_days - 2)
                for d in range(1, drift_days + 1):
                    rets[c + d] += insider_drift_per_day
                base_px = 100.0
                for owner in ("CEO", "CFO", "DIR"):
                    ticker_insiders.append(
                        InsiderTxn(
                            ticker, cal[c], cal[c], owner, "buy",
                            shares=2000, price=base_px,
                        )
                    )
                injected["insider"].append((ticker, cal[c]))

        # Compound returns into bars.
        px = 50.0 + rng.uniform(0, 150)
        bars: List[PriceBar] = []
        for i, r in enumerate(rets):
            px = max(0.5, px * (1.0 + r))
            bars.append(PriceBar(date=cal[i], open=px, high=px, low=px, close=px))
        price.add(ticker, bars)
        earnings.add(ticker, ticker_earnings)
        if ticker_insiders:
            insider.add(ticker, ticker_insiders)

    return price, earnings, insider, injected


def all_synthetic_tickers(n_tickers: int = 40) -> List[str]:
    return [f"SYN{k:02d}" for k in range(n_tickers)]
