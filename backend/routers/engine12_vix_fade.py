"""Engine 12 — VIX Spike Fade / Volatility Dislocation Engine.

Regime-based volatility dislocation engine that detects geopolitical
shock-induced IV overshoot and systematically fades mean-reverting
vol clusters while respecting fat-tail escalation risk.

Endpoints:
  GET  /api/engine12/scan       — Full analysis dashboard
  GET  /api/engine12/historical — Historical shock comparison table
  GET  /api/engine12/simulate   — Custom Monte Carlo with user scenario weights
  POST /api/engine12/explain    — GPT-5.5 contextual desk notes for any card/section
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from cachetools import TTLCache
from fastapi import APIRouter, HTTPException, Query, Request

from backend.config import get_flags

LOG = logging.getLogger(__name__)

router = APIRouter()

_engine12_cache: TTLCache = TTLCache(maxsize=32, ttl=15 * 60)
_engine12_cache_lock = threading.Lock()


def _fetch_eodhd_prices(eodhd, symbol: str, days: int = 120) -> List[float]:
    """Fetch daily close prices from EODHD."""
    try:
        start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        resp = eodhd.get_eod(symbol, from_date=start)
        return [
            float(r.get("adjusted_close") or r.get("close", 0))
            for r in (resp.rows or [])
            if r.get("adjusted_close") or r.get("close")
        ]
    except Exception as e:
        LOG.warning("EODHD price fetch failed for %s: %s", symbol, e)
        return []


def _fetch_live_vix(eodhd) -> Optional[float]:
    """Fetch delayed-live VIX quote via EODHD real-time endpoint (~15min delay)."""
    if eodhd is None:
        return None
    try:
        resp = eodhd.get_live_quote("VIX.INDX")
        for row in resp.rows or ([resp.raw] if isinstance(resp.raw, dict) else []):
            for key in ("close", "previousClose", "last", "price"):
                v = row.get(key)
                if v is not None:
                    fv = float(v)
                    if fv > 5:
                        return fv
    except Exception as e:
        LOG.warning("Live VIX quote failed: %s", e)
    return None


def _is_market_hours() -> bool:
    """Rough check: weekday between 14:00-21:30 UTC (pre-market through close ET)."""
    now = dt.datetime.utcnow()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 840 <= minutes <= 1290  # 14:00 - 21:30 UTC


def _fetch_orats_vix_term_structure(orats) -> Dict[str, Optional[float]]:
    """Fetch SPX IV at 30d/60d/90d DTE using Engine 2's proven fetch_iv_curve.

    Walks back up to 5 business days to find the most recent available data.
    """
    out: Dict[str, Optional[float]] = {"iv_30d": None, "iv_60d": None, "iv_90d": None}
    if orats is None:
        return out

    try:
        from backend.spx_ic.ohlc import fetch_iv_curve
    except ImportError:
        LOG.warning("Could not import fetch_iv_curve from spx_ic")
        return out

    for days_back in range(0, 6):
        try:
            trade_date = dt.date.today() - dt.timedelta(days=days_back)
            curve = fetch_iv_curve(
                orats,
                ticker="SPX",
                trade_date=trade_date,
                dte_targets=[30, 60, 90],
            )
            iv30 = curve.get(30)
            iv60 = curve.get(60)
            iv90 = curve.get(90)

            if iv30 is not None:
                out["iv_30d"] = iv30
                out["iv_60d"] = iv60
                out["iv_90d"] = iv90
                LOG.info("ORATS term structure loaded from %s: 30d=%.1f, 60d=%s, 90d=%s",
                         trade_date.isoformat(), iv30,
                         f"{iv60:.1f}" if iv60 else "N/A",
                         f"{iv90:.1f}" if iv90 else "N/A")
                break
        except Exception as e:
            LOG.warning("ORATS term structure fetch for %s failed: %s",
                        (dt.date.today() - dt.timedelta(days=days_back)).isoformat(), e)
            continue

    return out


def _fetch_vix_option_chain(orats, vix_current: float, dte_target: int = 21) -> Optional[Dict[str, Any]]:
    """Fetch live VIX option chain from ORATS for real bid/ask pricing.

    Tries ticker variants: VIX, $VIX. Returns strike-level data for
    the nearest expiry matching dte_target, or None on failure.
    """
    if orats is None or vix_current <= 0:
        return None

    fields = (
        "strike,callMidIv,putMidIv,callBidPrice,callAskPrice,"
        "putBidPrice,putAskPrice,callMidPrice,putMidPrice,"
        "dte,expirDate,spotPrice,callOpenInterest,putOpenInterest"
    )

    for ticker_variant in ("VIX", "$VIX"):
        try:
            resp = orats.live_strikes(ticker=ticker_variant, fields=fields)
            rows = resp.rows or []
            if not rows:
                continue

            # Find best expiry near dte_target
            dte_vals = set()
            for r in rows:
                d = r.get("dte")
                if d is not None:
                    dte_vals.add(int(d))
            if not dte_vals:
                continue

            best_dte = min(dte_vals, key=lambda d: abs(d - dte_target))
            chain_rows = [r for r in rows if r.get("dte") is not None and int(r["dte"]) == best_dte]
            if not chain_rows:
                continue

            expiry = chain_rows[0].get("expirDate", "")
            spot = None
            for r in chain_rows:
                s = r.get("spotPrice")
                if s and float(s) > 0:
                    spot = float(s)
                    break

            strikes = []
            for r in chain_rows:
                k = r.get("strike")
                if k is None:
                    continue
                strikes.append({
                    "strike": float(k),
                    "callBid": r.get("callBidPrice"),
                    "callAsk": r.get("callAskPrice"),
                    "callMid": r.get("callMidPrice"),
                    "putBid": r.get("putBidPrice"),
                    "putAsk": r.get("putAskPrice"),
                    "putMid": r.get("putMidPrice"),
                    "callOI": r.get("callOpenInterest"),
                    "putOI": r.get("putOpenInterest"),
                })

            LOG.info("VIX chain loaded: %s, %d DTE, %d strikes, ticker=%s", expiry, best_dte, len(strikes), ticker_variant)
            return {
                "available": True,
                "ticker": ticker_variant,
                "expiry": expiry,
                "dte": best_dte,
                "spot": spot,
                "strikes": sorted(strikes, key=lambda s: s["strike"]),
            }
        except Exception as e:
            LOG.debug("VIX chain fetch failed for %s: %s", ticker_variant, e)
            continue

    return None


def _compute_live_structure_pricing(
    chain: Dict[str, Any],
    vix_current: float,
) -> Dict[str, Any]:
    """Compute real bid/ask pricing for all 4 structures from the live chain."""
    strikes = chain.get("strikes", [])
    if not strikes:
        return {}

    def _find_nearest(target: float) -> Optional[Dict[str, Any]]:
        best = None
        best_dist = float("inf")
        for s in strikes:
            d = abs(s["strike"] - target)
            if d < best_dist:
                best_dist = d
                best = s
        return best

    def _safe_float(v) -> Optional[float]:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    result = {}
    expiry = chain.get("expiry", "")
    dte = chain.get("dte", 0)

    # Short Call Spread: sell ATM+2 / buy ATM+7
    cs_short = _find_nearest(vix_current + 2)
    cs_long = _find_nearest(vix_current + 7)
    if cs_short and cs_long:
        s_mid = _safe_float(cs_short.get("callMid"))
        l_mid = _safe_float(cs_long.get("callMid"))
        s_bid = _safe_float(cs_short.get("callBid"))
        l_ask = _safe_float(cs_long.get("callAsk"))
        result["shortCallSpread"] = {
            "shortStrike": cs_short["strike"],
            "longStrike": cs_long["strike"],
            "midCredit": round(s_mid - l_mid, 2) if s_mid and l_mid else None,
            "worstCredit": round(s_bid - l_ask, 2) if s_bid and l_ask else None,
            "expiry": expiry,
            "dte": dte,
        }

    # Long Put: ATM-1
    put_row = _find_nearest(vix_current - 1)
    if put_row:
        result["longPut"] = {
            "strike": put_row["strike"],
            "midCost": _safe_float(put_row.get("putMid")),
            "askCost": _safe_float(put_row.get("putAsk")),
            "expiry": expiry,
            "dte": dte,
        }

    # Long Put Spread: buy ATM-1 / sell ATM-6
    ps_long = _find_nearest(vix_current - 1)
    ps_short = _find_nearest(vix_current - 6)
    if ps_long and ps_short:
        l_mid = _safe_float(ps_long.get("putMid"))
        s_mid = _safe_float(ps_short.get("putMid"))
        result["longPutSpread"] = {
            "longStrike": ps_long["strike"],
            "shortStrike": ps_short["strike"],
            "midDebit": round(l_mid - s_mid, 2) if l_mid and s_mid else None,
            "expiry": expiry,
            "dte": dte,
        }

    # Calendar: ATM+1 (front sell / back buy — we only have one expiry here)
    cal_row = _find_nearest(vix_current + 1)
    if cal_row:
        result["calendarStrike"] = {
            "strike": cal_row["strike"],
            "frontCallMid": _safe_float(cal_row.get("callMid")),
            "expiry": expiry,
            "dte": dte,
            "note": "Back-month pricing requires second expiry — estimate only.",
        }

    return result


def _fetch_dealer_gamma(orats) -> Dict[str, Any]:
    """Fetch SPX dealer gamma context via existing infrastructure."""
    if orats is None:
        return {"netGammaSign": "unknown", "magnitudeBucket": "low"}
    try:
        from backend.dealer_gamma_context import compute_dealer_gamma_context
        rows = orats.live_strikes(
            ticker="SPX",
            fields="strike,gamma,callOpenInterest,putOpenInterest,spotPrice",
        ).rows or []
        if not rows:
            return {"netGammaSign": "unknown", "magnitudeBucket": "low"}
        dg = compute_dealer_gamma_context(rows, band_pct=0.03)
        return {
            "netGammaSign": dg.get("netGammaSign", "unknown"),
            "magnitudeBucket": dg.get("magnitudeBucket", "low"),
            "netGex": dg.get("netGex"),
            "callsGex": dg.get("callsGex"),
            "putsGex": dg.get("putsGex"),
            "topGammaStrikes": dg.get("topGammaStrikes", [])[:3],
        }
    except Exception as e:
        LOG.warning("Dealer gamma fetch failed: %s", e)
        return {"netGammaSign": "unknown", "magnitudeBucket": "low"}


@router.get("/api/engine12/scan")
def engine12_scan(
    request: Request,
    vix_override: Optional[float] = Query(None, ge=5, le=100, description="Manual VIX override for what-if scenarios"),
):
    """Engine 12: Full VIX spike fade analysis dashboard."""
    flags = get_flags()
    if not flags.ENABLE_ENGINE12_VIX_FADE:
        raise HTTPException(status_code=503, detail="Engine 12 (VIX Fade) is disabled.")

    cache_key = ("engine12_scan", dt.date.today().isoformat(), vix_override)
    with _engine12_cache_lock:
        cached = _engine12_cache.get(cache_key)
    if cached is not None:
        return cached

    from backend.deps import get_client_optional
    from backend.eodhd_client import EodhdClient
    from backend.engine12_spike_detector import (
        detect_vix_spike, classify_event_severity,
        estimate_scenario_probabilities, load_shock_db, find_similar_events,
    )
    from backend.engine12_ou_model import calibrate_ou, implied_forward_curve
    from backend.engine12_edge import compute_edge_composite
    from backend.engine12_stress import compute_geopolitical_stress
    from backend.engine12_mc import fit_empirical_jump_distribution, run_vix_fade_mc
    from backend.engine12_structures import recommend_structure

    orats = get_client_optional()
    try:
        eodhd = EodhdClient.from_env()
    except Exception:
        eodhd = None

    warnings: List[str] = []

    # ── Parallel data fetch ──
    lookback = flags.ENGINE12_OU_CALIBRATION_LOOKBACK_DAYS
    vix_closes: List[float] = []
    spx_closes: List[float] = []
    oil_closes: List[float] = []
    gold_closes: List[float] = []
    hyg_closes: List[float] = []
    dxy_closes: List[float] = []
    tlt_closes: List[float] = []
    vixy_closes: List[float] = []

    if eodhd:
        with ThreadPoolExecutor(max_workers=flags.ENGINE12_MAX_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_eodhd_prices, eodhd, "VIX.INDX", lookback): "vix",
                pool.submit(_fetch_eodhd_prices, eodhd, "GSPC.INDX", lookback): "spx",
                pool.submit(_fetch_eodhd_prices, eodhd, "USO.US", 120): "oil",
                pool.submit(_fetch_eodhd_prices, eodhd, "GLD.US", 120): "gold",
                pool.submit(_fetch_eodhd_prices, eodhd, "HYG.US", 120): "hyg",
                pool.submit(_fetch_eodhd_prices, eodhd, "UUP.US", 120): "dxy",
                pool.submit(_fetch_eodhd_prices, eodhd, "TLT.US", 120): "tlt",
                pool.submit(_fetch_eodhd_prices, eodhd, "VIXY.US", 30): "vixy",
            }
            for f in as_completed(futures):
                key = futures[f]
                try:
                    data = f.result()
                    if key == "vix":
                        vix_closes = data
                    elif key == "spx":
                        spx_closes = data
                    elif key == "oil":
                        oil_closes = data
                    elif key == "gold":
                        gold_closes = data
                    elif key == "hyg":
                        hyg_closes = data
                    elif key == "dxy":
                        dxy_closes = data
                    elif key == "tlt":
                        tlt_closes = data
                    elif key == "vixy":
                        vixy_closes = data
                except Exception as e:
                    warnings.append(f"Data fetch failed for {key}: {e}")
    else:
        warnings.append("EODHD not configured. Engine 12 requires price data.")

    if not vix_closes or len(vix_closes) < 30:
        return {
            "engine": "engine12",
            "status": "error",
            "message": "Insufficient VIX data for analysis.",
            "warnings": warnings,
        }

    # ── Live VIX / Override ──
    vix_source = "eod"
    if vix_override is not None:
        vix_closes = vix_closes[:-1] + [vix_override]
        vix_source = "override"
        warnings.append(f"VIX override active: using {vix_override:.2f} instead of market data.")
    elif _is_market_hours() and eodhd:
        live_vix = _fetch_live_vix(eodhd)
        if live_vix is not None:
            vix_closes.append(live_vix)
            vix_source = "live"

    # ── Spike detection ──
    spike = detect_vix_spike(vix_closes)

    # ── Cross-asset stress ──
    stress_weights = {
        "oil": flags.ENGINE12_STRESS_WEIGHT_OIL,
        "gold": flags.ENGINE12_STRESS_WEIGHT_GOLD,
        "hyg": flags.ENGINE12_STRESS_WEIGHT_HYG,
        "dxy": flags.ENGINE12_STRESS_WEIGHT_DXY,
        "tlt_vol": flags.ENGINE12_STRESS_WEIGHT_TLT_VOL,
    }
    geo_stress = compute_geopolitical_stress(
        oil_closes=oil_closes,
        gold_closes=gold_closes,
        hyg_closes=hyg_closes,
        dxy_closes=dxy_closes,
        tlt_closes=tlt_closes,
        weights=stress_weights,
    )

    # ── Dealer gamma ──
    dealer_gamma = {"netGammaSign": "unknown", "magnitudeBucket": "low"}
    if flags.ENGINE12_DEALER_GAMMA_ENABLED:
        dealer_gamma = _fetch_dealer_gamma(orats)

    # ── SPX gap ──
    spx_gap = 0.0
    if len(spx_closes) >= 2:
        spx_gap = (spx_closes[-1] - spx_closes[-2]) / spx_closes[-2] * 100

    # ── Oil gap ──
    oil_gap = 0.0
    if len(oil_closes) >= 2:
        oil_gap = (oil_closes[-1] - oil_closes[-2]) / oil_closes[-2] * 100

    # ── Severity ──
    severity = classify_event_severity(
        vix_spike_pct=spike.spike_pct_above_ma,
        spx_gap_pct=spx_gap,
        oil_gap_pct=oil_gap,
        cross_asset_stress=geo_stress.score,
        dealer_gamma_sign=dealer_gamma["netGammaSign"],
        dealer_gamma_bucket=dealer_gamma["magnitudeBucket"],
    )

    # ── OU calibration (before edges, needed for persistence mispricing) ──
    ou_params = calibrate_ou(vix_closes)
    ou_dict: Dict[str, Any] = {}
    forward_curve: List[Dict[str, Any]] = []
    if ou_params:
        ou_dict = ou_params.to_dict()
        forward_curve = implied_forward_curve(
            ou_params, spike.vix_current, [1, 2, 3, 5, 10, 15, 20, 30],
        )

    # ── ORATS term structure ──
    term_struct = _fetch_orats_vix_term_structure(orats)

    # ── Edge decomposition (before scenarios, edge score feeds probability model) ──
    shock_db = load_shock_db()
    historical_rvs = [evt.get("rv_5d_after", 0) for evt in shock_db if evt.get("rv_5d_after")]

    edge_composite = compute_edge_composite(
        vix_spot=spike.vix_current,
        iv_30d=term_struct.get("iv_30d"),
        iv_60d=term_struct.get("iv_60d"),
        iv_90d=term_struct.get("iv_90d"),
        ou_params=ou_params,
        historical_rv_post_events=historical_rvs,
        vixy_closes=vixy_closes,
    )

    # ── Scenario probabilities (uses ALL signals: severity, gamma, stress, edges, history) ──
    scenarios = estimate_scenario_probabilities(
        severity.score,
        dealer_gamma_sign=dealer_gamma["netGammaSign"],
        dealer_gamma_bucket=dealer_gamma["magnitudeBucket"],
        cross_asset_stress=geo_stress.score,
        vix_spike_pct=spike.spike_pct_above_ma,
        spx_gap_pct=spx_gap,
        oil_gap_pct=oil_gap,
        edge_score=edge_composite.score,
        pre_event_regime=spike.pre_event_regime,
        shock_db=shock_db,
    )

    # ── Monte Carlo ──
    jump_dist = fit_empirical_jump_distribution(shock_db)
    mc_result = None
    if ou_params:
        mc_result = run_vix_fade_mc(
            vix_current=spike.vix_current,
            ou_params=ou_params,
            scenario_probs=(scenarios.p_contained, scenarios.p_disruption, scenarios.p_escalation),
            jump_dist=jump_dist,
            dealer_gamma_sign=dealer_gamma["netGammaSign"],
            dealer_gamma_bucket=dealer_gamma["magnitudeBucket"],
            n_sims=flags.ENGINE12_MC_N_SIMS,
            seed=flags.ENGINE12_MC_SEED,
        )

    # ── Structure recommendation ──
    recommendation = None
    if mc_result:
        edge_details = {}
        for e in edge_composite.edges:
            edge_details[e.edge_id] = e.score

        recommendation = recommend_structure(
            edge_score=edge_composite.score,
            edge_details=edge_details,
            mc_result=mc_result,
            severity_score=severity.score,
            p_contained=scenarios.p_contained,
            p_disruption=scenarios.p_disruption,
            p_escalation=scenarios.p_escalation,
            secondary_spike_threshold=flags.ENGINE12_SECONDARY_SPIKE_THRESHOLD,
            contained_threshold=flags.ENGINE12_CONTAINED_THRESHOLD,
            dealer_gamma_sign=dealer_gamma.get("netGammaSign", "unknown"),
            dealer_gamma_bucket=dealer_gamma.get("magnitudeBucket", "low"),
        )

    # ── Live VIX option chain (best-effort) ──
    live_chain = None
    live_pricing = {}
    if orats:
        live_chain = _fetch_vix_option_chain(orats, spike.vix_current)
        if live_chain:
            live_pricing = _compute_live_structure_pricing(live_chain, spike.vix_current)

    # ── Historical comparisons (top 5) ──
    similar = find_similar_events(
        vix_spike_pct=spike.spike_pct_above_ma,
        spx_gap_pct=spx_gap,
        oil_gap_pct=oil_gap,
        shock_db=shock_db,
        top_n=5,
    )

    result = {
        "engine": "engine12",
        "status": "ok",
        "asOfDate": dt.date.today().isoformat(),
        "updatedAt": dt.datetime.utcnow().isoformat() + "Z",
        "vixSource": vix_source,
        "spike": spike.to_dict(),
        "severity": severity.to_dict(),
        "scenarios": scenarios.to_dict(),
        "dealerGamma": dealer_gamma,
        "crossAssetStress": geo_stress.to_dict(),
        "ouModel": ou_dict,
        "forwardCurve": forward_curve,
        "termStructure": term_struct,
        "edgeComposite": edge_composite.to_dict(),
        "monteCarlo": mc_result.to_dict() if mc_result else None,
        "jumpDistribution": jump_dist.to_dict(),
        "recommendation": recommendation.to_dict() if recommendation else None,
        "liveChain": {
            "available": live_chain is not None,
            "chain": {k: v for k, v in (live_chain or {}).items() if k != "strikes"} if live_chain else None,
            "pricing": live_pricing,
        },
        "historicalComparisons": similar[:5],
        "warnings": warnings,
    }

    with _engine12_cache_lock:
        _engine12_cache[cache_key] = result
    return result


@router.get("/api/engine12/alert")
def engine12_alert():
    """Engine 12: Check spike alert state from Redis (lightweight, no auth)."""
    from backend.redis_store import get_store_optional
    store = get_store_optional()
    if not store:
        return {"detected": False, "note": "Redis unavailable."}
    alert = store.get_json("e12:alert:latest")
    if alert is None:
        return {"detected": False, "note": "No alert data. Monitor may not be running."}
    return alert


@router.get("/api/engine12/historical")
def engine12_historical():
    """Engine 12: Historical geopolitical shock comparison table."""
    flags = get_flags()
    if not flags.ENABLE_ENGINE12_VIX_FADE:
        raise HTTPException(status_code=503, detail="Engine 12 (VIX Fade) is disabled.")

    from backend.engine12_spike_detector import load_shock_db
    from backend.engine12_mc import fit_empirical_jump_distribution

    shock_db = load_shock_db()
    jump_dist = fit_empirical_jump_distribution(shock_db)

    enriched = []
    for evt in shock_db:
        vix_open = evt.get("vix_event_open", 0)
        peak = evt.get("peak_vix", vix_open)
        vix_pre = evt.get("vix_pre_close", 0)
        e = dict(evt)
        e["jumpRatio"] = round(peak / vix_open, 3) if vix_open > 0 else None
        e["spikePct"] = round((vix_open - vix_pre) / vix_pre * 100, 1) if vix_pre > 0 else None
        e["decayTo5d"] = round(
            (evt.get("vix_5d_after", 0) - vix_open) / vix_open * 100, 1
        ) if vix_open > 0 else None
        e["decayTo10d"] = round(
            (evt.get("vix_10d_after", 0) - vix_open) / vix_open * 100, 1
        ) if vix_open > 0 else None
        enriched.append(e)

    return {
        "engine": "engine12",
        "events": enriched,
        "jumpDistribution": jump_dist.to_dict(),
        "eventCount": len(enriched),
    }


@router.get("/api/engine12/simulate")
def engine12_simulate(
    request: Request,
    p_contained: float = Query(0.55, ge=0.0, le=1.0, description="Probability of contained scenario"),
    p_disruption: float = Query(0.28, ge=0.0, le=1.0, description="Probability of disruption scenario"),
    p_escalation: float = Query(0.17, ge=0.0, le=1.0, description="Probability of escalation scenario"),
    vix_current: Optional[float] = Query(None, description="Override current VIX level"),
    n_days: int = Query(10, ge=1, le=30, description="Simulation horizon in trading days"),
):
    """Engine 12: Custom Monte Carlo with user scenario weights."""
    flags = get_flags()
    if not flags.ENABLE_ENGINE12_VIX_FADE:
        raise HTTPException(status_code=503, detail="Engine 12 (VIX Fade) is disabled.")

    from backend.eodhd_client import EodhdClient
    from backend.engine12_ou_model import calibrate_ou
    from backend.engine12_spike_detector import load_shock_db
    from backend.engine12_mc import fit_empirical_jump_distribution, run_vix_fade_mc
    from backend.engine12_structures import recommend_structure
    from backend.deps import get_client_optional

    try:
        eodhd = EodhdClient.from_env()
    except Exception:
        raise HTTPException(status_code=503, detail="EODHD not configured.")

    lookback = flags.ENGINE12_OU_CALIBRATION_LOOKBACK_DAYS
    vix_closes = _fetch_eodhd_prices(eodhd, "VIX.INDX", lookback)
    if not vix_closes or len(vix_closes) < 60:
        raise HTTPException(status_code=400, detail="Insufficient VIX data.")

    actual_vix = vix_closes[-1]
    vix = vix_current if vix_current is not None else actual_vix

    ou_params = calibrate_ou(vix_closes)
    if ou_params is None:
        raise HTTPException(status_code=500, detail="OU calibration failed.")

    total = p_contained + p_disruption + p_escalation
    if total <= 0:
        raise HTTPException(status_code=400, detail="Scenario probabilities must sum to > 0.")

    shock_db = load_shock_db()
    jump_dist = fit_empirical_jump_distribution(shock_db)

    orats = get_client_optional()
    dealer_gamma = _fetch_dealer_gamma(orats) if flags.ENGINE12_DEALER_GAMMA_ENABLED else {}

    mc_result = run_vix_fade_mc(
        vix_current=vix,
        ou_params=ou_params,
        scenario_probs=(p_contained / total, p_disruption / total, p_escalation / total),
        jump_dist=jump_dist,
        dealer_gamma_sign=dealer_gamma.get("netGammaSign", "unknown"),
        dealer_gamma_bucket=dealer_gamma.get("magnitudeBucket", "low"),
        n_sims=flags.ENGINE12_MC_N_SIMS,
        n_days=n_days,
        seed=flags.ENGINE12_MC_SEED,
    )

    edge_details = {}
    recommendation = recommend_structure(
        edge_score=50.0,
        edge_details=edge_details,
        mc_result=mc_result,
        severity_score=50.0,
        p_contained=p_contained / total,
        p_disruption=p_disruption / total,
        p_escalation=p_escalation / total,
        secondary_spike_threshold=flags.ENGINE12_SECONDARY_SPIKE_THRESHOLD,
        contained_threshold=flags.ENGINE12_CONTAINED_THRESHOLD,
        dealer_gamma_sign=dealer_gamma.get("netGammaSign", "unknown"),
        dealer_gamma_bucket=dealer_gamma.get("magnitudeBucket", "low"),
    )

    return {
        "engine": "engine12",
        "status": "ok",
        "vixCurrent": round(vix, 2),
        "scenarioWeights": {
            "pContained": round(p_contained / total, 3),
            "pDisruption": round(p_disruption / total, 3),
            "pEscalation": round(p_escalation / total, 3),
        },
        "nDays": n_days,
        "ouModel": ou_params.to_dict(),
        "dealerGamma": dealer_gamma,
        "monteCarlo": mc_result.to_dict(),
        "recommendation": recommendation.to_dict(),
    }


# ---------------------------------------------------------------------------
# GPT-5.5 Contextual Desk Notes
# ---------------------------------------------------------------------------

_E12_SYSTEM_PROMPT = """You are a senior volatility trader and quant strategist running the VIX options desk at a top quantitative family office. You have 20+ years of experience fading geopolitical VIX spikes.

