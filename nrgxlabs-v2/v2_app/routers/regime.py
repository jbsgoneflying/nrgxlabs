"""v2 regime-encoder endpoints (Phase 1 module 3).

Feature-space MVP backed by v1's existing front_layer:dms:* corpus —
same recipe as the analogue index but over daily market states instead
of trade setups. Endpoints:

    POST /api/v2/regime/build      — rebuild the index from v1 DMS history
    POST /api/v2/regime/embed      — z-score a market state + return knn-cluster prior
    POST /api/v2/regime/nearest    — top-K most similar historical days
    GET  /api/v2/regime/stats      — index stats (for dashboard tile)
    GET  /api/v2/regime/features   — feature schema (so callers know what to send)

Phase 2 swaps the encoder for a learned latent space; the API stays stable.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..foundation.regime import (
    FEATURE_NAMES,
    extract_market_state,
    regime_label,
)
from ..foundation.regime_store import (
    build_and_persist,
    index_summary,
    load_index,
)

LOG = logging.getLogger("v2.regime_api")
router = APIRouter()


# ── Request models ────────────────────────────────────────


class BuildPayload(BaseModel):
    max_days: int = Field(365, ge=1, le=2000)


class MarketStatePayload(BaseModel):
    """Either a parsed feature dict or a raw v1 DMS document.

    If ``features`` is provided, it's used as-is (skipped through extraction).
    Otherwise we derive features from ``dms`` via ``extract_market_state``.
    """

    features: Optional[dict[str, float | None]] = None
    dms: Optional[dict[str, Any]] = None


class NearestPayload(MarketStatePayload):
    k: int = Field(5, ge=1, le=50)
    date_exclude: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────


def _resolve_features(p: MarketStatePayload) -> dict[str, float | None]:
    if p.features is not None:
        return {k: (None if v is None else float(v))
                for k, v in p.features.items() if k in FEATURE_NAMES}
    if p.dms is not None:
        return extract_market_state(p.dms)
    raise HTTPException(
        status_code=422,
        detail="Provide either `features` (parsed map) or `dms` (raw v1 doc).",
    )


# ── Endpoints ─────────────────────────────────────────────


@router.post("/api/v2/regime/build")
def build(payload: BuildPayload | None = None) -> dict:
    p = payload or BuildPayload()
    try:
        stats = build_and_persist(max_days=p.max_days)
    except Exception as exc:
        LOG.exception("regime build failed")
        raise HTTPException(status_code=503, detail=f"build failed: {exc}") from exc
    return {"ok": stats.get("ok", True), **stats}


@router.post("/api/v2/regime/embed")
def embed(payload: MarketStatePayload) -> dict:
    feats = _resolve_features(payload)
    label = regime_label(payload.dms or {}) if payload.dms else None
    try:
        index = load_index()
    except Exception as exc:
        LOG.warning("regime/embed redis unavailable: %s", exc)
        index = None
    if index is None or index.n_indexed == 0:
        return {
            "status": "not_built",
            "message": "POST /api/v2/regime/build to construct the index from v1 DMS history.",
            "features": feats,
            "label": label,
        }
    out = index.encode(feats)
    out.update({"status": "ok", "features": feats, "label": label})
    return out


@router.post("/api/v2/regime/nearest")
def nearest(payload: NearestPayload) -> dict:
    feats = _resolve_features(payload)
    try:
        index = load_index()
    except Exception as exc:
        LOG.warning("regime/nearest redis unavailable: %s", exc)
        index = None
    if index is None or index.n_indexed == 0:
        return {
            "status": "not_built",
            "message": "POST /api/v2/regime/build first.",
            "features": feats,
            "k": payload.k,
            "neighbors": [],
        }
    nbrs = index.search(feats, k=payload.k, date_exclude=payload.date_exclude)
    return {
        "status": "ok",
        "k": payload.k,
        "n_indexed": index.n_indexed,
        "features": feats,
        "neighbors": nbrs,
    }


@router.get("/api/v2/regime/stats")
def stats() -> dict:
    try:
        summary = index_summary()
    except Exception as exc:
        LOG.warning("regime/stats redis unavailable: %s", exc)
        return {"status": "redis_unavailable", "n_indexed": 0, "error": str(exc)}
    return {"status": "ok", **summary}


@router.get("/api/v2/regime/features")
def features() -> dict:
    return {"feature_names": FEATURE_NAMES, "engine": "regime"}


# Legacy GET stubs — keep alive for back-compat with anything pointing at
# the Phase 0 contract (frontend may still hit GET /api/v2/regime/embed).


@router.get("/api/v2/regime/embed")
def embed_legacy(date: Optional[str] = Query(None)) -> dict:
    return {
        "status": "phase1_mvp_active",
        "message": "POST /api/v2/regime/embed with {features|dms} payload. GET retained for backward compat.",
        "as_of": date,
        "feature_names": FEATURE_NAMES,
    }


@router.get("/api/v2/regime/nearest")
def nearest_legacy(date: Optional[str] = Query(None), k: int = Query(5, ge=1, le=50)) -> dict:
    return {
        "status": "phase1_mvp_active",
        "message": "POST /api/v2/regime/nearest with {features|dms, k} payload. GET retained for backward compat.",
        "as_of": date,
        "k": int(k),
        "neighbors": [],
    }
