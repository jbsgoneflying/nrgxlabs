"""Engine 3 (backend) / Engine 4 (UI): Red Dog Reversal Scanner routes.

Backend module is named engine3 for historical reasons. Users see this as
Engine 4 in the navigation. See ENGINE_REGISTRY in config.py.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from backend.config import get_flags
from backend.deps import (
    LOG,
    get_client,
    get_client_optional,
    engine3_cache,
    engine3_cache_lock,
)
from backend.engine3_screener import (
    compute_engine3_scan,
    compute_single_ticker_scan,
    get_all_signals,
    refresh_signal_statuses,
    set_desk_status as set_engine3_desk_status,
)
from backend.gating import (
    gate_scan_results,
    summarize_gates,
    reconcile_red_dog_verdict,
    summarize_verdicts,
)
from backend.orats_client import OratsError

router = APIRouter()


def _get_gate_context(flags) -> dict:
    """Gather regime and vol context for gating decisions.

    Prefers Market Intelligence v2's canonical regime + vol state when
    available (single source of truth across E3/E4/E7), falls back to
    the raw Engine 5 snapshot if MI v2 is disabled or insufficient.
    """
    ctx: dict = {
        "regime_label": "",
        "vol_direction": "",
        "gamma_ctx": None,
        "high_events_within_days": 0,
    }

    # --- 1) Try MI v2 canonical regime (single source of truth) -------
    if getattr(flags, "ENABLE_MI_V2", True):
        try:
            from backend.market_intel import regime_snapshot
            mi = regime_snapshot()
            if mi.label:
                ctx["regime_label"] = mi.label
            vol_state = mi.vol_state or {}
            term = str(vol_state.get("term_structure", "")).lower()
            # Collapse term_structure into the legacy vol_direction vocabulary
            # the gating policy strings expect.
            if term == "backwardation":
                ctx["vol_direction"] = "rising"
            elif term == "contango":
                ctx["vol_direction"] = "falling"
            elif term == "flat":
                ctx["vol_direction"] = "stable"
        except Exception:
            pass

    # --- 2) Engine 5 snapshot fallback / supplement -------------------
    try:
        from backend.redis_store import get_store_optional

        store = get_store_optional()
        if store and flags.ENABLE_ENGINE5_LEAD_LAG:
            from backend.engine5_snapshot import select_best_snapshot

            snap = select_best_snapshot(
                store,
                max_age_days=flags.ENGINE5_SNAPSHOT_BEST_MAX_AGE_DAYS,
                snapshot_ttl=flags.ENGINE5_SNAPSHOT_TTL_S,
            )
            if snap:
                data = snap.get("data", {})
                regime = data.get("regime", {})
                if not ctx["regime_label"]:
                    ctx["regime_label"] = regime.get("label") or regime.get("current_label") or ""
                if not ctx["vol_direction"]:
                    vol = data.get("volLeadLag", {})
                    ctx["vol_direction"] = vol.get("global_vol_direction") or vol.get("globalVolDirection") or ""
    except Exception:
        pass
    return ctx


@router.get("/api/engine3-red-dog")
def engine3_red_dog_scan(
    request: Request,
    date: Optional[str] = Query(None, description="Scan date (YYYY-MM-DD), defaults to today"),
    min_score: int = Query(50, ge=0, le=100, description="Minimum score to include"),
    direction: Optional[str] = Query(None, description="Filter by direction: bullish, bearish, or both"),
):
    """
    Engine 3: Red Dog Reversal Scanner

    Scans SP500 + Nasdaq100 (516 tickers) for Red Dog Reversal setups with A+ quality scoring.

    Returns setups categorized by grade:
    - aPlus: Score >= 75 (high-quality setups)
    - standard: Score 50-74 (decent setups)
    - watchlist: Combined and sorted by score
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE3_RED_DOG:
        raise HTTPException(
            status_code=503,
            detail="Engine 3 (Red Dog Reversal) is disabled. Set ENABLE_ENGINE3_RED_DOG=1 to enable.",
        )

    try:
        client = get_client_optional()
        if client is None:
            raise HTTPException(status_code=503, detail="ORATS unavailable (missing ORATS_TOKEN).")

        dir_filter = None
        if direction:
            d = str(direction).strip().lower()
            if d in ("bullish", "bull", "long"):
                dir_filter = "bullish"
            elif d in ("bearish", "bear", "short"):
                dir_filter = "bearish"

        cache_key = (date, min_score, dir_filter)
        with engine3_cache_lock:
            cached = engine3_cache.get(cache_key)
        if cached is not None:
            return cached

        result = compute_engine3_scan(
            client,
            as_of_date=date,
            min_score=min_score,
            direction=dir_filter,
            max_workers=flags.ENGINE3_MAX_WORKERS,
            use_cache=True,
        )

        if flags.ENABLE_GATING and isinstance(result, dict):
            try:
                gate_ctx = _get_gate_context(flags)
                regime_allow = [s.strip() for s in str(getattr(flags, "GATE_RD_REGIME_ALLOW", "")).split(",") if s.strip()] or None
                vol_allow = [s.strip() for s in str(getattr(flags, "GATE_RD_VOL_STATE_ALLOW", "")).split(",") if s.strip()] or None
                for key in ("aPlus", "standard", "watchlist"):
                    setups = result.get(key)
                    if isinstance(setups, list):
                        gate_scan_results(
                            scan_results=setups,
                            engine="engine3_red_dog",
                            regime_allow=regime_allow,
                            vol_state_allow=vol_allow,
                            **gate_ctx,
                        )
                gs = summarize_gates(
                    (result.get("aPlus") or []) + (result.get("standard") or [])
                )
                result["gateSummary"] = gs
                result["gateContext"] = gate_ctx

                # Reconcile grade + gate + gamma + trend into ONE desk verdict
                # per signal, then summarize. This is what the UI leads with.
                gamma_ctx = result.get("marketGamma") or {}
                regime_label = gate_ctx.get("regime_label", "")
                for key in ("aPlus", "standard", "watchlist"):
                    setups = result.get(key)
                    if isinstance(setups, list):
                        for s in setups:
                            s["verdict"] = reconcile_red_dog_verdict(
                                s, gamma_ctx=gamma_ctx, regime_label=regime_label
                            )
                result["verdictSummary"] = summarize_verdicts(
                    (result.get("aPlus") or []) + (result.get("standard") or [])
                )
            except Exception as gate_err:
                LOG.warning(f"Gate injection failed for engine3: {gate_err}")

        with engine3_cache_lock:
            engine3_cache[cache_key] = result

        return result

    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure (engine3-red-dog)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (engine3-red-dog)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@router.get("/api/engine3-red-dog/status")
