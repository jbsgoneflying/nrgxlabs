"""Fill semantics: open/close/stop/stop-gap."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class FillResult:
    status: str  # filled|rejected_gap|rejected_missing|expired
    fill_price: Optional[float]
    reason: str = ""


def fill_open(bar: Dict[str, Any], *, side: str, limit: Optional[float] = None) -> FillResult:
    o = bar.get("open")
    if o is None:
        return FillResult("rejected_missing", None, "no_open")
    px = float(o)
    if limit is not None:
        if side == "long" and px > limit:
            return FillResult("expired", None, "limit_not_touched")
        if side == "short" and px < limit:
            return FillResult("expired", None, "limit_not_touched")
    return FillResult("filled", px)


def fill_stop(bar: Dict[str, Any], *, side: str, stop: float) -> FillResult:
    """Intraday stop; gap-through uses open as fill (worse than stop)."""
    o, h, l = bar.get("open"), bar.get("high"), bar.get("low")
    if o is None or h is None or l is None:
        return FillResult("rejected_missing", None, "incomplete_bar")
    o, h, l = float(o), float(h), float(l)
    if side == "long":
        if o < stop:
            return FillResult("filled", o, "gap_through_stop")
        if l <= stop:
            return FillResult("filled", stop, "stop")
        return FillResult("expired", None, "stop_not_hit")
    # short
    if o > stop:
        return FillResult("filled", o, "gap_through_stop")
    if h >= stop:
        return FillResult("filled", stop, "stop")
    return FillResult("expired", None, "stop_not_hit")
