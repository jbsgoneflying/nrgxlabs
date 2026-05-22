"""Flexible-expiry historical-window builder.

The Friday-locked Engine 2 builds windows as ``entry_dow → Friday of same
week``; that assumption is what we have to break to evaluate a trade like
"Fri 2026-05-22 → Tue 2026-05-26" (Memorial Day spans the weekend).

This module produces ``(entry_date, expiry_date)`` analogue windows that
share the **shape** of the live trade the desk wants to score:

- Same entry weekday (Mon..Fri).
- Same number of trading sessions held between entry and expiry
  (computed via NYSE-aware :func:`engine15.trading_calendar.add_business_days`).
- Within ``calendar_days_tol`` calendar days of the target span — so a
  Fri→Tue across a 3-day weekend (4 calendar days) does not collide with
  a normal Fri→Mon (3 calendar days).

Both entry and expiry must exist as real trading days in the ORATS
``trade_dates`` index passed in. That's how the upstream Friday path
already filters historical windows; we just generalize the anchor.

No ORATS calls are made here — the trade-date list is the fast-path
input, mirroring :func:`backend.spx_ic.weekly_windows.build_weekly_windows_from_trade_dates`.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from backend.engine15.trading_calendar import (
    add_business_days,
    business_days_between,
    is_trading_day,
)
from backend.spx_ic.utils import _fmt_date
from backend.spx_ic.weekly_windows import WeeklyWindow


# Map a calendar-(month, day) pair onto a stable holiday family label so we
# can build "exact-holiday" subsamples (e.g. only Memorial Day analogues for
# a Memorial Day weekend trade). The map is intentionally broad — a holiday
# can land on different weekdays year-over-year (e.g. Christmas) and the
# label needs to stay constant so subsample-matching works. Keys cover the
# observable date range; the lookup is forgiving for dates not in the
# table (they get the generic "NYSE holiday" label).
_HOLIDAY_FAMILY: Dict[Tuple[int, int], str] = {
    (1, 1): "New Year's Day",
    (1, 2): "New Year's Day (observed)",
    (1, 9): "Day of Mourning",
    (1, 15): "MLK Day", (1, 16): "MLK Day", (1, 17): "MLK Day",
    (1, 18): "MLK Day", (1, 19): "MLK Day", (1, 20): "MLK Day", (1, 21): "MLK Day",
    (2, 15): "Presidents' Day", (2, 16): "Presidents' Day", (2, 17): "Presidents' Day",
    (2, 18): "Presidents' Day", (2, 19): "Presidents' Day", (2, 20): "Presidents' Day",
    (2, 21): "Presidents' Day",
    (3, 25): "Good Friday", (3, 26): "Good Friday", (3, 27): "Good Friday",
    (3, 28): "Good Friday", (3, 29): "Good Friday", (3, 30): "Good Friday",
    (3, 31): "Good Friday",
    (4, 1): "Good Friday", (4, 2): "Good Friday", (4, 3): "Good Friday",
    (4, 4): "Good Friday", (4, 5): "Good Friday", (4, 6): "Good Friday",
    (4, 7): "Good Friday", (4, 8): "Good Friday", (4, 9): "Good Friday",
    (4, 10): "Good Friday", (4, 11): "Good Friday", (4, 12): "Good Friday",
    (4, 13): "Good Friday", (4, 14): "Good Friday", (4, 15): "Good Friday",
    (4, 16): "Good Friday", (4, 17): "Good Friday", (4, 18): "Good Friday",
    (4, 19): "Good Friday", (4, 20): "Good Friday", (4, 21): "Good Friday",
    (4, 22): "Good Friday", (4, 23): "Good Friday",
    (5, 25): "Memorial Day", (5, 26): "Memorial Day", (5, 27): "Memorial Day",
    (5, 28): "Memorial Day", (5, 29): "Memorial Day", (5, 30): "Memorial Day",
    (5, 31): "Memorial Day",
    (6, 18): "Juneteenth", (6, 19): "Juneteenth", (6, 20): "Juneteenth",
    (7, 3): "Independence Day", (7, 4): "Independence Day", (7, 5): "Independence Day",
    (9, 1): "Labor Day", (9, 2): "Labor Day", (9, 3): "Labor Day", (9, 4): "Labor Day",
    (9, 5): "Labor Day", (9, 6): "Labor Day", (9, 7): "Labor Day",
    (11, 22): "Thanksgiving", (11, 23): "Thanksgiving", (11, 24): "Thanksgiving",
    (11, 25): "Thanksgiving", (11, 26): "Thanksgiving", (11, 27): "Thanksgiving",
    (11, 28): "Thanksgiving",
    (12, 24): "Christmas", (12, 25): "Christmas", (12, 26): "Christmas",
}


def classify_holiday(d: dt.date) -> str:
    """Return a stable family label for an NYSE-closed weekday.

    The label is what downstream code uses to define an *exact* analogue
    subsample. Unknown dates fall back to ``"NYSE holiday"`` so the
    family stays well-defined even when the holiday table is sparse.
    """
    return _HOLIDAY_FAMILY.get((d.month, d.day)) or "NYSE holiday"


@dataclass(frozen=True)
class FlexWindow:
    """Historical analogue of the requested (entry, expiry) trade shape."""

    entry_date: dt.date
    expiry_date: dt.date
    dte_sessions: int
    dte_calendar_days: int
    spans_holiday: bool
    holiday_label: Optional[str] = None
    holiday_date: Optional[dt.date] = None

    def to_weekly_window(self) -> WeeklyWindow:
        """Adapt to the Friday-engine dataclass so downstream code is interchangeable."""
        return WeeklyWindow(
            entry_date=self.entry_date,
            expiry_date=self.expiry_date,
            dte_sessions=int(self.dte_sessions),
            dte_calendar_days=int(self.dte_calendar_days),
        )


def derive_target_shape(
    *,
    entry_date: dt.date,
    expiry_date: dt.date,
) -> dict:
    """Compute the (sessions, calendar-days, spans-holiday) signature of the live trade.

    Both endpoints are inclusive in the sense that ``entry_date`` is the
    entry close and ``expiry_date`` is the settlement close. Sessions
    counted here are sessions held *after* entry (matching how the
    upstream engine uses ``dte_sessions`` to scale IV in EM math).
    """
    if expiry_date <= entry_date:
        raise ValueError(f"expiry_date ({expiry_date}) must be after entry_date ({entry_date}).")
    sessions = business_days_between(entry_date, expiry_date)
    cal_days = (expiry_date - entry_date).days
    # A "holiday span" means the gap between entry and expiry contains
    # at least one weekday that the NYSE was closed (Memorial Day, July
    # 4, etc.). Used both as a UI hint AND downstream as a subsample
    # filter for "exact-holiday" analogues.
    holiday_hit = _first_holiday_weekday(entry_date, expiry_date)
    spans_holiday = holiday_hit is not None
    holiday_label = classify_holiday(holiday_hit) if holiday_hit else None
    holiday_date = holiday_hit.isoformat() if holiday_hit else None
    return {
        "entryDate": _fmt_date(entry_date),
        "expiryDate": _fmt_date(expiry_date),
        "entryWeekday": entry_date.weekday(),
        "expiryWeekday": expiry_date.weekday(),
        "dteSessions": int(sessions),
        "dteCalendarDays": int(cal_days),
        "spansHoliday": bool(spans_holiday),
        "holidayLabel": holiday_label,
        "holidayDate": holiday_date,
    }


def _first_holiday_weekday(entry_date: dt.date, expiry_date: dt.date) -> Optional[dt.date]:
    """Return the first NYSE-closed weekday in the strict (entry, expiry) gap.

    Pure weekends do not count; we want the "extra" theta days the desk
    is actually trying to harvest. Returns ``None`` when the gap is
    weekends + sessions only.
    """
    d = entry_date + dt.timedelta(days=1)
    while d < expiry_date:
        if d.weekday() < 5 and not is_trading_day(d):
            return d
        d += dt.timedelta(days=1)
    return None


def _gap_contains_full_holiday(entry_date: dt.date, expiry_date: dt.date) -> bool:
    """Back-compat wrapper around :func:`_first_holiday_weekday`."""
    return _first_holiday_weekday(entry_date, expiry_date) is not None


def build_flex_windows(
    *,
    trade_dates: List[str],
    target_entry_weekday: int,
    target_sessions: int,
    target_calendar_days: int,
    years: int = 2,
    today: Optional[dt.date] = None,
    calendar_days_tol: int = 0,
    max_windows: int = 520,
) -> List[FlexWindow]:
    """Build historical (entry, expiry) windows matching the trade shape.

    Args:
        trade_dates: Sorted ORATS ``trade_dates`` (YYYY-MM-DD) — used both
            as the candidate-entry universe and as the "expiry has a bar"
            filter, identical to the upstream fast path.
        target_entry_weekday: ``entry_date.weekday()`` of the live trade
            (0 = Mon, 4 = Fri).
        target_sessions: Trading sessions held between entry and expiry
            in the live trade (1 for Fri→Tue-after-Mem-Day, 3 for normal
            Mon→Thu, 4 for normal Mon→Fri, etc.).
        target_calendar_days: Calendar days held in the live trade.
        years: Lookback horizon. Windows older than ``today - years*365``
            calendar days are excluded.
        today: Anchor for the lookback window (defaults to UTC today).
        calendar_days_tol: ± calendar-day slack on the calendar-day
            match. Default 0 enforces an exact match so a Fri→Mon
            (3 cal days, 1 session) does not collide with a Fri→Tue
            holiday-weekend trade (4 cal days, 1 session). Increase to
            1 to loosen the desk-grade match.
        max_windows: Safety cap on the number of returned windows.

    Returns:
        Sorted (by entry_date) list of :class:`FlexWindow` analogues.
    """
    if not trade_dates:
        return []
    if target_sessions <= 0:
        return []

    today = today or dt.date.today()
    earliest = today - dt.timedelta(days=int(max(years, 1)) * 365)
    date_set = {str(d)[:10] for d in trade_dates if d}

    out: List[FlexWindow] = []
    for d_str in sorted(date_set):
        try:
            entry = dt.date.fromisoformat(d_str)
        except ValueError:
            continue
        if entry < earliest:
            continue
        # Exclude the live trade itself (any candidate >= today): the
        # historical grid is by definition resolved trades only.
        if entry >= today:
            continue
        if entry.weekday() != int(target_entry_weekday):
            continue
        # Skip entries that aren't actual NYSE trading days (a holiday
        # observed Friday, for example) — we cannot enter a trade on
        # a closed session.
        if not is_trading_day(entry):
            continue

        try:
            expiry = add_business_days(entry, int(target_sessions))
        except Exception:
            continue

        cal_days = (expiry - entry).days
        if abs(cal_days - int(target_calendar_days)) > int(calendar_days_tol):
            continue

        # Both endpoints must have ORATS bars (fast-path filter).
        if _fmt_date(entry) not in date_set or _fmt_date(expiry) not in date_set:
            continue

        holiday_hit = _first_holiday_weekday(entry, expiry)
        out.append(
            FlexWindow(
                entry_date=entry,
                expiry_date=expiry,
                dte_sessions=int(target_sessions),
                dte_calendar_days=int(cal_days),
                spans_holiday=bool(holiday_hit is not None),
                holiday_label=classify_holiday(holiday_hit) if holiday_hit else None,
                holiday_date=holiday_hit if holiday_hit else None,
            )
        )
        if len(out) >= int(max_windows):
            break

    out.sort(key=lambda w: w.entry_date)
    return out


def to_weekly_windows(flex_windows: Iterable[FlexWindow]) -> List[WeeklyWindow]:
    """Convenience adapter used by the engine grid loop."""
    return [fw.to_weekly_window() for fw in flex_windows]


__all__ = [
    "FlexWindow",
    "build_flex_windows",
    "classify_holiday",
    "derive_target_shape",
    "to_weekly_windows",
]
