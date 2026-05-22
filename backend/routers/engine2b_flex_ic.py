"""Engine 2b — Flex-Expiry SPX Iron Condor route.

Sibling of ``/api/spx-ic``. Lets the desk evaluate any SPX/SPXW expiration
(not just the same-week Friday) with the full Engine 2 odds stack. The
main ``/api/spx-ic`` route is intentionally untouched; if this router is
disabled (``ENABLE_E2B_FLEX_EXPIRY=0``), it 404s and the desk's Friday
flow keeps working unchanged.
"""
from __future__ import annotations

import datetime as dt
import threading
from typing import Any, Dict, Optional

from cachetools import TTLCache
from fastapi import APIRouter, Body, HTTPException, Query

from backend.config import get_flags
from backend.deps import (
    LOG,
    get_benzinga_client_optional,
    get_client,
)
from backend.engine2b import compute_engine2b_flex_ic
from backend.market_hours import is_us_equity_market_open
from backend.orats_client import OratsError

router = APIRouter()


# Dedicated cache so flex requests don't evict Friday-engine entries.
flex_cache: TTLCache = TTLCache(maxsize=128, ttl=30 * 60)
flex_cache_lock = threading.Lock()


def _flex_cache_key(params: Dict[str, Any], flags_fp: tuple) -> tuple:
    items = tuple(sorted((k, str(v)) for k, v in (params or {}).items()))
    return ("spx_ic_flex", items, flags_fp)


