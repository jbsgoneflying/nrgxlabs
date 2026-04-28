"""Engine 8 (backend) / Engine 7 (UI): Post-Event Trade Extension router.

Backend module is named engine8 for historical reasons. Users see this as
Engine 7 in the navigation. See ENGINE_REGISTRY in config.py.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend.deps import (
    LOG,
    get_client,
    get_client_optional,
    get_benzinga_client_optional,
    get_fmp_client_optional,
    breach_cache,
    breach_cache_lock,
    breach_cache_key,
)
from backend.config import get_flags
from backend.orats_client import OratsError
from backend.redis_store import get_store_optional

router = APIRouter()


# ---------------------------------------------------------------------------
# LLM system prompts
# ---------------------------------------------------------------------------

_E8_DESK_NOTES_SYSTEM = """You are a senior quant on an options-focused systematic desk.
A junior desk quant is reviewing an earnings playbook for an upcoming earnings event.
They need your guidance on how to interpret and trade the scenarios.

You will receive a JSON payload with:
- ticker, earnings_date, timing (BMO/AMC)
- breach_stats: historical breach rate, avg overshoot, realized/implied ratio
- expected_move: ORATS EM %, straddle EM, strike targets at 1.0x/1.5x/2.0x
- playbook: scenario matrix with magnitude buckets, continuation/reversion rates, actions
- thresholds: dollar price levels at EM multiples

Write a concise desk briefing in this exact JSON structure:

{
  "overall_thesis": "3-4 sentences: What this ticker's earnings history tells us. Is it a momentum name (gaps continue) or a mean-reverter (gaps fade)? What's the historical edge?",
  "iron_condor_view": "3-4 sentences: Given the breach rate and EM data, how should we think about selling an iron condor here? Wing placement relative to EM multiples. Is the premium worth the risk given the breach history?",
  "scenario_playbook": "4-6 sentences: Walk through the key scenarios. If it gaps up within EM — what do we do? If it gaps beyond 1.5x EM? If it gaps down? Reference the actual continuation rates and drift numbers.",
  "entry_timing": "2-3 sentences: When to put the trade on (days before earnings?), how to manage delta exposure into the event, and when to act post-announcement.",
  "risk_management": "2-3 sentences: Position sizing relative to the EM, stop-loss levels, max acceptable loss. How the breach rate informs our risk budget.",
  "what_breaks_it": "2-3 sentences: What scenario invalidates the playbook — regime change, unusual vol, earnings restatement, guidance surprise beyond historical norms.",
  "desk_takeaway": "2-3 sentences: The one key insight a junior quant should remember about trading this name around earnings. What makes this ticker different from the average stock."
}

