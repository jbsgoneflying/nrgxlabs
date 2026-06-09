"""Unit tests for the Tier 2 catalyst-convexity pilot (offline, no network)."""
from __future__ import annotations

import datetime as dt

import pytest

from backend.research.data_provider import (
    InMemoryChainProvider,
    InMemoryPriceProvider,
    OptionQuote,
    PriceBar,
)
from backend.research.strategies.catalyst_convexity import (
    Catalyst,
    load_catalyst_calendar,
    run_convexity_study,
)


def _bdays(start: str, n: int):
    out = []
    d = dt.date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def _flat_bars(dates, px=100.0):
    return [PriceBar(date=d, open=px, high=px, low=px, close=px) for d in dates]


def _straddle(expiry, strike, dte, call_mid, put_mid):
    return [
        OptionQuote(expiry, strike, "C", call_mid, dte),
        OptionQuote(expiry, strike, "P", put_mid, dte),
    ]


def test_convexity_straddle_pnl():
    dates = _bdays("2022-05-02", 40)
    prov = InMemoryPriceProvider()
    prov.add("BIO", _flat_bars(dates))

    event_date = dates[20]
    ev_idx = 20
    entry_date = dates[ev_idx - 10]
    exit_date = dates[ev_idx + 1]

    chains = InMemoryChainProvider()
    # entry straddle premium = 6; exit premium = 9 -> gross +50%
    chains.add("BIO", entry_date, _straddle("2022-07-15", 100.0, 40, 3.0, 3.0))
    chains.add("BIO", exit_date, _straddle("2022-07-15", 100.0, 25, 5.0, 4.0))

    out = run_convexity_study(
        [Catalyst("BIO", event_date, "fda_pdufa")], prov, chains,
        entry_lead_days=10, exit_offset_days=1, target_dte=40, premium_cost_pct=0.04,
    )
    assert out.n == 1
    r = out.results[0]
    assert r.entry_price == pytest.approx(6.0)
    assert r.exit_price == pytest.approx(9.0)
    assert r.gross_return == pytest.approx(0.5)
    assert r.net_return == pytest.approx(0.46)
    assert r.tags["kind"] == "fda_pdufa"


def test_convexity_skips_when_no_entry_chain():
    dates = _bdays("2022-05-02", 40)
    prov = InMemoryPriceProvider()
    prov.add("BIO", _flat_bars(dates))
    chains = InMemoryChainProvider()  # empty
    out = run_convexity_study(
        [Catalyst("BIO", dates[20], "product")], prov, chains, entry_lead_days=10,
    )
    assert out.n == 0
    assert out.skipped and out.skipped[0]["reason"] == "no_entry_straddle"


def test_convexity_requires_min_remaining_dte():
    dates = _bdays("2022-05-02", 40)
    prov = InMemoryPriceProvider()
    prov.add("BIO", _flat_bars(dates))
    event_date = dates[20]
    entry_date = dates[10]
    chains = InMemoryChainProvider()
    # dte only 5 at entry -> below min_dte (10+1+7=18) -> skipped
    chains.add("BIO", entry_date, _straddle("2022-05-20", 100.0, 5, 3.0, 3.0))
    out = run_convexity_study([Catalyst("BIO", event_date)], prov, chains, entry_lead_days=10)
    assert out.n == 0


def test_load_seed_calendar():
    cats = load_catalyst_calendar()
    # seed file exists with at least the placeholder
    assert isinstance(cats, list)
    assert any(c.ticker == "EXAMPLE" for c in cats)
