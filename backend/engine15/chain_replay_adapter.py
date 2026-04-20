"""Engine 15 — adapt Engine 14 chain replay for single-name earnings IC.

This module is a thin shim on top of
:func:`backend.engine14.chain_replay.reprice_ic`. It does NOT fork the
pricing logic — the NBBO math, fill penalties, snap tolerance, and
short-vs-long leg conventions are all reused verbatim. The adapter's
job is purely:

1.  Pick the best historical expiry for each earnings window by
    matching the user's entry-to-expiry calendar-day gap against
    the expiries actually listed on ``entry_date_hist`` in the cache
    (see :func:`_resolve_expiry_for_window`).
2.  Map the user's four strikes into the analogue's strike space
    preserving EM-distance (same transformation as Engine 14, via
    :func:`backend.engine14.analogue_matcher.map_user_strikes_to_analogue`).
3.  Drive the replay loop from ``entry_date_hist`` through
    ``planned_exit_date_hist`` (NOT the options' expiry) and classify
    the outcome at that planned-exit boundary.

The result shape is :class:`backend.engine14.simulator.AnaloguePath` so
aggregation (`_summarize_outcomes`, `_build_mtm_timeline`,
`_bootstrap_outcome_ci`, `optimize_exit_rules`, `compute_sizing`,
`attribute_path`) can be reused without any per-engine branches.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional, Tuple

from backend.engine14 import chain_cache
from backend.engine14.analogue_matcher import map_user_strikes_to_analogue
from backend.engine14.chain_replay import FillModel, reprice_ic
from backend.engine14.simulator import (
    AnaloguePath,
    _is_scary_mae,
    _should_reclassify_as_white_knuckle,
)
from backend.engine15.event_universe import EarningsWindow
from backend.spx_ic.ohlc import iv_to_em1sigma_pct

LOG = logging.getLogger("engine15.chain_replay_adapter")


__all__ = [
    "EventReplayResult",
    "simulate_event",
    "resolve_event_context",
    "EventContext",
]


# ---------------------------------------------------------------------------
# Per-event context resolution
# ---------------------------------------------------------------------------

class EventContext:
    """Cached per-event replay inputs pulled once from the chain cache."""

    __slots__ = ("window", "expiry_hist", "entry_chain", "entry_close", "entry_iv")

    def __init__(
        self,
        window: EarningsWindow,
        expiry_hist: Optional[str],
        entry_chain: List[Any],
        entry_close: Optional[float],
        entry_iv: Optional[float],
    ) -> None:
        self.window = window
        self.expiry_hist = expiry_hist
        self.entry_chain = entry_chain
        self.entry_close = entry_close
        self.entry_iv = entry_iv

    def to_dict(self) -> Dict[str, Any]:
        return {
            "earnDate": self.window.earn_date_hist,
            "entryDateHist": self.window.entry_date_hist,
            "expiryHist": self.expiry_hist,
            "entryClose": self.entry_close,
            "entryIV": self.entry_iv,
        }


def _parse_iso(s: Any) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _resolve_expiry_for_window(
    *,
    ticker: str,
    entry_date_hist: str,
    target_dte_calendar: int,
    max_offset_days: int,
) -> Optional[str]:
    """Pick the closest-DTE expiry at entry_date_hist to the user's target gap.

    Returns the ISO expiry string from the cache, or ``None`` if no
    expiry within ``max_offset_days`` calendar days of target exists.
    """
    expiries = chain_cache.fetch_expiries_on(ticker=ticker, trade_date=entry_date_hist)
    if not expiries:
        return None
    ed = _parse_iso(entry_date_hist)
    if ed is None:
        return None
    target_delta = max(1, int(target_dte_calendar))

    best: Optional[Tuple[int, str]] = None
    for exp in expiries:
        xd = _parse_iso(exp)
        if xd is None or xd < ed:
            continue
        diff = abs((xd - ed).days - target_delta)
        if diff > int(max_offset_days):
            continue
        if best is None or diff < best[0]:
            best = (diff, exp)
    return best[1] if best else None


def _spot_from_chain(chain: List[Any]) -> Optional[float]:
    if not chain:
        return None
    spots = [float(r.spot) for r in chain if getattr(r, "spot", None) is not None]
    if not spots:
        return None
    # Multiple strikes publish the same spot; take the median as a safety.
    spots.sort()
    return float(spots[len(spots) // 2])


def _atm_iv_from_chain(chain: List[Any], spot: float) -> Optional[float]:
    if not chain or spot is None or spot <= 0:
        return None
    best = min(chain, key=lambda r: abs(float(r.strike) - float(spot)))
    iv = best.call_iv if best.call_iv is not None else best.put_iv
    if iv is None or iv <= 0:
        return None
    return float(iv)


def _infer_analogue_em_pct(
    *,
    chain: List[Any],
    spot: float,
    entry_date_hist: str,
    expiry_hist: Optional[str],
    fallback_implied_move_pct: Optional[float],
) -> float:
    """Compute the analogue's 1σ expected move over entry→expiry.

    Preference order:
      1. ATM IV from the cached entry-day chain, converted via
         ``iv_to_em1sigma_pct``.
      2. E1's ``impliedMovePct`` from the event row (this is the expected
         move over the earnings window itself — a known bias vs the
         same-DTE sigma but is the closest substitute when no chain is
         cached).
      3. Generic 15% annualized IV over the requested DTE.
    """
    iv_dec = _atm_iv_from_chain(chain, spot)
    ed = _parse_iso(entry_date_hist)
    xd = _parse_iso(expiry_hist) if expiry_hist else None
    dte_cal = max(1, (xd - ed).days) if (ed and xd) else 4

    if iv_dec is not None:
        return float(iv_to_em1sigma_pct(iv_pct=iv_dec * 100.0, dte_calendar_days=dte_cal))
    if fallback_implied_move_pct is not None and float(fallback_implied_move_pct) > 0:
        return float(fallback_implied_move_pct)
    return float(iv_to_em1sigma_pct(iv_pct=15.0, dte_calendar_days=dte_cal))


def resolve_event_context(
    *,
    ticker: str,
    window: EarningsWindow,
    target_dte_calendar: int,
    max_expiry_offset_days: int,
) -> Optional[EventContext]:
    """Populate the per-event inputs needed by :func:`simulate_event`.

    Returns ``None`` when the event cannot be priced from the cache
    (missing expiry, missing entry-day chain, or zero rows). The caller
    decides whether to drop or warn.
    """
    expiry_hist = _resolve_expiry_for_window(
        ticker=ticker,
        entry_date_hist=window.entry_date_hist,
        target_dte_calendar=target_dte_calendar,
        max_offset_days=max_expiry_offset_days,
    )
    if expiry_hist is None:
        return None
    entry_chain = chain_cache.fetch_chain_slice(
        ticker=ticker,
        trade_date=window.entry_date_hist,
        expiry=expiry_hist,
    )
    if not entry_chain:
        return None
    spot = _spot_from_chain(entry_chain)
    if spot is None or spot <= 0:
        return None
    iv_dec = _atm_iv_from_chain(entry_chain, spot)
    window.entry_close = float(spot)
    return EventContext(
        window=window,
        expiry_hist=expiry_hist,
        entry_chain=entry_chain,
        entry_close=float(spot),
        entry_iv=(None if iv_dec is None else float(iv_dec)),
    )


# ---------------------------------------------------------------------------
# Per-event replay
# ---------------------------------------------------------------------------

class EventReplayResult:
    """Compact replay result for a single historical earnings event."""

    __slots__ = (
        "path", "context", "mapped_strikes", "notes", "analogue_entry_credit",
    )

    def __init__(
        self,
        path: Optional[AnaloguePath],
        context: EventContext,
        mapped_strikes: Tuple[float, float, float, float],
        notes: List[str],
        analogue_entry_credit: Optional[float] = None,
    ) -> None:
        self.path = path
        self.context = context
        self.mapped_strikes = mapped_strikes
        self.notes = notes
        self.analogue_entry_credit = analogue_entry_credit


def _enumerate_biz_days(entry: dt.date, exit_: dt.date) -> List[str]:
    """Business-day sequence ``[entry, entry+1, ..., exit]`` inclusive.

    We don't consult a holiday calendar. If the cache has no rows for a
    given date we skip pricing it, same as Engine 14.
    """
    days: List[str] = []
    cur = entry
    while cur <= exit_:
        if cur.weekday() <= 4:
            days.append(cur.isoformat())
        cur += dt.timedelta(days=1)
    return days


def simulate_event(
    context: EventContext,
    *,
    ticker: str,
    user_spot: float,
    user_em_pct: float,
    user_strikes: Tuple[float, float, float, float],
    entry_credit: float,
    profit_target_pct: float,
    stop_loss_pct: float,
    snap_max_pts: float,
    fill_model: Optional[FillModel] = None,
    intraday_crush_factor: float = 1.0,
) -> EventReplayResult:
    """Replay a single earnings analogue from entry→planned-exit.

    The ``intraday_crush_factor`` is applied ONLY to the realized
    exit-day pnl (not to the MTM timeline) because ORATS historical is
    EOD. If ``plannedExitDate`` is the day OF the earnings print, the
    desk typically exits in the AM session (~30-90min after open) where
    IV has crushed more than the EOD close reflects. A factor of 1.0
    leaves the close-proxy unchanged; 0.8 assumes ~80% of the full
    close-to-close move has already played out at the planned exit
    time. This is an intentionally simple knob; the Phase 2 plan is to
    calibrate it empirically from journaled trades.
    """
    w = context.window
    notes: List[str] = []
    try:
        mapped = map_user_strikes_to_analogue(
            user_spot=float(user_spot),
            user_em_pct=float(user_em_pct),
            analogue_spot=float(context.entry_close or 0.0),
            analogue_em_pct=_infer_analogue_em_pct(
                chain=context.entry_chain,
                spot=float(context.entry_close or 0.0),
                entry_date_hist=w.entry_date_hist,
                expiry_hist=context.expiry_hist,
                fallback_implied_move_pct=w.implied_move_pct,
            ),
            user_strikes=user_strikes,
        )
    except Exception as e:
        LOG.debug("engine15: strike mapping failed for %s: %s", w.earn_date_hist, e)
        return EventReplayResult(None, context, user_strikes, [f"strike mapping failed: {e}"])

    try:
        entry_dt = dt.date.fromisoformat(w.entry_date_hist)
        exit_dt = dt.date.fromisoformat(w.planned_exit_date_hist)
    except Exception as e:
        return EventReplayResult(None, context, mapped, [f"bad date: {e}"])

    trade_days = _enumerate_biz_days(entry_dt, exit_dt)
    if not trade_days:
        return EventReplayResult(None, context, mapped, ["empty planned-exit window"])

    sp_k, lp_k, sc_k, lc_k = mapped
    fm = fill_model or FillModel()

    # Derive the analogue's OWN entry-day natural credit.
    # Without this, using the user's forward credit (e.g. a pre-market
    # $0.17 on a trade where every historical analogue's entry-day NBBO
    # priced the same mapped strikes at $0.40+) causes an instant
    # ``pnl_pct = (user_credit - analogue_debit) / user_credit`` hit on
    # day 0 that trips the stop-loss before the earnings event itself.
    # We reprice the entry chain with a sentinel credit to extract the
    # analogue's NBBO mid (``net_debit_to_close``) and use THAT as the
    # analogue-relative credit for the replay. Each analogue's pnl_pct
    # is therefore scaled to its own natural credit, which is the only
    # way a multi-event historical distribution is comparable.
    try:
        entry_priced_for_credit = reprice_ic(
            chain=context.entry_chain,
            short_put_strike=sp_k, long_put_strike=lp_k,
            short_call_strike=sc_k, long_call_strike=lc_k,
            entry_credit=1.0,
            snap_max_pts=float(snap_max_pts),
            fill_model=fm,
        )
    except Exception as e:
        notes.append(f"entry credit derivation failed: {type(e).__name__}: {e}")
        entry_priced_for_credit = None
    analogue_entry_credit: Optional[float] = None
    if entry_priced_for_credit is not None:
        dbc = float(entry_priced_for_credit.net_debit_to_close)
        if dbc > 0:
            analogue_entry_credit = float(dbc)
    if analogue_entry_credit is None or analogue_entry_credit <= 0:
        # Last-resort fallback: use the user's credit. The analogue will
        # show biased pnl_pct magnitudes but at least won't short-circuit
        # the replay. The note explains the caveat.
        analogue_entry_credit = float(entry_credit)
        notes.append(
            "Analogue entry-credit derivation failed; falling back to user credit "
            "— pnl_pct scaling may be distorted for this event."
        )

    daily: List[Tuple[int, float]] = []
    mae = 0.0
    mae_at_exit: Optional[float] = None
    exit_day: Optional[int] = None
    exit_pnl: Optional[float] = None
    outcome: Optional[str] = None
    priced_days = 0

    entry_priced_at_entry: Optional[float] = None

    for i, td in enumerate(trade_days):
        days_held_remaining = len(trade_days) - 1 - i
        chain = chain_cache.fetch_chain_slice(
            ticker=ticker, trade_date=td, expiry=context.expiry_hist,
        )
        if not chain:
            notes.append(f"{td}: no cached chain; skipped")
            continue
        priced = reprice_ic(
            chain=chain,
            short_put_strike=sp_k,
            long_put_strike=lp_k,
            short_call_strike=sc_k,
            long_call_strike=lc_k,
            entry_credit=float(analogue_entry_credit),
            snap_max_pts=float(snap_max_pts),
            fill_model=fm,
        )
        if priced is None:
            notes.append(f"{td}: pricing failed (strike snap out of tol)")
            continue
        pnl_pct = float(priced.pnl_pct_of_credit)
        if i == 0:
            entry_priced_at_entry = pnl_pct
        priced_days += 1
        daily.append((int(days_held_remaining), float(pnl_pct)))
        if pnl_pct < mae:
            mae = float(pnl_pct)

        if exit_day is None:
            if pnl_pct >= float(profit_target_pct):
                exit_day = i
                exit_pnl = pnl_pct
                mae_at_exit = float(mae)
                outcome = "earlyTarget"
            elif pnl_pct <= -float(stop_loss_pct):
                exit_day = i
                exit_pnl = pnl_pct
                mae_at_exit = float(mae)
                outcome = "stopOut"

    if priced_days == 0:
        return EventReplayResult(None, context, mapped, notes + ["no priceable days in window"])

    # Force time-stop at planned exit boundary (the canonical engine15 semantic).
    if exit_day is None:
        last_pnl = daily[-1][1]
        # Apply the intraday crush factor ONLY on the planned exit
        # boundary — this approximates the T+1 ~10:30 AM realized P&L
        # versus the T+1 close that ORATS publishes.
        if intraday_crush_factor is not None and float(intraday_crush_factor) > 0 \
                and entry_priced_at_entry is not None:
            # Blend toward the entry-day P&L by (1 - factor). factor=1.0
            # keeps the close value; factor<1 means less of the close-to-
            # close move has played out by morning.
            factor = max(0.0, min(1.2, float(intraday_crush_factor)))
            last_pnl = float(entry_priced_at_entry) + factor * (float(last_pnl) - float(entry_priced_at_entry))
            notes.append(
                f"planned-exit pnl crushed close→morning by factor={factor:.2f} "
                f"(close={daily[-1][1]:.1f}% → est AM={last_pnl:.1f}%)"
            )
        exit_day = len(daily) - 1
        exit_pnl = float(last_pnl)
        mae_at_exit = float(mae)
        if last_pnl >= 95.0:
            outcome = "fullCollect"
        elif last_pnl < -float(stop_loss_pct):
            outcome = "stopOut"
        elif last_pnl < 0.0:
            outcome = "stopOut"
            notes.append("planned exit ended negative below zero (stop rule not hit)")
        else:
            outcome = "fullCollect"
            if last_pnl < 95.0:
                notes.append("partial credit kept at planned exit (pnl < 95% but positive)")

    # Breach detection on the planned exit day: short strike broken.
    last_chain = context.entry_chain
    last_spot: Optional[float] = None
    try:
        # Closing spot on the planned-exit date, from the cached chain.
        eod_chain = chain_cache.fetch_chain_slice(
            ticker=ticker,
            trade_date=trade_days[-1],
            expiry=context.expiry_hist,
        ) or last_chain
        last_spot = _spot_from_chain(eod_chain)
    except Exception:
        last_spot = None
    breached = False
    if last_spot is not None and last_spot > 0:
        if last_spot < sp_k or last_spot > sc_k:
            breached = True
    if breached and (exit_pnl is not None and exit_pnl <= -50.0):
        outcome = "breach"

    effective_mae = float(mae_at_exit if mae_at_exit is not None else mae)
    if _should_reclassify_as_white_knuckle(
        current_outcome=str(outcome),
        effective_mae_at_exit=effective_mae,
        stop_loss_pct=float(stop_loss_pct),
    ):
        prior = outcome
        outcome = "whiteKnuckle"
        notes.append(
            f"whiteKnuckle: won (exit via {prior}) but MAE={effective_mae:.1f}% reached scary "
            f"threshold {-max(50.0, 0.5 * float(stop_loss_pct)):.1f}%"
        )

    # years_to_expiry — used by greeks attribution.
    try:
        exp_d = dt.date.fromisoformat(context.expiry_hist) if context.expiry_hist else exit_dt
        yte = max(1.0 / 365.0, (exp_d - entry_dt).days / 365.0)
    except Exception:
        yte = max(len(trade_days), 1) / 252.0

    path = AnaloguePath(
        entry_date=w.entry_date_hist,
        expiry_date=context.expiry_hist or w.planned_exit_date_hist,
        dte_sessions=int(len(trade_days)),
        mapped_strikes=mapped,
        daily_pnl_pct=daily,
        outcome=str(outcome),
        exit_day=int(exit_day),
        exit_pnl_pct=float(exit_pnl if exit_pnl is not None else 0.0),
        max_adverse_excursion_pct=float(effective_mae),
        breached=bool(breached),
        notes=list(notes),
        exit_pnl_pct_mid=None,
        mae_proxy_pct=None,
        mae_source="eod",
        entry_close=float(context.entry_close or 0.0),
        exit_close=(float(last_spot) if last_spot else None),
        entry_iv=(None if context.entry_iv is None else float(context.entry_iv)),
        # Store the analogue's own entry credit (not the user's) so
        # greeks attribution + sizing operate on the same units the
        # ``exit_pnl_pct`` already uses.
        entry_credit=float(analogue_entry_credit),
        years_to_expiry=float(yte),
    )
    return EventReplayResult(path, context, mapped, notes, analogue_entry_credit=float(analogue_entry_credit))
