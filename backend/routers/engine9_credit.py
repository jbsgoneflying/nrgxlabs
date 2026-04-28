"""Engine 9 (backend) / Engine 8 (UI): Credit Stress Drift Detection router.

Backend module is named engine9 for historical reasons. Users see this as
Engine 8 in the navigation. See ENGINE_REGISTRY in config.py.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend.deps import (
    LOG,
    get_client,
    get_client_optional,
    get_fred_client_optional,
    get_api_ninjas_client_optional,
    engine9_cache,
    engine9_cache_lock,
)
from backend.config import get_flags
from backend.orats_client import OratsError
from backend.redis_store import get_store_optional

router = APIRouter()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/api/engine9/scan")
def engine9_scan():
    """Full dashboard scan: all tiers, all 8 signals, phase + triggers, forced seller map."""
    flags = get_flags()
    if not getattr(flags, "ENABLE_ENGINE9_CREDIT_STRESS", True):
        raise HTTPException(status_code=404, detail="Engine 9 disabled")

    with engine9_cache_lock:
        cached = engine9_cache.get("scan")
    if cached is not None:
        return cached

    from backend.fred_client import FredClient, SERIES_HY_OAS, SERIES_IG_OAS, SERIES_DGS2, SERIES_DGS10, SERIES_FEDFUNDS
    from backend.engine9_signals import (
        compute_bdc_divergence, compute_spread_signal, compute_nlp_delta_of_language,
        compute_nlp_from_llm_analyses,
        compute_insider_signal, compute_correlation_breakdown, compute_etf_nav_deviation,
        compute_funding_stress, compute_time_compression, compute_weighted_composite,
        evaluate_triggers, evaluate_thesis_health, SignalResult,
    )
    from backend.engine9_watchlist import (
        TIERS, TIER_1_BDCS, TIER_2_ALT_MANAGERS, TIER_3_CREDIT_ETFS, TIER_4_VOL_HEDGES,
        compute_ticker_score, compute_forced_seller_map, compute_put_skew_25d, compute_iv_rank,
        get_structural_profile,
    )
    from backend.eodhd_client import EodhdClient

    fred = get_fred_client_optional()
    orats = get_client_optional()
    ninjas = get_api_ninjas_client_optional()

    try:
        eodhd = EodhdClient.from_env()
    except Exception:
        eodhd = None

    today_str = dt.date.today().isoformat()
    one_year_ago = (dt.date.today() - dt.timedelta(days=365)).isoformat()

    # ── Fetch FRED data ──
    hy_oas_values: list[float] = []
    ig_oas_values: list[float] = []
    dgs2_values: list[float] = []
    dgs10_values: list[float] = []
    ff_latest = None
    ff_30d = None

    if fred:
        try:
            hy_res = fred.get_series(SERIES_HY_OAS, one_year_ago, today_str)
            hy_oas_values = [o.value for o in hy_res.observations if o.value is not None]
        except Exception as e:
            LOG.warning("FRED HY OAS fetch failed: %s", e)
        try:
            ig_res = fred.get_series(SERIES_IG_OAS, one_year_ago, today_str)
            ig_oas_values = [o.value for o in ig_res.observations if o.value is not None]
        except Exception as e:
            LOG.warning("FRED IG OAS fetch failed: %s", e)
        try:
            d2_res = fred.get_series(SERIES_DGS2, one_year_ago, today_str)
            dgs2_values = [o.value for o in d2_res.observations if o.value is not None]
        except Exception as e:
            LOG.warning("FRED DGS2 fetch failed: %s", e)
        try:
            d10_res = fred.get_series(SERIES_DGS10, one_year_ago, today_str)
            dgs10_values = [o.value for o in d10_res.observations if o.value is not None]
        except Exception as e:
            LOG.warning("FRED DGS10 fetch failed: %s", e)
        try:
            ff_res = fred.get_series(SERIES_FEDFUNDS, (dt.date.today() - dt.timedelta(days=60)).isoformat(), today_str)
            ff_vals = [o.value for o in ff_res.observations if o.value is not None]
            if ff_vals:
                ff_latest = ff_vals[-1]
                ff_30d = ff_vals[-30] if len(ff_vals) >= 30 else ff_vals[0]
        except Exception:
            pass

    # ── Fetch price data via EODHD ──
    def _fetch_prices(ticker: str, days: int = 120) -> list[float]:
        if not eodhd:
            return []
        try:
            start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
            resp = eodhd.get_eod(f"{ticker}.US", from_date=start)
            return [float(r.get("adjusted_close") or r.get("close", 0)) for r in (resp.rows or []) if r.get("adjusted_close") or r.get("close")]
        except Exception as e:
            LOG.warning("Engine 9 price fetch failed for %s: %s", ticker, e)
            return []

    all_tickers = TIER_1_BDCS + TIER_2_ALT_MANAGERS + TIER_3_CREDIT_ETFS + TIER_4_VOL_HEDGES + ["SPY"]
    price_data: dict[str, list[float]] = {}

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_fetch_prices, t): t for t in all_tickers}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                price_data[t] = fut.result()
            except Exception:
                price_data[t] = []

    # ── Fetch VIX for spread signal (EODHD uses VIX.INDX) ──
    vix_prices: list[float] = []
    if eodhd:
        try:
            start = (dt.date.today() - dt.timedelta(days=365)).isoformat()
            resp = eodhd.get_eod("VIX.INDX", from_date=start)
            vix_prices = [float(r.get("adjusted_close") or r.get("close", 0)) for r in (resp.rows or []) if r.get("adjusted_close") or r.get("close")]
        except Exception as e:
            LOG.warning("VIX price fetch failed: %s", e)

    # ── Fetch BDC book values from EODHD fundamentals (with timeout) ──
    bdc_book_values: dict[str, float | None] = {}
    if eodhd:
        def _fetch_bv(ticker: str):
            try:
                return ticker, eodhd.get_book_value(f"{ticker}.US")
            except Exception:
                return ticker, None
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futs = {pool.submit(_fetch_bv, tk): tk for tk in TIER_1_BDCS}
                for fut in as_completed(futs, timeout=15):
                    t, bv = fut.result()
                    bdc_book_values[t] = bv
        except TimeoutError:
            LOG.warning("BDC book value fetch timed out after 15s")

    # ── Fetch ETF NAV for Price/NAV signal ──
    etf_nav_map: dict[str, float | None] = {}
    if eodhd:
        for etf_sym in ["HYG", "BKLN", "JNK", "LQD"]:
            try:
                etf_nav_map[etf_sym] = eodhd.get_etf_nav(f"{etf_sym}.US")
            except Exception:
                etf_nav_map[etf_sym] = None

    # ── Compute Signals ──
    signal_results: list[SignalResult] = []

    # Signal 1: BDC Divergence (aggregate across Tier 1)
    bdc_scores = []
    bdc_details = []
    for bdc in TIER_1_BDCS:
        p = price_data.get(bdc, [])
        bv = bdc_book_values.get(bdc)
        sig = compute_bdc_divergence(
            prices_30d=p[-30:] if len(p) >= 30 else p,
            prices_60d=p[-60:] if len(p) >= 60 else p,
            prices_90d=p[-90:] if len(p) >= 90 else p,
            last_book_value=bv,
            current_price=p[-1] if p else None,
        )
        bdc_scores.append(sig.score)
        bdc_details.append({"ticker": bdc, "score": sig.score, "book_value": bv, "price": p[-1] if p else None})
    avg_bdc = sum(bdc_scores) / len(bdc_scores) if bdc_scores else 0
    bdc_signal = SignalResult(
        key="bdc_divergence", label="BDC Divergence",
        score=round(avg_bdc, 1), weight=0.25,
        detail=f"Avg across {len(TIER_1_BDCS)} BDCs", triggered=avg_bdc > 40,
        data={"avg_score": round(avg_bdc, 1), "bdc_count": len(TIER_1_BDCS), "per_bdc": bdc_details},
    )
    signal_results.append(bdc_signal)

    # Signal 2: Spread Acceleration
    spread_signal = compute_spread_signal(hy_oas_values, vix_prices)
    signal_results.append(spread_signal)

    # Signal 3: NLP Delta-of-Language
    nlp_signal = SignalResult(
        key="nlp_language", label="NLP Language Drift",
        score=0, weight=0.05, detail="Awaiting transcript data",
    )
    openai_key = os.getenv("OPENAI_API_KEY", "")
    nlp_tickers = TIER_1_BDCS + TIER_2_ALT_MANAGERS
    analyses_by_ticker: dict[str, list[dict]] = {}

    from backend.engine9_store import load_transcript_history as _load_th
    for tk in nlp_tickers:
        cached_th = _load_th(tk, quarters=4)
        if cached_th:
            analyses_by_ticker[tk] = cached_th

    if analyses_by_ticker:
        nlp_signal = compute_nlp_from_llm_analyses(analyses_by_ticker)
    elif ninjas:
        all_transcripts: list[dict] = []
        for t in nlp_tickers[:4]:
            try:
                transcripts = ninjas.get_transcript_history(t, quarters=4)
                all_transcripts.extend(transcripts)
            except Exception:
                pass
        if all_transcripts:
            nlp_signal = compute_nlp_delta_of_language(all_transcripts)
    signal_results.append(nlp_signal)

    # Signal 4: Insider Selling (per-ticker with Redis baselines)
    from backend.engine9_store import (
        store_insider_latest, load_insider_latest,
        update_insider_baseline, load_insider_baseline,
    )
    insider_totals = {"net_30d": 0, "net_60d": 0, "net_90d": 0, "txn_count": 0}
    insider_30_data: dict[str, float] = {}
    insider_per_ticker: list[dict] = []
    all_tickers_for_insider = TIER_1_BDCS + TIER_2_ALT_MANAGERS
    if ninjas:
        def _fetch_insider(tkr: str):
            try:
                return tkr, ninjas.get_insider_net_selling(tkr, days=90)
            except Exception:
                return tkr, {}
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                futs = {pool.submit(_fetch_insider, tk): tk for tk in all_tickers_for_insider}
                for fut in as_completed(futs, timeout=20):
                    t, data = fut.result()
                    net_90 = data.get("net_selling", 0)
                    txn_ct = data.get("transaction_count", 0)
                    insider_totals["net_90d"] += net_90
                    insider_totals["txn_count"] += txn_ct
                    monthly_net = net_90 / 3.0 if net_90 else 0
                    insider_30_data[t] = monthly_net
                    insider_totals["net_60d"] += net_90 * (2 / 3)
                    store_insider_latest(t, data)
                    baseline = update_insider_baseline(t, monthly_net)
                    baseline_avg = baseline.get("avg", 0)
                    anomaly = abs(monthly_net / baseline_avg) if baseline_avg else 0
                    insider_per_ticker.append({
                        "ticker": t, "net_90d": net_90, "monthly_net": round(monthly_net, 2),
                        "baseline_avg": round(baseline_avg, 2), "anomaly_ratio": round(anomaly, 2),
                        "txn_count": txn_ct,
                    })
        except TimeoutError:
            LOG.warning("Insider data fetch timed out after 20s")
        insider_totals["net_30d"] = sum(insider_30_data.values())

    insider_signal = compute_insider_signal(
        insider_totals["net_30d"], insider_totals["net_60d"],
        insider_totals["net_90d"], insider_totals["txn_count"],
    )
    insider_signal.data = insider_signal.data or {}
    insider_signal.data["per_ticker"] = insider_per_ticker
    signal_results.append(insider_signal)

    # Signal 5: Correlation Breakdown
    spy_prices = price_data.get("SPY", [])
    hyg_prices = price_data.get("HYG", [])
    spy_rets = [(spy_prices[i] / spy_prices[i-1] - 1) for i in range(1, len(spy_prices))] if len(spy_prices) > 1 else []
    hyg_rets = [(hyg_prices[i] / hyg_prices[i-1] - 1) for i in range(1, len(hyg_prices))] if len(hyg_prices) > 1 else []
    corr_signal = compute_correlation_breakdown(spy_rets, hyg_rets, hyg_prices)
    signal_results.append(corr_signal)

    # Signal 6: ETF Price/NAV — check multiple ETFs, use best signal
    _nav_candidates = [
        ("HYG", hyg_prices, etf_nav_map.get("HYG")),
        ("BKLN", price_data.get("BKLN", []), etf_nav_map.get("BKLN")),
        ("JNK", price_data.get("JNK", []), etf_nav_map.get("JNK")),
    ]
    best_nav_signal = None
    for _etf_name, _etf_px, _etf_nav in _nav_candidates:
        _ns = compute_etf_nav_deviation(_etf_px, etf_nav=_etf_nav)
        if _ns.score > 0 or _etf_nav:
            _ns.data = _ns.data or {}
            _ns.data["etf"] = _etf_name
            _ns.data["nav"] = _etf_nav
            if best_nav_signal is None or _ns.score > best_nav_signal.score:
                best_nav_signal = _ns
    if best_nav_signal is None:
        best_nav_signal = compute_etf_nav_deviation(hyg_prices, etf_nav=None)
    best_nav_signal.data = best_nav_signal.data or {}
    best_nav_signal.data["all_navs"] = {k: v for k, v in etf_nav_map.items() if v}
    signal_results.append(best_nav_signal)

    # Signal 7: Funding Stress
    bkln_prices = price_data.get("BKLN", [])
    funding_signal = compute_funding_stress(bkln_prices, hyg_prices, dgs2_values, dgs10_values)
    signal_results.append(funding_signal)

    # Signal 8: Time Compression (with actual trigger history from Redis)
    from backend.engine9_store import load_trigger_dates, append_trigger_dates
    recent_trigger_dates = load_trigger_dates()
    tc_signal = compute_time_compression(signal_results, recent_trigger_dates)
    signal_results.append(tc_signal)

    # Persist today's trigger state for future time compression calculations
    append_trigger_dates(signal_results, today_str)

    # ── News Cycle Signal (context overlay, not scored) ──
    from backend.engine9_signals import filter_credit_news, score_news_with_llm
    from backend.engine9_store import store_news_scan, load_news_scan
    news_data: dict = {}
    today_str_news = dt.date.today().isoformat()
    cached_news = load_news_scan(today_str_news)
    if cached_news:
        news_data = cached_news
    elif eodhd:
        try:
            all_news_articles: list[dict] = []
            week_ago = (dt.date.today() - dt.timedelta(days=7)).isoformat()
            news_tickers = TIER_1_BDCS + TIER_2_ALT_MANAGERS + TIER_3_CREDIT_ETFS
            for tk in news_tickers:
                try:
                    resp = eodhd.get_news(ticker=f"{tk}.US", from_date=week_ago, limit=10)
                    all_news_articles.extend(resp.rows or [])
                except Exception:
                    pass
            relevant = filter_credit_news(all_news_articles)
            relevant = relevant[:20]
            news_data = {"articles": relevant, "llm_scored": False, "avg_relevance": 0}

            api_key = os.getenv("OPENAI_API_KEY", "")
            if api_key and relevant:
                news_data = score_news_with_llm(relevant, api_key)
            store_news_scan(today_str_news, news_data)
        except Exception as e:
            LOG.warning("News cycle scan failed: %s", e)

    # ── Composite & Phase ──
    composite = compute_weighted_composite(signal_results, tc_signal.triggered)

    # ── Triggers ──
    sig_map = {s.key: s for s in signal_results}
    triggers = evaluate_triggers(sig_map, hyg_prices)

    # ── Thesis Health ──
    hy_20d_ma = None
    if len(hy_oas_values) >= 20:
        hy_20d_ma = sum(hy_oas_values[-20:]) / 20
    thesis = evaluate_thesis_health(ff_latest, ff_30d, hy_oas_values[-1] if hy_oas_values else None, hy_20d_ma)

    # ── Watchlist Scores ──
    def _skew_for(ticker: str) -> float | None:
        if not orats:
            return None
        try:
            resp = orats.live_strikes(ticker, fields="strike,putIv,callIv,putDelta,smvVol,spotPrice,stockPrice")
            return compute_put_skew_25d(resp.rows or [])
        except Exception:
            return None

    def _iv_rank_for(ticker: str) -> float | None:
        if not orats:
            return None
        try:
            resp = orats.hist_cores(ticker, trade_date=today_str, fields="tradeDate,ivRank")
            rows = resp.rows or []
            if rows:
                latest = rows[-1] if isinstance(rows, list) else rows
                rank_val = latest.get("ivRank")
                if rank_val is not None:
                    rank_f = float(rank_val)
                    return round(rank_f * 100, 1) if rank_f <= 1.0 else round(rank_f, 1)
        except Exception:
            pass
        return None

    watchlist_by_tier: dict[str, list] = {}
    for tier_key, tier_info in TIERS.items():
        scores = []
        for ticker in tier_info["tickers"]:
            p = price_data.get(ticker, [])
            skew = _skew_for(ticker) if tier_key in ("tier1", "tier2") else None
            insider = insider_30_data.get(ticker, 0) if insider_30_data else None
            iv_rank = _iv_rank_for(ticker) if tier_key in ("tier1", "tier2") else None
            ts = compute_ticker_score(
                ticker, p,
                iv_rank=iv_rank,
                put_skew_25d=skew,
                insider_net_30d=insider,
                current_phase=composite.get("phase", 1),
            )
            scores.append({
                "ticker": ts.ticker, "tier": ts.tier, "price": ts.price,
                "change_5d_pct": ts.change_5d_pct, "change_20d_pct": ts.change_20d_pct,
                "iv_rank": ts.iv_rank, "put_skew_25d": ts.put_skew_25d,
                "insider_net_30d": ts.insider_net_30d, "signal_score": ts.signal_score,
                "phase_alignment": ts.phase_alignment, "conviction": ts.conviction,
            })
        scores.sort(key=lambda x: x["signal_score"], reverse=True)
        watchlist_by_tier[tier_key] = scores

    # ── Forced Seller Map ──
    fsd: dict[str, dict] = {}
    for t in TIER_1_BDCS + TIER_2_ALT_MANAGERS:
        p = price_data.get(t, [])
        chg20 = (p[-1] / p[-21] - 1) * 100 if len(p) >= 21 else None
        profile = get_structural_profile(t)
        fsd[t] = {
            "leverage": profile.get("leverage"),
            "liquidity_mismatch": profile.get("liquidity_mismatch"),
            "retail_exposure": profile.get("retail_exposure"),
            "put_skew_25d": _skew_for(t),
            "price_20d_pct": chg20,
            "insider_net_30d": insider_30_data.get(t, 0) if insider_30_data else None,
        }
    forced_map = compute_forced_seller_map(ticker_data=fsd)

    result = {
        "composite": composite,
        "signals": [{
            "key": s.key, "label": s.label, "score": s.score,
            "weight": s.weight, "detail": s.detail, "triggered": s.triggered,
            "data": s.data,
        } for s in signal_results],
        "triggers": [{
            "name": t.name, "level": t.level, "active": t.active,
            "condition": t.condition, "action": t.action, "sizing": t.sizing,
        } for t in triggers],
        "thesis_health": thesis,
        "forced_seller_map": [{
            "ticker": e.ticker, "tier": e.tier,
            "fragility_score": e.fragility_score, "leverage": e.leverage,
            "liquidity_mismatch": e.liquidity_mismatch,
            "retail_exposure": e.retail_exposure,
            "put_skew_25d": e.put_skew_25d,
            "price_20d_pct": e.price_20d_pct,
            "insider_net_30d": e.insider_net_30d,
        } for e in forced_map],
        "watchlist": watchlist_by_tier,
        "news": news_data,
        "updated_at": dt.datetime.utcnow().isoformat() + "Z",
        "data_freshness": {
            "fred_hy_oas_points": len(hy_oas_values),
            "eodhd_tickers_with_data": sum(1 for v in price_data.values() if len(v) >= 5),
            "eodhd_tickers_total": len(all_tickers),
            "eodhd_stale": sum(1 for v in price_data.values() if len(v) < 5),
            "insider_tickers_queried": len(insider_per_ticker),
            "news_articles_found": len((news_data or {}).get("articles", [])),
            "orats_available": orats is not None,
        },
    }

    with engine9_cache_lock:
        engine9_cache["scan"] = result
    return result


@router.get("/api/engine9/spreads")
def engine9_spreads():
    """Credit spread time series for the chart: HY OAS, IG OAS, 2s10s curve."""
    from backend.fred_client import SERIES_HY_OAS, SERIES_IG_OAS, SERIES_DGS2, SERIES_DGS10
    fred = get_fred_client_optional()
    if not fred:
        raise HTTPException(status_code=503, detail="FRED client unavailable")

    today_str = dt.date.today().isoformat()
    one_year_ago = (dt.date.today() - dt.timedelta(days=365)).isoformat()

    result: dict = {}
    try:
        hy = fred.get_series(SERIES_HY_OAS, one_year_ago, today_str)
        result["hy_oas"] = {
            "dates": [o.date for o in hy.observations if o.value is not None],
            "values": [o.value for o in hy.observations if o.value is not None],
        }
    except Exception:
        result["hy_oas"] = {"dates": [], "values": []}

    try:
        ig = fred.get_series(SERIES_IG_OAS, one_year_ago, today_str)
        result["ig_oas"] = {
            "dates": [o.date for o in ig.observations if o.value is not None],
            "values": [o.value for o in ig.observations if o.value is not None],
        }
    except Exception:
        result["ig_oas"] = {"dates": [], "values": []}

    try:
        d2 = fred.get_series(SERIES_DGS2, one_year_ago, today_str)
        d10 = fred.get_series(SERIES_DGS10, one_year_ago, today_str)
        d2_map = {o.date: o.value for o in d2.observations if o.value is not None}
        d10_map = {o.date: o.value for o in d10.observations if o.value is not None}
        common_dates = sorted(set(d2_map.keys()) & set(d10_map.keys()))
        result["curve_2s10s"] = {
            "dates": common_dates,
            "values": [round(d10_map[d] - d2_map[d], 3) for d in common_dates],
        }
    except Exception:
        result["curve_2s10s"] = {"dates": [], "values": []}

    return result


@router.get("/api/engine9/ticker/{ticker}")
def engine9_ticker_detail(ticker: str):
    """Deep dive on a single ticker: price, IV, skew, insider, transcript history."""
    from backend.engine9_watchlist import compute_put_skew_25d, get_tier_for_ticker
    from backend.eodhd_client import EodhdClient

    ticker = ticker.upper().strip()
    tier = get_tier_for_ticker(ticker)

    orats = get_client_optional()
    ninjas = get_api_ninjas_client_optional()

    try:
        eodhd = EodhdClient.from_env()
    except Exception:
        eodhd = None

    result: dict = {"ticker": ticker, "tier": tier}

    if eodhd:
        try:
            start = (dt.date.today() - dt.timedelta(days=120)).isoformat()
            resp = eodhd.get_eod(f"{ticker}.US", from_date=start)
            prices = [float(r.get("adjusted_close") or r.get("close", 0)) for r in (resp.rows or []) if r.get("adjusted_close") or r.get("close")]
            result["prices"] = prices[-60:]
            result["price"] = prices[-1] if prices else None
            result["change_5d"] = round((prices[-1] / prices[-6] - 1) * 100, 2) if len(prices) >= 6 else None
            result["change_20d"] = round((prices[-1] / prices[-21] - 1) * 100, 2) if len(prices) >= 21 else None
        except Exception as e:
            LOG.warning("Engine 9 ticker detail price fetch failed for %s: %s", ticker, e)
            result["prices"] = []

    if orats:
        try:
            resp = orats.live_strikes(ticker, fields="strike,putIv,callIv,putDelta,smvVol,spotPrice,stockPrice")
            result["put_skew_25d"] = compute_put_skew_25d(resp.rows or [])
        except Exception:
            result["put_skew_25d"] = None

    if ninjas:
        try:
            insider = ninjas.get_insider_net_selling(ticker, days=90)
            result["insider"] = insider
        except Exception:
            result["insider"] = None
        try:
            transcripts = ninjas.get_latest_transcripts(ticker, limit=4)
            result["transcripts"] = transcripts
        except Exception:
            result["transcripts"] = []

    return result


@router.post("/api/engine9/desk-notes")
def engine9_desk_notes(body: dict):
    """LLM-powered credit desk morning brief (GPT-5.5)."""
    import openai

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="OpenAI API key not configured")

    scan_data = body.get("scan_data") or {}

    system_prompt = """You are the head of a credit trading desk at a top-tier quantitative hedge fund.
