from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

from cachetools import TTLCache

from backend.orats_client import OratsClient


# 6h TTL to match /api/breach caching cadence
_skew_cache: TTLCache = TTLCache(maxsize=50_000, ttl=6 * 60 * 60)
_skew_cache_lock = threading.Lock()


def _cache_get(key: Tuple[Any, ...]) -> Optional[dict]:
    with _skew_cache_lock:
        return _skew_cache.get(key)


def _cache_set(key: Tuple[Any, ...], value: dict) -> None:
    with _skew_cache_lock:
        _skew_cache[key] = value


def _missing_snapshot(*, as_of_date: str, target_dte: int, notes: str) -> Dict[str, Any]:
    return {
        "asOfDate": str(as_of_date)[:10],
        "targetDTE": int(target_dte),
        "rr25": None,
        "rr10": None,
        "bf25": None,
        "skewQuality": "MISSING",
        "notes": notes,
    }


def compute_skew_snapshot(
    client: OratsClient,
    *,
    ticker: str,
    as_of_date: str,
    target_dte: int = 2,
) -> Dict[str, Any]:
    """
    Compute a SkewSnapshot for a given ticker/date.

    Degraded-mode safe: if ORATS skew is unavailable, returns skewQuality="MISSING" with notes.
    """
    key = ("skew", ticker.upper(), str(as_of_date)[:10], int(target_dte))
    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        pts = client.get_skew_by_delta(ticker=ticker.upper(), trade_date=str(as_of_date)[:10], dte_target=int(target_dte), deltas=[10, 25], rights=["C", "P"])

        c25 = pts.get(("C", 25))
        p25 = pts.get(("P", 25))
        c10 = pts.get(("C", 10))
        p10 = pts.get(("P", 10))
        atm = pts.get("atm")

        rr25 = (float(c25) - float(p25)) if c25 is not None and p25 is not None else None
        rr10 = (float(c10) - float(p10)) if c10 is not None and p10 is not None else None
        bf25 = (0.5 * (float(c25) + float(p25)) - float(atm)) if (c25 is not None and p25 is not None and atm is not None) else None

        quality = "OK" if rr25 is not None else ("PARTIAL" if (rr10 is not None or bf25 is not None) else "MISSING")
        notes = ""
        if quality != "OK":
            missing = []
            if rr25 is None:
                missing.append("rr25")
            if rr10 is None:
                missing.append("rr10")
            if bf25 is None:
                missing.append("bf25")
            notes = f"Skew partial: missing {', '.join(missing)}."

        out = {
            "asOfDate": str(pts.get("asOfDate") or str(as_of_date)[:10])[:10],
            "targetDTE": int(target_dte),
            "rr25": None if rr25 is None else round(rr25, 4),
            "rr10": None if rr10 is None else round(rr10, 4),
            "bf25": None if bf25 is None else round(bf25, 4),
            "skewQuality": quality,
            "notes": notes,
        }
    except Exception as e:
        out = _missing_snapshot(
            as_of_date=str(as_of_date)[:10],
            target_dte=int(target_dte),
            notes=f"Skew unavailable in this build ({type(e).__name__}: {e}).",
        )

    _cache_set(key, out)
    return out


def compute_skew_overlay(
    client: OratsClient,
    *,
    ticker: str,
    current_as_of_date: str,
    events: List[Dict[str, Any]],
    target_dte: int = 2,
) -> Dict[str, Any]:
    """
    Compute skew overlay:
      - current snapshot (as-of current_as_of_date)
      - atEvents snapshots keyed by pricingDateUsed (no lookahead; date is pricingDateUsed)
    """
    current = compute_skew_snapshot(client, ticker=ticker, as_of_date=current_as_of_date, target_dte=target_dte)

    at_events: Dict[str, Any] = {}
    for e in events:
        d = e.get("pricingDateUsed")
        if not d:
            continue
        dd = str(d)[:10]
        # Retry/backoff logic would be implemented here when skew endpoint exists.
        at_events[dd] = compute_skew_snapshot(client, ticker=ticker, as_of_date=dd, target_dte=target_dte)

    return {"current": current, "atEvents": at_events}

