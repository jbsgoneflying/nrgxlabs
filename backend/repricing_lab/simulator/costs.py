"""Simulator cost model — extends research CostModel with liquidity tiers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backend.research.cost_model import CostModel


@dataclass(frozen=True)
class LabCostModel:
    """Per-side bps with optional ADV-tier uplift."""

    base: CostModel = CostModel(per_side_bps=10.0)
    illiquid_uplift_bps: float = 15.0
    illiquid_adv_usd: float = 2_000_000.0

    def per_side_bps_for(self, *, adv20_usd: Optional[float] = None) -> float:
        bps = self.base.per_side_bps
        if adv20_usd is not None and adv20_usd < self.illiquid_adv_usd:
            bps += self.illiquid_uplift_bps
        return bps

    def round_trip_fraction(self, *, adv20_usd: Optional[float] = None) -> float:
        return 2.0 * self.per_side_bps_for(adv20_usd=adv20_usd) / 10_000.0

    @classmethod
    def from_research(cls, model: CostModel) -> "LabCostModel":
        return cls(base=model)
