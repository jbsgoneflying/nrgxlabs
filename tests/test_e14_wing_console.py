"""Engine 14 v2 — Wing Console scorer tests."""
from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass

import pytest

from backend.engine14 import (
    DEFAULT_WEIGHTS,
    MAEDistribution,
    PlacementScore,
    ScoringContext,
    WingConsoleWeights,
    build_wing_console,
    run_forward_mc,
    score_placements,
    score_single_placement,
)
from backend.engine14.scoring_context import (
    clear_scoring_cache,
    get_scoring_context,
    store_scoring_context,
)


@dataclass
class _Bar:
    open:  float
    high:  float
    low:   float
    close: float


def _synthetic():
    rng = random.Random(42)
    start = dt.date(2024, 1, 2)
    closes = {}
    spot = 5000.0
    d = start
    while len(closes) < 60 * 5:
        if d.weekday() < 5:
            spot *= (1.0 + rng.gauss(0, 0.008))
            closes[d.isoformat()] = spot
        d += dt.timedelta(days=1)
    dates = sorted(closes.keys())
    wins = []
    for i in range(0, len(dates) - 5, 5):
        wins.append({
            "entry_date":    dates[i],
            "expiry_date":   dates[i + 4],
            "regime_bucket": "MODERATE",
            "macro_bucket":  "NORMAL",
        })
    ohlc = {d: _Bar(open=closes[d], high=closes[d] * 1.008,
                     low=closes[d] * 0.992, close=closes[d])
             for d in closes}
    return closes, wins, ohlc


def test_score_placements_returns_ranked_grid():
    closes, wins, _ohlc = _synthetic()
    mc = run_forward_mc(
        ticker="SPX", as_of_date="2026-04-21",
        spot=5000.0, em_pct=1.5, hold_days=5,
        analogue_windows=wins, closes_by_date=closes,
        placements=[(1.0, 10.0), (1.5, 10.0), (2.0, 10.0)],
        n_sims=600, min_pool=10,
    )
    mae = MAEDistribution(n=20, p50=0.5, p75=1.0, p90=1.4, p95=1.8,
                           source="daily_ohlc")
    plc = score_placements(
        spot=5000.0, em_pct=1.5, hold_days=5, dte_calendar_days=5,
        historical_events=[{"signed_move_pct": 0.0}] * 30,
        mae=mae, mc_result=mc,
        em_mults=[1.0, 1.5, 2.0], wing_pts=[10.0],
    )
    assert len(plc) == 3
    scores = [p.composite_score for p in plc]
    assert scores == sorted(scores, reverse=True)
    for p in plc:
        assert isinstance(p, PlacementScore)
        assert p.short_put_strike < 5000.0 < p.short_call_strike


def test_score_placements_weights_sensitivity():
    closes, wins, _ohlc = _synthetic()
    mc = run_forward_mc(
        ticker="SPX", as_of_date="2026-04-21",
        spot=5000.0, em_pct=1.5, hold_days=5,
        analogue_windows=wins, closes_by_date=closes,
        placements=[(1.0, 10.0), (2.0, 10.0)],
        n_sims=500, min_pool=10,
    )
    mae = MAEDistribution(n=20, p95=2.2, source="daily_ohlc")
    safety = WingConsoleWeights(close=0.80, touch=0.05, mae=0.05, theta=0.05, credit=0.05)
    default_w = score_placements(
        spot=5000.0, em_pct=1.5, hold_days=5, dte_calendar_days=5,
        historical_events=[], mae=mae, mc_result=mc,
        em_mults=[1.0, 2.0], wing_pts=[10.0], weights=DEFAULT_WEIGHTS,
    )
    safety_w = score_placements(
        spot=5000.0, em_pct=1.5, hold_days=5, dte_calendar_days=5,
        historical_events=[], mae=mae, mc_result=mc,
        em_mults=[1.0, 2.0], wing_pts=[10.0], weights=safety,
    )
    # Weights that emphasise safety should produce DIFFERENT scores than
    # the balanced defaults — we assert sensitivity rather than a specific
    # directional ordering (which is a function of the synthetic pool).
    by_em_d = {p.em_mult: p for p in default_w}
    by_em_s = {p.em_mult: p for p in safety_w}
    assert by_em_d[1.0].composite_score != by_em_s[1.0].composite_score
    assert by_em_d[2.0].composite_score != by_em_s[2.0].composite_score


def test_score_placements_guards_invalid_inputs():
    assert score_placements(
        spot=0, em_pct=1.5, hold_days=5, dte_calendar_days=5,
        historical_events=[], em_mults=[1.0], wing_pts=[10.0],
    ) == []


def test_build_wing_console_publishes_scoring_context():
    clear_scoring_cache()
    closes, wins, ohlc = _synthetic()
    payload, mae, mc = build_wing_console(
        entry_date="2024-06-03", expiry_date="2024-06-07",
        as_of_date="2024-06-03",
        spot=5000.0, em_pct=1.5, hold_days=5, dte_calendar_days=4,
        analogue_pool=wins, closes_by_date=closes, ohlc_by_date=ohlc,
        regime_label="MODERATE", regime_bucket="MODERATE",
        regime_mi_v2={"label": "Transitional", "probabilities": {"Transitional": 0.6}},
        macro_bucket="NORMAL",
    )
    assert payload.n_analogues == len(wins)
    assert len(payload.placements) > 0
    ctx = get_scoring_context("2024-06-03", "2024-06-07", "2024-06-03")
    assert isinstance(ctx, ScoringContext)
    assert ctx.spot == 5000.0
    assert ctx.em_pct == 1.5


def test_score_single_placement_against_cached_context():
    clear_scoring_cache()
    closes, wins, _ohlc = _synthetic()
    ctx = ScoringContext(
        entry_date="2024-06-03", expiry_date="2024-06-07", as_of_date="2024-06-03",
        spot=5000.0, em_pct=1.5, hold_days=5,
        analogue_pool=wins, closes_by_date=closes,
        mae_dist={"n": 20, "p95": 1.8, "source": "daily_ohlc"},
        regime_bucket="MODERATE", macro_bucket="NORMAL",
        weights=DEFAULT_WEIGHTS.as_dict(),
    )
    store_scoring_context(ctx)
    retrieved = get_scoring_context("2024-06-03", "2024-06-07", "2024-06-03")
    assert retrieved is ctx
    placement = score_single_placement(
        context=retrieved, em_mult=1.35, wing_pts=12.5,
    )
    assert placement.em_mult == pytest.approx(1.35, abs=1e-4)
    assert placement.wing_pts == pytest.approx(12.5, abs=1e-3)
    assert 0.0 <= placement.composite_score <= 100.0


def test_weights_from_flags_match_defaults():
    from backend.config import get_flags
    flags = get_flags()
    w = WingConsoleWeights.from_flags(flags)
    assert w.close == DEFAULT_WEIGHTS.close
    assert w.touch == DEFAULT_WEIGHTS.touch
    assert w.mae == DEFAULT_WEIGHTS.mae