Key data fields in each scenario:
- high_vol_pct: % of events where volume was >1.5x the 20-day average. High volume confirms information flow (continuation). Low volume = overreaction (fade candidate).
- hold_pct: % of events where the gap held intraday (didn't fade by close). HOLD events have the strongest PEAD (post-earnings drift).
- optimal_hold_days: the horizon (1d, 3d, or 5d) with the highest continuation rate — the suggested holding period.
- continuation_rate_3d: the 3-day continuation rate — often the sweet spot for PEAD capture.
- avg_rel_volume: average relative volume across events in this scenario.

Rules:
- Write as a senior quant talking to a junior: clear, direct, practical.
- Reference the ACTUAL numbers from the data (breach rate, EM %, continuation rates, volume, specific dollar levels).
- Be specific about this ticker — don't give generic earnings trading advice.
- If breach rate is high (>25%), emphasize the risk to short premium strategies.
- If high_vol_pct is high (>60%), note that volume confirms the gap is real (not overreaction).
- If continuation rates are strongly directional, highlight the PEAD opportunity and recommend the optimal_hold_days.
- If HOLD structure events dominate (hold_pct > 50%), call out that gap-and-hold is the strongest PEAD signal.
- Keep each field under 100 words.
- Output valid JSON only."""


_E8_ROW_PLAYBOOK_SYSTEM = """You are a senior quant on an options-focused systematic desk writing a trade ticket for ONE specific earnings scenario.

Context: The desk runs short iron condors into earnings. After the event, the IC is closed or expires. The trader now needs to decide whether to deploy a directional follow-through trade based on the gap that occurred. Your job is to give them an actionable blueprint for THIS specific scenario.

You will receive a JSON payload with:
- scenario: a single row from the scenario matrix (magnitude bucket, direction, structure, continuation/reversion rates at 1d/3d/5d, drift, volume confirmation, hold %, optimal hold days, action, confidence, reason)
- matched_events: the actual historical earnings events that fell into this bucket (dates, actual moves, forward returns, volume)
- context.ticker, context.stock_price, context.em_pct
- context.breach_stats: historical breach rate, overshoot, realized/implied ratio
- context.thresholds: dollar price levels at 1.0x/1.5x/2.0x EM
- context.strike_targets: IC wing distances at EM multiples
- context.dealer_flow (when available): real-time dealer gamma positioning for both the ticker and SPX:
  - ticker_gamma: netGammaSign (positive=dealer long gamma, dampens moves; negative=dealer short gamma, amplifies moves), magnitudeBucket (low/medium/high), callPutImbalance, topGammaStrikes, putWallStrike, callWallStrike, tailIgnition (up/down risk scores 0-100 with labels)
  - spx_gamma: same structure for SPX — provides the macro gamma backdrop

Write a trade ticket in this exact JSON structure:

{
  "verdict": "CONTINUE or FADE or PASS",
  "conviction": "HIGH or MEDIUM or LOW",
  "one_liner": "One sentence: the core thesis for this scenario in plain desk language.",
  "entry_plan": {
    "trigger": "Exact condition that activates this trade. Reference dollar levels from thresholds.",
    "instrument": "Specific instrument recommendation: shares, debit spread with strikes, or skip. Be concrete.",
    "timing": "When to enter relative to the open — first 30 min, wait for structure confirmation, etc.",
    "size": "Position sizing guidance as % of book or risk units. Scale to conviction."
  },
  "exit_plan": {
    "profit_target": "Where to take profit — % of gap, dollar level, or % of max spread value.",
    "stop_loss": "Hard stop condition — price level or % retracement that invalidates the thesis.",
    "time_stop": "When to close if thesis hasn't played out. Reference optimal_hold_days.",
    "hold_period": "Recommended hold in days. Reference the horizon with highest edge."
  },
  "risk_notes": "Breach rate context, tail risk, what the realized/implied ratio tells us about this name's tendency to surprise. 2-3 sentences.",
  "historical_anchor": "Cite the matched events — how many, what happened, what the average drift was. Be specific with dates and numbers. 2-3 sentences.",
  "what_if_wrong": "If this scenario plays out opposite to the action — what does the trader do? Flip, stop out, or wait? 2-3 sentences.",
  "gamma_read": "Interpret the dealer_flow data: Is the ticker in positive or negative gamma? How does that affect post-earnings drift (negative gamma amplifies, positive dampens)? What does SPX gamma tell us about the macro backdrop? Reference put/call walls, tail ignition scores, and top gamma strikes. If no dealer_flow data, say 'No gamma context available.' 2-3 sentences.",
  "desk_voice": "The senior quant's parting words. Is this a bread-and-butter setup or an edge case? How does it compare to the average earnings trade? Be direct. 2-3 sentences."
}

Rules:
- Write as a senior quant on the desk, not a textbook. Be direct and practical.
- Reference the ACTUAL numbers: continuation rates, drift percentages, event counts, dollar levels, dates from matched events.
- If the action is PASS, the verdict must be PASS. Still fill out the blueprint explaining WHY there is no edge.
- If continuation_rate_5d >= 70%, lean into the CONTINUE thesis hard. Cite the rate and sample size.
- If high_vol_pct >= 60%, note that volume confirms the information content of the gap.
- If hold_pct >= 50%, highlight that gap-and-hold is the strongest PEAD signal.
- For FADE scenarios, look for low volume + high reversion rates. The instrument should be a reversal play.
- For CONTINUE scenarios, the instrument should capture drift in the gap direction.
- If dealer_flow.ticker_gamma is provided: negative gamma (dealer short gamma) amplifies moves — favors continuation plays. Positive gamma dampens moves — fade setups need stronger conviction. Reference the put/call wall strikes as support/resistance levels.
- If dealer_flow.spx_gamma is provided: negative SPX gamma means broader market moves are amplified — increases tail risk on ALL trades. Positive SPX gamma is stabilizing.
- If tailIgnition scores are HIGH (>60), warn about tail risk in that direction.
- Keep each field concise — under 75 words per field.
- Output valid JSON only."""


_E8_ACTIVATION_SYSTEM = """You are a senior quant on a systematic desk issuing a real-time GO / NO-GO activation call for a post-earnings stock trade. This is NOT an options trade — the desk will BUY shares, SHORT shares, or PASS entirely.

Context: The desk ran Engine 8 pre-earnings and built a scenario playbook. Earnings have now reported. The market has been open for ~30 minutes. You are reading live market data at T+30 min and deciding whether the pre-planned trade activates.

You will receive a JSON payload with:
- activation_metrics: real-time data from EODHD (last_price, session_open/high/low, volume, previous close, gap %, structure read, volume read)
- matched_scenario: the pre-planned playbook row that matches the current gap (continuation rates, drift, action, confidence)
- phase_a_context: pre-earnings baseline (breach rates, expected move, stock price, thresholds, strike targets for reference)
- dealer_flow: real-time dealer gamma positioning for ticker and SPX (net gamma sign, walls, tail ignition)
- playbook_quick_ref: the quick-reference bullet points from the playbook

Write an activation note in this exact JSON structure:

{
  "activation": "GO or NO-GO or WAIT",
  "action": "BUY or SHORT or PASS",
  "conviction": "HIGH or MEDIUM or LOW",
  "live_read": {
    "gap": "One line: gap % vs EM, direction, magnitude bucket. Use actual numbers.",
    "structure": "One line: is the gap holding, fading, or stalling? Reference session_open vs last_price vs high/low. Use actual prices.",
    "volume": "One line: session volume vs average, what it means for information content.",
    "iv_crush": "One line: IV crush magnitude if available, what it means for premium sellers.",
    "gamma": "One line: dealer gamma read — is hedging flow amplifying or dampening? Reference walls."
  },
  "trade_ticket": {
    "action": "BUY [N] shares at $X.XX or SHORT [N] shares at $X.XX — be specific with the current price.",
    "stop_loss": "Hard stop price level and the logic behind it (EM threshold, session low, etc.).",
    "profit_target": "Target price or % and hold period. Reference historical drift from matched scenario.",
    "position_size": "Risk units or % of book. Scale to conviction and stop distance."
  },
  "desk_note": "3-4 sentences maximum. Senior quant voice. Be direct about why this is a GO or NO-GO. Reference the specific data — gap holding at X%, volume is Y% of daily, Z/N historical events continued. If PASS, say why clearly."
}

Rules:
- This is a STOCK trade only. BUY shares or SHORT shares. No options, no spreads, no iron condors.
- BUY when: gap UP + HOLD structure + volume confirms + historical continuation supports it.
- SHORT when: gap DOWN + HOLD structure + volume confirms + historical continuation supports it (follow the gap direction, not fade it).
- PASS when: structure is FADE or STALL, volume is LOW, historical edge is weak, or conviction is too low.
- For FADE structure: default to PASS unless historical reversion rate is very high (>70%) AND volume confirms. Even then, conviction should be LOW.
- WAIT when: structure is ambiguous (STALL) but metrics lean toward a trade — suggest checking again in 15-30 min.
- Reference ACTUAL numbers from activation_metrics. Don't make up prices or percentages.
- Keep each field concise. The desk_note is the most important field — make it count.
- If dealer gamma is negative (amplifies), that SUPPORTS continuation trades. If positive (dampens), note it as headwind.
- Output valid JSON only."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_dealer_gamma_summary(orats, ticker: str) -> dict | None:
    """Best-effort fetch of dealer gamma context for a single ticker."""
    try:
        from backend.dealer_gamma_context import compute_dealer_gamma_context
        from backend.oi_clusters import compute_open_interest_clusters
        from backend.engine2_gamma_addons import compute_tail_ignition

        resp = orats.live_strikes(
            ticker=ticker,
            fields="strike,gamma,callOpenInterest,putOpenInterest,spotPrice,stockPrice,callVolume,putVolume",
        )
        rows = resp.rows if resp and getattr(resp, "rows", None) else []
        if not rows:
            return None

        dg = compute_dealer_gamma_context(rows, contract_multiplier=100, band_pct=0.05, top_n=5)
        spot = dg.get("spot")

        put_wall_strike = None
        call_wall_strike = None
        try:
            oi = compute_open_interest_clusters(rows, band_pct=0.10, top_n=5, cluster_steps=2)
            pw = oi.get("putWall")
            cw = oi.get("callWall")
            if pw:
                put_wall_strike = pw.get("peakStrike")
            if cw:
                call_wall_strike = cw.get("peakStrike")
        except Exception:
            pass

        ti = None
        try:
            ti = compute_tail_ignition(
                rows,
                spot=float(spot) if spot else None,
                put_wall_strike=put_wall_strike,
                call_wall_strike=call_wall_strike,
                contract_multiplier=100,
            )
        except Exception:
            pass

        summary = {
            "ticker": ticker,
            "spot": spot,
            "netGex": dg.get("netGex"),
            "netGammaSign": dg.get("netGammaSign"),
            "magnitudeBucket": dg.get("magnitudeBucket"),
            "callPutImbalance": dg.get("callPutImbalance"),
            "topGammaStrikes": dg.get("topGammaStrikes", [])[:3],
            "putWallStrike": put_wall_strike,
            "callWallStrike": call_wall_strike,
        }
        if ti and ti.get("enabled"):
            summary["tailIgnition"] = {
                "down": {"score": ti["down"]["score"], "label": ti["down"]["label"]},
                "up": {"score": ti["up"]["score"], "label": ti["up"]["label"]},
                "gammaFlipStrike": ti.get("gammaFlipStrike"),
            }
        return summary
    except Exception as exc:
        LOG.debug("Dealer gamma fetch failed for %s: %s", ticker, exc)
        return None


def _compute_activation_metrics(
    live_quote: dict,
    phase_a: dict,
    live_options_rows: list[dict] | None = None,
) -> dict:
    """Compute activation metrics from EODHD live quote + Phase A baseline.

    live_quote: single row from EODHD get_live_quote() or get_us_quote_delayed()
    phase_a:    the cached Phase A engine8/evaluate response
    live_options_rows: raw ORATS live_strikes rows (optional, for IV crush)
    """
    e1 = phase_a.get("engine1", {})
    cur = e1.get("current", {})
    em = e1.get("expectedMove", {})

    prev_close = (
        live_quote.get("previousClosePrice")
        or live_quote.get("previousClose")
        or live_quote.get("close")
        or cur.get("stockPrice")
    )
    last_price = (
        live_quote.get("lastTradePrice")
        or live_quote.get("close")
        or 0
    )
    session_open = live_quote.get("open") or last_price
    session_high = live_quote.get("high") or last_price
    session_low = live_quote.get("low") or last_price
    session_volume = live_quote.get("volume") or 0
    avg_volume = live_quote.get("averageVolume") or 0

    prev_close = float(prev_close) if prev_close else 0
    last_price = float(last_price)
    session_open = float(session_open)
    session_high = float(session_high)
    session_low = float(session_low)
    session_volume = float(session_volume)
    avg_volume = float(avg_volume)

    em_pct = float(
        cur.get("impliedMovePct")
        or cur.get("delayedImpliedMovePct")
        or em.get("expectedMovePct")
        or 0
    )

    live_gap_pct = ((session_open - prev_close) / prev_close * 100) if prev_close else 0
    gap_direction = "UP" if live_gap_pct > 0 else "DOWN"
    gap_vs_em = (abs(live_gap_pct) / em_pct) if em_pct else 0

    if abs(live_gap_pct) < 0.05:
        magnitude_bucket = "flat"
    elif gap_vs_em < 1.0:
        magnitude_bucket = "contained"
    elif gap_vs_em < 1.5:
        magnitude_bucket = "extended"
    else:
        magnitude_bucket = "extreme"

    gap_size = session_open - prev_close
    retracement_pct = 0.0
    if abs(gap_size) > 0.001:
        if gap_direction == "UP":
            retracement_pct = (session_open - last_price) / gap_size
        else:
            retracement_pct = (last_price - session_open) / abs(gap_size)
    retracement_pct = max(0.0, min(retracement_pct, 2.0))

    if retracement_pct < 0.30:
        structure_read = "HOLD"
    elif retracement_pct > 0.50:
        structure_read = "FADE"
    else:
        structure_read = "STALL"

    if avg_volume > 0:
        vol_ratio = session_volume / avg_volume
        if vol_ratio > 0.50:
            volume_read = "HIGH"
        elif vol_ratio < 0.20:
            volume_read = "LOW"
        else:
            volume_read = "NORMAL"
    else:
        vol_ratio = 0
        volume_read = "UNKNOWN"

    # IV crush from live options (best-effort)
    iv_crush_pct = None
    pre_iv = cur.get("impErnMv") or cur.get("impliedMovePct")
    if live_options_rows and pre_iv:
        spot = last_price
        atm_rows = sorted(
            [r for r in live_options_rows if r.get("strike") and r.get("callMidIv")],
            key=lambda r: abs(float(r.get("strike", 0)) - spot),
        )
        if atm_rows:
            live_atm_iv = float(atm_rows[0].get("callMidIv") or atm_rows[0].get("putMidIv") or 0)
            if live_atm_iv > 0 and float(pre_iv) > 0:
                iv_crush_pct = round((live_atm_iv - float(pre_iv)) / float(pre_iv) * 100, 1)

    # Options flow proxy (near-ATM put/call volume)
    options_flow = None
    if live_options_rows:
        near_atm = [
            r for r in live_options_rows
            if r.get("strike") and abs(float(r.get("strike", 0)) - last_price) / max(last_price, 1) < 0.05
        ]
        total_call_vol = sum(float(r.get("callVolume", 0)) for r in near_atm)
        total_put_vol = sum(float(r.get("putVolume", 0)) for r in near_atm)
        pc_ratio = (total_put_vol / total_call_vol) if total_call_vol > 0 else None
        options_flow = {
            "nearAtmCallVolume": int(total_call_vol),
            "nearAtmPutVolume": int(total_put_vol),
            "putCallRatio": round(pc_ratio, 2) if pc_ratio is not None else None,
        }

    return {
        "last_price": round(last_price, 2),
        "prev_close": round(prev_close, 2),
        "session_open": round(session_open, 2),
        "session_high": round(session_high, 2),
        "session_low": round(session_low, 2),
        "session_volume": int(session_volume),
        "avg_volume": int(avg_volume),
        "volume_ratio": round(vol_ratio, 2),
        "volume_read": volume_read,
        "live_gap_pct": round(live_gap_pct, 2),
        "gap_direction": gap_direction,
        "gap_vs_em": round(gap_vs_em, 2),
        "magnitude_bucket": magnitude_bucket,
        "em_pct": round(em_pct, 2),
        "retracement_pct": round(retracement_pct * 100, 1),
        "structure_read": structure_read,
        "iv_crush_pct": iv_crush_pct,
        "options_flow": options_flow,
    }


def _match_playbook_scenario(metrics: dict, scenarios: list[dict]) -> dict | None:
    """Find the playbook scenario row that best matches the live gap."""
    if not scenarios:
        return None
    direction = metrics["gap_direction"]
    bucket = metrics["magnitude_bucket"]

    # Try exact match first (magnitude + direction + HOLD/FADE based on structure)
    structure = metrics["structure_read"]
    for s in scenarios:
        s_mag = (s.get("magnitude") or "").lower()
        s_dir = (s.get("direction") or "").upper()
        s_struct = (s.get("structure") or "").upper()
        if s_mag == bucket and s_dir == direction and s_struct == structure:
            return s

    # Relax structure constraint
    for s in scenarios:
        s_mag = (s.get("magnitude") or "").lower()
        s_dir = (s.get("direction") or "").upper()
        if s_mag == bucket and s_dir == direction:
            return s

    # Relax direction -- just match magnitude
    for s in scenarios:
        s_mag = (s.get("magnitude") or "").lower()
        if s_mag == bucket:
            return s

    return scenarios[0] if scenarios else None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/api/engine8/evaluate")
async def engine8_evaluate(
    ticker: str = Query(..., description="US equity ticker"),
    earnings_date: str = Query(..., description="Earnings date (YYYY-MM-DD)"),
    timing: str = Query(..., description="BMO or AMC"),
):
    """Engine 8 lifecycle evaluation.

    All three parameters are required — the desk provides them:
      - ticker: what to evaluate
      - earnings_date: when earnings are/were
      - timing: BMO (before market open) or AMC (after market close)

    Phase detection is deterministic from earnings_date vs today:
      Phase A (pre-earnings): earnings_date >= today (AMC same-day = pre)
      Phase B (post-earnings): earnings_date < today (BMO same-day = post)
    """
    import asyncio

    flags = get_flags()
    if not flags.ENABLE_ENGINE8_POST_EVENT:
        raise HTTPException(status_code=404, detail="Engine 8 is not enabled")

    ticker = ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")

    timing = timing.strip().upper()
    if timing not in ("BMO", "AMC"):
        raise HTTPException(status_code=400, detail="timing must be BMO or AMC")

    try:
        ed = dt.date.fromisoformat(earnings_date[:10])
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Invalid earnings_date format (YYYY-MM-DD)")

    orats = get_client_optional()
    if orats is None:
        raise HTTPException(status_code=503, detail="ORATS client unavailable")

    store = get_store_optional()
    today = dt.date.today()

    try:
        from backend.price_service import get_price_service
        price_svc = get_price_service()
    except Exception:
        price_svc = None

    bz = get_benzinga_client_optional()

    # -- Phase detection (deterministic) ---------------------------------------
    is_pre_earnings = ed > today
    if ed == today and timing == "AMC":
        is_pre_earnings = True

    # =========================================================================
    # PHASE A: Pre-Earnings — run Engine 1, persist, return IC analysis
    # =========================================================================
    if is_pre_earnings:
        try:
            from backend.engine8_e1_bridge import run_engine1_for_phase_a

            e1_result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: run_engine1_for_phase_a(
                    ticker=ticker,
                    orats_client=orats,
                    store=store,
                    earnings_date=ed,
                    today=today,
                    benzinga_client=bz,
                    price_svc=price_svc,
                ),
            )

            summary = e1_result.get("summary", {})
            trade_builder = e1_result.get("tradeBuilder")
            go_no_go = e1_result.get("goNoGo", {})
            regime = e1_result.get("regime", {})
            current = e1_result.get("current", {})
            expected_move = e1_result.get("expectedMove", {})
            strike_targets = e1_result.get("strikeTargets")
            baseline = e1_result.get("baseline", {})
            playbook = e1_result.get("playbook")
            hold_risk = e1_result.get("earningsHoldRisk", {})

            return {
                "phase": "pre_earnings",
                "ticker": ticker,
                "earnings_date": ed.isoformat(),
                "timing": timing,
                "countdown_days": (ed - today).days,
                "stock_price": current.get("stockPrice"),
                "engine1": {
                    "summary": {
                        "breach_rate_pct": summary.get("breach_rate_pct"),
                        "events_used": summary.get("events_used"),
                        "events_found": summary.get("events_found"),
                        "upBreachRatePct": summary.get("upBreachRatePct"),
                        "downBreachRatePct": summary.get("downBreachRatePct"),
                        "avgUpOvershootPct": summary.get("avgUpOvershootPct"),
                        "avgDownOvershootPct": summary.get("avgDownOvershootPct"),
                        "avg_above_breach_pct": summary.get("avg_above_breach_pct"),
                        "tailBias": summary.get("tailBias"),
                        "avg_implied_all_pct": summary.get("avg_implied_all_pct"),
                    },
                    "baseline": {
                        "avg_ratio_realized_to_implied": baseline.get("avg_ratio_realized_to_implied"),
                    },
                    "current": {
                        "stockPrice": current.get("stockPrice"),
                        "asOfDate": current.get("asOfDate"),
                        "source": current.get("source"),
                        "impliedMovePct": current.get("impliedMovePct"),
                        "impErnMv": current.get("impErnMv"),
                        "delayedImpliedMovePct": current.get("delayedImpliedMovePct"),
                        "delayedUpdatedAt": current.get("delayedUpdatedAt"),
                        "delayedTradeDate": current.get("delayedTradeDate"),
                    },
                    "expectedMove": {
                        "expectedMovePct": (expected_move or {}).get("expectedMovePct"),
                        "expectedMoveDollars": (expected_move or {}).get("expectedMoveDollars"),
                        "expiry": (expected_move or {}).get("expiry"),
                        "source": (expected_move or {}).get("source"),
                    },
                    "strikeTargets": strike_targets,
                    "tradeBuilder": trade_builder,
                    "goNoGo": go_no_go,
                    "regime": {
                        "label": regime.get("label"),
                        "guidance": regime.get("guidance"),
                    },
                    "holdRisk": {
                        "breach_1_5x": (hold_risk.get("unconditional", {}).get("earnings_close", {}).get("1.5")),
                        "breach_2_0x": (hold_risk.get("unconditional", {}).get("earnings_close", {}).get("2.0")),
                    },
                    "gapVsCtc": e1_result.get("gapVsCtc"),
                },
                "playbook": playbook,
                "decision": None,
            }
        except Exception as e:
            LOG.exception("Engine 8 Phase A failed for %s", ticker)
            raise HTTPException(status_code=500, detail=f"Engine 8 Phase A error: {e}") from e

    # =========================================================================
    # PHASE B: Post-Earnings — load Engine 1, run extension pipeline
    # =========================================================================
    try:
        from backend.engine8_e1_bridge import load_engine1_for_phase_b, derive_trade_outcome_from_e1
        from backend.engine8_pipeline import evaluate_ticker

        engine1_trade = None
        e1_persisted = None
        if store is not None:
            e1_persisted = await asyncio.get_event_loop().run_in_executor(
                None, lambda: load_engine1_for_phase_b(
                    ticker=ticker, earnings_date=ed.isoformat(), store=store,
                ),
            )
            if e1_persisted:
                engine1_trade = e1_persisted

        # Fall back to in-memory breach cache
        if engine1_trade is None:
            try:
                key = breach_cache_key(ticker, 20, 5, 1.0, flags.cache_fingerprint())
                with breach_cache_lock:
                    cached_breach = breach_cache.get(key)
                if cached_breach and isinstance(cached_breach, dict):
                    engine1_trade = cached_breach
            except Exception:
                pass

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: evaluate_ticker(
                ticker=ticker,
                engine1_trade=engine1_trade,
                earnings_date=ed,
                earnings_timing=timing,
                orats_client=orats,
                price_svc=price_svc,
                store=store,
                flags=flags,
            ),
        )

        result["phase"] = "post_earnings"
        result["timing"] = timing

        # Attach Engine 1 summary for the IC outcome card
        if e1_persisted:
            tb = e1_persisted.get("tradeBuilder")
            current_price_val = None
            if price_svc:
                try:
                    bars = price_svc.fetch_daily_bars(ticker, today - dt.timedelta(days=5), today)
                    if bars:
                        bars.sort(key=lambda b: b.date, reverse=True)
                        current_price_val = bars[0].close
                except Exception:
                    pass

            trade_outcome = derive_trade_outcome_from_e1(e1_persisted, current_price_val, flags.ENGINE8_MAX_CONTROLLED_LOSS_PCT)
            result["engine1_summary"] = {
                "had_phase_a": True,
                "trade_outcome": trade_outcome,
                "tradeBuilder": tb,
                "breach_rate_pct": (e1_persisted.get("summary") or {}).get("breach_rate_pct"),
                "expected_move_pct": (e1_persisted.get("current") or {}).get("impliedMovePct"),
            }
        else:
            result["engine1_summary"] = {
                "had_phase_a": False,
                "trade_outcome": "unknown",
                "message": "No pre-earnings setup found. Run Engine 8 before earnings to set up the lifecycle.",
            }

        return result
    except Exception as e:
        LOG.exception("Engine 8 Phase B failed for %s", ticker)
        raise HTTPException(status_code=500, detail=f"Engine 8 error: {e}") from e


