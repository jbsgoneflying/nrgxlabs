"""Unit tests for the dealer-gamma history factor (Market Intelligence v2).

Covers the pure logic that does not need ORATS/EODHD credentials: the raw
stress signal + sign convention, series alignment/z, the model-capability
detector that auto-gates live injection, and the validation date matcher.
"""
from __future__ import annotations

import os

import pytest

from backend.market_intel import gamma_history as gh
from backend.market_intel.factors import FACTOR_KEYS


def _chain(spot: float, call_oi: float, put_oi: float, gamma: float = 0.05):
    """Synthetic near-spot strikes payload (3 strikes around spot)."""
    rows = []
    for k in (spot * 0.99, spot, spot * 1.01):
        rows.append({
            "strike": k,
            "spotPrice": spot,
            "gamma": gamma,
            "callOpenInterest": call_oi,
            "putOpenInterest": put_oi,
        })
    return rows


class TestStressRawSignal:
    def test_puts_dominate_is_positive_stress(self):
        # Net negative dealer gamma (puts >> calls) → stress_raw > 0.
        v = gh.stress_raw_from_rows(_chain(100.0, call_oi=100, put_oi=900))
        assert v is not None and v > 0

    def test_calls_dominate_is_negative_stress(self):
        # Net positive dealer gamma (calls >> puts) → stress_raw < 0.
        v = gh.stress_raw_from_rows(_chain(100.0, call_oi=900, put_oi=100))
        assert v is not None and v < 0

    def test_balanced_is_near_zero(self):
        v = gh.stress_raw_from_rows(_chain(100.0, call_oi=500, put_oi=500))
        assert v is not None and abs(v) < 1e-6

    def test_empty_rows_returns_none(self):
        assert gh.stress_raw_from_rows([]) is None


class TestSeriesAlignment:
    def test_forward_fill_and_leading_zero(self):
        series = {"2024-01-03": 0.5, "2024-01-05": -0.2}
        dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
        out = gh.aligned_stress_series(series, dates)
        # Leading day before first obs → 0.0; gaps carry last; tail carries last.
        assert out == [0.0, 0.5, 0.5, -0.2, -0.2]

    def test_z_series_is_zero_without_enough_history(self):
        series = {f"2024-01-{d:02d}": 0.1 * d for d in range(1, 10)}
        dates = sorted(series.keys())
        z = gh.dealer_z_series(series, dates)
        # < MIN_Z_OBSERVATIONS points → rolling z is 0 everywhere.
        assert all(abs(x) < 1e-9 for x in z)

    def test_z_series_reacts_to_a_spike(self):
        # 80 calm days then a big spike — final z should be strongly positive.
        series = {}
        for i in range(80):
            series[f"d{i:03d}"] = 0.0 if i % 2 == 0 else 0.001
        series["d080"] = 5.0
        dates = sorted(series.keys())
        z = gh.dealer_z_series(series, dates)
        assert z[-1] > 2.0


class _FakeModel:
    def __init__(self, means, stds):
        self.emission_means = means
        self.emission_stds = stds
        self.n_states = len(means)


def _row(dealer_val: float) -> list:
    r = [0.0] * len(FACTOR_KEYS)
    r[gh.DEALER_GAMMA_IDX] = dealer_val
    return r


class TestModelCapabilityDetector:
    def test_zero_trained_placeholder_is_not_used(self):
        # Means all 0, stds at the variance floor (~0.0316): inert column.
        means = [_row(0.0) for _ in range(3)]
        stds = [[0.0316] * len(FACTOR_KEYS) for _ in range(3)]
        assert gh.model_uses_dealer_gamma(_FakeModel(means, stds)) is False

    def test_real_feature_is_detected_via_mean_spread(self):
        means = [_row(-0.8), _row(0.0), _row(1.2)]
        stds = [[0.0316] * len(FACTOR_KEYS) for _ in range(3)]
        assert gh.model_uses_dealer_gamma(_FakeModel(means, stds)) is True

    def test_real_feature_is_detected_via_std(self):
        means = [_row(0.0) for _ in range(3)]
        stds = [[0.0316] * len(FACTOR_KEYS) for _ in range(3)]
        for s in range(3):
            stds[s][gh.DEALER_GAMMA_IDX] = 0.9
        assert gh.model_uses_dealer_gamma(_FakeModel(means, stds)) is True

    def test_malformed_model_is_safe(self):
        assert gh.model_uses_dealer_gamma(object()) is False


class TestPersistenceRoundtrip:
    def test_save_load_upsert(self, tmp_path, monkeypatch):
        path = tmp_path / "raw.json"
        monkeypatch.setenv("MI_DEALER_GAMMA_RAW_PATH", str(path))
        gh.save_raw_series({"2024-01-02": 0.3}, store=None)
        loaded = gh.load_raw_series(store=None)
        assert loaded["2024-01-02"] == pytest.approx(0.3)
        gh.upsert_raw_point("2024-01-03", -0.4, store=None)
        loaded2 = gh.load_raw_series(store=None)
        assert loaded2["2024-01-03"] == pytest.approx(-0.4)
        assert loaded2["2024-01-02"] == pytest.approx(0.3)


class TestNearestDateIndex:
    def test_exact_and_near_match(self):
        from backend.market_intel.calibration import _nearest_date_index
        dates = ["2022-09-28", "2022-09-29", "2022-09-30", "2022-10-03"]
        assert _nearest_date_index(dates, "2022-09-30") == 2
        # 2022-10-01 is a weekend; nearest within 10d is 2022-09-30 or 10-03.
        assert _nearest_date_index(dates, "2022-10-01") in (2, 3)

    def test_far_target_returns_none(self):
        from backend.market_intel.calibration import _nearest_date_index
        dates = ["2024-01-02", "2024-01-03"]
        assert _nearest_date_index(dates, "2020-03-16") is None
