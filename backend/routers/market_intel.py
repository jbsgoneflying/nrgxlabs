from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from typing import Any, Dict, Optional

from cachetools import TTLCache
from fastapi import APIRouter, Body, Header, HTTPException, Query

from backend.deps import (
    LOG,
    get_client,
    get_client_optional,
    get_benzinga_client_optional,
    get_fmp_client_optional,
    get_api_ninjas_client_optional,
    condor_rank_cache,
    condor_rank_cache_lock,
    macro_stats_cache,
    macro_stats_cache_lock,
)
from backend.config import get_flags
from backend.orats_client import OratsError
from backend.earnings_logic import BreachInputError
from backend.condor_rank import compute_condor_rank
from backend.macro_event_stats import compute_macro_event_stats

router = APIRouter()

_news_risk_cache: TTLCache = TTLCache(maxsize=10, ttl=30 * 60)
_news_risk_cache_lock = threading.Lock()


@router.get("/api/condor-rank")
def condor_rank(
    ticker: str = Query(..., description="US equity ticker"),
    n: int = Query(20, ge=5, le=50),
    years: int = Query(5, ge=1, le=10),
):
    """
    Iron Condor Rank endpoint (lightweight, cached).
    """
    try:
        t = str(ticker or "").strip().upper()
        key = (t, int(n), int(years), get_flags().cache_fingerprint())
        with condor_rank_cache_lock:
            cached = condor_rank_cache.get(key)
        if cached is not None:
            return cached

        payload = compute_condor_rank(get_client(), ticker=t, n=int(n), years=int(years))
        with condor_rank_cache_lock:
            condor_rank_cache[key] = payload
        return payload
    except BreachInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure (condor-rank)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (condor-rank)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@router.get("/api/macro-event-stats")
def macro_event_stats(
    key: str = Query(..., description="Macro event key (e.g., CPI, FOMC_RATE_DECISION, NFP)"),
    lookback_years: int = Query(5, ge=1, le=10),
    max_events: int = Query(60, ge=10, le=200),
):
    """
    On-demand macro event reaction stats (risk-only).
    Uses Benzinga economics history + SPY close-to-close returns.
    Cached to avoid repeated computation.
    """
    try:
        k = str(key or "").strip().upper()
        if not k:
            raise HTTPException(status_code=400, detail="Missing key.")
        cache_key = (k, int(lookback_years), int(max_events))
        with macro_stats_cache_lock:
            cached = macro_stats_cache.get(cache_key)
        if cached is not None:
            return cached

        bz = get_benzinga_client_optional()
        if bz is None:
            raise HTTPException(status_code=503, detail="Benzinga unavailable or disabled.")
        client = get_client_optional()
        if client is None:
            raise HTTPException(status_code=503, detail="ORATS unavailable (missing ORATS_TOKEN).")

        payload = compute_macro_event_stats(
            key=k,
            bz=bz,
            orats=client,
            lookback_years=int(lookback_years),
            max_events=int(max_events),
        )
        with macro_stats_cache_lock:
            macro_stats_cache[cache_key] = payload
        return payload
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (macro-event-stats)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@router.get("/api/news-risk")
def news_risk(
    week_offset: int = Query(0, ge=-12, le=12, description="Week offset: 0=current, 1=next, -1=last"),
):
    """
    News Risk Engine: Weekly view of macro events, analyst ratings, and news headlines
    with historical SPX impact data for event risk planning.
    """
    from backend.news_risk import build_news_risk_payload

    try:
        cache_key = ("news_risk", int(week_offset))
        with _news_risk_cache_lock:
            cached = _news_risk_cache.get(cache_key)
        if cached is not None:
            return cached

        bz = get_benzinga_client_optional()
        if bz is None:
            raise HTTPException(status_code=503, detail="Benzinga unavailable or disabled.")

        orats = get_client_optional()
        if orats is None:
            raise HTTPException(status_code=503, detail="ORATS unavailable (missing ORATS_TOKEN).")

        payload = build_news_risk_payload(
            bz=bz,
            orats=orats,
            week_offset=int(week_offset),
        )

        with _news_risk_cache_lock:
            _news_risk_cache[cache_key] = payload
        return payload
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (news-risk)")
        raise HTTPException(status_code=500, detail="Internal error") from e


# ---------------------------------------------------------------------------
# Market Intelligence v2 endpoints
# ---------------------------------------------------------------------------


