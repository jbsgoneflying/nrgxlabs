"""Engine 1 — Breach & Compare routes + Earnings IC Advisor / Trade Journal."""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request

from backend.breach_ranker import rank_tickers, summarize_tiers
from backend.config import get_flags
from backend.deps import (
    LOG,
    breach_cache,
    breach_cache_key,
    breach_cache_lock,
    engine1_elig_cache,
    engine1_elig_cache_lock,
    get_benzinga_client_optional,
    get_client,
    get_client_optional,
)
from backend.earnings_gamma_context import compute_earnings_gamma_context
from backend.earnings_logic import BreachInputError, compute_breach_stats, compute_current_snapshot
from backend.go_no_go import compute_go_no_go
from backend.orats_client import OratsError

router = APIRouter()


@router.get("/api/breach")
def breach(
    ticker: str = Query(..., description="US equity ticker"),
    n: int = Query(20, ge=1, le=50),
    years: int = Query(5, ge=1, le=10),
    k: float = Query(1.0, gt=0.0),
    mode: str | None = Query(None, description="trade builder: auto|equal_delta|equal_premium"),
    symmetry: str | None = Query(None, description="trade builder: auto|symmetric|manual"),
    target_delta: float | None = Query(None, gt=0.0, lt=1.0),
    target_premium: float | None = Query(None, gt=0.0),
    wing_width: float | None = Query(None, gt=0.0),
    dte_target: int | None = Query(None, ge=1, le=60),
    exp: str | None = Query(None, description="trade builder expiration (YYYY-MM-DD)"),
    mc: bool | None = Query(None, description="enable Monte Carlo earnings gap risk outputs (additive)"),
    mc_opt: bool | None = Query(None, description="enable Monte Carlo wing optimization (risk-only)"),
    mc_stability: bool | None = Query(None, description="enable bootstrap stability + asymmetry caps (additive)"),
    mc_cond_quarter: bool | None = Query(None, description="MC conditioning: quarter"),
    mc_cond_regime: bool | None = Query(None, description="MC conditioning: regime"),
    mc_event_date: str | None = Query(None, description="manual next earnings date override (YYYY-MM-DD)"),
    mc_event_timing: str | None = Query(None, description="manual next earnings timing override (AMC|BMO)"),
):
    try:
        trade_builder_inputs = {
            "mode": mode,
            "symmetry": symmetry,
            "target_delta": target_delta,
            "target_premium": target_premium,
            "wing_width": wing_width,
            "dte_target": dte_target,
            "exp": exp,
        }
        has_trade_builder = any(v is not None for v in trade_builder_inputs.values())

        base_flags = get_flags()
        overrides = {}
        if mc is not None:
            overrides["ENABLE_MONTE_CARLO_EARNINGS"] = bool(mc)
        if mc_opt is not None:
            overrides["MC_ENABLE_WING_OPTIMIZATION"] = bool(mc_opt)
        if mc_stability is not None:
            overrides["MC_ENABLE_TAS_STABILITY"] = bool(mc_stability)
        if mc_cond_quarter is not None:
            overrides["MC_ENABLE_CONDITION_ON_QUARTER"] = bool(mc_cond_quarter)
        if mc_cond_regime is not None:
            overrides["MC_ENABLE_CONDITION_ON_REGIME"] = bool(mc_cond_regime)

        effective_flags = replace(base_flags, **overrides) if overrides else base_flags
        enable_mc = bool(effective_flags.ENABLE_MONTE_CARLO_EARNINGS)

        # MC depends on near-term anchoring (nextEvent/current snapshot); avoid mixing stale cached payloads.
        if enable_mc:
            has_trade_builder = True

        key = breach_cache_key(ticker, n, years, k, effective_flags.cache_fingerprint())
        if not has_trade_builder:
            with breach_cache_lock:
                cached = breach_cache.get(key)
            if cached is not None:
                # Refresh "current" snapshot even when the heavy payload is cached.
                # This prevents stale assumed-price/EM issues in the Trade Builder UI.
                try:
                    fresh = dict(cached)
                    client0 = get_client()
                    fresh["current"] = compute_current_snapshot(client=client0, ticker=ticker.strip().upper())
                    try:
                        bz_for_go = get_benzinga_client_optional() if bool(get_flags().ENABLE_BENZINGA) else None
                        fresh["goNoGo"] = compute_go_no_go(client0, ticker=ticker.strip().upper(), payload=fresh, benzinga_client=bz_for_go)
                    except Exception:
                        pass
                    return fresh
                except Exception:
                    return cached

        client = get_client()
        payload = compute_breach_stats(
            client=client,
            ticker=ticker,
            n=n,
            years=years,
            k=k,
            trade_builder_inputs=(trade_builder_inputs if has_trade_builder else None),
            flags_override=effective_flags,
            next_event_override={"date": mc_event_date, "timing": mc_event_timing},
            benzinga_client=get_benzinga_client_optional(),
        )

        # Inject Earnings Gamma Context (Raven-Tech 2.0)
        try:
            from backend.dealer_gamma_context import compute_dealer_gamma_context
            from backend.engine2_gamma_addons import compute_tail_ignition
            t_upper = ticker.strip().upper()
            rows = client.live_strikes(ticker=t_upper, fields="strike,gamma,callOpenInterest,putOpenInterest,spotPrice").rows or []
            if rows:
                dg = compute_dealer_gamma_context(rows)
                ti_data = compute_tail_ignition(client, t_upper)
                spot = None
                for r in rows:
                    if isinstance(r, dict) and r.get("spotPrice"):
                        spot = float(r["spotPrice"])
                        break
                current = payload.get("current") or {}
                im_pct = current.get("impliedMovePct")
                egc = compute_earnings_gamma_context(
                    ticker=t_upper,
                    as_of_date=dt.date.today().isoformat(),
                    dealer_gamma=dg,
                    tail_ignition=ti_data,
                    spot=spot,
                    implied_move_pct=im_pct,
                )
                payload["earningsGammaContext"] = egc.to_dict()
        except Exception as egc_err:
            LOG.debug(f"Earnings gamma context skipped for {ticker}: {egc_err}")

        if not has_trade_builder:
            with breach_cache_lock:
                breach_cache[key] = payload
        return payload
    except BreachInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure")
        raise HTTPException(status_code=500, detail="Internal error") from e


