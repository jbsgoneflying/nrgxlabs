"""Engine 2 — SPX Iron Condor & Levels routes + AI Trade Advisor."""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from backend.config import get_flags
from backend.engine2_advisor import (
    compute_trade_tracking,
    generate_checkin_analysis,
    generate_trade_analysis,
)
from backend.engine2_trades import (
    add_checkin,
    close_trade,
    compute_trade_performance_digest,
    get_trade,
    list_active_trades,
    list_closed_trades,
    log_trade,
)
from backend.market_hours import is_us_equity_market_open
from backend.deps import (
    LOG,
    get_benzinga_client_optional,
    get_client,
    levels_cache,
    levels_cache_key,
    levels_cache_lock,
    spx_ic_cache,
    spx_ic_cache_key,
    spx_ic_cache_lock,
    spx_levels_cache,
    spx_levels_cache_key,
    spx_levels_cache_lock,
)
from backend.orats_client import OratsError
from backend.redis_store import get_store_optional
from backend.spx_ic import (
    compute_engine2_spx_ic,
    compute_live_levels,
    compute_spx_live_levels,
    fetch_dailies_ohlc_range,
)
from backend.technicals import fetch_live_price_context_optional

router = APIRouter()


@router.get("/api/spx-ic")
def spx_ic(
    underlying: str = Query("SPX", description="Underlying: SPX|SPY|QQQ"),
    entry_day: str = Query("mon", description="Entry day: mon|tue|wed"),
    years: int = Query(3, ge=1, le=5),
    widths: str = Query("0.8,1.0,1.2", description="Comma-separated EM width multiples (e.g. 0.8,1.0,1.2)"),
    risk_target_breach_pct: float = Query(25.0, gt=0.0, le=100.0),
    seasonality_mode: str = Query("none", description="Seasonality conditioning: none|quarter|month|summer|opex"),
    weeks_offset: int = Query(0, ge=0, le=5000, description="Pagination: weeks offset"),
    weeks_limit: int = Query(120, ge=0, le=500, description="Pagination: weeks limit (0 to omit weeks)"),
    grid_limit: int = Query(0, ge=0, le=50000, description="Optional cap on riskGrid cells (0 = all)"),
):
    f = get_flags()
    if not f.ENABLE_ENGINE2_SPX_IC:
        raise HTTPException(status_code=404, detail="Engine 2 disabled (ENABLE_ENGINE2_SPX_IC=0).")

    try:
        under = str(underlying or "SPX").strip().upper()
        if under not in ("SPX", "SPY", "QQQ"):
            raise HTTPException(status_code=400, detail="underlying must be SPX|SPY|QQQ")
        params = {
            "underlying": under,
            "entry_day": entry_day,
            "years": years,
            "widths": widths,
            "risk_target_breach_pct": risk_target_breach_pct,
            "seasonality_mode": seasonality_mode,
            "weeks_offset": weeks_offset,
            "weeks_limit": weeks_limit,
            "grid_limit": grid_limit,
        }
        key = spx_ic_cache_key(params, f.cache_key_engine2())
        cache_enabled = not is_us_equity_market_open()
        if cache_enabled:
            with spx_ic_cache_lock:
                cached = spx_ic_cache.get(key)
            if cached is not None:
                return cached

        ws: list[float] = []
        for part in str(widths).split(","):
            p = part.strip()
            if not p:
                continue
            ws.append(float(p))
        if not ws:
            ws = [0.8, 1.0, 1.2]
        ws = [w for w in ws if w > 0]
        ws = sorted(list(dict.fromkeys(ws)))

        payload = compute_engine2_spx_ic(
            client=get_client(),
            benzinga_client=get_benzinga_client_optional(),
            flags=f,
            underlying_preference=under,
            entry_day=entry_day,
            years=years,
            widths=ws,
            risk_target_breach_pct=risk_target_breach_pct,
            seasonality_mode=seasonality_mode,
        )

        payload["schemaVersion"] = 2
        payload["updatedAt"] = dt.datetime.utcnow().isoformat() + "Z"

        weeks_obj = payload.get("weeks") if isinstance(payload.get("weeks"), dict) else None
        if weeks_obj is not None:
            all_rows = weeks_obj.get("rows") if isinstance(weeks_obj.get("rows"), list) else []
            if weeks_limit <= 0:
                weeks_obj["rows"] = []
                weeks_obj["page"] = {"offset": int(weeks_offset), "limit": 0, "returned": 0, "total": int(weeks_obj.get("count") or len(all_rows))}
            else:
                sl = all_rows[int(weeks_offset) : int(weeks_offset) + int(weeks_limit)]
                weeks_obj["rows"] = sl
                weeks_obj["page"] = {"offset": int(weeks_offset), "limit": int(weeks_limit), "returned": len(sl), "total": int(weeks_obj.get("count") or len(all_rows))}

        grid_obj = payload.get("riskGrid") if isinstance(payload.get("riskGrid"), dict) else None
        if grid_obj is not None:
            cells = grid_obj.get("cells") if isinstance(grid_obj.get("cells"), list) else []
            if grid_limit and int(grid_limit) > 0:
                grid_obj["cells"] = cells[: int(grid_limit)]
                grid_obj["page"] = {"limit": int(grid_limit), "returned": len(grid_obj["cells"]), "total": len(cells)}
            else:
                grid_obj["page"] = {"limit": 0, "returned": len(cells), "total": len(cells)}

        if cache_enabled:
            with spx_ic_cache_lock:
                spx_ic_cache[key] = payload
        return payload
    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure (spx-ic)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (spx-ic)")
        raise HTTPException(status_code=500, detail="Internal error") from e


