"""Event / fundamental features (earnings surprise, timing)."""
from __future__ import annotations

from typing import Any, Dict, Optional


def compute_event_fundamental(earnings: Optional[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    if not earnings:
        return {
            "eps_surprise": None,
            "estimate_is_pit": None,
            "earnings_timing_bmo": None,
        }
    actual = earnings.get("eps_actual")
    estimate = earnings.get("eps_estimate")
    surprise = None
    if actual is not None and estimate not in (None, 0):
        try:
            surprise = (float(actual) - float(estimate)) / abs(float(estimate))
        except (TypeError, ValueError, ZeroDivisionError):
            surprise = None
    timing = str(earnings.get("timing") or "")
    return {
        "eps_surprise": surprise,
        "estimate_is_pit": float(earnings.get("estimate_is_pit") or 0),
        "earnings_timing_bmo": 1.0 if timing == "bmo" else 0.0,
    }
