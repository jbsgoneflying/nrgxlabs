"""Hedge Sizer — institutional-grade tail-hedge sizing for short-premium positions.

Built originally for the flex-expiry holiday-weekend iron condor desk,
but intentionally position-agnostic so the same math powers any short-
premium book:

- Engine 2 / 2b iron condors (multi-leg defined-risk shorts).
- Engine 1 earnings short straddles / strangles needing cheap tail
  protection against earnings pops above the expected-move range.
- Any naked / covered short premium structure with a defined per-
  contract max loss.

Core idea: a tail hedge is a LOTTERY TICKET, not insurance. One long
single OTM option pays unbounded intrinsic past its strike and can
recoup the loss on MANY short premium contracts at a stress gap. The
right hedge ratio is the one that caps total position loss at a
target dollar amount — NOT a mechanical 1:1.

Sizing algorithm:
  1. Caller supplies the short position (count + max loss per contract).
  2. Caller picks a "stress gap" the hedge is designed to cap
     (e.g. +3% / -3% for an Iran-class weekend gap, or +12% / -12%
     for an earnings-pop tail on a single name).
  3. Caller picks a max-loss target as a % of unhedged max loss
     (50% / 33% / 20% are the standard institutional tiers).
  4. Module computes integer hedge counts so that at the stress gap
     the long hedge intrinsic + short position max loss <= target.

Three pre-built tiers are returned plus an asymmetric tier the caller
can parameterize independently for upside vs downside.

All dollar values are post-multiplier (per-contract × 100 for equity
options; the multiplier is configurable).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShortPosition:
    """Describes the short-premium position being hedged.

    Args:
        contracts: Number of short contracts.
        max_loss_per_contract: Dollar max loss per contract (already
            multiplied — e.g. $4.50 wing − $0.50 credit = $4.50, then
            × 100 multiplier = $450 per contract).
        credit_per_contract: Dollar credit per contract (post-multiplier).
        label: Human-readable description.
    """
    contracts: int
    max_loss_per_contract: float
    credit_per_contract: float
    label: str = ""


@dataclass(frozen=True)
class HedgeStrike:
    """A single OTM long option used as a tail hedge.

    Args:
        strike: Strike price in underlying points.
        side: ``"call"`` (upside tail) or ``"put"`` (downside tail).
        mid_price: Live bid/ask mid for the option in dollars
            (pre-multiplier). ``None`` if unavailable — caller can fall
            back to a default.
        distance_pct: Signed % distance from spot (positive for calls,
            negative for puts). Used for telemetry / UI labels.
    """
    strike: float
    side: str
    mid_price: Optional[float]
    distance_pct: float


# ---------------------------------------------------------------------------
# Sizing math
# ---------------------------------------------------------------------------

def _ceil_int(x: float) -> int:
    """Ceiling helper that returns 0 for non-positive input."""
    if x is None or x <= 0:
        return 0
    return int(math.ceil(x))


def _intrinsic_at_stress(
    *,
    strike: float,
    side: str,
    spot: float,
    stress_gap_pct: float,
    multiplier: float,
) -> float:
    """Compute the intrinsic dollar value of a single long option at the stress gap.

    Always evaluates at the side-appropriate stress spot:
      - call strike: spot × (1 + |gap|/100), gain = max(0, stress - strike)
      - put  strike: spot × (1 − |gap|/100), gain = max(0, strike - stress)

    The caller passes the absolute gap magnitude; this helper applies
    the side-appropriate sign internally.
    """
    g = abs(float(stress_gap_pct)) / 100.0
    if side == "call":
        stress_spot = float(spot) * (1.0 + g)
        intrinsic_points = max(0.0, stress_spot - float(strike))
    elif side == "put":
        stress_spot = float(spot) * (1.0 - g)
        intrinsic_points = max(0.0, float(strike) - stress_spot)
    else:
        return 0.0
    return float(intrinsic_points) * float(multiplier)


def _hedge_count_for_target(
    *,
    target_recoup_dollars: float,
    intrinsic_per_contract: float,
) -> int:
    """How many hedge contracts are needed to recoup ``target_recoup_dollars`` at stress?

    Uses integer ceiling — we never half-hedge. If the per-contract
    intrinsic is zero (strike not ITM at the stress gap), returns 0
    because no quantity can recoup; the caller surfaces this as a
    "stress gap too small for strike" warning.
    """
    if intrinsic_per_contract is None or intrinsic_per_contract <= 0:
        return 0
    return _ceil_int(target_recoup_dollars / float(intrinsic_per_contract))


def _scenario_pnl(
    *,
    short_position: ShortPosition,
    hedge_call: HedgeStrike,
    hedge_put: HedgeStrike,
    calls: int,
    puts: int,
    spot: float,
    gap_pcts: List[float],
    multiplier: float,
    fallback_mid: float,
) -> List[Dict[str, Any]]:
    """Build a per-gap P&L scenario row used by the UI table.

    For each gap %, computes:
      - short position loss at that gap (capped at max_loss × contracts)
      - hedge intrinsic at that gap (long call if up, long put if down)
      - net P&L = -short_loss + hedge_intrinsic - hedge_premium_paid

    Returns sorted by gap %.
    """
    call_mid = hedge_call.mid_price if hedge_call.mid_price is not None else float(fallback_mid)
    put_mid = hedge_put.mid_price if hedge_put.mid_price is not None else float(fallback_mid)
    hedge_premium = (calls * call_mid + puts * put_mid) * float(multiplier)
    credit_dollars = short_position.contracts * short_position.credit_per_contract * float(multiplier)
    net_credit = credit_dollars - hedge_premium

    out: List[Dict[str, Any]] = []
    for g in sorted(gap_pcts):
        gv = float(g)
        # Short position P&L at the gap. The naked IC loss profile is
        # already encoded in max_loss_per_contract; for the scenario
        # table we treat any |gap| big enough to breach the short
        # strike as full max loss. This is a deliberate simplification:
        # the goal of the table is decision support at stress points,
        # not Greeks-accurate mid-day MTM.
        target_spot = float(spot) * (1.0 + gv / 100.0)
        short_loss_dollars = 0.0
        # If the gap is positive and large enough to threaten the call
        # side (or short straddle / strangle equivalent for E1), assume
        # full max loss. Same logic for puts on the down side. For
        # small gaps inside the structure we assume credit is kept
        # (this is reference math, not exact mid-day MTM).
        if abs(gv) >= 1.5:  # threshold = roughly the EM × ~1.5 boundary; tunable per use case
            short_loss_dollars = short_position.contracts * short_position.max_loss_per_contract * float(multiplier)
        # Hedge intrinsic at this gap. Only the side-appropriate hedge
        # contributes (long calls on +gap, long puts on -gap).
        hedge_intrinsic = 0.0
        if gv > 0 and calls > 0:
            hedge_intrinsic = calls * max(0.0, target_spot - hedge_call.strike) * float(multiplier)
        elif gv < 0 and puts > 0:
            hedge_intrinsic = puts * max(0.0, hedge_put.strike - target_spot) * float(multiplier)
        # Realized net P&L at the gap = credit kept (or lost via max loss)
        # + hedge intrinsic at that gap − hedge premium paid up-front.
        if abs(gv) < 1.5:
            # Inside the IC structure: full credit retained, hedges
            # likely worthless (premium decayed to zero by expiry).
            net = credit_dollars - hedge_premium
        else:
            net = hedge_intrinsic - short_loss_dollars - hedge_premium

        out.append({
            "gapPct": round(gv, 2),
            "targetSpot": round(target_spot, 2),
            "shortLoss": round(short_loss_dollars, 2),
            "hedgeIntrinsic": round(hedge_intrinsic, 2),
            "netPnl": round(net, 2),
        })
    return out


def compute_hedge_sizing(
    *,
    short_position: ShortPosition,
    hedge_call: HedgeStrike,
    hedge_put: HedgeStrike,
    spot: float,
    stress_gap_pct: float = 3.0,
    target_caps_pct: Optional[List[float]] = None,
    asymmetric: Optional[Dict[str, float]] = None,
    multiplier: float = 100.0,
    fallback_mid: float = 0.05,
    scenario_gap_pcts: Optional[List[float]] = None,
) -> Dict[str, Any]:
    """Compute hedge tiers for the supplied short position.

    Args:
        short_position: The short-premium position being hedged.
        hedge_call: Long call hedge strike (upside tail protection).
        hedge_put: Long put hedge strike (downside tail protection).
        spot: Current spot price of the underlying.
        stress_gap_pct: Gap % at which the hedge sizing is calibrated
            (e.g. ``3.0`` for Iran-class weekend gap, ``12.0`` for an
            earnings-pop tail). The same magnitude is used for both
            directions; supply ``asymmetric`` to vary by side.
        target_caps_pct: List of max-loss caps (as % of unhedged max
            loss) for which to build pre-named tiers. Defaults to
            ``[50.0, 33.0, 20.0]`` — Lottery / Defined / Conservative.
        asymmetric: Optional ``{"upStress": X, "downStress": Y, "upCap": A, "downCap": B}``
            override for desks with directional conviction. When
            present, an extra ``"Asymmetric"`` tier is appended with
            independent sizing per side.
        multiplier: Contract multiplier (default 100 for equity options).
        fallback_mid: Default mid to use when a hedge strike's
            ``mid_price`` is ``None``. Default ``0.05`` is a reasonable
            estimate for ~3% OTM SPX 4-DTE singles.
        scenario_gap_pcts: Gaps used in the scenario P&L table
            (defaults to ``[-5, -3, -2, -1, 0, +1, +2, +3, +5]``).

    Returns a dict shaped for both the engine payload and direct UI
    rendering. The frontend can override ``short_position.contracts``
    or any tier param by recomputing in JS using the same math — see
    ``hedgeSizer`` in ``static/spx.js``.
    """
    if short_position.contracts <= 0:
        return {
            "enabled": False,
            "notes": ["Short position has 0 contracts; nothing to size."],
        }

    caps = list(target_caps_pct) if target_caps_pct else [50.0, 33.0, 20.0]
    gaps = list(scenario_gap_pcts) if scenario_gap_pcts else [-5.0, -3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0, 5.0]

    call_intrinsic = _intrinsic_at_stress(
        strike=hedge_call.strike, side="call",
        spot=spot, stress_gap_pct=stress_gap_pct, multiplier=multiplier,
    )
    put_intrinsic = _intrinsic_at_stress(
        strike=hedge_put.strike, side="put",
        spot=spot, stress_gap_pct=stress_gap_pct, multiplier=multiplier,
    )

    unhedged_max_loss = (
        short_position.contracts
        * short_position.max_loss_per_contract
        * float(multiplier)
    )
    credit_dollars = (
        short_position.contracts
        * short_position.credit_per_contract
        * float(multiplier)
    )

    call_mid = hedge_call.mid_price if hedge_call.mid_price is not None else float(fallback_mid)
    put_mid = hedge_put.mid_price if hedge_put.mid_price is not None else float(fallback_mid)

    def _tier(*, name: str, target_cap_pct: float, calls: int, puts: int) -> Dict[str, Any]:
        hedge_cost = (calls * call_mid + puts * put_mid) * float(multiplier)
        net_credit = credit_dollars - hedge_cost
        capped_max_loss_up = unhedged_max_loss - (calls * call_intrinsic)
        capped_max_loss_down = unhedged_max_loss - (puts * put_intrinsic)
        # Floor at zero — we don't pretend the hedge produces a
        # guaranteed profit at the design point even if intrinsic
        # exceeds the IC max loss (in practice it can, but for a max-
        # loss CAP metric we report "loss capped at X" not "profit
        # of X").
        scenarios = _scenario_pnl(
            short_position=short_position,
            hedge_call=hedge_call,
            hedge_put=hedge_put,
            calls=calls,
            puts=puts,
            spot=spot,
            gap_pcts=gaps,
            multiplier=multiplier,
            fallback_mid=fallback_mid,
        )
        normal_roc_pct = None
        capital_at_risk = unhedged_max_loss + hedge_cost
        if capital_at_risk > 0:
            normal_roc_pct = round(100.0 * net_credit / capital_at_risk, 2)
        return {
            "name": name,
            "targetMaxLossPct": target_cap_pct,
            "calls": int(calls),
            "puts": int(puts),
            "hedgeCostDollars": round(hedge_cost, 2),
            "netCreditDollars": round(net_credit, 2),
            "capitalAtRiskDollars": round(capital_at_risk, 2),
            "maxLossAtUpStressDollars": round(max(0.0, capped_max_loss_up), 2),
            "maxLossAtDownStressDollars": round(max(0.0, capped_max_loss_down), 2),
            "normalRegimeRocPct": normal_roc_pct,
            "scenarios": scenarios,
        }

    tiers: List[Dict[str, Any]] = []
    standard_names = {50.0: "Lottery (50% cap)", 33.0: "Defined (33% cap)", 20.0: "Conservative (20% cap)"}
    for cap in caps:
        target_max_loss_dollars = unhedged_max_loss * (float(cap) / 100.0)
        recoup_needed = unhedged_max_loss - target_max_loss_dollars
        calls = _hedge_count_for_target(
            target_recoup_dollars=recoup_needed,
            intrinsic_per_contract=call_intrinsic,
        )
        puts = _hedge_count_for_target(
            target_recoup_dollars=recoup_needed,
            intrinsic_per_contract=put_intrinsic,
        )
        tiers.append(_tier(
            name=standard_names.get(float(cap), f"Custom ({cap:.0f}% cap)"),
            target_cap_pct=float(cap),
            calls=calls,
            puts=puts,
        ))

    if asymmetric:
        up_stress = float(asymmetric.get("upStress", stress_gap_pct))
        down_stress = float(asymmetric.get("downStress", stress_gap_pct))
        up_cap_pct = float(asymmetric.get("upCap", 50.0))
        down_cap_pct = float(asymmetric.get("downCap", 20.0))
        up_intrinsic = _intrinsic_at_stress(
            strike=hedge_call.strike, side="call",
            spot=spot, stress_gap_pct=up_stress, multiplier=multiplier,
        )
        down_intrinsic = _intrinsic_at_stress(
            strike=hedge_put.strike, side="put",
            spot=spot, stress_gap_pct=down_stress, multiplier=multiplier,
        )
        calls = _hedge_count_for_target(
            target_recoup_dollars=unhedged_max_loss * (1.0 - up_cap_pct / 100.0),
            intrinsic_per_contract=up_intrinsic,
        )
        puts = _hedge_count_for_target(
            target_recoup_dollars=unhedged_max_loss * (1.0 - down_cap_pct / 100.0),
            intrinsic_per_contract=down_intrinsic,
        )
        tiers.append(_tier(
            name=f"Asymmetric (up {up_cap_pct:.0f}% / down {down_cap_pct:.0f}%)",
            target_cap_pct=min(up_cap_pct, down_cap_pct),
            calls=calls,
            puts=puts,
        ))

    notes: List[str] = []
    if call_intrinsic <= 0:
        notes.append(
            f"Call strike {hedge_call.strike} is not ITM at +{stress_gap_pct}% stress (spot would be "
            f"{spot * (1 + stress_gap_pct/100.0):.2f}); upside-tail sizing returned 0. "
            "Widen the stress gap or pick a closer strike."
        )
    if put_intrinsic <= 0:
        notes.append(
            f"Put strike {hedge_put.strike} is not ITM at -{stress_gap_pct}% stress (spot would be "
            f"{spot * (1 - stress_gap_pct/100.0):.2f}); downside-tail sizing returned 0. "
            "Widen the stress gap or pick a closer strike."
        )
    if hedge_call.mid_price is None or hedge_put.mid_price is None:
        notes.append(
            f"At least one hedge strike has no live mid; using fallback ${fallback_mid}/contract. "
            "Pull broker quotes before sizing."
        )

    return {
        "enabled": True,
        "spot": round(float(spot), 2),
        "stressGapPctDefault": float(stress_gap_pct),
        "multiplier": float(multiplier),
        "shortStructure": {
            "label": short_position.label,
            "contracts": int(short_position.contracts),
            "creditPerContract": float(short_position.credit_per_contract),
            "maxLossPerContract": float(short_position.max_loss_per_contract),
            "totalCreditDollars": round(credit_dollars, 2),
            "totalMaxLossDollars": round(unhedged_max_loss, 2),
        },
        "hedgeStrikes": {
            "call": {
                "strike": float(hedge_call.strike),
                "midPrice": hedge_call.mid_price,
                "distancePct": round(float(hedge_call.distance_pct), 3),
                "intrinsicAtStressDollars": round(call_intrinsic, 2),
            },
            "put": {
                "strike": float(hedge_put.strike),
                "midPrice": hedge_put.mid_price,
                "distancePct": round(float(hedge_put.distance_pct), 3),
                "intrinsicAtStressDollars": round(put_intrinsic, 2),
            },
        },
        "tiers": tiers,
        "scenarioGapPcts": gaps,
        "notes": notes,
    }


__all__ = [
    "HedgeStrike",
    "ShortPosition",
    "compute_hedge_sizing",
]
