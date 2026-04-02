from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.deps import (
    LOG,
    get_client,
    get_client_optional,
    get_benzinga_client_optional,
    get_fmp_client_optional,
    get_fred_client_optional,
    dms_cache,
    morning_brief_cache,
    weekly_roadmap_cache,
    front_layer_lock,
)
from backend.config import get_flags
from backend.orats_client import OratsError
from backend.redis_store import get_store_optional
from backend.daily_market_state import (
    DailyMarketState,
    build_daily_market_state,
    persist_dms,
    load_dms,
    load_dms_history,
    compute_dms_diff,
    DMS_INDEX_KEY,
)
from backend.cross_asset_stress import (
    CrossAssetStressSnapshot,
    AssetStressReading,
    compute_asset_stress,
    build_cross_asset_snapshot,
    CROSS_ASSET_UNIVERSE,
)
from backend.news_theme_intelligence import (
    NewsThemeSnapshot,
    score_themes,
    extract_headlines_from_eodhd,
    extract_headlines_from_benzinga,
    persist_theme_snapshot,
    load_theme_history,
)
from backend.front_layer_llm import (
    generate_morning_brief,
    generate_weekly_roadmap,
    detect_asymmetries,
    generate_asset_insight,
    generate_card_insight,
)
from backend.benzinga_client import BenzingaClient
from backend.calendar_api import build_calendar_payload

router = APIRouter()


# ---------------------------------------------------------------------------
# Helper: build live DMS from engine data
# ---------------------------------------------------------------------------

