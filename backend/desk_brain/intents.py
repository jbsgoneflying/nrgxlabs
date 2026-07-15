"""Desk Brain adapters for PositionIntent netting (flag-gated)."""
from __future__ import annotations

from typing import List, Optional, Tuple

from backend.desk_brain.aggregator import Opportunity
from backend.repricing_lab.intents import AggregatedIntent, PositionIntent, merge_intents


def opportunity_to_intent(opp: Opportunity, *, default_risk_pct: float = 1.0) -> PositionIntent:
    side = "long"
    d = (opp.direction or "").lower()
    if d in ("short", "sell", "bearish"):
        side = "short"
    raw = opp.raw or {}
    return PositionIntent(
        ticker=str(opp.ticker).upper(),
        side=side,
        engine_id=int(opp.engine_id),
        engine_name=str(opp.engine_name),
        requested_risk_pct=float(default_risk_pct),
        entry_price=_f(raw.get("entry") or raw.get("entryPrice") or raw.get("entry_trigger")),
        stop_price=_f(raw.get("stop") or raw.get("stopLoss") or raw.get("stop_loss")),
        gap_stress=_f(raw.get("gapStress") or raw.get("gap_stress")),
        conviction=float(opp.conviction or 0),
        structure=opp.structure,
        sleeve=opp.sleeve,
        reason_codes=[opp.summary] if opp.summary else [],
        raw=dict(raw),
    )


def merge_opportunities_to_intents(
    opportunities: List[Opportunity],
    *,
    default_risk_pct: float = 1.0,
) -> List[AggregatedIntent]:
    intents = [opportunity_to_intent(o, default_risk_pct=default_risk_pct) for o in opportunities]
    return merge_intents(intents)


def collapse_opportunities_for_allocator(
    opportunities: List[Opportunity],
    *,
    default_risk_pct: float = 1.0,
) -> Tuple[List[Opportunity], List[str]]:
    """Reduce duplicate (ticker, side) opportunities to one representative each.

    Returns (collapsed_opportunities, conflict_notes). Legacy allocator then
    sizes the collapsed set — so risk is not multiplied across engines.
    """
    aggregated = merge_opportunities_to_intents(
        opportunities, default_risk_pct=default_risk_pct,
    )
    # Map back: pick the highest-conviction contributor as the representative Opportunity
    by_key = {(a.ticker, a.side): a for a in aggregated}
    collapsed: List[Opportunity] = []
    seen = set()
    notes: List[str] = []
    for opp in opportunities:
        side = "short" if (opp.direction or "").lower() in ("short", "sell", "bearish") else "long"
        key = (str(opp.ticker).upper(), side)
        if key in seen:
            continue
        seen.add(key)
        agg = by_key.get(key)
        if agg is None:
            collapsed.append(opp)
            continue
        notes.extend(agg.conflict_notes)
        # Prefer highest conviction contributor as the shell Opportunity
        best = max(agg.contributors, key=lambda c: c.conviction)
        # Find matching original opp
        match = next(
            (o for o in opportunities
             if o.engine_id == best.engine_id and str(o.ticker).upper() == agg.ticker),
            opp,
        )
        collapsed.append(match)
    return collapsed, notes


def _f(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None