@router.get("/api/engine8/history")
async def engine8_history(
    ticker: str = Query(..., description="US equity ticker"),
    n: int = Query(40, ge=1, le=100, description="Number of historical events"),
):
    """Return historical pattern analysis for a ticker (debugging/transparency)."""
    import asyncio

    flags = get_flags()
    if not flags.ENABLE_ENGINE8_POST_EVENT:
        raise HTTPException(status_code=404, detail="Engine 8 is not enabled")

    ticker = ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")

    orats = get_client_optional()
    if orats is None:
        raise HTTPException(status_code=503, detail="ORATS client unavailable")

    try:
        from backend.price_service import get_price_service
        price_svc = get_price_service()
    except Exception:
        price_svc = None

    try:
        from backend.engine8_pipeline import _build_all_event_rows
        from backend.config import FeatureFlags
        effective_flags = replace(flags, ENGINE8_LOOKBACK_EVENTS=n)

        loop = asyncio.get_event_loop()
        today = dt.date.today()

        event_rows = await loop.run_in_executor(
            None,
            lambda: _build_all_event_rows(
                ticker=ticker,
                current_earnings_date=today,
                orats_client=orats,
                price_svc=price_svc,
                flags=effective_flags,
            ),
        )
        return {
            "ticker": ticker,
            "event_count": len(event_rows),
            "events": event_rows,
        }
    except Exception as e:
        LOG.exception("Engine 8 history failed for %s", ticker)
        raise HTTPException(status_code=500, detail=f"Engine 8 history error: {e}") from e