def _mi_v2_enabled() -> bool:
    try:
        flags = get_flags()
        return bool(getattr(flags, "ENABLE_MI_V2", True))
    except Exception:
        return True


def _mi_admin_ok(supplied: Optional[str]) -> bool:
    expected = (
        os.getenv("MI_ADMIN_TOKEN", "").strip()
        or os.getenv("DESK_INSIGHT_ADMIN_TOKEN", "").strip()
        or os.getenv("ENGINE15_ADMIN_TOKEN", "").strip()
        or os.getenv("ENGINE14_ADMIN_TOKEN", "").strip()
    )
    if not expected:
        return False
    return bool(supplied) and str(supplied).strip() == expected


@router.get("/api/market-intel/health")
def mi_v2_health() -> Dict[str, Any]:
    """Public health probe: which model is serving + data knobs."""
    if not _mi_v2_enabled():
        return {"enabled": False, "source": "disabled"}
    try:
        from backend.market_intel import service_health
        health = service_health()
    except Exception as e:
        return {"enabled": True, "error": f"{type(e).__name__}: {e}"}
    return {"enabled": True, **health}


@router.get("/api/market-intel/regime")
def mi_v2_regime_endpoint(
    force_refresh: bool = Query(False, description="Bypass the 5-min cache"),
) -> Dict[str, Any]:
    """Return the canonical RegimeSnapshot for today. Public."""
    if not _mi_v2_enabled():
        raise HTTPException(status_code=404, detail="Market Intelligence v2 disabled")
    try:
        from backend.market_intel import regime_snapshot
        snap = regime_snapshot(force_refresh=force_refresh)
        return snap.to_dict()
    except Exception as e:
        LOG.exception("mi_v2_regime_endpoint failed")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@router.post("/api/market-intel/calibrate")
def mi_v2_calibrate_endpoint(
    body: Dict[str, Any] = Body(default_factory=dict),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
) -> Dict[str, Any]:
    """Admin-gated: refit the HMM on the latest 5y of factor history."""
    if not _mi_v2_enabled():
        raise HTTPException(status_code=404, detail="Market Intelligence v2 disabled")
    if not _mi_admin_ok(x_admin_token):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing X-Admin-Token "
                   "(MI_ADMIN_TOKEN / DESK_INSIGHT_ADMIN_TOKEN / ENGINE*_ADMIN_TOKEN)."
        )
    try:
        # Compare/dry-run: train a gamma-aware candidate model WITHOUT writing
        # it, and return a side-by-side validation diff vs the live model on
        # known stress/calm windows. Use this to vet a retrain before swapping.
        if bool(body.get("compare", False)):
            from backend.market_intel.calibration import run_calibration_compare
            lookback = int(body.get("lookback_days") or 1900)
            return run_calibration_compare(lookback_days=lookback)

        from backend.market_intel.calibration import run_calibration
        lookback = int(body.get("lookback_days") or 1260)
        persist = bool(body.get("persist", True))
        report = run_calibration(lookback_days=lookback, persist=persist)
        return report.to_dict()
    except Exception as e:
        LOG.exception("mi_v2_calibrate failed")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@router.get("/api/market-intel/diff")
def mi_v2_diff_endpoint(
    target_date: Optional[str] = Query(None, alias="date"),
) -> Dict[str, Any]:
    """Return the day-over-day intelligence diff.

    If ``date`` is omitted, uses today vs the most recent prior day in
    the DMS Redis index.
    """
    if not _mi_v2_enabled():
        raise HTTPException(status_code=404, detail="Market Intelligence v2 disabled")
    try:
        from backend.market_intel import compute_market_diff
        from backend.daily_market_state import load_dms, load_dms_history
        from backend.redis_store import get_store_optional
        store = get_store_optional()
        history = load_dms_history(store, n=3) if store else []
        if len(history) < 2:
            return {
                "has_changes": False,
                "headline_summary": "Insufficient history for diff.",
                "from_date": "",
                "to_date": history[0].date if history else "",
            }
        today_dms = history[0].to_dict()
        yesterday_dms = history[1].to_dict()
        return compute_market_diff(
            today_dms=today_dms,
            yesterday_dms=yesterday_dms,
        ).to_dict()
    except Exception as e:
        LOG.exception("mi_v2_diff failed")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
