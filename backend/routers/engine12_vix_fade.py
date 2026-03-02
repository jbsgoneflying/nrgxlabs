"""Engine 12 — VIX Spike Fade / Volatility Dislocation Engine.

Regime-based volatility dislocation engine that detects geopolitical
shock-induced IV overshoot and systematically fades mean-reverting
vol clusters while respecting fat-tail escalation risk.

Endpoints:
  GET  /api/engine12/scan       — Full analysis dashboard
  GET  /api/engine12/historical — Historical shock comparison table
  GET  /api/engine12/simulate   — Custom Monte Carlo with user scenario weights
  POST /api/engine12/explain    — GPT-5.3 contextual desk notes for any card/section
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


def _fetch_orats_vix_term_structure(orats) -> Dict[str, Optional[float]]:
    """Fetch VIX IV at multiple DTEs via ORATS hist_monies_implied."""
    out: Dict[str, Optional[float]] = {"iv_30d": None, "iv_60d": None, "iv_90d": None}
    if orats is None:
        return out
    try:
        today = dt.date.today().isoformat()
        resp = orats.hist_monies_implied(
            ticker="SPX",
            trade_date=today,
            fields="tradeDate,dte,vol50",
        )
        for row in resp.rows or []:
            dte = row.get("dte")
            vol50 = row.get("vol50")
            if dte is None or vol50 is None:
                continue
            dte = int(dte)
            vol = float(vol50) * 100  # convert to percentage
            if 20 <= dte <= 40 and out["iv_30d"] is None:
                out["iv_30d"] = vol
            elif 50 <= dte <= 70 and out["iv_60d"] is None:
                out["iv_60d"] = vol
            elif 80 <= dte <= 100 and out["iv_90d"] is None:
                out["iv_90d"] = vol
    except Exception as e:
        LOG.warning("ORATS VIX term structure fetch failed: %s", e)
    return out


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
    date: Optional[str] = Query(None, description="Analysis date (YYYY-MM-DD), defaults to today"),
):
    """Engine 12: Full VIX spike fade analysis dashboard."""
    flags = get_flags()
    if not flags.ENABLE_ENGINE12_VIX_FADE:
        raise HTTPException(status_code=503, detail="Engine 12 (VIX Fade) is disabled.")

    cache_key = ("engine12_scan", date or dt.date.today().isoformat())
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
        )

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
        "asOfDate": date or dt.date.today().isoformat(),
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
        "historicalComparisons": similar[:5],
        "warnings": warnings,
    }

    with _engine12_cache_lock:
        _engine12_cache[cache_key] = result
    return result


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
# GPT-5.3 Contextual Desk Notes
# ---------------------------------------------------------------------------

_E12_SYSTEM_PROMPT = """You are a senior volatility trader and quant strategist running the VIX options desk at a top quantitative family office. You have 20+ years of experience fading geopolitical VIX spikes.

A desk agent is looking at Raven-Tech Engine 12 — the VIX Spike Fade / Volatility Dislocation Engine — and needs your expert interpretation of a specific dashboard element. This engine detects geopolitical shock-induced IV overshoot and helps the desk systematically fade mean-reverting vol clusters while respecting fat-tail escalation risk.

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
    """Engine 12: GPT-5.3 contextual desk notes for any card or section."""
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
            model="gpt-5.3",
            messages=[
                {"role": "system", "content": _E12_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=1500,
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
