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
    get_benzinga_client_optional,
    engine4_cache,
    engine4_cache_lock,
)
from backend.engine4_screener import (
    run_universe_scan as compute_engine4_scan,
    run_close_preview as compute_engine4_close_preview,
    scan_single_ticker as compute_engine4_single_ticker,
    get_all_signals as get_engine4_signals,
    refresh_signal_statuses as refresh_engine4_statuses,
    set_desk_status as set_engine4_desk_status,
    remove_signal as remove_engine4_signal,
    apply_live_price_overlay as apply_engine4_live_overlay,
)
from backend.gating import (
    gate_scan_results,
    summarize_gates,
    reconcile_ichimoku_verdict,
    summarize_verdicts,
)

router = APIRouter()


def _get_gate_context(flags) -> dict:
    """Gather regime and vol context for gating decisions.

    Mirrors backend/routers/engine3_red_dog.py::_get_gate_context — both
    consume the same canonical Market Intelligence v2 regime so E3 + E4
    gating can never silently disagree.
    """
    ctx: dict = {
        "regime_label": "",
        "regime_confidence": None,
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
            if mi.confidence is not None:
                ctx["regime_confidence"] = float(mi.confidence)
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
    force: bool = Query(False, description="Bypass the structure-scan cache and pull a fresh universe scan"),
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
    - Earnings filter (downgrade if within 5 sessions)

    Market data is served entirely by EODHD (bars + live quotes).
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE4_ICHIMOKU:
        raise HTTPException(
            status_code=503,
            detail="Engine 4 (Ichimoku Continuation) is disabled. Set ENABLE_ENGINE4_ICHIMOKU=1 to enable.",
        )

    try:
        dir_filter = None
        if direction:
            d = str(direction).strip().lower()
            if d in ("bullish", "bull", "long"):
                dir_filter = "bullish"
            elif d in ("bearish", "bear", "short"):
                dir_filter = "bearish"

        use_cache = not force
        cache_key = (date, min_score, dir_filter)
        result = None
        if use_cache:
            with engine4_cache_lock:
                result = engine4_cache.get(cache_key)

        if result is None:
            benzinga_client = get_benzinga_client_optional()

            result = compute_engine4_scan(
                as_of_date=date,
                min_score=min_score,
                direction=dir_filter,
                benzinga_client=benzinga_client,
                max_workers=flags.ENGINE4_MAX_WORKERS,
                use_cache=use_cache,
            )

            result = _gate_engine4_result(result, flags)

            with engine4_cache_lock:
                engine4_cache[cache_key] = result

        # Live re-pricing overlay — applied on EVERY request (even a cache
        # hit) so the "distance to trigger" reflects the current market, not
        # the scan-time close. Cheap: one quote per surfaced name.
        if getattr(flags, "ENGINE4_LIVE_REPRICE", True) and isinstance(result, dict):
            try:
                apply_engine4_live_overlay(result, max_workers=flags.ENGINE4_MAX_WORKERS)
            except Exception as live_err:
                LOG.warning(f"Live re-pricing overlay failed for engine4: {live_err}")

        return result

    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (engine4-ichimoku)")
        raise HTTPException(status_code=500, detail="Internal error") from e


def _gate_engine4_result(result, flags):
    """Apply gating + verdict reconciliation to a fresh scan result.

    Pulled out of the request handler so the result can be gated once on a
    cache miss, then served from cache with only the (cheap) live overlay
    re-applied per request.
    """
    if flags.ENABLE_GATING and isinstance(result, dict):
        try:
            # Every playbook section runs behind the same gate + verdict
            # reconciliation: core lists live at the top level, research
            # playbooks (tk_cross / kumo_breakout) under result["playbooks"].
            def _signal_lists(res):
                for key in ("actionable", "structure", "watchlist"):
                    setups = res.get(key)
                    if isinstance(setups, list):
                        yield setups
                playbooks = res.get("playbooks")
                if isinstance(playbooks, dict):
                    for block in playbooks.values():
                        if not isinstance(block, dict):
                            continue
                        for key in ("actionable", "structure"):
                            setups = block.get(key)
                            if isinstance(setups, list):
                                yield setups

            gate_ctx = _get_gate_context(flags)
            regime_allow = [s.strip() for s in str(flags.GATE_ICH_REGIME_ALLOW).split(",") if s.strip()]
            regime_allow_short = [s.strip() for s in str(flags.GATE_ICH_REGIME_ALLOW_SHORT).split(",") if s.strip()]
            vol_state_allow = [s.strip() for s in str(flags.GATE_ICH_VOL_STATE_ALLOW).split(",") if s.strip()]
            # Top-down index-trend alignment context (from the scan result).
            index_states = result.get("indexState") if isinstance(result.get("indexState"), dict) else None
            index_align_enable = bool(getattr(flags, "GATE_ICH_INDEX_ALIGN_ENABLE", False))
            index_beta_hard = float(getattr(flags, "GATE_ICH_INDEX_BETA_HARD", 1.0) or 1.0)
            index_corr_hard = float(getattr(flags, "GATE_ICH_INDEX_CORR_HARD", 0.6) or 0.6)
            for setups in _signal_lists(result):
                gate_scan_results(
                    scan_results=setups,
                    engine="engine4_ichimoku",
                    regime_allow=regime_allow,
                    regime_allow_short=regime_allow_short,
                    vol_state_allow=vol_state_allow,
                    regime_min_confidence=float(
                        getattr(flags, "GATE_ICH_REGIME_MIN_CONFIDENCE", 0.0) or 0.0
                    ),
                    index_states=index_states,
                    index_align_enable=index_align_enable,
                    index_beta_hard=index_beta_hard,
                    index_corr_hard=index_corr_hard,
                    **gate_ctx,
                )
            gs = summarize_gates(
                (result.get("actionable") or []) + (result.get("structure") or [])
            )
            result["gateSummary"] = gs
            result["gateContext"] = gate_ctx

            # Reconcile grade + freshness + gate into one continuation
            # verdict per name, and lead the card with it.
            regime_label = gate_ctx.get("regime_label", "")
            for setups in _signal_lists(result):
                for sig in setups:
                    sig["verdict"] = reconcile_ichimoku_verdict(
                        sig, regime_label=regime_label
                    )
            result["verdictSummary"] = summarize_verdicts(
                (result.get("actionable") or []) + (result.get("structure") or [])
            )
        except Exception as gate_err:
            LOG.warning(f"Gate injection failed for engine4: {gate_err}")

    return result


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
        live_reprice = getattr(flags, "ENGINE4_LIVE_REPRICE", True)

        if refresh:
            refresh_result = refresh_engine4_statuses(as_of_date=date)
            signals = get_engine4_signals()
            if live_reprice:
                try:
                    from backend.engine4_screener import overlay_tracker_signals
                    overlay_tracker_signals(signals, max_workers=flags.ENGINE4_MAX_WORKERS)
                except Exception as live_err:
                    LOG.warning(f"Tracker live overlay failed for engine4: {live_err}")
            return {
                "refreshed": True,
                **refresh_result,
                "signals": signals,
            }

        signals = get_engine4_signals()
        # Even on a plain load, re-price the desk book against the live market
        # so a name that already blew through its trigger doesn't keep reading
        # its scan-time distance.
        if live_reprice:
            try:
                from backend.engine4_screener import overlay_tracker_signals
                overlay_tracker_signals(signals, max_workers=flags.ENGINE4_MAX_WORKERS)
            except Exception as live_err:
                LOG.warning(f"Tracker live overlay failed for engine4: {live_err}")
        return {
            "refreshed": False,
            "signals": signals,
        }

    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (engine4-ichimoku/status)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@router.post("/api/engine4-ichimoku/track")
async def engine4_ichimoku_track(request: Request):
    """Desk Trade Tracker override.

    Body: {ticker, status, signalDate?, note?, pinned?, signal?}
    `status` is one of watching/entered/working/broken/exited. Desk states
    survive scan refreshes and are never clobbered by the auto-evaluator.
    `signal` (full card payload) seeds the tracker for research-playbook
    names, which are never auto-persisted by the scan.
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

    # Untrack: remove a mis-clicked / stale name from the desk book entirely.
    if status in ("untrack", "remove", "clear"):
        try:
            result = remove_engine4_signal(ticker, signal_date=body.get("signalDate"))
            if not result.get("ok"):
                raise HTTPException(status_code=400, detail=result.get("error", "Could not remove."))
            return {"ok": True, "removed": result.get("removed"), "signals": get_engine4_signals()}
        except HTTPException:
            raise
        except Exception as e:
            LOG.exception("Unhandled failure (engine4-ichimoku/track untrack)")
            raise HTTPException(status_code=500, detail="Internal error") from e

    try:
        result = set_engine4_desk_status(
            ticker,
            desk_status=status,
            signal_date=body.get("signalDate"),
            note=body.get("note"),
            pinned=body.get("pinned"),
            # Research playbook cards aren't auto-persisted by the scan; the
            # card sends its full signal payload so the first Watch seeds it.
            signal=body.get("signal") if isinstance(body.get("signal"), dict) else None,
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
    entry_model: str = Query("trigger", description="Entry model: 'trigger' (stop order at trigger), 'close' (filled at signal-bar close, actionable-only), or 'system' (gap-aware stop entry, breakeven at +1R, Kijun-cross exit, no target)"),
):
    """Engine 4: walk-forward continuation backtest (measured edge).

    Win-rate / R / expectancy / MAE-MFE broken out by grade, freshness
    bucket, playbook, AND entry day-of-week — plus average hold duration —
    so the desk can see whether 'structure' actually pays and whether a
    Friday close-entry is a good idea.
    """
    import datetime as _dt

    flags = get_flags()
    if not flags.ENABLE_ENGINE4_ICHIMOKU:
        raise HTTPException(status_code=503, detail="Engine 4 (Ichimoku Continuation) is disabled.")

    try:
        end_d = _dt.date.fromisoformat(end[:10]) if end else _dt.date.today()
        start_d = _dt.date.fromisoformat(start[:10]) if start else (end_d - _dt.timedelta(days=365))

        if tickers:
            universe = [t.strip().upper() for t in tickers.split(",") if t.strip()]
        else:
            from backend.universe import load_universe_sp500_and_nasdaq100
            universe = load_universe_sp500_and_nasdaq100()

        em = str(entry_model).strip().lower()
        if em not in ("trigger", "close", "system"):
            em = "trigger"
        from backend.engine4_backtest import backtest_ichimoku
        result = backtest_ichimoku(
            tickers=universe,
            start=start_d,
            end=end_d,
            min_score=min_score,
            max_tickers=max_tickers,
            entry_model=em,
            # System exits let winners run to the Kijun cross — the 10-bar
            # research cap would truncate exactly the trades being measured.
            max_hold=60 if em == "system" else 10,
        )
        return result
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date: {e}") from e
    except Exception as e:
        LOG.exception("Unhandled failure (engine4-ichimoku/backtest)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@router.get("/api/engine4-ichimoku/close-preview")
def engine4_ichimoku_close_preview(
    request: Request,
    direction: Optional[str] = Query(None, description="Filter by direction: bullish or bearish"),
    force: bool = Query(False, description="Bypass the 90s preview cache"),
):
    """Engine 4: Close Preview — evaluate today's FORMING daily candle.

    Synthesizes today's bar from live EODHD quotes (last trade = hypothetical
    close), appends it to the daily series, and runs every playbook detector.
    Built for the last 15-20 minutes of the session: if a candidate is
    actionable here and holds into the bell, the close IS the entry.

    Same gate + verdict reconciliation as the main scan. Never persists to
    the desk tracker — preview signals enter the book via manual Watch only.
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE4_ICHIMOKU:
        raise HTTPException(status_code=503, detail="Engine 4 (Ichimoku Continuation) is disabled.")

    try:
        dir_filter = None
        if direction:
            d = str(direction).strip().lower()
            if d in ("bullish", "bull", "long"):
                dir_filter = "bullish"
            elif d in ("bearish", "bear", "short"):
                dir_filter = "bearish"

        result = compute_engine4_close_preview(
            direction=dir_filter,
            benzinga_client=get_benzinga_client_optional(),
            max_workers=flags.ENGINE4_MAX_WORKERS,
            use_cache=not force,
        )
        result = _gate_engine4_result(result, flags)
        return result
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (engine4-ichimoku/close-preview)")
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
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE4_ICHIMOKU:
        raise HTTPException(
            status_code=503,
            detail="Engine 4 (Ichimoku Continuation) is disabled.",
        )

    try:
        t = str(ticker or "").strip().upper()
        if not t:
            raise HTTPException(status_code=400, detail="Missing ticker.")

        benzinga_client = get_benzinga_client_optional()

        result = compute_engine4_single_ticker(
            ticker=t,
            as_of_date=date,
            benzinga_client=benzinga_client,
        )

        return result

    except HTTPException:
        raise
    except Exception as e:
        LOG.exception(f"Unhandled failure (engine4-ichimoku/{ticker})")
        raise HTTPException(status_code=500, detail="Internal error") from e
