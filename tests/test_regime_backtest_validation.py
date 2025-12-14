from backend.regime_overlay import _build_regime_validation, _compute_regime_from_series


def test_regime_validation_counts():
    events = [
        {"breach": True, "impliedMovePct": 5.0, "realizedMovePct": 8.0, "aboveBreachPct": 60.0, "regimeAtEvent": {"tradeGate": "OK"}},
        {"breach": True, "impliedMovePct": 5.0, "realizedMovePct": 9.0, "aboveBreachPct": 80.0, "regimeAtEvent": {"tradeGate": "CAUTION"}},
        {"breach": False, "impliedMovePct": 5.0, "realizedMovePct": 4.0, "aboveBreachPct": None, "regimeAtEvent": {"tradeGate": "NO_TRADE"}},
        {"breach": False, "impliedMovePct": 5.0, "realizedMovePct": 3.0, "aboveBreachPct": None, "regimeAtEvent": {"tradeGate": "OK"}},
    ]
    out = _build_regime_validation(events)
    assert out["eventsUsed"] == 4
    assert out["breaches"] == 2
    assert out["breachesFlagged"] == 1
    assert out["breachesMissed"] == 1
    assert out["flaggedNonBreaches"] == 1
    assert out["breachRateByGatePct"]["OK"] == 50.0  # 1/2
    assert out["breachRateByGatePct"]["CAUTION"] == 100.0  # 1/1
    assert out["breachRateByGatePct"]["NO_TRADE"] == 0.0  # 0/1


def test_no_lookahead_series_uses_only_upto_asof():
    # Construct an SPY series where after the as-of date volatility spikes.
    spy_dates = [f"2025-01-{d:02d}" for d in range(1, 31)]
    # calm then spike
    closes = []
    px = 100.0
    for i in range(30):
        # Keep calm through day 25, then spike after (days 26-30)
        if i < 25:
            px *= 1.001
        else:
            px *= (1.0 + (0.02 if i % 2 == 0 else -0.02))
        closes.append(px)
    iv_by_date = {d: 0.3 for d in spy_dates}

    # As-of is day 25: should not be impacted by the later spike (days 26-30).
    core_full = _compute_regime_from_series(as_of_date="2025-01-25", spy_dates=spy_dates, spy_closes=closes, iv_by_date=iv_by_date)
    core_trunc = _compute_regime_from_series(as_of_date="2025-01-25", spy_dates=spy_dates[:25], spy_closes=closes[:25], iv_by_date=iv_by_date)

    assert core_full["tailMultiplier"] == core_trunc["tailMultiplier"]
    assert core_full["label"] == core_trunc["label"]
    assert core_full["tradeGate"] == core_trunc["tradeGate"]


