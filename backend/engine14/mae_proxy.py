"""Intraweek MAE proxy for the E14 IC Scenario Command Deck.

Parallel to :mod:`backend.engine2.mae_proxy` but sourced from E14's
analogue pool (weekly hold windows built by
:mod:`backend.engine14.analogue_matcher`) plus the daily OHLC the
simulator already fetches from ORATS.

A weekly iron condor's main failure mode is not "close at expiry
outside the shorts" — it is "spot touched a short strike midweek,
the desk panicked, and closed at the worst possible mark". The Wing
Console's composite score therefore needs a per-placement
distribution of the worst intraweek excursion so we can penalise
placements whose p95 historical excursion blew past the shorts.

Math (deterministic, no assumptions about IV crush):

- For each historical analogue week ``(entry_date, expiry_date,
  entry_close)``:
  ``mae_pct = max(|high - entry|, |entry - low|) / entry * 100``
  where ``high`` / ``low`` are the extreme prints across the hold
  window daily bars.
- Aggregate per-event MAE across the pool into p50/p75/p90/p95 plus
  a ``source`` tag (``daily_ohlc`` / ``open_close_fallback`` /
  ``mixed``).

The wing-console scorer calls :func:`mae_p95_vs_wing_ratio` to turn
the realised % move into a "fraction of wing width" penalty term.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

LOG = logging.getLogger("engine14.mae_proxy")


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class WeeklyMAE:
    """Per-week MAE reading."""

    entry_date:  str = ""
    expiry_date: str = ""
    entry_close: Optional[float] = None
    mae_pct:     Optional[float] = None
    direction:   str = ""
    source:      str = "daily_ohlc"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MAEDistribution:
    """Aggregated MAE distribution across the analogue pool."""

    n:       int = 0
    p50:     float = 0.0
    p75:     float = 0.0
    p90:     float = 0.0
    p95:     float = 0.0
    max:     float = 0.0
    source:  str = "daily_ohlc"
    notes:   List[str] = field(default_factory=list)
    events:  List[WeeklyMAE] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["events"] = [e.to_dict() for e in self.events]
        return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    xs = sorted(values)
    k = (pct / 100.0) * (len(xs) - 1)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return float(xs[lo])
    frac = k - lo
    return float(xs[lo] + (xs[hi] - xs[lo]) * frac)


def _compute_week_mae(
    *,
    entry_close: float,
    bars: Sequence[Any],
) -> Optional[WeeklyMAE]:
    """Compute the worst intraweek excursion vs ``entry_close``.

    ``bars`` is an iterable of anything with ``.high`` / ``.low`` /
    ``.open`` / ``.close`` attributes. When high/low are missing we
    fall back to the bar's open/close as a conservative proxy and tag
    the source as ``open_close_fallback``.
    """
    if not (entry_close and math.isfinite(entry_close) and entry_close > 0):
        return None
    if not bars:
        return None

    worst_up = 0.0
    worst_dn = 0.0
    source = "daily_ohlc"
    had_any = False
    for b in bars:
        hi = _as_float(getattr(b, "high", None))
        lo = _as_float(getattr(b, "low",  None))
        o  = _as_float(getattr(b, "open", None))
        c  = _as_float(getattr(b, "close", None)) or _as_float(getattr(b, "clsPx", None))
        if hi is None or lo is None:
            candidates = [x for x in (o, c) if x is not None]
            if not candidates:
                continue
            hi = max(candidates)
            lo = min(candidates)
            source = "open_close_fallback"
        had_any = True
        if hi > entry_close:
            worst_up = max(worst_up, (hi - entry_close) / entry_close)
        if lo < entry_close:
            worst_dn = max(worst_dn, (entry_close - lo) / entry_close)

    if not had_any:
        return None

    if worst_up == 0.0 and worst_dn == 0.0:
        return WeeklyMAE(entry_close=entry_close, mae_pct=0.0,
                         direction="flat", source=source)
    if worst_up >= worst_dn:
        return WeeklyMAE(entry_close=entry_close, mae_pct=worst_up * 100.0,
                         direction="up", source=source)
    return WeeklyMAE(entry_close=entry_close, mae_pct=worst_dn * 100.0,
                     direction="down", source=source)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_mae_distribution(
    *,
    windows: Iterable[Dict[str, Any]],
    bars_by_date: Dict[str, Any],
) -> MAEDistribution:
    """Compute the intraweek MAE distribution across the analogue pool.

    ``windows`` is an iterable of dicts each carrying at minimum:

    - ``entry_date``   (YYYY-MM-DD)
    - ``expiry_date``  (YYYY-MM-DD)
    - ``entry_close``  (float)

    ``bars_by_date`` maps trade-date strings to :class:`DailyOHLC`-like
    bar objects (the simulator already carries ``ohlc_by_date`` built
    from ORATS dailies — pass it straight through).
    """
    per_week: List[WeeklyMAE] = []
    per_week_values: List[float] = []
    fallback_count = 0

    sorted_dates = sorted(bars_by_date.keys())

    for win in (windows or []):
        ed = str(win.get("entry_date") or win.get("entryDate") or "")[:10]
        xp = str(win.get("expiry_date") or win.get("expiryDate") or "")[:10]
        entry_close = _as_float(
            win.get("entry_close") or win.get("entryClose") or win.get("entryPx")
        )
        if not ed or not xp or entry_close is None:
            continue

        hold_bars: List[Any] = []
        for d in sorted_dates:
            if d <= ed:
                continue
            if d > xp:
                break
            b = bars_by_date.get(d)
            if b is not None:
                hold_bars.append(b)
        if not hold_bars:
            continue

        reading = _compute_week_mae(entry_close=entry_close, bars=hold_bars)
        if reading is None or reading.mae_pct is None:
            continue

        reading.entry_date = ed
        reading.expiry_date = xp
        per_week.append(reading)
        per_week_values.append(float(reading.mae_pct))
        if reading.source == "open_close_fallback":
            fallback_count += 1

    n = len(per_week_values)
    if n == 0:
        return MAEDistribution(
            n=0,
            notes=["mae_pool_empty: no analogue windows resolved OHLC data"],
        )

    dist = MAEDistribution(
        n=n,
        p50=round(_percentile(per_week_values, 50), 3),
        p75=round(_percentile(per_week_values, 75), 3),
        p90=round(_percentile(per_week_values, 90), 3),
        p95=round(_percentile(per_week_values, 95), 3),
        max=round(max(per_week_values), 3),
        events=per_week,
    )

    if fallback_count == 0:
        dist.source = "daily_ohlc"
    elif fallback_count >= n * 0.5:
        dist.source = "open_close_fallback"
        dist.notes.append(
            f"{fallback_count}/{n} weeks used open/close fallback; "
            "p95 under-estimates true intraweek MAE."
        )
    else:
        dist.source = "mixed"
        dist.notes.append(
            f"{fallback_count}/{n} weeks fell back to open/close for MAE."
        )
    return dist


# ---------------------------------------------------------------------------
# Placement-time adapter
# ---------------------------------------------------------------------------


def mae_p95_vs_wing_ratio(
    *,
    mae_p95_pct:       float,
    em_multiple:       float,
    implied_move_pct:  float,
    wing_width_pts:    float,
    spot:              float,
) -> float:
    """Convert the historical p95 MAE into a "fraction of wing width".

    Same math as E2's ``mae_p95_vs_wing_ratio`` (SPX weekly ICs are
    priced in points, not % of EM). At the p95 intraweek print the
    IC's spot is ``entry ± mae_p95_pct%``. Loss vs short strike is
    ``max(0, mae_p95_pct - em_multiple * im_pct) * spot * 0.01``
    points; fraction of wing width = ``loss_pts / wing_width_pts``,
    clamped to ``[0, 1.5]`` so "way past max" becomes a saturating
    penalty rather than runaway score.
    """
    if (
        spot <= 0 or wing_width_pts <= 0 or
        implied_move_pct <= 0 or em_multiple <= 0 or
        not math.isfinite(mae_p95_pct)
    ):
        return 0.0

    pct_past_short = max(0.0, mae_p95_pct - em_multiple * implied_move_pct)
    pts_past_short = pct_past_short * 0.01 * spot
    ratio = pts_past_short / wing_width_pts
    return float(max(0.0, min(1.5, ratio)))
