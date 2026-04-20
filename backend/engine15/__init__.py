"""Engine 15 — Earnings IC Scenario Simulator.

Blends Engine 1 (single-name earnings VRP / breach) with Engine 14 style
path-dependent replay. Given a user's prospective iron condor around a
ticker's upcoming earnings, Engine 15:

  1. Runs Engine 1 on the name to harvest VRP / entry-quality / next-event
     metadata (anncTod AMC/BMO, expected move, history of prior events).
  2. Uses this ticker's own last ~20 earnings events as the analogue pool.
  3. On-demand backfills ORATS historical chains for the 2-3 trading days
     around each event into the shared Engine 14 chain cache.
  4. Replays the user's strikes against each analogue event using the
     real chain (reusing ``backend.engine14.chain_replay.reprice_ic``)
     from ``entry_date_hist`` out to ``planned_exit_date_hist`` — NOT to
     expiry. Planned-exit date + time-of-day is a first-class concept.
  5. Aggregates a five-bucket outcome distribution, MTM timeline, exit-rule
     grid, sizing, and earnings-aware conditioning modifiers.

Design tenet: do not fork E14 primitives. The chain cache schema is
already keyed on ``ticker`` (SPX restriction is only at the router layer),
``reprice_ic`` / ``FillModel`` are ticker-agnostic, and the exit-rule
optimizer, sizing, and greeks attribution modules are pure functions
over ``AnaloguePath``-shaped records we reconstruct here.
"""
from __future__ import annotations

__all__ = [
    "run_earnings_scenario",
]


def run_earnings_scenario(*args, **kwargs):
    """Forward to the simulator entrypoint (lazy to avoid import cycles)."""
    from backend.engine15.simulator import run_earnings_scenario as _run
    return _run(*args, **kwargs)
