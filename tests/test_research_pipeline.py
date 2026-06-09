"""End-to-end pipeline test on synthetic data (offline, no network).

Proves the full bake-off chain works: synthetic world with injected edges ->
signal gen -> event study -> report -> scorecard, with the known edges flagged
alive and the no-edge control flagged dead.
"""
from __future__ import annotations

import random

from backend.research.cost_model import CostModel
from backend.research.event_study import SignalEvent, run_event_study
from backend.research.report import build_scorecard, build_strategy_report
from backend.research.strategies.insider_cluster import generate_insider_cluster_events
from backend.research.strategies.pead import generate_pead_events
from backend.research.synthetic import all_synthetic_tickers, build_synthetic_dataset


def test_synthetic_pipeline_separates_edge_from_noise():
    price, earnings, insider, injected = build_synthetic_dataset(seed=7)
    tickers = all_synthetic_tickers()
    cost = CostModel(per_side_bps=10.0)

    # PEAD: injected drift in surprise direction -> strong positive edge.
    pead_events = generate_pead_events(earnings, tickers, "2021-01-01", "2025-12-31", horizon_days=10)
    assert len(pead_events) > 100
    pead_out = run_event_study(pead_events, price, cost_model=cost)
    pead_report = build_strategy_report("PEAD", pead_out)
    assert pead_report["out_of_sample"]["avg_net_return"] > 0
    assert pead_report["out_of_sample"]["t_stat"] > 1.5

    # Random control: no injected edge -> should not be flagged alive.
    rng = random.Random(123)
    ctrl_events = []
    for t in tickers:
        bars = price.get_bars(t, "2021-01-01", "2025-09-01")
        for _ in range(20):
            b = rng.choice(bars)
            ctrl_events.append(SignalEvent(t, b.date, rng.choice([-1, 1]), 10, "RandomControl"))
    ctrl_out = run_event_study(ctrl_events, price, cost_model=cost)
    ctrl_report = build_strategy_report("RandomControl", ctrl_out)

    sc = build_scorecard([pead_report, ctrl_report])
    rows = {r["strategy"]: r for r in sc["rows"]}
    assert rows["PEAD"]["alive"] is True
    assert rows["RandomControl"]["alive"] is False
    # PEAD ranks above the control.
    assert sc["rows"][0]["strategy"] == "PEAD"


def test_synthetic_insider_signals_have_positive_edge():
    price, _earnings, insider, _injected = build_synthetic_dataset(seed=7)
    tickers = all_synthetic_tickers()
    events = generate_insider_cluster_events(
        insider, tickers, "2021-01-01", "2025-12-31",
        min_net_dollars=100_000, horizon_days=10,
    )
    assert events, "expected insider cluster signals in synthetic data"
    out = run_event_study(events, price, cost_model=CostModel(per_side_bps=10.0))
    report = build_strategy_report("InsiderCluster", out)
    # Injected upward drift -> positive full-sample edge regardless of sample size.
    assert report["full_sample"]["avg_net_return"] > 0
    assert report["full_sample"]["hit_rate"] > 0.5
