"""v2 analogue retrieval API.

Phase 1 module 2 — feature-space MVP. Pure-Python brute-force cosine
search over a hand-crafted feature embedding of v1 closed trades.
Cross-ticker, cross-time. Phase 2 will swap the encoder out for a
learned contrastive embedder; the index API stays stable so callers
don't change.

Endpoints:

- POST /api/v2/analogues/build   — rebuild the engine's analogue index from v1 trades.
- POST /api/v2/analogues/search  — given a query setup, return top-K analogues + outcome summary.
- GET  /api/v2/analogues/stats   — per-engine index sizes + feature names.
- GET  /api/v2/analogues/features?engine=  — feature list (so callers know what to send).

The legacy GET /api/v2/analogues/search stub is kept for backward compatibility
but now delegates to the real search when an index exists.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..config import get_config
from ..foundation.analogues import feature_names
from ..foundation.analogues_store import (
    V1_TRADE_SOURCES,
    build_and_persist,
    index_summaries,
    load_index,
)

router = APIRouter()


# ── /build ──


class BuildPayload(BaseModel):
    engine: str = Field(..., description="e1 | e2")
    cap: int = Field(5000, ge=1, le=50000)


@router.post("/api/v2/analogues/build")
def build(payload: BuildPayload) -> dict:
    if payload.engine not in V1_TRADE_SOURCES:
        raise HTTPException(status_code=400, detail=f"unknown engine: {payload.engine!r}")
    return build_and_persist(payload.engine, cap=payload.cap)


# ── /search ──


class SearchPayload(BaseModel):
    engine: str = Field(..., description="e1 | e2")
    query: dict[str, float | None] = Field(
        default_factory=dict,
        description="Feature dict — use /features to see the schema for the engine",
    )
    k: int = Field(10, ge=1, le=200)
    ticker_exclude: str | None = Field(
        None, description="If set, drop neighbors with this ticker (avoids self-matches)"
    )


@router.post("/api/v2/analogues/search")
def search_real(payload: SearchPayload) -> dict:
    if payload.engine not in V1_TRADE_SOURCES:
        raise HTTPException(status_code=400, detail=f"unknown engine: {payload.engine!r}")
    idx = load_index(payload.engine)
    if idx is None or idx.n_indexed == 0:
        return {
            "engine": payload.engine,
            "n_indexed": 0,
            "neighbors": [],
            "outcome_summary": {"n": 0},
            "message": "no index built yet — POST /api/v2/analogues/build first",
        }

    neighbors = idx.search(payload.query, k=payload.k, ticker_exclude=payload.ticker_exclude)
    return {
        "engine": payload.engine,
        "n_indexed": idx.n_indexed,
        "k_returned": len(neighbors),
        "feature_names": idx.feature_names,
        "neighbors": neighbors,
        "outcome_summary": idx.outcome_summary(neighbors),
    }


# ── /stats ──


@router.get("/api/v2/analogues/stats")
def stats() -> dict:
    return {
        "indexes": index_summaries(),
        "engines_supported": list(V1_TRADE_SOURCES.keys()),
    }


@router.get("/api/v2/analogues/features")
def features(engine: str = Query(...)) -> dict:
    if engine not in V1_TRADE_SOURCES:
        raise HTTPException(status_code=400, detail=f"unknown engine: {engine!r}")
    return {"engine": engine, "feature_names": feature_names(engine)}


# ── /search (legacy GET stub kept for back-compat) ──


@router.get("/api/v2/analogues/search")
def search_legacy(
    ticker: Optional[str] = None,
    event_date: Optional[str] = None,
    k: int = 10,
    cross_ticker: bool = True,
) -> dict:
    """Pre-Phase-1 stub. Kept so any earlier integrations don't 404.

    Real search is the POST endpoint above which expects a feature dict.
    """
    cfg = get_config()
    return {
        "status": "phase1_mvp_active" if cfg.enable_contrastive_analogues else "stub",
        "query": {"ticker": (ticker or "").upper() or None,
                  "event_date": event_date,
                  "k": int(k),
                  "cross_ticker": bool(cross_ticker)},
        "neighbors": [],
        "embedding_space": {
            "dim": "feature-space (Phase 1 MVP)",
            "training_corpus_size": None,
            "trained_at": None,
        },
        "message": (
            "Phase 1 MVP is live: POST /api/v2/analogues/search with "
            "{engine, query, k} for the real cosine-similarity search."
        ),
    }
