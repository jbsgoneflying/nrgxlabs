"""Transaction-cost model for the research harness.

Costs are decisive for short-horizon strategies (especially residual reversal,
which is high-turnover). We model a per-side cost in basis points that bundles
commission, half the bid/ask spread, and slippage. A round trip pays it twice.

Defaults are deliberately conservative for liquid US large-caps; override per
strategy. Small-caps / illiquid names should use a wider model.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    """Per-side transaction cost in basis points (1 bp = 0.01%).

    ``per_side_bps`` should already bundle commission + half-spread + slippage.
    ``round_trip()`` returns the fractional cost charged once per completed trade
    (entry + exit), so a 10 bp/side model costs 0.0020 (20 bp) round trip.
    """

    per_side_bps: float = 10.0

    def __post_init__(self) -> None:
        if self.per_side_bps < 0:
            raise ValueError("per_side_bps must be non-negative")

    def round_trip(self) -> float:
        """Fractional cost for a full entry+exit cycle."""
        return 2.0 * self.per_side_bps / 10_000.0

    @classmethod
    def liquid_large_cap(cls) -> "CostModel":
        return cls(per_side_bps=8.0)

    @classmethod
    def mid_cap(cls) -> "CostModel":
        return cls(per_side_bps=20.0)

    @classmethod
    def small_cap(cls) -> "CostModel":
        return cls(per_side_bps=45.0)

    @classmethod
    def frictionless(cls) -> "CostModel":
        """For isolating gross edge from cost drag (diagnostics only)."""
        return cls(per_side_bps=0.0)
