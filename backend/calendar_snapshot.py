from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from zoneinfo import ZoneInfo

from backend.earnings_logic import classify_timing
from backend.orats_client import OratsClient
from backend.redis_store import RedisStore
from backend.universe import load_universe_sp500_and_nasdaq100


ET = ZoneInfo("America/New_York")


EARNINGS_SNAPSHOT_KEY = "calendar:earnings_snapshot:v1"
EARNINGS_LAST_REFRESH_ET_DATE_KEY = "calendar:lastRefreshETDate:v1"


def _fmt_date(d: dt.date) -> str:
    return d.isoformat()


def _parse_date(s: Any) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_et() -> dt.datetime:
    return _now_utc().astimezone(ET)


def _to_str(v: Any) -> str:
    return "" if v is None else str(v)


def _snapshot_ttl_s() -> int:
    # Keep enough TTL to tolerate a missed cron run (weekends / deploys).
    return int(float(os.getenv("CALENDAR_EARNINGS_SNAPSHOT_TTL_S") or (48 * 60 * 60)))


def should_refresh_today_et(*, now_et: dt.datetime, last_refresh_et_date: Optional[str]) -> bool:
    """
    True if:
    - current ET time is >= 04:00, and
    - last_refresh_et_date != today
    """
    if now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=ET)
    today = _fmt_date(now_et.date())
    if str(last_refresh_et_date or "")[:10] == today:
        return False
    # 4:00am ET gate
    gate = now_et.replace(hour=4, minute=0, second=0, microsecond=0)
    return now_et >= gate


@dataclass(frozen=True)
class RefreshResult:
    ok: bool
    etDate: str
    universeSize: int
    oratsCalls: int
    rowsUsed: int
    byDateSize: int
    errors: int
    notes: List[str]


def _is_placeholder_date(s: str) -> bool:
    d = str(s or "").strip()
    return d in ("", "0", "0000-00-00", "0000-00-00T00:00:00", "0000-00-00 00:00:00")


def _to_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        x = int(float(v))
        return x
    except Exception:
        return None


def _fetch_next_earnings_from_orats(
    client: OratsClient,
    *,
    ticker: str,
    today: dt.date,
) -> Tuple[Optional[dt.date], str, bool]:
    """
    Return (nextErnDate, timing, estimated) from ORATS /cores.
    timing in AMC|BMO|UNK
    """
    fields = "ticker,nextErn,nextErnTod,daysToNextErn,wksNextErn"
    rows = client.cores(ticker=str(ticker).upper(), fields=fields).rows or []
    row = rows[0] if rows else {}
    if not isinstance(row, dict):
        return None, "UNK", False
    d0 = str(row.get("nextErn") or "")[:10]
    timing = classify_timing(row.get("nextErnTod"))
    if timing not in ("AMC", "BMO"):
        timing = "UNK"

    # Primary: exact date
    if not _is_placeholder_date(d0):
        d = _parse_date(d0)
        if d is not None:
            return d, timing, False

    # Fallback: approximate from days/weeks-to-earnings if date is missing on this ORATS plan.
    days = _to_int(row.get("daysToNextErn"))
    wks = _to_int(row.get("wksNextErn"))
    if (days is None or days <= 0) and (wks is not None and wks > 0):
        days = int(wks) * 7
    if days is not None and days > 0:
        return today + dt.timedelta(days=int(days)), timing, True

    return None, timing, False


def build_earnings_snapshot(
    client: OratsClient,
    *,
    universe: List[str],
    now_et: dt.datetime,
    horizon_days: int = 180,
) -> Dict[str, Any]:
    """
    Build an earnings snapshot payload.
    Only includes dates within [today..today+horizon_days] (ET date).
    """
    today = now_et.date()
    end = today + dt.timedelta(days=int(horizon_days))

    by_date: Dict[str, Dict[str, List[str]]] = {}
    calls = 0
    used = 0
    est_used = 0
    errors = 0
    notes: List[str] = []

    for t in universe:
        sym = str(t or "").strip().upper()
        if not sym:
            continue
        try:
            d, timing, est = _fetch_next_earnings_from_orats(client, ticker=sym, today=today)
            calls += 1
            if not d:
                continue
            if d < today or d > end:
                continue
            k = _fmt_date(d)
            if k not in by_date:
                by_date[k] = {"BMO": [], "AMC": [], "UNK": []}
            by_date[k][timing].append(sym)
            used += 1
            if est:
                est_used += 1
        except Exception:
            errors += 1
            continue

    # stable sorting
    for d0 in list(by_date.keys()):
        for k in ("BMO", "AMC", "UNK"):
            by_date[d0][k] = sorted(list(dict.fromkeys(by_date[d0][k])))

    meta = {
        "refreshedAtUtc": _now_utc().isoformat(),
        "refreshedAtET": now_et.isoformat(),
        "etDate": _fmt_date(today),
        "universeSize": int(len(universe)),
        "oratsCalls": int(calls),
        "rowsUsed": int(used),
        "estimatedDatesUsed": int(est_used),
        "errors": int(errors),
        "horizonDays": int(horizon_days),
        "notes": notes,
    }
    return {"meta": meta, "byDate": by_date}


def refresh_earnings_snapshot_if_needed(
    client: OratsClient,
    store: RedisStore,
    *,
    now_et: Optional[dt.datetime] = None,
    horizon_days: int = 180,
    force: bool = False,
) -> RefreshResult:
    """
    Refresh once per ET day after 4am ET. Safe to run hourly.
    """
    now = now_et or _now_et()
    et_date = _fmt_date(now.date())
    last = store.get_json(EARNINGS_LAST_REFRESH_ET_DATE_KEY)
    last_s = str(last)[:10] if last is not None else None

    if (not force) and (not should_refresh_today_et(now_et=now, last_refresh_et_date=last_s)):
        return RefreshResult(
            ok=True,
            etDate=et_date,
            universeSize=0,
            oratsCalls=0,
            rowsUsed=0,
            byDateSize=0,
            errors=0,
            notes=["No refresh needed (already refreshed today or before 4am ET)."],
        )

    universe = load_universe_sp500_and_nasdaq100()
    snap = build_earnings_snapshot(client, universe=universe, now_et=now, horizon_days=int(horizon_days))
    ttl = _snapshot_ttl_s()
    ok1 = store.set_json(EARNINGS_SNAPSHOT_KEY, snap, ttl_s=ttl)
    ok2 = store.set_json(EARNINGS_LAST_REFRESH_ET_DATE_KEY, et_date, ttl_s=ttl)

    meta = snap.get("meta") if isinstance(snap.get("meta"), dict) else {}
    by_date = snap.get("byDate") if isinstance(snap.get("byDate"), dict) else {}

    return RefreshResult(
        ok=bool(ok1 and ok2),
        etDate=str(meta.get("etDate") or et_date)[:10],
        universeSize=int(meta.get("universeSize") or 0),
        oratsCalls=int(meta.get("oratsCalls") or 0),
        rowsUsed=int(meta.get("rowsUsed") or 0),
        byDateSize=int(len(by_date)),
        errors=int(meta.get("errors") or 0),
        notes=(
            ["Refreshed earnings snapshot (forced)."] if (force and ok1 and ok2) else
            (["Refreshed earnings snapshot."] if (ok1 and ok2) else ["Failed to write snapshot to Redis."])
        ),
    )


def load_earnings_snapshot(store: RedisStore) -> Optional[Dict[str, Any]]:
    snap = store.get_json(EARNINGS_SNAPSHOT_KEY)
    return snap if isinstance(snap, dict) else None