A desk agent is looking at NRGX Labs Engine 12 — the VIX Spike Fade / Volatility Dislocation Engine — and needs your expert interpretation of a specific dashboard element. This engine detects geopolitical shock-induced IV overshoot and helps the desk systematically fade mean-reverting vol clusters while respecting fat-tail escalation risk.

Context types you may receive:
- "regime": The regime dashboard — spike detection, severity, dealer gamma state, cross-asset stress. Explain what the current regime means for trading, whether conditions favor fading the spike, and what would change your mind.
- "edge": An individual edge or the composite edge score. Explain what this edge measures, how strong the signal is, and how the desk should think about it for structure selection.
- "ou_model": The Ornstein-Uhlenbeck mean-reversion model — calibrated half-life, theta, forward curve. Explain what the calibration tells us about VIX dynamics, how fast the spike should decay, and what the forward curve implies for entry timing.
- "scenarios": The scenario probabilities (contained/disruption/escalation) and their adjustments. Explain what's driving the probabilities, how dealer gamma and cross-asset stress are shifting them, and how the desk should use the re-simulate sliders to stress-test alternative scenarios.
- "recommendation": The structure recommendation and position sizing. Explain WHY this structure was chosen over alternatives, how to think about the trade, entry timing, what to watch for, and when to cut.
- "mc_results": The Monte Carlo P&L table across all structures. Explain how to read the Sharpe ratios, CVaR, and probability of profit — what the numbers are actually telling the desk about risk/reward.
- "historical": The historical geopolitical shock comparison table. Explain which past events are most analogous to current conditions, what the jump ratios and decay patterns tell us, and what lessons from history apply now.
- "persistence": The persistence mispricing metric (implied half-life vs modeled half-life). This is the most quantitative edge — explain it clearly, what the number means in practical terms, and how it translates to dollars.

