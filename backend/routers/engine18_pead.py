"""Engine 18 — Earnings Drift (PEAD) — API routes.

Endpoints:
- ``GET  /api/engine18``                  — current scan (Redis-cached; rescore fallback).
- ``POST /api/engine18/refresh``          — rebuild (``?background=true`` spawns the script).
- ``POST /api/engine18/profile``          — manual on-demand PEAD profile for one ticker.
- ``GET  /api/engine18/status``           — last run health + background-process state.
- ``GET  /api/engine18/evidence/{t}``     — per-ticker evidence (report + grades + excerpt).
- ``POST /api/engine18/advisor``          — narrative-only LLM desk note.
- ``POST /api/engine18/trade``            — log a tracked drift trade.
- ``GET  /api/engine18/trades``           — list tracked trades.
- ``POST /api/engine18/trade/{id}/close`` — close with outcome.
- ``POST /api/engine18/trade/{id}/checkin`` — append a check-in note.

The GET path is cheap: it serves the Redis snapshot written by the morning
cron (``scripts/refresh_engine18.py``, 12:45 UTC). The heavy ingest+LLM build
only runs on /refresh and in cron, never stampeding the request path.
"""
from __future__ import annotations

import logging
import pathlib
import subprocess
import sys
import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from backend.config import get_flags

LOG = logging.getLogger("engine18")

router = APIRouter()

_refresh_lock = threading.Lock()
_bg_proc: Optional[subprocess.Popen] = None


def _require_enabled() -> None:
    if not getattr(get_flags(), "ENABLE_ENGINE18", False):
        raise HTTPException(status_code=404, detail="Engine 18 (Earnings Drift) is disabled")


def _empty_payload() -> Dict[str, Any]:
    from backend.engine18.models import candidates_to_payload

    payload = candidates_to_payload([])
    payload["cached"] = False
    payload["note"] = "No scan built yet — the 12:45 UTC cron or POST /api/engine18/refresh populates it."
    return payload