def engine3_red_dog_status(
    request: Request,
    refresh: bool = Query(False, description="Re-evaluate tracked signals against current prices"),
    date: Optional[str] = Query(None, description="As-of date for refresh (YYYY-MM-DD)"),
):
    """
    Engine 3: Red Dog signal-outcome tracker.

    Returns every tracked signal grouped by lifecycle status
    (pending / triggered / target_hit / stopped / expired / invalidated) plus a
    live forward win-rate. With ?refresh=true, walks each open signal forward
    against current bars and resolves its outcome.

    NOTE: declared before /{ticker} so the literal path isn't captured as a ticker.
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE3_RED_DOG:
        raise HTTPException(status_code=503, detail="Engine 3 (Red Dog) is disabled.")

    try:
        if refresh:
            client = get_client_optional()
            if client is None:
                raise HTTPException(status_code=503, detail="ORATS unavailable for refresh.")
            refresh_result = refresh_signal_statuses(client, as_of_date=date)
            return {"refreshed": True, **refresh_result, **get_all_signals()}
        return {"refreshed": False, **get_all_signals()}
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (engine3-red-dog/status)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@router.get("/api/engine3-red-dog/backtest")
def engine3_red_dog_backtest(
    request: Request,
    years: float = Query(3.0, ge=0.25, le=8.0, description="Lookback window in years"),
    min_score: float = Query(0.0, ge=0.0, le=100.0, description="Minimum score to include"),
    max_tickers: int = Query(40, ge=1, le=200, description="Universe sample cap"),
    tickers: Optional[str] = Query(None, description="Comma-separated tickers (defaults to a universe sample)"),
):
    """
    Engine 3: Red Dog backtest.

    Replays the pattern over `years` of history and reports win-rate, avg-R,
    expectancy and MAE/MFE — broken out by grade and by trend alignment.

    NOTE: declared before /{ticker} so the literal path isn't captured as a ticker.
    """
    import datetime as dt

    flags = get_flags()
    if not flags.ENABLE_ENGINE3_RED_DOG:
        raise HTTPException(status_code=503, detail="Engine 3 (Red Dog) is disabled.")

    try:
        client = get_client_optional()
        if client is None:
            raise HTTPException(status_code=503, detail="ORATS unavailable (missing ORATS_TOKEN).")

        from backend.engine3_backtest import backtest_red_dog
        from backend.universe import load_universe_sp500_and_nasdaq100

        if tickers:
            ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
        else:
            ticker_list = load_universe_sp500_and_nasdaq100()

        end = dt.date.today()
        start = end - dt.timedelta(days=int(years * 365))

        cache_key = ("backtest", round(years, 2), round(min_score, 1), max_tickers, tickers or "")
        with engine3_cache_lock:
            cached = engine3_cache.get(cache_key)
        if cached is not None:
            return cached

        result = backtest_red_dog(
            client,
            tickers=ticker_list,
            start=start,
            end=end,
            min_score=min_score,
            max_tickers=max_tickers,
        )

        with engine3_cache_lock:
            engine3_cache[cache_key] = result
        return result

    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure (engine3-red-dog/backtest)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (engine3-red-dog/backtest)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@router.post("/api/engine3-red-dog/track")
async def engine3_red_dog_track(request: Request):
    """Desk Trade Tracker override for Red Dog.

    Body: {ticker, status, signalDate?, note?, pinned?}
    `status` is one of watching/entered/working/broken/exited. Desk states
    survive scan refreshes and are never clobbered by the auto-evaluator, so
    the desk is never left holding a name the engine stopped surfacing.

    NOTE: declared before /{ticker} so the literal path isn't captured as a ticker.
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE3_RED_DOG:
        raise HTTPException(status_code=503, detail="Engine 3 (Red Dog) is disabled.")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")

    ticker = str(body.get("ticker") or "").strip().upper()
    status = str(body.get("status") or "").strip().lower()
    if not ticker or not status:
        raise HTTPException(status_code=400, detail="ticker and status are required.")

    try:
        result = set_engine3_desk_status(
            ticker,
            desk_status=status,
            signal_date=body.get("signalDate"),
            note=body.get("note"),
            pinned=body.get("pinned"),
        )
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "Could not update."))
        return {"ok": True, "record": result.get("record"), "signals": get_all_signals()}
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (engine3-red-dog/track)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@router.get("/api/engine3-red-dog/{ticker}")
def engine3_red_dog_ticker(
    request: Request,
    ticker: str,
    date: Optional[str] = Query(None, description="Scan date (YYYY-MM-DD), defaults to today"),
):
    """
    Engine 3: Single ticker Red Dog analysis

    Analyzes a specific ticker for Red Dog Reversal setup with full indicator details.
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE3_RED_DOG:
        raise HTTPException(
            status_code=503,
            detail="Engine 3 (Red Dog Reversal) is disabled. Set ENABLE_ENGINE3_RED_DOG=1 to enable.",
        )

    try:
        client = get_client_optional()
        if client is None:
            raise HTTPException(status_code=503, detail="ORATS unavailable (missing ORATS_TOKEN).")

        t = str(ticker or "").strip().upper()
        if not t:
            raise HTTPException(status_code=400, detail="Missing ticker.")

        result = compute_single_ticker_scan(
            client,
            ticker=t,
            as_of_date=date,
        )

        return result

    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception(f"ORATS failure (engine3-red-dog/{ticker})")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception(f"Unhandled failure (engine3-red-dog/{ticker})")
        raise HTTPException(status_code=500, detail="Internal error") from e
