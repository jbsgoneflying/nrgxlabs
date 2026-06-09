"""Unit tests for the Pass B LLM quality overlay (offline, no network)."""
from __future__ import annotations

import datetime as dt

from backend.research.cost_model import CostModel
from backend.research.data_provider import InMemoryPriceProvider, PriceBar
from backend.research.event_study import SignalEvent
from backend.research.strategies.llm_overlay import (
    HeuristicGuidanceGrader,
    NewsVetoGrader,
    assign_quality_quintiles,
    run_quality_overlay,
)


# ---------------------------------------------------------------------------
# Graders
# ---------------------------------------------------------------------------

def test_heuristic_grader_tone():
    g = HeuristicGuidanceGrader()
    assert g.grade({"text": "We are raising guidance, record revenue, strong momentum"}) > 0.7
    assert g.grade({"text": "We cut guidance amid weak demand and headwinds, miss"}) < 0.3
    assert g.grade({"text": ""}) == 0.5
    assert g.grade({"text": "the weather was fine"}) == 0.5  # no signal words


def test_news_veto_grader():
    g = NewsVetoGrader()
    assert g.grade({"has_fresh_news": True}) == 0.0
    assert g.grade({"has_fresh_news": False}) == 1.0


# ---------------------------------------------------------------------------
# Quintile assignment
# ---------------------------------------------------------------------------

def test_assign_quality_quintiles_balanced():
    events = [SignalEvent(f"T{i}", "2022-06-01", 1, 10) for i in range(50)]
    # grader returns a score tied to the ticker index via context_fn
    grader = HeuristicGuidanceGrader()

    def ctx(ev):
        i = int(ev.ticker[1:])
        # more bullish words for higher i
        return {"text": "raise " * (i + 1) + "cut " * (50 - i)}

    tagged = assign_quality_quintiles(events, grader, ctx)
    quints = [e.tags["quality_quintile"] for e in tagged]
    assert set(quints) == {"Q1", "Q2", "Q3", "Q4", "Q5"}
    # roughly balanced
    for q in ("Q1", "Q5"):
        assert quints.count(q) == 10


# ---------------------------------------------------------------------------
# End-to-end overlay: quality should sort winners from losers
# ---------------------------------------------------------------------------

def _drift_series(drift_per_day: float, n_drift: int = 10, base="2022-05-02"):
    """Flat at 100 through the signal, then drift for n_drift sessions."""
    bars = []
    d = dt.date.fromisoformat(base)
    # 8 flat sessions (signal lands during these), then drift, then flat
    px = 100.0
    sessions = 0
    drift_started = 0
    while sessions < 40:
        if d.weekday() < 5:
            # start drifting after 2022-06-01
            if d.isoformat() > "2022-06-01" and drift_started < n_drift:
                px *= (1.0 + drift_per_day)
                drift_started += 1
            bars.append(PriceBar(date=d.isoformat(), open=px, high=px, low=px, close=px))
            sessions += 1
        d += dt.timedelta(days=1)
    return bars


def test_overlay_top_quintile_beats_bottom():
    prov = InMemoryPriceProvider()
    events = []
    grader = HeuristicGuidanceGrader()
    texts = {}
    n = 50
    for i in range(n):
        ticker = f"T{i:02d}"
        q = i / (n - 1)               # quality 0..1
        drift = (q - 0.5) * 0.01      # high quality -> up drift, low -> down
        prov.add(ticker, _drift_series(drift))
        events.append(SignalEvent(ticker, "2022-06-01", 1, 10, "PEAD"))
        texts[ticker] = {"text": "raise " * (i + 1) + "cut " * (n - i)}

    def ctx(ev):
        return texts[ev.ticker]

    out = run_quality_overlay(
        events, prov, grader, ctx,
        cost_model=CostModel.frictionless(), oos_start=None,
    )
    full = out["full"]
    assert full["adds_incremental_edge"] is True
    assert full["top_quintile_avg_net"] > full["bottom_quintile_avg_net"]
    assert full["top_lift_vs_full"] > 0
