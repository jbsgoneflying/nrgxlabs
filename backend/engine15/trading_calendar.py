"""NYSE trading calendar for Engine 15 replay.

The desk runs earnings ICs anchored to specific calendar dates. Mon-Fri
heuristics misclassify US market holidays (Thanksgiving, July 4, etc.)
as trading days, which then poisons the MTM timeline x-axis and the
per-event DTE math. This module provides a deterministic, no-dependency
NYSE calendar covering 2018-2030 — enough runway for the desk's
lookback window (5y) and plenty of room for forward planning.

Public API:

- :func:`is_holiday`, :func:`is_half_day`, :func:`is_trading_day`
- :func:`business_days` — enumerate trading days in a half-open range
- :func:`add_business_days` — shift by N trading days
- :func:`business_days_between` — count trading days in a span

All functions accept :class:`datetime.date` or ``YYYY-MM-DD`` strings.
Holidays are hard-coded from published NYSE calendars; half-days are
early-close 1:00 PM ET sessions (day after Thanksgiving, day before
Independence Day when it falls on a weekday, day before Christmas when
it falls on a weekday).

The calendar auto-emits a warning in :func:`_verify_horizon` when the
caller asks about dates beyond the last calibrated year so the desk
notices the calendar needs an update well before it starts degrading
to the Mon-Fri heuristic silently.
"""
from __future__ import annotations

import datetime as dt
import logging
from functools import lru_cache
from typing import FrozenSet, Iterator, Set, Union

LOG = logging.getLogger("engine15.trading_calendar")

DateLike = Union[dt.date, str]


# ---------------------------------------------------------------------------
# Holiday tables (NYSE / NASDAQ observed)
# ---------------------------------------------------------------------------

# Sources: published NYSE holiday calendars for 2018-2030. When a fixed
# date holiday falls on Saturday it is observed the prior Friday; when
# on Sunday, the following Monday. July 4 2026 falls on Saturday, so
# markets close Friday July 3.

