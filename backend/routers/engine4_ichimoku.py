"""Engine 4 (backend) / Engine 5 (UI): Ichimoku Cloud Continuation Scanner routes.

Backend module is named engine4 for historical reasons. Users see this as
Engine 5 in the navigation. See ENGINE_REGISTRY in config.py.
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
    get_benzinga_client_optional,
    engine4_cache,
    engine4_cache_lock,
)
from backend.engine4_screener import (
    run_universe_scan as compute_engine4_scan,
    scan_single_ticker as compute_engine4_single_ticker,
    get_all_signals as get_engine4_signals,
    refresh_signal_statuses as refresh_engine4_statuses,
    set_desk_status as set_engine4_desk_status,
)
from backend.gating import (
    gate_scan_results,
    summarize_gates,
    reconcile_ichimoku_verdict,
    summarize_verdicts,
)
from backend.orats_client import OratsError

router = APIRouter()


def _get_gate_context(flags) -> dict:
    """Gather regime and vol context for gating decisions.

    Mirrors backend/routers/engine3_red_dog.py::_get_gate_context — both
    consume the same canonical Market Intelligence v2 regime so E3 + E4
    gating can never silently disagree.
    """
    ctx: dict = {
        "regime_label": "",
        "vol_direction": "",
        "gamma_ctx": None,
        "high_events_within_days": 0,
    }

    # --- 1) Canonical MI v2 regime ------------------------------------
    if getattr(flags, "ENABLE_MI_V2", True):
        try:
            from backend.market_intel import regime_snapshot
            mi = regime_snapshot()
            if mi.label:
                ctx["regime_label"] = mi.label
            term = str((mi.vol_state or {}).get("term_structure", "")).lower()
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


@router.get("/api/engine4-ichimoku")
def engine4_ichimoku_scan(
    request: Request,
    date: Optional[str] = Query(None, description="Scan date (YYYY-MM-DD), defaults to today"),
    min_score: int = Query(50, ge=0, le=100, description="Minimum score to include"),
    direction: Optional[str] = Query(None, description="Filter by direction: bullish, bearish, or both"),
):
    """
    Engine 4: Ichimoku Cloud Continuation Scanner

    Scans SP500 + Nasdaq100 for Ichimoku continuation setups (Kijun pullback + Tenkan reclaim)
    with A+ quality scoring.

    Returns setups categorized by grade:
    - aPlus: Score >= 75 (high-quality setups)
    - others: Score 50-74 (decent setups)

    Features:
    - Standard Ichimoku settings (9/26/52)
    - Trend qualification (price vs cloud, Kijun slope)
    - Pullback detection (past Tenkan, near Kijun)
    - Entry triggers (Tenkan reclaim with candle quality)
    - Dealer gamma context (SPX for S&P, NDX for Nasdaq)
    - Earnings filter (downgrade if within 5 sessions)
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE4_ICHIMOKU:
        raise HTTPException(
            status_code=503,
            detail="Engine 4 (Ichimoku Continuation) is disabled. Set ENABLE_ENGINE4_ICHIMOKU=1 to enable.",
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
        with engine4_cache_lock:
            cached = engine4_cache.get(cache_key)
        if cached is not None:
            return cached

        benzinga_client = get_benzinga_client_optional()

        result = compute_engine4_scan(
            client,
            as_of_date=date,
            min_score=min_score,
            direction=dir_filter,
            benzinga_client=benzinga_client,
            max_workers=flags.ENGINE4_MAX_WORKERS,
        )

        if flags.ENABLE_GATING and isinstance(result, dict):
            try:
                gate_ctx = _get_gate_context(flags)
                regime_allow = [s.strip() for s in str(flags.GATE_ICH_REGIME_ALLOW).split(",") if s.strip()]
                regime_allow_short = [s.strip() for s in str(flags.GATE_ICH_REGIME_ALLOW_SHORT).split(",") if s.strip()]
                vol_state_allow = [s.strip() for s in str(flags.GATE_ICH_VOL_STATE_ALLOW).split(",") if s.strip()]
                for key in ("actionable", "structure", "watchlist"):
                    setups = result.get(key)
                    if isinstance(setups, list):
                        gate_scan_results(
                            scan_results=setups,
                            engine="engine4_ichimoku",
                            regime_allow=regime_allow,
                            regime_allow_short=regime_allow_short,
                            vol_state_allow=vol_state_allow,
                            **gate_ctx,
                        )
                gs = summarize_gates(
                    (result.get("actionable") or []) + (result.get("structure") or [])
                )
                result["gateSummary"] = gs
                result["gateContext"] = gate_ctx

                # Reconcile grade + freshness + gate + gamma into one
                # continuation verdict per name, and lead the card with it.
                market_gamma = result.get("marketGamma", {}) if isinstance(result.get("marketGamma"), dict) else {}
                regime_label = gate_ctx.get("regime_label", "")
                for key in ("actionable", "structure", "watchlist"):
                    setups = result.get(key)
                    if not isinstance(setups, list):
                        continue
                    for sig in setups:
                        membership = str(sig.get("indexMembership") or "sp500")
                        gctx = market_gamma.get("ndx") if membership == "nasdaq100" else market_gamma.get("spx")
                        sig["verdict"] = reconcile_ichimoku_verdict(
                            sig, gamma_ctx=gctx, regime_label=regime_label
                        )
                result["verdictSummary"] = summarize_verdicts(
                    (result.get("actionable") or []) + (result.get("structure") or [])
                )
            except Exception as gate_err:
                LOG.warning(f"Gate injection failed for engine4: {gate_err}")

        with engine4_cache_lock:
            engine4_cache[cache_key] = result

        return result

    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure (engine4-ichimoku)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (engine4-ichimoku)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@router.get("/api/engine4-ichimoku/status")
def engine4_ichimoku_status(
    request: Request,
    refresh: bool = Query(False, description="Refresh signal statuses against current prices"),
    date: Optional[str] = Query(None, description="As-of date for refresh (YYYY-MM-DD)"),
):
    """
    Engine 4: Signal Status Tracker

    Returns current status of all tracked Ichimoku signals.

    If refresh=True, updates signal statuses based on current price action:
    - Checks if entry triggers have been hit
    - Checks if stops have been hit
    - Marks invalidated signals
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE4_ICHIMOKU:
        raise HTTPException(
            status_code=503,
            detail="Engine 4 (Ichimoku Continuation) is disabled.",
        )

    try:
        if refresh:
            client = get_client_optional()
            if client is None:
                raise HTTPException(status_code=503, detail="ORATS unavailable for refresh.")

            refresh_result = refresh_engine4_statuses(client, as_of_date=date)
            return {
                "refreshed": True,
                **refresh_result,
                "signals": get_engine4_signals(),
            }

        return {
            "refreshed": False,
            "signals": get_engine4_signals(),
        }

    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (engine4-ichimoku/status)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@router.post("/api/engine4-ichimoku/track")
async def engine4_ichimoku_track(request: Request):
    """Desk Trade Tracker override.

    Body: {ticker, status, signalDate?, note?, pinned?}
    `status` is one of watching/entered/working/broken/exited. Desk states
    survive scan refreshes and are never clobbered by the auto-evaluator.
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE4_ICHIMOKU:
        raise HTTPException(status_code=503, detail="Engine 4 (Ichimoku Continuation) is disabled.")

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
        result = set_engine4_desk_status(
            ticker,
            desk_status=status,
            signal_date=body.get("signalDate"),
            note=body.get("note"),
            pinned=body.get("pinned"),
        )
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "Could not update."))
        return {"ok": True, "record": result.get("record"), "signals": get_engine4_signals()}
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (engine4-ichimoku/track)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@router.get("/api/engine4-ichimoku/backtest")
def engine4_ichimoku_backtest(
    request: Request,
    start: Optional[str] = Query(None, description="Backtest start (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="Backtest end (YYYY-MM-DD)"),
    min_score: float = Query(75.0, ge=0, le=100, description="Min score to include"),
    max_tickers: int = Query(40, ge=1, le=120, description="Universe cap (runtime)"),
    tickers: Optional[str] = Query(None, description="Comma-separated tickers (defaults to universe sample)"),
):
    """Engine 4: walk-forward continuation backtest (measured edge).

    Win-rate / R / expectancy / MAE-MFE broken out by grade AND by freshness
    bucket — so the desk can see whether 'structure' actually pays.
    """
    import datetime as _dt

    flags = get_flags()
    if not flags.ENABLE_ENGINE4_ICHIMOKU:
        raise HTTPException(status_code=503, detail="Engine 4 (Ichimoku Continuation) is disabled.")

    try:
        client = get_client_optional()
        if client is None:
            raise HTTPException(status_code=503, detail="ORATS unavailable (missing ORATS_TOKEN).")

        end_d = _dt.date.fromisoformat(end[:10]) if end else _dt.date.today()
        start_d = _dt.date.fromisoformat(start[:10]) if start else (end_d - _dt.timedelta(days=365))

        if tickers:
            universe = [t.strip().upper() for t in tickers.split(",") if t.strip()]
        else:
            from backend.universe import load_universe_sp500_and_nasdaq100
            universe = load_universe_sp500_and_nasdaq100()

        from backend.engine4_backtest import backtest_ichimoku
        result = backtest_ichimoku(
            client,
            tickers=universe,
            start=start_d,
            end=end_d,
            min_score=min_score,
            max_tickers=max_tickers,
        )
        return result
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date: {e}") from e
    except OratsError as e:
        LOG.exception("ORATS failure (engine4-ichimoku/backtest)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (engine4-ichimoku/backtest)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@router.get("/api/engine4-ichimoku/{ticker}")
def engine4_ichimoku_ticker(
    request: Request,
    ticker: str,
    date: Optional[str] = Query(None, description="Scan date (YYYY-MM-DD), defaults to today"),
):
    """
    Engine 4: Single ticker Ichimoku analysis

    Analyzes a specific ticker for Ichimoku continuation setup with full details:
    - Complete Ichimoku state (Tenkan, Kijun, cloud, Chikou)
    - Trend regime qualification
    - Pullback state machine
    - Entry trigger detection
    - A+ scoring breakdown
    - Dealer gamma context
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE4_ICHIMOKU:
        raise HTTPException(
            status_code=503,
            detail="Engine 4 (Ichimoku Continuation) is disabled.",
        )

    try:
        client = get_client_optional()
        if client is None:
            raise HTTPException(status_code=503, detail="ORATS unavailable (missing ORATS_TOKEN).")

        t = str(ticker or "").strip().upper()
        if not t:
            raise HTTPException(status_code=400, detail="Missing ticker.")

        benzinga_client = get_benzinga_client_optional()

        result = compute_engine4_single_ticker(
            client,
            ticker=t,
            as_of_date=date,
            benzinga_client=benzinga_client,
        )

        return result

    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception(f"ORATS failure (engine4-ichimoku/{ticker})")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception(f"Unhandled failure (engine4-ichimoku/{ticker})")
        raise HTTPException(status_code=500, detail="Internal error") from e
