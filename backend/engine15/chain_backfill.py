"""Engine 15 — per-ticker event-oriented ORATS historical chain backfill.

Instead of Engine 14's continuous ~500 daily SPX slices over a 2-year
window, Engine 15 only pulls the chain slices it actually needs to
replay a ticker's earnings events: for each event, a small bundle of
trading days around ``earnDate`` (default ``earnDate - 1`` through
``earnDate + 2``). That's ~40-80 daily slices per ticker, not 500 — so
the first-run cost of scanning a new name is bounded at seconds-to-tens-
of-seconds of ORATS quota, and every subsequent scan is a pure cache
hit.

Writes are delegated to ``backend.engine14.chain_cache`` — the schema is
already ticker-keyed (primary key ``(trade_date, ticker, expiry,
strike)``), and the SPX restriction was only a router-layer guard. No
new DB / file is introduced.

The core entry point, :func:`backfill_ticker_events`, is idempotent:
dates already in the manifest are skipped (unless ``force=True``), so
calling it repeatedly converges to a fully warmed cache.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from backend.engine14 import chain_cache
from backend.orats_client import OratsClient, OratsError

LOG = logging.getLogger("engine15.chain_backfill")


__all__ = [
    "EventBackfillPlan",
    "EventBackfillResult",
    "plan_event_backfill",
    "backfill_ticker_events",
    "collect_event_dates",
]


@dataclass
class EventBackfillPlan:
    """What the backfill intends to fetch before touching the network."""

    ticker: str
    events: List[Dict[str, Any]] = field(default_factory=list)
    dates_to_fetch: List[str] = field(default_factory=list)
    dates_cached: List[str] = field(default_factory=list)
    dates_skipped_non_trading: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "events": len(self.events),
            "datesToFetch": list(self.dates_to_fetch),
            "datesAlreadyCached": list(self.dates_cached),
            "datesSkippedNonTrading": list(self.dates_skipped_non_trading),
        }


@dataclass
class EventBackfillResult:
    """Final per-date outcome record after the backfill runs."""

    ticker: str
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_already_cached: int = 0
    per_date: List[Dict[str, Any]] = field(default_factory=list)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    max_dte: int = 45

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "attempted": int(self.attempted),
            "succeeded": int(self.succeeded),
            "failed": int(self.failed),
            "skippedAlreadyCached": int(self.skipped_already_cached),
            "perDate": list(self.per_date),
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "maxDte": int(self.max_dte),
        }


def _parse_iso_date(s: Any) -> Optional[dt.date]:
    if s is None:
        return None
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _biz_shift(d: dt.date, days: int) -> dt.date:
    """Shift ``d`` by ``days`` business days.

    v2: NYSE holiday-aware when ``ENGINE15_HOLIDAY_CALENDAR`` is on so
    the backfill plan doesn't schedule ORATS hits on closed sessions.
    Falls back to Mon-Fri when disabled.
    """
    try:
        from backend.config import get_flags
        if bool(getattr(get_flags(), "ENGINE15_HOLIDAY_CALENDAR", True)):
            from backend.engine15.trading_calendar import add_business_days
            return add_business_days(d, int(days))
    except Exception:
        pass
    step = 1 if days >= 0 else -1
    remaining = abs(int(days))
    cur = d
    while cur.weekday() > 4:
        cur += dt.timedelta(days=step)
    while remaining > 0:
        cur += dt.timedelta(days=step)
        if cur.weekday() <= 4:
            remaining -= 1
    return cur


def _event_window(earn_date: dt.date, *, days_before: int, days_after: int) -> List[str]:
    """Business-day window around an earnings date.

    Returns an ascending list of ISO date strings covering
    ``[earn_date - days_before, earn_date + days_after]`` on the
    Mon-Fri calendar. ``earn_date`` itself is always included.
    """
    out: List[str] = []
    for off in range(-int(days_before), int(days_after) + 1):
        d = _biz_shift(earn_date, off)
        out.append(d.isoformat())
    # Dedupe while preserving order.
    seen = set()
    ordered: List[str] = []
    for d in sorted(out):
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered


def collect_event_dates(
    events: Iterable[Dict[str, Any]],
    *,
    days_before: int,
    days_after: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Normalize E1 ``events[]`` into (parsed_events, unique_dates).

    ``events`` is the list emitted by
    ``backend.earnings_logic.compute_breach_stats`` (``earnDate``,
    ``anncTod``, ``timing``, ``impliedMovePct``, etc.). We coerce each
    row into a compact dict and enumerate the union of per-event
    backfill windows as an ascending, deduplicated list of ISO dates.
    """
    parsed: List[Dict[str, Any]] = []
    all_dates: List[str] = []
    for ev in events or []:
        ed = _parse_iso_date(ev.get("earnDate") or ev.get("earn_date") or ev.get("date"))
        if ed is None:
            continue
        window = _event_window(ed, days_before=days_before, days_after=days_after)
        parsed.append({
            "earnDate": ed.isoformat(),
            "anncTod": ev.get("anncTod") or ev.get("timing"),
            "timing": ev.get("timing"),
            "impliedMovePct": ev.get("impliedMovePct"),
            "realizedMovePct": ev.get("realizedMovePct"),
            "signedMovePct": ev.get("signedMovePct"),
            "breach": ev.get("breach"),
            "window": window,
        })
        all_dates.extend(window)
    unique_dates = sorted(set(all_dates))
    return parsed, unique_dates