def _build_live_dms(today_str: str, store) -> dict:
    """Build a DailyMarketState from live engine data.

    Reads from existing engines without modifying their logic.
    """
    flags = get_flags()

    # --- Engine 5: regime + vol ---
    regime_data = {}
    vol_direction = ""
    iv_stress = 50.0
    regime_source: Dict[str, Any] = {}

    try:
        from backend.engine5_snapshot import select_best_snapshot
        snapshot = select_best_snapshot(store) if store else None
        if snapshot:
            snap_data = snapshot.get("data", {})
            regime_data = snap_data.get("regime", {})
            vol_ll = snap_data.get("volLeadLag", {})
            vol_direction = str(vol_ll.get("volLagState") or vol_ll.get("vol_lag_state", ""))
            iv_stress = float(regime_data.get("components", {}).get("iv_stress", 50.0))
            regime_source = {
                "snapshot_id": snapshot.get("id", ""),
                "snapshot_generated_at": snapshot.get("generated_at", ""),
                "snapshot_grade": snapshot.get("grade", ""),
            }
    except Exception as e:
        LOG.warning("Front Layer: Engine 5 data unavailable: %s", e)

    # --- Sequencer ---
    seq_summary = {}
    try:
        from backend.sequencer import current_week_id, build_weekly_sequence, SequencerEvent
        wk = current_week_id()
        events_raw = []
        if store:
            events_raw = store.get_json(f"sequencer:week:{wk}") or []
        events = [SequencerEvent.from_dict(e) for e in events_raw] if events_raw else []
        seq = build_weekly_sequence(week_id=wk, events=events)
        seq_summary = seq.to_dict()
    except Exception as e:
        LOG.warning("Front Layer: Sequencer data unavailable: %s", e)

    # --- News risk ---
    event_count = 0
    high_sev = 0
    upcoming: List[str] = []
    try:
        cal = build_calendar_payload(mode="week")
        events = cal.get("events", [])
        event_count = len(events)
        high_sev = sum(1 for ev in events if str(ev.get("importance", "")).lower() in ("high", "critical"))
        upcoming = [str(ev.get("title", "")) for ev in events[:5] if ev.get("title")]
    except Exception as e:
        LOG.warning("Front Layer: Calendar data unavailable: %s", e)

    # --- News Themes ---
    themes_list: List[dict] = []
    try:
        headlines: List[str] = []
        try:
            from backend.eodhd_client import EodhdClient
            eodhd = EodhdClient.from_env()
            resp = eodhd.get_news(topic="market", limit=50)
            headlines.extend(extract_headlines_from_eodhd(resp.rows))
        except Exception:
            pass
        try:
            benz = BenzingaClient.from_env()
            resp = benz.news(page_size=50)
            headlines.extend(extract_headlines_from_benzinga(resp.rows))
        except Exception:
            pass

        if headlines:
            prior_themes = load_theme_history(store, n_days=flags.FRONT_LAYER_THEME_LOOKBACK_DAYS) if store else []
            theme_snap = score_themes(headlines=headlines, prior_snapshots=prior_themes, date_str=today_str)
            themes_list = theme_snap.themes
            if store:
                persist_theme_snapshot(theme_snap, store)
    except Exception as e:
        LOG.warning("Front Layer: News theme scoring failed: %s", e)

    # --- Cross-Asset Stress ---
    cross_asset_snap: Optional[dict] = None
    try:
        from backend.eodhd_client import EodhdClient as _EodhdCls
        _eodhd = _EodhdCls.from_env()
        spx_return = 0.0
        try:
            spx_resp = _eodhd.get_eod("GSPC.INDX", period="d")
            spx_bars = sorted(spx_resp.rows, key=lambda b: str(b.get("date", "")))
            if len(spx_bars) >= 2:
                cur_c = float(spx_bars[-1].get("adjusted_close") or spx_bars[-1].get("close", 0))
                prv_c = float(spx_bars[-2].get("adjusted_close") or spx_bars[-2].get("close", 0))
                if prv_c:
                    spx_return = round((cur_c - prv_c) / abs(prv_c) * 100, 4)
        except Exception:
            pass

        readings: List[AssetStressReading] = []
        for key, meta in CROSS_ASSET_UNIVERSE.items():
            try:
                resp = _eodhd.get_eod(meta["symbol"], period="d")
                bars = sorted(resp.rows, key=lambda b: str(b.get("date", "")))
                if len(bars) >= 2:
                    cur_c = float(bars[-1].get("adjusted_close") or bars[-1].get("close", 0))
                    prv_c = float(bars[-2].get("adjusted_close") or bars[-2].get("close", 0))
                    history = [float(b.get("adjusted_close") or b.get("close", 0)) for b in bars[-30:]]
                    r = compute_asset_stress(
                        symbol_key=key,
                        current_close=cur_c,
                        prior_close=prv_c,
                        equity_return_1d=spx_return,
                        history_closes=history,
                    )
                    readings.append(r)
            except Exception:
                pass

        if readings:
            now_ts = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
            cross_asset_snap = build_cross_asset_snapshot(
                readings=readings,
                timestamp=now_ts,
            ).to_dict()
            LOG.info("Front Layer: Cross-asset stress: %d readings", len(readings))
    except Exception as e:
        LOG.warning("Front Layer: Cross-asset stress unavailable: %s", e)

    # --- Build DMS ---
    dms = build_daily_market_state(
        date_str=today_str,
        regime=regime_data,
        regime_source=regime_source,
        vol_direction=vol_direction,
        iv_stress=iv_stress,
        event_count_5d=event_count,
        high_severity_count=high_sev,
        upcoming_events=upcoming,
        cross_asset_stress=cross_asset_snap,
        news_themes=themes_list,
        sequencer_summary=seq_summary,
    )

    # Detect asymmetries
    dms_dict = dms.to_dict()
    history = load_dms_history(store, n=flags.FRONT_LAYER_DMS_HISTORY_DAYS) if store else []
    history_dicts = [h.to_dict() for h in history]
    asymmetries = detect_asymmetries(dms_dict, history_dicts)
    dms_dict["asymmetry_signals"] = asymmetries

    # Persist
    if store:
        dms_updated = DailyMarketState.from_dict(dms_dict)
        persist_dms(dms_updated, store, ttl_s=flags.FRONT_LAYER_DMS_TTL_S)

    return dms_dict


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/api/front-layer/daily-market-state")
def api_front_layer_dms():
    """Return today's DailyMarketState (build or load cached)."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    today_str = dt.date.today().isoformat()

    cached = dms_cache.get(f"dms:{today_str}")
    if cached is not None:
        return cached

    store = get_store_optional()
    dms = load_dms(today_str, store) if store else None

    if dms is not None:
        result = dms.to_dict()
        dms_cache[f"dms:{today_str}"] = result
        return result

    dms_dict = _build_live_dms(today_str, store)
    dms_cache[f"dms:{today_str}"] = dms_dict
    return dms_dict


@router.post("/api/front-layer/refresh")
def api_front_layer_refresh():
    """Force-refresh: pull live data from all engines, rebuild DMS, bust caches.

    This is a manual desk trigger that:
    - Bypasses all in-memory and Redis caches
    - Fetches the freshest data from every engine and data source
    - Rebuilds the DailyMarketState with a new timestamp
    - Persists the updated snapshot (additive to rolling history)
    - Re-generates the Morning Brief with fresh context
    - Does NOT interfere with cron schedules or retention policy

    Use during the trading day after major events: commodity shocks,
    crypto sell-offs, surprise news releases, regime flips, etc.
    """
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    now = dt.datetime.now(dt.timezone.utc)
    today_str = dt.date.today().isoformat()
    store = get_store_optional()

    # ── 1. Bust all in-memory caches ────────────────────────────────
    dms_cache.clear()
    morning_brief_cache.clear()
    weekly_roadmap_cache.clear()

    # ── 2. Build fresh DMS (all live data, no cache reads) ──────────
    dms_dict = _build_live_dms(today_str, store)
    dms_dict["_refresh"] = {
        "triggered_at": now.isoformat().replace("+00:00", "Z"),
        "source": "manual_desk_refresh",
    }

    # ── 3. Persist (overwrites today's snapshot; rolling history intact)
    if store:
        dms_obj = DailyMarketState.from_dict(dms_dict)
        persist_dms(dms_obj, store, ttl_s=flags.FRONT_LAYER_DMS_TTL_S)

    # ── 4. Re-cache in memory so subsequent GET reads are fresh ─────
    dms_cache[f"dms:{today_str}"] = dms_dict

    # ── 5. Re-generate Morning Brief with the fresh DMS ─────────────
    brief = None
    brief_source = "disabled"
    brief_error = None
    if flags.ENABLE_FRONT_LAYER_LLM:
        try:
            history = load_dms_history(store, n=flags.FRONT_LAYER_DMS_HISTORY_DAYS) if store else []
            history_dicts = [h.to_dict() for h in history]
            brief = generate_morning_brief(dms_dict, history_dicts)
            brief_source = brief.get("_source", "unknown") if brief else "error"
            morning_brief_cache[f"brief:{today_str}"] = brief
            if store:
                store.set_json(f"front_layer:brief:{today_str}", brief, ttl_s=7 * 86400)
        except Exception as e:
            brief_error = str(e)
            LOG.warning("Refresh: Morning Brief re-generation failed: %s", e)

    # ── 6. LLM diagnostics ─────────────────────────────────────────
    llm_diag: Dict[str, Any] = {
        "enabled": flags.ENABLE_FRONT_LAYER_LLM,
        "brief_source": brief_source,
    }
    if brief_error:
        llm_diag["brief_error"] = brief_error
    if brief and brief.get("_fallback_reason"):
        llm_diag["fallback_reason"] = brief["_fallback_reason"]
    openai_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    llm_diag["openai_key_set"] = bool(openai_key)
    llm_diag["openai_key_len"] = len(openai_key) if openai_key else 0

    return {
        "status": "ok",
        "refreshed_at": now.isoformat().replace("+00:00", "Z"),
        "date": today_str,
        "regime": dms_dict.get("regime", {}),
        "cross_asset_score": dms_dict.get("cross_asset_stress", {}).get("composite_score"),
        "cross_asset_readings": len(dms_dict.get("cross_asset_stress", {}).get("readings", [])),
        "asymmetry_count": len(dms_dict.get("asymmetry_signals", [])),
        "theme_count": len(dms_dict.get("news_themes", [])),
        "brief_regenerated": brief_source == "llm",
        "llm": llm_diag,
    }


@router.get("/api/front-layer/backtest/summary")
def api_backtest_summary():
    """Paper-trade performance summary across all engines."""
    from backend.backtest_engine import get_backtest_summary
    store = get_store_optional()
    return get_backtest_summary(store=store).to_dict()


@router.get("/api/front-layer/backtest/trades")
def api_backtest_trades(
    engine_id: Optional[int] = Query(None, description="Filter by engine ID"),
    status: Optional[str] = Query(None, description="Filter by status: open|closed"),
):
    """List paper trades with optional filtering."""
    from backend.backtest_engine import get_paper_trades
    store = get_store_optional()
    trades = get_paper_trades(store=store, engine_id=engine_id, status=status)
    return {"trades": [t.to_dict() for t in trades], "count": len(trades)}


@router.post("/api/front-layer/backtest/trade")
def api_backtest_log_trade(body: dict = Body(...)):
    """Log a new paper trade from any engine signal."""
    from backend.backtest_engine import PaperTrade, log_paper_trade
    store = get_store_optional()
    trade = PaperTrade(**{k: v for k, v in body.items()
                          if k in PaperTrade.__dataclass_fields__})
    trade_id = log_paper_trade(trade, store=store)
    return {"status": "ok", "tradeId": trade_id}


@router.post("/api/front-layer/backtest/trade/{trade_id}/close")
def api_backtest_close_trade(trade_id: str, body: dict = Body(...)):
    """Close an open paper trade and compute P&L."""
    from backend.backtest_engine import close_paper_trade
    store = get_store_optional()
    if store is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")
    result = close_paper_trade(
        trade_id,
        exit_price=float(body.get("exit_price", 0)),
        exit_reason=str(body.get("exit_reason", "manual")),
        store=store,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found or already closed")
    return {"status": "ok", "trade": result.to_dict()}


@router.get("/api/trade-review/weekly")
def api_weekly_review(week_of: Optional[str] = Query(None)):
    """Get a weekly trade review. Defaults to the most recent available."""
    from backend.weekly_review_engine import get_weekly_review, list_weekly_reviews
    store = get_store_optional()
    if week_of:
        review = get_weekly_review(week_of, store=store)
        if review is None:
            raise HTTPException(status_code=404, detail=f"No review found for week of {week_of}")
        return review
    reviews = list_weekly_reviews(store=store, limit=1)
    if not reviews:
        return {"message": "No weekly reviews generated yet."}
    return get_weekly_review(reviews[0]["weekOf"], store=store)


@router.get("/api/trade-review/weekly/list")
def api_weekly_review_list(limit: int = Query(12, ge=1, le=52)):
    """List available weekly reviews."""
    from backend.weekly_review_engine import list_weekly_reviews
    store = get_store_optional()
    return {"reviews": list_weekly_reviews(store=store, limit=limit)}


@router.post("/api/trade-review/weekly/generate")
def api_weekly_review_generate(body: dict = Body(default={})):
    """Trigger weekly review generation (usually run by cron)."""
    from backend.weekly_review_engine import generate_weekly_review
    store = get_store_optional()
    week_end = None
    if body.get("weekEnd"):
        try:
            week_end = dt.date.fromisoformat(str(body["weekEnd"]))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid weekEnd date format")
    result = generate_weekly_review(week_end=week_end, store=store)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@router.get("/api/front-layer/consensus")
def api_front_layer_consensus():
    """Cross-engine consensus dashboard.

    Aggregates signals from regime (Engine 3), IC scanner (Engine 2),
    VIX fade (Engine 12), and credit stress (Engine 8) into a single
    agreement/disagreement view with composite conviction scoring.
    """
    from backend.consensus_engine import build_consensus_from_apis

    store = get_store_optional()
    regime_data = None
    ic_data = None
    vix_data = None
    credit_data = None

    # Fetch regime from Engine 5 snapshot
    if store is not None:
        try:
            from backend.engine5_snapshot import select_best_snapshot
            flags = get_flags()
            snap = select_best_snapshot(
                store,
                max_age_days=flags.ENGINE5_SNAPSHOT_BEST_MAX_AGE_DAYS,
                snapshot_ttl=flags.ENGINE5_SNAPSHOT_TTL_S,
            )
            if snap:
                regime_data = snap.get("data", {}).get("regime", {})
        except Exception:
            pass

    # Fetch VIX fade from cache
    if store is not None:
        try:
            cached_vix = store.get_json("engine12:cache:scan")
            if cached_vix:
                vix_data = cached_vix
        except Exception:
            pass

    # Fetch credit stress from cache
    if store is not None:
        try:
            cached_credit = store.get_json("engine9:cache:scan")
            if cached_credit:
                credit_data = cached_credit
        except Exception:
            pass

    result = build_consensus_from_apis(
        regime_data=regime_data,
        ic_data=ic_data,
        vix_data=vix_data,
        credit_data=credit_data,
    )
    return result.to_dict()


@router.get("/api/front-layer/vol-surface")
def api_front_layer_vol_surface(ticker: str = Query("SPY", description="Underlying: SPY or SPX")):
    """Intraday vol surface snapshot.

    Monitors option-implied volatility surface for skew, term structure,
    and anomalies. Useful for Engines 2 (IC width selection) and 12
    (VIX fade context).
    """
    from backend.vol_surface_engine import get_vol_surface
    orats = get_client_optional()
    if orats is None:
        raise HTTPException(status_code=503, detail="ORATS unavailable")
    store = get_store_optional()
    surface = get_vol_surface(orats, ticker=ticker.upper(), store=store)
    return surface.to_dict()


@router.get("/api/front-layer/macro-proximity")
def api_front_layer_macro_proximity():
    """System-wide macro event proximity signal.

    Returns upcoming high-impact macro events (FOMC, CPI, NFP, etc.)
    with a composite multiplier and per-engine sizing guidance.
    Consumed by all engines for position sizing and structure selection.
    """
    from backend.macro_calendar_engine import get_macro_proximity
    bz = get_benzinga_client_optional()
    store = get_store_optional()
    result = get_macro_proximity(benzinga_client=bz, store=store)
    return result.to_dict()


@router.get("/api/front-layer/morning-brief")
def api_front_layer_morning_brief():
    """Return today's Morning Brief (LLM-generated)."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    today_str = dt.date.today().isoformat()

    cached = morning_brief_cache.get(f"brief:{today_str}")
    if cached is not None:
        return cached

    store = get_store_optional()
    dms = load_dms(today_str, store) if store else None
    if dms is None:
        dms_dict = _build_live_dms(today_str, store)
    else:
        dms_dict = dms.to_dict()

    history = load_dms_history(store, n=flags.FRONT_LAYER_DMS_HISTORY_DAYS) if store else []
    history_dicts = [h.to_dict() for h in history]

    if flags.ENABLE_FRONT_LAYER_LLM:
        brief = generate_morning_brief(dms_dict, history_dicts)
    else:
        brief = {
            "market_posture": "LLM generation disabled. Review DailyMarketState directly.",
            "changes_vs_yesterday": "Enable ENABLE_FRONT_LAYER_LLM for narrative generation.",
            "active_themes": "See Active Themes panel.",
            "cross_asset_signals": "See Cross-Asset Stress panel.",
            "engine_alignment": "See Engine Gates in DailyMarketState.",
            "watch_list": "None",
            "stand_down": "Review regime state manually.",
            "_source": "disabled",
            "_generated_at": dt.datetime.utcnow().isoformat() + "Z",
        }

    morning_brief_cache[f"brief:{today_str}"] = brief
    return brief