def _build_levels_response(
    *,
    ticker: str,
    view: str,
    window_days: int,
    points: int,
    include_heatmap: int,
    heatmap_expiries: int,
    heatmap_band_pct: float,
    heatmap_mode: str,
    heatmap_view: str,
    slope_window: int,
    flip_adjacent_n: int,
    symbols: tuple[str, ...] | None = None,
    use_spx_live: bool = False,
) -> dict:
    """Shared logic for /api/spx-levels and /api/levels."""
    client = get_client()

    end = dt.date.today()
    start = end - dt.timedelta(days=window_days)
    bars = fetch_dailies_ohlc_range(client, ticker=ticker, start=start, end=end)
    if not bars:
        raise HTTPException(
            status_code=502,
            detail=f"{ticker} unavailable in ORATS dailies (no rows returned for requested window).",
        )
    closes = [
        {"date": b.trade_date, "close": float(b.close)}
        for b in bars
        if getattr(b, "close", None)
    ]
    if points > 0 and len(closes) > points:
        closes = closes[-points:]

    hm_kwargs = dict(
        include_heatmap=bool(include_heatmap),
        heatmap_expiries=heatmap_expiries,
        heatmap_band_pct=heatmap_band_pct,
        heatmap_mode=heatmap_mode,
        heatmap_view=heatmap_view,
        slope_window=slope_window,
        flip_adjacent_n=flip_adjacent_n,
    )

    if use_spx_live:
        levels_obj = compute_spx_live_levels(
            client, view=view, band_pct=0.05, top_n=5, cluster_steps=2,
            **hm_kwargs,
        )
    else:
        levels_obj = compute_live_levels(
            client,
            underlying=ticker,
            symbols=symbols or (ticker,),
            view=view, band_pct=0.05, top_n=5, cluster_steps=2,
            **hm_kwargs,
        )

    return {"schemaVersion": 3, "priceSeries": closes, "levels": levels_obj}


