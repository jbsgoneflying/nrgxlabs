"""Tests for the Phase 1 module 5 path generator."""

from __future__ import annotations

import json
import math
import random
from typing import Any

import pytest
from fastapi.testclient import TestClient

from v2_app.foundation.paths import (
    PathSampler,
    _bootstrap_proportion_ci,
    _quantile,
    regime_weights_from_neighbors,
)


# ── Quantile helper ───────────────────────────────────────


def test_quantile_simple_cases() -> None:
    assert _quantile([1.0], 0.5) == 1.0
    assert _quantile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    assert _quantile([1.0, 2.0, 3.0, 4.0, 5.0], 0.0) == 1.0
    assert _quantile([1.0, 2.0, 3.0, 4.0, 5.0], 1.0) == 5.0


def test_quantile_unsorted_input() -> None:
    # Same data, scrambled — should produce the same answer.
    assert _quantile([5, 1, 4, 2, 3], 0.5) == 3.0


def test_quantile_interpolates() -> None:
    # 0.25 of 4 elements → between index 0 and 1 by 0.75
    val = _quantile([10.0, 20.0, 30.0, 40.0], 0.25)
    assert math.isclose(val, 17.5)


# ── Path sampler ──────────────────────────────────────────


def _gaussian_returns(n: int = 250, mu: float = 0.0, sigma: float = 0.01,
                      seed: int = 13) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mu, sigma) for _ in range(n)]


def test_path_sampler_warmup_threshold() -> None:
    sampler = PathSampler(returns=_gaussian_returns(n=10))
    assert sampler.is_warm() is False
    with pytest.raises(RuntimeError):
        sampler.sample_paths(n_samples=10, horizon_days=5)


def test_path_sampler_strips_nan_inf() -> None:
    bad = _gaussian_returns(n=50) + [float("nan"), float("inf"), -float("inf")]
    sampler = PathSampler(returns=bad)
    assert sampler.n_returns == 50


def test_path_sampler_deterministic_with_seed() -> None:
    returns = _gaussian_returns(n=100)
    s1 = PathSampler(returns=returns, rng=random.Random(42))
    s2 = PathSampler(returns=returns, rng=random.Random(42))
    p1 = s1.sample_paths(n_samples=20, horizon_days=10)
    p2 = s2.sample_paths(n_samples=20, horizon_days=10)
    assert p1 == p2


def test_path_sampler_paths_have_correct_shape() -> None:
    returns = _gaussian_returns(n=100)
    sampler = PathSampler(returns=returns)
    paths = sampler.sample_paths(n_samples=50, horizon_days=21)
    assert len(paths) == 50
    assert all(len(p) == 21 for p in paths)


def test_path_sampler_terminal_mean_recovers_drift() -> None:
    """A high-mean return corpus should produce positive-mean terminal returns."""
    returns = _gaussian_returns(n=500, mu=0.005, sigma=0.01, seed=7)
    sampler = PathSampler(returns=returns, rng=random.Random(7))
    res = sampler.breach_probability(
        lower_threshold=-0.10, upper_threshold=0.10,
        n_samples=2000, horizon_days=21,
    )
    assert res.path_stats.terminal_mean > 0.05


def test_breach_probability_obeys_bracket_width() -> None:
    """A narrower bracket must produce a higher breach probability."""
    returns = _gaussian_returns(n=500, sigma=0.02, seed=99)
    sampler = PathSampler(returns=returns, rng=random.Random(99))
    narrow = sampler.breach_probability(
        lower_threshold=-0.02, upper_threshold=0.02,
        n_samples=1500, horizon_days=21, bootstrap_ci_resamples=50,
    )
    sampler2 = PathSampler(returns=returns, rng=random.Random(99))
    wide = sampler2.breach_probability(
        lower_threshold=-0.20, upper_threshold=0.20,
        n_samples=1500, horizon_days=21, bootstrap_ci_resamples=50,
    )
    assert narrow.p_breach > wide.p_breach


def test_breach_probability_one_sided() -> None:
    returns = _gaussian_returns(n=500, sigma=0.01, seed=3)
    sampler = PathSampler(returns=returns, rng=random.Random(3))
    one_sided = sampler.breach_probability(
        lower_threshold=None, upper_threshold=0.05,
        n_samples=1500, horizon_days=21, bootstrap_ci_resamples=50,
    )
    assert one_sided.p_lower_breach == 0.0
    assert one_sided.p_upper_breach == one_sided.p_breach


