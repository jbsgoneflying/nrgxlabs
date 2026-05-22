"""Tests for the institutional hedge sizer (engine2b.hedge_sizer)."""
from __future__ import annotations

import math

import pytest

from backend.engine2b.hedge_sizer import (
    HedgeStrike,
    ShortPosition,
    compute_hedge_sizing,
)


def _base_inputs():
    short = ShortPosition(
        contracts=20,
        max_loss_per_contract=4.50,
        credit_per_contract=0.50,
        label="2.0x EM / $5 wing",
    )
    call = HedgeStrike(strike=7700.0, side="call", mid_price=0.05, distance_pct=2.76)
    put = HedgeStrike(strike=7300.0, side="put", mid_price=0.05, distance_pct=-2.58)
    return short, call, put


def test_basic_three_tier_sizing_returns_integer_counts():
    short, call, put = _base_inputs()
    out = compute_hedge_sizing(
        short_position=short,
        hedge_call=call,
        hedge_put=put,
        spot=7493.51,
        stress_gap_pct=3.0,
    )
    assert out["enabled"] is True
    assert len(out["tiers"]) >= 3  # the three caps + asymmetric default may be added if requested
    for tier in out["tiers"]:
        assert isinstance(tier["calls"], int)
        assert isinstance(tier["puts"], int)
        assert tier["calls"] >= 0
        assert tier["puts"] >= 0


def test_conservative_tier_uses_more_contracts_than_lottery():
    short, call, put = _base_inputs()
    out = compute_hedge_sizing(
        short_position=short,
        hedge_call=call,
        hedge_put=put,
        spot=7493.51,
        stress_gap_pct=3.0,
        target_caps_pct=[50.0, 33.0, 20.0],
    )
    lottery = next(t for t in out["tiers"] if t["targetMaxLossPct"] == 50.0)
    conservative = next(t for t in out["tiers"] if t["targetMaxLossPct"] == 20.0)
    assert conservative["calls"] >= lottery["calls"]
    assert conservative["puts"] >= lottery["puts"]


def test_zero_contract_position_disables_block():
    short = ShortPosition(contracts=0, max_loss_per_contract=4.5, credit_per_contract=0.5)
    _, call, put = _base_inputs()
    out = compute_hedge_sizing(
        short_position=short,
        hedge_call=call,
        hedge_put=put,
        spot=7493.51,
    )
    assert out["enabled"] is False


def test_missing_mid_uses_fallback_and_flags_note():
    short, call, put = _base_inputs()
    bad_call = HedgeStrike(strike=7700.0, side="call", mid_price=None, distance_pct=2.76)
    out = compute_hedge_sizing(
        short_position=short,
        hedge_call=bad_call,
        hedge_put=put,
        spot=7493.51,
        stress_gap_pct=3.0,
        fallback_mid=0.07,
    )
    assert out["enabled"] is True
    assert any("fallback" in n.lower() for n in out["notes"])
    # Hedge cost should reflect the fallback for the call leg specifically.
    tier = out["tiers"][0]
    expected_min = tier["calls"] * 0.07 * 100.0
    # The total hedge cost must include the call fallback contribution.
    assert tier["hedgeCostDollars"] >= expected_min - 0.01


def test_asymmetric_tier_uses_independent_up_and_down_caps():
    short, call, put = _base_inputs()
    out = compute_hedge_sizing(
        short_position=short,
        hedge_call=call,
        hedge_put=put,
        spot=7493.51,
        stress_gap_pct=3.0,
        target_caps_pct=[50.0],
        asymmetric={"upCap": 50.0, "downCap": 20.0},
    )
    asym = next(t for t in out["tiers"] if t["name"].startswith("Asymmetric"))
    sym = next(t for t in out["tiers"] if t["targetMaxLossPct"] == 50.0)
    # Asymmetric downside cap is tighter -> more puts than the 50/50 sym tier.
    assert asym["puts"] >= sym["puts"]
    # Upside cap matches sym (both 50%), so calls should be identical.
    assert asym["calls"] == sym["calls"]


