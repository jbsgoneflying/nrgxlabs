"""Open book state for the chronological simulator."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class OpenPosition:
    position_id: str
    candidate_id: str
    instrument_id: str
    side: str
    shares: float
    entry_session: str
    entry_price: float
    stop_price: float
    risk_per_share: float
    notional: float
    sector: Optional[str] = None
    mfe_r: float = 0.0
    mae_r: float = 0.0


@dataclass
class Book:
    cash: float
    positions: Dict[str, OpenPosition] = field(default_factory=dict)
    closed: List[dict] = field(default_factory=list)
    rejected: List[dict] = field(default_factory=list)

    def open_list(self) -> List[dict]:
        return [
            {
                "instrument_id": p.instrument_id,
                "notional": p.notional,
                "sector": p.sector,
            }
            for p in self.positions.values()
        ]