@router.post("/api/engine8/desk-notes")
def engine8_desk_notes(body: dict):
    """Engine 8: Generate GPT-5.5 senior quant desk notes for the earnings playbook."""
    payload_data = body.get("payload")
    if not payload_data:
        raise HTTPException(status_code=400, detail="Missing 'payload' in request body")

    try:
        from backend.llm_client import _get_openai_client, _parse_desk_brief_json

        client = _get_openai_client()
        if client is None:
            raise HTTPException(status_code=503, detail="OpenAI client unavailable — set OPENAI_API_KEY")

        import json as _json
        payload_str = _json.dumps(payload_data, default=str)
        if len(payload_str) > 12000:
            payload_str = payload_str[:12000]

        resp = client.chat.completions.create(
            model="gpt-5.5",
            messages=[
                {"role": "system", "content": _E8_DESK_NOTES_SYSTEM},
                {"role": "user", "content": payload_str},
            ],
            temperature=1,
            max_completion_tokens=1800,
            timeout=45,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or ""
        parsed = _parse_desk_brief_json(content)
        if parsed is None:
            raise HTTPException(status_code=502, detail="LLM returned unparseable response")

        parsed["_source"] = "gpt-5.5"
        parsed["_ticker"] = payload_data.get("ticker", "")
        return parsed

    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Engine 8 desk-notes LLM failed")
        raise HTTPException(status_code=500, detail=f"LLM error: {e}") from e


@router.post("/api/engine8/row-playbook")
def engine8_row_playbook(body: dict):
    """Engine 8: Generate GPT-5.5 trade ticket for a single scenario row."""
    scenario = body.get("scenario")
    context = body.get("context", {})
    if not scenario:
        raise HTTPException(status_code=400, detail="Missing 'scenario' in request body")

    try:
        from backend.llm_client import _get_openai_client, _parse_desk_brief_json

        llm_client = _get_openai_client()
        if llm_client is None:
            raise HTTPException(status_code=503, detail="OpenAI client unavailable — set OPENAI_API_KEY")

        ticker = (context.get("ticker") or "").upper()

        # Best-effort: enrich with dealer gamma for ticker + SPX
        gamma_context: dict = {}
        orats = get_client_optional()
        if orats and ticker:
            ticker_gamma = _fetch_dealer_gamma_summary(orats, ticker)
            if ticker_gamma:
                gamma_context["ticker_gamma"] = ticker_gamma
            spx_gamma = _fetch_dealer_gamma_summary(orats, "SPX")
            if spx_gamma:
                gamma_context["spx_gamma"] = spx_gamma

        import json as _json
        payload = {
            "scenario": scenario,
            "matched_events": scenario.get("matched_events", []),
            "context": {
                "ticker": ticker,
                "stock_price": context.get("stock_price"),
                "em_pct": context.get("em_pct"),
                "breach_stats": context.get("breach_stats", {}),
                "thresholds": context.get("thresholds", {}),
                "strike_targets": context.get("strike_targets", {}),
                "dealer_flow": gamma_context if gamma_context else None,
            },
        }
        payload_str = _json.dumps(payload, default=str)
        if len(payload_str) > 16000:
            payload_str = payload_str[:16000]

        resp = llm_client.chat.completions.create(
            model="gpt-5.5",
            messages=[
                {"role": "system", "content": _E8_ROW_PLAYBOOK_SYSTEM},
                {"role": "user", "content": payload_str},
            ],
            temperature=1,
            max_completion_tokens=2000,
            timeout=60,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or ""
        parsed = _parse_desk_brief_json(content)
        if parsed is None:
            raise HTTPException(status_code=502, detail="LLM returned unparseable response")

        parsed["_source"] = "gpt-5.5"
        parsed["_scenario_key"] = scenario.get("key", "")
        return parsed

    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Engine 8 row-playbook LLM failed")
        raise HTTPException(status_code=500, detail=f"LLM error: {e}") from e


@router.post("/api/engine8/activation-scan")
def engine8_activation_scan(body: dict):
    """Engine 8.5: Real-time post-open activation scanner.

    Request body: {
      "ticker": "AAPL",
      "earnings_date": "2026-02-20",
      "timing": "AMC",
      "phase_a": { ... cached Phase A response from engine8/evaluate ... }
    }
    """
    ticker = (body.get("ticker") or "").strip().upper()
    phase_a = body.get("phase_a") or {}
    if not ticker:
        raise HTTPException(status_code=400, detail="Missing 'ticker'")
    if not phase_a.get("engine1"):
        raise HTTPException(status_code=400, detail="Missing 'phase_a' with engine1 data — run Engine 8 pre-earnings first")

    try:
        from backend.llm_client import _get_openai_client, _parse_desk_brief_json

        llm_client = _get_openai_client()
        if llm_client is None:
            raise HTTPException(status_code=503, detail="OpenAI client unavailable — set OPENAI_API_KEY")

        # 1. Fetch live stock quote from EODHD
        live_quote: dict = {}
        try:
            from backend.eodhd_client import EodhdClient
            eodhd = EodhdClient.from_env()
            eodhd_symbol = f"{ticker}.US"
            us_resp = eodhd.get_us_quote_delayed(eodhd_symbol)
            if us_resp.rows:
                live_quote = us_resp.rows[0]
            else:
                simple_resp = eodhd.get_live_quote(eodhd_symbol)
                if simple_resp.rows:
                    live_quote = simple_resp.rows[0]
        except Exception as eq_err:
            LOG.warning("EODHD live quote failed for %s: %s", ticker, eq_err)

        if not live_quote.get("lastTradePrice") and not live_quote.get("close"):
            raise HTTPException(
                status_code=502,
                detail=f"Could not fetch live quote for {ticker} — market may be closed or EODHD unavailable",
            )

        # 2. Fetch live options chain from ORATS (for IV crush + flow)
        live_options_rows: list[dict] = []
        orats = get_client_optional()
        if orats:
            try:
                resp = orats.live_strikes(
                    ticker=ticker,
                    fields="strike,callMidIv,putMidIv,callVolume,putVolume,callOpenInterest,putOpenInterest,gamma,spotPrice,stockPrice",
                )
                live_options_rows = resp.rows if resp and getattr(resp, "rows", None) else []
            except Exception as orats_err:
                LOG.warning("ORATS live_strikes failed for %s: %s", ticker, orats_err)

        # 3. Compute activation metrics
        metrics = _compute_activation_metrics(live_quote, phase_a, live_options_rows or None)

        # 4. Match playbook scenario
        pb = phase_a.get("playbook", {})
        scenarios = pb.get("scenarios", [])
        matched = _match_playbook_scenario(metrics, scenarios)

        # 5. Fetch dealer gamma (reuse existing helper)
        gamma_context: dict = {}
        if orats:
            ticker_gamma = _fetch_dealer_gamma_summary(orats, ticker)
            if ticker_gamma:
                gamma_context["ticker_gamma"] = ticker_gamma
            spx_gamma = _fetch_dealer_gamma_summary(orats, "SPX")
            if spx_gamma:
                gamma_context["spx_gamma"] = spx_gamma

        # 6. Build LLM payload
        e1 = phase_a.get("engine1", {})
        import json as _json
        payload = {
            "activation_metrics": metrics,
            "matched_scenario": matched,
            "phase_a_context": {
                "ticker": ticker,
                "em_pct": metrics["em_pct"],
                "pre_stock_price": metrics["prev_close"],
                "breach_stats": e1.get("summary", {}),
                "thresholds": pb.get("thresholds", {}),
                "hold_risk": e1.get("holdRisk", {}),
            },
            "dealer_flow": gamma_context if gamma_context else None,
            "playbook_quick_ref": pb.get("quick_reference", []),
        }

        payload_str = _json.dumps(payload, default=str)
        if len(payload_str) > 20000:
            payload_str = payload_str[:20000]

        # 7. Call GPT-5.5
        resp = llm_client.chat.completions.create(
            model="gpt-5.5",
            messages=[
                {"role": "system", "content": _E8_ACTIVATION_SYSTEM},
                {"role": "user", "content": payload_str},
            ],
            temperature=1,
            max_completion_tokens=1500,
            timeout=60,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or ""
        parsed = _parse_desk_brief_json(content)
        if parsed is None:
            raise HTTPException(status_code=502, detail="LLM returned unparseable response")

        parsed["_source"] = "gpt-5.5"
        parsed["_metrics"] = metrics
        parsed["_matched_scenario_key"] = (matched or {}).get("key", "")
        return parsed

    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Engine 8.5 activation-scan failed for %s", ticker)
        raise HTTPException(status_code=500, detail=f"Activation scan error: {e}") from e
