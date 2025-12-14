from backend.regime_overlay import clamp, percentile_rank, _label_from_tail_multiplier, _trade_gate


def test_clamp():
    assert clamp(0.7, 2.0, 0.1) == 0.7
    assert clamp(0.7, 2.0, 2.5) == 2.0
    assert clamp(0.7, 2.0, 1.1) == 1.1


def test_percentile_rank_basic():
    xs = [1, 2, 3, 4]
    assert percentile_rank(1, xs) == 0.25
    assert percentile_rank(2, xs) == 0.5
    assert percentile_rank(4, xs) == 1.0


def test_tail_multiplier_label_and_gate():
    assert _label_from_tail_multiplier(0.85) == "Calm"
    assert _label_from_tail_multiplier(1.0) == "Normal"
    assert _label_from_tail_multiplier(1.4) == "Elevated"
    assert _label_from_tail_multiplier(1.6) == "Stress"

    assert _trade_gate("Calm") == "OK"
    assert _trade_gate("Normal") == "OK"
    assert _trade_gate("Elevated") == "CAUTION"
    assert _trade_gate("Stress") == "NO_TRADE"


