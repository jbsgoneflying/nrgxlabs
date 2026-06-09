"""Unit tests for the Edge Bake-Off research harness core (offline, no network).

Validates the point-in-time event study math, cohort statistics, splits, and the
scorecard ranking against deterministic synthetic data.
"""
from __future__ import annotations

import math

import pytest

from backend.research.cohort_stats import group_by_tag, summarize
from backend.research.cost_model import CostModel
from backend.research.data_provider import InMemoryPriceProvider, PriceBar, PriceSeries
from backend.research.event_study import SignalEvent, run_event_study
from backend.research.report import build_scorecard
from backend.research.splits import decay_by_year, split_in_out

# Trading days in early Jan 2022 (weekends removed).
_DATES = [
    "2022-01-03", "2022-01-04", "2022-01-05", "2022-01-06", "2022-01-07",
    "2022-01-10", "2022-01-11", "2022-01-12", "2022-01-13", "2022-01-14",
]


def _bars(prices):
    """Build flat OHLC bars (open=high=low=close=price) unless price is a tuple
    (open, close), in which case open/close differ for field-selection tests."""
    out = []
    for d, p in zip(_DATES, prices):
        if isinstance(p, tuple):
            o, c = p
            out.append(PriceBar(date=d, open=o, high=max(o, c), low=min(o, c), close=c))
        else:
            out.append(PriceBar(date=d, open=p, high=p, low=p, close=p))
    return out


# ---------------------------------------------------------------------------
# CostModel
# ---------------------------------------------------------------------------

def test_cost_model_round_trip():
    assert CostModel(per_side_bps=10.0).round_trip() == pytest.approx(0.0020)
    assert CostModel.frictionless().round_trip() == 0.0
    with pytest.raises(ValueError):
        CostModel(per_side_bps=-1)


# ---------------------------------------------------------------------------
# PriceSeries navigation
# ---------------------------------------------------------------------------

def test_price_series_navigation_and_dedup():
    bars = _bars([100] * 10)
    # inject a duplicate date — should keep one entry
    dup = bars + [PriceBar(date="2022-01-05", open=1, high=1, low=1, close=1)]
    s = PriceSeries(dup)
    assert len(s) == 10
    assert s.index_strictly_after("2022-01-04") == 2   # -> 01-05
    assert s.index_on_or_after("2022-01-05") == 2
    assert s.index_strictly_after("2099-01-01") is None


# ---------------------------------------------------------------------------
# Event study P&L
# ---------------------------------------------------------------------------

def test_event_study_long_pnl_and_entry_offset():
    prices = [100, 100, 100, 100, 100, 110, 100, 100, 100, 100]  # 01-05=100, 01-10=110
    prov = InMemoryPriceProvider()
    prov.add("AAA", _bars(prices))
    ev = SignalEvent(ticker="AAA", signal_date="2022-01-04", direction=1, horizon_days=3, strategy="t")
    out = run_event_study([ev], prov, cost_model=CostModel(per_side_bps=10.0))
    assert out.n == 1
    r = out.results[0]
    assert r.entry_date == "2022-01-05"
    assert r.exit_date == "2022-01-10"
    assert r.holding_days == 3
    assert r.gross_return == pytest.approx(0.10)
    assert r.net_return == pytest.approx(0.098)
    assert r.win is True


def test_event_study_short_is_inverse():
    prices = [100, 100, 100, 100, 100, 110, 100, 100, 100, 100]
    prov = InMemoryPriceProvider()
    prov.add("AAA", _bars(prices))
    ev = SignalEvent(ticker="AAA", signal_date="2022-01-04", direction=-1, horizon_days=3)
    out = run_event_study([ev], prov, cost_model=CostModel(per_side_bps=10.0))
    r = out.results[0]
    assert r.gross_return == pytest.approx(-0.10)
    assert r.net_return == pytest.approx(-0.102)


def test_event_study_uses_configured_price_field():
    # open differs from close; default price_field='open' must use opens.
    prices = [(100, 999)] * 5 + [(110, 1)] + [(100, 100)] * 4
    prov = InMemoryPriceProvider()
    prov.add("AAA", _bars(prices))
    ev = SignalEvent(ticker="AAA", signal_date="2022-01-04", direction=1, horizon_days=3)
    out = run_event_study([ev], prov)
    assert out.results[0].gross_return == pytest.approx(0.10)


def test_event_study_skips_insufficient_forward_bars():
    prov = InMemoryPriceProvider()
    prov.add("AAA", _bars([100] * 10))
    # signal near the end with a long horizon -> not enough forward bars
    ev = SignalEvent(ticker="AAA", signal_date="2022-01-13", direction=1, horizon_days=10)
    out = run_event_study([ev], prov)
    assert out.n == 0
    assert len(out.skipped) == 1
    assert out.skipped[0]["reason"] == "insufficient_forward_bars"
    assert out.coverage == 0.0


