"""Tests for backend.spx_ic.edge_score.compute_edge_score.

The Edge Score is the SPX/SPY analogue of E1's VRP score: a deterministic
0-100 composite that drives the pre-LLM Edge Scorecard and feeds the
advisor prompt. These tests pin the shape, the directionality of each
component, the flag surfacing, and the label/confidence bucketing so a
regression on any sub-scorer is caught immediately.
"""
from __future__ import annotations

from backend.spx_ic.edge_score import compute_edge_score


# ---------------------------------------------------------------------------
# Shape + key presence
# ---------------------------------------------------------------------------

def test_shape_has_all_expected_keys():
    res = compute_edge_score()
    for k in ("edgeScore", "components", "label", "flags", "confidence",
              "preferredEm", "breachAtPreferredEmPct", "inputs"):
        assert k in res, f"missing top-level key: {k}"
    for c in ("regimeAlignment", "macroProximity", "volPressure",
              "dealerGamma", "newsGate", "breachAtPreferredEm"):
        assert c in res["components"], f"missing component: {c}"


def test_defaults_produce_moderate_score():
    res = compute_edge_score()
    assert 30 <= res["edgeScore"] <= 80
    assert res["confidence"] == "LOW"  # no real inputs supplied
    assert res["flags"] == []


# ---------------------------------------------------------------------------
# Component directionality
# ---------------------------------------------------------------------------

def test_strong_setup_scores_high(monkeypatch):
    """Calm regime + low macro + paid premium + positive gamma + quiet
    news + low breach → high edge score, STRONG label, HIGH confidence."""
    res = compute_edge_score(
        regime_score=25,
        regime_bucket="LOW",
        macro_multiplier=1.05,
        vol_pressure_state="BID",
        dealer_gamma_sign="positive",
        news_gate={"gate": "ok", "maxAdjustedIntensity": 8.0},
        em_breach_summary={"1.0": 28.0, "1.5": 14.0, "2.0": 6.0},
        preferred_em=1.5,
    )
    assert res["edgeScore"] >= 70
    assert res["label"] == "STRONG"
    assert res["confidence"] == "HIGH"
    assert res["flags"] == []
    assert res["preferredEm"] == 1.5
    assert res["breachAtPreferredEmPct"] == 14.0


def test_weak_setup_scores_low_and_flags():
    """ELEVATED regime + macro 1.8 + ASK vol + negative gamma + caution
    news + high breach → low edge score, WEAK/AVOID label, flag list."""
    res = compute_edge_score(
        regime_score=70,
        regime_bucket="ELEVATED",
        macro_multiplier=1.8,
        vol_pressure_state="ASK",
        dealer_gamma_sign="negative",
        news_gate={"gate": "caution", "maxAdjustedIntensity": 55.0},
        em_breach_summary={"1.0": 40.0, "1.5": 28.0, "2.0": 12.0},
        preferred_em=1.5,
    )
    assert res["edgeScore"] < 50
    assert res["label"] in ("WEAK", "AVOID")
    assert "regime_elevated" in res["flags"]
    assert "macro_in_window" in res["flags"]
    assert "negative_dealer_gamma" in res["flags"]


def test_avoid_setup_with_no_trade_regime():
    res = compute_edge_score(
        regime_score=85,
        regime_bucket="NO_TRADE",
        macro_multiplier=2.2,
        vol_pressure_state="SPIKING",
        dealer_gamma_sign="negative",
        news_gate={"gate": "block", "maxAdjustedIntensity": 90.0},
        em_breach_summary={"1.0": 55.0, "1.5": 40.0, "2.0": 30.0},
        preferred_em=2.0,
    )
    assert res["edgeScore"] < 25
    assert res["label"] == "AVOID"
    assert "regime_no_trade" in res["flags"]
    assert "vol_spiking" in res["flags"]
    assert "news_gate_block" in res["flags"]


# ---------------------------------------------------------------------------
# Component scorers — directional sanity (each component varies as expected)
# ---------------------------------------------------------------------------

def test_regime_score_inverse_to_stress():
    low = compute_edge_score(regime_score=10, regime_bucket="LOW")
    high = compute_edge_score(regime_score=80, regime_bucket="ELEVATED")
    assert low["components"]["regimeAlignment"] > high["components"]["regimeAlignment"]