You are writing a morning brief for the desk, focused on private credit stress and short positioning.

Your tone: direct, professional, no hedging language. Speak like a senior desk head.

You receive the current state of our Credit Stress Drift engine including:
- 8 signal scores with weights
- Current phase (1-4) and composite score
- Active execution triggers (A/B/C)
- Forced seller rankings
- Thesis health indicators

Respond ONLY with valid JSON containing these fields:
{
  "phase_assessment": "2-3 sentence assessment of current credit stress phase",
  "active_triggers_commentary": "commentary on which triggers are active and what they mean for positioning",
  "top_trades": [
    {"instrument": "TICKER", "action": "short/put spread/avoid", "sizing": "% of book", "rationale": "why"}
  ],
  "forced_seller_spotlight": "1-2 sentences on the most vulnerable player and why",
  "risk_flags": "what could go wrong this week",
  "invalidation_triggers": "what would make us unwind positions",
  "position_sizing_guidance": "overall book risk guidance based on current phase"
}"""

    payload_str = json.dumps(scan_data, default=str)[:12000]
    user_msg = f"Current Engine 9 scan state:\n{payload_str}"

    try:
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-5.5",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=1,
            max_tokens=2000,
            response_format={"type": "json_object"},
        )
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].strip()
        try:
            parsed = json.loads(text)
            return parsed
        except json.JSONDecodeError:
            return {"raw_text": text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM call failed: {type(e).__name__}: {e}")


@router.post("/api/engine9/explain")
def engine9_explain(body: dict):
    """Contextual LLM explanation for any Engine 9 section, signal, ticker, or metric."""
    import openai

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="OpenAI API key not configured")

    context_type = body.get("type", "")       # signal, ticker, section, metric
    context_key = body.get("key", "")          # e.g. "bdc_divergence", "ARCC", "forced_seller"
    context_data = body.get("data", {})        # relevant data payload
    scan_summary = body.get("scan_summary", {})  # composite, phase, etc.

    system_prompt = """You are a senior credit research analyst and trading desk head at a top quantitative hedge fund. You specialize in private credit stress detection and short positioning.