@router.get("/api/spx-levels")
def spx_levels(
    underlying: str = Query("SPX", description="Underlying: SPX|SPY|QQQ"),
    view: str = Query("weekly", description="weekly|nearest"),
    window_days: int = Query(180, ge=30, le=800, description="Calendar days to scan back for SPX EOD closes (chart window)"),
    points: int = Query(90, ge=30, le=260, description="Max trading-day points to return for charting"),
    include_heatmap: int = Query(1, ge=0, le=1, description="Include net $GEX heatmap matrix (0|1)"),
    heatmap_expiries: int = Query(30, ge=6, le=60, description="How many expiries to include in the raw heatmap grid"),
    heatmap_band_pct: float = Query(0.05, ge=0.01, le=0.20, description="Spot band for heatmap strikes (e.g. 0.05 = ±5%)"),
    heatmap_mode: str = Query("slope", description="Heatmap mode: net|slope"),
    heatmap_view: str = Query("composite", description="Heatmap view: composite|raw"),
    slope_window: int = Query(5, ge=1, le=25, description="Slope smoothing window (strikes)"),
    flip_adjacent_n: int = Query(5, ge=2, le=20, description="Persistence requirement for acceleration boundary detection"),
):
    """
    Lightweight chart payload for Engine 2's dealer-gamma / OI wall visualization.
    - Uses ORATS EOD daily closes (range fetch) for SPX price series.
    - Uses ORATS LIVE strikes (short TTL) for OI walls/clusters and gamma peaks.
    """
    f = get_flags()
    if not f.ENABLE_ENGINE2_SPX_IC:
        raise HTTPException(status_code=404, detail="Engine 2 disabled (ENABLE_ENGINE2_SPX_IC=0).")

    v = str(view or "weekly").strip().lower()
    if v not in ("weekly", "nearest"):
        raise HTTPException(status_code=400, detail="view must be weekly|nearest")

    under = str(underlying or "SPX").strip().upper()
    if under not in ("SPX", "SPY", "QQQ"):
        raise HTTPException(status_code=400, detail="underlying must be SPX|SPY|QQQ")

    try:
        params = {
            "underlying": under, "view": v, "window_days": window_days,
            "points": points, "include_heatmap": include_heatmap,
            "heatmap_expiries": heatmap_expiries, "heatmap_band_pct": heatmap_band_pct,
            "heatmap_mode": heatmap_mode or "net", "heatmap_view": heatmap_view or "composite",
            "slope_window": slope_window, "flip_adjacent_n": flip_adjacent_n,
        }
        key = spx_levels_cache_key(params, f.cache_key_engine2())
        with spx_levels_cache_lock:
            cached = spx_levels_cache.get(key)
        if cached is not None:
            return cached

        payload = _build_levels_response(
            ticker=under, view=v, window_days=window_days, points=points,
            include_heatmap=include_heatmap, heatmap_expiries=heatmap_expiries,
            heatmap_band_pct=heatmap_band_pct,
            heatmap_mode=heatmap_mode or "net", heatmap_view=heatmap_view or "composite",
            slope_window=slope_window, flip_adjacent_n=flip_adjacent_n,
            symbols=(under,),
            use_spx_live=(under == "SPX"),
        )

        with spx_levels_cache_lock:
            spx_levels_cache[key] = payload
        return payload
    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure (spx-levels)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (spx-levels)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@router.get("/api/levels")
def levels(
    ticker: str = Query(..., description="Underlying ticker (e.g. AAPL, TSLA, SPX)"),
    view: str = Query("weekly", description="weekly|nearest"),
    window_days: int = Query(180, ge=30, le=800, description="Calendar days to scan back for EOD closes (chart window)"),
    points: int = Query(90, ge=30, le=260, description="Max trading-day points to return for charting"),
    include_heatmap: int = Query(1, ge=0, le=1, description="Include net $GEX heatmap matrix (0|1)"),
    heatmap_expiries: int = Query(30, ge=6, le=60, description="How many expiries to include in the raw heatmap grid"),
    heatmap_band_pct: float = Query(0.05, ge=0.01, le=0.20, description="Spot band for heatmap strikes (e.g. 0.05 = ±5%)"),
    heatmap_mode: str = Query("slope", description="Heatmap mode: net|slope"),
    heatmap_view: str = Query("composite", description="Heatmap view: composite|raw"),
    slope_window: int = Query(5, ge=1, le=25, description="Slope smoothing window (strikes)"),
    flip_adjacent_n: int = Query(5, ge=2, le=20, description="Persistence requirement for acceleration boundary detection"),
):
    """
    Lightweight chart payload for Dealer Gamma Map + Weekly Gamma Risk Heat-Map (per underlying).
    Used by Engine 1 (single-name) and can be used by Engine 2 (SPX) as well.
    """
    f = get_flags()

    t = str(ticker or "").strip().upper()
    if not t:
        raise HTTPException(status_code=400, detail="ticker is required")

    v = str(view or "weekly").strip().lower()
    if v not in ("weekly", "nearest"):
        raise HTTPException(status_code=400, detail="view must be weekly|nearest")

    try:
        params = {
            "ticker": t, "view": v, "window_days": window_days,
            "points": points, "include_heatmap": include_heatmap,
            "heatmap_expiries": heatmap_expiries, "heatmap_band_pct": heatmap_band_pct,
            "heatmap_mode": heatmap_mode or "net", "heatmap_view": heatmap_view or "composite",
            "slope_window": slope_window, "flip_adjacent_n": flip_adjacent_n,
        }
        key = levels_cache_key(t, params, f.cache_key_engine2())
        with levels_cache_lock:
            cached = levels_cache.get(key)
        if cached is not None:
            return cached

        payload = _build_levels_response(
            ticker=t, view=v, window_days=window_days, points=points,
            include_heatmap=include_heatmap, heatmap_expiries=heatmap_expiries,
            heatmap_band_pct=heatmap_band_pct,
            heatmap_mode=heatmap_mode or "net", heatmap_view=heatmap_view or "composite",
            slope_window=slope_window, flip_adjacent_n=flip_adjacent_n,
            symbols=(("SPXW", "SPX", "SPY") if t == "SPX" else (t,)),
        )
        payload["ticker"] = t

        with levels_cache_lock:
            levels_cache[key] = payload
        return payload
    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure (levels)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (levels)")
        raise HTTPException(status_code=500, detail="Internal error") from e