def test_breach_probability_requires_at_least_one_threshold() -> None:
    sampler = PathSampler(returns=_gaussian_returns(n=100))
    with pytest.raises(ValueError):
        sampler.breach_probability(
            lower_threshold=None, upper_threshold=None,
            n_samples=100, horizon_days=5,
        )


def test_weighted_sampler_concentrates_on_high_weight_returns() -> None:
    """If we weight only the positive returns heavily, terminal mean shifts up."""
    returns = [-0.02] * 50 + [0.02] * 50  # symmetric corpus
    weights = [0.0] * 50 + [1.0] * 50     # only positive half
    sampler = PathSampler(
        returns=returns,
        weights=weights,
        rng=random.Random(11),
    )
    res = sampler.breach_probability(
        lower_threshold=-0.50, upper_threshold=0.50,
        n_samples=1000, horizon_days=10, bootstrap_ci_resamples=50,
    )
    # Strict positive drift → terminal mean must be solidly positive.
    assert res.path_stats.terminal_mean > 0.15


def test_weighted_sampler_falls_back_to_uniform_when_all_zero() -> None:
    sampler = PathSampler(
        returns=_gaussian_returns(n=100),
        weights=[0.0] * 100,
    )
    assert sampler.weights is None


# ── Bootstrap CI ──────────────────────────────────────────


def test_bootstrap_ci_brackets_point_estimate() -> None:
    flags = [True] * 30 + [False] * 70  # 30% true rate
    rng = random.Random(0)
    lo, hi = _bootstrap_proportion_ci(flags, n_resamples=400, rng=rng)
    assert lo <= 0.30 <= hi
    assert hi - lo < 0.30  # not absurdly wide


def test_bootstrap_ci_handles_empty() -> None:
    rng = random.Random(0)
    assert _bootstrap_proportion_ci([], n_resamples=100, rng=rng) == (0.0, 0.0)


# ── regime_weights_from_neighbors ─────────────────────────


def test_regime_weights_aligns_to_dates() -> None:
    neighbors = [
        {"date": "2025-01-02", "similarity": 0.9},
        {"date": "2025-01-04", "similarity": 0.7},
        {"date": "2025-01-09", "similarity": 0.55},
    ]
    return_dates = ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-05"]
    weights = regime_weights_from_neighbors(neighbors, return_dates=return_dates)
    assert weights == [0.0, 0.9, 0.0, 0.7, 0.0]


def test_regime_weights_skips_malformed_neighbors() -> None:
    neighbors = [
        {"date": "2025-01-02", "similarity": 0.9},
        {"similarity": 0.5},                       # no date
        {"date": "2025-01-04"},                    # no similarity
        {"date": "2025-01-05", "similarity": "not-a-number"},
    ]
    return_dates = ["2025-01-02", "2025-01-04", "2025-01-05"]
    weights = regime_weights_from_neighbors(neighbors, return_dates=return_dates)
    assert weights == [0.9, 0.0, 0.0]


def test_regime_weights_clamps_negative_similarity_to_zero() -> None:
    neighbors = [{"date": "2025-01-02", "similarity": -0.4}]
    return_dates = ["2025-01-02"]
    weights = regime_weights_from_neighbors(neighbors, return_dates=return_dates)
    assert weights == [0.0]


# ── Endpoint contracts ─────────────────────────────────────


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.kv.get(key)

    def set(self, key: str, value: str) -> None:
        self.kv[key] = value


@pytest.fixture
def patched_client(monkeypatch: pytest.MonkeyPatch):
    from v2_app import main as v2_main
    from v2_app.foundation import paths_store

    fake = FakeRedis()
    monkeypatch.setattr(paths_store, "_redis_client", lambda: fake)

    return TestClient(v2_main.app), fake