A junior analyst or portfolio manager is looking at our proprietary Engine 9 Credit Stress Drift dashboard and needs your expert interpretation of a specific element. Explain it as if you're teaching someone who is smart but new to this specific domain.

Context types:
- "signal": a single signal card (spread_acceleration, bdc_divergence, etc.)
- "ticker": an individual instrument (ARCC, OWL, HYG, etc.)
- "section": an entire dashboard section overview (signal grid, forced seller map, watchlist, execution triggers, thesis health, news cycle, phase composite). For section overviews, synthesize all the data points into a cohesive narrative — how the pieces fit together, what the section is telling us as a whole, and what the aggregate picture means for positioning.
- "tier": a specific watchlist tier (tier1 BDCs, tier2 Alt Managers, tier3 Credit ETFs, tier4 Vol/Tail Hedges). Explain the role this tier plays in credit stress detection, why these instruments matter as a group, how to read the aggregate data, and what this tier's current readings mean for the overall thesis.
- "chart": a visual chart (credit spread history). Interpret the trend, inflection points, and what the shape of the curve tells us about market regime.

Your response must be valid JSON with these keys:
{
  "headline": "1-line summary of what this means right now",
  "what_it_is": "2-3 sentences explaining what this metric/signal/ticker actually measures and why it matters in credit stress detection",
  "current_read": "2-3 sentences interpreting the current values — what does this specific reading tell us about market conditions?",
  "what_to_watch": "2-3 bullet points of specific things to monitor that would change your interpretation",
  "trade_implication": "How this affects positioning — should we be adding, holding, or reducing exposure based on this? Be specific about instruments.",
  "breaking_event_proximity": "How close are we to this signal triggering a structural break? Scale: Far (months), Approaching (weeks), Imminent (days), Active (now)",
  "fixing_event_risk": "What would invalidate or fix this signal? What would tell us the stress is resolving?",
  "desk_note": "1-2 sentences in the voice of a desk head — what would you tell the PM in the morning meeting about this?"
}