# ═══════════════════════════════════════════════════════════════════════════
# AI Trade Advisor endpoints
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/api/spx-ic/advisor")
def spx_ic_advisor(
    body: Optional[Dict[str, Any]] = Body(default=None),
    underlying: str = Query("SPX", description="Underlying: SPX|SPY|QQQ"),
    entry_day: str = Query("mon", description="Entry day: mon|tue|wed"),
    seasonality_mode: str = Query("none", description="Seasonality conditioning"),
):
    """LLM trade advisor — accepts pre-computed Engine2 payload or re-runs scan."""
    f = get_flags()
    if not f.ENABLE_ENGINE2_SPX_IC:
        raise HTTPException(status_code=404, detail="Engine 2 disabled")
    if not f.ENGINE2_ADVISOR_ENABLED:
        raise HTTPException(status_code=404, detail="Engine 2 advisor disabled")

    try:
        payload: Dict[str, Any]
        if body and isinstance(body, dict) and body.get("current"):
            payload = body
        else:
            under = str(underlying or "SPX").strip().upper()
            if under not in ("SPX", "SPY", "QQQ"):
                raise HTTPException(status_code=400, detail="underlying must be SPX|SPY|QQQ")
            payload = compute_engine2_spx_ic(
                client=get_client(),
                benzinga_client=get_benzinga_client_optional(),
                flags=f,
                underlying_preference=under,
                entry_day=entry_day,
                seasonality_mode=seasonality_mode,
            )

        analysis = generate_trade_analysis(
            engine2_payload=payload,
            width_analysis=payload.get("widthComparison"),
            flags=f,
        )

        return {
            "advisor": analysis,
            "widthComparison": payload.get("widthComparison", []),
            "recommendation": payload.get("recommendation"),
            "recSimple": payload.get("recSimple"),
            "strikeTargets": payload.get("strikeTargets"),
            "current": payload.get("current"),
            "expectedMove": payload.get("expectedMove"),
            "underlying": payload.get("underlying"),
            "asOfDate": payload.get("asOfDate"),
        }
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Engine2 advisor failure")
        raise HTTPException(status_code=500, detail=f"Advisor error: {type(e).__name__}: {e}") from e


@router.post("/api/spx-ic/trade")
def spx_ic_trade_log(body: Dict[str, Any] = Body(...)):
    """Log a new trade (from advisor recommendation or manual entry)."""
    f = get_flags()
    if not f.ENGINE2_ADVISOR_ENABLED:
        raise HTTPException(status_code=404, detail="Engine 2 advisor disabled")

    store = get_store_optional()
    if store is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    if "marketSnapshot" not in body:
        try:
            from backend.trade_memory import capture_market_snapshot
            from backend.deps import get_client_optional
            body["marketSnapshot"] = capture_market_snapshot(
                store=store, orats_client=get_client_optional(), ticker="SPY",
            )
        except Exception:
            pass

    trade_id = log_trade(trade_data=body, store=store, flags=f)
    if trade_id is None:
        raise HTTPException(status_code=500, detail="Failed to log trade")

    trade = get_trade(trade_id, store=store)
    return {"status": "ok", "tradeId": trade_id, "trade": trade}


