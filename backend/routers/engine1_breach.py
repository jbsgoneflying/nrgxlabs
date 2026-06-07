"""Engine 1 — Breach & Compare routes + Earnings IC Advisor / Trade Journal."""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import TYPE_CHECKING, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from backend.breach_ranker import rank_tickers, summarize_tiers
from backend.config import get_flags
from backend.deps import (
    LOG,
    breach_cache,
    breach_cache_key,
    breach_cache_lock,
    get_benzinga_client_optional,
    get_client,
    get_client_optional,
)
from backend.earnings_gamma_context import compute_earnings_gamma_context
from backend.earnings_logic import BreachInputError, compute_breach_stats, compute_current_snapshot
from backend.go_no_go import compute_go_no_go
from backend.orats_client import OratsError

router = APIRouter()

# Must stay below gunicorn --timeout so pool threads surface real errors instead of BrokenProcessPool noise.
_BREACH_COMPARE_FUTURE_TIMEOUT_S = 210


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
    mc: bool | None = Query(None, description="deprecated: accepted-but-ignored in E1 v2; MC is always on"),
    mc_opt: bool | None = Query(None, description="enable Monte Carlo wing optimization (risk-only)"),
    mc_stability: bool | None = Query(None, description="enable bootstrap stability + asymmetry caps (additive)"),
    mc_cond_quarter: bool | None = Query(None, description="MC conditioning: quarter"),
    mc_cond_regime: bool | None = Query(None, description="MC conditioning: regime"),
    mc_event_date: str | None = Query(None, description="[alias] deprecated: use event_date"),
    mc_event_timing: str | None = Query(None, description="[alias] deprecated: use event_timing"),
    event_date: str | None = Query(None, description="REQUIRED (E1 v2) — next earnings date (YYYY-MM-DD)"),
    event_timing: str | None = Query(None, description="REQUIRED (E1 v2) — next earnings timing (AMC|BMO)"),
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
        has_trade_builder_params = any(v is not None for v in trade_builder_inputs.values())

        # Event date/timing: prefer new top-level params; accept legacy mc_event_* as aliases.
        event_date_eff = (event_date or mc_event_date or "").strip() or None
        event_timing_eff = (event_timing or mc_event_timing or "").strip().upper() or None
        if event_timing_eff and event_timing_eff not in ("AMC", "BMO"):
            raise HTTPException(status_code=400, detail="event_timing must be AMC or BMO")
        if event_date_eff:
            try:
                dt.date.fromisoformat(event_date_eff[:10])
            except ValueError as _e:
                raise HTTPException(status_code=400, detail="event_date must be YYYY-MM-DD") from _e

        base_flags = get_flags()
        if getattr(base_flags, "E1_REQUIRE_EVENT_DATE", False) and bool(getattr(base_flags, "ENABLE_E1_V2", False)):
            if not event_date_eff or not event_timing_eff:
                raise HTTPException(
                    status_code=400,
                    detail="event_date + event_timing (AMC|BMO) are required. Pass ?event_date=YYYY-MM-DD&event_timing=AMC|BMO.",
                )
        overrides = {}
        # v2: mc query param is accepted-but-ignored; MC is always on under
        # ENABLE_MONTE_CARLO_EARNINGS (kill-switch, default True).
        # if mc is not None:  # intentionally no-op in v2
        #     overrides["ENABLE_MONTE_CARLO_EARNINGS"] = bool(mc)
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

        # Full breach cache is unsafe for MC (monteCarlo is tied to snapshot at compute time; we only refresh
        # `current` on cache hits for non-MC). Skip cache whenever MC is on or trade-builder params are set.
        skip_breach_cache = bool(enable_mc or has_trade_builder_params)

        key = breach_cache_key(
            ticker, n, years, k, effective_flags.cache_fingerprint(),
            event_date=event_date_eff, event_timing=event_timing_eff,
        )
        if not skip_breach_cache:
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
            trade_builder_inputs=(trade_builder_inputs if has_trade_builder_params else None),
            flags_override=effective_flags,
            next_event_override={"date": event_date_eff, "timing": event_timing_eff},
            benzinga_client=get_benzinga_client_optional(),
        )

        # Inject Earnings Gamma Context (NRGX Labs 2.0)
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

        if not skip_breach_cache:
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