Be direct, specific, and actionable. No hedging language. Speak like money is on the line."""

    user_msg = f"Context type: {context_type}\nContext key: {context_key}\n\nData:\n{json.dumps(context_data, default=str)[:6000]}\n\nCurrent scan summary:\n{json.dumps(scan_summary, default=str)[:3000]}"

    try:
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-5.5",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=1,
            max_tokens=1200,
            response_format={"type": "json_object"},
        )
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw_text": text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Explain failed: {type(e).__name__}: {e}")


@router.post("/api/engine9/thesis-scan")
def engine9_thesis_scan(body: dict):
    """
    On-demand GPT-5.5 analysis: new risks, new instruments, scenario projection,
    non-obvious connections across all Engine 9 data.
    Also triggers LLM transcript analysis + news scoring if not cached.
    """
    import openai
    from backend.engine9_store import store_thesis, load_thesis, load_news_scan, store_news_scan
    from backend.engine9_signals import (
        analyze_transcript_llm, filter_credit_news, score_news_with_llm,
    )
    from backend.engine9_watchlist import TIER_1_BDCS, TIER_2_ALT_MANAGERS

    cached = load_thesis()
    force_refresh = body.get("force", False)
    if cached and not force_refresh:
        return cached

    api_key = os.getenv("OPENAI_API_KEY")

    # Trigger LLM transcript analysis for tickers that don't have cached results
    if api_key:
        ninjas = get_api_ninjas_client_optional()
        if ninjas:
            from backend.engine9_store import load_transcript_history as _load_th
            nlp_tickers = TIER_1_BDCS + TIER_2_ALT_MANAGERS
            for tkr in nlp_tickers:
                existing = _load_th(tkr, quarters=4)
                if len(existing) >= 2:
                    continue
                try:
                    raw = ninjas.get_transcript_history(tkr, quarters=4)
                    for rt in raw:
                        y, q = rt.get("year"), rt.get("quarter")
                        if not y or not q:
                            continue
                        already = any(a.get("_year") == y and a.get("_quarter") == q for a in existing)
                        if already:
                            continue
                        text = rt.get("transcript", "")
                        if len(text) > 200:
                            analyze_transcript_llm(tkr, y, q, text, api_key)
                except Exception as e:
                    LOG.warning("Thesis scan: transcript analysis for %s failed: %s", tkr, e)

        # Score news with LLM if not already done
        today_str_news = dt.date.today().isoformat()
        cached_news = load_news_scan(today_str_news)
        if cached_news and not cached_news.get("llm_scored") and cached_news.get("articles"):
            scored = score_news_with_llm(cached_news["articles"], api_key)
            store_news_scan(today_str_news, scored)
    if not api_key:
        raise HTTPException(status_code=503, detail="OpenAI API key not configured")

    scan_data = body.get("scan_data") or {}
    news_data = body.get("news_data") or {}

    system_prompt = """You are a senior credit research analyst at a quantitative hedge fund.