@router.get("/api/spx-ic/trades")
def spx_ic_trades_list():
    """List active trades with live tracking metrics."""
    f = get_flags()
    if not f.ENGINE2_ADVISOR_ENABLED:
        raise HTTPException(status_code=404, detail="Engine 2 advisor disabled")

    store = get_store_optional()
    trades = list_active_trades(store=store)

    px_ctx = fetch_live_price_context_optional(client=get_client(), ticker="SPX")
    current_spot = float(px_ctx.get("price", 0)) if px_ctx else 0.0

    current_regime = None
    current_vol = None
    try:
        from backend.engine5_snapshot import select_best_snapshot
        e5 = select_best_snapshot(store, max_age_days=f.ENGINE5_SNAPSHOT_BEST_MAX_AGE_DAYS,
                                  snapshot_ttl=f.ENGINE5_SNAPSHOT_TTL_S)
        if e5:
            rdata = e5.get("data", {}).get("regime", {})
            current_regime = {"score": rdata.get("score"), "bucket": rdata.get("label")}
            current_vol = rdata.get("vol_pressure_state")
    except Exception:
        pass

    enriched = []
    for t in trades:
        tracking = None
        if current_spot > 0:
            tracking = compute_trade_tracking(
                trade=t,
                current_spot=current_spot,
                current_regime=current_regime,
                current_vol_pressure=current_vol,
            )
        enriched.append({
            **t,
            "tracking": tracking,
            "currentSpot": current_spot,
        })

    return {"trades": enriched, "count": len(enriched)}


@router.post("/api/spx-ic/trade/{trade_id}/checkin")
def spx_ic_trade_checkin(trade_id: str, body: Dict[str, Any] = Body(default={})):
    """Run a check-in analysis on an open trade."""
    f = get_flags()
    if not f.ENGINE2_ADVISOR_ENABLED:
        raise HTTPException(status_code=404, detail="Engine 2 advisor disabled")

    store = get_store_optional()
    if store is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    trade = get_trade(trade_id, store=store)
    if trade is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")

    px_ctx = fetch_live_price_context_optional(client=get_client(), ticker="SPX")
    current_spot = float(px_ctx.get("price", 0)) if px_ctx else 0.0
    if current_spot <= 0:
        raise HTTPException(status_code=502, detail="Could not fetch current spot price")

    current_regime = None
    current_vol = None
    try:
        from backend.engine5_snapshot import select_best_snapshot
        e5 = select_best_snapshot(store, max_age_days=f.ENGINE5_SNAPSHOT_BEST_MAX_AGE_DAYS,
                                  snapshot_ttl=f.ENGINE5_SNAPSHOT_TTL_S)
        if e5:
            rdata = e5.get("data", {}).get("regime", {})
            current_regime = {"score": rdata.get("score"), "bucket": rdata.get("label")}
            current_vol = rdata.get("vol_pressure_state")
    except Exception:
        pass

    tracking = compute_trade_tracking(
        trade=trade,
        current_spot=current_spot,
        current_regime=current_regime,
        current_vol_pressure=current_vol,
    )

    analysis = generate_checkin_analysis(
        trade=trade,
        tracking=tracking,
        flags=f,
    )

    checkin_snapshot = None
    try:
        from backend.trade_memory import capture_market_snapshot
        from backend.deps import get_client_optional
        checkin_snapshot = capture_market_snapshot(
            store=store, orats_client=get_client_optional(), ticker="SPY",
        )
    except Exception:
        pass

    checkin_record = {
        "status": analysis.get("status", tracking.get("deterministicStatus")),
        "headline": analysis.get("headline"),
        "recommendation": analysis.get("recommendation"),
        "adjustment": analysis.get("adjustmentIfNeeded"),
        "spotAnalysis": analysis.get("spotAnalysis"),
        "regimeDrift": analysis.get("regimeDrift"),
        "riskUpdate": analysis.get("riskUpdate"),
        "deskNote": analysis.get("deskNote"),
        "tracking": tracking,
        "spotAtCheckin": current_spot,
        "marketSnapshot": checkin_snapshot,
        "overrideNote": body.get("overrideNote"),
        "_llmSource": analysis.get("_source"),
    }
    add_checkin(trade_id, checkin_record, store=store, flags=f)

    return {
        "tradeId": trade_id,
        "analysis": analysis,
        "tracking": tracking,
        "currentSpot": current_spot,
    }


