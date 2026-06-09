"""Train / out-of-sample splits and yearly decay analysis.

The desk's core fear is "looks great historically, dead in 2026." Two tools
answer it:

  * ``split_in_out`` — split trades at a cutoff (default 2023-01-01) into an
    in-sample (development) set and an out-of-sample set. An edge that only
    exists in-sample is overfit; we rank on the OOS slice.
  * ``decay_by_year`` — per-year cohort stats so you can *see* whether the edge
    is fading, stable, or strengthening (and specifically whether 2024-2026 is
    still positive).
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from backend.research.cohort_stats import CohortStats, summarize
from backend.research.event_study import TradeResult


def split_in_out(
    results: List[TradeResult],
    oos_start: str = "2023-01-01",
) -> Tuple[List[TradeResult], List[TradeResult]]:
    """Partition trades by entry date into (in_sample, out_of_sample).

    A trade belongs to OOS when its entry date is on/after ``oos_start``.
    """
    in_sample = [r for r in results if r.entry_date < oos_start]
    oos = [r for r in results if r.entry_date >= oos_start]
    return in_sample, oos


def decay_by_year(results: List[TradeResult]) -> Dict[str, CohortStats]:
    """Cohort stats per entry-year, ordered ascending."""
    buckets: Dict[int, List[TradeResult]] = {}
    for r in results:
        buckets.setdefault(r.year, []).append(r)
    return {str(y): summarize(buckets[y]) for y in sorted(buckets.keys())}


def split_summary(
    results: List[TradeResult],
    oos_start: str = "2023-01-01",
) -> Dict[str, CohortStats]:
    """Convenience: {'full','in_sample','out_of_sample'} cohort stats."""
    in_s, oos = split_in_out(results, oos_start)
    return {
        "full": summarize(results),
        "in_sample": summarize(in_s),
        "out_of_sample": summarize(oos),
    }
