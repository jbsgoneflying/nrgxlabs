"""Engine 14 — IC Scenario Command Deck.

Path-dependent replay of an iron condor over its life, using historical
ORATS option chains as empirical evidence. v2 adds a Wing Decision
Console on top of the scenario simulator so the desk can rank candidate
placements before committing to a specific 4-strike structure.

v1 flow (preserved): Given a user's proposed IC (short put / long put /
short call / long call + credit + expiry):

  1) Find historical weekly windows with comparable regime/season/macro.
  2) Re-price the user's IC at each day-in-trade using real chain mids
     snapped to EM-distance-equivalent strikes.
  3) Classify each analogue outcome into one of five buckets.
  4) Surface MTM percentile timeline + exit recommendation.

v2 additions:

- :func:`build_wing_console` / :func:`score_placements` rank the grid
  of ``(em_mult, wing_pts)`` candidates BEFORE the desk picks strikes.
- :func:`run_forward_mc` bootstraps the analogue pool into a forward
  Monte Carlo distribution (predictive side-by-side with analogue
  realized).
- :func:`compute_mae_distribution` aggregates the historical intraweek
  MAE so the composite score can penalise "historically blown-through"
  placements.
- :class:`ScoringContext` + :mod:`shared_cache` let the slider endpoint
  re-score arbitrary points without re-fetching ORATS dailies.

Phase 1 scope: SPX only, weeklies only, empirical MTM from day one.
"""

from __future__ import annotations

from backend.engine14.mae_proxy import (
    MAEDistribution,
    compute_mae_distribution,
    mae_p95_vs_wing_ratio,
)
from backend.engine14.mc_simulator import (
    MCPlacementResult,
    MCResult,
    build_mc_pool,
    run_forward_mc,
)
from backend.engine14.scoring_context import (
    ScoringContext,
    clear_scoring_cache,
    get_scoring_context,
    store_scoring_context,
)
from backend.engine14.shared_cache import (
    build_key as build_scenario_cache_key,
    clear as clear_scenario_cache,
    get_or_compute_scenario,
    get_stats_snapshot as get_scenario_cache_stats,
    reset_stats as reset_scenario_cache_stats,
)
from backend.engine14.wing_console import (
    DEFAULT_WEIGHTS,
    PlacementScore,
    WingConsolePayload,
    WingConsoleWeights,
    build_wing_console,
    score_placements,
    score_single_placement,
)


__all__ = [
    "DEFAULT_WEIGHTS",
    "MAEDistribution",
    "MCPlacementResult",
    "MCResult",
    "PlacementScore",
    "ScoringContext",
    "WingConsolePayload",
    "WingConsoleWeights",
    "build_mc_pool",
    "build_scenario_cache_key",
    "build_wing_console",
    "clear_scenario_cache",
    "clear_scoring_cache",
    "compute_mae_distribution",
    "get_or_compute_scenario",
    "get_scenario_cache_stats",
    "get_scoring_context",
    "mae_p95_vs_wing_ratio",
    "reset_scenario_cache_stats",
    "run_forward_mc",
    "score_placements",
    "score_single_placement",
    "simulate_ic_scenario",
    "store_scoring_context",
]


def simulate_ic_scenario(*args, **kwargs):
    """Forward to the simulator.run_scenario entrypoint (lazy import)."""
    from backend.engine14.simulator import run_scenario
    return run_scenario(*args, **kwargs)
