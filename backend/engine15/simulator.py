"""Engine 15 — earnings IC scenario simulator (main entrypoint).

Fuses Engine 1 earnings data with Engine 14's path-dependent replay
machinery. Public entrypoint: :func:`run_earnings_scenario`.

End-to-end flow:

1. Run Engine 1 on the ticker (``compute_breach_stats`` + VRP /
   width-comparison / entry-quality / desk-consensus enrichment) to
   harvest ``events[]`` and current state.
2. Build the analogue universe from those events
   (:mod:`backend.engine15.event_universe`).
3. Filter by anncTod parity + optional season
   (:mod:`backend.engine15.event_matcher`).
4. Resolve each event's historical expiry + entry-day chain; on-demand
   backfill any missing dates via
   :mod:`backend.engine15.chain_backfill`.
5. Replay each event from ``entry_date_hist`` → ``planned_exit_date_hist``
   via :mod:`backend.engine15.chain_replay_adapter`, producing
   :class:`backend.engine14.simulator.AnaloguePath` records that can
   flow through Engine 14's aggregation helpers.
6. Aggregate: outcome distribution, MTM timeline, bootstrap CI, exit
   rule optimization, sizing, greeks attribution.
7. Apply earnings-specialized conditioning modifiers
   (:mod:`backend.engine15.conditioning`) to produce
   ``adjustedOutcomeDistribution``.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from backend.config import FeatureFlags, get_flags
from backend.engine14.chain_replay import FillModel
from backend.engine14.greeks import aggregate_attribution, attribute_path
from backend.engine14.simulator import (
    AnaloguePath,
    _bootstrap_outcome_ci,
    _build_mtm_timeline,
    _summarize_conditioning,
    _summarize_outcomes,
    OUTCOMES,
)
from backend.engine14.sizing import compute_sizing
from backend.engine15 import chain_backfill, event_matcher, event_universe
from backend.engine15.chain_replay_adapter import (
    EventContext,
    resolve_event_context,
    simulate_event,
)
from backend.spx_ic.ohlc import iv_to_em1sigma_pct

LOG = logging.getLogger("engine15.simulator")

__version__ = "0.1.0"


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EarningsIcRequest:
    ticker: str
    entry_date: str
    expiry: str
    earnings_date: str
    earnings_timing: str                  # "BMO" | "AMC" | "UNK"
    planned_exit_date: str
    planned_exit_offset_hours: float
    short_put: float
    long_put: float
    short_call: float
    long_call: float
    credit_received: float
    profit_target_pct: float = 50.0
    stop_loss_pct: float = 150.0
    season_mode: str = "none"             # "none" | "quarter" | "month"
    season_value: Optional[str] = None
    include_e1_payload: bool = True
    n_history: int = 20
    years_history: int = 5

    def dte_calendar(self) -> int:
        try:
            a = dt.date.fromisoformat(self.entry_date)
            b = dt.date.fromisoformat(self.expiry)
            return max(1, (b - a).days)
        except Exception:
            return 7

    def planned_hold_biz_days(self) -> int:
        """Business-day gap from entry to planned exit (≥0)."""
        try:
            a = dt.date.fromisoformat(self.entry_date)
            b = dt.date.fromisoformat(self.planned_exit_date)
        except Exception:
            return 1
        if b <= a:
            return 0
        # Same Mon-Fri heuristic used by the universe builder.
        n = 0
        cur = a + dt.timedelta(days=1)
        while cur <= b:
            if cur.weekday() <= 4:
                n += 1
            cur += dt.timedelta(days=1)
        return max(0, n)

    def strike_tuple(self) -> Tuple[float, float, float, float]:
        return (
            float(self.short_put),
            float(self.long_put),
            float(self.short_call),
            float(self.long_call),
        )

    def wing_width(self) -> float:
        put_w = abs(float(self.short_put) - float(self.long_put))
        call_w = abs(float(self.long_call) - float(self.short_call))
        return float(min(put_w, call_w)) if (put_w > 0 and call_w > 0) else float(max(put_w, call_w))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_engine1(
    *,
    ticker: str,
    n: int,
    years: int,
    client: Any,
    benzinga_client: Any,
    flags: FeatureFlags,
) -> Dict[str, Any]:
    """Inline Engine 1 run — mirrors :func:`routers/engine15_earnings_ic._run_engine1`.

    Kept out of a shared util because the router call is public and
    orchestration-heavy (handles HTTP shaping), while this one is
    scoped to the simulator's need for VRP + events + next-event
    metadata only.
    """
    from backend.e1_vrp_engine import (
        compute_e1_desk_consensus,
        compute_earnings_width_comparison,
        compute_em_preference,
        compute_entry_quality,
        compute_vrp_score,
    )
    from backend.earnings_logic import compute_breach_stats
    from backend.go_no_go import compute_go_no_go

    payload = compute_breach_stats(
        client=client, ticker=ticker, n=int(n), years=int(years), k=1.0,
        trade_builder_inputs=None, flags_override=flags,
        benzinga_client=benzinga_client,
    )
    try:
        payload["goNoGo"] = compute_go_no_go(
            client, ticker=ticker, payload=payload, benzinga_client=benzinga_client,
        )
    except Exception as e:
        LOG.debug("engine15: goNoGo failed for %s: %s", ticker, e)

    events = payload.get("events") or []
    current = payload.get("current") or {}
    current_em_pct: Optional[float] = None
    try:
        current_em_pct = float(current.get("impliedMovePct") or 0) or None
    except Exception:
        pass
    # Pre-market on announcement day ORATS live /cores can return a null
    # implied move; fall back to the delayed snapshot so VRP re-scoring +
    # breach-at-current-EM stats stay populated.
    if current_em_pct is None:
        try:
            d = current.get("delayedImpliedMovePct")
            if d is not None:
                f = float(d)
                if f > 0:
                    current_em_pct = f
        except Exception:
            pass
    try:
        vrp = compute_vrp_score(events, current_implied_move_pct=current_em_pct)
        payload["vrpAnalysis"] = vrp
        em_mults = [float(x.strip()) for x in str(flags.E1_EM_MULTS).split(",") if x.strip()]
        wing_pts = [float(x.strip()) for x in str(flags.E1_WING_WIDTH_PTS).split(",") if x.strip()]
        stock_price: Optional[float] = None
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
            iv_elevation=vrp.get("ivElevation"),
            skew_overlay=payload.get("skewOverlay"),
            regime=payload.get("regime"),
            ticker_dealer_gamma=payload.get("tickerDealerGamma"),
            current=current, go_no_go=payload.get("goNoGo"),
        )
        payload["entryQuality"] = eq
        dc = compute_e1_desk_consensus(
            vrp=vrp, entry_quality=eq, em_breach_summary=em_breach,
            regime=payload.get("regime"), gap_vs_ctc=payload.get("gapVsCtc"),
            event_risk=payload.get("eventRisk"),
        )
        payload["deskConsensus"] = dc
        payload["emPreference"] = compute_em_preference(
            em_breach, vrp.get("vrpScore"), eq.get("entryQuality"),
        )
    except Exception as e:
        LOG.warning("engine15: VRP enrichment failed for %s: %s", ticker, e)
    return payload


def _infer_user_em_pct(
    req: EarningsIcRequest,
    current: Dict[str, Any],
) -> Tuple[float, float, str]:
    """Derive the user's entry spot + 1σ EM over (entry→expiry).

    Preference order:
      1. ``current.stockPrice`` + an option-market IV if cached for the
         requested expiry.
      2. ``current.stockPrice`` + fallback 30% annualized IV (single
         names are typically more volatile than SPX; we use 30% here
         instead of the 15% E14 uses for SPX).
      3. Strike midpoint — last-resort guard.
    """
    from backend.engine14 import chain_cache
    spot = None
    try:
        spot = float(current.get("stockPrice") or 0) or None
    except Exception:
        spot = None
    src_spot = "E1 current.stockPrice"
    if spot is None or spot <= 0:
        spot = (float(req.short_put) + float(req.short_call)) / 2.0
        src_spot = "strike midpoint fallback"

    # Is there a cached live chain on req.entry_date for req.expiry? (Usually
    # no — the cache is historical. Skip and use a fallback IV.)
    chain = chain_cache.fetch_chain_slice(
        ticker=req.ticker, trade_date=req.entry_date, expiry=req.expiry,
    )
    iv_dec: Optional[float] = None
    if chain:
        best = min(chain, key=lambda r: abs(float(r.strike) - float(spot)))
        iv = best.call_iv if best.call_iv is not None else best.put_iv
        if iv is not None and iv > 0:
            iv_dec = float(iv)

    if iv_dec is None:
        # Try to estimate IV from the current ATM implied move E1 reported.
        # Pre-market on announcement day, ORATS' live /cores endpoint often
        # returns ``impErnMv=null`` because it hasn't ticked yet; Engine 1
        # exposes the last-available delayed snapshot at
        # ``current.delayedImpliedMovePct`` — use it as a second source.
        em_pct_raw: Optional[float] = None
        em_source_label: Optional[str] = None
        try:
            v = current.get("impliedMovePct")
            if v is not None:
                f = float(v)
                if f > 0:
                    em_pct_raw = f
                    em_source_label = "E1 current.impliedMovePct"
        except Exception:
            pass
        if em_pct_raw is None:
            try:
                v = current.get("delayedImpliedMovePct")
                if v is not None:
                    f = float(v)
                    if f > 0:
                        em_pct_raw = f
                        delayed_ts = str(current.get("delayedTradeDate") or current.get("delayedUpdatedAt") or "").strip()
                        em_source_label = (
                            f"E1 current.delayedImpliedMovePct (pre-market fallback, asOf {delayed_ts or 'n/a'})"
                        )
            except Exception:
                pass
        if em_pct_raw is not None and em_pct_raw > 0:
            return (
                float(spot),
                float(em_pct_raw),
                f"{em_source_label} ({em_pct_raw:.2f}%); spot={src_spot}",
            )
        em = iv_to_em1sigma_pct(iv_pct=30.0, dte_calendar_days=req.dte_calendar())
        return (float(spot), float(em), f"fallback IV=30% annualized; spot={src_spot}")

    em = iv_to_em1sigma_pct(iv_pct=iv_dec * 100.0, dte_calendar_days=req.dte_calendar())
    return (
        float(spot),
        float(em),
        f"IV from cached entry chain; spot={src_spot}",
    )


def _empty_payload(
    *,
    req: EarningsIcRequest,
    reason: str,
    events_considered: int = 0,
    engine1: Optional[Dict[str, Any]] = None,
    coverage: Optional[Dict[str, Any]] = None,
    data_quality_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    empty_dist = {
        o: {"pct": 0.0, "n": 0, "avgPnlPct": 0.0, "avgDays": 0.0, "maxAdverseExcursionPct": 0.0}
        for o in OUTCOMES
    }
    data_quality = {
        "chainCoverage": coverage or {},
        "eventsConsidered": int(events_considered),
        "eventsWithFullChain": 0,
        "minEventsMet": False,
    }
    if data_quality_extra:
        data_quality.update(data_quality_extra)
    return {
        "engine": 15,
        "version": __version__,
        "request": asdict(req),
        "eventsUsed": 0,
        "eventsConsidered": int(events_considered),
        "entryState": None,
        "plannedExit": None,
        "outcomeDistribution": empty_dist,
        "outcomeDistributionCI": {"_meta": {"n": 0, "iterations": 0, "confidence": 0.90, "thinSample": True}},
        "adjustedOutcomeDistribution": {},
        "conditioningModifiers": {},
        "conditioningSummary": None,
        "mtmTimeline": [],
        "expectedValue": {"meanPnlPct": 0.0, "medianPnlPct": 0.0, "sharpeProxy": 0.0},
        "exitRulesOptimization": {
            "recommendedProfitTarget": float(req.profit_target_pct),
            "recommendedStopLoss": float(req.stop_loss_pct),
            "recommendedTimeStopDays": int(req.planned_hold_biz_days() or 1),
            "deltaFromDefault": {"winRatePct": 0.0, "avgPnlPct": 0.0},
        },
        "sizing": {"n": 0, "consensusFraction": 0.0},
        "greeksAttribution": {
            "n": 0, "deltaPct": 0.0, "gammaPct": 0.0, "thetaPct": 0.0,
            "vegaPct": 0.0, "residualPct": 0.0, "totalPct": 0.0,
        },
        "matchedEvents": [],
        "droppedEvents": [],
        "engine1Summary": _summarize_engine1(engine1),
        "engine1": engine1 if (engine1 and req.include_e1_payload) else None,
        "dataQuality": data_quality,
        "notes": [reason],
    }


def _summarize_engine1(e1: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact Engine-1 summary for the Engine-15 scan card grid.

    Mirrors the data Engine 1's UI surfaces so the desk sees the same
    numbers they'd see on /breach — ORATS EM (EOD + 15-min delayed),
    straddle EM (upcoming Friday expiry), 1x/1.5x/2x strike targets,
    regime/event-risk chips, and breach summary.
    """
    if not e1:
        return {}
    current = e1.get("current") or {}
    vrp = e1.get("vrpAnalysis") or {}
    dc = e1.get("deskConsensus") or {}
    em_breach = e1.get("emBreachSummary") or {}
    next_event = e1.get("nextEvent") or {}
    expected_move = e1.get("expectedMove") or {}
    strike_targets = e1.get("strikeTargets") or {}
    regime = e1.get("regime") or {}
    event_risk = e1.get("eventRisk") or {}
    summary = e1.get("summary") or {}

    em_pct = current.get("impliedMovePct")
    em_pct_source = "live"
    if em_pct is None:
        em_pct = current.get("delayedImpliedMovePct")
        if em_pct is not None:
            em_pct_source = "delayed"

    # Breach summary can arrive either as a simple float map keyed
    # by EM multiple ("1.0": 0.0, ...) from compute_earnings_width_comparison,
    # or as a dict with breachRatePct/n keys from older callsites. Support
    # both so the UI can always render a headline "n-ratio breach" number.
    breach_rate_1x: Optional[float] = None
    breach_rate_15x: Optional[float] = None
    breach_rate_2x: Optional[float] = None
    try:
        if isinstance(em_breach, dict):
            if "1.0" in em_breach:
                breach_rate_1x = float(em_breach.get("1.0"))
            if "1.5" in em_breach:
                breach_rate_15x = float(em_breach.get("1.5"))
            if "2.0" in em_breach:
                breach_rate_2x = float(em_breach.get("2.0"))
    except Exception:
        pass
    breach_rate_fallback = em_breach.get("breachRatePct") if isinstance(em_breach, dict) else None

    return {
        # --- Core anchors ---
        "ticker": e1.get("ticker"),
        "stockPrice": current.get("stockPrice"),
        "asOfDate": current.get("asOfDate"),
        "vrpScore": vrp.get("vrpScore"),
        "ivElevation": vrp.get("ivElevation"),
        "deskConsensus": dc.get("consensus") or dc.get("verdict"),
        "deskConsensusScore": dc.get("score"),
        "historyN": len(e1.get("events") or []),
        "eventsUsed": summary.get("events_used"),
        "eventsFound": summary.get("events_found"),
        # --- ORATS EM (impErnMv) — EOD + 15-min delayed ---
        "oratsEmPct": current.get("impliedMovePct"),
        "oratsEmAsOf": current.get("asOfDate"),
        "oratsEmSource": current.get("source"),
        "delayedEmPct": current.get("delayedImpliedMovePct"),
        "delayedUpdatedAt": current.get("delayedUpdatedAt"),
        "delayedTradeDate": current.get("delayedTradeDate"),
        # Headline EM the UI renders — prefers live, falls back to delayed.
        "emPct": em_pct,
        "emPctSource": em_pct_source,
        # --- Straddle EM (upcoming Friday expiry) ---
        "straddleEmPct": expected_move.get("expectedMovePct"),
        "straddleEmDollars": expected_move.get("expectedMoveDollars"),
        "straddleExpiry": expected_move.get("expiry"),
        "straddleSource": expected_move.get("source"),
        "straddleDte": expected_move.get("dte"),
        "straddleSpotPrice": expected_move.get("spotPrice"),
        # --- Strike Targets (1x/1.5x/2x wing distances as % of spot) ---
        "strikeTargets": {
            "whitePct": strike_targets.get("whitePct"),
            "bluePct": strike_targets.get("bluePct"),
            "redPct": strike_targets.get("redPct"),
            "whitePts": strike_targets.get("whitePts"),
            "bluePts": strike_targets.get("bluePts"),
            "redPts": strike_targets.get("redPts"),
            "emSource": strike_targets.get("emSource"),
            "basedOnEmPct": strike_targets.get("basedOnEmPct"),
            "basedOnSpot": strike_targets.get("basedOnSpot"),
        } if strike_targets else None,
        # --- Next event (with Friday-expiry fallback) ---
        "anncTod": next_event.get("anncTod") or next_event.get("timing") or next_event.get("timingPlanned"),
        "nextEventDate": next_event.get("earnDate") or next_event.get("date") or next_event.get("earnDateNext"),
        "nextEventPricingExpiry": next_event.get("pricingExpiry"),
        "nextEventSource": next_event.get("source"),
        "nextEventConfidence": next_event.get("confidence"),
        # --- Regime / event risk chips ---
        "regimeLabel": regime.get("label"),
        "regimeTailMultiplier": regime.get("tailMultiplier"),
        "eventRiskLabel": event_risk.get("label"),
        # --- Breach summary (supports both float-map and legacy dict shape) ---
        "emBreachRate1xPct": breach_rate_1x if breach_rate_1x is not None else breach_rate_fallback,
        "emBreachRate15xPct": breach_rate_15x,
        "emBreachRate2xPct": breach_rate_2x,
        "emBreachN": summary.get("events_used"),
        # Back-compat fields (older callers).
        "emBreachPct": em_breach.get("breachRatePct") or em_breach.get("breachPct") if isinstance(em_breach, dict) else None,
    }


