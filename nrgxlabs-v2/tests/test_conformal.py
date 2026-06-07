"""Tests for the split-conformal calibrator (Phase 1 module 1).

Two layers:

1. Pure-Python math correctness — verify that on synthetic data drawn from
   a known noise distribution, the empirical coverage of the produced
   intervals meets or exceeds the nominal ``1 - α`` target. This is the
   one property that distinguishes "real" conformal prediction from
   bootstrap CIs and is the reason we are shipping this module.

2. HTTP endpoints — the router contracts the v2 frontend and v1 mirror
   will depend on.
"""

from __future__ import annotations

import os
import random

import pytest
from fastapi.testclient import TestClient

from v2_app.foundation.conformal import (
    SplitConformalCalibrator,
    empirical_coverage,
    nonconformity,
    quantile_with_finite_sample_correction,
)


# ── Math correctness ──────────────────────────────────────


def test_nonconformity_is_absolute_residual() -> None:
    assert nonconformity(0.5, 0.7) == pytest.approx(0.2)
    assert nonconformity(1.0, 0.0) == pytest.approx(1.0)
    assert nonconformity(0.3, 0.3) == pytest.approx(0.0)


def test_quantile_finite_sample_correction_matches_textbook() -> None:
    # n=10, alpha=0.1 → ceil((10+1)*0.9) = 10 → the largest score.
    scores = [0.05, 0.07, 0.09, 0.10, 0.11, 0.12, 0.14, 0.18, 0.20, 0.30]
    q = quantile_with_finite_sample_correction(scores, alpha=0.1)
    assert q == pytest.approx(0.30)

    # n=20, alpha=0.1 → ceil(21*0.9) = 19 → the 19th score.
    scores2 = [i / 100.0 for i in range(1, 21)]  # 0.01..0.20
    q2 = quantile_with_finite_sample_correction(scores2, alpha=0.1)
    assert q2 == pytest.approx(0.19)


def test_quantile_rejects_invalid_alpha() -> None:
    with pytest.raises(ValueError):
        quantile_with_finite_sample_correction([0.1, 0.2], alpha=0.0)
    with pytest.raises(ValueError):
        quantile_with_finite_sample_correction([0.1, 0.2], alpha=1.0)


def test_empirical_coverage_guarantee_on_synthetic_normal_residuals() -> None:
    """The headline property: marginal coverage ≥ 1 - α on exchangeable data.

    We draw 600 samples where the residual is iid Normal(0, 0.05). Because
    nonconformity = |residual|, the LOO empirical coverage at α=0.1 should
    be ≥ 0.9 (with finite-sample correction it is typically a touch above).
    """
    rng = random.Random(1729)
    cal = SplitConformalCalibrator(bound=(0.0, 1.0), buf_size=600)
    for _ in range(600):
        # Anchor predictions inside [0.1, 0.9] so the bounded interval doesn't
        # get truncated; we want to test the math, not the clipping.
        prediction = rng.uniform(0.2, 0.8)
        residual = rng.gauss(0.0, 0.05)
        realized = max(0.0, min(1.0, prediction + residual))
        cal.observe(prediction=prediction, realized=realized)

    cov = cal.empirical_coverage(alpha=0.1)
    assert cov >= 0.88, f"empirical coverage {cov:.3f} below 1-α target"
    # Also sanity-check that with α=0.2 coverage drops accordingly.
    cov80 = cal.empirical_coverage(alpha=0.2)
    assert cov80 >= 0.78
    assert cov80 < cov


def test_empirical_coverage_helper_handles_thin_samples() -> None:
    import math
    assert math.isnan(empirical_coverage([], alpha=0.1))
    assert math.isnan(empirical_coverage([0.1], alpha=0.1))


def test_calibrator_warmup_returns_wide_interval() -> None:
    cal = SplitConformalCalibrator(bound=(0.0, 1.0))
    cal.observe(prediction=0.5, realized=0.5)
    ci = cal.interval(prediction=0.5, alpha=0.1)
    assert ci.warmup is True
    # Bound range is 1.0 → warm-up half-width = 0.25.
    assert ci.lower == pytest.approx(0.25)
    assert ci.upper == pytest.approx(0.75)


def test_calibrator_clips_to_bounds() -> None:
    cal = SplitConformalCalibrator(bound=(0.0, 1.0))
    # Seed with residuals of magnitude ~0.20 so the conformal quantile is
    # large enough that an interval near the bounds gets genuinely clipped.
    rng = random.Random(8675309)
    for _ in range(60):
        pred = rng.uniform(0.4, 0.6)
        realized = max(0.0, min(1.0, pred + rng.choice([-1, 1]) * 0.20))
        cal.observe(prediction=pred, realized=realized)

    # Prediction near the lower bound — interval would extend below 0.
    ci = cal.interval(prediction=0.05, alpha=0.1)
    assert ci.lower == 0.0  # clipped
    assert ci.upper > 0.0

    # Prediction near the upper bound — interval would extend past 1.
    ci = cal.interval(prediction=0.98, alpha=0.1)
    assert ci.upper == 1.0  # clipped
    assert ci.lower < 1.0


def test_calibrator_unbounded_metric() -> None:
    cal = SplitConformalCalibrator(bound=(None, None), buf_size=200)
    rng = random.Random(31337)
    for _ in range(200):
        pred = rng.uniform(50, 200)
        realized = pred + rng.gauss(0.0, 5.0)
        cal.observe(prediction=pred, realized=realized)
    ci = cal.interval(prediction=120.0, alpha=0.1)
    assert ci.warmup is False
    # Roughly ±~10 (ish) for a normal with σ=5 at 90% coverage.
    assert 5.0 < ci.upper - ci.point < 25.0
    assert ci.bound_lo is None and ci.bound_hi is None