def test_intrinsic_at_stress_drives_count():
    # If a strike is exactly at the stress spot, intrinsic = 0 -> count = 0.
    short, _, put = _base_inputs()
    spot = 7493.51
    stress_gap = 3.0
    # 7493.51 * 1.03 = 7718.31; a call strike at that level has zero intrinsic at +3% stress.
    far_call = HedgeStrike(strike=7720.0, side="call", mid_price=0.05, distance_pct=3.02)
    out = compute_hedge_sizing(
        short_position=short,
        hedge_call=far_call,
        hedge_put=put,
        spot=spot,
        stress_gap_pct=stress_gap,
    )
    assert out["hedgeStrikes"]["call"]["intrinsicAtStressDollars"] <= 0.01
    for tier in out["tiers"]:
        assert tier["calls"] == 0  # Cannot size upside hedge — intrinsic is 0.
    assert any("not ITM" in n or "0" in n for n in out["notes"])


def test_ceil_rounding_never_under_hedges():
    # Construct a case where exact hedge count is non-integer (e.g. 1.4) and
    # confirm we ceil to 2 rather than truncate to 1.
    short = ShortPosition(contracts=10, max_loss_per_contract=4.5, credit_per_contract=0.5)
    # 10 contracts * $4.50 * 100 = $4,500 unhedged max loss
    # 50% cap = $2,250 must be recouped at +3% stress
    # Call hedge intrinsic at +3%: spot 7493.51 * 1.03 = 7718.31; 7718.31 - 7700 = 18.31 * 100 = $1,831 per contract
    # Required: ceil(2250 / 1831) = ceil(1.229) = 2
    call = HedgeStrike(strike=7700.0, side="call", mid_price=0.05, distance_pct=2.76)
    put = HedgeStrike(strike=7300.0, side="put", mid_price=0.05, distance_pct=-2.58)
    out = compute_hedge_sizing(
        short_position=short,
        hedge_call=call,
        hedge_put=put,
        spot=7493.51,
        stress_gap_pct=3.0,
        target_caps_pct=[50.0],
    )
    tier = out["tiers"][0]
    assert tier["calls"] == 2


def test_scenario_table_covers_up_and_down_gaps():
    short, call, put = _base_inputs()
    out = compute_hedge_sizing(
        short_position=short,
        hedge_call=call,
        hedge_put=put,
        spot=7493.51,
        stress_gap_pct=3.0,
        scenario_gap_pcts=[-5.0, -3.0, 0.0, 3.0, 5.0],
    )
    tier = out["tiers"][0]
    gaps = [s["gapPct"] for s in tier["scenarios"]]
    assert gaps == sorted(gaps)
    assert -5.0 in gaps and 5.0 in gaps
    # At 0 gap inside the IC, the structure should retain credit
    # (hedge premium paid is the only cost).
    zero_row = next(s for s in tier["scenarios"] if s["gapPct"] == 0.0)
    expected_credit = (
        short.contracts * short.credit_per_contract * 100.0
        - (tier["calls"] * call.mid_price + tier["puts"] * put.mid_price) * 100.0
    )
    assert zero_row["netPnl"] == pytest.approx(expected_credit, abs=0.05)
    # At +5% gap, the hedge intrinsic should dominate vs the max loss.
    up_stress = next(s for s in tier["scenarios"] if s["gapPct"] == 5.0)
    assert up_stress["hedgeIntrinsic"] > 0


def test_position_agnostic_works_for_e1_like_inputs():
    """A single-stock straddle short with a wide pop-protection put should size cleanly."""
    short = ShortPosition(contracts=5, max_loss_per_contract=12.0, credit_per_contract=4.0,
                          label="NVDA earnings short straddle")
    # Earnings pop expectation ~10% above EM; tail strike at +12%.
    call = HedgeStrike(strike=550.0, side="call", mid_price=0.20, distance_pct=12.0)
    put = HedgeStrike(strike=440.0, side="put", mid_price=0.15, distance_pct=-10.0)
    out = compute_hedge_sizing(
        short_position=short,
        hedge_call=call,
        hedge_put=put,
        spot=490.0,
        stress_gap_pct=15.0,  # design for a 15% earnings move
        target_caps_pct=[50.0, 33.0, 20.0],
    )
    assert out["enabled"] is True
    assert all(t["calls"] >= 0 and t["puts"] >= 0 for t in out["tiers"])
    assert out["shortStructure"]["totalMaxLossDollars"] == pytest.approx(5 * 12.0 * 100, abs=0.01)
