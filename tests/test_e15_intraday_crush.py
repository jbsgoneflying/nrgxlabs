"""Engine 15 v2 — empirical intraday-crush estimator tests."""
from __future__ import annotations

import pytest

from backend.engine15.intraday_crush import CrushReading, compute_crush_factor


class _Path:
    """Minimal stand-in for engine14.simulator.AnaloguePath."""
    def __init__(self, daily):
        self.daily_pnl_pct = daily


def test_empty_pool_returns_fixed_fallback():
    r = compute_crush_factor(paths=[], fallback=0.80)
    assert r.source == "fixed"
    assert r.factor == pytest.approx(0.80)
    assert r.n_events == 0


def test_thin_sample_falls_back_to_fixed():
    p = _Path([(7, 5.0), (0, 10.0)])
    r = compute_crush_factor(paths=[p, p], fallback=0.80, min_sample=3)
    assert r.source == "fixed"
    assert "sample" in r.fallback_reason


def test_full_crush_factor_empirical():
    # Entry-day PnL = 10%, close PnL = 20% => ratio 0.5
    paths = [_Path([(7, 10.0), (0, 20.0)]) for _ in range(5)]
    r = compute_crush_factor(paths=paths, fallback=0.80, min_sample=3)
    assert r.source == "empirical"
    assert r.factor == pytest.approx(0.5, abs=0.01)
    assert r.n_events == 5


def test_crush_factor_clips_to_valid_range():
    # A pathological event with entry_pnl = 50%, close = 10% -> ratio 5.0
    # gets clipped to 1.2 upper bound.
    paths = [_Path([(7, 50.0), (0, 10.0)])] * 5
    r = compute_crush_factor(paths=paths, fallback=0.80, min_sample=3)
    assert r.factor <= 1.21


def test_paths_with_flat_exit_skipped():
    # |exit_pnl| < 5% is below noise floor, should be skipped.
    p_flat = _Path([(7, 1.0), (0, 2.0)])
    p_real = _Path([(7, 5.0), (0, 10.0)])
    r = compute_crush_factor(paths=[p_flat, p_flat, p_real, p_real, p_real], fallback=0.80, min_sample=3)
    # Only 3 events contributed (the flat ones dropped).
    assert r.n_events == 3


def test_interquartile_range_populated():
    paths = [
        _Path([(7, 4.0),  (0, 20.0)]),   # 0.2
        _Path([(7, 6.0),  (0, 20.0)]),   # 0.3
        _Path([(7, 10.0), (0, 20.0)]),   # 0.5
        _Path([(7, 14.0), (0, 20.0)]),   # 0.7
        _Path([(7, 18.0), (0, 20.0)]),   # 0.9
    ]
    r = compute_crush_factor(paths=paths, fallback=0.80, min_sample=3)
    assert r.p25 is not None
    assert r.p75 is not None
    assert r.p25 < r.p50 < r.p75


def test_to_dict_shape():
    paths = [_Path([(7, 10.0), (0, 20.0)]) for _ in range(5)]
    r = compute_crush_factor(paths=paths)
    d = r.to_dict()
    assert "factor" in d and "source" in d and "n_events" in d
    assert "p25" in d and "p75" in d
    assert "notes" in d and isinstance(d["notes"], list)
