"""Equity Repricing Lab — shadow command-surface API.

No ENGINE_REGISTRY entry; shadow-only by default (REPRICING_LAB_SHADOW_ONLY=1).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from backend.config import get_flags

router = APIRouter(prefix="/api/equity-repricing", tags=["equity-repricing-lab"])


def _require_enabled():
    flags = get_flags()
    if not flags.REPRICING_LAB_ENABLED:
        raise HTTPException(
            status_code=404,
            detail="Equity Repricing Lab disabled (REPRICING_LAB_ENABLED=0)",
        )
    return flags


@router.get("/health")
def lab_health():
    flags = get_flags()
    return {
        "enabled": bool(flags.REPRICING_LAB_ENABLED),
        "shadowOnly": bool(flags.REPRICING_LAB_SHADOW_ONLY),
        "shortEnabled": bool(flags.REPRICING_LAB_SHORT_ENABLED),
        "engineRegistered": False,
    }


@router.get("/scout")
def scout(limit: int = Query(50, ge=1, le=200)):
    flags = _require_enabled()
    from backend.repricing_lab.signals import list_shadow_candidates
    return {
        "shadowOnly": bool(flags.REPRICING_LAB_SHADOW_ONLY),
        "candidates": list_shadow_candidates(limit=limit),
    }


@router.post("/refresh")
def refresh(lookback_days: int = Query(5, ge=1, le=14)):
    """Rebuild shadow scout from recent EODHD earnings calendar."""
    _require_enabled()
    from backend.repricing_lab.signals import refresh_live_scout
    try:
        return refresh_live_scout(lookback_days=lookback_days)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"scout refresh failed: {exc}") from exc


@router.get("/validation")
def validation():
    _require_enabled()
    from backend.repricing_lab.signals import decay_check, list_shadow_candidates
    cands = list_shadow_candidates(limit=200)
    return {
        "decay": decay_check(expectancy_r=0.0, n=0),
        "candidateCount": len(cands),
        "note": "Shadow validation — no live risk while shadow-only is on",
        "shadowOnly": True,
    }