@router.post("/api/spx-ic/trade/{trade_id}/close")
def spx_ic_trade_close(trade_id: str, body: Dict[str, Any] = Body(default={})):
    """Close a trade with optional outcome data."""
    f = get_flags()
    if not f.ENGINE2_ADVISOR_ENABLED:
        raise HTTPException(status_code=404, detail="Engine 2 advisor disabled")

    store = get_store_optional()
    if store is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    if "spotAtExit" not in body:
        try:
            px_ctx = fetch_live_price_context_optional(client=get_client(), ticker="SPX")
            if px_ctx:
                body["spotAtExit"] = float(px_ctx.get("price", 0))
        except Exception:
            pass
    if "vixAtExit" not in body:
        try:
            from backend.deps import get_client_optional
            orats = get_client_optional()
            if orats:
                resp = orats.live_summaries(ticker="SPY")
                rows = resp.rows or []
                if rows:
                    body["vixAtExit"] = rows[0].get("iv30dMean") or rows[0].get("ivMean")
        except Exception:
            pass
    if "regimeAtExit" not in body:
        try:
            from backend.engine5_snapshot import select_best_snapshot
            e5 = select_best_snapshot(store, max_age_days=f.ENGINE5_SNAPSHOT_BEST_MAX_AGE_DAYS,
                                      snapshot_ttl=f.ENGINE5_SNAPSHOT_TTL_S)
            if e5:
                rdata = e5.get("data", {}).get("regime", {})
                body["regimeAtExit"] = {"label": rdata.get("label"), "score": rdata.get("score")}
        except Exception:
            pass

    trade = close_trade(trade_id, close_data=body, store=store, flags=f)
    if trade is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")

    return {"status": "ok", "trade": trade}


@router.post("/api/spx-ic/trade/{trade_id}/post-mortem")
def spx_ic_trade_post_mortem(trade_id: str):
    """Generate and store an LLM post-mortem for a closed trade."""
    f = get_flags()
    if not f.ENGINE2_ADVISOR_ENABLED:
        raise HTTPException(status_code=404, detail="Engine 2 advisor disabled")

    store = get_store_optional()
    if store is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    trade = get_trade(trade_id, store=store)
    if trade is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")
    if trade.get("status") != "closed":
        raise HTTPException(status_code=400, detail="Trade must be closed for post-mortem")

    journal_ctx = None
    try:
        digest = compute_trade_performance_digest(store=store)
        from backend.engine2_advisor import _build_journal_context
        journal_ctx = _build_journal_context(digest)
    except Exception:
        pass

    from backend.engine2_advisor import generate_post_mortem as gen_pm
    from backend.engine2_trades import set_post_mortem

    pm = gen_pm(trade, flags=f, journal_context=journal_ctx)
    updated = set_post_mortem(trade_id, pm, store=store, flags=f)
    if updated is None:
        raise HTTPException(status_code=500, detail="Failed to persist post-mortem")

    return {"tradeId": trade_id, "postMortem": pm}


@router.get("/api/spx-ic/trades/history")
def spx_ic_trades_history(limit: int = Query(default=30, ge=1, le=100)):
    """Return closed trades for the trade journal."""
    f = get_flags()
    if not f.ENGINE2_ADVISOR_ENABLED:
        raise HTTPException(status_code=404, detail="Engine 2 advisor disabled")
    store = get_store_optional()
    closed = list_closed_trades(store=store, limit=limit)
    return {"trades": closed, "count": len(closed)}


@router.get("/api/spx-ic/trades/performance")
def spx_ic_trades_performance():
    """Return aggregated performance digest from closed trades."""
    f = get_flags()
    if not f.ENGINE2_ADVISOR_ENABLED:
        raise HTTPException(status_code=404, detail="Engine 2 advisor disabled")
    store = get_store_optional()
    digest = compute_trade_performance_digest(store=store)
    return digest
