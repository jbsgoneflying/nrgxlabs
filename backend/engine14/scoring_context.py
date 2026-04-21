"""Scoring context cache for the E14 Wing Console slider endpoint.

Parallel to :mod:`backend.engine2.scoring_context` but keyed on
``(entry_date, expiry_date, as_of_date)`` so the E14 slider endpoint
can re-score arbitrary ``(em_mult, wing_pts)`` points against the
same analogue pool + MC pool + MAE distribution the primary Wing
Console call built, without re-fetching ORATS dailies or
re-matching analogues.

The primary Wing Console route
(``POST /api/ic-scenario/wing-console``) publishes a
:class:`ScoringContext` under its canonical triple key; the slider
route (``.../score-placement``) reads it back and re-scores just
the requested placement.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from cachetools import TTLCache


@dataclass
class ScoringContext:
    """Snapshot of the inputs :func:`score_single_placement` needs."""

    entry_date:    str = ""
    expiry_date:   str = ""
    as_of_date:    str = ""
    spot:          float = 0.0
    em_pct:        float = 0.0                       # 1σ move in %
    hold_days:     int = 5
    analogue_pool: List[Dict[str, Any]] = field(default_factory=list)
    closes_by_date: Dict[str, float] = field(default_factory=dict)
    mae_dist:      Optional[Dict[str, Any]] = None
    mc_result:     Optional[Dict[str, Any]] = None
    regime_bucket: Optional[str] = None
    macro_bucket:  Optional[str] = None
    regime_mi_v2:  Optional[Dict[str, Any]] = None
    weights:       Dict[str, Any] = field(default_factory=dict)
    flags_fp:      Tuple[Any, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------


_scoring_cache: TTLCache = TTLCache(maxsize=2048, ttl=10 * 60)
_scoring_lock = threading.Lock()


def _context_key(entry_date: str, expiry_date: str, as_of_date: str) -> str:
    return (
        f"{(entry_date or '')[:10]}|"
        f"{(expiry_date or '')[:10]}|"
        f"{(as_of_date or '')[:10]}"
    )


def store_scoring_context(ctx: ScoringContext) -> None:
    """Publish a :class:`ScoringContext` under its canonical triple key."""
    k = _context_key(ctx.entry_date, ctx.expiry_date, ctx.as_of_date)
    with _scoring_lock:
        _scoring_cache[k] = ctx


def get_scoring_context(
    entry_date: str, expiry_date: str, as_of_date: str,
) -> Optional[ScoringContext]:
    """Return the cached context, or ``None`` if expired / never set."""
    k = _context_key(entry_date, expiry_date, as_of_date)
    with _scoring_lock:
        return _scoring_cache.get(k)


def clear_scoring_cache() -> None:
    """Drop everything (tests only)."""
    with _scoring_lock:
        _scoring_cache.clear()


__all__ = [
    "ScoringContext",
    "clear_scoring_cache",
    "get_scoring_context",
    "store_scoring_context",
]
