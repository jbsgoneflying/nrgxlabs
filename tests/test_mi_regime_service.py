"""Unit tests for backend.market_intel.regime_service."""
from __future__ import annotations

import datetime as dt

import pytest

from backend.market_intel import (
    canonical_vol_state,
    clear_cache,
    regime_snapshot,
    service_health,
)
from backend.market_intel.factors import OK, FactorReading, FactorSnapshot
from backend.market_intel.regime_model import _default_sticky_model
from backend.market_intel.regime_service import _model_is_fresh, stressed_label_guard


def setup_function(_fn):
    clear_cache()


def test_service_health_returns_metadata():
    h = service_health()
    assert h["model_version"] == "mi_hmm_v1"
    assert h["model_source"] in ("default", "disk", "redis", "memo")
    assert "feature_keys" in h
    assert len(h["feature_keys"]) == 8
    assert "state_labels" in h
    assert h["state_labels"] == ["Risk-On", "Transitional", "Stressed"]


def test_regime_snapshot_offline_returns_legacy_fallback():
    """With no clients, all factors are MISSING — service should return
    the legacy_fallback path with a synthesized probs vector."""
    snap = regime_snapshot(force_refresh=True)
    assert snap.source == "legacy_fallback"
    assert snap.label in ("Risk-On", "Transitional", "Risk-Off", "Stressed")
    assert sum(snap.probs.values()) == pytest.approx(1.0, abs=1e-3)
    assert snap.data_quality["insufficient"] is True
    # All 8 factors should be in MISSING.
    assert len(snap.data_quality["missing"]) == 8


def test_canonical_vol_state_with_factor_snapshot():
    snap = FactorSnapshot(as_of="2026-04-21")
    snap.readings["vix_term_slope"] = FactorReading(
        key="vix_term_slope", quality=OK, z=1.5, value=2.0,
    )
    snap.readings["rv_spx_20d"] = FactorReading(
        key="rv_spx_20d", quality=OK, z=0.5, value=18.0,
    )
    snap.readings["dealer_gamma"] = FactorReading(
        key="dealer_gamma", quality=OK, z=1.5, value=1.5,
    )
    vs = canonical_vol_state(factor_snap=snap)
    assert vs["term_structure"] == "backwardation"  # slope z > 0.5
    assert vs["level"] == 18.0  # from rv_spx_20d
    assert vs["skew"] == "elevated"  # dealer gamma z > 1.0


def test_canonical_vol_state_legacy_fallback():
    vs = canonical_vol_state(engine5_vol_direction="rising")
    assert vs["term_structure"] == "backwardation"
    vs = canonical_vol_state(engine5_vol_direction="compressing")
    assert vs["term_structure"] == "contango"


def test_regime_snapshot_caches_for_5_minutes():
    a = regime_snapshot()
    b = regime_snapshot()
    # Same as_of date → cache hit, should be the same object.
    assert a is b
    # force_refresh bypasses.
    c = regime_snapshot(force_refresh=True)
    assert c is not a or a.generated_at == c.generated_at


def test_regime_snapshot_with_engine5_label_pass_through():
    """When MI v2 is in fallback mode, it copies the E5 label."""
    e5 = {"data": {"regime": {"label": "Stressed", "score": 82.0}}}
    snap = regime_snapshot(engine5_snapshot=e5, force_refresh=True)
    if snap.source == "legacy_fallback":
        assert snap.label == "Stressed"
        # And probs concentrate on stressed.
        assert snap.probs["stressed"] > 0.6


# ---------------------------------------------------------------------------
# Stressed-label sanity guard (sign-aware)
# ---------------------------------------------------------------------------


def _snap_with_zs(zs: dict[str, float]) -> FactorSnapshot:
    snap = FactorSnapshot(as_of="2026-08-07")
    for key, z in zs.items():
        snap.readings[key] = FactorReading(key=key, quality=OK, z=z, value=z)
    return snap


def test_stressed_guard_demotes_on_benign_composite():
    """A big FAVORABLE move (negative z = calm) must not stay 'Stressed'.

    Regression for 2026-08-07: credit_hyg_lqd z=-2.53 (spreads tightening
    hard) landed in the calibrated model's fat-variance stressed state at
    88.7% confidence and gate-suppressed valid Ichimoku setups.
    """
    snap = _snap_with_zs({
        "rv_spx_20d": 0.62, "vix_term_slope": -0.48, "credit_hyg_lqd": -2.53,
        "dxy_drift": -0.89, "commodity_stress": -0.04, "btc_decoupling": 1.43,
        "breadth_proxy": -0.63,
    })
    label, override = stressed_label_guard("Stressed", snap)
    assert label == "Transitional"
    assert override is not None
    assert override["original_label"] == "Stressed"
    assert override["signed_composite"] < 0


def test_stressed_guard_keeps_genuine_stress():
    snap = _snap_with_zs({
        "rv_spx_20d": 2.1, "vix_term_slope": 1.8, "credit_hyg_lqd": 2.4,
        "dxy_drift": 0.9, "breadth_proxy": 1.2,
    })
    label, override = stressed_label_guard("Stressed", snap)
    assert label == "Stressed"
    assert override is None


def test_stressed_guard_ignores_other_labels():
    snap = _snap_with_zs({"rv_spx_20d": -3.0})
    label, override = stressed_label_guard("Risk-On", snap)
    assert label == "Risk-On"
    assert override is None


def test_stressed_guard_no_ok_factors_is_noop():
    snap = FactorSnapshot(as_of="2026-08-07")
    label, override = stressed_label_guard("Stressed", snap)
    assert label == "Stressed"
    assert override is None


# ---------------------------------------------------------------------------
# Persisted-model staleness guard
# ---------------------------------------------------------------------------


def test_model_freshness_accepts_recent_calibration():
    model = _default_sticky_model()
    model.calibrated_at = dt.datetime.now(dt.timezone.utc).isoformat() + "Z"
    assert _model_is_fresh(model) is True


def test_model_freshness_rejects_old_calibration():
    model = _default_sticky_model()
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)
    model.calibrated_at = old.isoformat() + "Z"
    assert _model_is_fresh(model) is False


def test_model_freshness_rejects_missing_timestamp():
    model = _default_sticky_model()
    model.calibrated_at = ""
    assert _model_is_fresh(model) is False


def test_model_freshness_guard_can_be_disabled(monkeypatch):
    monkeypatch.setenv("MI_MODEL_MAX_AGE_DAYS", "0")
    model = _default_sticky_model()
    model.calibrated_at = ""
    assert _model_is_fresh(model) is True
