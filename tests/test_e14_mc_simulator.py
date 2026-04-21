"""Engine 14 v2 — forward MC simulator (thin wrapper tests)."""
from __future__ import annotations

import datetime as dt
import random

from backend.engine14.mc_simulator import (
    MCResult,
    build_mc_pool,
    run_forward_mc,
)


def _synthetic_closes(n_weeks: int = 30, rng_seed: int = 42):
    rng = random.Random(rng_seed)
    start = dt.date(2024, 1, 2)
    closes = {}
    spot = 5000.0
    d = start
    while len(closes) < n_weeks * 5:
        if d.weekday() < 5:
            spot *= (1.0 + rng.gauss(0, 0.008))
            closes[d.isoformat()] = spot
        d += dt.timedelta(days=1)
    return closes


def _windows_from_closes(closes):
    dates = sorted(closes.keys())
    wins = []
    for i in range(0, len(dates) - 5, 5):
        wins.append({
            "entry_date":    dates[i],
            "expiry_date":   dates[i + 4],
            "regime_bucket": "MODERATE",
            "macro_bucket":  "NORMAL",
        })
    return wins


def test_build_mc_pool_emits_daily_returns():
    closes = _synthetic_closes(10)
    wins = _windows_from_closes(closes)
    pool = build_mc_pool(windows=wins, closes_by_date=closes, hold_days=5)
    assert len(pool) == len(wins)
    for row in pool:
        assert row["daily_returns"]
        assert len(row["daily_returns"]) == 5
        assert row["regime_bucket"] == "MODERATE"
        assert row["macro_bucket"] == "NORMAL"


def test_run_forward_mc_returns_placement_stats():
    closes = _synthetic_closes(50)
    wins = _windows_from_closes(closes)
    r = run_forward_mc(
        ticker="SPX", as_of_date="2026-04-21",
        spot=5000.0, em_pct=1.5, hold_days=5,
        analogue_windows=wins, closes_by_date=closes,
        placements=[(1.0, 10.0), (1.5, 10.0), (2.0, 10.0)],
        n_sims=1000, min_pool=10,
        want_regime_bucket="MODERATE", want_macro_bucket="NORMAL",
    )
    assert isinstance(r, MCResult)
    assert r.n_sims == 1000
    assert r.mode == "bootstrap"
    assert len(r.placements) == 3
    breaches = [p.breach_close_prob for p in r.placements]
    assert breaches[0] >= breaches[1] >= breaches[2]


def test_run_forward_mc_deterministic():
    closes = _synthetic_closes(40)
    wins = _windows_from_closes(closes)
    kw = dict(
        ticker="SPX", as_of_date="2026-04-21",
        spot=5000.0, em_pct=1.5, hold_days=5,
        analogue_windows=wins, closes_by_date=closes,
        placements=[(1.25, 10.0)],
        n_sims=500, min_pool=10,
        want_regime_bucket="MODERATE", want_macro_bucket="NORMAL",
    )
    r1 = run_forward_mc(**kw)
    r2 = run_forward_mc(**kw)
    assert r1.seed == r2.seed
    for a, b in zip(r1.placements, r2.placements):
        assert a.breach_close_prob == b.breach_close_prob


def test_run_forward_mc_empty_pool_unavailable():
    r = run_forward_mc(
        ticker="SPX", as_of_date="2026-04-21",
        spot=5000.0, em_pct=1.5, hold_days=5,
        analogue_windows=[], closes_by_date={},
        placements=[(1.0, 10.0)], n_sims=100,
    )
    assert r.mode == "unavailable"
    assert r.n_sims == 0
