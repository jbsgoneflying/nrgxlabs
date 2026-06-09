"""Unit tests for the Tier 1 strategy signal generators (offline, no network)."""
from __future__ import annotations

import datetime as dt

import pytest

from backend.research.data_provider import (
    EarningsEvent,
    InMemoryEarningsProvider,
    InMemoryInsiderProvider,
    InMemoryPriceProvider,
    InsiderTxn,
    PriceBar,
)
from backend.research.strategies.insider_cluster import generate_insider_cluster_events
from backend.research.strategies.pead import generate_pead_events
from backend.research.strategies.residual_reversal import (
    generate_residual_reversal_events,
)


# ---------------------------------------------------------------------------
# PEAD
# ---------------------------------------------------------------------------

def test_pead_direction_and_threshold():
    prov = InMemoryEarningsProvider()
    prov.add("AAA", [
        EarningsEvent("AAA", "2022-02-01", "amc", actual_eps=1.30, estimate_eps=1.00),  # +30% beat
        EarningsEvent("AAA", "2022-05-01", "amc", actual_eps=0.50, estimate_eps=1.00),  # -50% miss
        EarningsEvent("AAA", "2022-08-01", "amc", actual_eps=1.01, estimate_eps=1.00),  # +1% (below floor)
    ])
    evs = generate_pead_events(prov, ["AAA"], "2022-01-01", "2022-12-31", min_abs_surprise=0.05)
    assert len(evs) == 2  # the +1% surprise is filtered
    beat = next(e for e in evs if e.signal_date == "2022-02-01")
    miss = next(e for e in evs if e.signal_date == "2022-05-01")
    assert beat.direction == 1
    assert beat.tags["surprise_bucket"] == "beat_large"
    assert miss.direction == -1
    assert miss.tags["surprise_bucket"] == "miss_large"


def test_pead_long_only_skips_misses():
    prov = InMemoryEarningsProvider()
    prov.add("AAA", [
        EarningsEvent("AAA", "2022-05-01", "amc", actual_eps=0.50, estimate_eps=1.00),
    ])
    evs = generate_pead_events(prov, ["AAA"], "2022-01-01", "2022-12-31", long_only=True)
    assert evs == []


def test_pead_skips_missing_or_zero_estimate():
    prov = InMemoryEarningsProvider()
    prov.add("AAA", [
        EarningsEvent("AAA", "2022-02-01", "amc", actual_eps=1.0, estimate_eps=None),
        EarningsEvent("AAA", "2022-05-01", "amc", actual_eps=1.0, estimate_eps=0.0),
    ])
    evs = generate_pead_events(prov, ["AAA"], "2022-01-01", "2022-12-31")
    assert evs == []


# ---------------------------------------------------------------------------
# Insider cluster
# ---------------------------------------------------------------------------

def test_insider_cluster_requires_distinct_buyers():
    prov = InMemoryInsiderProvider()
    # Two distinct owners buying within the window, each $1M -> cluster.
    prov.add("AAA", [
        InsiderTxn("AAA", "2022-03-01", "2022-03-01", "CEO", "buy", shares=10000, price=100),
        InsiderTxn("AAA", "2022-03-03", "2022-03-03", "CFO", "buy", shares=10000, price=100),
    ])
    evs = generate_insider_cluster_events(
        prov, ["AAA"], "2022-01-01", "2022-12-31",
        min_distinct_buyers=2, window_days=7, min_net_dollars=100_000,
    )
    assert len(evs) == 1
    assert evs[0].direction == 1
    assert evs[0].tags["cluster_bucket"] == "cluster_2"
    assert evs[0].meta["distinct_buyers"] == 2.0


def test_insider_single_buyer_no_signal():
    prov = InMemoryInsiderProvider()
    prov.add("AAA", [
        InsiderTxn("AAA", "2022-03-01", "2022-03-01", "CEO", "buy", shares=10000, price=100),
        InsiderTxn("AAA", "2022-03-02", "2022-03-02", "CEO", "buy", shares=10000, price=100),
    ])
    evs = generate_insider_cluster_events(prov, ["AAA"], "2022-01-01", "2022-12-31", min_distinct_buyers=2)
    assert evs == []


