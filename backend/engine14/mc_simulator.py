"""Forward Monte Carlo simulator for the E14 IC Scenario Command Deck.

Thin adapter around :mod:`backend.engine2.mc_simulator`. E2's weekly
MC is ticker-agnostic (it bootstraps over a pool of weekly daily-
return paths and aggregates breach / touch / MAE stats for a list
of ``(em_mult, wing_pts)`` placements), which is exactly the shape
E14's Wing Console needs. Rather than reimplement the math we feed
E14's analogue pool (built by
:func:`backend.engine14.analogue_matcher.build_analogue_universe`)
into the shared routine.

The one E14-specific concern is **pool adaptation**: E2's MC expects
each pool row to carry ``daily_returns`` (per-day simple returns
across the hold window) + ``regime_bucket`` + ``macro_bucket``.
E14's analogues carry close-by-date references + regime metadata on
the ``AnalogueWindow`` dataclass, so we translate here.

Usage: see :func:`run_forward_mc`.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from backend.engine2.mc_simulator import (
    MCPlacementResult,
    MCResult,
    run_weekly_mc,
)

LOG = logging.getLogger("engine14.mc_simulator")


# ---------------------------------------------------------------------------
# Pool adaptation
# ---------------------------------------------------------------------------


def _as_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _per_day_returns(
    *,
    closes_by_date: Dict[str, float],
    entry_date: str,
    expiry_date: str,
) -> List[float]:
    """Extract per-day simple returns for the hold window (entry exclusive,
    expiry inclusive)."""
    try:
        ed_iso = str(entry_date)[:10]
        xp_iso = str(expiry_date)[:10]
    except Exception:
        return []
    ed_d = _safe_date(ed_iso)
    xp_d = _safe_date(xp_iso)
    if ed_d is None or xp_d is None:
        return []

    sorted_dates = sorted(closes_by_date.keys())
    # Prior close = entry-day close (or last close before entry if missing).
    entry_close = closes_by_date.get(ed_iso)
    if entry_close is None:
        for d in reversed(sorted_dates):
            if d <= ed_iso:
                entry_close = closes_by_date.get(d)
                break
    if entry_close is None or entry_close <= 0:
        return []

    out: List[float] = []
    prev = float(entry_close)
    for d in sorted_dates:
        if d <= ed_iso:
            continue
        if d > xp_iso:
            break
        c = _as_float(closes_by_date.get(d))
        if c is None or c <= 0:
            continue
        out.append((c - prev) / prev)
        prev = c
    return out


def _safe_date(s: str) -> Optional[_dt.date]:
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def build_mc_pool(
    *,
    windows: Iterable[Any],
    closes_by_date: Dict[str, float],
    hold_days: int,
) -> List[Dict[str, Any]]:
    """Translate E14 analogue windows into MC-friendly rows.

    Each row carries ``daily_returns`` + ``signed_move_pct`` +
    ``regime_bucket`` + ``macro_bucket`` so
    :func:`backend.engine2.mc_simulator.run_weekly_mc` can bootstrap
    without further adaptation.
    """
    pool: List[Dict[str, Any]] = []
    for win in (windows or []):
        ed = getattr(win, "entry_date", None) or win.get("entry_date") if isinstance(win, dict) else getattr(win, "entry_date", None)
        xp = getattr(win, "expiry_date", None) if not isinstance(win, dict) else win.get("expiry_date")
        regime = (
            getattr(win, "regime_bucket", None)
            if not isinstance(win, dict) else win.get("regime_bucket")
        ) or "UNKNOWN"
        macro = (
            getattr(win, "macro_bucket", None)
            if not isinstance(win, dict) else win.get("macro_bucket")
        ) or "NORMAL"
        if ed is None or xp is None:
            continue
        dr = _per_day_returns(
            closes_by_date=closes_by_date,
            entry_date=str(ed),
            expiry_date=str(xp),
        )
        if not dr:
            continue
        # Pad/clip to hold_days so MC samples have consistent length.
        if len(dr) < hold_days:
            dr = dr + [0.0] * (hold_days - len(dr))
        else:
            dr = dr[:hold_days]
        total = 1.0
        for r in dr:
            total *= (1.0 + r)
        signed_move_pct = (total - 1.0) * 100.0
        pool.append({
            "entry_date":      str(ed)[:10],
            "expiry_date":     str(xp)[:10],
            "regime_bucket":   str(regime).upper(),
            "macro_bucket":    str(macro).upper(),
            "daily_returns":   dr,
            "signed_move_pct": round(float(signed_move_pct), 4),
        })
    return pool


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_forward_mc(
    *,
    ticker: str,
    as_of_date: str,
    spot: float,
    em_pct: float,
    hold_days: int,
    analogue_windows: Iterable[Any],
    closes_by_date: Dict[str, float],
    placements: Sequence[Tuple[float, float]],
    n_sims: int = 5000,
    min_pool: int = 20,
    seed: int = 1337,
    condition_on_regime: bool = True,
    condition_on_macro: bool = True,
    want_regime_bucket: Optional[str] = None,
    want_macro_bucket: Optional[str] = None,
    gbm_fallback: bool = True,
    flags_fp: Optional[Tuple[Any, ...]] = None,
) -> MCResult:
    """Run the forward MC pass for E14's Wing Console.

    Conditioning, seeding, and path math are delegated to
    :func:`backend.engine2.mc_simulator.run_weekly_mc`. Returns an
    :class:`MCResult` with a per-placement entry for every
    ``(em_mult, wing_pts)`` tuple passed in.
    """
    pool = build_mc_pool(
        windows=analogue_windows,
        closes_by_date=closes_by_date,
        hold_days=int(hold_days),
    )
    return run_weekly_mc(
        ticker=str(ticker).upper(),
        as_of_date=str(as_of_date or "")[:10],
        spot=float(spot),
        em_pct=float(em_pct),
        hold_days=int(hold_days),
        weekly_pool=pool,
        placements=list(placements),
        n_sims=int(n_sims),
        min_pool=int(min_pool),
        seed=int(seed),
        condition_on_regime=bool(condition_on_regime),
        condition_on_macro=bool(condition_on_macro),
        want_regime_bucket=want_regime_bucket,
        want_macro_bucket=want_macro_bucket,
        gbm_fallback=bool(gbm_fallback),
        flags_fp=tuple(flags_fp or ()),
    )


__all__ = [
    "MCPlacementResult",
    "MCResult",
    "build_mc_pool",
    "run_forward_mc",
]
