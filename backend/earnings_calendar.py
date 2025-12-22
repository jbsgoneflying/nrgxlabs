from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional

from backend.benzinga_client import BenzingaClient


def _parse_date(s: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _fmt_date(d: dt.date) -> str:
    return d.isoformat()


def infer_timing_from_time_str(time_str: Any) -> str:
    """
    Benzinga earnings calendar provides `time` as string (HH:MM:SS).
    We map to AMC/BMO/UNK with a conservative heuristic:
      - >= 16:00 -> AMC
      - <= 09:30 -> BMO
      - else UNK
    """
    if time_str is None:
        return "UNK"
    s = str(time_str).strip()
    if not s:
        return "UNK"
    try:
        hh = int(s[:2])
        mm = int(s[3:5])
    except Exception:
        return "UNK"
    minutes = hh * 60 + mm
    if minutes >= 16 * 60:
        return "AMC"
    if minutes <= (9 * 60 + 30):
        return "BMO"
    return "UNK"


@dataclass(frozen=True)
class ResolvedNextEarnings:
    earn_date: str
    timing: str  # AMC|BMO|UNK
    confidence: str  # HIGH|MED|LOW
    source: str  # benzinga
    raw_time: Optional[str] = None
    date_confirmed: Optional[bool] = None


def benzinga_next_earnings(
    bz: BenzingaClient,
    *,
    ticker: str,
    now: dt.date,
    lookahead_days: int = 365,
) -> Optional[ResolvedNextEarnings]:
    """
    Query Benzinga earnings calendar for the nearest upcoming earnings.

    Docs: GET /api/v2/calendar/earnings with parameters[tickers], parameters[date_from], parameters[date_to]
    """
    t = str(ticker).strip().upper()
    if not t:
        return None
    start = now
    end = now + dt.timedelta(days=int(lookahead_days))

    resp = bz.calendar_earnings(
        tickers=t,
        date_from=_fmt_date(start),
        date_to=_fmt_date(end),
        pagesize=1000,
        page=0,
    )
    rows = resp.rows or []
    best_d = None
    best_row = None
    for r in rows:
        d = _parse_date(str(r.get("date") or r.get("earnings_date") or "")[:10])
        if not d:
            continue
        if d < now:
            continue
        if best_d is None or d < best_d:
            best_d = d
            best_row = r
    if best_d is None or best_row is None:
        return None

    raw_time = best_row.get("time")
    timing = infer_timing_from_time_str(raw_time)
    dc = best_row.get("date_confirmed")
    confirmed = None
    if dc is not None:
        s = str(dc).strip()
        if s in ("1", "true", "True", "TRUE", "yes", "Y"):
            confirmed = True
        elif s in ("0", "false", "False", "FALSE", "no", "N"):
            confirmed = False

    # Confidence: confirmed date+time => HIGH, confirmed date only => MED, otherwise LOW.
    conf = "LOW"
    if confirmed is True and timing in ("AMC", "BMO"):
        conf = "HIGH"
    elif confirmed is True:
        conf = "MED"
    elif confirmed is False and timing in ("AMC", "BMO"):
        conf = "MED"

    return ResolvedNextEarnings(
        earn_date=_fmt_date(best_d),
        timing=timing,
        confidence=conf,
        source="benzinga",
        raw_time=None if raw_time is None else str(raw_time),
        date_confirmed=confirmed,
    )