_FULL_HOLIDAYS: FrozenSet[dt.date] = frozenset([
    # 2018
    dt.date(2018, 1, 1),   dt.date(2018, 1, 15),  dt.date(2018, 2, 19),
    dt.date(2018, 3, 30),  dt.date(2018, 5, 28),  dt.date(2018, 7, 4),
    dt.date(2018, 9, 3),   dt.date(2018, 11, 22), dt.date(2018, 12, 5),  # GHW Bush day of mourning
    dt.date(2018, 12, 25),

    # 2019
    dt.date(2019, 1, 1),   dt.date(2019, 1, 21),  dt.date(2019, 2, 18),
    dt.date(2019, 4, 19),  dt.date(2019, 5, 27),  dt.date(2019, 7, 4),
    dt.date(2019, 9, 2),   dt.date(2019, 11, 28), dt.date(2019, 12, 25),

    # 2020
    dt.date(2020, 1, 1),   dt.date(2020, 1, 20),  dt.date(2020, 2, 17),
    dt.date(2020, 4, 10),  dt.date(2020, 5, 25),  dt.date(2020, 7, 3),  # observed
    dt.date(2020, 9, 7),   dt.date(2020, 11, 26), dt.date(2020, 12, 25),

    # 2021
    dt.date(2021, 1, 1),   dt.date(2021, 1, 18),  dt.date(2021, 2, 15),
    dt.date(2021, 4, 2),   dt.date(2021, 5, 31),  dt.date(2021, 7, 5),   # observed
    dt.date(2021, 9, 6),   dt.date(2021, 11, 25), dt.date(2021, 12, 24), # observed

    # 2022
    dt.date(2022, 1, 17),  dt.date(2022, 2, 21),
    dt.date(2022, 4, 15),  dt.date(2022, 5, 30),  dt.date(2022, 6, 20),  # Juneteenth observed
    dt.date(2022, 7, 4),
    dt.date(2022, 9, 5),   dt.date(2022, 11, 24), dt.date(2022, 12, 26), # observed

    # 2023
    dt.date(2023, 1, 2),   dt.date(2023, 1, 16),  dt.date(2023, 2, 20),
    dt.date(2023, 4, 7),   dt.date(2023, 5, 29),  dt.date(2023, 6, 19),
    dt.date(2023, 7, 4),
    dt.date(2023, 9, 4),   dt.date(2023, 11, 23), dt.date(2023, 12, 25),

    # 2024
    dt.date(2024, 1, 1),   dt.date(2024, 1, 15),  dt.date(2024, 2, 19),
    dt.date(2024, 3, 29),  dt.date(2024, 5, 27),  dt.date(2024, 6, 19),
    dt.date(2024, 7, 4),
    dt.date(2024, 9, 2),   dt.date(2024, 11, 28), dt.date(2024, 12, 25),

    # 2025
    dt.date(2025, 1, 1),   dt.date(2025, 1, 9),   # Carter day of mourning
    dt.date(2025, 1, 20),  dt.date(2025, 2, 17),  dt.date(2025, 4, 18),
    dt.date(2025, 5, 26),  dt.date(2025, 6, 19),  dt.date(2025, 7, 4),
    dt.date(2025, 9, 1),   dt.date(2025, 11, 27), dt.date(2025, 12, 25),

    # 2026
    dt.date(2026, 1, 1),   dt.date(2026, 1, 19),  dt.date(2026, 2, 16),
    dt.date(2026, 4, 3),   dt.date(2026, 5, 25),  dt.date(2026, 6, 19),
    dt.date(2026, 7, 3),                            # observed (July 4 is Saturday)
    dt.date(2026, 9, 7),   dt.date(2026, 11, 26), dt.date(2026, 12, 25),

    # 2027
    dt.date(2027, 1, 1),   dt.date(2027, 1, 18),  dt.date(2027, 2, 15),
    dt.date(2027, 3, 26),  dt.date(2027, 5, 31),  dt.date(2027, 6, 18),  # observed (6/19 Sat)
    dt.date(2027, 7, 5),                            # observed (7/4 Sunday)
    dt.date(2027, 9, 6),   dt.date(2027, 11, 25), dt.date(2027, 12, 24), # observed

    # 2028
    dt.date(2028, 1, 17),  dt.date(2028, 2, 21),
    dt.date(2028, 4, 14),  dt.date(2028, 5, 29),  dt.date(2028, 6, 19),
    dt.date(2028, 7, 4),
    dt.date(2028, 9, 4),   dt.date(2028, 11, 23), dt.date(2028, 12, 25),

    # 2029
    dt.date(2029, 1, 1),   dt.date(2029, 1, 15),  dt.date(2029, 2, 19),
    dt.date(2029, 3, 30),  dt.date(2029, 5, 28),  dt.date(2029, 6, 19),
    dt.date(2029, 7, 4),
    dt.date(2029, 9, 3),   dt.date(2029, 11, 22), dt.date(2029, 12, 25),

    # 2030
    dt.date(2030, 1, 1),   dt.date(2030, 1, 21),  dt.date(2030, 2, 18),
    dt.date(2030, 4, 19),  dt.date(2030, 5, 27),  dt.date(2030, 6, 19),
    dt.date(2030, 7, 4),
    dt.date(2030, 9, 2),   dt.date(2030, 11, 28), dt.date(2030, 12, 25),
])

# Half-days: early close at 1:00 PM ET. Day after Thanksgiving is the
# reliable one. Christmas Eve + July 3 are half-days when July 4 / Dec 25
# fall on a weekday. The table below is conservative — it only includes
# confirmed half-days from published calendars.

_HALF_DAYS: FrozenSet[dt.date] = frozenset([
    dt.date(2018, 7, 3),   dt.date(2018, 11, 23), dt.date(2018, 12, 24),
    dt.date(2019, 7, 3),   dt.date(2019, 11, 29), dt.date(2019, 12, 24),
    dt.date(2020, 11, 27), dt.date(2020, 12, 24),
    dt.date(2021, 11, 26),
    dt.date(2022, 11, 25),
    dt.date(2023, 7, 3),   dt.date(2023, 11, 24),
    dt.date(2024, 7, 3),   dt.date(2024, 11, 29), dt.date(2024, 12, 24),
    dt.date(2025, 7, 3),   dt.date(2025, 11, 28), dt.date(2025, 12, 24),
    dt.date(2026, 11, 27), dt.date(2026, 12, 24),
    dt.date(2027, 11, 26),
    dt.date(2028, 7, 3),   dt.date(2028, 11, 24),
    dt.date(2029, 7, 3),   dt.date(2029, 11, 23), dt.date(2029, 12, 24),
    dt.date(2030, 7, 3),   dt.date(2030, 11, 29), dt.date(2030, 12, 24),
])


