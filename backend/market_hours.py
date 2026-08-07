from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from backend.market_calendar import market_structure_events_by_date

_ET = ZoneInfo("America/New_York")
_REGULAR_OPEN_ET = dt.time(9, 30)
_REGULAR_CLOSE_ET = dt.time(16, 0)
_EARLY_CLOSE_ET = dt.time(13, 0)


def _close_time_for_day(day: dt.date) -> dt.time:
    events = market_structure_events_by_date(start=day, end=day).get(day.isoformat(), [])
    for ev in events:
        if isinstance(ev, dict) and str(ev.get("kind") or "").upper() == "HOLIDAY":
            # 00:00 indicates closed all day.
            return dt.time(0, 0)
    for ev in events:
        if isinstance(ev, dict) and str(ev.get("kind") or "").upper() == "EARLY_CLOSE":
            return _EARLY_CLOSE_ET
    return _REGULAR_CLOSE_ET


def minutes_to_close(now_dt: dt.datetime | None = None) -> float | None:
    """Minutes until today's cash close (negative after the close).

    Returns None on weekends/holidays when there is no session at all.
    Early closes (1pm ET) are respected.
    """
    now_et = now_dt.astimezone(_ET) if (now_dt and now_dt.tzinfo is not None) else (now_dt.replace(tzinfo=_ET) if now_dt else dt.datetime.now(tz=_ET))
    day = now_et.date()
    if day.weekday() >= 5:
        return None
    close_t = _close_time_for_day(day)
    if close_t == dt.time(0, 0):
        return None
    close_dt = dt.datetime.combine(day, close_t, tzinfo=_ET)
    return round((close_dt - now_et).total_seconds() / 60.0, 1)


def is_us_equity_market_open(now_dt: dt.datetime | None = None) -> bool:
    """Return True when US cash equity session is open (ET, holiday-aware)."""
    now_et = now_dt.astimezone(_ET) if (now_dt and now_dt.tzinfo is not None) else (now_dt.replace(tzinfo=_ET) if now_dt else dt.datetime.now(tz=_ET))
    day = now_et.date()

    # Weekends are always closed.
    if day.weekday() >= 5:
        return False

    close_t = _close_time_for_day(day)
    # Holiday marker.
    if close_t == dt.time(0, 0):
        return False

    t = now_et.time()
    return (_REGULAR_OPEN_ET <= t) and (t < close_t)