def _parse_iso_date(s: str, *, field: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{field} must be YYYY-MM-DD ({e})") from e


@router.get("/api/spx-ic/flex")
def spx_ic_flex(
    underlying: str = Query("SPX", description="Underlying: SPX|SPY|QQQ"),
    entry_date: str = Query(..., description="Entry close date (YYYY-MM-DD). Must be a trading day."),
    expiry: str = Query(..., description="Target expiry date (YYYY-MM-DD). Must be after entry_date."),
    years: int = Query(2, ge=1, le=5, description="Lookback for historical analogues."),
    widths: str = Query(
        "1.0,1.5,2.0,2.5",
        description="Comma-separated EM-multiple grid. Default includes 2.5× for holiday-weekend trades.",
    ),
    risk_target_breach_pct: float = Query(25.0, gt=0.0, le=100.0),
    include_live_chain: bool = Query(True, description="Pull live SPXW chain for the requested expiry and project per-EM strike credits."),
):
    flags = get_flags()
    if not flags.ENABLE_ENGINE2_SPX_IC:
        raise HTTPException(status_code=404, detail="Engine 2 disabled (ENABLE_ENGINE2_SPX_IC=0).")
    if not bool(getattr(flags, "ENABLE_E2B_FLEX_EXPIRY", False)):
        raise HTTPException(status_code=404, detail="Engine 2b flex-expiry disabled (ENABLE_E2B_FLEX_EXPIRY=0).")

    under = str(underlying or "SPX").strip().upper()
    if under not in ("SPX", "SPY", "QQQ"):
        raise HTTPException(status_code=400, detail="underlying must be SPX|SPY|QQQ")

    entry_dt = _parse_iso_date(entry_date, field="entry_date")
    expiry_dt = _parse_iso_date(expiry, field="expiry")
    if expiry_dt <= entry_dt:
        raise HTTPException(status_code=400, detail="expiry must be strictly after entry_date.")
    if (expiry_dt - entry_dt).days > 60:
        raise HTTPException(status_code=400, detail="Flex span > 60 calendar days is not supported (use Engine 2 for monthly/quarterly).")

    try:
        ws = []
        for part in str(widths).split(","):
            p = part.strip()
            if not p:
                continue
            ws.append(float(p))
        ws = [w for w in ws if w > 0]
        ws = sorted(list(dict.fromkeys(ws))) if ws else [1.0, 1.5, 2.0, 2.5]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"widths must be a comma-separated list of positive floats ({e})") from e

    params = {
        "underlying": under,
        "entry_date": entry_dt.isoformat(),
        "expiry": expiry_dt.isoformat(),
        "years": years,
        "widths": ",".join(str(w) for w in ws),
        "risk_target_breach_pct": risk_target_breach_pct,
        "include_live_chain": bool(include_live_chain),
    }
    cache_enabled = not is_us_equity_market_open()
    key = _flex_cache_key(params, flags.cache_key_engine2())
    if cache_enabled:
        with flex_cache_lock:
            cached = flex_cache.get(key)
        if cached is not None:
            return cached

    try:
        payload = compute_engine2b_flex_ic(
            client=get_client(),
            benzinga_client=get_benzinga_client_optional(),
            flags=flags,
            underlying_preference=under,
            entry_date=entry_dt,
            expiry_date=expiry_dt,
            years=int(years),
            widths=ws,
            risk_target_breach_pct=float(risk_target_breach_pct),
            include_live_chain=bool(include_live_chain),
        )
        payload["schemaVersion"] = 1
        payload["updatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        if cache_enabled:
            with flex_cache_lock:
                flex_cache[key] = payload
        return payload
    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure (spx-ic/flex)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (spx-ic/flex)")
        raise HTTPException(status_code=500, detail="Internal error") from e


# ═════════════════════════════════════════════════════════════════════════
# Flex AI Advisor — POST /api/spx-ic/flex/advisor
# ═════════════════════════════════════════════════════════════════════════

@router.post("/api/spx-ic/flex/advisor")
def spx_ic_flex_advisor(
    body: Optional[Dict[str, Any]] = Body(default=None),
    underlying: str = Query("SPX"),
    entry_date: Optional[str] = Query(None),
    expiry: Optional[str] = Query(None),
    years: int = Query(2, ge=1, le=5),
    widths: str = Query("1.0,1.5,2.0,2.5"),
    risk_target_breach_pct: float = Query(25.0, gt=0.0, le=100.0),
    include_live_chain: bool = Query(True),
):
    """Run the flex-expiry LLM trade advisor.

    Two call modes:

    1. Pass a pre-computed flex payload in ``body`` (must contain
       ``flexExpiry``). This is what the frontend uses after a
       ``runFlex()`` call so we don't double-fetch ORATS.
    2. Pass query params (``entry_date`` / ``expiry``) and the route
       re-runs ``compute_engine2b_flex_ic`` then advises. Useful for
       headless backtesting / curl.
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE2_SPX_IC:
        raise HTTPException(status_code=404, detail="Engine 2 disabled (ENABLE_ENGINE2_SPX_IC=0).")
    if not bool(getattr(flags, "ENABLE_E2B_FLEX_EXPIRY", False)):
        raise HTTPException(status_code=404, detail="Engine 2b flex-expiry disabled (ENABLE_E2B_FLEX_EXPIRY=0).")
    if not getattr(flags, "ENGINE2_ADVISOR_ENABLED", False):
        raise HTTPException(status_code=404, detail="Engine 2 advisor disabled (ENGINE2_ADVISOR_ENABLED=0).")

    payload: Dict[str, Any]
    if body and isinstance(body, dict) and body.get("flexExpiry"):
        payload = body
    else:
        if not entry_date or not expiry:
            raise HTTPException(
                status_code=400,
                detail="Either POST a flex payload (with flexExpiry) or pass entry_date + expiry query params.",
            )
        entry_dt = _parse_iso_date(entry_date, field="entry_date")
        expiry_dt = _parse_iso_date(expiry, field="expiry")
        if expiry_dt <= entry_dt:
            raise HTTPException(status_code=400, detail="expiry must be strictly after entry_date.")
        under = str(underlying or "SPX").strip().upper()
        if under not in ("SPX", "SPY", "QQQ"):
            raise HTTPException(status_code=400, detail="underlying must be SPX|SPY|QQQ")
        try:
            ws = [float(p.strip()) for p in str(widths).split(",") if p.strip()]
            ws = sorted({w for w in ws if w > 0}) or [1.0, 1.5, 2.0, 2.5]
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"widths bad ({e})") from e
        try:
            payload = compute_engine2b_flex_ic(
                client=get_client(),
                benzinga_client=get_benzinga_client_optional(),
                flags=flags,
                underlying_preference=under,
                entry_date=entry_dt,
                expiry_date=expiry_dt,
                years=int(years),
                widths=ws,
                risk_target_breach_pct=float(risk_target_breach_pct),
                include_live_chain=bool(include_live_chain),
            )
        except OratsError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    try:
        from backend.engine2b.advisor import generate_flex_advisor
        advisor = generate_flex_advisor(payload, flags=flags)
    except Exception as e:
        LOG.exception("Engine 2b flex advisor failure")
        raise HTTPException(status_code=500, detail=f"Flex advisor error: {type(e).__name__}: {e}") from e

    return {
        "advisor": advisor,
        "flexExpiry": payload.get("flexExpiry"),
        "flexAnalytics": payload.get("flexAnalytics"),
        "liveChain": payload.get("liveChain"),
        "weekendStress": payload.get("weekendStress"),
        "expectedMove": payload.get("expectedMove"),
        "deskConsensus": payload.get("deskConsensus"),
        "current": payload.get("current"),
        "underlying": payload.get("underlying"),
        "asOfDate": payload.get("asOfDate"),
    }