@router.get("/api/front-layer/weekly-roadmap")
def api_front_layer_weekly_roadmap():
    """Return the Weekly Roadmap (LLM-generated, Sunday night)."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    today_str = dt.date.today().isoformat()

    cached = weekly_roadmap_cache.get(f"roadmap:{today_str}")
    if cached is not None:
        return cached

    store = get_store_optional()

    if store:
        roadmap_data = store.get_json(f"front_layer:roadmap:{today_str}")
        if roadmap_data:
            weekly_roadmap_cache[f"roadmap:{today_str}"] = roadmap_data
            return roadmap_data

    dms = load_dms(today_str, store) if store else None
    if dms is None:
        dms_dict = _build_live_dms(today_str, store)
    else:
        dms_dict = dms.to_dict()

    history = load_dms_history(store, n=flags.FRONT_LAYER_DMS_HISTORY_DAYS) if store else []
    history_dicts = [h.to_dict() for h in history]

    if flags.ENABLE_FRONT_LAYER_LLM:
        roadmap = generate_weekly_roadmap(dms_dict, history_dicts)
    else:
        roadmap = {
            "regime_flow_summary": "LLM generation disabled.",
            "expected_pattern": "Check sequencer panel.",
            "high_risk_days": [],
            "engine_behaviors": "See Engine Gates.",
            "earnings_focus": [],
            "asymmetry_radar": "No asymmetries detected.",
            "break_the_plan": "Review regime transition triggers.",
            "_source": "disabled",
            "_generated_at": dt.datetime.utcnow().isoformat() + "Z",
        }

    if store:
        store.set_json(f"front_layer:roadmap:{today_str}", roadmap, ttl_s=7 * 86400)

    weekly_roadmap_cache[f"roadmap:{today_str}"] = roadmap
    return roadmap


@router.get("/api/front-layer/cross-asset-stress")
def api_front_layer_cross_asset():
    """Return live cross-asset stress snapshot."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    store = get_store_optional()
    today_str = dt.date.today().isoformat()
    dms = load_dms(today_str, store) if store else None
    if dms and dms.cross_asset_stress:
        return dms.cross_asset_stress

    return {"readings": [], "composite_score": 50.0, "composite_label": "Neutral", "timestamp": ""}