def test_buf_size_caps_window() -> None:
    cal = SplitConformalCalibrator(bound=(0.0, 1.0), buf_size=50)
    for _ in range(120):
        cal.observe(prediction=0.5, realized=0.5)
    assert cal.state.n == 50  # newest 50 only


# ── HTTP endpoint contract ───────────────────────────────


@pytest.fixture(scope="module")
def client() -> TestClient:
    os.environ["PUBLIC_ACCESS"] = "1"
    os.environ.setdefault("AUTH_SECRET", "test-secret-not-real")
    from v2_app.main import app
    return TestClient(app)


def _stub_redis(monkeypatch) -> dict[str, str]:
    """Replace the conformal store's Redis client with an in-memory dict."""
    store: dict[str, str] = {}
    index: set[str] = set()

    class FakeRedis:
        def get(self, key):
            return store.get(key)

        def set(self, key, value):
            store[key] = value

        def sadd(self, key, member):
            index.add(member)

        def smembers(self, key):
            return set(index)

    from v2_app.foundation import conformal_store
    monkeypatch.setattr(conformal_store, "_redis_client", lambda: FakeRedis())
    return store


def test_endpoint_observe_then_interval_then_coverage(client: TestClient, monkeypatch) -> None:
    _stub_redis(monkeypatch)

    rng = random.Random(101)
    for _ in range(80):
        pred = rng.uniform(0.2, 0.6)
        realized = max(0.0, min(1.0, pred + rng.gauss(0.0, 0.04)))
        res = client.post(
            "/api/v2/conformal/observe",
            json={
                "engine": "e14",
                "metric": "breach_probability",
                "prediction": pred,
                "realized": realized,
            },
        )
        assert res.status_code == 200, res.text

    # Last observe response should report warmed_up.
    last = res.json()
    assert last["n"] == 80
    assert last["warmed_up"] is True
    assert last["persisted"] is True

    # Interval at a fresh prediction.
    res2 = client.post(
        "/api/v2/conformal/interval",
        json={"engine": "e14", "metric": "breach_probability", "prediction": 0.30, "alpha": 0.1},
    )
    assert res2.status_code == 200
    body = res2.json()
    assert body["warmup"] is False
    assert body["coverage_target"] == pytest.approx(0.9)
    assert body["n_calibration"] == 80
    assert body["bound"] == [0.0, 1.0]
    assert 0.0 <= body["lower"] <= body["point"] <= body["upper"] <= 1.0

    # Coverage diagnostic.
    res3 = client.get("/api/v2/conformal/coverage", params={
        "engine": "e14", "metric": "breach_probability", "alpha": 0.1,
    })
    assert res3.status_code == 200
    cov = res3.json()
    assert cov["n"] == 80
    assert cov["empirical_coverage"] >= 0.85
    assert cov["target_coverage"] == pytest.approx(0.9)


def test_endpoint_coverage_rejects_thin_calibrator(client: TestClient, monkeypatch) -> None:
    _stub_redis(monkeypatch)
    res = client.get("/api/v2/conformal/coverage", params={
        "engine": "e14", "metric": "untouched_metric", "alpha": 0.1,
    })
    assert res.status_code == 400
    assert "≥2 observations" in res.json()["detail"]


def test_endpoint_list_calibrators(client: TestClient, monkeypatch) -> None:
    _stub_redis(monkeypatch)
    # Seed two different calibrators.
    for engine, metric in [("e14", "breach_probability"), ("e1", "p95_mae_pct")]:
        client.post("/api/v2/conformal/observe", json={
            "engine": engine, "metric": metric, "prediction": 0.5, "realized": 0.5,
        })
    res = client.get("/api/v2/conformal/list")
    assert res.status_code == 200
    body = res.json()
    assert body["n_calibrators"] == 2
    seen = {(c["engine"], c["metric"]) for c in body["calibrators"]}
    assert seen == {("e14", "breach_probability"), ("e1", "p95_mae_pct")}
    # Default bounds map is exposed so the v1 mirror knows what to plug in.
    assert body["default_bounds_by_metric"]["breach_probability"] == [0.0, 1.0]


def test_endpoint_interval_in_warmup_when_no_observations(client: TestClient, monkeypatch) -> None:
    _stub_redis(monkeypatch)
    res = client.post(
        "/api/v2/conformal/interval",
        json={"engine": "e2", "metric": "breach_probability", "prediction": 0.4, "alpha": 0.1},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["warmup"] is True
    assert body["n_calibration"] == 0
    # Bounded probability → warm-up interval is [point - 0.25, point + 0.25] clipped to [0,1].
    assert body["lower"] == pytest.approx(0.15)
    assert body["upper"] == pytest.approx(0.65)


def test_endpoint_alpha_bounds(client: TestClient) -> None:
    res = client.post("/api/v2/conformal/interval", json={
        "engine": "e1", "metric": "x", "prediction": 0.5, "alpha": 0.0,
    })
    assert res.status_code == 422  # FastAPI Field bounds reject α=0
    res = client.post("/api/v2/conformal/interval", json={
        "engine": "e1", "metric": "x", "prediction": 0.5, "alpha": 0.6,
    })
    assert res.status_code == 422