def test_insider_sells_ignored_and_dollar_floor():
    prov = InMemoryInsiderProvider()
    prov.add("AAA", [
        InsiderTxn("AAA", "2022-03-01", "2022-03-01", "CEO", "sell", shares=10000, price=100),
        InsiderTxn("AAA", "2022-03-02", "2022-03-02", "CFO", "buy", shares=10, price=100),  # $1k only
        InsiderTxn("AAA", "2022-03-03", "2022-03-03", "Dir", "buy", shares=10, price=100),
    ])
    evs = generate_insider_cluster_events(prov, ["AAA"], "2022-01-01", "2022-12-31", min_net_dollars=100_000)
    assert evs == []  # buys are tiny, sells ignored


# ---------------------------------------------------------------------------
# Residual reversal
# ---------------------------------------------------------------------------

def _series_from_returns(returns, base=100.0, start="2022-01-03"):
    """Build PriceBars (flat OHLC=close) from a return series on business days."""
    bars = []
    d = dt.date.fromisoformat(start)
    px = base
    # day 0 bar at base, then apply returns
    bars.append(PriceBar(date=d.isoformat(), open=px, high=px, low=px, close=px))
    for r in returns:
        d = _next_business_day(d)
        px = px * (1.0 + r)
        bars.append(PriceBar(date=d.isoformat(), open=px, high=px, low=px, close=px))
    return bars


def _next_business_day(d: dt.date) -> dt.date:
    d = d + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d = d + dt.timedelta(days=1)
    return d


def test_residual_reversal_long_loser_short_winner():
    n = 30
    # The single rebalance fires at bar index 18 (beta_window 15 + formation 3),
    # whose 3-day formation window is daily returns R[15], R[16], R[17].
    # Market: alternating returns for beta variance, but the formation window is
    # set to net ~0 so beta-estimation noise can't flip the residual ranking.
    mkt_rets = [0.01 if i % 2 == 0 else -0.006 for i in range(n)]
    formation_idx = (15, 16, 17)
    mkt_rets[15], mkt_rets[16], mkt_rets[17] = 0.02, -0.02, 0.0
    # Stocks track the market (beta ~1) plus an idiosyncratic shock applied
    # exactly in the rebalance's formation window.
    base = list(mkt_rets)

    def with_recent(shock):
        r = list(base)
        for k in formation_idx:
            r[k] = r[k] + shock
        return r

    prov = InMemoryPriceProvider()
    prov.add("MKT", _series_from_returns(mkt_rets))
    prov.add("LOSER", _series_from_returns(with_recent(-0.05)))   # recent drop -> long
    prov.add("WINNER", _series_from_returns(with_recent(+0.05)))  # recent jump -> short
    prov.add("NEUT1", _series_from_returns(base))
    prov.add("NEUT2", _series_from_returns(base))

    # Determine the rebalance date range from the generated calendar.
    bars = prov.get_bars("MKT", "2000-01-01", "2099-01-01")
    all_dates = [b.date for b in bars]

    evs = generate_residual_reversal_events(
        prov,
        universe=["LOSER", "WINNER", "NEUT1", "NEUT2"],
        market_ticker="MKT",
        start=all_dates[0],
        end=all_dates[-1],
        formation_days=3,
        hold_days=3,
        beta_window=15,
        top_frac=0.25,
        rebalance_every=100,   # single rebalance for a deterministic assertion
        history_buffer_days=5,
        min_cross_section=4,
    )
    assert evs, "expected at least one rebalance to fire"
    longs = {e.ticker for e in evs if e.direction == 1}
    shorts = {e.ticker for e in evs if e.direction == -1}
    assert "LOSER" in longs
    assert "WINNER" in shorts


def test_residual_reversal_handles_short_market_series():
    prov = InMemoryPriceProvider()
    prov.add("MKT", _series_from_returns([0.01, -0.01, 0.01]))
    evs = generate_residual_reversal_events(
        prov, universe=["A"], market_ticker="MKT",
        start="2022-01-03", end="2022-02-01",
        beta_window=60, formation_days=5,
    )
    assert evs == []