@router.get("/api/front-layer/news-themes")
def api_front_layer_news_themes():
    """Return active news theme readings."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    store = get_store_optional()
    today_str = dt.date.today().isoformat()

    if store:
        data = store.get_json(f"front_layer:themes:{today_str}")
        if data:
            return data

    dms = load_dms(today_str, store) if store else None
    if dms and dms.news_themes:
        return {"date": today_str, "themes": dms.news_themes}

    return {"date": today_str, "themes": [], "dominant_theme": "", "total_headline_count": 0}


@router.get("/api/front-layer/asymmetry-radar")
def api_front_layer_asymmetry():
    """Return current asymmetry radar signals."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    store = get_store_optional()
    today_str = dt.date.today().isoformat()
    dms = load_dms(today_str, store) if store else None

    if dms and dms.asymmetry_signals:
        return {"signals": dms.asymmetry_signals, "count": len(dms.asymmetry_signals)}

    dms_dict = _build_live_dms(today_str, store)
    signals = dms_dict.get("asymmetry_signals", [])
    return {"signals": signals, "count": len(signals)}


@router.get("/api/front-layer/history")
def api_front_layer_history(days: int = Query(default=7, ge=1, le=120)):
    """Return rolling DMS history."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    store = get_store_optional()
    if not store:
        return {"snapshots": [], "count": 0}

    history = load_dms_history(store, n=days)
    return {
        "snapshots": [h.to_dict() for h in history],
        "count": len(history),
    }


@router.get("/api/front-layer/diff")
def api_front_layer_diff():
    """Return diff between today's and yesterday's DMS."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    store = get_store_optional()
    if not store:
        return {"has_changes": False, "changes": {}, "error": "No persistence layer"}

    today_str = dt.date.today().isoformat()
    yesterday_str = (dt.date.today() - dt.timedelta(days=1)).isoformat()

    today_dms = load_dms(today_str, store)
    yesterday_dms = load_dms(yesterday_str, store)

    if not today_dms or not yesterday_dms:
        return {"has_changes": False, "changes": {}, "error": "Insufficient history for diff"}

    return compute_dms_diff(today_dms, yesterday_dms)