@router.get("/api/breach-compare")
def breach_compare(
    tickers: str = Query(..., description="Comma-separated list of tickers (max 10)"),
    k: float = Query(1.0, gt=0.0, description="Breach multiple (1.0, 1.5, 2.0)"),
    n: int = Query(10, ge=1, le=50, description="Number of earnings events to analyze"),
    years: int = Query(3, ge=1, le=10, description="Lookback years"),
):
    """
    Compare and rank multiple tickers for earnings plays.

    Returns ranked list with composite scores based on:
    - Breach rate (25%)
    - IV elevation (20%)
    - EM richness (15%)
    - Liquidity (15%)
    - Tail coverage (10%)
    - Market regime (10%)
    - Event risk (5%)
    """
    try:
        ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
        ticker_list = list(dict.fromkeys(ticker_list))

        if not ticker_list:
            raise HTTPException(status_code=400, detail="No valid tickers provided")

        if len(ticker_list) > 10:
            raise HTTPException(status_code=400, detail="Maximum 10 tickers allowed")

        LOG.info(f"Breach compare: {len(ticker_list)} tickers at k={k}")

        client = get_client()
        benzinga_client = get_benzinga_client_optional()
        base_flags = get_flags()

        payloads: list[tuple[str, dict]] = []
        errors: list[dict] = []

        def fetch_single(ticker: str):
            """Fetch breach stats + goNoGo (for liquidity) for a single ticker."""
            payload = compute_breach_stats(
                client=client,
                ticker=ticker,
                n=n,
                years=years,
                k=k,
                trade_builder_inputs=None,
                flags_override=base_flags,
                benzinga_client=benzinga_client,
            )
            try:
                payload["goNoGo"] = compute_go_no_go(
                    client,
                    ticker=ticker,
                    payload=payload,
                    benzinga_client=benzinga_client,
                )
            except Exception as e:
                LOG.warning(f"goNoGo failed for {ticker}: {e}")
            return ticker, payload

        with ThreadPoolExecutor(max_workers=min(len(ticker_list), 5)) as executor:
            futures = {executor.submit(fetch_single, t): t for t in ticker_list}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    _, payload = future.result(timeout=60)
                    payloads.append((ticker, payload))
                except Exception as e:
                    LOG.warning(f"Failed to fetch {ticker}: {e}")
                    errors.append({"ticker": ticker, "error": str(e)})

        rankings = rank_tickers(payloads)
        tier_summary = summarize_tiers(rankings)

        return {
            "asOfDate": dt.date.today().isoformat(),
            "k": k,
            "n": n,
            "years": years,
            "tickersRequested": len(ticker_list),
            "tickersAnalyzed": len(payloads),
            "summary": tier_summary,
            "rankings": rankings,
            "errors": errors if errors else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (breach-compare)")
        raise HTTPException(status_code=500, detail="Internal error") from e


# ---------------------------------------------------------------------------
# Engine 1 Earnings IC Advisor
# ---------------------------------------------------------------------------

@router.post("/api/breach/advisor")
async def e1_advisor(request: Request):
    """Run the Engine 1 Earnings IC (Vol Crush) LLM Trade Advisor."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    ticker = str(body.get("ticker", "")).strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")

    try:
        f = get_flags()
        client = get_client()
        benzinga_client = get_benzinga_client_optional()

        payload = compute_breach_stats(
            client=client,
            ticker=ticker,
            n=int(body.get("n", 20)),
            years=int(body.get("years", 5)),
            k=1.0,
            flags_override=f,
            benzinga_client=benzinga_client,
        )

        from backend.e1_vrp_engine import (
            compute_vrp_score,
            compute_earnings_width_comparison,
            compute_entry_quality,
            compute_e1_desk_consensus,
            compute_em_preference,
        )
        from backend.e1_earnings_advisor import generate_e1_trade_analysis

        events = payload.get("events") or []
        current = payload.get("current") or {}
        current_em_pct = None
        try:
            current_em_pct = float(current.get("impliedMovePct") or 0) or None
        except Exception:
            pass

        vrp = compute_vrp_score(events, current_implied_move_pct=current_em_pct)

        em_mults = [float(x.strip()) for x in str(f.E1_EM_MULTS).split(",") if x.strip()]
        wing_pts = [float(x.strip()) for x in str(f.E1_WING_WIDTH_PTS).split(",") if x.strip()]
        stock_price = None
        try:
            stock_price = float(current.get("stockPrice") or 0) or None
        except Exception:
            pass

        wc, em_breach = compute_earnings_width_comparison(
            events,
            em_mults=em_mults,
            wing_pts=wing_pts,
            current_implied_move_pct=current_em_pct,
            stock_price=stock_price,
        )

        eq = compute_entry_quality(
            iv_elevation=vrp.get("ivElevation"),
            skew_overlay=payload.get("skewOverlay"),
            regime=payload.get("regime"),
            ticker_dealer_gamma=payload.get("tickerDealerGamma"),
            current=current,
        )

        dc = compute_e1_desk_consensus(
            vrp=vrp,
            entry_quality=eq,
            em_breach_summary=em_breach,
            regime=payload.get("regime"),
            gap_vs_ctc=payload.get("gapVsCtc"),
            event_risk=payload.get("eventRisk"),
        )

        emp = compute_em_preference(em_breach, vrp.get("vrpScore"), eq.get("entryQuality"))

        analysis = generate_e1_trade_analysis(
            breach_payload=payload,
            vrp_analysis=vrp,
            width_analysis=wc,
            entry_quality=eq,
            desk_consensus=dc,
            em_preference=emp,
            flags=f,
        )

        return {
            "advisor": analysis,
            "vrpAnalysis": vrp,
            "widthComparison": wc,
            "emBreachSummary": em_breach,
            "entryQuality": eq,
            "deskConsensus": dc,
            "emPreference": emp,
        }

    except BreachInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OratsError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("E1 advisor failed")
        raise HTTPException(status_code=500, detail="Internal error") from e


# ---------------------------------------------------------------------------
# Engine 1 Earnings IC Trade CRUD
# ---------------------------------------------------------------------------

@router.post("/api/breach/trade")
async def e1_log_trade(request: Request):
    """Log a new Engine 1 earnings IC trade."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if "marketSnapshot" not in body:
        try:
            from backend.trade_memory import capture_market_snapshot
            from backend.deps import get_client_optional
            from backend.redis_store import get_store_optional
            body["marketSnapshot"] = capture_market_snapshot(
                store=get_store_optional(),
                orats_client=get_client_optional(),
                ticker=str(body.get("ticker", "SPY")),
            )
        except Exception:
            pass

    from backend.e1_earnings_trades import log_trade
    trade_id = log_trade(body)
    if trade_id is None:
        raise HTTPException(status_code=500, detail="Failed to persist trade")
    return {"tradeId": trade_id, "status": "active"}


@router.get("/api/breach/trades")
def e1_list_trades():
    """List active Engine 1 earnings IC trades."""
    from backend.e1_earnings_trades import list_active_trades
    return {"trades": list_active_trades()}


@router.post("/api/breach/trade/{trade_id}/checkin")
async def e1_trade_checkin(trade_id: str, request: Request):
    """Post-earnings check-in: capture realized move, gap, and breach status."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    from backend.e1_earnings_trades import get_trade, add_checkin

    trade = get_trade(trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")

    ticker = trade.get("ticker", "")
    entry = trade.get("entry", {})
    predicted_move_pct = float(entry.get("impliedMovePct", 0) or 0)
    pre_earnings_close = float(entry.get("spotAtEntry", 0) or body.get("preEarningsClose", 0) or 0)
    post_earnings_open = float(body.get("postEarningsOpen", 0) or 0)

    actual_move_pct = 0.0
    move_vs_predicted = None
    gap_direction = "flat"
    if pre_earnings_close > 0 and post_earnings_open > 0:
        actual_move_pct = round(abs(post_earnings_open - pre_earnings_close) / pre_earnings_close * 100, 2)
        gap_direction = "up" if post_earnings_open > pre_earnings_close else "down" if post_earnings_open < pre_earnings_close else "flat"
        if predicted_move_pct > 0:
            move_vs_predicted = round(actual_move_pct / predicted_move_pct, 3)

    short_put = float(entry.get("shortPutStrike", 0) or 0)
    short_call = float(entry.get("shortCallStrike", 0) or 0)
    breach_occurred = False
    if post_earnings_open > 0:
        if short_put > 0 and post_earnings_open < short_put:
            breach_occurred = True
        if short_call > 0 and post_earnings_open > short_call:
            breach_occurred = True

    current_vix = None
    try:
        from backend.deps import get_client_optional
        orats = get_client_optional()
        if orats:
            resp = orats.live_summaries(ticker="SPY")
            rows = resp.rows or []
            if rows:
                current_vix = rows[0].get("iv30dMean") or rows[0].get("ivMean")
    except Exception:
        pass

    llm_assessment = None
    try:
        from backend.e1_earnings_advisor import _get_openai_client, _parse_llm_json
        client = _get_openai_client()
        if client and predicted_move_pct > 0:
            prompt = (
                f"Earnings check-in for {ticker}. Predicted EM: {predicted_move_pct:.1f}%, "
                f"Actual move: {actual_move_pct:.1f}% ({gap_direction}), "
                f"Ratio actual/predicted: {move_vs_predicted:.2f}. "
                f"Breach occurred: {breach_occurred}. "
                f"Pre-close: ${pre_earnings_close:.2f}, Post-open: ${post_earnings_open:.2f}. "
                "Provide a 2-3 sentence assessment of this outcome and what it means "
                "for the VRP thesis on this name. Return JSON with keys: assessment, volCrushWorked (bool), lesson."
            )
            resp = client.chat.completions.create(
                model="gpt-5.4",
                messages=[
                    {"role": "system", "content": "You are a vol-crush desk analyst reviewing a post-earnings outcome. Be concise."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2, max_completion_tokens=300, timeout=20,
                response_format={"type": "json_object"},
            )
            llm_assessment = _parse_llm_json(resp.choices[0].message.content.strip())
    except Exception:
        pass

    checkin_data = {
        "type": "post_earnings",
        "postEarningsOpen": post_earnings_open,
        "preEarningsClose": pre_earnings_close,
        "actualMovePct": actual_move_pct,
        "predictedMovePct": predicted_move_pct,
        "moveVsPredicted": move_vs_predicted,
        "gapDirection": gap_direction,
        "breachOccurred": breach_occurred,
        "vixAtCheckin": current_vix,
        "llmAssessment": llm_assessment,
        "userNotes": body.get("notes"),
    }

    success = add_checkin(trade_id, checkin_data)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to persist check-in")

    return {"tradeId": trade_id, "checkin": checkin_data}


@router.post("/api/breach/trade/{trade_id}/post-mortem")
async def e1_trade_post_mortem(trade_id: str):
    """Generate and store an LLM post-mortem for a closed E1 trade."""
    from backend.e1_earnings_trades import get_trade, set_post_mortem, compute_e1_trade_performance_digest

    trade = get_trade(trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")
    if trade.get("status") != "closed":
        raise HTTPException(status_code=400, detail="Trade must be closed for post-mortem")

    journal_ctx = None
    try:
        from backend.e1_earnings_advisor import _build_e1_journal_context
        digest = compute_e1_trade_performance_digest()
        journal_ctx = _build_e1_journal_context(digest)
    except Exception:
        pass

    from backend.e1_earnings_advisor import generate_e1_post_mortem
    pm = generate_e1_post_mortem(trade, journal_context=journal_ctx)
    success = set_post_mortem(trade_id, pm)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to persist post-mortem")

    return {"tradeId": trade_id, "postMortem": pm}


@router.post("/api/breach/trade/{trade_id}/close")
async def e1_close_trade(trade_id: str, request: Request):
    """Close an Engine 1 earnings IC trade with outcome data."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    from backend.e1_earnings_trades import close_trade
    result = close_trade(trade_id, close_data=body)
    if result is None:
        raise HTTPException(status_code=404, detail="Trade not found or close failed")
    return result


@router.get("/api/breach/trades/history")
def e1_trade_history(limit: int = Query(20, ge=1, le=100)):
    """List closed Engine 1 earnings IC trades."""
    from backend.e1_earnings_trades import list_closed_trades
    return {"trades": list_closed_trades(limit=limit)}


@router.get("/api/breach/trades/performance")
def e1_trade_performance():
    """Aggregated cross-ticker performance digest for the learning system."""
    from backend.e1_earnings_trades import compute_e1_trade_performance_digest
    return compute_e1_trade_performance_digest()
