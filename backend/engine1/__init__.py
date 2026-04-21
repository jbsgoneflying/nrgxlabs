"""Engine 1 v2 — Wing Decision Console.

Transforms the legacy "should I take this trade?" verdict machine into a
"where do I place my wings?" decision console. The desk is assumed to be
taking the trade; this engine tells them HOW to position for maximum
theta capture without breach-gap, breach-CTC, or White-Knuckle (MAE)
risk forcing an early exit.

Public entry points:

- :func:`score_placements` — fit a grid of (EM-multiple, wing-width)
  candidates, return ranked :class:`PlacementScore` objects plus the
  scoring context used.
- :func:`build_wing_console` — full pipeline: given a breach-stats
  payload + (ticker, event_date, event_timing), produce a
  :class:`WingConsolePayload` ready for the frontend / LLM advisor.

The scoring math is deterministic, cacheable, and auditable. The LLM
advisor (``POST /api/breach/advisor``) can still ride on top as an
on-demand narrative layer — this engine is the source of truth.
"""
from __future__ import annotations

from backend.engine1.mae_proxy import (
    MAEDistribution,
    compute_mae_distribution,
    mae_percentile_to_credit_pct,
)
from backend.engine1.theta_capture import (
    ThetaCaptureReading,
    estimate_theta_capture,
)
from backend.engine1.shared_cache import (
    clear as clear_shared_cache,
    get_or_compute_breach_stats,
    get_stats_snapshot as get_shared_cache_stats,
    reset_stats as reset_shared_cache_stats,
)
from backend.engine1.wing_console import (
    DEFAULT_WEIGHTS,
    PlacementScore,
    ScoringContext,
    WingConsolePayload,
    WingConsoleWeights,
    build_wing_console,
    get_scoring_context,
    score_placements,
    score_single_placement,
    store_scoring_context,
)

__all__ = [
    "DEFAULT_WEIGHTS",
    "MAEDistribution",
    "PlacementScore",
    "ScoringContext",
    "ThetaCaptureReading",
    "WingConsolePayload",
    "WingConsoleWeights",
    "build_wing_console",
    "clear_shared_cache",
    "compute_mae_distribution",
    "estimate_theta_capture",
    "get_or_compute_breach_stats",
    "get_scoring_context",
    "get_shared_cache_stats",
    "mae_percentile_to_credit_pct",
    "reset_shared_cache_stats",
    "score_placements",
    "score_single_placement",
    "store_scoring_context",
]
