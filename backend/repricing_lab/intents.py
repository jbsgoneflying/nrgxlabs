"""PositionIntent contract — shared economic-exposure unit for Desk Brain."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class PositionIntent:
    """One engine's requested exposure.

    Aggregation rule (lab Phase 4):
      same (ticker, side) → one economic exposure at max(requested_risk_pct),
      widest stop, attribution preserved. Agreement raises confidence only —
      it does NOT multiply risk.
    """

    ticker: str
    side: str  # long | short
    engine_id: int
    engine_name: str
    requested_risk_pct: float
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    gap_stress: Optional[float] = None
    conviction: float = 50.0
    structure: str = ""
    sleeve: str = ""
    reason_codes: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def risk_per_share(self) -> Optional[float]:
        if self.entry_price is None or self.stop_price is None:
            return None
        structural = abs(float(self.entry_price) - float(self.stop_price))
        gap = float(self.gap_stress or 0.0)
        return max(structural, gap)


@dataclass
class AggregatedIntent:
    ticker: str
    side: str
    risk_pct: float
    stop_price: Optional[float]
    entry_price: Optional[float]
    gap_stress: Optional[float]
    conviction: float
    contributors: List[PositionIntent]
    conflict: bool = False
    conflict_notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "side": self.side,
            "riskPct": self.risk_pct,
            "stopPrice": self.stop_price,
            "entryPrice": self.entry_price,
            "gapStress": self.gap_stress,
            "conviction": self.conviction,
            "conflict": self.conflict,
            "conflictNotes": list(self.conflict_notes),
            "contributors": [
                {
                    "engineId": c.engine_id,
                    "engineName": c.engine_name,
                    "requestedRiskPct": c.requested_risk_pct,
                    "conviction": c.conviction,
                }
                for c in self.contributors
            ],
        }


def merge_intents(
    intents: List[PositionIntent],
    *,
    agreement_bonus_pct: float = 0.0,
) -> List[AggregatedIntent]:
    """Net same-(ticker, side) intents; surface cross-side conflicts."""
    by_key: Dict[tuple, List[PositionIntent]] = {}
    for it in intents:
        key = (it.ticker.upper(), it.side.lower())
        by_key.setdefault(key, []).append(it)

    # Cross-side conflicts
    sides_by_ticker: Dict[str, set] = {}
    for (ticker, side), _ in by_key.items():
        sides_by_ticker.setdefault(ticker, set()).add(side)

    out: List[AggregatedIntent] = []
    for (ticker, side), group in sorted(by_key.items()):
        risk = max(c.requested_risk_pct for c in group)
        if len(group) > 1 and agreement_bonus_pct:
            # Confidence bonus only — capped so risk never exceeds max + bonus
            risk = min(risk + agreement_bonus_pct, risk * 1.0 + agreement_bonus_pct)
        stops = [c.stop_price for c in group if c.stop_price is not None]
        entries = [c.entry_price for c in group if c.entry_price is not None]
        gaps = [c.gap_stress for c in group if c.gap_stress is not None]
        # Widest stop = farthest from entry for the side
        stop = None
        entry = entries[0] if entries else None
        if stops and entry is not None:
            if side == "long":
                stop = min(stops)
            else:
                stop = max(stops)
        elif stops:
            stop = stops[0]
        conflict = len(sides_by_ticker.get(ticker, set())) > 1
        notes = []
        if conflict:
            notes.append(f"{ticker}: opposing side intents present")
        if len(group) > 1:
            notes.append(f"{len(group)} engines agree on {ticker} {side}")
        out.append(AggregatedIntent(
            ticker=ticker,
            side=side,
            risk_pct=float(risk),
            stop_price=stop,
            entry_price=entry,
            gap_stress=max(gaps) if gaps else None,
            conviction=max(c.conviction for c in group),
            contributors=list(group),
            conflict=conflict,
            conflict_notes=notes,
        ))
    return out
