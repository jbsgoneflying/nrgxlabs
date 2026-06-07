"""v2 path-generator endpoints (Phase 1 module 5).

Bootstrap path generator with optional regime-conditional weighting.
The regime weights come from POSTing a list of nearest-day similarity
neighbors (typically obtained from /api/v2/regime/nearest) — coupling
between modules stays explicit and testable.

Endpoints
---------
    POST /api/v2/paths/corpus/save    — persist a return corpus for a ticker
    GET  /api/v2/paths/corpus/list    — list known corpora
    POST /api/v2/paths/sample         — sample N paths (returns terminal stats only)
    POST /api/v2/paths/breach-prob    — compute breach probability for a bracket
    GET  /api/v2/paths/stats          — corpus stats (used by the dashboard tile)
"""

from __future__ import annotations

import logging
import random
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..foundation.paths import (
    PathSampler,
    regime_weights_from_neighbors,
)
from ..foundation.paths_store import (
    list_corpora,
    load_corpus,
    save_corpus,
)

LOG = logging.getLogger("v2.paths_api")
router = APIRouter()


# ── Request models ────────────────────────────────────────


class CorpusRow(BaseModel):
    date: str = Field(..., min_length=4, max_length=32)
    log_return: float


class SaveCorpusPayload(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=12)
    rows: list[CorpusRow] = Field(..., min_length=1)


class NeighborWeight(BaseModel):
    date: str
    similarity: float


class SamplePayload(BaseModel):
    ticker: Optional[str] = None
    returns: Optional[list[float]] = None
    return_dates: Optional[list[str]] = None  # required when conditioning
    regime_neighbors: Optional[list[NeighborWeight]] = None
    n_samples: int = Field(2000, ge=10, le=20000)
    horizon_days: int = Field(21, ge=1, le=120)
    seed: Optional[int] = None


class BreachPayload(SamplePayload):
    lower_threshold: Optional[float] = None
    upper_threshold: Optional[float] = None
    bootstrap_ci_resamples: int = Field(200, ge=10, le=2000)


# ── Helpers ───────────────────────────────────────────────


def _resolve_returns(p: SamplePayload) -> tuple[list[float], list[str]]:
    """Resolve a (returns, dates) pair from payload — either inline or
    looked up from the persisted corpus by ticker."""
    if p.returns is not None and len(p.returns) >= PathSampler.MIN_RETURNS:
        # Inline corpus.
        if p.return_dates and len(p.return_dates) == len(p.returns):
            return list(p.returns), list(p.return_dates)
        return list(p.returns), [str(i) for i in range(len(p.returns))]

    if not p.ticker:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Provide either inline `returns` (>= {PathSampler.MIN_RETURNS}) "
                "or `ticker` pointing at a persisted corpus."
            ),
        )
    try:
        doc = load_corpus(p.ticker)
    except Exception as exc:
        LOG.warning("paths: corpus load failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"redis unavailable: {exc}") from exc
    if not doc or not doc.get("rows"):
        raise HTTPException(
            status_code=404,
            detail=f"no corpus for ticker {p.ticker}; POST /api/v2/paths/corpus/save first.",
        )
    rows = doc["rows"]
    return [float(r["log_return"]) for r in rows], [str(r["date"]) for r in rows]


def _build_sampler(p: SamplePayload, returns: list[float], dates: list[str]) -> PathSampler:
    weights = None
    if p.regime_neighbors:
        weights = regime_weights_from_neighbors(
            [n.model_dump() for n in p.regime_neighbors],
            return_dates=dates,
        )
        if not any(w > 0 for w in weights):
            weights = None  # no overlap → fall back to uniform
    rng = random.Random(p.seed) if p.seed is not None else random.Random()
    return PathSampler(returns=returns, weights=weights, rng=rng)


# ── Endpoints ─────────────────────────────────────────────


@router.post("/api/v2/paths/corpus/save")
def corpus_save(payload: SaveCorpusPayload) -> dict:
    rows = [r.model_dump() for r in payload.rows]
    try:
        out = save_corpus(payload.ticker, rows)
    except Exception as exc:
        LOG.exception("paths: corpus save failed")
        raise HTTPException(status_code=503, detail=f"redis unavailable: {exc}") from exc
    return out


@router.get("/api/v2/paths/corpus/list")
def corpus_list() -> dict:
    try:
        return {"ok": True, **list_corpora()}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "tickers": [], "n_total": 0}


@router.post("/api/v2/paths/sample")
def sample(payload: SamplePayload) -> dict:
    returns, dates = _resolve_returns(payload)
    sampler = _build_sampler(payload, returns, dates)
    try:
        paths = sampler.sample_paths(
            n_samples=payload.n_samples, horizon_days=payload.horizon_days,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    terminals = [p[-1] for p in paths] if paths else []
    stats = sampler._terminal_stats(terminals, payload.horizon_days)
    return {
        "ok": True,
        "n_returns": sampler.n_returns,
        "n_samples": payload.n_samples,
        "horizon_days": payload.horizon_days,
        "regime_conditional": sampler.weights is not None,
        "terminal_stats": stats.to_dict(),
    }


@router.post("/api/v2/paths/breach-prob")
def breach_prob(payload: BreachPayload) -> dict:
    if payload.lower_threshold is None and payload.upper_threshold is None:
        raise HTTPException(
            status_code=422,
            detail="Provide at least one of lower_threshold / upper_threshold.",
        )
    returns, dates = _resolve_returns(payload)
    sampler = _build_sampler(payload, returns, dates)
    try:
        result = sampler.breach_probability(
            lower_threshold=payload.lower_threshold,
            upper_threshold=payload.upper_threshold,
            n_samples=payload.n_samples,
            horizon_days=payload.horizon_days,
            bootstrap_ci_resamples=payload.bootstrap_ci_resamples,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "ok": True,
        "n_returns": sampler.n_returns,
        "regime_conditional": sampler.weights is not None,
        **result.to_dict(),
    }


@router.get("/api/v2/paths/stats")
def stats() -> dict:
    try:
        out = list_corpora()
    except Exception as exc:
        return {"status": "redis_unavailable", "error": str(exc), "tickers": [], "n_total": 0}
    return {"status": "ok", **out}
