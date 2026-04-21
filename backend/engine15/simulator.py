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
        """Business-day gap from entry to planned exit (>=0).

        v2: defers to :mod:`backend.engine15.event_universe` which in turn
        uses the NYSE holiday calendar when the flag is on.
        """
        try:
            a = dt.date.fromisoformat(self.entry_date)
            b = dt.date.fromisoformat(self.planned_exit_date)
        except Exception:
            return 1
        if b <= a:
            return 0
        try:
            from backend.engine15.event_universe import biz_diff
            return max(0, biz_diff(a, b))
        except Exception:
            pass
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
    event_date: Optional[str] = None,
    event_timing: Optional[str] = None,
    trade_builder_inputs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Thin forwarder around :func:`backend.engine1.get_or_compute_breach_stats`.

    E15 v2 uses the shared cross-engine cache so an E1 scan followed by
    an E15 scenario on the same ticker + event (the primary desk flow)
    only pays ORATS cost once. ``event_date`` / ``event_timing`` /
    ``trade_builder_inputs`` participate in the cache key so overrides
    or alternate wing-console placements correctly bust the slot.
    """
    from backend.engine1 import get_or_compute_breach_stats
    return get_or_compute_breach_stats(
        ticker=ticker, n=int(n), years=int(years), k=1.0,
        event_date=event_date, event_timing=event_timing,
        trade_builder_inputs=trade_builder_inputs,
        client=client, benzinga_client=benzinga_client, flags=flags,
    )


def _resolve_exit_iv(
    *,
    ticker: str,
    path: Any,
    universe: Any,
) -> Optional[float]:
    """Look up ATM IV on an analogue path's exit trade date.

    Used by greeks attribution so the vega term actually fires. Returns
    a decimal IV (e.g. 0.28) or ``None`` when the chain cache has no
    usable entry for the exit trade-date/expiry pair. Never raises.
    """
    try:
        from backend.engine14 import chain_cache
        from backend.engine15.chain_replay_adapter import _atm_iv_from_chain
    except Exception:
        return None

    try:
        entry_d = dt.date.fromisoformat(str(path.entry_date))
    except Exception:
        return None
    exit_day_idx = int(getattr(path, "exit_day", 0) or 0)

    # Use the holiday-aware calendar if available.
    try:
        from backend.engine15.trading_calendar import add_business_days
        exit_dt = add_business_days(entry_d, exit_day_idx)
    except Exception:
        cur = entry_d
        step = exit_day_idx
        while step > 0:
            cur = cur + dt.timedelta(days=1)
            if cur.weekday() <= 4:
                step -= 1
        exit_dt = cur

    try:
        rows = chain_cache.fetch_chain_slice(
            ticker=str(ticker).upper(),
            trade_date=exit_dt.isoformat(),
            expiry=str(path.expiry_date),
        ) or []
    except Exception:
        return None

    if not rows:
        return None
    try:
        spot = float(path.exit_close or path.entry_close or 0.0)
        if spot <= 0:
            return None
        iv = _atm_iv_from_chain(rows, spot)
        if iv is None:
            return None
        iv_f = float(iv)
        if not (0.0 < iv_f < 5.0):
            return None
        return iv_f
    except Exception:
        return None


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
    em_breach = e1.get("emBreachSummary") or {}
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
        # deskConsensus intentionally omitted: E15 runs assume the desk has
        # already committed to the trade, so E1's GO/LEAN_PASS/PASS verdict
        # is suppressed to keep the advisor focused on fidelity rather than
        # re-litigating the entry decision. Raw numerics (vrpScore,
        # ivElevation, emBreach*) remain the analytical substrate.
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
        # --- Next event ---
        # Authoritative earnings date + AMC/BMO timing come from the desk's
        # EarningsIcRequest (scenario.request.earningsDate / earningsTiming)
        # and are NOT re-echoed from E1's nextEvent here. Historically those
        # E1 fields diverged from what the desk entered (stale ORATS / BZ
        # calendars) and confused the advisor; the request is now sole truth.
        # --- Regime / event risk chips ---
        "regimeLabel": regime.get("label"),
        "regimeTailMultiplier": regime.get("tailMultiplier"),
        # v2: expose the full MI v2 HMM reading (probabilities, vol_state,
        # source) so the Entry State card can render the regime chip + a
        # probability band rather than the scalar label alone.
        "regimeMiV2": (regime.get("mi_v2") or None),
        "eventRiskLabel": event_risk.get("label"),
        # --- Breach summary (supports both float-map and legacy dict shape) ---
        "emBreachRate1xPct": breach_rate_1x if breach_rate_1x is not None else breach_rate_fallback,
        "emBreachRate15xPct": breach_rate_15x,
        "emBreachRate2xPct": breach_rate_2x,
        "emBreachN": summary.get("events_used"),
        # --- E1 v2 cross-validation anchors ---
        # Exposed here so the E15 advisor + the Command Deck's "cross-check"
        # badge can confirm / diverge from E1's MAE pool.
        "e1WingMAE": (e1.get("e1WingMAE") or None),
        "nextEventOverrideSource": ((e1.get("nextEvent") or {}).get("override_source") or None),
        # Back-compat fields (older callers).
        "emBreachPct": em_breach.get("breachRatePct") or em_breach.get("breachPct") if isinstance(em_breach, dict) else None,
    }


def _wing_console_mini(request: "EarningsIcRequest") -> Optional[Dict[str, Any]]:
    """Build a compact top-3 Wing Console slice for the Command Deck.

    Reads :func:`backend.engine1.get_scoring_context` for the same
    ticker + event + timing; if cached, re-scores the grid once and
    returns the top 3 placements + key metadata so the E15 frontend
    can render a cross-nav card without a separate fetch.

    Returns ``None`` when the Wing Console context cache is cold —
    not an error, just "nothing to show here yet."
    """
    try:
        from backend.engine1 import (
            DEFAULT_WEIGHTS, get_scoring_context, score_placements,
        )
    except Exception:
        return None
    ctx = get_scoring_context(
        request.ticker, request.earnings_date, request.earnings_timing,
    )
    if ctx is None:
        return None
    try:
        placements, _theta = score_placements(
            ticker=request.ticker, spot=float(ctx.spot),
            implied_move_pct=float(ctx.implied_move_pct),
            events=list(ctx.events or []),
            mae=ctx.mae,
            weights=ctx.weights or DEFAULT_WEIGHTS,
            median_credit_pts=ctx.median_credit_pts,
        )
    except Exception:
        return None
    if not placements:
        return None
    top = placements[:3]
    return {
        "ticker":       request.ticker,
        "event_date":   request.earnings_date,
        "event_timing": request.earnings_timing,
        "placements": [
            {
                "rank":              i,
                "em_mult":           float(p.em_mult),
                "wing_pts":          float(p.wing_pts),
                "short_put_strike":  p.short_put_strike,
                "short_call_strike": p.short_call_strike,
                "long_put_strike":   p.long_put_strike,
                "long_call_strike":  p.long_call_strike,
                "credit_est":        float(p.credit_est or 0.0),
                "credit_dollars":    float(p.credit_dollars or 0.0),
                "composite_score":   float(p.composite_score or 0.0),
                "breach_gap_prob":   float(p.breach_gap_prob or 0.0),
                "theta_capture_pct": float(p.theta_capture_pct or 0.0),
                "confidence":        str(p.confidence or ""),
            }
            for i, p in enumerate(top)
        ],
        "grid_size":     int(len(placements)),
        "context_age_s": 0,  # TTL is short; treat cached context as fresh
    }


def _compute_e1_wing_mae_crosscheck(
    engine1: Dict[str, Any],
    outcome_summary: Dict[str, Any],
) -> Dict[str, Any]:
    """Cross-validate E1's MAE p95 pool against E15's replay outcomes.

    E1's ``e1WingMAE.p95`` is the 95th-percentile max adverse excursion
    (in % move) across the last N earnings events. E15's
    ``whiteKnuckle + breach`` outcome pcts measure how often the desk's
    chosen placement was forced into a scary/losing exit on the same
    pool.

    Both pools tell the same story when they're correlated. When they
    diverge materially (>= 20 percentage points of white_knuckle+breach
    vs a naive MAE-implied threshold hit), something is off — usually a
    stale chain cache, a bad EM override, or a placement that E1 hasn't
    re-scored against the desk's chosen strikes.

    Returns ``{ e1_mae_p95_pct, e15_white_knuckle_pct, e15_breach_pct,
    divergence, note, source }`` — ``divergence`` is the absolute
    percentage-point gap normalised to [0, 1] so the UI can render a
    "match / diverge" badge.
    """
    out: Dict[str, Any] = {
        "e1_mae_p95_pct":        None,
        "e15_white_knuckle_pct": None,
        "e15_breach_pct":        None,
        "divergence":            None,
        "note":                  "",
        "source":                "unavailable",
    }

    mae = (engine1 or {}).get("e1WingMAE") or {}
    try:
        p95 = mae.get("p95")
        if p95 is not None:
            out["e1_mae_p95_pct"] = round(float(p95), 2)
    except Exception:
        out["e1_mae_p95_pct"] = None

    wk = ((outcome_summary or {}).get("whiteKnuckle") or {}).get("pct")
    br = ((outcome_summary or {}).get("breach") or {}).get("pct")
    try:
        if wk is not None:
            out["e15_white_knuckle_pct"] = round(float(wk), 2)
        if br is not None:
            out["e15_breach_pct"] = round(float(br), 2)
    except Exception:
        pass

    if out["e1_mae_p95_pct"] is None or out["e15_white_knuckle_pct"] is None:
        out["source"] = "missing_inputs"
        out["note"] = "Cross-check skipped: E1 MAE or E15 outcome buckets missing."
        return out

    # Convergence heuristic: if E1's MAE p95 is low (< 8% move) the
    # desk should see a low whiteKnuckle+breach pct (< 25%). If E1
    # MAE p95 is high (>= 15%), we expect whiteKnuckle+breach >= 35%.
    # Score the magnitude of the mismatch on a 0..1 scale.
    combined_risk = float(out["e15_white_knuckle_pct"]) + float(out["e15_breach_pct"] or 0.0)
    mae_level = float(out["e1_mae_p95_pct"])
    # Rough expectation band: whiteKnuckle+breach ~ 2.0 * MAE p95
    # (empirically calibrated; a 10% MAE p95 typically lines up with a
    # ~20% combined panic-exit rate in the replay pool).
    expected = max(0.0, min(80.0, 2.0 * mae_level))
    raw_gap = abs(combined_risk - expected)
    # Divergence: 0 when gap is 0, 1 when gap >= 40 percentage points.
    divergence = max(0.0, min(1.0, raw_gap / 40.0))
    out["divergence"] = round(float(divergence), 3)

    if divergence < 0.25:
        out["source"] = "convergent"
        out["note"] = (
            f"E1 MAE p95 ({mae_level:.1f}%) and E15 whiteKnuckle+breach "
            f"({combined_risk:.1f}%) agree — both pools paint the same tail."
        )
    elif divergence < 0.5:
        out["source"] = "mild_divergence"
        out["note"] = (
            f"E1 MAE p95 ({mae_level:.1f}%) and E15 whiteKnuckle+breach "
            f"({combined_risk:.1f}%) differ modestly; check event-pool "
            "overlap and strike mapping."
        )
    else:
        out["source"] = "divergent"
        out["note"] = (
            f"E1 MAE p95 ({mae_level:.1f}%) and E15 whiteKnuckle+breach "
            f"({combined_risk:.1f}%) diverge materially; likely chain-cache "
            "gap or stale event pool. Re-scan before acting on either."
        )
    return out


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
    # Feed the scenario's strikes into trade_builder_inputs so E1's credit
    # estimator shapes its output around the desk's chosen placement.
    _tb_inputs: Dict[str, Any] = {
        "symmetry":   "symmetric",
        "wing_width": float(request.wing_width() or 0) or None,
        "exp":        str(request.expiry or "") or None,
    }
    # Drop None entries so the cache key stays stable across requests
    # that omit optional trade-builder fields.
    _tb_inputs = {k: v for k, v in _tb_inputs.items() if v is not None}

    try:
        engine1 = _run_engine1(
            ticker=ticker,
            n=int(request.n_history),
            years=int(request.years_history),
            client=client,
            benzinga_client=benzinga_client,
            flags=flags,
            event_date=request.earnings_date,
            event_timing=request.earnings_timing,
            trade_builder_inputs=(_tb_inputs or None),
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

    # v2: intraday crush mode. In "empirical" mode we run the replay with
    # no crush blend (factor=1.0) so the pool reflects raw close-to-close
    # PnL, then compute a per-ticker crush factor from the resulting paths
    # and apply it post-hoc. In "fixed" mode we stay on the hard-coded
    # ENGINE15_INTRADAY_CRUSH_FACTOR (legacy behaviour).
    _crush_mode = str(getattr(flags, "ENGINE15_INTRADAY_CRUSH_MODE", "empirical") or "fixed").strip().lower()
    _crush_fixed = float(flags.ENGINE15_INTRADAY_CRUSH_FACTOR)
    _replay_crush = 1.0 if _crush_mode == "empirical" else _crush_fixed

    paths: List[AnaloguePath] = []
    matched_events_ui: List[Dict[str, Any]] = []
    replay_drops: List[Dict[str, str]] = []
    analogue_entry_credits: List[float] = []
    user_strikes = request.strike_tuple()

    # v2: replay events in parallel. Each simulate_event is mostly
    # I/O-bound (chain_cache.fetch_chain_slice over SQLite). max_workers=5
    # is a sensible cap — SQLite serialises at the DB layer, so adding
    # more workers mostly queues. Determinism preserved by storing results
    # keyed by context index and reassembling in order.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _simulate_one(idx: int, ctx: Any) -> Tuple[int, Any]:
        return idx, simulate_event(
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
            intraday_crush_factor=_replay_crush,
        )

    results_by_idx: Dict[int, Any] = {}
    with ThreadPoolExecutor(max_workers=min(5, max(1, len(contexts)))) as pool:
        futures = [pool.submit(_simulate_one, i, ctx) for i, ctx in enumerate(contexts)]
        for fut in as_completed(futures):
            try:
                idx, res = fut.result()
                results_by_idx[idx] = res
            except Exception as err:
                LOG.warning("engine15: event simulate future failed: %s", err)

    # Reassemble in deterministic (input) order so downstream caching
    # and MTM timeline stays stable across runs.
    for i, ctx in enumerate(contexts):
        res = results_by_idx.get(i)
        if res is None:
            replay_drops.append({
                "earnDate": ctx.window.earn_date_hist,
                "reason": "simulate_event worker failed",
            })
            continue
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

    # --- 6a. Empirical intraday crush (if mode=empirical) ----------------
    # Compute the per-ticker crush factor from the raw close-to-close paths
    # and apply it post-hoc to every path's planned-exit PnL (only paths
    # that ran to time-stop — early-exit paths already captured PT/SL).
    crush_reading_dict: Dict[str, Any] = {}
    if _crush_mode == "empirical":
        try:
            from backend.engine15.intraday_crush import compute_crush_factor
            _reading = compute_crush_factor(
                paths=paths, fallback=_crush_fixed, min_sample=3,
            )
            crush_reading_dict = _reading.to_dict()
            _emp_factor = float(_reading.factor)
            if _reading.source == "empirical":
                # Apply crush to each path's exit PnL when it ran to the
                # time-stop boundary (exit_day == last day of daily_pnl_pct).
                _applied = 0
                for p in paths:
                    try:
                        daily = list(getattr(p, "daily_pnl_pct", []) or [])
                        if not daily or getattr(p, "exit_day", None) is None:
                            continue
                        # Only re-blend time-stop exits; early PT/SL exits stay as-is.
                        if int(p.exit_day) != len(daily) - 1:
                            continue
                        entry_pnl = float(daily[0][1])
                        close_pnl = float(daily[-1][1])
                        new_pnl = entry_pnl + _emp_factor * (close_pnl - entry_pnl)
                        # Attempt in-place update on the dataclass (frozen=False
                        # in engine14.AnaloguePath; if frozen, catch + skip).
                        try:
                            p.exit_pnl_pct = float(new_pnl)
                            _applied += 1
                        except Exception:
                            pass
                    except Exception:
                        continue
                if _applied > 0:
                    crush_reading_dict["paths_adjusted"] = int(_applied)
        except Exception as e:
            LOG.debug("engine15: empirical crush computation failed: %s", e)
            crush_reading_dict = {
                "factor": _crush_fixed, "n_events": 0, "source": "fixed",
                "fallback_reason": f"{type(e).__name__}: {e}",
            }
    else:
        crush_reading_dict = {
            "factor": _crush_fixed, "n_events": 0, "source": "fixed",
            "fallback_reason": "ENGINE15_INTRADAY_CRUSH_MODE=fixed",
        }

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
            # v2: resolve exit_iv from the cached chain at the actual exit
            # trade date so greeks attribution captures the IV crush (vega)
            # component. Falls back to None (legacy behaviour) when the
            # chain slice for that date has no usable ATM IV.
            exit_iv = _resolve_exit_iv(
                ticker=ticker,
                path=p,
                universe=universe,
            )
            attributions.append(attribute_path(
                entry_date=str(p.entry_date),
                entry_credit=float(p.entry_credit),
                entry_spot=float(p.entry_close),
                exit_spot=float(p.exit_close),
                entry_iv=float(p.entry_iv),
                exit_iv=exit_iv,
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

    response: Dict[str, Any] = {
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
            # crush_factor here reflects what was used for the per-event
            # loop (1.0 in empirical mode, the fixed config default in
            # fixed mode). The actual applied crush is in crushReading.
            "intradayCrushFactor": float(crush_reading_dict.get("factor") or crush_factor),
            "fidelityCaveat": _planned_exit_fidelity_caveat(
                request, float(crush_reading_dict.get("factor") or crush_factor),
            ),
        },
        "crushReading": crush_reading_dict,
        "fillModel": {
            "mode": fill_model.mode,
            "penaltyPct": float(fill_model.penalty_pct),
        },
        "creditRichness": credit_richness,
        "outcomeDistribution": outcome_summary,
        "outcomeDistributionCI": outcome_ci,
        # v2: cross-validation badge against E1's Wing Console MAE pool.
        "e1WingMAECrossCheck": _compute_e1_wing_mae_crosscheck(engine1, outcome_summary),
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
        # v2: mini grid of the E1 Wing Console top placements for the
        # same ticker + event so the Command Deck can render a cross-nav
        # card without a separate fetch. ``None`` when the ScoringContext
        # cache is cold (fresh page load on E15 without prior E1 pass).
        "wingConsoleMini": _wing_console_mini(request),
        "engine1": engine1 if request.include_e1_payload else None,
        "dataQuality": data_quality,
        "notes": notes,
    }
    # v2: strip legacy desk-consensus verdict fields from the riding E1
    # payload when the E15 flag is off (parity with E1_EMIT_DESK_CONSENSUS).
    # Keeps the response surface clean of TRADE / LEAN_PASS / PASS / FADE
    # strings when the primary output is the replay + cross-check.
    if not bool(getattr(flags, "E15_EMIT_DESK_CONSENSUS", False)):
        e1 = response.get("engine1") if isinstance(response.get("engine1"), dict) else None
        if e1 is not None:
            for _k in ("deskConsensus", "emPreference",
                       "e1DeskConsensus", "e1EmPreference"):
                e1.pop(_k, None)
    return response