You specialize in detecting structural breaks in private credit markets before they become
consensus trades. You have access to our proprietary Engine 9 signal data.

Analyze ALL the data provided and return ONLY valid JSON with these keys:

{
  "new_risks": [
    {"risk": "specific risk description", "probability": "low/medium/high", "timeline": "days/weeks/months", "impact": "description of market impact"}
  ],
  "new_instruments_to_watch": [
    {"ticker": "SYMBOL", "rationale": "why this should be on our radar", "signal_type": "leading/coincident/lagging"}
  ],
  "scenario_projection_30d": {
    "base_case": {"probability": "X%", "description": "what happens", "positioning": "how to position"},
    "bull_case": {"probability": "X%", "description": "what happens", "positioning": "how to position"},
    "bear_case": {"probability": "X%", "description": "what happens", "positioning": "how to position"}
  },
  "non_obvious_connections": [
    {"observation": "what you noticed", "implication": "what it means for positioning"}
  ],
  "signal_gaps": ["things our engine should be tracking but isn't"],
  "conviction_level": "low/medium/high",
  "one_liner": "single sentence thesis summary for the desk"
}

Think deeply. Look for patterns across signals that individually seem minor but collectively
indicate something. Consider second-order effects. What are the forced sellers going to do next?
Where does liquidity break first?"""

    payload_parts = []
    if scan_data:
        payload_parts.append(f"SIGNAL DATA:\n{json.dumps(scan_data, default=str)[:8000]}")
    if news_data:
        payload_parts.append(f"\nNEWS DATA:\n{json.dumps(news_data, default=str)[:3000]}")

    user_msg = "\n".join(payload_parts) if payload_parts else "No scan data available."

    try:
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-5.5",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=1,
            max_tokens=3000,
            response_format={"type": "json_object"},
        )
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].strip()
        try:
            parsed = json.loads(text)
            parsed["generated_at"] = dt.datetime.utcnow().isoformat() + "Z"
            store_thesis(parsed)
            return parsed
        except json.JSONDecodeError:
            return {"raw_text": text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Thesis scan failed: {type(e).__name__}: {e}")
