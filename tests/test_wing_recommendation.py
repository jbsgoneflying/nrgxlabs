from backend.wing_recommendation import compute_wing_recommendation


def test_wing_recommendation_tas_sign_and_multiplier_bounds():
    summary = {
        "events_used": 20,
        "upBreachRatePct": 10.0,
        "downBreachRatePct": 30.0,
        "avgUpOvershootPct": 20.0,
        "avgDownOvershootPct": 60.0,
    }
    quarters = {"Q4": {"recommendation": "Standard"}}
    regime = {"label": "Elevated", "tailMultiplier": 1.2, "guidance": {"tradeGate": "OK"}}

    wr = compute_wing_recommendation(
        summary=summary,
        quarters=quarters,
        regime=regime,
        current_quarter_key="Q4",
        skew_component=None,
    )

    assert wr["tas"] < 0  # downside tail dominant
    assert wr["recommendationLabel"] == "WIDEN_PUTS_TIGHTEN_CALLS"

    base = wr["baseWingMultiple"]
    put = wr["putWingMultiple"]
    call = wr["callWingMultiple"]

    assert base is not None
    assert put is not None
    assert call is not None

    # Asymmetry is capped at 35%
    assert put <= round(base * 1.35, 2)
    assert call >= round(base * 0.65, 2)


def test_wing_recommendation_no_trade_gate_is_respected():
    summary = {
        "events_used": 20,
        "upBreachRatePct": 10.0,
        "downBreachRatePct": 30.0,
        "avgUpOvershootPct": 20.0,
        "avgDownOvershootPct": 60.0,
    }
    quarters = {"Q4": {"recommendation": "Standard"}}
    regime = {"label": "Stress", "tailMultiplier": 1.5, "guidance": {"tradeGate": "NO_TRADE"}}

    wr = compute_wing_recommendation(
        summary=summary,
        quarters=quarters,
        regime=regime,
        current_quarter_key="Q4",
        skew_component=None,
    )

    assert wr["tradeGate"] == "NO_TRADE"
    assert wr["recommendationLabel"] == "NO_TRADE"
    assert "No Trade" in (wr["rationale"] or "")

