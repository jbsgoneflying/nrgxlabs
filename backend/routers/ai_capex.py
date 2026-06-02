"""AI Capex Reality Engine (Engine 17) — API routes.

Endpoints:
- ``GET  /api/ai-capex``              — current scan (cached; cheap rescore fallback).
- ``POST /api/ai-capex/refresh``      — full rebuild (ingest + LLM extract + score).
- ``GET  /api/ai-capex/evidence/{t}`` — per-ticker evidence audit trail.
- ``GET  /api/ai-capex/universe``     — taxonomy (categories + tickers).

The GET path is cheap: it serves the Redis-cached scan written by the nightly
``scripts/refresh_ai_capex.py`` job, or — if none exists yet — re-scores from
already-persisted evidence (no LLM/network). The heavy ingest+LLM rebuild only
runs on ``/refresh`` (serialised) and in the nightly job, so the request path
never stampedes the LLM.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query

from backend.config import get_flags

LOG = logging.getLogger("ai_capex")

router = APIRouter()

_refresh_lock = threading.Lock()


def _require_enabled() -> None:
    if not getattr(get_flags(), "ENABLE_AI_CAPEX", False):
        raise HTTPException(status_code=404, detail="AI Capex Reality Engine is disabled")


def _empty_payload() -> Dict[str, Any]:
    import datetime as dt
    from backend.ai_capex import models
    return {
        "asOf": dt.datetime.utcnow().isoformat() + "Z",
        "engine": 17,
        "engineName": "AI Capex Reality Engine",
        "source": "empty",
        "evidenceTotal": 0,
        "webEvidence": 0,
        "verdicts": [],
        "baskets": [],
        "summary": {"total": 0, "actionable": 0, "byLabel": {}, "byCategory": {}},
        "labels": models.LABEL_DISPLAY,
        "categories": {
            cid: {"name": meta.get("name"), "role": meta.get("role"), "blurb": meta.get("blurb")}
            for cid, meta in models.load_universe().get("categories", {}).items()
        },
        "cached": False,
        "note": "No scan built yet — run the nightly refresh job or POST /api/ai-capex/refresh.",
    }


@router.get("/api/ai-capex")
def get_scan(refresh: bool = Query(False, description="Force a full rebuild (heavy)")):
    """Return the current AI-capex scan (cached, or cheap rescore fallback)."""
    _require_enabled()
    try:
        from backend.ai_capex import pipeline, store

        if refresh:
            with _refresh_lock:
                return pipeline.build_scan(with_web_agent=True)

        cached = store.get_scan()
        if cached:
            cached["cached"] = True
            return cached

        rescored = pipeline.rescore_from_store()
        if rescored is not None:
            return rescored

        return _empty_payload()
    except HTTPException:
        raise
    except Exception as exc:
        LOG.exception("ai_capex: scan failed")
        raise HTTPException(status_code=500, detail=f"AI Capex scan failed: {exc}")


@router.post("/api/ai-capex/refresh")
def refresh_scan(
    web_agent: bool = Query(True, description="Include Tier-2 web sourcing (if enabled in flags)"),
):
    """Force a full rebuild (ingest + LLM extract + score). Serialised."""
    _require_enabled()
    with _refresh_lock:
        try:
            from backend.ai_capex import pipeline
            return pipeline.build_scan(with_web_agent=web_agent)
        except HTTPException:
            raise
        except Exception as exc:
            LOG.exception("ai_capex: refresh failed")
            raise HTTPException(status_code=500, detail=f"AI Capex refresh failed: {exc}")


@router.get("/api/ai-capex/evidence/{ticker}")
def get_evidence(ticker: str):
    """Per-ticker evidence audit trail (what the labels are built from)."""
    _require_enabled()
    try:
        from backend.ai_capex import store

        evid = store.get_evidence(ticker)
        return {
            "ticker": str(ticker).upper().strip(),
            "count": len(evid),
            "evidence": [e.to_dict() for e in evid],
        }
    except HTTPException:
        raise
    except Exception as exc:
        LOG.exception("ai_capex: evidence read failed")
        raise HTTPException(status_code=500, detail=f"AI Capex evidence read failed: {exc}")


@router.get("/api/ai-capex/universe")
def get_universe():
    """Taxonomy: categories, roles, and tickers."""
    _require_enabled()
    from backend.ai_capex import models

    uni = models.load_universe()
    return {
        "version": uni.get("version"),
        "updated": uni.get("updated"),
        "categories": uni.get("categories", {}),
        "secondOrderEdges": uni.get("second_order_edges", {}),
        "tickerCount": len(models.all_tickers()),
    }
