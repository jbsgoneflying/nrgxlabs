"""Conformal calibration API.

Endpoints:

- ``POST /api/v2/conformal/observe``  — record a (prediction, realized) pair
- ``POST /api/v2/conformal/interval`` — calibrated coverage interval at α
- ``GET  /api/v2/conformal/coverage`` — leave-one-out empirical coverage
- ``GET  /api/v2/conformal/list``     — every persisted calibrator + stats

Calibrators are keyed by ``(engine, metric)`` so the same machinery serves
E14 breach probability, E1 P95 MAE, E2 touch probability, etc.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..foundation.conformal_store import (
    DEFAULT_BOUNDS_BY_METRIC,
    list_calibrators,
    load_calibrator,
    now_ts,
    save_calibrator,
)
from ..foundation.v1_mirror import mirror_v1_breach_probability

router = APIRouter()


class ObservePayload(BaseModel):
    engine: str = Field(..., description="e1 | e2 | e14 | e15 | mi")
    metric: str = Field(..., description="e.g. breach_probability, p95_mae_pct")
    prediction: float
    realized: float
    ts: str | None = None


class IntervalPayload(BaseModel):
    engine: str
    metric: str
    prediction: float
    alpha: float = Field(0.1, ge=0.001, le=0.5, description="miscoverage level (default 0.1 → 90% coverage)")


@router.post("/api/v2/conformal/observe")
def observe(payload: ObservePayload) -> dict:
    cal = load_calibrator(payload.engine, payload.metric)
    n = cal.observe(prediction=payload.prediction, realized=payload.realized, ts=payload.ts or now_ts())
    persisted = save_calibrator(payload.engine, payload.metric, cal)
    return {
        "ok": True,
        "engine": payload.engine,
        "metric": payload.metric,
        "n": n,
        "persisted": persisted,
        "warmed_up": n >= cal.MIN_WARMUP_N,
    }


@router.post("/api/v2/conformal/interval")
def interval(payload: IntervalPayload) -> dict:
    cal = load_calibrator(payload.engine, payload.metric)
    ci = cal.interval(prediction=payload.prediction, alpha=payload.alpha)
    return {
        "engine": payload.engine,
        "metric": payload.metric,
        "alpha": ci.alpha,
        "coverage_target": ci.coverage_target,
        "n_calibration": ci.n_calibration,
        "warmup": ci.warmup,
        "point": ci.point,
        "lower": ci.lower,
        "upper": ci.upper,
        "width": ci.width,
        "bound": [ci.bound_lo, ci.bound_hi],
    }


@router.get("/api/v2/conformal/coverage")
def coverage(
    engine: str = Query(..., min_length=1),
    metric: str = Query(..., min_length=1),
    alpha: float = Query(0.1, ge=0.001, le=0.5),
) -> dict:
    cal = load_calibrator(engine, metric)
    n = cal.state.n
    if n < 2:
        raise HTTPException(
            status_code=400,
            detail=f"need ≥2 observations to compute coverage; have {n}",
        )
    cov = cal.empirical_coverage(alpha=alpha)
    target = 1.0 - alpha
    return {
        "engine": engine,
        "metric": metric,
        "alpha": alpha,
        "n": n,
        "empirical_coverage": cov,
        "target_coverage": target,
        "drift": cov - target,
        "last_observation_ts": cal.state.last_observation_ts,
    }


@router.get("/api/v2/conformal/list")
def list_all() -> dict:
    rows = list_calibrators()
    return {
        "n_calibrators": len(rows),
        "default_bounds_by_metric": DEFAULT_BOUNDS_BY_METRIC,
        "calibrators": rows,
    }


class MirrorPayload(BaseModel):
    only_engine: str | None = Field(
        None, description="If set to 'e1' or 'e2', restrict mirror to that engine only."
    )
    reset: bool = Field(
        True,
        description="Clear each touched calibrator before replaying so re-runs are idempotent.",
    )
    max_per_engine: int = Field(5000, ge=1, le=50000)


@router.post("/api/v2/conformal/mirror")
def mirror_v1(payload: MirrorPayload | None = None) -> dict:
    """Replay v1 closed trades into the v2 conformal calibrator.

    Reads ``e1:trades:*`` and ``e2:trades:*`` from Redis, extracts the
    (predicted breach probability, realized loss/no-loss) pair from each
    closed trade, and observes it via the breach_probability calibrator.
    """
    p = payload or MirrorPayload()
    return mirror_v1_breach_probability(
        only_engine=p.only_engine,
        reset=p.reset,
        max_per_engine=p.max_per_engine,
    )
