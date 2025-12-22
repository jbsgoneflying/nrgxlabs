from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.benzinga_client import BenzingaClient
from backend.earnings_calendar import benzinga_next_earnings


def _parse_date(s: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _fmt_date(d: dt.date) -> str:
    return d.isoformat()


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(float(v))
    except Exception:
        return None


def _uniq(seq: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for s in seq:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


@dataclass(frozen=True)
class _Window:
    anchor: Optional[str]
    start: str
    end: str


def _build_window(*, now: dt.date, earn_date_next: Optional[str]) -> _Window:
    anchor_d = _parse_date(earn_date_next) if earn_date_next else None
    if anchor_d is not None:
        # Earnings-week context: include the day before and the day after.
        start = anchor_d - dt.timedelta(days=3)
        end = anchor_d + dt.timedelta(days=1)
        return _Window(anchor=_fmt_date(anchor_d), start=_fmt_date(start), end=_fmt_date(end))

    # No upcoming earnings date available: use "recent risk" window.
    start = now - dt.timedelta(days=3)
    end = now
    return _Window(anchor=None, start=_fmt_date(start), end=_fmt_date(end))


def compute_event_risk_overlay(
    bz: BenzingaClient,
    *,
    ticker: str,
    as_of_date: str,
    now: dt.date,
    earn_date_next: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compute an additive event-risk overlay from Benzinga data.

    Output is deterministic given:
    - as_of_date (for anchoring)
    - now (to pick rolling windows)
    - Benzinga responses (cached in BenzingaClient)
    """
    t = str(ticker).strip().upper()
    asof = str(as_of_date)[:10]
    win = _build_window(now=now, earn_date_next=earn_date_next)

    notes: List[str] = []
    sources: List[str] = []

    # ---- Macro proximity (Economic Calendar) ----
    macro_score = 0.0
    macro_top: List[str] = []
    macro_count = 0
    macro_max_importance = None
    try:
        # Filter client-side to avoid being too strict on country; importance is an integer [0..5].
        econ = bz.calendar_economics(date_from=win.start, date_to=win.end, pagesize=1000, page=0)
        sources.append("benzinga:/calendar/economics")
        rows = econ.rows or []
        # Prefer US high-importance events.
        hi = []
        for r in rows:
            imp = _safe_int(r.get("importance"))
            ctry = str(r.get("country") or "").upper()
            if imp is None:
                continue
            if ctry and ctry not in ("US", "UNITED STATES", "USA"):
                continue
            if imp >= 3:
                hi.append(r)
        macro_count = len(hi)
        macro_max_importance = max((_safe_int(r.get("importance")) or 0) for r in hi) if hi else None
        macro_top = _uniq([f'{str(r.get("date") or "")[:10]} {str(r.get("event_name") or "").strip()}' for r in hi][:5])
        # Score: saturate at 5 high-impact events.
        macro_score = _clamp01(macro_count / 5.0)
    except Exception as e:
        notes.append(f"macroProximity unavailable: {type(e).__name__}: {e}")

    # ---- Headline shock (News + WIIM channel) ----
    headline_score = 0.0
    news_count = 0
    wiim_count = 0
    try:
        news = bz.news(
            tickers=t,
            date_from=_fmt_date(now - dt.timedelta(days=3)),
            date_to=_fmt_date(now),
            page_size=50,
            display_output="headline",
        )
        sources.append("benzinga:/news")
        news_rows = news.rows or []
        news_count = len(news_rows)
    except Exception as e:
        notes.append(f"headlineShock/news unavailable: {type(e).__name__}: {e}")

    try:
        wiim = bz.news(
            tickers=t,
            date_from=_fmt_date(now - dt.timedelta(days=3)),
            date_to=_fmt_date(now),
            channels="WIIM",
            page_size=50,
            display_output="headline",
        )
        sources.append("benzinga:/news?channels=WIIM")
        wiim_rows = wiim.rows or []
        wiim_count = len(wiim_rows)
    except Exception:
        # Not all plans may have WIIM; treat as optional.
        pass

    # Score: 0.5 if any news, +0.5 if any WIIM, capped to 1.0.
    headline_score = _clamp01((0.5 if news_count > 0 else 0.0) + (0.5 if wiim_count > 0 else 0.0))

    # ---- Analyst cluster (Calendar Ratings) ----
    ratings_score = 0.0
    ratings_count = 0
    ratings_actions: List[str] = []
    try:
        rat = bz.calendar_ratings(
            tickers=t,
            date_from=_fmt_date(now - dt.timedelta(days=7)),
            date_to=_fmt_date(now),
            pagesize=1000,
            page=0,
        )
        sources.append("benzinga:/calendar/ratings")
        rows = rat.rows or []
        ratings_count = len(rows)
        ratings_actions = _uniq([str(r.get("action_company") or r.get("action_pt") or "").strip() for r in rows if (r.get("action_company") or r.get("action_pt"))][:5])
        # Score: saturate at 3 ratings in a week.
        ratings_score = _clamp01(ratings_count / 3.0)
    except Exception as e:
        notes.append(f"analystCluster unavailable: {type(e).__name__}: {e}")

    # ---- Options activity (Signals: option_activity) ----
    options_score = 0.0
    options_count = 0
    options_sentiment: List[str] = []
    try:
        oa = bz.signal_option_activity(
            tickers=t,
            date_from=_fmt_date(now - dt.timedelta(days=3)),
            date_to=_fmt_date(now),
            pagesize=1000,
            page=0,
        )
        sources.append("benzinga:/signal/option_activity")
        rows = oa.rows or []
        options_count = len(rows)
        options_sentiment = _uniq([str(r.get("sentiment") or "").strip() for r in rows if r.get("sentiment")][:5])
        # Score: saturate at 5 signals in 3 days.
        options_score = _clamp01(options_count / 5.0)
    except Exception:
        # Optional; not all keys include Signals.
        pass

    # Weighted combination (simple + explainable).
    score01 = _clamp01(0.35 * macro_score + 0.25 * headline_score + 0.25 * ratings_score + 0.15 * options_score)
    label = "LOW" if score01 < 0.33 else "MED" if score01 < 0.66 else "HIGH"

    return {
        "enabled": True,
        "asOfDate": asof,
        "earnDateNext": win.anchor,
        "window": {"start": win.start, "end": win.end},
        "score01": round(float(score01), 3),
        "label": label,
        "components": {
            "macroProximity": {
                "score01": round(float(macro_score), 3),
                "countHighImpactUS": int(macro_count),
                "maxImportance": macro_max_importance,
                "top": macro_top,
            },
            "headlineShock": {
                "score01": round(float(headline_score), 3),
                "newsCount3d": int(news_count),
                "wiimCount3d": int(wiim_count),
            },
            "analystCluster": {
                "score01": round(float(ratings_score), 3),
                "ratingsCount7d": int(ratings_count),
                "actions": ratings_actions,
            },
            "optionsActivity": {
                "score01": round(float(options_score), 3),
                "signalsCount3d": int(options_count),
                "sentiment": options_sentiment,
            },
        },
        "sources": _uniq(sources),
        "notes": notes,
    }


def compute_event_risk_overlay_optional(
    bz: Optional[BenzingaClient],
    *,
    ticker: str,
    as_of_date: str,
    now: dt.date,
    earn_date_next: Optional[str] = None,
) -> Dict[str, Any]:
    if bz is None:
        return {
            "enabled": False,
            "asOfDate": str(as_of_date)[:10],
            "earnDateNext": None if not earn_date_next else str(earn_date_next)[:10],
            "window": None,
            "score01": None,
            "label": None,
            "components": {},
            "sources": [],
            "notes": ["Benzinga unavailable (no client)."],
        }
    return compute_event_risk_overlay(bz, ticker=ticker, as_of_date=as_of_date, now=now, earn_date_next=earn_date_next)


