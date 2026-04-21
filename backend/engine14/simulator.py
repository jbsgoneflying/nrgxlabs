"""Engine 14 — IC scenario simulator.

Given a user's prospective short iron condor, this module:

  1) Enumerates historical analogue weekly windows (regime-matched).
  2) Maps the user's four strikes into each analogue's strike space
     preserving EM-distance.
  3) Replays the position day-by-day using cached ORATS chains,
     producing a MTM time series per analogue.
  4) Classifies each analogue outcome and aggregates a distribution
     + MTM percentile timeline + optimal exit rule.

The payload shape matches the plan-doc:
  analoguesUsed, outcomeDistribution, mtmTimeline, expectedValue,
  exitRulesOptimization, conditioningNotes, matchedAnalogues.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import random
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.config import FeatureFlags, get_flags
from backend.engine14 import chain_cache
from backend.engine14.analogue_matcher import (
    AnalogueWindow,
    MatchCriteria,
    REGIME_BUCKETS,
    build_analogue_universe,
    filter_analogues,
    map_user_strikes_to_analogue,
    user_em_multiple,
)
from backend.engine14.chain_replay import FillModel, expiry_payoff, reprice_ic
from backend.engine14.conditioning import apply_modifiers_to_distribution, compute_conditioning
from backend.engine14.exit_rules import optimize_exit_rules
from backend.engine14.greeks import aggregate_attribution, attribute_path
from backend.engine14.sizing import compute_sizing
from backend.spx_ic.ohlc import DailyOHLC, fetch_dailies_ohlc_range, iv_to_em1sigma_pct

LOG = logging.getLogger("engine14.simulator")

OUTCOMES = ("earlyTarget", "fullCollect", "whiteKnuckle", "stopOut", "breach")


# ---- Request / response schemas ----

@dataclass(frozen=True)
class IcScenarioRequest:
    underlying: str
    entry_date: str
    expiry: str
    short_put: float
    long_put: float
    short_call: float
    long_call: float
    credit_received: float
    profit_target_pct: float = 50.0
    stop_loss_pct: float = 200.0
    season_mode: str = "none"            # none|quarter|month|summer|opex
    season_value: Optional[str] = None

    def dte_calendar(self) -> int:
        try:
            a = dt.date.fromisoformat(self.entry_date)
            b = dt.date.fromisoformat(self.expiry)
            return max(1, (b - a).days)
        except Exception:
            return 7

    def strike_tuple(self) -> Tuple[float, float, float, float]:
        return (float(self.short_put), float(self.long_put),
                float(self.short_call), float(self.long_call))

    def wing_width(self) -> float:
        put_w = abs(float(self.short_put) - float(self.long_put))
        call_w = abs(float(self.long_call) - float(self.short_call))
        return float(min(put_w, call_w)) if (put_w > 0 and call_w > 0) else float(max(put_w, call_w))


# ---- Per-analogue simulation ----

@dataclass
class AnaloguePath:
    entry_date: str
    expiry_date: str
    dte_sessions: int
    mapped_strikes: Tuple[float, float, float, float]
    daily_pnl_pct: List[Tuple[int, float]]  # (dte_remaining, pnl_pct_of_credit) from entry→expiry
    outcome: str
    exit_day: int
    exit_pnl_pct: float
    max_adverse_excursion_pct: float
    breached: bool
    notes: List[str] = field(default_factory=list)
    # Phase A — when an NBBO run also runs a legacy-mid replay for the
    # same path, we record the mid-mode exit P&L alongside for honesty.
    exit_pnl_pct_mid: Optional[float] = None
    # Phase A2 — MAE enhanced with an OHLC-range proxy (EOD values only
    # miss intraday wicks; this approximates the worst-case IC MTM using
    # the day the underlying traveled farthest against the short strikes).
    mae_proxy_pct: Optional[float] = None
    mae_source: str = "eod"   # "eod" | "ohlc_proxy"
    # Phase E3 — spot / IV anchors used for greeks-based P&L attribution.
    entry_close: Optional[float] = None
    exit_close: Optional[float] = None
    entry_iv: Optional[float] = None     # decimal (e.g. 0.18)
    entry_credit: Optional[float] = None
    years_to_expiry: Optional[float] = None


def _daily_chain_for(ticker: str, trade_date: str, expiry: str):
    return chain_cache.fetch_chain_slice(ticker=ticker, trade_date=trade_date, expiry=expiry)


def _is_scary_mae(mae_pct: float, stop_loss_pct: float) -> bool:
    """Classify a max-adverse-excursion as 'scary' for whiteKnuckle purposes.

    Scary means the trade's MAE reached at least 50% of the stop-loss
    distance, with an absolute floor at -50% of credit so the classifier
    stays meaningful when stop_loss_pct is very small or zero.

    Example: stop_loss_pct=200 -> threshold=-100% (half the stop);
             stop_loss_pct=50  -> threshold=-50%  (floor hits);
             stop_loss_pct=0   -> threshold=-50%  (floor hits).
    """
    scary_threshold = -max(50.0, 0.5 * float(stop_loss_pct))
    return float(mae_pct) <= scary_threshold


def _should_reclassify_as_white_knuckle(
    *,
    current_outcome: str,
    effective_mae_at_exit: float,
    stop_loss_pct: float,
) -> bool:
    """Return True if a winning analogue should be reclassified as whiteKnuckle.

    An analogue is a whiteKnuckle win when it exited profitably (either via
    the profit target rule or by rolling to expiry positive) *and* its
    intraday/EOD MAE during the trade reached stop territory per
    :func:`_is_scary_mae`. Losing outcomes (stopOut/breach) are never
    reclassified.
    """
    if current_outcome not in ("earlyTarget", "fullCollect"):
        return False
    return _is_scary_mae(float(effective_mae_at_exit), float(stop_loss_pct))


def _ohlc_mae_proxy_pct(
    *,
    ohlc_by_date: Dict[str, DailyOHLC],
    trade_days: List[str],
    mapped_strikes: Tuple[float, float, float, float],
    entry_credit: float,
    entry_eod_mae: float,
) -> float:
    """Best-effort intraday MAE using daily high/low of the underlying.

    We don't have intraday option-chain bars, but we DO have the daily
    OHLC of the underlying. For each replay day, compute the worst-case
    IC intrinsic P&L if the underlying had expired at that day's low
    (put-side scare) or high (call-side scare) — whichever was worse —
    and take the min across the window.

    This OVERstates MAE slightly (treats each day as expiry) but it's a
    conservative floor that surfaces "this day saw a real scare" better
    than EOD-only. We return the more-adverse of (EOD MAE, OHLC proxy).
    """
    sp_k, lp_k, sc_k, lc_k = mapped_strikes
    worst = float(entry_eod_mae)
    for td in trade_days:
        bar = ohlc_by_date.get(td)
        if bar is None:
            continue
        candidates: List[float] = []
        if bar.low is not None:
            candidates.append(float(bar.low))
        if bar.high is not None:
            candidates.append(float(bar.high))
        for px in candidates:
            pnl_val = expiry_payoff(
                expiry_spot=px,
                short_put_strike=sp_k, long_put_strike=lp_k,
                short_call_strike=sc_k, long_call_strike=lc_k,
                entry_credit=float(entry_credit),
            )
            pct = 100.0 * pnl_val / float(entry_credit) if float(entry_credit) > 0 else 0.0
            if pct < worst:
                worst = float(pct)
    return float(worst)


def _simulate_single_analogue(
    *,
    ticker: str,
    window: AnalogueWindow,
    user_strikes: Tuple[float, float, float, float],
    user_spot: float,
    user_em_pct: float,
    entry_credit: float,
    profit_target_pct: float,
    stop_loss_pct: float,
    closes_by_date: Dict[str, float],
    snap_max_pts: float,
    fill_model: Optional[FillModel] = None,
    also_price_mid: bool = False,
    ohlc_by_date: Optional[Dict[str, DailyOHLC]] = None,
    mae_proxy_enabled: bool = True,
) -> Optional[AnaloguePath]:
    try:
        mapped = map_user_strikes_to_analogue(
            user_spot=float(user_spot),
            user_em_pct=float(user_em_pct),
            analogue_spot=float(window.entry_close),
            analogue_em_pct=float(window.entry_em_pct),
            user_strikes=user_strikes,
        )
    except Exception as e:
        LOG.debug("strike mapping failed for %s: %s", window.entry_date, e)
        return None

    sp_k, lp_k, sc_k, lc_k = mapped
    # Sanity: puts should be below spot, calls above.
    if not (lp_k <= sp_k < window.entry_close < sc_k <= lc_k):
        # Don't reject outright — user may pass unusual strikes — but note it.
        pass

    # Enumerate trade-dates in [entry, expiry].
    try:
        e_date = dt.date.fromisoformat(window.entry_date)
        x_date = dt.date.fromisoformat(window.expiry_date)
    except Exception:
        return None

    trade_days: List[str] = []
    d = e_date
    while d <= x_date:
        ds = d.isoformat()
        if ds in closes_by_date:
            trade_days.append(ds)
        d += dt.timedelta(days=1)
    if len(trade_days) < 2:
        return None

    daily: List[Tuple[int, float]] = []
    daily_mid: List[Tuple[int, float]] = []
    mae = 0.0
    mae_at_exit: Optional[float] = None  # snapshot of mae when exit rule fires
    exit_day: Optional[int] = None
    exit_pnl: Optional[float] = None
    exit_pnl_mid: Optional[float] = None
    outcome: Optional[str] = None
    notes: List[str] = []
    fm = fill_model or FillModel()
    mid_fm = FillModel(mode="mid") if also_price_mid else None

    for i, td in enumerate(trade_days):
        dte_remaining = len(trade_days) - 1 - i
        chain = _daily_chain_for(ticker, td, window.expiry_date)
        pnl_pct: Optional[float] = None
        pnl_pct_mid: Optional[float] = None

        if chain:
            priced = reprice_ic(
                chain=chain,
                short_put_strike=sp_k,
                long_put_strike=lp_k,
                short_call_strike=sc_k,
                long_call_strike=lc_k,
                entry_credit=float(entry_credit),
                snap_max_pts=float(snap_max_pts),
                fill_model=fm,
            )
            if priced is not None:
                pnl_pct = float(priced.pnl_pct_of_credit)
            if mid_fm is not None:
                priced_mid = reprice_ic(
                    chain=chain,
                    short_put_strike=sp_k,
                    long_put_strike=lp_k,
                    short_call_strike=sc_k,
                    long_call_strike=lc_k,
                    entry_credit=float(entry_credit),
                    snap_max_pts=float(snap_max_pts),
                    fill_model=mid_fm,
                )
                if priced_mid is not None:
                    pnl_pct_mid = float(priced_mid.pnl_pct_of_credit)

        # Fallback: terminal-day intrinsic payoff when chain missing at T=0.
        if pnl_pct is None and i == len(trade_days) - 1:
            spot = closes_by_date.get(td)
            if spot is not None:
                pnl_val = expiry_payoff(
                    expiry_spot=float(spot),
                    short_put_strike=sp_k,
                    long_put_strike=lp_k,
                    short_call_strike=sc_k,
                    long_call_strike=lc_k,
                    entry_credit=float(entry_credit),
                )
                pnl_pct = 100.0 * pnl_val / float(entry_credit)
                if mid_fm is not None:
                    pnl_pct_mid = pnl_pct
                notes.append("expiry pnl computed from intrinsic payoff (chain missing)")

        if pnl_pct is None:
            # Skip days we can't price — don't abort the whole path.
            continue

        daily.append((int(dte_remaining), float(pnl_pct)))
        if pnl_pct_mid is not None:
            daily_mid.append((int(dte_remaining), float(pnl_pct_mid)))
        if pnl_pct < mae:
            mae = float(pnl_pct)

        if exit_day is None:
            # Check exit rules on EOD mark (primary fill-model P&L).
            if pnl_pct >= float(profit_target_pct):
                exit_day = i
                exit_pnl = pnl_pct
                exit_pnl_mid = pnl_pct_mid
                mae_at_exit = float(mae)
                outcome = "earlyTarget"
            elif pnl_pct <= -float(stop_loss_pct):
                exit_day = i
                exit_pnl = pnl_pct
                exit_pnl_mid = pnl_pct_mid
                mae_at_exit = float(mae)
                outcome = "stopOut"

    if not daily:
        return None

    # If never exited, close at expiry-day mark.
    if exit_day is None:
        final = daily[-1][1]
        exit_day = len(daily) - 1
        exit_pnl = float(final)
        if daily_mid:
            exit_pnl_mid = daily_mid[-1][1]
        mae_at_exit = float(mae)
        if final >= 95.0:
            outcome = "fullCollect"
        elif final < -float(stop_loss_pct):
            # Gapped past stop on final day.
            outcome = "stopOut"
        elif final < 0.0:
            # Held to expiry and ended negative without the stop rule
            # firing. Functionally a loss — group with stopOut rather than
            # whiteKnuckle. whiteKnuckle is now reserved for "won despite a
            # scary drawdown" (see reclassification below).
            outcome = "stopOut"
            notes.append("held to expiry and ended negative below zero (stop rule not hit)")
        else:
            outcome = "fullCollect"  # >0 but <95% -> still "kept" the trade
            notes.append("partial credit kept (final pnl < 95% but positive)")

    # Detect breach (expiry close beyond short strike).
    breached = False
    last_td = trade_days[-1]
    last_close = closes_by_date.get(last_td)
    if last_close is not None:
        if last_close < sp_k or last_close > sc_k:
            breached = True

    # Escalate outcome to "breach" if expiry breached AND realized pnl was strongly negative.
    if breached and (exit_pnl is not None and exit_pnl <= -50.0):
        outcome = "breach"

    # Phase A2 — compute OHLC-based intraday MAE proxy over the *active*
    # trade window (entry through exit), not the full replay tail. This
    # keeps the MAE consistent with what the trader actually experienced
    # and avoids contaminating whiteKnuckle classification with post-exit
    # drawdown that the trader had already banked P&L on.
    active_days = trade_days[: int(exit_day) + 1] if exit_day is not None else trade_days
    mae_proxy: Optional[float] = None
    mae_source = "eod"
    if mae_proxy_enabled and ohlc_by_date:
        try:
            mae_proxy = _ohlc_mae_proxy_pct(
                ohlc_by_date=ohlc_by_date,
                trade_days=active_days,
                mapped_strikes=mapped,
                entry_credit=float(entry_credit),
                entry_eod_mae=float(mae_at_exit if mae_at_exit is not None else mae),
            )
            if mae_proxy < float(mae_at_exit if mae_at_exit is not None else mae):
                mae_source = "ohlc_proxy"
        except Exception as e:
            LOG.debug("mae proxy failed for %s: %s", window.entry_date, e)
            mae_proxy = None

    # --- whiteKnuckle reclassification (path-aware "scary-but-won") --------
    # A win whose intraday/EOD drawdown reached stop territory is more
    # informative to the desk than the same number buried in earlyTarget /
    # fullCollect. See :func:`_is_scary_mae` for the threshold.
    effective_mae_at_exit = float(mae_at_exit if mae_at_exit is not None else mae)
    if mae_proxy is not None and float(mae_proxy) < effective_mae_at_exit:
        effective_mae_at_exit = float(mae_proxy)
    if _should_reclassify_as_white_knuckle(
        current_outcome=str(outcome),
        effective_mae_at_exit=effective_mae_at_exit,
        stop_loss_pct=float(stop_loss_pct),
    ):
        prior = outcome
        outcome = "whiteKnuckle"
        notes.append(
            f"whiteKnuckle: won (exit via {prior}) but MAE={effective_mae_at_exit:.1f}% "
            f"during trade reached scary threshold "
            f"{-max(50.0, 0.5 * float(stop_loss_pct)):.1f}%"
        )

    # Phase E3 — capture spot at exit for greeks attribution.
    try:
        exit_td = trade_days[int(exit_day)]
    except Exception:
        exit_td = trade_days[-1]
    exit_close = closes_by_date.get(exit_td)

    try:
        years_to_expiry = max(1.0 / 365.0, (x_date - e_date).days / 365.0)
    except Exception:
        years_to_expiry = float(window.dte_sessions) / 252.0

    return AnaloguePath(
        entry_date=window.entry_date,
        expiry_date=window.expiry_date,
        dte_sessions=int(window.dte_sessions),
        mapped_strikes=mapped,
        daily_pnl_pct=daily,
        outcome=str(outcome),
        exit_day=int(exit_day),
        exit_pnl_pct=float(exit_pnl if exit_pnl is not None else 0.0),
        max_adverse_excursion_pct=float(mae_at_exit if mae_at_exit is not None else mae),
        breached=bool(breached),
        notes=notes,
        exit_pnl_pct_mid=(None if exit_pnl_mid is None else float(exit_pnl_mid)),
        mae_proxy_pct=(None if mae_proxy is None else float(mae_proxy)),
        mae_source=mae_source,
        entry_close=float(window.entry_close),
        exit_close=(None if exit_close is None else float(exit_close)),
        entry_iv=(float(window.entry_iv_pct) / 100.0 if window.entry_iv_pct is not None else None),
        entry_credit=float(entry_credit),
        years_to_expiry=float(years_to_expiry),
    )


# ---- Aggregation ----

def _percentiles(values: List[float], ps: List[float]) -> List[float]:
    if not values:
        return [0.0 for _ in ps]
    s = sorted(values)
    out: List[float] = []
    n = len(s)
    for p in ps:
        if n == 1:
            out.append(s[0])
            continue
        k = (n - 1) * float(p)
        lo = int(math.floor(k))
        hi = int(math.ceil(k))
        if lo == hi:
            out.append(s[lo])
        else:
            frac = k - lo
            out.append(s[lo] * (1.0 - frac) + s[hi] * frac)
    return out


def _build_mtm_timeline(paths: List[AnaloguePath]) -> List[Dict[str, Any]]:
    by_dte: Dict[int, List[float]] = {}
    breach_hits: Dict[int, int] = {}
    stop_hits: Dict[int, int] = {}
    for p in paths:
        seen_stop = False
        for dte_r, pnl in p.daily_pnl_pct:
            by_dte.setdefault(int(dte_r), []).append(float(pnl))
            if pnl <= -200.0 and not seen_stop:
                stop_hits[int(dte_r)] = stop_hits.get(int(dte_r), 0) + 1
                seen_stop = True
        if p.breached:
            final_dte = p.daily_pnl_pct[-1][0] if p.daily_pnl_pct else 0
            breach_hits[int(final_dte)] = breach_hits.get(int(final_dte), 0) + 1

    rows: List[Dict[str, Any]] = []
    n = max(1, len(paths))
    for dte_r in sorted(by_dte.keys(), reverse=True):  # entry (high dte) → expiry (0)
        vals = by_dte[dte_r]
        p10, p50, p90 = _percentiles(vals, [0.1, 0.5, 0.9])
        rows.append({
            "dte": int(dte_r),
            "p10": round(float(p10), 1),
            "p50": round(float(p50), 1),
            "p90": round(float(p90), 1),
            "n": len(vals),
            "pBreach": round(breach_hits.get(int(dte_r), 0) / n, 3),
            "pStopHit": round(stop_hits.get(int(dte_r), 0) / n, 3),
        })
    return rows


def _summarize_outcomes(paths: List[AnaloguePath]) -> Dict[str, Any]:
    total = max(1, len(paths))
    out: Dict[str, Dict[str, Any]] = {o: {"n": 0, "pnl": [], "days": [], "mae": []} for o in OUTCOMES}
    for p in paths:
        bkt = out.get(p.outcome) or out["whiteKnuckle"]
        bkt["n"] += 1
        bkt["pnl"].append(float(p.exit_pnl_pct))
        bkt["days"].append(int(p.exit_day))
        # Prefer the OHLC-proxy MAE when available — that's the "honest" worst-case
        mae_val = float(p.mae_proxy_pct) if p.mae_proxy_pct is not None else float(p.max_adverse_excursion_pct)
        bkt["mae"].append(mae_val)
    summary: Dict[str, Any] = {}
    for k, v in out.items():
        n = int(v["n"])
        summary[k] = {
            "pct": round(100.0 * n / total, 1),
            "n": n,
            "avgPnlPct": round(statistics.mean(v["pnl"]), 1) if v["pnl"] else 0.0,
            "avgDays": round(statistics.mean(v["days"]), 2) if v["days"] else 0.0,
            "maxAdverseExcursionPct": round(min(v["mae"]), 1) if v["mae"] else 0.0,
        }
    return summary


def _bootstrap_outcome_ci(
    paths: List[AnaloguePath],
    *,
    iterations: int = 500,
    confidence: float = 0.90,
    seed: int = 1337,
) -> Dict[str, Any]:
    """Bootstrap a confidence interval for each outcome's pct + avgPnl.

    We draw `iterations` resamples of size N=len(paths) with replacement
    and recompute pct and avgPnl per outcome, then keep the low/high
    quantiles set by `confidence` (default 90%).

    Returned shape::

        {
          "<outcome>": {
            "pctLow": float, "pctHigh": float,
            "pnlLow": float, "pnlHigh": float,
          },
          ...,
          "_meta": {"n": N, "iterations": I, "confidence": 0.90,
                    "thinSample": bool},
        }
    """
    n = int(len(paths))
    meta = {
        "n": n,
        "iterations": int(iterations),
        "confidence": float(confidence),
        "thinSample": bool(n < 20),
    }
    if n == 0:
        out: Dict[str, Any] = {o: {"pctLow": 0.0, "pctHigh": 0.0, "pnlLow": 0.0, "pnlHigh": 0.0}
                               for o in OUTCOMES}
        out["_meta"] = meta
        return out

    rng = random.Random(int(seed))
    # Pre-materialize (label, pnl) to avoid attribute lookups inside the loop.
    obs = [(p.outcome, float(p.exit_pnl_pct)) for p in paths]
    # Collect bootstrap samples.
    pct_samples: Dict[str, List[float]] = {o: [] for o in OUTCOMES}
    pnl_samples: Dict[str, List[float]] = {o: [] for o in OUTCOMES}
    for _ in range(int(iterations)):
        # Reservoir-free standard bootstrap: sample with replacement.
        draw = [obs[rng.randrange(n)] for _ in range(n)]
        cnt: Dict[str, int] = {o: 0 for o in OUTCOMES}
        pnl_sum: Dict[str, float] = {o: 0.0 for o in OUTCOMES}
        for label, pnl in draw:
            if label not in cnt:
                label = "whiteKnuckle"
            cnt[label] += 1
            pnl_sum[label] += pnl
        for o in OUTCOMES:
            pct_samples[o].append(100.0 * cnt[o] / n)
            pnl_samples[o].append((pnl_sum[o] / cnt[o]) if cnt[o] else 0.0)

    alpha = (1.0 - float(confidence)) / 2.0
    lo_idx = max(0, int(alpha * iterations))
    hi_idx = min(iterations - 1, int((1.0 - alpha) * iterations))

    result: Dict[str, Any] = {}
    for o in OUTCOMES:
        p = sorted(pct_samples[o])
        q = sorted(pnl_samples[o])
        result[o] = {
            "pctLow":  round(p[lo_idx], 1),
            "pctHigh": round(p[hi_idx], 1),
            "pnlLow":  round(q[lo_idx], 1),
            "pnlHigh": round(q[hi_idx], 1),
        }
    result["_meta"] = meta
    return result


def _summarize_outcomes_mid(paths: List[AnaloguePath]) -> Dict[str, Any]:
    """Parallel distribution computed from the legacy mid-price P&L.

    Only paths with a non-null `exit_pnl_pct_mid` contribute. This lets
    the UI show mid vs NBBO side-by-side for one release so users can
    calibrate the fill-realism delta on their own data.

    We reuse the *outcome labels* from the primary (NBBO) path so the two
    distributions line up row-for-row; only the P&L magnitudes differ.
    """
    contributors = [p for p in paths if p.exit_pnl_pct_mid is not None]
    total = max(1, len(contributors))
    out: Dict[str, Dict[str, Any]] = {o: {"n": 0, "pnl": []} for o in OUTCOMES}
    for p in contributors:
        bkt = out.get(p.outcome) or out["whiteKnuckle"]
        bkt["n"] += 1
        bkt["pnl"].append(float(p.exit_pnl_pct_mid))  # type: ignore[arg-type]
    summary: Dict[str, Any] = {}
    for k, v in out.items():
        n = int(v["n"])
        summary[k] = {
            "pct": round(100.0 * n / total, 1),
            "n": n,
            "avgPnlPct": round(statistics.mean(v["pnl"]), 1) if v["pnl"] else 0.0,
        }
    return summary


# ---- Entry-state inference ----

def _infer_user_em_pct(
    req: IcScenarioRequest,
    closes_by_date: Dict[str, float],
) -> Tuple[float, float, str, Optional[str]]:
    """Estimate the user's entry spot + 1-sigma EM% for the requested expiry.

    Returns ``(spot, em_pct, source_str, spot_as_of)`` where ``spot_as_of`` is
    the trade date the spot was actually observed on. ``spot_as_of`` equals
    ``req.entry_date`` when we have a live close on that date; otherwise it's
    the most recent available close on or before ``req.entry_date`` (e.g.
    Friday's close when a Monday trade is requested on a Saturday). Returned
    as ``None`` only in the pathological case where no history at all is
    available.

    Preference order for spot:
      1) Close on ``req.entry_date`` (live).
      2) Most recent close on or before ``req.entry_date`` (weekends, holidays,
         future-dated runs before the bar is published).
      3) Midpoint of short strikes — last-resort only; previously the default
         fallback, but it produced a synthetic spot that silently moved with
         the user's strike edits and made wing-distance % cards meaningless.

    Preference order for EM:
      a) ATM IV from the cached chain at ``spot_as_of`` (option-market
         implied). When the entry date has no chain (future date, weekend),
         looking up IV at the same ``spot_as_of`` date that we resolved spot
         from keeps both numbers internally consistent.
      b) Hard fallback: generic 15% annualized IV scaled by DTE.
    """
    spot = closes_by_date.get(req.entry_date)
    spot_as_of: Optional[str] = req.entry_date if spot is not None else None

    if spot is None:
        # Walk back to the most recent close on or before entry_date. This
        # is the common case on a weekend/holiday or when running a Monday
        # scenario before Monday's close has printed.
        prior_dates = sorted(d for d in closes_by_date if d <= req.entry_date)
        if prior_dates:
            spot_as_of = prior_dates[-1]
            spot = closes_by_date[spot_as_of]

    if spot is None:
        # Truly no history (shouldn't happen; the caller already guards on
        # len(closes_sorted) < 180). Fall back to strike midpoint and flag it.
        spot = (float(req.short_put) + float(req.short_call)) / 2.0
        spot_as_of = None
        spot_source_str = "synthetic strike midpoint (no history)"
    elif spot_as_of == req.entry_date:
        spot_source_str = "live close"
    else:
        spot_source_str = f"stale close {spot_as_of}"

    # Use the spot_as_of date for the chain lookup so spot + IV come from the
    # same session. Otherwise a Monday run on a Saturday would pair Friday's
    # spot with a missing Monday chain, unnecessarily falling back to the
    # generic 15% IV proxy.
    chain_trade_date = spot_as_of or req.entry_date
    chain = chain_cache.fetch_chain_slice(
        ticker=req.underlying, trade_date=chain_trade_date, expiry=req.expiry
    )
    if chain:
        best = min(chain, key=lambda r: abs(float(r.strike) - float(spot)))
        iv = best.call_iv if best.call_iv is not None else best.put_iv
        if iv is not None and iv > 0:
            em = iv_to_em1sigma_pct(
                iv_pct=float(iv) * 100.0, dte_calendar_days=req.dte_calendar()
            )
            return (
                float(spot),
                float(em),
                f"IV from cached chain ({chain_trade_date}); spot={spot_source_str}",
                spot_as_of,
            )

    em = iv_to_em1sigma_pct(iv_pct=15.0, dte_calendar_days=req.dte_calendar())
    return (
        float(spot),
        float(em),
        f"fallback IV=15% annualized; spot={spot_source_str}",
        spot_as_of,
    )


def _entry_regime_bucket(user_em_pct: float, req: IcScenarioRequest) -> str:
    """Rough regime bucket for the user's entry using just IV/EM magnitude.

    We convert the implied 1-sigma % into an annualized IV estimate and
    assign buckets using the Engine 2 labels. This remains as a fallback
    when DailyMarketState is unavailable — see ``_resolve_entry_regime``.
    """
    try:
        dte_c = req.dte_calendar()
        iv_ann = float(user_em_pct) / math.sqrt(max(1, int(dte_c)) / 365.0)
    except Exception:
        iv_ann = 18.0
    if iv_ann <= 12.0:
        return "LOW"
    if iv_ann <= 18.0:
        return "MODERATE"
    if iv_ann <= 28.0:
        return "ELEVATED"
    return "NO_TRADE"


def _summarize_conditioning(
    *,
    conditioning: Dict[str, Any],
    base: Dict[str, Any],
    adjusted: Dict[str, Any],
) -> Dict[str, Any]:
    """Produce a compact summary of the net conditioning effect.

    The engine already surfaces ``conditioningModifiers`` (component
    contributions) and ``adjustedOutcomeDistribution`` (final state), but
    those land in separate places in the UI. The summary answers a
    single question up front: "did conditioning materially change what
    the historical analogues suggested?"

    Fields:
      * ``netTailMultiplier`` / ``netWinRateShiftPct``: carried from the
        modifier engine for convenience.
      * ``material``: True iff the net effect would noticeably shift any
        bucket's probability (``>=1pp absolute`` on any of the five
        outcomes, or ``>=5%`` on the tail multiplier).
      * ``direction``: ``"tailwind" | "headwind" | "flat"`` — whether
        the modifiers improved or hurt the base win rate.
      * ``biggestShiftPct`` / ``biggestShiftBucket``: largest per-bucket
        absolute delta in percentage points (0 when no base/adjusted).
      * ``humanSummary``: single-line desk-facing explanation.
    """
    if not conditioning or conditioning.get("error"):
        return {
            "material": False,
            "direction": "flat",
            "netTailMultiplier": None,
            "netWinRateShiftPct": None,
            "biggestShiftPct": 0.0,
            "biggestShiftBucket": None,
            "humanSummary": "Conditioning unavailable; showing base analogue distribution.",
        }

    tail = conditioning.get("netTailMultiplier")
    wr = conditioning.get("netWinRateShiftPct")
    try:
        tail_f = float(tail) if tail is not None else 1.0
    except (TypeError, ValueError):
        tail_f = 1.0
    try:
        wr_f = float(wr) if wr is not None else 0.0
    except (TypeError, ValueError):
        wr_f = 0.0

    # Per-bucket deltas (pp) when both base and adjusted are present.
    biggest_bucket: Optional[str] = None
    biggest_abs = 0.0
    if base and adjusted:
        for bucket in OUTCOMES:
            b = float((base.get(bucket) or {}).get("pct") or 0.0)
            a = float((adjusted.get(bucket) or {}).get("pct") or 0.0)
            d = a - b
            if abs(d) > biggest_abs:
                biggest_abs = abs(d)
                biggest_bucket = bucket

    tail_material = abs(tail_f - 1.0) >= 0.05
    wr_material = abs(wr_f) >= 1.0
    dist_material = biggest_abs >= 1.0
    material = bool(tail_material or wr_material or dist_material)

    if not material:
        direction = "flat"
    elif wr_f > 0 or (wr_f == 0 and tail_f < 1.0):
        direction = "tailwind"
    else:
        direction = "headwind"

    if material:
        parts = []
        if tail_material:
            parts.append(f"tail ×{tail_f:.2f}")
        if wr_material:
            parts.append(f"WR {wr_f:+.1f}pp")
        if dist_material and biggest_bucket:
            parts.append(f"Δ{biggest_bucket} {biggest_abs:+.1f}pp")
        human = (
            f"Conditioning {direction}: " + ", ".join(parts) + "."
            if parts else f"Conditioning {direction}."
        )
    else:
        human = (
            f"Conditioning near-neutral (tail ×{tail_f:.2f}, WR {wr_f:+.1f}pp); "
            "adjusted distribution ≈ base."
        )

    return {
        "material": material,
        "direction": direction,
        "netTailMultiplier": round(tail_f, 3),
        "netWinRateShiftPct": round(wr_f, 2),
        "biggestShiftPct": round(biggest_abs, 2),
        "biggestShiftBucket": biggest_bucket,
        "humanSummary": human,
    }


def _map_mi_v2_label_to_bucket(label: str) -> str:
    """Map an MI v2 HMM label into E14's REGIME_BUCKETS taxonomy.

    MI v2 emits three states (Risk-On / Transitional / Stressed). E14
    has a four-bucket grid (LOW / MODERATE / ELEVATED / HIGH) inherited
    from the pre-v2 DMS regime reader. We map conservatively:

        Risk-On       -> LOW
        Transitional  -> MODERATE
        Stressed      -> ELEVATED

    "HIGH" is reserved for the legacy DMS extreme tail and never
    inferred from MI v2 alone (the HMM tops out at Stressed).
    """
    lab = (label or "").strip().lower()
    if lab.startswith("risk"):
        return "LOW"
    if lab.startswith("trans"):
        return "MODERATE"
    if lab.startswith("stress"):
        return "ELEVATED"
    return "MODERATE"


@dataclass(frozen=True)
class _ResolvedRegime:
    bucket: str
    source: str                     # "dms" | "em_proxy"
    as_of: Optional[str] = None     # DMS asOfDate when source=="dms"
    score100: Optional[float] = None
    note: Optional[str] = None


def _resolve_entry_regime(
    *,
    user_em_pct: float,
    request: IcScenarioRequest,
    store: Any,
    max_staleness_days: int = 3,
    flags: Optional[FeatureFlags] = None,
) -> _ResolvedRegime:
    """Resolve the user's entry-day regime.

    v2 priority order:

    1. **Market Intelligence v2** ``regime_snapshot()`` when
       ``ENABLE_MI_V2`` is on — the 3-state Gaussian HMM is now the
       single source of truth across E1 / E2 / E15 consoles. E14 joins
       the same source so the desk never sees a "scan said Transitional
       but the scenario says Moderate" mismatch.
    2. ``DailyMarketState`` loaded from Redis for ``request.entry_date``
       (kept for back-compat; also used when MI v2 hasn't been
       calibrated yet).
    3. EM-proxy bucket (``_entry_regime_bucket``).

    The DMS tier is still valuable for **historical** entry dates
    (MI v2 snapshot is today-only); for forward scenarios dated
    "tomorrow", MI v2 is used.
    """
    f = flags or get_flags()

    # ---- Tier 1: MI v2 HMM snapshot (today) --------------------------------
    # Only trust MI v2 when:
    #   1. ENABLE_MI_V2 is on.
    #   2. The snapshot source is the real calibrated HMM ('v2_hmm'), not
    #      a degraded 'default_model' / 'legacy-style' fallback. When MI
    #      v2 falls back, we let DMS win so the legacy regime semantics
    #      stay intact for existing trades.
    #   3. The entry date is forward-dated (today or within
    #      max_staleness_days); historical entry dates use DMS's as-of-
    #      day snapshot.
    try:
        if bool(getattr(f, "ENABLE_MI_V2", False)):
            from backend.market_intel import regime_snapshot as _mi_snap
            _mi = _mi_snap()
            if _mi is not None:
                _source = str(getattr(_mi, "source", "") or "").lower()
                _is_calibrated = "v2_hmm" in _source
                _label = str(getattr(_mi, "label", "") or "").strip()
                _probs = getattr(_mi, "probabilities", None) or {}
                try:
                    _score = float(_probs.get(_label) or 0.0) * 100.0 if _label else None
                except Exception:
                    _score = None
                bucket = _map_mi_v2_label_to_bucket(_label)
                try:
                    entry_dt = dt.date.fromisoformat(request.entry_date)
                    today = dt.date.today()
                    is_forward = entry_dt >= today - dt.timedelta(days=max_staleness_days)
                except Exception:
                    is_forward = True

                if _is_calibrated and bucket in REGIME_BUCKETS and is_forward:
                    return _ResolvedRegime(
                        bucket=bucket,
                        source="mi_v2_hmm",
                        as_of=str(getattr(_mi, "as_of", "") or ""),
                        score100=_score,
                        note=(
                            f"Regime from MI v2 HMM ({_label or 'unknown'}); "
                            "shared with E1/E2/E15 command decks."
                        ),
                    )
    except Exception as _mi_err:
        LOG.debug("engine14: MI v2 regime overlay skipped: %s", _mi_err)

    # ---- Tier 2: DMS (historical / fallback) -------------------------------
    try:
        from backend.daily_market_state import load_dms  # type: ignore
    except Exception:
        load_dms = None  # type: ignore

    if store is not None and load_dms is not None:
        try:
            entry_dt = dt.date.fromisoformat(request.entry_date)
        except Exception:
            entry_dt = None

        if entry_dt is not None:
            # Walk back day-by-day up to max_staleness_days (inclusive).
            for offset in range(0, int(max_staleness_days) + 1):
                probe = (entry_dt - dt.timedelta(days=offset)).isoformat()
                try:
                    dms = load_dms(probe, store)
                except Exception:
                    dms = None
                if dms is None:
                    continue
                regime = getattr(dms, "regime", None) or {}
                bucket = str(regime.get("bucket") or "").strip().upper()
                if bucket in REGIME_BUCKETS:
                    score = regime.get("score100")
                    try:
                        score_f = float(score) if score is not None else None
                    except (TypeError, ValueError):
                        score_f = None
                    note = None
                    if offset > 0:
                        note = (
                            f"Regime from DMS {probe} ({offset}d stale vs entry "
                            f"{request.entry_date}); DMS for entry date not yet computed."
                        )
                    return _ResolvedRegime(
                        bucket=bucket,
                        source="dms",
                        as_of=regime.get("asOfDate") or probe,
                        score100=score_f,
                        note=note,
                    )

    # Fall through to EM-proxy.
    bucket = _entry_regime_bucket(user_em_pct, request)
    return _ResolvedRegime(
        bucket=bucket,
        source="em_proxy",
        as_of=None,
        score100=None,
        note=(
            "Regime derived from entered EM% (DMS unavailable for entry date). "
            "Bucket accuracy reduced in trend/event/news-driven regimes."
        ),
    )


# ---- Public entrypoint ----

def run_scenario(
    request: IcScenarioRequest,
    *,
    client,
    flags: Optional[FeatureFlags] = None,
    benzinga_client: Any = None,
    store: Any = None,
) -> Dict[str, Any]:
    flags = flags or get_flags()
    ticker = str(request.underlying or "SPX").upper()
    if ticker != "SPX":
        raise ValueError("Engine 14 Phase 1 supports SPX only.")

    # 1. Load SPX closes to build analogue windows.
    today = dt.date.today()
    lookback_start = today - dt.timedelta(days=int(flags.ENGINE14_LOOKBACK_YEARS) * 370)
    bars = fetch_dailies_ohlc_range(client, ticker=ticker, start=lookback_start, end=today)
    closes_sorted: List[Tuple[str, float]] = [
        (b.trade_date, float(b.close)) for b in bars if b.close is not None
    ]
    closes_sorted.sort(key=lambda x: x[0])
    closes_by_date = {d: c for d, c in closes_sorted}
    ohlc_by_date: Dict[str, DailyOHLC] = {b.trade_date: b for b in bars if b is not None}
    if len(closes_sorted) < 180:
        return _empty_payload(
            request=request,
            reason="Insufficient SPX history loaded (need at least ~9 months of bars).",
        )

    # 2. Infer user's entry state.
    user_spot, user_em_pct, em_source, user_spot_as_of = _infer_user_em_pct(request, closes_by_date)
    resolved_regime = _resolve_entry_regime(
        user_em_pct=user_em_pct,
        request=request,
        store=store,
        flags=flags,
    )
    user_regime = resolved_regime.bucket

    # v2: capture the MI v2 HMM snapshot (independent of which tier won
    # above) so the response can carry it alongside the legacy DMS regime.
    # The Wing Console + Command Deck renderers key off this payload at
    # entryState.regimeMiV2 + top-level regime.mi_v2 — same shape E1 / E2
    # already emit, so the frontend source-chip + regime card reuse.
    regime_mi_v2: Optional[Dict[str, Any]] = None
    try:
        if bool(getattr(flags, "ENABLE_MI_V2", False)):
            from backend.market_intel import regime_snapshot as _mi_snap
            _mi = _mi_snap()
            if _mi is not None:
                _probs = getattr(_mi, "probabilities", None) or {}
                regime_mi_v2 = {
                    "label":         str(getattr(_mi, "label", "") or "") or None,
                    "probabilities": dict(_probs) if isinstance(_probs, dict) else {},
                    "vol_state":     getattr(_mi, "vol_state", None),
                    "source":        getattr(_mi, "source", "v2_hmm"),
                }
    except Exception as _mi_err:
        LOG.debug("engine14: MI v2 snapshot capture skipped: %s", _mi_err)

    # 3. Build + filter analogue universe.
    entry_dow = 0
    target_dte_calendar = 4
    try:
        entry_dow = dt.date.fromisoformat(request.entry_date).weekday()
        if entry_dow > 4:
            entry_dow = 2  # Wed fallback
        target_dte_calendar = max(
            1,
            (dt.date.fromisoformat(request.expiry) - dt.date.fromisoformat(request.entry_date)).days,
        )
    except Exception:
        pass

    # Count the user's trading-day sessions using the same close series
    # used to build analogue windows — keeps the dte_sessions filter
    # apples-to-apples with what the enumerator emits.
    target_dte_sessions = _count_trading_sessions(
        request.entry_date, request.expiry, closes_by_date, flags=flags
    )

    universe = build_analogue_universe(
        ticker=ticker,
        closes_sorted=closes_sorted,
        entry_dow=entry_dow,
        target_dte_calendar_days=target_dte_calendar,
        max_windows=260,
    )
    # Phase A3 — compute the user's short-strike EM multiple and push it
    # through MatchCriteria so analogues whose listed chains cannot cover
    # a comparable placement are rejected.
    user_em_mult = user_em_multiple(
        user_spot=float(user_spot), user_em_pct=float(user_em_pct),
        short_put=float(request.short_put), short_call=float(request.short_call),
    )
    # Phase C2 — KNN multi-factor regime match. We load the features store
    # for every analogue's entry date (and the user's own entry date) and
    # pass the map into `filter_analogues`. If the store is empty we fall
    # back to the legacy RV20 bucket gate transparently.
    knn_on = bool(getattr(flags, "ENGINE14_ENABLE_KNN_REGIME", False))
    knn_top_n = int(getattr(flags, "ENGINE14_KNN_TOP_N", 80))
    user_features = None
    cand_features: Optional[Dict[str, Any]] = None
    regime_match_quality: Optional[Dict[str, Any]] = None
    if knn_on:
        try:
            from backend.engine14 import regime_features as _rf
            user_features = _rf.fetch_features(request.entry_date)
            if user_features is not None:
                dates = [w.entry_date for w in universe]
                rows = _rf.fetch_features_range(start=min(dates), end=max(dates)) if dates else []
                cand_features = {r.trade_date: r for r in rows}
            else:
                LOG.info("KNN regime: no features for user entry date %s — falling back to bucket match.",
                         request.entry_date)
                knn_on = False
        except Exception as e:
            LOG.warning("KNN regime setup failed: %s — falling back to bucket match.", e)
            knn_on = False

    criteria = MatchCriteria(
        target_regime=user_regime,
        target_dte_sessions=target_dte_sessions,
        regime_bucket_tol=float(flags.ENGINE14_REGIME_BUCKET_TOL),
        season_mode=str(request.season_mode or "none"),
        season_value=request.season_value,
        target_em_multiple=user_em_mult,
        em_multiple_tol=float(getattr(flags, "ENGINE14_EM_MULTIPLE_TOL", 0.25)),
        enable_em_multiple_filter=bool(getattr(flags, "ENGINE14_ENABLE_EM_MULTIPLE_FILTER", False)),
        enable_knn_regime=bool(knn_on),
        knn_top_n=knn_top_n,
    )
    if knn_on:
        candidates, regime_match_quality = filter_analogues(
            universe, criteria=criteria, flags=flags,
            user_features=user_features, candidate_features=cand_features,
            return_match_quality=True,
        )
    else:
        candidates = filter_analogues(universe, criteria=criteria, flags=flags)
    if len(candidates) < int(flags.ENGINE14_MIN_ANALOGUES):
        # Relax DTE filter as a second-pass attempt (still require regime match).
        relaxed = [w for w in universe
                   if abs(int(w.dte_sessions) - int(criteria.target_dte_sessions)) <= 3
                   and w.regime_bucket in (user_regime, _neighbor_bucket(user_regime))]
        if len(relaxed) >= int(flags.ENGINE14_MIN_ANALOGUES):
            candidates = relaxed
        else:
            return _empty_payload(
                request=request,
                reason=f"Not enough analogue windows (found {len(candidates)}, need ≥{flags.ENGINE14_MIN_ANALOGUES}). "
                       f"Try broader strikes, or run the backfill script to extend the chain cache.",
                analogues_considered=len(universe),
            )

    # 4. Replay each analogue.
    user_strikes = request.strike_tuple()
    fill_model = FillModel.from_str(
        getattr(flags, "ENGINE14_FILL_MODEL", "nbbo"),
        penalty_pct=float(getattr(flags, "ENGINE14_FILL_PENALTY_PCT", 15.0)),
    )
    also_mid = bool(getattr(flags, "ENGINE14_EMIT_LEGACY_MID_DIST", True)) and fill_model.mode != "mid"
    mae_proxy_on = bool(getattr(flags, "ENGINE14_MAE_PROXY_ENABLED", True))
    paths: List[AnaloguePath] = []
    for w in candidates:
        p = _simulate_single_analogue(
            ticker=ticker,
            window=w,
            user_strikes=user_strikes,
            user_spot=user_spot,
            user_em_pct=user_em_pct,
            entry_credit=float(request.credit_received),
            profit_target_pct=float(request.profit_target_pct),
            stop_loss_pct=float(request.stop_loss_pct),
            closes_by_date=closes_by_date,
            snap_max_pts=float(flags.ENGINE14_STRIKE_SNAP_MAX_PTS),
            fill_model=fill_model,
            also_price_mid=also_mid,
            ohlc_by_date=ohlc_by_date,
            mae_proxy_enabled=mae_proxy_on,
        )
        if p is not None:
            paths.append(p)
    if len(paths) < int(flags.ENGINE14_MIN_ANALOGUES):
        return _empty_payload(
            request=request,
            reason=f"Replay yielded only {len(paths)} priceable analogues (need ≥{flags.ENGINE14_MIN_ANALOGUES}). "
                   f"Chain cache may be sparse around this regime.",
            analogues_considered=len(candidates),
        )

    # 5. Aggregate.
    outcome_summary = _summarize_outcomes(paths)
    outcome_summary_mid = _summarize_outcomes_mid(paths) if also_mid else {}
    outcome_distribution_ci = _bootstrap_outcome_ci(paths)
    timeline = _build_mtm_timeline(paths)
    final_pnls = [p.exit_pnl_pct for p in paths]
    mean_pnl = statistics.mean(final_pnls)
    median_pnl = statistics.median(final_pnls)
    sd_pnl = statistics.stdev(final_pnls) if len(final_pnls) > 1 else 1.0
    sharpe = (mean_pnl / sd_pnl) if sd_pnl > 1e-9 else 0.0

    exit_opt = optimize_exit_rules(
        paths=paths,
        default_profit_target_pct=float(request.profit_target_pct),
        default_stop_loss_pct=float(request.stop_loss_pct),
    )

    # Sizing — independent of exit optimization, purely distributional.
    sizing = compute_sizing(paths)

    # Phase E3 — greeks attribution per path, aggregated across analogues.
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
            LOG.debug("greeks attribution failed for %s: %s", p.entry_date, e)
    greeks_attribution = aggregate_attribution(attributions)

    # Sample recent analogues for the UI table.
    paths_by_entry = sorted(paths, key=lambda p: p.entry_date, reverse=True)
    sample_n = min(int(flags.ENGINE14_MAX_ANALOGUES), len(paths_by_entry))
    matched_analogues = [
        {
            "entryDate": p.entry_date,
            "expiryDate": p.expiry_date,
            "outcome": p.outcome,
            "exitDay": p.exit_day,
            "pnlPct": round(float(p.exit_pnl_pct), 1),
            "pnlPctMid": None if p.exit_pnl_pct_mid is None else round(float(p.exit_pnl_pct_mid), 1),
            "mae": round(float(p.max_adverse_excursion_pct), 1),
            "maeProxyPct": None if p.mae_proxy_pct is None else round(float(p.mae_proxy_pct), 1),
            "maeSource": p.mae_source,
            "mappedStrikes": {
                "shortPut": p.mapped_strikes[0],
                "longPut": p.mapped_strikes[1],
                "shortCall": p.mapped_strikes[2],
                "longCall": p.mapped_strikes[3],
            },
            "breached": bool(p.breached),
        }
        for p in paths_by_entry[:sample_n]
    ]

    _regime_note = (
        f"Regime={user_regime} via DMS ({resolved_regime.as_of})"
        if resolved_regime.source == "dms"
        else f"Regime={user_regime} via EM-proxy fallback (DMS unavailable)"
    )
    notes = [
        f"Analogue pool: {len(paths)} windows ({_regime_note}, tol=±{int(flags.ENGINE14_REGIME_BUCKET_TOL)}pts)",
        f"Entry EM inferred via {em_source}; 1σ={user_em_pct:.2f}%",
        "Strike mapping preserves EM-distance across historical spot levels.",
        f"Exit fill model: {fill_model.mode}"
        + (f" (+{fill_model.penalty_pct:.0f}% half-spread)" if fill_model.mode == "mid_penalty" else ""),
    ]
    if resolved_regime.note:
        notes.append(resolved_regime.note)
    if user_em_mult is not None:
        notes.append(
            f"Trade placement: |z|={user_em_mult:.2f}σ short strikes"
            + (" — EM-multiple filter ON" if criteria.enable_em_multiple_filter else "")
        )
    if mae_proxy_on:
        hit = sum(1 for p in paths if p.mae_source == "ohlc_proxy")
        if hit:
            notes.append(f"OHLC MAE proxy engaged on {hit}/{len(paths)} analogues (intraday scares).")

    # Phase 2: forward-looking conditioning modifiers.
    # These NEVER mutate the empirical outcomeDistribution; we emit them
    # alongside and produce an explicit adjustedOutcomeDistribution view.
    conditioning: Dict[str, Any] = {}
    adjusted_distribution: Dict[str, Any] = {}
    if getattr(flags, "ENGINE14_ENABLE_CONDITIONING", True):
        try:
            conditioning = compute_conditioning(
                entry_date=request.entry_date,
                expiry_date=request.expiry,
                orats_client=client,
                benzinga_client=benzinga_client,
                store=store,
            )
            adjusted_distribution = apply_modifiers_to_distribution(
                base_distribution=outcome_summary,
                net_tail_multiplier=float(conditioning.get("netTailMultiplier", 1.0)),
                net_wr_shift_pct=float(conditioning.get("netWinRateShiftPct", 0.0)),
            )
            for n in conditioning.get("notes", []):
                if n and n not in notes:
                    notes.append(n)
        except Exception as e:
            LOG.warning("engine14 conditioning failed: %s", e)
            conditioning = {"error": f"{type(e).__name__}: {e}"}
            adjusted_distribution = {}

    # Stage 4: summarize the *net* conditioning effect so the UI can tell
    # the user, in one sentence, whether adjustedOutcomeDistribution is
    # materially different from outcomeDistribution and why. When the
    # modifiers largely cancel out (which is the common case — calendar
    # headwinds offsetting dealer-gamma tailwinds, for instance), we
    # explicitly say so instead of silently showing identical bars.
    conditioning_summary = _summarize_conditioning(
        conditioning=conditioning,
        base=outcome_summary,
        adjusted=adjusted_distribution,
    )

    out = {
        "engine": 14,
        "version": "2.0.0",
        "request": asdict(request),
        "analoguesUsed": len(paths),
        "analoguesConsidered": len(candidates),
        "analogueBucket": {
            "regime": user_regime,
            "macro": "NORMAL",
            "season": request.season_mode or "ALL",
        },
        "entryState": {
            "userSpot": round(float(user_spot), 2),
            "userSpotAsOf": user_spot_as_of,
            "userSpotIsLive": bool(user_spot_as_of == request.entry_date),
            "userEmPct": round(float(user_em_pct), 3),
            "wingWidth": round(float(request.wing_width()), 2),
            "regimeBucket": user_regime,
            "regimeSource": resolved_regime.source,
            "regimeSourceAsOf": resolved_regime.as_of,
            "regimeScore": (
                None if resolved_regime.score100 is None
                else round(float(resolved_regime.score100), 2)
            ),
            "userEmMultiple": None if user_em_mult is None else round(float(user_em_mult), 2),
            # v2: MI v2 HMM overlay (same shape E1 / E2 emit).
            "regimeMiV2": regime_mi_v2,
        },
        # v2: top-level regime dict so consumers that treat it as a first-
        # class field (parity with E2's regime.mi_v2) can read it directly.
        "regime": {
            "label":  user_regime,
            "bucket": user_regime,
            "source": resolved_regime.source,
            "mi_v2":  regime_mi_v2,
        },
        "fillModel": {
            "mode": fill_model.mode,
            "penaltyPct": float(fill_model.penalty_pct),
            "emitLegacyMidDistribution": bool(also_mid),
            "maeProxyEnabled": bool(mae_proxy_on),
        },
        "regimeMatchQuality": regime_match_quality or {
            "source": "bucket",
            "bucket": user_regime,
            "n": int(len(candidates)),
        },
        "outcomeDistribution": outcome_summary,
        "outcomeDistributionMid": outcome_summary_mid,
        "outcomeDistributionCI": outcome_distribution_ci,
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
        "conditioningNotes": notes,
        "matchedAnalogues": matched_analogues,
    }
    return out


def _neighbor_bucket(b: str) -> str:
    try:
        i = REGIME_BUCKETS.index(b)
        if i < len(REGIME_BUCKETS) - 1:
            return REGIME_BUCKETS[i + 1]
        return REGIME_BUCKETS[i - 1]
    except ValueError:
        return b


def _count_trading_sessions(
    entry_iso: str, expiry_iso: str, trading_days: Dict[str, float],
    *,
    flags: Optional[FeatureFlags] = None,
) -> int:
    """Count trading sessions from entry through expiry (inclusive both ends).

    Uses the caller's close series as the trading-day calendar so the value
    is consistent with what `_build_matching_windows` emits as dte_sessions.
    For dates beyond the historical series (common case: user prices a
    forward-dated trade), v2 consults
    :mod:`backend.engine15.trading_calendar` when
    ``ENGINE14_HOLIDAY_CALENDAR`` is on (the default) so forward-dated
    entries correctly skip NYSE holidays + half-days instead of relying
    on the weekday heuristic. Flag-off reverts to the legacy Mon-Fri
    fallback to preserve back-compat for consumers that monkeypatched
    the old behavior (notably E15's replay adapter test fixtures).
    """
    try:
        a = dt.date.fromisoformat(entry_iso)
        b = dt.date.fromisoformat(expiry_iso)
    except Exception:
        return 1
    if b < a:
        return 1
    f = flags or get_flags()
    use_cal = bool(getattr(f, "ENGINE14_HOLIDAY_CALENDAR", True))
    cal_is_trading = None
    if use_cal:
        try:
            from backend.engine15.trading_calendar import is_trading_day as _tc_is_trading_day
            cal_is_trading = _tc_is_trading_day
        except Exception:
            cal_is_trading = None

    last_hist = max(trading_days.keys()) if trading_days else ""
    n = 0
    d = a
    while d <= b:
        ds = d.isoformat()
        if ds in trading_days:
            n += 1
        elif ds > last_hist:
            # Beyond the cached bar series — consult the NYSE calendar
            # when enabled, else fall back to the weekday heuristic.
            if cal_is_trading is not None:
                try:
                    if cal_is_trading(d):
                        n += 1
                except Exception:
                    if d.weekday() < 5:
                        n += 1
            elif d.weekday() < 5:
                n += 1
        d += dt.timedelta(days=1)
    return max(1, n)


def _is_iso(s: str) -> bool:
    try:
        dt.date.fromisoformat(str(s))
        return True
    except Exception:
        return False


def _empty_payload(*, request: IcScenarioRequest, reason: str, analogues_considered: int = 0) -> Dict[str, Any]:
    empty_dist = {o: {"pct": 0.0, "n": 0, "avgPnlPct": 0.0, "avgDays": 0.0, "maxAdverseExcursionPct": 0.0} for o in OUTCOMES}
    return {
        "engine": 14,
        "version": "1.3.0",
        "request": asdict(request),
        "analoguesUsed": 0,
        "analoguesConsidered": int(analogues_considered),
        "analogueBucket": {"regime": None, "macro": None, "season": request.season_mode or "ALL"},
        "entryState": None,
        "fillModel": {
            "mode": "nbbo", "penaltyPct": 15.0,
            "emitLegacyMidDistribution": False, "maeProxyEnabled": False,
        },
        "regimeMatchQuality": {"source": "bucket", "n": 0},
        "outcomeDistribution": empty_dist,
        "outcomeDistributionMid": {},
        "outcomeDistributionCI": {"_meta": {"n": 0, "iterations": 0, "confidence": 0.90, "thinSample": True}},
        "adjustedOutcomeDistribution": {},
        "conditioningModifiers": {},
        "mtmTimeline": [],
        "expectedValue": {"meanPnlPct": 0.0, "medianPnlPct": 0.0, "sharpeProxy": 0.0},
        "exitRulesOptimization": {
            "recommendedProfitTarget": float(request.profit_target_pct),
            "recommendedStopLoss": float(request.stop_loss_pct),
            "deltaFromDefault": {"winRatePct": 0.0, "avgPnlPct": 0.0},
        },
        "sizing": {"n": 0, "consensusFraction": 0.0},
        "greeksAttribution": {"n": 0, "deltaPct": 0.0, "gammaPct": 0.0, "thetaPct": 0.0,
                              "vegaPct": 0.0, "residualPct": 0.0, "totalPct": 0.0,
                              "shareOfAbsPnl": {}},
        "conditioningNotes": [reason],
        "matchedAnalogues": [],
    }
