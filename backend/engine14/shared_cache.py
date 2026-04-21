"""Command Deck cache — dedupes scenario + Wing Console + reconcile paths.

Parallel to :mod:`backend.engine1.shared_cache` /
:mod:`backend.engine2.shared_cache`. A single E14 scan can fire
against three routes in succession:

1. ``POST /api/ic-scenario/wing-console`` — ranked placement grid.
2. ``POST /api/ic-scenario``              — scenario drilldown.
3. ``POST /api/ic-scenario/reconcile``    — E2 reconciliation.

All three read from the same :class:`TTLCache` keyed on
``(entry_date, expiry_date, strikes_fp, credit, flags_fp)`` so the
desk pays the (expensive) analogue-match + chain-replay cost once
per scenario per trading day.

5-minute TTL balances:

- Long enough for a scan → console → scenario → reconcile chain.
- Short enough that intraday EM drift / macro calendar refreshes
  pick up fresh conditioning mid-session.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any, Callable, Dict, Optional, Tuple

from cachetools import TTLCache

LOG = logging.getLogger("engine14.shared_cache")


# ---------------------------------------------------------------------------
# Cache + stats
# ---------------------------------------------------------------------------


_SCENARIO_CACHE: TTLCache = TTLCache(maxsize=512, ttl=5 * 60)
_SCENARIO_LOCK = threading.Lock()

_STATS: Dict[str, int] = {"hits": 0, "misses": 0, "stores": 0, "busts": 0}
_STATS_LOCK = threading.Lock()


def _stats_bump(kind: str) -> None:
    with _STATS_LOCK:
        _STATS[kind] = int(_STATS.get(kind, 0)) + 1


def get_stats_snapshot() -> Dict[str, int]:
    """Return a copy of the hit/miss counters."""
    with _STATS_LOCK:
        return dict(_STATS)


def reset_stats() -> None:
    """Zero the counters (tests)."""
    with _STATS_LOCK:
        for k in list(_STATS.keys()):
            _STATS[k] = 0


def clear() -> None:
    """Drop all cached scenarios (tests + admin)."""
    with _SCENARIO_LOCK:
        _SCENARIO_CACHE.clear()
    _stats_bump("busts")


def _fingerprint(obj: Any) -> str:
    try:
        blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        blob = repr(obj)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def build_key(
    *,
    entry_date:  str,
    expiry_date: str,
    strikes:     Optional[Tuple[float, float, float, float]] = None,
    credit:      Optional[float] = None,
    extra:       Optional[Dict[str, Any]] = None,
    flags_fp:    Tuple[Any, ...] = (),
) -> str:
    """Canonical cache-key builder."""
    sf = _fingerprint(list(strikes or ()) or [])
    cf = _fingerprint(round(float(credit or 0.0), 4))
    xf = _fingerprint(extra or {})
    ff = _fingerprint(list(flags_fp) or [])
    return (
        f"{(entry_date or '')[:10]}|"
        f"{(expiry_date or '')[:10]}|"
        f"{sf}|{cf}|{xf}|{ff}"
    )


def get_or_compute_scenario(
    *,
    entry_date:  str,
    expiry_date: str,
    strikes:     Optional[Tuple[float, float, float, float]] = None,
    credit:      Optional[float] = None,
    extra:       Optional[Dict[str, Any]] = None,
    flags_fp:    Tuple[Any, ...] = (),
    compute:     Callable[[], Dict[str, Any]],
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Cache-first accessor for an enriched scenario payload.

    ``compute`` is the zero-arg callable that runs
    :func:`backend.engine14.simulate_ic_scenario` (or the Wing
    Console builder) on cache miss.
    """
    key = build_key(
        entry_date=entry_date, expiry_date=expiry_date,
        strikes=strikes, credit=credit, extra=extra, flags_fp=flags_fp,
    )
    if not force_refresh:
        with _SCENARIO_LOCK:
            hit = _SCENARIO_CACHE.get(key)
        if hit is not None:
            _stats_bump("hits")
            return hit

    _stats_bump("misses")
    payload = compute()
    with _SCENARIO_LOCK:
        _SCENARIO_CACHE[key] = payload
    _stats_bump("stores")
    return payload


__all__ = [
    "build_key",
    "clear",
    "get_or_compute_scenario",
    "get_stats_snapshot",
    "reset_stats",
]