def test_event_study_direction_validation():
    with pytest.raises(ValueError):
        SignalEvent(ticker="AAA", signal_date="2022-01-04", direction=0)


# ---------------------------------------------------------------------------
# Cohort stats
# ---------------------------------------------------------------------------

def _result(net, exit_date="2022-01-10", tag=None, gross=None, holding=3):
    from backend.research.event_study import TradeResult

    return TradeResult(
        ticker="AAA", strategy="t", signal_date="2022-01-04",
        entry_date=exit_date, exit_date=exit_date, direction=1,
        entry_price=100.0, exit_price=100.0 * (1 + net),
        gross_return=net if gross is None else gross, cost=0.0, net_return=net,
        holding_days=holding, tags=({} if tag is None else {"b": tag}),
    )


def test_summarize_basic_metrics():
    rs = [_result(0.10), _result(-0.05), _result(0.20), _result(-0.01)]
    s = summarize(rs)
    assert s.n == 4
    assert s.avg_net_return == pytest.approx((0.10 - 0.05 + 0.20 - 0.01) / 4)
    assert s.hit_rate == pytest.approx(0.5)
    assert s.best == pytest.approx(0.20)
    assert s.worst == pytest.approx(-0.05)
    # compounded: 1.1*0.95*1.2*0.99 - 1
    expected = 1.10 * 0.95 * 1.20 * 0.99 - 1.0
    assert s.total_compounded_return == pytest.approx(expected, abs=1e-6)


def test_summarize_empty():
    s = summarize([])
    assert s.n == 0 and s.avg_net_return == 0.0


def test_max_drawdown_detects_trough():
    # +10%, then -50% -> equity 1.1 then 0.55; peak 1.1 -> dd = (1.1-0.55)/1.1 = 0.5
    rs = [_result(0.10, exit_date="2022-01-05"), _result(-0.50, exit_date="2022-01-10")]
    s = summarize(rs)
    assert s.max_drawdown == pytest.approx(0.5, abs=1e-6)


def test_group_by_tag():
    rs = [_result(0.10, tag="hi"), _result(0.20, tag="hi"), _result(-0.10, tag="lo")]
    g = group_by_tag(rs, "b")
    assert set(g.keys()) == {"hi", "lo"}
    assert g["hi"].n == 2
    assert g["hi"].avg_net_return == pytest.approx(0.15)
    assert g["lo"].avg_net_return == pytest.approx(-0.10)


def test_t_stat_sign_matches_mean():
    pos = summarize([_result(0.05), _result(0.04), _result(0.06), _result(0.05)])
    assert pos.t_stat > 0
    neg = summarize([_result(-0.05), _result(-0.04), _result(-0.06), _result(-0.05)])
    assert neg.t_stat < 0


# ---------------------------------------------------------------------------
# Splits & decay
# ---------------------------------------------------------------------------

def test_split_in_out_by_entry_date():
    rs = [
        _result(0.10, exit_date="2021-06-01"),
        _result(0.20, exit_date="2023-06-01"),
        _result(-0.10, exit_date="2024-06-01"),
    ]
    in_s, oos = split_in_out(rs, oos_start="2023-01-01")
    assert len(in_s) == 1
    assert len(oos) == 2


def test_decay_by_year():
    rs = [
        _result(0.10, exit_date="2022-03-01"),
        _result(0.20, exit_date="2022-09-01"),
        _result(-0.10, exit_date="2024-03-01"),
    ]
    decay = decay_by_year(rs)
    assert set(decay.keys()) == {"2022", "2024"}
    assert decay["2022"].n == 2
    assert decay["2024"].n == 1


# ---------------------------------------------------------------------------
# Scorecard ranking
# ---------------------------------------------------------------------------

def test_scorecard_ranks_and_flags_alive():
    reports = [
        {"strategy": "dead", "out_of_sample": {"n": 200, "avg_net_return": -0.001, "t_stat": -0.3, "hit_rate": 0.48, "sharpe_annualized": -0.2, "max_drawdown": 0.3}},
        {"strategy": "alive", "out_of_sample": {"n": 200, "avg_net_return": 0.004, "t_stat": 2.6, "hit_rate": 0.55, "sharpe_annualized": 1.1, "max_drawdown": 0.2}},
        {"strategy": "thin", "out_of_sample": {"n": 12, "avg_net_return": 0.02, "t_stat": 3.0, "hit_rate": 0.7, "sharpe_annualized": 2.0, "max_drawdown": 0.1}},
    ]
    sc = build_scorecard(reports)
    rows = {r["strategy"]: r for r in sc["rows"]}
    assert rows["alive"]["alive"] is True
    assert rows["dead"]["alive"] is False
    assert rows["thin"]["alive"] is False  # fails n>=30
    # alive strategy ranks first
    assert sc["rows"][0]["strategy"] == "alive"