def plan_event_backfill(
    *,
    ticker: str,
    events: Sequence[Dict[str, Any]],
    days_before: int = 1,
    days_after: int = 2,
    force: bool = False,
) -> EventBackfillPlan:
    """Produce an :class:`EventBackfillPlan` without issuing any network
    calls. Useful for the UI to tell the user ahead of time how many
    ORATS calls a backfill will cost."""
    ticker = str(ticker).upper()
    parsed, unique_dates = collect_event_dates(
        events, days_before=days_before, days_after=days_after,
    )
    to_fetch: List[str] = []
    cached: List[str] = []
    skipped: List[str] = []
    # v2: skip non-trading sessions (weekends + NYSE holidays) eagerly so
    # the backfill plan doesn't schedule ORATS hits on closed days.
    try:
        from backend.config import get_flags
        from backend.engine15.trading_calendar import is_trading_day
        _holiday_aware = bool(getattr(get_flags(), "ENGINE15_HOLIDAY_CALENDAR", True))
    except Exception:
        is_trading_day = None  # type: ignore[assignment]
        _holiday_aware = False

    for d in unique_dates:
        try:
            dd = dt.date.fromisoformat(d)
        except Exception:
            continue
        if _holiday_aware and is_trading_day is not None:
            if not is_trading_day(dd):
                skipped.append(d)
                continue
        elif dd.weekday() > 4:
            skipped.append(d)
            continue
        if not force and chain_cache.has_trade_date(ticker=ticker, trade_date=d):
            cached.append(d)
        else:
            to_fetch.append(d)
    return EventBackfillPlan(
        ticker=ticker,
        events=list(parsed),
        dates_to_fetch=to_fetch,
        dates_cached=cached,
        dates_skipped_non_trading=skipped,
    )


def backfill_ticker_events(
    client: OratsClient,
    *,
    ticker: str,
    earnings_events: Sequence[Dict[str, Any]],
    days_before: int = 1,
    days_after: int = 2,
    max_dte: int = 45,
    delay_ms: int = 200,
    force: bool = False,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Pull and cache ORATS historical chain slices for each event window.

    Parameters
    ----------
    client:
        A warm ``OratsClient`` (``backend.deps.get_client()``).
    ticker:
        Equity ticker (e.g. ``"GE"``).
    earnings_events:
        Slice of ``events[]`` emitted by Engine 1 / ``compute_breach_stats``.
        Each row must have ``earnDate`` (ISO ``YYYY-MM-DD``).
    days_before / days_after:
        Business-day window around each earnings date. Defaults match
        ``ENGINE15_EVENT_BACKFILL_DAYS_BEFORE`` / ``_DAYS_AFTER`` in
        ``config.py``.
    max_dte:
        Cap on ``dte`` sent to ORATS per fetch. 45 keeps the cache lean
        while still covering weekly + monthly expiries the desk uses.
    delay_ms:
        Sleep between successive ORATS fetches to respect rate limits.
    force:
        When True, re-fetch even if the manifest already has the date.
    on_progress:
        Optional callback invoked after each fetch with a dict payload
        suitable for UI display (see implementation).

    Returns the :class:`EventBackfillResult` as a dict.
    """
    ticker = str(ticker).upper()
    plan = plan_event_backfill(
        ticker=ticker, events=earnings_events,
        days_before=days_before, days_after=days_after, force=force,
    )

    res = EventBackfillResult(
        ticker=ticker,
        attempted=0, succeeded=0, failed=0,
        skipped_already_cached=len(plan.dates_cached),
        max_dte=int(max_dte),
        started_at=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
    )

    total_to_fetch = len(plan.dates_to_fetch)
    LOG.info(
        "engine15 backfill %s: events=%d to_fetch=%d cached=%d skipped=%d",
        ticker, len(plan.events), total_to_fetch, len(plan.dates_cached),
        len(plan.dates_skipped_non_trading),
    )

    if on_progress:
        try:
            on_progress({
                "stage": "planned",
                "plan": plan.to_dict(),
                "completed": 0,
                "total": total_to_fetch,
            })
        except Exception:
            pass

    delay_s = max(0.0, float(delay_ms) / 1000.0)

    for idx, td in enumerate(plan.dates_to_fetch, start=1):
        res.attempted += 1
        try:
            rows = chain_cache.fetch_and_cache_day(
                client, ticker=ticker, trade_date=td, max_dte=int(max_dte),
            )
            if rows > 0:
                res.succeeded += 1
                res.per_date.append({"date": td, "rows": int(rows), "status": "ok"})
            else:
                # No rows returned — most likely a non-trading day. Record
                # it in the manifest via the empty upsert inside
                # fetch_and_cache_day so we don't retry on next call.
                res.per_date.append({"date": td, "rows": 0, "status": "empty"})
        except OratsError as e:
            LOG.warning("engine15 backfill: ORATS error %s %s: %s", ticker, td, e)
            res.failed += 1
            res.per_date.append({"date": td, "rows": 0, "status": "error", "error": str(e)})
        except Exception as e:
            LOG.exception("engine15 backfill: unexpected error %s %s", ticker, td)
            res.failed += 1
            res.per_date.append({"date": td, "rows": 0, "status": "error", "error": f"{type(e).__name__}: {e}"})

        if on_progress:
            try:
                on_progress({
                    "stage": "fetching",
                    "completed": idx,
                    "total": total_to_fetch,
                    "lastDate": td,
                    "succeeded": res.succeeded,
                    "failed": res.failed,
                })
            except Exception:
                pass

        if delay_s and idx < total_to_fetch:
            time.sleep(delay_s)

    res.finished_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z"
    out = res.to_dict()
    out["plan"] = plan.to_dict()
    out["coverage"] = chain_cache.cache_coverage(ticker=ticker)
    if on_progress:
        try:
            on_progress({"stage": "done", "result": out})
        except Exception:
            pass
    return out