def test_corpus_save_and_list_round_trip(patched_client) -> None:
    client, _ = patched_client
    rows = [
        {"date": f"2025-01-{i:02d}", "log_return": (i - 25) / 1000.0}
        for i in range(1, 51)
    ]
    r = client.post(
        "/api/v2/paths/corpus/save",
        json={"ticker": "spx", "rows": rows},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["ticker"] == "SPX"
    assert body["n"] == 50

    r = client.get("/api/v2/paths/corpus/list")
    assert r.status_code == 200
    body = r.json()
    assert body["n_total"] == 50
    assert any(t["ticker"] == "SPX" for t in body["tickers"])


def test_sample_endpoint_inline_returns(patched_client) -> None:
    client, _ = patched_client
    returns = _gaussian_returns(n=100, sigma=0.012)
    r = client.post(
        "/api/v2/paths/sample",
        json={"returns": returns, "n_samples": 500, "horizon_days": 10, "seed": 5},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["regime_conditional"] is False
    assert body["terminal_stats"]["n_samples"] == 500


def test_sample_requires_corpus_or_returns(patched_client) -> None:
    client, _ = patched_client
    r = client.post("/api/v2/paths/sample", json={"n_samples": 100, "horizon_days": 5})
    assert r.status_code == 422


def test_breach_prob_with_corpus_lookup(patched_client) -> None:
    client, _ = patched_client
    rows = [
        {"date": f"2025-01-{i:02d}", "log_return": v}
        for i, v in enumerate(_gaussian_returns(n=120, sigma=0.015), start=1)
    ]
    client.post(
        "/api/v2/paths/corpus/save",
        json={"ticker": "spx", "rows": rows},
    )
    r = client.post(
        "/api/v2/paths/breach-prob",
        json={
            "ticker": "SPX",
            "lower_threshold": -0.05,
            "upper_threshold": 0.05,
            "n_samples": 800,
            "horizon_days": 21,
            "bootstrap_ci_resamples": 50,
            "seed": 7,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert 0.0 <= body["p_breach"] <= 1.0
    assert body["p_breach_interval"][0] <= body["p_breach"] <= body["p_breach_interval"][1]


def test_breach_prob_regime_conditional_paths(patched_client) -> None:
    client, _ = patched_client
    # Build a corpus: half negative, half positive, distinct dates.
    rows = []
    for i in range(60):
        rows.append({"date": f"2025-01-{i+1:02d}", "log_return": -0.02})
    for i in range(60):
        rows.append({"date": f"2025-02-{i+1:02d}", "log_return": +0.02})
    client.post("/api/v2/paths/corpus/save", json={"ticker": "demo", "rows": rows})

    # Regime-condition on the positive-return days only.
    neighbors = [
        {"date": f"2025-02-{i+1:02d}", "similarity": 0.9}
        for i in range(20)
    ]
    r = client.post(
        "/api/v2/paths/breach-prob",
        json={
            "ticker": "DEMO",
            "regime_neighbors": neighbors,
            "lower_threshold": -0.50,
            "upper_threshold": 0.50,
            "n_samples": 600,
            "horizon_days": 10,
            "bootstrap_ci_resamples": 50,
            "seed": 11,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["regime_conditional"] is True
    # All sampled returns are +0.02 → terminal must be sharply positive.
    assert body["path_stats"]["terminal_mean"] > 0.15


def test_breach_prob_validates_thresholds(patched_client) -> None:
    client, _ = patched_client
    returns = _gaussian_returns(n=100)
    r = client.post(
        "/api/v2/paths/breach-prob",
        json={"returns": returns, "n_samples": 100, "horizon_days": 5},
    )
    assert r.status_code == 422


def test_stats_endpoint_when_empty(patched_client) -> None:
    client, _ = patched_client
    r = client.get("/api/v2/paths/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["n_total"] == 0


def test_breach_prob_returns_404_for_missing_ticker(patched_client) -> None:
    client, _ = patched_client
    r = client.post(
        "/api/v2/paths/breach-prob",
        json={
            "ticker": "MISSING",
            "lower_threshold": -0.05,
            "upper_threshold": 0.05,
            "n_samples": 100,
            "horizon_days": 5,
        },
    )
    assert r.status_code == 404


def test_path_generator_flag_in_version(patched_client) -> None:
    client, _ = patched_client
    r = client.get("/api/v2/version")
    assert r.status_code == 200
    body = r.json()
    assert body["foundation"]["path_generator"] is True
