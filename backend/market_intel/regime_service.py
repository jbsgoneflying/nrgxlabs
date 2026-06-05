"""Canonical Market Intelligence service — single source of truth.

Every consumer that needs regime state, vol state, or a factor snapshot
MUST call through this module rather than rebuilding locally. The
service handles:

- In-process + Redis caching
- Graceful fallback ladder: HMM → disk-calibration → legacy E5 regime →
  static safe default
- Data-quality classification (``insufficient_data`` when too many
  factors are MISSING)

Call it like::

    from backend.market_intel import regime_snapshot
    snap = regime_snapshot()
    print(snap.probs, snap.label, snap.confidence)
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from cachetools import TTLCache

from backend.market_intel.factors import (
    FactorSnapshot,
    FACTOR_KEYS,
    build_factor_snapshot,
    MISSING as FACTOR_MISSING,
)
from backend.market_intel.regime_model import (
    MODEL_VERSION,
    STATE_LABELS,
    CalibratedModel,
    ConfidenceBand,
    RegimeInference,
    _default_sticky_model,
    bootstrap_confidence,
    infer,
    load_model,
    model_from_redis,
)

LOG = logging.getLogger("market_intel.regime_service")


# ---------------------------------------------------------------------------
# Config knobs (read fresh each call so env-driven tests behave)
# ---------------------------------------------------------------------------


def _cfg_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _cfg_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v is not None else default


def _model_path() -> str:
    return _cfg_str("MI_MODEL_STORAGE_PATH", "data/market_intel_regime_model.json")


def _redis_key() -> str:
    return _cfg_str("MI_REDIS_MODEL_KEY", "market_intel:model:v1")


def _insufficient_floor() -> int:
    return _cfg_int("MI_INSUFFICIENT_DATA_MISSING_FLOOR", 3)


def _factor_stale_days() -> int:
    return _cfg_int("MI_FACTOR_STALE_DAYS", 1)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class RegimeSnapshot:
    """The canonical response. Everything a consumer needs in one object."""

    as_of:                str = ""
    probs:                Dict[str, float] = field(default_factory=dict)
    label:                str = "Transitional"
    confidence:           float = 0.0
    transition_risk_1d:   float = 0.0
    factor_readings:      Dict[str, dict] = field(default_factory=dict)
    factor_contributions: Dict[str, float] = field(default_factory=dict)
    anomaly_score:        float = 0.0
    confidence_band:      Dict[str, Any] = field(default_factory=dict)
    vol_state:            Dict[str, Any] = field(default_factory=dict)
    data_quality:         Dict[str, Any] = field(default_factory=dict)
    model_version:        str = MODEL_VERSION
    calibrated_at:        str = ""
    source:               str = "v2_hmm"  # v2_hmm | legacy_fallback | insufficient_data | default_model
    generated_at:         str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: TTLCache = TTLCache(maxsize=4, ttl=5 * 60)

_model_lock = threading.Lock()
_model_cache: Optional[CalibratedModel] = None
_model_cache_at: float = 0.0
_MODEL_MEMO_TTL_S = 300  # 5 min in-process model cache


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
    global _model_cache, _model_cache_at
    with _model_lock:
        _model_cache = None
        _model_cache_at = 0.0


# ---------------------------------------------------------------------------
# Model loading (Redis → disk → sticky default)
# ---------------------------------------------------------------------------


def _load_calibrated_model() -> tuple[CalibratedModel, str]:
    """Return (model, provenance) tuple."""
    global _model_cache, _model_cache_at
    with _model_lock:
        if _model_cache and (time.time() - _model_cache_at) < _MODEL_MEMO_TTL_S:
            return _model_cache, "memo"

    # Redis first (multi-worker consistency).
    try:
        from backend.redis_store import get_store_optional
        store = get_store_optional()
        if store is not None:
            rmodel = model_from_redis(store, _redis_key())
            if rmodel:
                with _model_lock:
                    _model_cache = rmodel
                    _model_cache_at = time.time()
                return rmodel, "redis"
    except Exception as e:
        LOG.debug("market_intel: redis model load failed: %s", e)

    # Disk fallback.
    dmodel = load_model(_model_path())
    if dmodel:
        with _model_lock:
            _model_cache = dmodel
            _model_cache_at = time.time()
        return dmodel, "disk"

    # Final fallback: sticky default (cold start).
    default = _default_sticky_model()
    with _model_lock:
        _model_cache = default
        _model_cache_at = time.time()
    return default, "default"


# ---------------------------------------------------------------------------
# Vol state (single canonical derivation)
# ---------------------------------------------------------------------------


def canonical_vol_state(
    *,
    factor_snap: Optional[FactorSnapshot] = None,
    vix_level: Optional[float] = None,
    engine5_vol_direction: Optional[str] = None,
) -> Dict[str, Any]:
    """Single source of truth for vol state.

    Fuses (in priority order):
    1. ``vix_term_slope`` factor z — if OK, drives term_structure.
    2. ``rv_spx_20d`` factor value — drives level.
    3. Legacy engine5 vol_direction string (backwards compat).
    """
    term = "flat"
    skew = "neutral"
    level: Optional[float] = vix_level

    if factor_snap:
        slope_reading = factor_snap.readings.get("vix_term_slope")
        if slope_reading and slope_reading.quality == "OK":
            # Positive slope = backwardation = stress.
            if slope_reading.z > 0.5:
                term = "backwardation"
            elif slope_reading.z < -0.5:
                term = "contango"
            else:
                term = "flat"
        rv_reading = factor_snap.readings.get("rv_spx_20d")
        if rv_reading and rv_reading.quality == "OK" and level is None:
            level = float(rv_reading.value)
        # Skew proxy: use dealer_gamma (short-gamma = elevated skew for puts).
        dg = factor_snap.readings.get("dealer_gamma")
        if dg and dg.quality == "OK":
            if dg.z > 1.0:
                skew = "elevated"
            elif dg.z < -0.5:
                skew = "low"

    # Legacy fallback if factor snap didn't set term.
    if term == "flat" and engine5_vol_direction:
        d = (engine5_vol_direction or "").lower()
        if d in ("rising", "confirmed_stress", "expanding"):
            term = "backwardation"
        elif d in ("falling", "compressing"):
            term = "contango"

    return {
        "level":          round(level, 2) if isinstance(level, (int, float)) else 0.0,
        "term_structure": term,
        "skew":           skew,
        "source":         "market_intel.canonical_vol_state",
    }


# ---------------------------------------------------------------------------
# Live dealer-gamma injection
# ---------------------------------------------------------------------------


def _maybe_inject_dealer_gamma(factor_snap: FactorSnapshot, *, today: dt.date) -> None:
    """Patch the dealer_gamma reading with a live z when the model uses it.

    No-op (and zero ORATS calls) when the serving model is the legacy
    zero-trained placeholder, so deploying this ahead of a retrain changes
    nothing. Any failure leaves dealer_gamma at its neutral default.
    """
    try:
        from backend.market_intel.gamma_history import (
            dealer_z_today,
            model_uses_dealer_gamma,
        )

        model, _prov = _load_calibrated_model()
        if not model_uses_dealer_gamma(model):
            return

        try:
            from backend.deps import get_client_optional
            orats_client = get_client_optional()
        except Exception:
            orats_client = None
        if orats_client is None:
            return

        store = None
        try:
            from backend.redis_store import get_store_optional
            store = get_store_optional()
        except Exception:
            store = None

        z = dealer_z_today(orats_client, store=store, today=today)
        if z is None:
            return

        from backend.market_intel.factors import (
            FactorReading,
            FACTOR_LABELS,
            OK as FACTOR_OK,
        )

        factor_snap.readings["dealer_gamma"] = FactorReading(
            key="dealer_gamma",
            label=FACTOR_LABELS.get("dealer_gamma", "Dealer Gamma z"),
            value=float(z),
            z=float(z),
            quality=FACTOR_OK,
            as_of=today.isoformat(),
            source="market_intel.gamma_history",
            note="live dealer-gamma z",
        )
        # Recompute the quality buckets so the data-quality gate sees it present.
        readings = factor_snap.readings
        factor_snap.missing = [k for k, r in readings.items() if r.quality == FACTOR_MISSING]
        factor_snap.stale = [k for k, r in readings.items() if r.quality == "STALE"]
        factor_snap.ok = [k for k, r in readings.items() if r.quality == FACTOR_OK]
    except Exception as e:  # noqa: BLE001 - never let gamma injection break the regime
        LOG.debug("market_intel: dealer_gamma injection skipped: %s", e)


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def regime_snapshot(
    *,
    as_of: Optional[dt.date] = None,
    eodhd_client: Any = None,
    gamma_context: Optional[dict] = None,
    engine5_snapshot: Optional[dict] = None,
    force_refresh: bool = False,
) -> RegimeSnapshot:
    """Return the canonical market regime snapshot.

    Heavy call — factor builders fetch from EODHD etc — so results are
    cached 5 minutes in-process per (as_of) key. Set ``force_refresh=True``
    to bypass.

    If the caller didn't supply an ``eodhd_client`` or ``gamma_context``,
    we try to resolve them from ``backend.deps`` so this function is
    callable from anywhere with zero wiring.
    """
    today = as_of or dt.date.today()
    ckey = f"snap:{today.isoformat()}"
    if not force_refresh:
        with _cache_lock:
            hit = _cache.get(ckey)
        if hit is not None:
            return hit

    # Resolve clients if not provided.
    if eodhd_client is None:
        try:
            from backend.eodhd_client import EodhdClient
            eodhd_client = EodhdClient.from_env()
        except Exception as e:
            LOG.debug("market_intel: EODHD client unavailable: %s", e)
            eodhd_client = None

    # Build factor snapshot.
    factor_snap = build_factor_snapshot(
        eodhd_client=eodhd_client,
        gamma_context=gamma_context,
        stale_days=_factor_stale_days(),
        today=today,
    )

    # Inject the live dealer-gamma z — but only when the serving model was
    # actually trained on a real (non-constant) dealer_gamma feature. With the
    # legacy zero-trained model this is a no-op (the column cancels anyway), so
    # this is safe to ship before any retrain and self-activates after the swap.
    _maybe_inject_dealer_gamma(factor_snap, today=today)

    # Data quality gate.
    n_missing = len(factor_snap.missing)
    insufficient = n_missing >= _insufficient_floor()

    # VIX level from engine5 snapshot if we have it.
    vix_level: Optional[float] = None
    engine5_vol_direction: Optional[str] = None
    if engine5_snapshot:
        snap_data = (engine5_snapshot or {}).get("data", {})
        regime_data = snap_data.get("regime", {})
        iv_stress = regime_data.get("components", {}).get("iv_stress")
        if iv_stress is not None:
            vix_level = float(iv_stress) * 0.5
        vol_ll = snap_data.get("volLeadLag", {})
        engine5_vol_direction = str(
            vol_ll.get("volLagState") or vol_ll.get("vol_lag_state", "")
        )

    # Insufficient data → legacy fallback path.
    if insufficient:
        LOG.warning(
            "market_intel: insufficient factor data (%d missing); returning legacy-style snapshot",
            n_missing,
        )
        snap = _legacy_fallback_snapshot(
            today=today,
            engine5_snapshot=engine5_snapshot,
            factor_snap=factor_snap,
            reason=f"{n_missing} factors missing (>= {_insufficient_floor()})",
        )
        with _cache_lock:
            _cache[ckey] = snap
        return snap

    # Run HMM inference.
    model, model_provenance = _load_calibrated_model()
    vector = factor_snap.vector
    inf_result = infer(model, vector)
    band = bootstrap_confidence(model, vector, n_samples=min(200, 500))

    # Vol state.
    vol_state = canonical_vol_state(
        factor_snap=factor_snap,
        vix_level=vix_level,
        engine5_vol_direction=engine5_vol_direction,
    )

    source = "v2_hmm" if model.training_days >= 200 else "default_model"

    snap = RegimeSnapshot(
        as_of=today.isoformat(),
        probs=inf_result.probs,
        label=inf_result.label,
        confidence=inf_result.confidence,
        transition_risk_1d=inf_result.transition_risk_1d,
        factor_readings={k: v.to_dict() for k, v in factor_snap.readings.items()},
        factor_contributions=inf_result.factor_contributions,
        anomaly_score=inf_result.anomaly_score,
        confidence_band=band.to_dict(),
        vol_state=vol_state,
        data_quality={
            "ok":      list(factor_snap.ok),
            "stale":   list(factor_snap.stale),
            "missing": list(factor_snap.missing),
            "insufficient": False,
            "model_source": model_provenance,
        },
        model_version=model.model_version,
        calibrated_at=model.calibrated_at,
        source=source,
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat() + "Z",
    )
    with _cache_lock:
        _cache[ckey] = snap
    return snap


def _legacy_fallback_snapshot(
    *,
    today: dt.date,
    engine5_snapshot: Optional[dict],
    factor_snap: FactorSnapshot,
    reason: str,
) -> RegimeSnapshot:
    """When data is too thin, wrap the legacy Engine 5 linear score in the
    RegimeSnapshot schema so downstream consumers never see ``None``.
    """
    # Pull label + score from Engine 5 snapshot if we have it.
    label = "Transitional"
    score = 50.0
    if engine5_snapshot:
        snap_data = (engine5_snapshot or {}).get("data", {})
        regime_data = snap_data.get("regime", {})
        label = str(regime_data.get("label", "Transitional"))
        score = float(regime_data.get("score", 50.0))

    # Synthesize a probs vector that concentrates on the legacy label.
    probs = {"risk_on": 0.0, "transitional": 0.0, "stressed": 0.0}
    if label == "Risk-On":
        probs = {"risk_on": 0.80, "transitional": 0.18, "stressed": 0.02}
    elif label == "Stressed":
        probs = {"risk_on": 0.02, "transitional": 0.18, "stressed": 0.80}
    elif label in ("Risk-Off", "Transitional"):
        probs = {"risk_on": 0.15, "transitional": 0.70, "stressed": 0.15}

    vol_state = canonical_vol_state(
        factor_snap=factor_snap,
        vix_level=None,
        engine5_vol_direction=None,
    )

    return RegimeSnapshot(
        as_of=today.isoformat(),
        probs=probs,
        label=label,
        confidence=max(probs.values()),
        transition_risk_1d=0.0,
        factor_readings={k: v.to_dict() for k, v in factor_snap.readings.items()},
        factor_contributions={},
        anomaly_score=0.0,
        confidence_band={},
        vol_state=vol_state,
        data_quality={
            "ok":      list(factor_snap.ok),
            "stale":   list(factor_snap.stale),
            "missing": list(factor_snap.missing),
            "insufficient": True,
            "fallback_reason": reason,
            "legacy_score": score,
        },
        model_version=MODEL_VERSION,
        calibrated_at="",
        source="legacy_fallback",
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat() + "Z",
    )


# ---------------------------------------------------------------------------
# Health (for the /api/market-intel/health endpoint)
# ---------------------------------------------------------------------------


def service_health() -> Dict[str, Any]:
    model, provenance = _load_calibrated_model()
    return {
        "model_version":   model.model_version,
        "model_source":    provenance,
        "training_days":   model.training_days,
        "calibrated_at":   model.calibrated_at,
        "state_labels":    list(model.state_labels),
        "feature_keys":    list(model.feature_keys),
        "insufficient_data_floor": _insufficient_floor(),
        "factor_stale_days":       _factor_stale_days(),
        "redis_key":       _redis_key(),
        "disk_path":       _model_path(),
    }
