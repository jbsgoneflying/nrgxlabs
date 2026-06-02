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
import pathlib
import subprocess
import sys
import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

from backend.config import get_flags

LOG = logging.getLogger("ai_capex")

router = APIRouter()

_refresh_lock = threading.Lock()
_bg_proc: Optional[subprocess.Popen] = None  # detached background rebuild, if any


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
    background: bool = Query(False, description="Spawn a detached rebuild and return immediately"),
    rescore: bool = Query(False, description="Cheap: re-apply scoring to stored evidence (no LLM/network)"),
):
    """Force a rebuild of the scan.

    - ``rescore=true``: re-score already-stored evidence + context with the
      current thresholds (instant, no LLM) — use after tuning scoring knobs.
    - ``background=true``: spawn ``scripts/refresh_ai_capex.py`` as a detached
      subprocess and return at once — the right way to kick the ~70-ticker LLM
      pass, which far exceeds the request timeout and would otherwise pin a
      gunicorn worker. Progress/health is readable via ``GET .../status``.
    - default (synchronous full rebuild): only safe for a small ``--tickers`` set.
    """
    _require_enabled()

    if rescore:
        from backend.ai_capex import pipeline
        out = pipeline.rescore_from_store()
        if out is None:
            raise HTTPException(status_code=409, detail="No stored evidence to rescore yet — run a full refresh first.")
        return out

    if background:
        global _bg_proc
        if _bg_proc is not None and _bg_proc.poll() is None:
            return {"status": "running", "pid": _bg_proc.pid}
        prev_rc = _bg_proc.poll() if _bg_proc is not None else None
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        script = repo_root / "scripts" / "refresh_ai_capex.py"
        cmd = [sys.executable, str(script)]
        if not web_agent:
            cmd.append("--no-web")
        try:
            _bg_proc = subprocess.Popen(
                cmd, cwd=str(repo_root),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,  # survive gunicorn worker recycling
            )
        except Exception as exc:
            LOG.exception("ai_capex: background refresh spawn failed")
            raise HTTPException(status_code=500, detail=f"spawn failed: {exc}")
        return {"status": "started", "pid": _bg_proc.pid, "webAgent": web_agent,
                "prevReturncode": prev_rc}

    with _refresh_lock:
        try:
            from backend.ai_capex import pipeline
            return pipeline.build_scan(with_web_agent=web_agent)
        except HTTPException:
            raise
        except Exception as exc:
            LOG.exception("ai_capex: refresh failed")
            raise HTTPException(status_code=500, detail=f"AI Capex refresh failed: {exc}")


@router.get("/api/ai-capex/status")
def get_status():
    """Last refresh-run health + whether a background rebuild is in flight."""
    _require_enabled()
    from backend.ai_capex import store

    bg_alive = _bg_proc is not None and _bg_proc.poll() is None
    return {
        "engine": 17,
        "lastRun": store.get_last_run(),
        "backgroundRunning": bg_alive,
        "backgroundPid": (_bg_proc.pid if bg_alive else None),
        "backgroundReturncode": (None if bg_alive or _bg_proc is None else _bg_proc.poll()),
    }


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
