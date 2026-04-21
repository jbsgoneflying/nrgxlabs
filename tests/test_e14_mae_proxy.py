"""Engine 14 v2 — intraweek MAE proxy tests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from backend.engine14.mae_proxy import (
    MAEDistribution,
    compute_mae_distribution,
    mae_p95_vs_wing_ratio,
)


@dataclass
class _Bar:
    open:  Optional[float]
    high:  Optional[float]
    low:   Optional[float]
    close: Optional[float]


def test_compute_mae_distribution_empty_returns_zero_n():
    dist = compute_mae_distribution(windows=[], bars_by_date={})
    assert isinstance(dist, MAEDistribution)
    assert dist.n == 0
    assert "mae_pool_empty" in dist.notes[0]


def test_compute_mae_distribution_picks_worst_excursion():
    windows = [{"entry_date": "2026-01-06", "expiry_date": "2026-01-10",
                "entry_close": 5000.0}]
    bars_by_date = {
        "2026-01-07": _Bar(open=5000, high=5050, low=4970, close=5010),
        "2026-01-08": _Bar(open=5010, high=5250, low=5000, close=5200),
        "2026-01-09": _Bar(open=5200, high=5210, low=4800, close=4900),
        "2026-01-10": _Bar(open=4900, high=4950, low=4870, close=4910),
    }
    dist = compute_mae_distribution(windows=windows, bars_by_date=bars_by_date)
    assert dist.n == 1
    # worst is 4800 (down 4%) vs 5250 (up 5%) -> up wins
    assert dist.p95 >= 4.99
    assert dist.source == "daily_ohlc"


def test_compute_mae_distribution_falls_back_without_high_low():
    windows = [{"entry_date": "2026-01-06", "expiry_date": "2026-01-10",
                "entry_close": 100.0}]
    bars_by_date = {
        "2026-01-07": _Bar(open=100, high=None, low=None, close=103),
        "2026-01-08": _Bar(open=100, high=None, low=None, close=101),
    }
    dist = compute_mae_distribution(windows=windows, bars_by_date=bars_by_date)
    assert dist.n == 1
    assert dist.source == "open_close_fallback"
    assert dist.p95 >= 2.99


def test_compute_mae_distribution_percentiles_monotone():
    windows = []
    bars_by_date = {}
    moves = [1.0, 2.0, 3.0, 4.0, 5.0]
    for i, m in enumerate(moves):
        ed = f"2026-01-0{i + 1}"
        xp = f"2026-01-0{i + 2}"
        windows.append({"entry_date": ed, "expiry_date": xp, "entry_close": 100.0})
        bars_by_date[xp] = _Bar(
            open=100, high=100 * (1 + m / 100), low=99, close=100,
        )
    dist = compute_mae_distribution(windows=windows, bars_by_date=bars_by_date)
    assert dist.n == 5
    assert dist.p50 <= dist.p75 <= dist.p90 <= dist.p95 <= dist.max


def test_mae_p95_vs_wing_ratio_penalizes_deep_moves():
    # Spot 5000, EM 1.5%, em_mult 1.0 -> short at 1.5% move from 5000 = 75pt.
    # p95 MAE 3% = 150pt from spot; 75pt past short. Wing 15pt -> ratio clamps at 1.5.
    ratio = mae_p95_vs_wing_ratio(
        mae_p95_pct=3.0, em_multiple=1.0, implied_move_pct=1.5,
        wing_width_pts=15.0, spot=5000.0,
    )
    assert 1.49 <= ratio <= 1.5


def test_mae_p95_vs_wing_ratio_zero_when_inside_shorts():
    ratio = mae_p95_vs_wing_ratio(
        mae_p95_pct=1.0, em_multiple=1.0, implied_move_pct=1.5,
        wing_width_pts=15.0, spot=5000.0,
    )
    assert ratio == 0.0


def test_mae_p95_vs_wing_ratio_guards_invalid_inputs():
    for bad in [
        dict(mae_p95_pct=1.0, em_multiple=0, implied_move_pct=1.5, wing_width_pts=15, spot=5000),
        dict(mae_p95_pct=1.0, em_multiple=1, implied_move_pct=0,   wing_width_pts=15, spot=5000),
        dict(mae_p95_pct=1.0, em_multiple=1, implied_move_pct=1.5, wing_width_pts=0,  spot=5000),
        dict(mae_p95_pct=1.0, em_multiple=1, implied_move_pct=1.5, wing_width_pts=15, spot=0),
    ]:
        assert mae_p95_vs_wing_ratio(**bad) == 0.0