_LAST_CALIBRATED_YEAR: int = max(d.year for d in _FULL_HOLIDAYS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_date(d: DateLike) -> dt.date:
    if isinstance(d, dt.date):
        return d
    return dt.date.fromisoformat(str(d)[:10])


@lru_cache(maxsize=1)
def _warn_once_beyond_horizon() -> None:
    LOG.warning(
        "trading_calendar: at least one query past %d; calendar will silently "
        "degrade to Mon-Fri heuristic for unknown holidays — update the table.",
        _LAST_CALIBRATED_YEAR,
    )


def _verify_horizon(d: dt.date) -> None:
    if d.year > _LAST_CALIBRATED_YEAR:
        _warn_once_beyond_horizon()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_holiday(d: DateLike) -> bool:
    """Return True for a full-day NYSE close (excludes half-days)."""
    day = _to_date(d)
    _verify_horizon(day)
    return day in _FULL_HOLIDAYS


def is_half_day(d: DateLike) -> bool:
    """Return True when the NYSE closes early (1:00 PM ET)."""
    day = _to_date(d)
    _verify_horizon(day)
    return day in _HALF_DAYS


def is_weekend(d: DateLike) -> bool:
    return _to_date(d).weekday() >= 5


def is_trading_day(d: DateLike) -> bool:
    """True for a full-or-half trading session; False for weekends + full holidays."""
    day = _to_date(d)
    if day.weekday() >= 5:
        return False
    if day in _FULL_HOLIDAYS:
        return False
    return True


def business_days(start: DateLike, end: DateLike, *, inclusive: bool = True) -> Iterator[dt.date]:
    """Yield NYSE trading days in ``[start, end]`` (inclusive by default).

    When ``inclusive=False`` the end date is excluded.
    """
    a = _to_date(start)
    b = _to_date(end)
    if b < a:
        return
    cur = a
    stop = b if inclusive else (b - dt.timedelta(days=1))
    while cur <= stop:
        if is_trading_day(cur):
            yield cur
        cur = cur + dt.timedelta(days=1)


def business_days_between(start: DateLike, end: DateLike) -> int:
    """Count trading days in (start, end] — entry excluded, exit included.

    Mirrors the semantics the old Mon-Fri loops in
    :mod:`backend.engine15.event_universe` + the scenario planned-hold
    computation used (count days after entry, up to and including exit).
    """
    a = _to_date(start)
    b = _to_date(end)
    if b <= a:
        return 0
    return sum(1 for _ in business_days(a + dt.timedelta(days=1), b, inclusive=True))


def add_business_days(start: DateLike, n: int) -> dt.date:
    """Return ``start`` shifted by ``n`` trading days.

    ``n`` may be negative. When ``n == 0`` and ``start`` is itself a
    trading day, ``start`` is returned unchanged; if ``start`` is a
    weekend / holiday, the next trading day is returned.
    """
    cur = _to_date(start)
    if n == 0:
        while not is_trading_day(cur):
            cur = cur + dt.timedelta(days=1)
        return cur
    step = 1 if n > 0 else -1
    remaining = abs(int(n))
    while remaining > 0:
        cur = cur + dt.timedelta(days=step)
        if is_trading_day(cur):
            remaining -= 1
    return cur


def last_calibrated_year() -> int:
    """Return the most recent year for which holidays are hard-coded."""
    return _LAST_CALIBRATED_YEAR


__all__ = [
    "DateLike",
    "add_business_days",
    "business_days",
    "business_days_between",
    "is_half_day",
    "is_holiday",
    "is_trading_day",
    "is_weekend",
    "last_calibrated_year",
]