def test_macro_score_inverse_to_multiplier():
    calm = compute_edge_score(macro_multiplier=1.0)
    hot = compute_edge_score(macro_multiplier=1.9)
    assert calm["components"]["macroProximity"] > hot["components"]["macroProximity"]


def test_vol_pressure_bid_beats_ask_and_spiking():
    bid = compute_edge_score(vol_pressure_state="BID")
    ask = compute_edge_score(vol_pressure_state="ASK")
    spiking = compute_edge_score(vol_pressure_state="SPIKING")
    assert bid["components"]["volPressure"] > ask["components"]["volPressure"]
    assert bid["components"]["volPressure"] > spiking["components"]["volPressure"]


def test_dealer_gamma_positive_beats_negative():
    pos = compute_edge_score(dealer_gamma_sign="positive")
    neg = compute_edge_score(dealer_gamma_sign="negative")
    assert pos["components"]["dealerGamma"] > neg["components"]["dealerGamma"]


def test_news_gate_quiet_beats_block():
    quiet = compute_edge_score(news_gate={"gate": "ok", "maxAdjustedIntensity": 5.0})
    block = compute_edge_score(news_gate={"gate": "block", "maxAdjustedIntensity": 90.0})
    assert quiet["components"]["newsGate"] > block["components"]["newsGate"]


def test_breach_low_beats_high():
    clean = compute_edge_score(em_breach_summary={"1.5": 5.0}, preferred_em=1.5)
    dirty = compute_edge_score(em_breach_summary={"1.5": 25.0}, preferred_em=1.5)
    assert clean["components"]["breachAtPreferredEm"] > dirty["components"]["breachAtPreferredEm"]


# ---------------------------------------------------------------------------
# Preferred EM fallback / inputs echo
# ---------------------------------------------------------------------------

def test_preferred_em_missing_falls_back_to_lowest_breach():
    """If the caller asks for an EM the breach summary doesn't carry,
    we should fall back to the lowest-breach EM in the grid."""
    res = compute_edge_score(
        em_breach_summary={"1.0": 35.0, "2.0": 8.0},
        preferred_em=1.5,  # not in the dict
    )
    # Lowest breach in the grid is at 2.0× with 8.0%
    assert res["preferredEm"] == 2.0
    assert res["breachAtPreferredEmPct"] == 8.0


def test_inputs_echo_carries_raw_values():
    res = compute_edge_score(
        regime_score=42,
        regime_bucket="MODERATE",
        macro_multiplier=1.3,
        vol_pressure_state="NEUTRAL",
        dealer_gamma_sign="positive",
        news_gate={"gate": "caution"},
    )
    inp = res["inputs"]
    assert inp["regimeBucket"] == "MODERATE"
    assert inp["macroMultiplier"] == 1.3
    assert inp["dealerGammaSign"] == "positive"
    assert inp["newsGate"] == {"gate": "caution"}


# ---------------------------------------------------------------------------
# Confidence bucketing
# ---------------------------------------------------------------------------

def test_confidence_high_with_full_signals():
    res = compute_edge_score(
        regime_score=40,
        regime_bucket="MODERATE",
        macro_multiplier=1.2,
        vol_pressure_state="BID",
        dealer_gamma_sign="positive",
        news_gate={"maxAdjustedIntensity": 20.0, "gate": "ok"},
        em_breach_summary={"1.5": 12.0},
        preferred_em=1.5,
    )
    assert res["confidence"] == "HIGH"


def test_confidence_low_when_unknown_inputs():
    res = compute_edge_score()
    assert res["confidence"] == "LOW"


# ---------------------------------------------------------------------------
# Score clamping
# ---------------------------------------------------------------------------

def test_score_is_clamped_to_0_100():
    res = compute_edge_score(
        regime_score=200,
        macro_multiplier=10.0,
        em_breach_summary={"1.0": 200.0},
        preferred_em=1.0,
    )
    assert 0 <= res["edgeScore"] <= 100
    for v in res["components"].values():
        if v is None:
            continue
        assert 0 <= v <= 100
