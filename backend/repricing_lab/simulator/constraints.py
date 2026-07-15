"""Portfolio constraint checks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class ConstraintConfig:
    max_positions: int = 20
    sector_cap_pct: float = 25.0
    max_gross_pct: float = 100.0
    account_size: float = 25_000.0


@dataclass
class Reject:
    code: str
    message: str


def check_entry(
    *,
    open_positions: List[dict],
    instrument_id: str,
    sector: Optional[str],
    notional: float,
    config: ConstraintConfig,
) -> Optional[Reject]:
    if len(open_positions) >= config.max_positions:
        return Reject("max_positions", f"already {len(open_positions)} open")
    # Duplicate ticker → reject new independent risk (netting is an intents concern)
    if any(p.get("instrument_id") == instrument_id for p in open_positions):
        return Reject("duplicate_ticker", instrument_id)
    gross = sum(float(p.get("notional") or 0) for p in open_positions) + notional
    if config.account_size > 0 and (gross / config.account_size) * 100 > config.max_gross_pct:
        return Reject("gross_cap", f"gross={gross:.0f}")
    if sector:
        sector_n = sum(
            float(p.get("notional") or 0)
            for p in open_positions
            if p.get("sector") == sector
        ) + notional
        if config.account_size > 0 and (sector_n / config.account_size) * 100 > config.sector_cap_pct:
            return Reject("sector_cap", sector)
    return None