@router.get("/api/front-layer/patterns")
def api_front_layer_patterns():
    """Return pattern templates and current week's match for the Pattern Library UI."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    from backend.sequencer import PATTERN_TEMPLATES, current_week_id, build_weekly_sequence, SequencerEvent

    store = get_store_optional()
    wk = current_week_id()
    events_raw = []
    if store:
        events_raw = store.get_json(f"sequencer:week:{wk}") or []

    events = [SequencerEvent.from_dict(e) for e in events_raw] if events_raw else []
    seq = build_weekly_sequence(week_id=wk, events=events)

    matched = {}
    if seq.pattern_match:
        tmpl = PATTERN_TEMPLATES.get(seq.pattern_match, {})
        matched = {
            "key": seq.pattern_match,
            "label": tmpl.get("label", seq.pattern_match),
            "confidence": int(seq.pattern_confidence * 100),
            "favored_play_types": seq.favored_play_types,
            "primary_risk": seq.primary_risk,
        }

    return {
        "templates": {k: {"label": v["label"], "description": v["description"]} for k, v in PATTERN_TEMPLATES.items()},
        "matched": matched,
        "week_id": wk,
        "event_count": len(events_raw),
    }


@router.get("/api/front-layer/backfill-status")
def api_front_layer_backfill_status():
    """Report whether historical DMS data has been seeded.

    Returns snapshot count, date range, and per-day data quality flags.
    Useful for the UI to show whether the backfill script has been run.
    """
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    store = get_store_optional()
    if not store:
        return {
            "seeded": False,
            "snapshot_count": 0,
            "date_range": None,
            "days": [],
        }

    index = store.get_json(DMS_INDEX_KEY) or []
    if not isinstance(index, list):
        index = []

    if not index:
        return {
            "seeded": False,
            "snapshot_count": 0,
            "date_range": None,
            "days": [],
        }

    days = []
    for date_str in index[:14]:
        dms = load_dms(date_str, store)
        if dms is None:
            continue
        d = dms.to_dict()
        has_cross_asset = bool(d.get("cross_asset_stress", {}).get("readings"))
        has_themes = bool(d.get("news_themes"))
        has_regime = d.get("regime", {}).get("state", "Transitional") != "Transitional" or bool(d.get("regime", {}).get("drivers"))
        days.append({
            "date": date_str,
            "has_cross_asset": has_cross_asset,
            "has_themes": has_themes,
            "has_regime": has_regime,
        })

    sorted_dates = sorted(index)
    return {
        "seeded": len(index) >= 3,
        "snapshot_count": len(index),
        "date_range": {
            "earliest": sorted_dates[0] if sorted_dates else None,
            "latest": sorted_dates[-1] if sorted_dates else None,
        },
        "days": days,
    }


@router.post("/api/front-layer/asset-insight")
def api_front_layer_asset_insight(body: dict):
    """Generate a desk-level LLM insight for a single cross-asset stress reading.

    Request body: { "asset": { ...AssetStressReading dict... } }
    The DMS context is loaded automatically from today's snapshot.
    """
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER or not flags.ENABLE_FRONT_LAYER_LLM:
        raise HTTPException(status_code=503, detail="Front Layer LLM is disabled.")

    asset = body.get("asset")
    if not asset or not isinstance(asset, dict):
        raise HTTPException(status_code=400, detail="Missing 'asset' in request body.")

    today_str = dt.date.today().isoformat()
    store = get_store_optional()
    dms_dict = dms_cache.get(f"dms:{today_str}")
    if not dms_dict and store:
        dms_obj = load_dms(today_str, store)
        if dms_obj:
            dms_dict = dms_obj.to_dict()
    dms_dict = dms_dict or {}

    insight = generate_asset_insight(asset, dms_dict)
    return insight


@router.post("/api/front-layer/card-insight")
def api_front_layer_card_insight(body: dict):
    """Generate a desk-level LLM insight for any MI card type.

    Request body: { "card_type": "composite|theme|regime|flow|asymmetry|diff", "card_data": { ... } }
    """
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER or not flags.ENABLE_FRONT_LAYER_LLM:
        raise HTTPException(status_code=503, detail="Front Layer LLM is disabled.")

    card_type = body.get("card_type", "").strip()
    card_data = body.get("card_data")
    valid_types = {
        # Market Intelligence
        "composite", "theme", "regime", "flow", "asymmetry", "diff",
        # Engine 5 – Lead-Lag
        "e5_regime", "e5_vol", "e5_narrative", "e5_index_bias",
        "e5_sector_bias", "e5_trade_idea", "e5_triggers", "e5_component",
        # Engine 1 – Breach / Earnings Hold Risk
        "e1_decision", "e1_hold_risk", "e1_monte_carlo", "e1_regime",
        "e1_skew_wings", "e1_event_risk", "e1_gamma_context",
        "e1_quarter", "e1_strike_targets", "e1_dealer_gamma",
        # Engine 1 – Earnings Playbook Cards
        "e1_iv_check", "e1_premium_richness", "e1_liquidity_check", "e1_macro_overlay",
        # Engine 2 – SPX Iron Condor Scanner
        "e2_regime", "e2_macro", "e2_odds", "e2_dealer_gamma",
        "e2_gex", "e2_hedging_pressure", "e2_tail_ignition",
        "e2_vol_pressure", "e2_expected_move", "e2_technicals",
        # Engine 3 – Red Dog
        "rd_signal", "rd_gamma", "rd_trend", "rd_scan_summary", "rd_gate",
        # Engine 4 – Ichimoku
        "ik_signal", "ik_gamma", "ik_scan_summary", "ik_gate",
    }

    if card_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Invalid card_type. Must be one of: {', '.join(sorted(valid_types))}")
    if not card_data or not isinstance(card_data, dict):
        raise HTTPException(status_code=400, detail="Missing 'card_data' in request body.")

    today_str = dt.date.today().isoformat()
    store = get_store_optional()
    dms_dict = dms_cache.get(f"dms:{today_str}")
    if not dms_dict and store:
        dms_obj = load_dms(today_str, store)
        if dms_obj:
            dms_dict = dms_obj.to_dict()
    if not dms_dict:
        dms_dict = body.get("dms_summary") or {}

    insight = generate_card_insight(card_type, card_data, dms_dict)
    return insight