# ---------------------------------------------------------------------------
# Engine 1 v2 — Wing Decision Console (REMOVED 2026-05-20)
# ---------------------------------------------------------------------------
# The wing-console + score-placement routes were retired alongside the
# Trade Builder + Tracked Trades refactor. The new flow logs candidates
# directly via POST /api/breach/trade with mode="tracked" and reads
# strikes/credit via POST /api/breach/trade/draft-price (below).


@router.get("/api/breach-compare")
def breach_compare(
    tickers: str = Query(..., description="Comma-separated list of tickers (max 10)"),
    k: float = Query(1.0, gt=0.0, description="Breach multiple (1.0, 1.5, 2.0)"),
    n: int = Query(10, ge=1, le=50, description="Number of earnings events to analyze"),
    years: int = Query(3, ge=1, le=10, description="Lookback years"),
    # E1 v2: accepted for parity with /api/breach; batch compare uses
    # per-ticker auto-discovery so these only apply when all tickers
    # share the same earnings event (rare). Not enforced as required.
    event_date: str | None = Query(None, description="Optional shared earnings date (YYYY-MM-DD)"),
    event_timing: str | None = Query(None, description="Optional shared earnings timing (AMC|BMO)"),
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

        event_timing_norm = (event_timing or "").strip().upper() or None
        if event_timing_norm and event_timing_norm not in ("AMC", "BMO"):
            raise HTTPException(status_code=400, detail="event_timing must be AMC or BMO")
        event_date_norm = (event_date or "").strip() or None
        if event_date_norm:
            try:
                dt.date.fromisoformat(event_date_norm[:10])
            except ValueError as _e:
                raise HTTPException(status_code=400, detail="event_date must be YYYY-MM-DD") from _e
        shared_override = (
            {"date": event_date_norm, "timing": event_timing_norm}
            if event_date_norm else None
        )

        payloads: list[tuple[str, dict]] = []
        errors: list[dict] = []

        def fetch_single(ticker: str):
            """Fetch breach stats + goNoGo + VRP enrichment for a single ticker."""
            from backend.e1_vrp_engine import (
                compute_vrp_score,
                compute_earnings_width_comparison,
                compute_entry_quality,
                compute_e1_desk_consensus,
                compute_em_preference,
            )

            payload = compute_breach_stats(
                client=client,
                ticker=ticker,
                n=n,
                years=years,
                k=k,
                trade_builder_inputs=None,
                flags_override=base_flags,
                next_event_override=shared_override,
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

            try:
                events = payload.get("events") or []
                current = payload.get("current") or {}
                current_em_pct = None
                try:
                    current_em_pct = float(current.get("impliedMovePct") or 0) or None
                except Exception:
                    pass

                vrp = compute_vrp_score(events, current_implied_move_pct=current_em_pct)
                payload["vrpAnalysis"] = vrp

                em_mults = [float(x.strip()) for x in str(base_flags.E1_EM_MULTS).split(",") if x.strip()]
                wing_pts = [float(x.strip()) for x in str(base_flags.E1_WING_WIDTH_PTS).split(",") if x.strip()]
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
                payload["widthComparison"] = wc
                payload["emBreachSummary"] = em_breach

                eq = compute_entry_quality(
                    iv_elevation=vrp.get("ivElevation"),
                    skew_overlay=payload.get("skewOverlay"),
                    regime=payload.get("regime"),
                    ticker_dealer_gamma=payload.get("tickerDealerGamma"),
                    current=current,
                    go_no_go=payload.get("goNoGo"),
                )
                payload["entryQuality"] = eq

                dc = compute_e1_desk_consensus(
                    vrp=vrp,
                    entry_quality=eq,
                    em_breach_summary=em_breach,
                    regime=payload.get("regime"),
                    gap_vs_ctc=payload.get("gapVsCtc"),
                    event_risk=payload.get("eventRisk"),
                )
                emp = compute_em_preference(em_breach, vrp.get("vrpScore"), eq.get("entryQuality"))
                # E1 v2: only attach verdict-emitting fields when the flag is on.
                if bool(getattr(base_flags, "E1_EMIT_DESK_CONSENSUS", False)):
                    payload["deskConsensus"] = dc
                    payload["emPreference"] = emp
            except Exception as e:
                LOG.warning(f"VRP enrichment failed for {ticker}: {e}")

            return ticker, payload

        with ThreadPoolExecutor(max_workers=min(len(ticker_list), 5)) as executor:
            futures = {executor.submit(fetch_single, t): t for t in ticker_list}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    _, payload = future.result(timeout=_BREACH_COMPARE_FUTURE_TIMEOUT_S)
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
# Engine 10 Portfolio Advisor (multi-ticker allocation game plan)
# ---------------------------------------------------------------------------

def _ensure_desk_consensus_on_rankings(rankings: list) -> None:
    """Re-attach ``deskConsensus`` + ``emPreference`` to each ranking in place.

    The public ``/api/breach-compare`` response strips these blocks when
    ``E1_EMIT_DESK_CONSENSUS=False`` (the v2 default), but the E10 portfolio
    advisor's deterministic allocator keys off ``deskConsensus.verdict`` to
    decide which names are tradeable. When the client sends pre-computed
    rankings to the advisor's fast path, the stripped payloads cause every
    ticker to default to ``PASS`` and the allocator returns zero deploy.

    Recompute the verdict locally from the per-ticker enrichment fields the
    client preserves (``vrpAnalysis`` / ``entryQuality`` / ``emBreachSummary``,
    plus ``regime`` / ``gapVsCtc`` / ``eventRisk`` when available). Slow-path
    rankings already carry the consensus, so this is a no-op for them.
    """
    if not rankings:
        return
    try:
        from backend.e1_vrp_engine import (
            compute_e1_desk_consensus,
            compute_em_preference,
        )
    except Exception:  # pragma: no cover - import safety
        return

    for r in rankings:
        if not isinstance(r, dict):
            continue
        fp = r.get("fullPayload")
        if not isinstance(fp, dict):
            continue
        dc = fp.get("deskConsensus")
        if isinstance(dc, dict) and dc.get("verdict"):
            continue  # already populated (slow path or flag-on response)

        vrp = fp.get("vrpAnalysis") or {}
        eq = fp.get("entryQuality") or {}
        embs = fp.get("emBreachSummary") or {}
        if not vrp and not embs:
            continue  # no signal to recompute from — leave as-is

        try:
            recomputed_dc = compute_e1_desk_consensus(
                vrp=vrp,
                entry_quality=eq,
                em_breach_summary=embs,
                regime=fp.get("regime") or {},
                gap_vs_ctc=fp.get("gapVsCtc") or {},
                event_risk=fp.get("eventRisk") or {},
            )
            fp["deskConsensus"] = recomputed_dc
        except Exception as e:
            LOG.warning("E10 advisor: failed to recompute deskConsensus for %s: %s", r.get("ticker"), e)
            continue

        if not isinstance(fp.get("emPreference"), dict):
            try:
                fp["emPreference"] = compute_em_preference(
                    embs, vrp.get("vrpScore"), eq.get("entryQuality")
                )
            except Exception as e:
                LOG.debug("E10 advisor: failed to recompute emPreference for %s: %s", r.get("ticker"), e)


@router.post("/api/breach-compare/advisor")
async def e10_portfolio_advisor(request: Request):
    """Run the Engine 10 Portfolio Advisor: deterministic allocation + LLM game plan.

    Accepts pre-computed rankings from the client (from the initial /api/breach-compare
    call) so we skip the expensive re-fetch of ORATS data.  Falls back to a fresh
    fetch if ``rankings`` is not provided in the body.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    try:
        from backend.e10_portfolio_advisor import (
            compute_portfolio_allocation,
            generate_portfolio_advisor,
        )

        f = get_flags()
        rankings = body.get("rankings")

        # --- Fast path: client sent pre-computed rankings from /api/breach-compare ---
        if isinstance(rankings, list) and len(rankings) > 0:
            LOG.info(f"E10 Portfolio Advisor (fast path): {len(rankings)} pre-computed rankings")
        else:
            # --- Slow path: fetch from scratch (fallback) ---
            tickers_raw = body.get("tickers", "")
            if isinstance(tickers_raw, list):
                tickers_raw = ",".join(tickers_raw)
            tickers_raw = str(tickers_raw).strip()
            if not tickers_raw:
                raise HTTPException(status_code=400, detail="tickers or rankings is required")

            k = float(body.get("k", 1.0))
            n_param = int(body.get("n", 20))
            years_param = int(body.get("years", 5))

            ticker_list = [t.strip().upper() for t in tickers_raw.split(",") if t.strip()]
            ticker_list = list(dict.fromkeys(ticker_list))
            if not ticker_list:
                raise HTTPException(status_code=400, detail="No valid tickers provided")
            if len(ticker_list) > 10:
                raise HTTPException(status_code=400, detail="Maximum 10 tickers allowed")

            LOG.info(f"E10 Portfolio Advisor (slow path): {len(ticker_list)} tickers at k={k}")

            from backend.e1_vrp_engine import (
                compute_vrp_score,
                compute_earnings_width_comparison,
                compute_entry_quality,
                compute_e1_desk_consensus,
                compute_em_preference,
            )

            client = get_client()
            benzinga_client = get_benzinga_client_optional()
            payloads: list[tuple[str, dict]] = []
            errors: list[dict] = []

            def _fetch_enriched(ticker: str):
                payload = compute_breach_stats(
                    client=client, ticker=ticker, n=n_param, years=years_param, k=k,
                    trade_builder_inputs=None, flags_override=f, benzinga_client=benzinga_client,
                )
                try:
                    payload["goNoGo"] = compute_go_no_go(
                        client, ticker=ticker, payload=payload, benzinga_client=benzinga_client,
                    )
                except Exception as e:
                    LOG.warning(f"goNoGo failed for {ticker}: {e}")
                try:
                    events = payload.get("events") or []
                    current = payload.get("current") or {}
                    current_em_pct = None
                    try:
                        current_em_pct = float(current.get("impliedMovePct") or 0) or None
                    except Exception:
                        pass
                    vrp = compute_vrp_score(events, current_implied_move_pct=current_em_pct)
                    payload["vrpAnalysis"] = vrp
                    em_mults = [float(x.strip()) for x in str(f.E1_EM_MULTS).split(",") if x.strip()]
                    wing_pts = [float(x.strip()) for x in str(f.E1_WING_WIDTH_PTS).split(",") if x.strip()]
                    stock_price = None
                    try:
                        stock_price = float(current.get("stockPrice") or 0) or None
                    except Exception:
                        pass
                    wc, em_breach = compute_earnings_width_comparison(
                        events, em_mults=em_mults, wing_pts=wing_pts,
                        current_implied_move_pct=current_em_pct, stock_price=stock_price,
                    )
                    payload["widthComparison"] = wc
                    payload["emBreachSummary"] = em_breach
                    eq = compute_entry_quality(
                        iv_elevation=vrp.get("ivElevation"), skew_overlay=payload.get("skewOverlay"),
                        regime=payload.get("regime"), ticker_dealer_gamma=payload.get("tickerDealerGamma"),
                        current=current, go_no_go=payload.get("goNoGo"),
                    )
                    payload["entryQuality"] = eq
                    dc = compute_e1_desk_consensus(
                        vrp=vrp, entry_quality=eq, em_breach_summary=em_breach,
                        regime=payload.get("regime"), gap_vs_ctc=payload.get("gapVsCtc"),
                        event_risk=payload.get("eventRisk"),
                    )
                    payload["deskConsensus"] = dc
                    emp = compute_em_preference(em_breach, vrp.get("vrpScore"), eq.get("entryQuality"))
                    payload["emPreference"] = emp
                except Exception as e:
                    LOG.warning(f"VRP enrichment failed for {ticker}: {e}")
                return ticker, payload

            with ThreadPoolExecutor(max_workers=min(len(ticker_list), 5)) as executor:
                futures = {executor.submit(_fetch_enriched, t): t for t in ticker_list}
                for future in as_completed(futures):
                    ticker = futures[future]
                    try:
                        _, payload = future.result(timeout=_BREACH_COMPARE_FUTURE_TIMEOUT_S)
                        payloads.append((ticker, payload))
                    except Exception as e:
                        LOG.warning(f"Failed to fetch {ticker}: {e}")
                        errors.append({"ticker": ticker, "error": str(e)})

            rankings = rank_tickers(payloads)

        regime_label = "moderate"
        try:
            from backend.daily_market_state import load_dms as _load_dms
            from backend.redis_store import get_store_optional
            _store = get_store_optional()
            if _store:
                import datetime as _dt
                _dms = _load_dms(_dt.date.today().strftime("%Y-%m-%d"), _store)
                if _dms:
                    _d = _dms.to_dict()
                    regime_label = (_d.get("regime") or {}).get("label", "moderate")
        except Exception:
            pass

        # Re-enrich any rankings that arrived stripped of deskConsensus
        # (the public /api/breach-compare scrubs verdict fields when
        # E1_EMIT_DESK_CONSENSUS=False, which is the v2 default).
        _ensure_desk_consensus_on_rankings(rankings)

        det_alloc = compute_portfolio_allocation(rankings, market_regime_label=regime_label)
        advisor = generate_portfolio_advisor(
            rankings=rankings,
            deterministic_allocation=det_alloc,
            flags=f,
        )

        return {
            "asOfDate": dt.date.today().isoformat(),
            "tickers": [r.get("ticker") for r in rankings],
            "deterministicAllocation": det_alloc,
            "advisor": advisor,
            "errors": None,
        }

    except HTTPException:
        raise
    except (BreachInputError, OratsError) as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (breach-compare/advisor)")
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

    # E1 v2: accept event_date / event_timing with mc_event_* aliases.
    event_date_eff = str(body.get("event_date") or body.get("mc_event_date") or "").strip() or None
    event_timing_eff = str(body.get("event_timing") or body.get("mc_event_timing") or "").strip().upper() or None
    if event_timing_eff and event_timing_eff not in ("AMC", "BMO"):
        raise HTTPException(status_code=400, detail="event_timing must be AMC or BMO")
    if event_date_eff:
        try:
            dt.date.fromisoformat(event_date_eff[:10])
        except ValueError as _e:
            raise HTTPException(status_code=400, detail="event_date must be YYYY-MM-DD") from _e

    f = get_flags()
    if getattr(f, "E1_REQUIRE_EVENT_DATE", False) and bool(getattr(f, "ENABLE_E1_V2", False)):
        if not event_date_eff or not event_timing_eff:
            raise HTTPException(
                status_code=400,
                detail="event_date + event_timing (AMC|BMO) are required by Engine 1 v2.",
            )

    try:
        client = get_client()
        benzinga_client = get_benzinga_client_optional()

        payload = compute_breach_stats(
            client=client,
            ticker=ticker,
            n=int(body.get("n", 20)),
            years=int(body.get("years", 5)),
            k=1.0,
            flags_override=f,
            next_event_override=(
                {"date": event_date_eff, "timing": event_timing_eff}
                if event_date_eff else None
            ),
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
    """Log a new Engine 1 earnings IC trade.

    Body honors ``mode`` (``"tracked"`` default, or ``"live"`` for an
    immediately-committed position). Tracked trades behave identically
    in storage and live-review pipelines; the UI buckets them separately
    and an explicit ``/promote`` flip is required to mark them live.
    """
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

    from backend.e1_earnings_trades import log_trade, get_trade
    trade_id = log_trade(body)
    if trade_id is None:
        raise HTTPException(status_code=500, detail="Failed to persist trade")
    persisted = get_trade(trade_id) or {}
    return {
        "tradeId": trade_id,
        "status": "active",
        "mode": persisted.get("mode", "tracked"),
    }


@router.post("/api/breach/trade/draft-price")
async def e1_trade_draft_price(request: Request):
    """Single-placement strike + credit preview for the Trade Builder.

    Body:
        {
            "ticker":        "NVDA",       # required
            "event_date":    "YYYY-MM-DD", # required
            "event_timing":  "AMC" | "BMO",# required
            "emMultiple":    1.25,         # required, > 0
            "wingWidth":     5.0           # required, > 0 (points)
        }

    Returns the four candidate strikes (rounded to nearest $0.50), the
    estimated credit from the breach trade builder when available, and
    breach-distance percentages. Uses the existing ORATS breach cache
    (5-min TTL via `breach_cache`) so repeated previews on the same
    ticker do not re-fetch.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    ticker = str(body.get("ticker") or "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")

    event_date_eff = str(body.get("event_date") or "").strip() or None
    event_timing_eff = str(body.get("event_timing") or "").strip().upper() or None
    if not event_date_eff or not event_timing_eff:
        raise HTTPException(
            status_code=400,
            detail="event_date + event_timing (AMC|BMO) are required.",
        )
    if event_timing_eff not in ("AMC", "BMO"):
        raise HTTPException(status_code=400, detail="event_timing must be AMC or BMO")
    try:
        dt.date.fromisoformat(event_date_eff[:10])
    except ValueError as _e:
        raise HTTPException(status_code=400, detail="event_date must be YYYY-MM-DD") from _e

    try:
        em_mult = float(body.get("emMultiple") or 0)
        wing_pts = float(body.get("wingWidth") or 0)
    except (TypeError, ValueError) as _e:
        raise HTTPException(status_code=400, detail="emMultiple and wingWidth must be numeric") from _e
    if not (0.25 <= em_mult <= 3.0):
        raise HTTPException(status_code=400, detail="emMultiple out of range [0.25, 3.0]")
    if not (1.0 <= wing_pts <= 50.0):
        raise HTTPException(status_code=400, detail="wingWidth out of range [1.0, 50.0]")

    f = get_flags()
    key = breach_cache_key(ticker=ticker, n=20, years=5, k=1.0, event_date=event_date_eff, event_timing=event_timing_eff)
    payload = None
    with breach_cache_lock:
        payload = breach_cache.get(key)

    try:
        if payload is None:
            client = get_client()
            benzinga_client = get_benzinga_client_optional()
            payload = compute_breach_stats(
                client=client,
                ticker=ticker,
                n=20,
                years=5,
                k=1.0,
                flags_override=f,
                next_event_override={"date": event_date_eff, "timing": event_timing_eff},
                benzinga_client=benzinga_client,
            )
            with breach_cache_lock:
                breach_cache[key] = payload
    except BreachInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OratsError as e:
        LOG.exception("ORATS failure (draft-price)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("draft-price failed")
        raise HTTPException(status_code=500, detail="Internal error") from e

    current = payload.get("current") or {}
    try:
        spot = float(current.get("stockPrice") or 0) or None
    except Exception:
        spot = None
    try:
        em_pct = float(current.get("impliedMovePct") or 0) or None
    except Exception:
        em_pct = None

    if not spot or not em_pct or em_pct <= 0:
        raise HTTPException(
            status_code=422,
            detail="Spot or implied-move not available for draft pricing; rerun the E1 scan first.",
        )

    em_dollars = spot * (em_pct / 100.0)
    short_put_raw = spot - (em_mult * em_dollars)
    short_call_raw = spot + (em_mult * em_dollars)

    def _round_half(x: float) -> float:
        return round(x * 2.0) / 2.0

    short_put = _round_half(short_put_raw)
    short_call = _round_half(short_call_raw)
    long_put = _round_half(short_put - wing_pts)
    long_call = _round_half(short_call + wing_pts)

    breach_dist_put_pct = round((spot - short_put) / spot * 100.0, 2) if spot else None
    breach_dist_call_pct = round((short_call - spot) / spot * 100.0, 2) if spot else None

    # Best-effort credit lookup from the trade-builder block if it matches
    # our wing width; otherwise fall back to a heuristic of ~10% of wing.
    est_credit = None
    credit_source = "heuristic"
    tb = payload.get("tradeBuilder") or {}
    try:
        if isinstance(tb, dict):
            tb_wings = tb.get("wingWidth")
            tb_credit = tb.get("credit") or tb.get("midCredit") or tb.get("estCredit")
            if tb_wings is not None and tb_credit is not None and abs(float(tb_wings) - wing_pts) < 0.5:
                est_credit = round(float(tb_credit), 2)
                credit_source = "trade_builder"
    except Exception:
        pass
    if est_credit is None:
        est_credit = round(wing_pts * 0.10, 2)

    return {
        "ticker": ticker,
        "eventDate": event_date_eff,
        "eventTiming": event_timing_eff,
        "emMultiple": em_mult,
        "wingWidth": wing_pts,
        "spot": round(spot, 2),
        "impliedMovePct": round(em_pct, 2),
        "shortPutStrike": short_put,
        "longPutStrike": long_put,
        "shortCallStrike": short_call,
        "longCallStrike": long_call,
        "estCredit": est_credit,
        "creditSource": credit_source,
        "breachDistPutPct": breach_dist_put_pct,
        "breachDistCallPct": breach_dist_call_pct,
        "historyBreakerRisk": (payload.get("historyBreakerRisk") if isinstance(payload.get("historyBreakerRisk"), dict) else None),
    }


@router.get("/api/breach/trades")
def e1_list_trades():
    """List active Engine 1 earnings IC trades (tracked + live)."""
    from backend.e1_earnings_trades import list_active_trades
    return {"trades": list_active_trades()}


@router.post("/api/breach/trade/{trade_id}/promote")
async def e1_trade_promote(trade_id: str):
    """Promote a tracked Engine 1 trade to live mode."""
    from backend.e1_earnings_trades import promote_to_live
    trade = promote_to_live(trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found, closed, or unavailable")
    return {
        "tradeId": trade_id,
        "mode": trade.get("mode"),
        "promotedAt": trade.get("promotedAt"),
        "status": trade.get("status"),
    }


@router.post("/api/breach/trade/{trade_id}/live-review")
async def e1_trade_live_review(trade_id: str, request: Request):
    """v2: Live open-trade review (three-phase, quant-grade evidence).

    The existing /checkin endpoint is earnings-specific (requires
    postEarningsOpen to compute gap / breach). Before earnings comes
    out, the desk still wants a hold/cut recommendation from the LLM
    reading *current* conditions, and after the print the desk wants a
    quant-grade replay-vs-current-mark verdict.

    v2 routes through ``backend.e1_live_review.run_live_review`` which
    assembles a phase-tuned evidence packet (current vs entry deltas,
    news, regime, macro, analogue breach ladder, AND a full E15-style
    replay of the desk's strikes against the analogue pool) in parallel,
    feeds it into a phase-aware LLM verdict, and persists the full
    payload as a check-in for post-mortem reconstruction.

    Body (optional):
      {
        "phase":       "pre_event" | "pre_open" | "post_open"  # explicit; else auto
        "currentSpot": 180.5,     # override for backtest / paper
        "currentVix":  16.2,      # override
        "notes":       "...",     # user note
        "force_refresh": true     # bypass 5-min orchestrator cache
      }
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    from backend.e1_earnings_trades import get_trade, add_checkin

    trade = get_trade(trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")

    # --- v2 path -------------------------------------------------------
    use_v2 = bool(body.get("v2", True))
    if use_v2:
        try:
            from backend.e1_live_review import run_live_review

            payload = run_live_review(
                trade=trade,
                phase_request=body.get("phase"),
                current_spot_override=float(body.get("currentSpot") or 0.0),
                current_vix_override=body.get("currentVix"),
                notes=body.get("notes"),
                force_refresh=bool(body.get("force_refresh", False)),
            )
            review = payload.get("review") or {}

            # Persist full v2 review (phase + evidence + recommendation) so
            # the desk can reconstruct what they saw at any past check-in
            # for post-mortems and learning. ``add_checkin`` accepts an
            # arbitrary dict; type=live_review_v2 marks the new shape.
            try:
                checkin_record = dict(review)
                checkin_record["type"] = "live_review_v2"
                checkin_record["userNotes"] = body.get("notes")
                add_checkin(trade_id, checkin_record)
            except Exception:
                pass

            return payload
        except Exception as e:
            LOG.exception("E1 live review v2 dispatch failed; falling through to v1")
            # Intentionally fall through to v1 on unexpected failures so the
            # desk never sees a 500 from a tertiary feature regression.

    # --- v1 fallback (legacy contract) ---------------------------------

    ticker = trade.get("ticker") or ""
    entry = trade.get("entry") or {}
    entry_ctx = trade.get("entryContext") or {}
    short_put = float(entry.get("shortPutStrike") or 0)
    short_call = float(entry.get("shortCallStrike") or 0)
    long_put = float(entry.get("longPutStrike") or 0)
    long_call = float(entry.get("longCallStrike") or 0)
    entry_credit = float(entry.get("entryCredit") or 0)
    spot_at_entry = float(entry.get("spotAtEntry") or body.get("spotAtEntry") or 0)
    earn_date = str(entry.get("earningsDate") or "")[:10]
    timing = str(entry.get("earningsTiming") or "").upper()

    # Pull current spot + VIX (best-effort).
    current_spot = float(body.get("currentSpot") or 0)
    current_vix = body.get("currentVix")
    if current_spot <= 0:
        try:
            from backend.deps import get_client_optional
            from backend.technicals import fetch_live_price_context_optional
            orats = get_client_optional()
            if orats and ticker:
                px = fetch_live_price_context_optional(client=orats, ticker=ticker)
                current_spot = float((px or {}).get("price") or 0)
        except Exception:
            current_spot = 0.0
    if current_vix is None:
        try:
            from backend.deps import get_client_optional
            orats = get_client_optional()
            if orats and ticker:
                resp = orats.live_summaries(ticker=ticker)
                rows = resp.rows or []
                if rows:
                    current_vix = rows[0].get("iv30dMean") or rows[0].get("ivMean")
        except Exception:
            current_vix = None

    # Derive rough status chips (pre-LLM so the response is useful even
    # when the LLM is rate-limited or unavailable).
    put_dist_pct = None
    call_dist_pct = None
    nearest_short_pct = None
    if current_spot > 0 and short_put > 0:
        put_dist_pct = round((current_spot - short_put) / current_spot * 100.0, 2)
    if current_spot > 0 and short_call > 0:
        call_dist_pct = round((short_call - current_spot) / current_spot * 100.0, 2)
    dists = [d for d in (put_dist_pct, call_dist_pct) if d is not None]
    if dists:
        nearest_short_pct = min(dists)

    status_chip = "unknown"
    if nearest_short_pct is not None:
        if nearest_short_pct < 0:
            status_chip = "breached"
        elif nearest_short_pct < 0.5:
            status_chip = "short_strike_challenged"
        elif nearest_short_pct < 1.5:
            status_chip = "caution"
        else:
            status_chip = "on_track"

    # Days remaining until earnings.
    import datetime as _dt
    days_to_earnings = None
    if earn_date:
        try:
            ed = _dt.date.fromisoformat(earn_date)
            days_to_earnings = (ed - _dt.date.today()).days
        except Exception:
            days_to_earnings = None

    # LLM narrative (best-effort; deterministic shell otherwise).
    llm = None
    try:
        from backend.e1_earnings_advisor import _get_openai_client, _parse_llm_json
        client = _get_openai_client()
        if client and current_spot > 0:
            prompt = (
                f"Live open-trade review for {ticker} short iron condor. "
                f"Entry: put wings {long_put}/{short_put}, call wings "
                f"{short_call}/{long_call}, credit ${entry_credit:.2f}, "
                f"spot at entry ${spot_at_entry:.2f}. "
                f"Earnings {earn_date or 'unknown'} {timing or ''}, "
                f"{days_to_earnings if days_to_earnings is not None else '?'} days away. "
                f"Current spot: ${current_spot:.2f}. Nearest short-strike distance: "
                f"{nearest_short_pct if nearest_short_pct is not None else '?'}%. "
                f"Current VIX proxy: {current_vix}. "
                f"Status chip: {status_chip}. "
                "Give the desk a verdict: HOLD (wait through white-knuckle; historically worth it), "
                "ADJUST (roll the challenged short out/down), or CUT (sell now before it gets worse). "
                "Return JSON with keys: verdict (HOLD|ADJUST|CUT), confidence (0-1), "
                "narrative (2-3 sentences), keyPoints (list of 2-4 bullets), "
                "riskFactors (list of 1-3 bullets), deskNote (one sentence)."
            )
            resp = client.chat.completions.create(
                model="gpt-5.5",
                messages=[
                    {"role": "system", "content": "You are a vol-crush desk analyst reviewing an OPEN earnings iron condor. Be concise, decisive, and cite the numbers."},
                    {"role": "user", "content": prompt},
                ],
                temperature=1, max_completion_tokens=2500, timeout=45,
                response_format={"type": "json_object"},
            )
            llm = _parse_llm_json(resp.choices[0].message.content.strip())
    except Exception:
        llm = None

    review_record = {
        "type": "live_review",
        "statusChip": status_chip,
        "currentSpot": current_spot,
        "currentVix": current_vix,
        "daysToEarnings": days_to_earnings,
        "putDistPct": put_dist_pct,
        "callDistPct": call_dist_pct,
        "nearestShortPct": nearest_short_pct,
        "llmAssessment": llm,
        "userNotes": body.get("notes"),
    }
    try:
        add_checkin(trade_id, review_record)
    except Exception:
        pass

    return {"tradeId": trade_id, "review": review_record}


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
                model="gpt-5.5",
                messages=[
                    {"role": "system", "content": "You are a vol-crush desk analyst reviewing a post-earnings outcome. Be concise."},
                    {"role": "user", "content": prompt},
                ],
                temperature=1, max_completion_tokens=2000, timeout=40,
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
