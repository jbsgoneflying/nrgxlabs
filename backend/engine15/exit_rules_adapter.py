"""Engine 15 — exit-rule optimizer adapter for planned-hold semantics.

Delegates to :func:`backend.engine14.exit_rules.optimize_exit_rules` but:

* Forces a time-stop at the user's planned hold length (the desk never
  holds these past the AM session after earnings).
* Narrows the rule grid via ``max_grid_cells`` so we don't explore
  absurd PT/SL combinations over a 1-2 session window.

The returned payload shape is a superset of Engine 14's with one extra
field, ``recommendedTimeStopDays``, that the UI pins as "time stop" in
the exit-rules card.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from backend.engine14.exit_rules import optimize_exit_rules

LOG = logging.getLogger("engine15.exit_rules_adapter")


__all__ = ["optimize_planned_exit_rules"]


def optimize_planned_exit_rules(
    *,
    paths: List[Any],
    default_profit_target_pct: float,
    default_stop_loss_pct: float,
    planned_hold_days: int,
) -> Dict[str, Any]:
    """Narrow-grid PT/SL optimizer for a hard-capped hold window."""
    if not paths:
        return {
            "recommendedProfitTarget": float(default_profit_target_pct),
            "recommendedStopLoss": float(default_stop_loss_pct),
            "recommendedTimeStopDays": int(max(1, planned_hold_days)),
            "deltaFromDefault": {"winRatePct": 0.0, "avgPnlPct": 0.0},
            "grid": [],
            "gridSize": 0,
            "notes": ["No analogue paths available."],
        }

    base = optimize_exit_rules(
        paths=paths,
        default_profit_target_pct=float(default_profit_target_pct),
        default_stop_loss_pct=float(default_stop_loss_pct),
        extended_grid=False,
        max_grid_cells=16,
    )
    # The planned exit dominates time-stop semantics — override whatever
    # the grid picked with the user's hard cap, so the UI is unambiguous.
    base["recommendedTimeStopDays"] = int(max(1, planned_hold_days))
    base.setdefault("notes", []).append(
        f"Time stop forced to planned hold ({planned_hold_days} biz day"
        f"{'s' if planned_hold_days != 1 else ''}) regardless of grid."
    )
    return base
