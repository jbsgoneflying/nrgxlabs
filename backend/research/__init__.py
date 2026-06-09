"""NRGX Labs — Edge Bake-Off research harness (OFFLINE).

This package is a standalone, offline research toolkit for validating candidate
trading edges *before* any production engine is built. It is intentionally NOT
imported by `backend/app.py` and is never wired into a live route.

Design principles
-----------------
1. **Decoupled from the network.** All strategy / statistics logic operates on
   plain dataclasses behind small Protocols (see ``data_provider``). This lets the
   entire harness run (and be unit-tested) against in-memory fixtures with zero
   API calls. Real API access lives only in ``live_providers`` and the CLI.
2. **Pure Python.** No numpy / pandas dependency (not installed in this repo);
   all math is stdlib so the harness runs anywhere the app runs.
3. **Point-in-time discipline.** Signals carry the date they were *knowable*;
   entries happen strictly afterwards (default: next session). The event study
   never peeks at data unavailable at signal time.

Two-pass methodology (per the Edge Bake-Off plan)
-------------------------------------------------
- **Pass A** proves the *raw* anomaly is alive out-of-sample (2023+).
- **Pass B** tests whether an LLM grading overlay adds *incremental* edge on the
  survivors — i.e. validates the actual moat, not just the published anomaly.
"""
from __future__ import annotations

from backend.research.cost_model import CostModel
from backend.research.data_provider import (
    EarningsEvent,
    InMemoryEarningsProvider,
    InMemoryInsiderProvider,
    InMemoryPriceProvider,
    InsiderTxn,
    PriceBar,
)
from backend.research.event_study import SignalEvent, TradeResult, run_event_study
from backend.research.cohort_stats import CohortStats, group_by, summarize
from backend.research.splits import decay_by_year, split_in_out
from backend.research.report import build_strategy_report, build_scorecard, write_report

__all__ = [
    "CostModel",
    "PriceBar",
    "EarningsEvent",
    "InsiderTxn",
    "InMemoryPriceProvider",
    "InMemoryEarningsProvider",
    "InMemoryInsiderProvider",
    "SignalEvent",
    "TradeResult",
    "run_event_study",
    "CohortStats",
    "summarize",
    "group_by",
    "split_in_out",
    "decay_by_year",
    "build_strategy_report",
    "build_scorecard",
    "write_report",
]