def _planned_exit_fidelity_caveat(req: EarningsIcRequest, crush_factor: float) -> str:
    hours = float(req.planned_exit_offset_hours or 0)
    if req.planned_exit_date == req.entry_date:
        return (
            f"Planned exit same day as entry ({req.planned_exit_date} "
            f"+{hours:.1f}h after open). Historical slices are EOD; treating "
            f"intraday move as approximated by entry-to-close blend."
        )
    return (
        f"Planned exit {req.planned_exit_date} +{hours:.1f}h after open. "
        f"ORATS historical chains are EOD, so replay uses the {req.planned_exit_date} "
        f"close and applies an intraday crush factor of {crush_factor:.2f} "
        "toward the entry-day P&L to approximate the desk's AM exit. "
        "This is conservative on winners and slightly pessimistic on losers; "
        "calibration from journaled trades is a Phase 2 enhancement."
    )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def run_earnings_scenario(
    request: EarningsIcRequest,
    *,
    client: Any,
    flags: Optional[FeatureFlags] = None,
    benzinga_client: Any = None,
    store: Any = None,
) -> Dict[str, Any]:
    """Run Engine 15 end-to-end for a single user request."""
    flags = flags or get_flags()
    ticker = request.ticker.upper()

    # --- 1. Engine 1 pull ------------------------------------------------
    try:
        engine1 = _run_engine1(
            ticker=ticker,
            n=int(request.n_history),
            years=int(request.years_history),
            client=client,
            benzinga_client=benzinga_client,
            flags=flags,
        )
    except Exception as e:
        LOG.exception("engine15: Engine 1 pull failed for %s", ticker)
        raise ValueError(f"Engine 1 pull failed: {type(e).__name__}: {e}")

    events = list(engine1.get("events") or [])
    if not events:
        return _empty_payload(
            req=request,
            reason=f"No earnings events available for {ticker} from Engine 1.",
            engine1=engine1,
        )

    # --- 2. Build event universe ----------------------------------------
    universe = event_universe.build_event_universe(
        events,
        ticker=ticker,
        user_entry_date=request.entry_date,
        user_planned_exit_date=request.planned_exit_date,
        user_earnings_date=request.earnings_date,
    )[: int(flags.ENGINE15_MAX_EVENTS)]
    if not universe:
        return _empty_payload(
            req=request,
            reason="No parseable earnings events (check E1 'events[]' payload).",
            engine1=engine1,
            events_considered=len(events),
        )

    # --- 3. Filter ------------------------------------------------------
    criteria = event_matcher.MatchCriteria(
        user_annc_tod=request.earnings_timing,
        season_mode=str(request.season_mode or "none"),
        season_value=request.season_value,
        strict_annc_tod=True,
        enable_em_multiple_filter=bool(flags.ENGINE15_ENABLE_EM_MULTIPLE_FILTER),
        em_multiple_tol=float(flags.ENGINE15_EM_MULTIPLE_TOL),
    )
    admitted, dropped = event_matcher.filter_events(universe, criteria=criteria)
    events_considered = len(universe)

    if len(admitted) < int(flags.ENGINE15_MIN_EVENTS):
        # Soften anncTod strictness as a second pass — better to return a
        # mixed-pool payload with a caveat than a hard 400 for a thin name.
        relaxed = event_matcher.MatchCriteria(
            user_annc_tod=request.earnings_timing,
            season_mode="none",
            season_value=None,
            strict_annc_tod=False,
            enable_em_multiple_filter=False,
            em_multiple_tol=criteria.em_multiple_tol,
        )
        relaxed_admitted, relaxed_dropped = event_matcher.filter_events(universe, criteria=relaxed)
        if len(relaxed_admitted) >= int(flags.ENGINE15_MIN_EVENTS):
            admitted = relaxed_admitted
            dropped = relaxed_dropped

    # --- 4. Resolve per-event context (on-demand backfill if needed) ----
    from backend.engine14 import chain_cache
    coverage_before = chain_cache.cache_coverage(ticker=ticker)

    missing_dates: List[str] = []
    for w in admitted:
        if not chain_cache.has_trade_date(ticker=ticker, trade_date=w.entry_date_hist):
            missing_dates.append(w.entry_date_hist)
        if not chain_cache.has_trade_date(ticker=ticker, trade_date=w.planned_exit_date_hist):
            missing_dates.append(w.planned_exit_date_hist)

    backfill_summary: Optional[Dict[str, Any]] = None
    if missing_dates:
        # Build a minimal set of "events" for the backfiller from the
        # ADMITTED universe only, so we don't spend quota on dropped events.
        bf_events = [
            {"earnDate": w.earn_date_hist, "anncTod": w.annc_tod}
            for w in admitted
        ]
        try:
            backfill_summary = chain_backfill.backfill_ticker_events(
                client,
                ticker=ticker,
                earnings_events=bf_events,
                days_before=int(flags.ENGINE15_EVENT_BACKFILL_DAYS_BEFORE),
                days_after=int(flags.ENGINE15_EVENT_BACKFILL_DAYS_AFTER),
                delay_ms=int(flags.ENGINE15_BACKFILL_DELAY_MS),
            )
        except Exception as e:
            LOG.warning("engine15: on-demand backfill failed for %s: %s", ticker, e)
            backfill_summary = {"error": f"{type(e).__name__}: {e}"}

    coverage_after = chain_cache.cache_coverage(ticker=ticker)

    # --- 5. Resolve contexts + replay -----------------------------------
    target_dte_cal = request.dte_calendar()
    contexts: List[EventContext] = []
    no_chain_drops: List[Dict[str, str]] = []
    for w in admitted:
        ctx = resolve_event_context(
            ticker=ticker,
            window=w,
            target_dte_calendar=target_dte_cal,
            max_expiry_offset_days=int(flags.ENGINE15_MAX_EXPIRY_OFFSET_DAYS),
        )
        if ctx is None:
            no_chain_drops.append({
                "earnDate": w.earn_date_hist,
                "reason": f"no cached expiry within ±{flags.ENGINE15_MAX_EXPIRY_OFFSET_DAYS}d of target DTE={target_dte_cal}",
            })
            continue
        contexts.append(ctx)

    events_with_full_chain = len(contexts)
    if events_with_full_chain < int(flags.ENGINE15_MIN_EVENTS):
        reason = (
            f"Replay pool too thin: {events_with_full_chain} events with full chain "
            f"(need ≥{flags.ENGINE15_MIN_EVENTS}). "
            "Run /api/earnings-ic/backfill or ``scripts/engine15_warm_ticker.py`` first."
        )
        return _empty_payload(
            req=request,
            reason=reason,
            engine1=engine1,
            events_considered=events_considered,
            coverage=coverage_after,
            data_quality_extra={
                "eventsWithFullChain": events_with_full_chain,
                "droppedForNoChain": no_chain_drops,
                "backfillSummary": backfill_summary,
            },
        )

    # Entry state inference
    user_spot, user_em_pct, em_source = _infer_user_em_pct(request, engine1.get("current") or {})

    fill_model = FillModel.from_str(
        getattr(flags, "ENGINE15_FILL_MODEL", "nbbo"),
        penalty_pct=float(getattr(flags, "ENGINE15_FILL_PENALTY_PCT", 15.0)),
    )

    paths: List[AnaloguePath] = []
    matched_events_ui: List[Dict[str, Any]] = []
    replay_drops: List[Dict[str, str]] = []
    analogue_entry_credits: List[float] = []
    user_strikes = request.strike_tuple()
    for ctx in contexts:
        res = simulate_event(
            ctx,
            ticker=ticker,
            user_spot=user_spot,
            user_em_pct=user_em_pct,
            user_strikes=user_strikes,
            entry_credit=float(request.credit_received),
            profit_target_pct=float(request.profit_target_pct),
            stop_loss_pct=float(request.stop_loss_pct),
            snap_max_pts=float(flags.ENGINE15_STRIKE_SNAP_MAX_PTS),
            fill_model=fill_model,
            intraday_crush_factor=float(flags.ENGINE15_INTRADAY_CRUSH_FACTOR),
        )
        if res.path is None:
            replay_drops.append({
                "earnDate": ctx.window.earn_date_hist,
                "reason": "; ".join(res.notes) or "replay produced no path",
            })
            continue
        paths.append(res.path)
        if res.analogue_entry_credit is not None and res.analogue_entry_credit > 0:
            analogue_entry_credits.append(float(res.analogue_entry_credit))
        matched_events_ui.append({
            "earnDate": ctx.window.earn_date_hist,
            "entryDateHist": ctx.window.entry_date_hist,
            "plannedExitDateHist": ctx.window.planned_exit_date_hist,
            "expiryHist": ctx.expiry_hist,
            "anncTod": ctx.window.annc_tod,
            "outcome": res.path.outcome,
            "exitDay": res.path.exit_day,
            "pnlPct": round(float(res.path.exit_pnl_pct), 1),
            "mae": round(float(res.path.max_adverse_excursion_pct), 1),
            "breached": bool(res.path.breached),
            "mappedStrikes": {
                "shortPut": res.mapped_strikes[0],
                "longPut": res.mapped_strikes[1],
                "shortCall": res.mapped_strikes[2],
                "longCall": res.mapped_strikes[3],
            },
            "analogueEntryCredit": (
                round(float(res.analogue_entry_credit), 3)
                if res.analogue_entry_credit is not None else None
            ),
            "impliedMovePct": ctx.window.implied_move_pct,
            "realizedMovePct": ctx.window.realized_move_pct,
        })

    if len(paths) < int(flags.ENGINE15_MIN_EVENTS):
        return _empty_payload(
            req=request,
            reason=(
                f"Replay yielded only {len(paths)} priceable events "
                f"(need ≥{flags.ENGINE15_MIN_EVENTS}). Chain cache likely sparse for "
                f"{ticker}; consider running /api/earnings-ic/backfill."
            ),
            engine1=engine1,
            events_considered=events_considered,
            coverage=coverage_after,
            data_quality_extra={
                "eventsWithFullChain": events_with_full_chain,
                "replayDrops": replay_drops,
                "backfillSummary": backfill_summary,
            },
        )

    # --- 6. Aggregate ---------------------------------------------------
    outcome_summary = _summarize_outcomes(paths)
    outcome_ci = _bootstrap_outcome_ci(paths)
    timeline = _build_mtm_timeline(paths)
    final_pnls = [float(p.exit_pnl_pct) for p in paths]
    mean_pnl = statistics.mean(final_pnls)
    median_pnl = statistics.median(final_pnls)
    sd_pnl = statistics.stdev(final_pnls) if len(final_pnls) > 1 else 1.0
    sharpe = (mean_pnl / sd_pnl) if sd_pnl > 1e-9 else 0.0

    from backend.engine15.exit_rules_adapter import optimize_planned_exit_rules
    exit_opt = optimize_planned_exit_rules(
        paths=paths,
        default_profit_target_pct=float(request.profit_target_pct),
        default_stop_loss_pct=float(request.stop_loss_pct),
        planned_hold_days=int(request.planned_hold_biz_days() or 1),
    )

    sizing = compute_sizing(paths)

    # Greeks attribution
    attributions: List[Any] = []
    for p in paths:
        if (p.entry_close is None or p.exit_close is None or p.entry_iv is None
                or p.entry_credit is None or p.years_to_expiry is None):
            continue
        try:
            days_held = max(1, int(p.exit_day) + 1)
            attributions.append(attribute_path(
                entry_date=str(p.entry_date),
                entry_credit=float(p.entry_credit),
                entry_spot=float(p.entry_close),
                exit_spot=float(p.exit_close),
                entry_iv=float(p.entry_iv),
                exit_iv=None,
                days_held=days_held,
                years_to_expiry=float(p.years_to_expiry),
                mapped_strikes=p.mapped_strikes,
                realized_pnl_pct=float(p.exit_pnl_pct),
            ))
        except Exception as e:
            LOG.debug("engine15: greeks attribution failed for %s: %s", p.entry_date, e)
    greeks_attribution = aggregate_attribution(attributions)

    # --- 7. Conditioning ------------------------------------------------
    conditioning: Dict[str, Any] = {}
    adjusted_distribution: Dict[str, Any] = {}
    if getattr(flags, "ENGINE15_ENABLE_CONDITIONING", True):
        try:
            from backend.engine15.conditioning import (
                apply_modifiers_to_distribution,
                compute_earnings_conditioning,
            )
            conditioning = compute_earnings_conditioning(
                request=request,
                engine1=engine1,
                orats_client=client,
                benzinga_client=benzinga_client,
                store=store,
            )
            adjusted_distribution = apply_modifiers_to_distribution(
                base_distribution=outcome_summary,
                net_tail_multiplier=float(conditioning.get("netTailMultiplier", 1.0)),
                net_wr_shift_pct=float(conditioning.get("netWinRateShiftPct", 0.0)),
            )
        except Exception as e:
            LOG.warning("engine15 conditioning failed: %s", e)
            conditioning = {"error": f"{type(e).__name__}: {e}"}
            adjusted_distribution = {}

    conditioning_summary = _summarize_conditioning(
        conditioning=conditioning,
        base=outcome_summary,
        adjusted=adjusted_distribution,
    )

    # --- Notes ----------------------------------------------------------
    notes: List[str] = [
        f"Analogue pool: {len(paths)} same-ticker earnings events "
        f"(anncTod filter={request.earnings_timing}, strict={criteria.strict_annc_tod}).",
        f"Entry spot={user_spot:.2f}, 1σ EM={user_em_pct:.2f}% via {em_source}.",
        f"Fill model: {fill_model.mode}"
        + (f" (+{fill_model.penalty_pct:.0f}% half-spread)" if fill_model.mode == "mid_penalty" else ""),
    ]
    if backfill_summary and not backfill_summary.get("error"):
        notes.append(
            f"On-demand backfill: fetched {backfill_summary.get('succeeded', 0)} "
            f"new chain slices (of {backfill_summary.get('attempted', 0)} attempted)."
        )
    if dropped:
        notes.append(
            f"Dropped {len(dropped)} analogues on gates "
            f"({', '.join(sorted({d['reason'].split(' (')[0] for d in dropped}))})."
        )
    if no_chain_drops:
        notes.append(f"Dropped {len(no_chain_drops)} events lacking a cached expiry at target DTE.")
    if replay_drops:
        notes.append(f"Dropped {len(replay_drops)} events whose chain could not be priced.")
    crush_factor = float(flags.ENGINE15_INTRADAY_CRUSH_FACTOR)
    notes.append(_planned_exit_fidelity_caveat(request, crush_factor))
    for c_note in (conditioning.get("notes") or []):
        if c_note and c_note not in notes:
            notes.append(c_note)

    data_quality = {
        "chainCoverage": coverage_after,
        "coverageBefore": coverage_before,
        "eventsConsidered": int(events_considered),
        "eventsWithFullChain": int(events_with_full_chain),
        "pathsPriced": int(len(paths)),
        "minEventsMet": bool(len(paths) >= int(flags.ENGINE15_MIN_EVENTS)),
        "droppedForNoChain": no_chain_drops,
        "replayDrops": replay_drops,
        "backfillSummary": backfill_summary,
    }

    # Credit richness: compare the user's forward credit to the mean
    # entry credit observed across the analogue pool. When the delta is
    # large the desk should be warned (pre-market wide spreads, stale IV,
    # or a placement mismatch).
    credit_richness: Optional[Dict[str, Any]] = None
    if analogue_entry_credits:
        import statistics as _st
        a_mean = float(_st.mean(analogue_entry_credits))
        a_median = float(_st.median(analogue_entry_credits))
        user_credit = float(request.credit_received)
        delta_pct = (
            ((user_credit - a_mean) / a_mean) * 100.0 if a_mean > 1e-6 else 0.0
        )
        # Richness verdict
        if delta_pct >= 15.0:
            verdict = "user_rich"
            note = (
                f"User credit ${user_credit:.2f} is {delta_pct:+.0f}% vs historical "
                f"analogue mean ${a_mean:.2f} — you're getting paid richly (verify the "
                "fill is realistic; pre-market NBBO can be misleading)."
            )
        elif delta_pct <= -15.0:
            verdict = "user_cheap"
            note = (
                f"User credit ${user_credit:.2f} is {delta_pct:+.0f}% vs historical "
                f"analogue mean ${a_mean:.2f} — you're selling cheap; waiting for the "
                "open to tighten the NBBO or moving strikes closer may raise premium."
            )
        else:
            verdict = "user_fair"
            note = (
                f"User credit ${user_credit:.2f} is {delta_pct:+.0f}% vs historical "
                f"analogue mean ${a_mean:.2f} — in-line with typical placement."
            )
        credit_richness = {
            "userCredit": round(user_credit, 3),
            "analogueMean": round(a_mean, 3),
            "analogueMedian": round(a_median, 3),
            "deltaPct": round(float(delta_pct), 1),
            "verdict": verdict,
            "note": note,
            "n": int(len(analogue_entry_credits)),
        }
        if note and note not in notes:
            notes.append(f"creditRichness: {note}")

    return {
        "engine": 15,
        "version": __version__,
        "request": asdict(request),
        "eventsUsed": int(len(paths)),
        "eventsConsidered": int(events_considered),
        "entryState": {
            "userSpot": round(float(user_spot), 2),
            "userEmPct": round(float(user_em_pct), 3),
            "wingWidth": round(float(request.wing_width()), 2),
            "userEmSource": em_source,
        },
        "plannedExit": {
            "plannedExitDate": request.planned_exit_date,
            "plannedExitOffsetHours": float(request.planned_exit_offset_hours),
            "holdBizDays": int(request.planned_hold_biz_days()),
            "intradayCrushFactor": float(crush_factor),
            "fidelityCaveat": _planned_exit_fidelity_caveat(request, crush_factor),
        },
        "fillModel": {
            "mode": fill_model.mode,
            "penaltyPct": float(fill_model.penalty_pct),
        },
        "creditRichness": credit_richness,
        "outcomeDistribution": outcome_summary,
        "outcomeDistributionCI": outcome_ci,
        "adjustedOutcomeDistribution": adjusted_distribution,
        "conditioningModifiers": conditioning,
        "conditioningSummary": conditioning_summary,
        "mtmTimeline": timeline,
        "expectedValue": {
            "meanPnlPct": round(float(mean_pnl), 1),
            "medianPnlPct": round(float(median_pnl), 1),
            "sharpeProxy": round(float(sharpe), 2),
        },
        "exitRulesOptimization": exit_opt,
        "sizing": sizing,
        "greeksAttribution": greeks_attribution,
        "matchedEvents": matched_events_ui,
        "droppedEvents": dropped + no_chain_drops + replay_drops,
        "engine1Summary": _summarize_engine1(engine1),
        "engine1": engine1 if request.include_e1_payload else None,
        "dataQuality": data_quality,
        "notes": notes,
    }
