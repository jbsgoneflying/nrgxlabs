"""Candidate-edge signal generators for the Edge Bake-Off.

Each module turns raw data (via the provider Protocols) into a list of
``SignalEvent`` objects that the event study can replay. Signal generation is
strictly point-in-time: a signal is anchored to the date it became knowable, and
the harness enters only on the following session.
"""
from __future__ import annotations

from backend.research.strategies.pead import generate_pead_events
from backend.research.strategies.residual_reversal import (
    generate_residual_reversal_events,
)
from backend.research.strategies.insider_cluster import (
    generate_insider_cluster_events,
)

__all__ = [
    "generate_pead_events",
    "generate_residual_reversal_events",
    "generate_insider_cluster_events",
]
