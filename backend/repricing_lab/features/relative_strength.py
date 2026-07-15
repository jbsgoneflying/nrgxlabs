"""Relative strength vs SPY / sector proxy."""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from backend.repricing_lab.features.price_trend import _closes, _ret


def compute_relative_strength(
    bars: Sequence[Dict[str, Any]],
    bench_bars: Sequence[Dict[str, Any]],
    *,
    lookback: int = 63,
) -> Dict[str, Optional[float]]:
    c = _closes(bars)
    b = _closes(bench_bars)
    r = _ret(c, lookback)
    rb = _ret(b, lookback)
    if r is None or rb is None:
        return {"rs_vs_spy": None, "ret_bench": None}
    return {"rs_vs_spy": r - rb, "ret_bench": rb}