Your response must be valid JSON with these keys:
{
  "headline": "1-line bold summary of the key takeaway",
  "what_it_is": "2-3 sentences: what this dashboard element actually measures and why it matters for a VIX fade trade",
  "current_read": "3-4 sentences: interpret the CURRENT values — what do these specific numbers tell us right now? Be precise with the data.",
  "how_to_trade": "3-4 sentences: specific, actionable trading guidance. What structure, what strikes relative to current VIX, what DTE, when to enter. Speak like you're giving instructions to the execution desk.",
  "what_to_watch": "3-4 bullet points: specific things that would change the thesis. Include concrete levels (e.g., 'if VIX reclaims 28 intraday') not vague statements.",
  "re_simulate_hint": "2-3 sentences: how the desk should use the scenario sliders to stress-test this. What scenario weight adjustments would stress the current recommendation?",
  "desk_note": "2-3 sentences in the voice of a desk head at the morning meeting — direct, no hedging, tell the PM what matters."
}

Rules:
- Be direct, specific, and quantitative. No hedge-fund-letter prose.
- Reference actual numbers from the data — don't generalize.
- When discussing structures, be specific about the trade: 'sell the 28/33 call spread in May VIX, 14 DTE' not 'consider a call spread'.
- When discussing risk, quantify it: '$X max loss per contract' not 'limited risk'.
- Speak like money is on the line because it is."""


@router.post("/api/engine12/explain")
def engine12_explain(body: dict):
    """Engine 12: GPT-5.5 contextual desk notes for any card or section."""
    flags = get_flags()
    if not flags.ENABLE_ENGINE12_VIX_FADE:
        raise HTTPException(status_code=503, detail="Engine 12 disabled.")

    import openai

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="OpenAI API key not configured.")

    context_type = body.get("type", "")
    context_key = body.get("key", "")
    context_data = body.get("data", {})
    scan_summary = body.get("scan_summary", {})

    user_msg = (
        f"Context type: {context_type}\n"
        f"Context key: {context_key}\n\n"
        f"Data:\n{json.dumps(context_data, default=str)[:8000]}\n\n"
        f"Full scan summary:\n{json.dumps(scan_summary, default=str)[:4000]}"
    )

    try:
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-5.5",
            messages=[
                {"role": "system", "content": _E12_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=1,
            max_completion_tokens=4000,
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
        LOG.exception("Engine 12 explain failed")
        raise HTTPException(status_code=500, detail=f"LLM call failed: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Post-Trade Tracking
# ---------------------------------------------------------------------------

_TRADE_KEY_PREFIX = "e12:trades:"
_TRADE_INDEX_KEY = "e12:trades:index"
_TRADE_TTL_S = 30 * 86400  # 30 days


@router.post("/api/engine12/trade")
def engine12_log_trade(body: dict):
    """Log a new VIX fade trade for tracking actual vs predicted decay."""
    from backend.redis_store import get_store_optional
    from backend.engine12_ou_model import calibrate_ou, implied_forward_curve, OUParams

    store = get_store_optional()
    if not store:
        raise HTTPException(status_code=503, detail="Redis unavailable.")

    trade_id = f"{int(dt.datetime.utcnow().timestamp())}"
    entry_vix = body.get("entryVix")
    structure = body.get("structure", "")
    strikes = body.get("strikes", {})
    entry_credit = body.get("entryCredit")

    if not entry_vix or not structure:
        raise HTTPException(status_code=400, detail="Missing entryVix or structure.")

    ou_data = body.get("ouParams", {})
    ou_params = None
    if ou_data and ou_data.get("kappa"):
        ou_params = OUParams(
            kappa=float(ou_data["kappa"]),
            theta=float(ou_data.get("theta", 17)),
            sigma=float(ou_data.get("sigma", 5)),
            n_obs=int(ou_data.get("nObs", 0)),
            r_squared=float(ou_data.get("rSquared", 0)),
        )

    expected_path = []
    if ou_params:
        expected_path = implied_forward_curve(
            ou_params, float(entry_vix), [1, 2, 3, 5, 7, 10, 15, 20, 30],
        )

    trade = {
        "tradeId": trade_id,
        "entryDate": dt.date.today().isoformat(),
        "entryVix": round(float(entry_vix), 2),
        "structure": structure,
        "strikes": strikes,
        "entryCredit": entry_credit,
        "ouParams": ou_data,
        "expectedPath": expected_path,
        "status": "active",
    }

    store.set_json(f"{_TRADE_KEY_PREFIX}{trade_id}", trade, ttl_s=_TRADE_TTL_S)

    # Update index
    index = store.get_json(_TRADE_INDEX_KEY) or []
    index.append(trade_id)
    index = index[-50:]  # cap at 50 trades
    store.set_json(_TRADE_INDEX_KEY, index, ttl_s=_TRADE_TTL_S)

    return {"status": "ok", "tradeId": trade_id, "trade": trade}


@router.get("/api/engine12/trades")
def engine12_list_trades():
    """List active trades with actual VIX path vs OU prediction."""
    from backend.redis_store import get_store_optional

    store = get_store_optional()
    if not store:
        return {"trades": []}

    index = store.get_json(_TRADE_INDEX_KEY) or []
    trades = []

    try:
        from backend.eodhd_client import EodhdClient
        eodhd = EodhdClient.from_env()
    except Exception:
        eodhd = None

    current_vix = None
    if eodhd:
        live = _fetch_live_vix(eodhd)
        if live:
            current_vix = live
        else:
            prices = _fetch_eodhd_prices(eodhd, "VIX.INDX", 5)
            if prices:
                current_vix = prices[-1]

    for tid in reversed(index):
        trade = store.get_json(f"{_TRADE_KEY_PREFIX}{tid}")
        if not trade or trade.get("status") != "active":
            continue

        entry_date = trade.get("entryDate", "")
        entry_vix = trade.get("entryVix", 0)
        expected_path = trade.get("expectedPath", [])

        # Compute days held
        days_held = 0
        try:
            ed = dt.date.fromisoformat(entry_date)
            days_held = (dt.date.today() - ed).days
        except Exception:
            pass

        # Fetch actual VIX path since entry
        actual_path: List[float] = []
        if eodhd and entry_date:
            try:
                resp = eodhd.get_eod("VIX.INDX", from_date=entry_date)
                actual_path = [
                    float(r.get("adjusted_close") or r.get("close", 0))
                    for r in (resp.rows or [])
                    if r.get("adjusted_close") or r.get("close")
                ]
            except Exception:
                pass

        # Expected VIX at current day
        expected_now = entry_vix
        for pt in expected_path:
            if pt.get("horizon_days", 0) <= days_held:
                expected_now = pt.get("expected_vix", entry_vix)

        deviation = (current_vix - expected_now) if current_vix else None
        status_label = "on_track"
        if deviation is not None:
            if deviation > 2:
                status_label = "behind_model"
            elif deviation < -2:
                status_label = "ahead_of_model"

        trade["daysHeld"] = days_held
        trade["currentVix"] = round(current_vix, 2) if current_vix else None
        trade["expectedVixNow"] = round(expected_now, 2)
        trade["deviation"] = round(deviation, 2) if deviation is not None else None
        trade["trackingStatus"] = status_label
        trade["actualPath"] = actual_path
        trades.append(trade)

    return {"trades": trades}


@router.post("/api/engine12/trade/{trade_id}/close")
def engine12_close_trade(trade_id: str, body: dict = {}):
    """Close an active trade."""
    from backend.redis_store import get_store_optional

    store = get_store_optional()
    if not store:
        raise HTTPException(status_code=503, detail="Redis unavailable.")

    trade = store.get_json(f"{_TRADE_KEY_PREFIX}{trade_id}")
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found.")

    trade["status"] = "closed"
    trade["closeDate"] = dt.date.today().isoformat()
    trade["exitVix"] = body.get("exitVix")
    trade["exitCredit"] = body.get("exitCredit")
    store.set_json(f"{_TRADE_KEY_PREFIX}{trade_id}", trade, ttl_s=_TRADE_TTL_S)

    return {"status": "ok", "trade": trade}