def _attach_validation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the latest continuous-validation record into the served payload."""
    try:
        from backend.engine18 import store

        payload["validation"] = store.get_validation()
    except Exception:
        payload["validation"] = None
    return payload


@router.get("/api/engine18")
def get_scan():
    """Current Earnings-Drift scan (cached snapshot, or cheap rescore fallback)."""
    _require_enabled()
    try:
        from backend.engine18 import pipeline, store

        cached = store.get_scan()
        if cached:
            cached["cached"] = True
            return _attach_validation(cached)

        rescored = pipeline.rescore_from_store()
        if rescored is not None:
            return _attach_validation(rescored)

        return _empty_payload()
    except HTTPException:
        raise
    except Exception as exc:
        LOG.exception("engine18: scan read failed")
        raise HTTPException(status_code=500, detail=f"Engine 18 scan failed: {exc}")


@router.post("/api/engine18/refresh")
def refresh_scan(
    background: bool = Query(False, description="Spawn a detached rebuild and return immediately"),
    rescore: bool = Query(False, description="Cheap: re-score stored evidence (no network/LLM)"),
):
    """Force a rebuild of the scan.

    - ``rescore=true``: re-apply scoring knobs to stored evidence (instant).
    - ``background=true``: spawn ``scripts/refresh_engine18.py`` detached — the
      right way to kick a full ingest+LLM pass from the UI.
    - default: synchronous full rebuild (the universe-calendar pass is one
      EODHD call; LLM grading covers only qualifying beats, typically a handful).
    """
    _require_enabled()

    if rescore:
        from backend.engine18 import pipeline

        out = pipeline.rescore_from_store()
        if out is None:
            raise HTTPException(status_code=409, detail="No stored scan to rescore yet — run a full refresh first.")
        return out

    if background:
        global _bg_proc
        if _bg_proc is not None and _bg_proc.poll() is None:
            return {"status": "running", "pid": _bg_proc.pid}
        prev_rc = _bg_proc.poll() if _bg_proc is not None else None
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        script = repo_root / "scripts" / "refresh_engine18.py"
        try:
            _bg_proc = subprocess.Popen(
                [sys.executable, str(script)],
                cwd=str(repo_root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # survive gunicorn worker recycling
            )
        except Exception as exc:
            LOG.exception("engine18: background refresh spawn failed")
            raise HTTPException(status_code=500, detail=f"spawn failed: {exc}")
        return {"status": "started", "pid": _bg_proc.pid, "prevReturncode": prev_rc}

    with _refresh_lock:
        try:
            from backend.engine18 import pipeline

            return _attach_validation(pipeline.build_scan())
        except HTTPException:
            raise
        except Exception as exc:
            LOG.exception("engine18: refresh failed")
            raise HTTPException(status_code=500, detail=f"Engine 18 refresh failed: {exc}")


@router.post("/api/engine18/profile")
async def run_profile(request: Request):
    """On-demand manual PEAD profile for one ticker (the hybrid desk path).

    Body: ``{ticker, actual_eps?, estimate_eps?, report_date?, timing?}`` —
    the EPS fields are a last-resort override for when vendors lag the print
    (the ORCL case). Qualifying candidates merge into the scan tagged
    ``origin="manual"``; non-qualifying reports return an explicit verdict.
    """
    _require_enabled()
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    ticker = str(body.get("ticker") or "").strip().upper()
    if not ticker or len(ticker) > 10 or not all(c.isalnum() or c in ".-" for c in ticker):
        raise HTTPException(status_code=422, detail="A valid ticker is required (e.g. ORCL)")

    overrides = {
        k: body.get(k)
        for k in ("actual_eps", "estimate_eps", "report_date", "timing")
        if body.get(k) not in (None, "")
    }

    with _refresh_lock:
        try:
            from backend.engine18 import pipeline

            return {"engine": 18, "ticker": ticker, **pipeline.build_profile(ticker, overrides=overrides)}
        except HTTPException:
            raise
        except Exception as exc:
            LOG.exception("engine18: manual profile failed for %s", ticker)
            raise HTTPException(status_code=500, detail=f"Engine 18 profile failed: {exc}")


@router.get("/api/engine18/status")
def get_status():
    """Last refresh-run health + whether a background rebuild is in flight."""
    _require_enabled()
    from backend.engine18 import store

    bg_alive = _bg_proc is not None and _bg_proc.poll() is None
    return {
        "engine": 18,
        "lastRun": store.get_last_run(),
        "validation": store.get_validation(),
        "backgroundRunning": bg_alive,
        "backgroundPid": (_bg_proc.pid if bg_alive else None),
        "backgroundReturncode": (None if bg_alive or _bg_proc is None else _bg_proc.poll()),
    }


@router.get("/api/engine18/evidence/{ticker}")
def get_evidence(ticker: str):
    """Per-ticker evidence: report, both grades, transcript excerpt."""
    _require_enabled()
    try:
        from backend.engine18 import store

        evid = store.get_evidence(ticker)
        return {
            "ticker": str(ticker).upper().strip(),
            "found": evid is not None,
            "evidence": evid,
        }
    except HTTPException:
        raise
    except Exception as exc:
        LOG.exception("engine18: evidence read failed")
        raise HTTPException(status_code=500, detail=f"Engine 18 evidence read failed: {exc}")


@router.post("/api/engine18/advisor")
async def drift_advisor(request: Request):
    """Narrative-only LLM desk note over the current scan + open trades."""
    _require_enabled()
    flags = get_flags()
    try:
        body = await request.json()
    except Exception:
        body = {}

    scan_payload = body.get("scanPayload")
    if not isinstance(scan_payload, dict) or not scan_payload.get("candidates"):
        from backend.engine18 import store

        scan_payload = store.get_scan() or _empty_payload()

    from backend.engine18 import trades as e18_trades
    from backend.engine18.advisor import generate_drift_advisor

    try:
        result = generate_drift_advisor(
            scan_payload,
            open_trades=e18_trades.list_trades(status="active"),
            model=str(flags.ENGINE18_MODEL),
        )
        return {"engine": 18, "advisor": result}
    except Exception as exc:
        LOG.exception("engine18: advisor failed")
        raise HTTPException(status_code=500, detail=f"Engine 18 advisor error: {type(exc).__name__}")


@router.get("/api/engine18/options/{ticker}")
def options_expression(ticker: str):
    """Informational options expression (long ~40Δ / short ~20Δ ~3-week call spread).

    NOT backtested — the validated edge is the equity drift. The payload
    carries an explicit disclaimer the UI must surface.
    """
    _require_enabled()
    try:
        from backend.engine18.options_card import suggest_call_spread

        card = suggest_call_spread(ticker)
        return {
            "ticker": str(ticker).upper().strip(),
            "available": card is not None,
            "card": card,
        }
    except HTTPException:
        raise
    except Exception as exc:
        LOG.exception("engine18: options expression failed")
        raise HTTPException(status_code=500, detail=f"Engine 18 options expression failed: {exc}")


# ---------------------------------------------------------------------------
# Trade tracker
# ---------------------------------------------------------------------------

@router.post("/api/engine18/trade")
async def log_trade(request: Request):
    """Log a tracked drift trade (entry snapshot + 10-trading-day countdown)."""
    _require_enabled()
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or not str(body.get("ticker") or "").strip():
        raise HTTPException(status_code=400, detail="ticker is required")

    from backend.engine18 import trades as e18_trades

    trade_id = e18_trades.log_trade(body)
    if trade_id is None:
        raise HTTPException(status_code=503, detail="Trade persistence unavailable (Redis down?)")
    return {"tradeId": trade_id, "status": "active"}


@router.get("/api/engine18/trades")
def list_trades(status: Optional[str] = Query(None, description="active | closed")):
    """All tracked drift trades, newest first."""
    _require_enabled()
    from backend.engine18 import trades as e18_trades

    rows = e18_trades.list_trades(status=status)
    return {"count": len(rows), "trades": rows}


@router.post("/api/engine18/trade/{trade_id}/close")
async def close_trade(trade_id: str, request: Request):
    """Close a tracked trade with outcome data (exit price/date, reason)."""
    _require_enabled()
    try:
        body = await request.json()
    except Exception:
        body = {}

    from backend.engine18 import trades as e18_trades

    doc = e18_trades.close_trade(trade_id, body if isinstance(body, dict) else {})
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")
    return doc


@router.post("/api/engine18/trade/{trade_id}/checkin")
async def trade_checkin(trade_id: str, request: Request):
    """Append a check-in note to a tracked trade."""
    _require_enabled()
    try:
        body = await request.json()
    except Exception:
        body = {}

    from backend.engine18 import trades as e18_trades

    doc = e18_trades.add_checkin(trade_id, body if isinstance(body, dict) else {})
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")
    return doc
