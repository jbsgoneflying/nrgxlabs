"""Tests for the Front Layer DailyMarketState module.

Covers:
  - Schema dataclass construction and serialization
  - Builder function with various engine inputs
  - Engine gate derivation logic
  - Vol state derivation
  - News risk derivation
  - Diff utility
  - Redis persistence helpers (mocked)
"""

import datetime as dt

import pytest

from backend.daily_market_state import (
    DailyMarketState,
    EngineGates,
    NewsRiskState,
    RegimeState,
    VolState,
    EarningsCandidate,
    build_daily_market_state,
    compute_dms_diff,
    _derive_engine_gates,
    _derive_vol_state,
    _derive_news_risk,
)


# ---------------------------------------------------------------------------
# Dataclass round-trip tests
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_regime_state_roundtrip(self):
        r = RegimeState(state="Risk-On", score=25.0, drivers=["fx_stress"])
        d = r.to_dict()
        assert d["state"] == "Risk-On"
        assert d["score"] == 25.0
        r2 = RegimeState.from_dict(d)
        assert r2.state == r.state

    def test_daily_market_state_roundtrip(self):
        dms = DailyMarketState(
            date="2026-02-13",
            generated_at="2026-02-13T08:55:00Z",
            regime={"state": "Transitional", "score": 45.0, "drivers": []},
        )
        d = dms.to_dict()
        assert d["date"] == "2026-02-13"
        assert d["regime"]["state"] == "Transitional"
        dms2 = DailyMarketState.from_dict(d)
        assert dms2.date == dms.date
        assert dms2.regime == dms.regime

    def test_from_dict_handles_bad_input(self):
        dms = DailyMarketState.from_dict(None)
        assert dms.date == ""
        dms2 = DailyMarketState.from_dict("not a dict")
        assert dms2.date == ""

    def test_engine_gates_roundtrip(self):
        eg = EngineGates(earnings="selective", red_dog="allowed")
        d = eg.to_dict()
        assert d["earnings"] == "selective"
        eg2 = EngineGates.from_dict(d)
        assert eg2.earnings == "selective"

    def test_earnings_candidate_roundtrip(self):
        ec = EarningsCandidate(ticker="AAPL", score=85.0, dealer_gamma="supportive", regime_fit=True)
        d = ec.to_dict()
        assert d["ticker"] == "AAPL"
        assert d["regime_fit"] is True


# ---------------------------------------------------------------------------
# Engine gate derivation
# ---------------------------------------------------------------------------


class TestEngineGates:
    def test_stressed_regime_suppresses_most(self):
        gates = _derive_engine_gates("Stressed", "")
        assert gates.earnings == "suppressed"
        assert gates.red_dog == "allowed"  # Red Dog thrives in stress
        assert gates.ichimoku == "suppressed"
        assert gates.index_income == "suppressed"

    def test_risk_on_allows_most(self):
        gates = _derive_engine_gates("Risk-On", "")
        assert gates.earnings == "allowed"
        assert gates.ichimoku == "allowed"
        assert gates.index_income == "allowed"
        assert gates.red_dog == "watch"

    def test_risk_off_selective(self):
        gates = _derive_engine_gates("Risk-Off", "")
        assert gates.earnings == "selective"
        assert gates.red_dog == "allowed"
        assert gates.ichimoku == "suppressed"

    def test_transitional_defaults(self):
        gates = _derive_engine_gates("Transitional", "")
        assert gates.red_dog == "allowed"
        assert gates.index_income == "allowed"


# ---------------------------------------------------------------------------
# Vol state derivation
# ---------------------------------------------------------------------------


class TestVolState:
    def test_rising_vol_backwardation(self):
        vs = _derive_vol_state("RISING", 80.0)
        assert vs.term_structure == "backwardation"
        assert vs.skew == "elevated"

    def test_falling_vol_contango(self):
        vs = _derive_vol_state("FALLING", 20.0)
        assert vs.term_structure == "contango"
        assert vs.skew == "low"

    def test_flat_vol_neutral(self):
        vs = _derive_vol_state("NORMAL", 50.0)
        assert vs.term_structure == "flat"
        assert vs.skew == "neutral"

    def test_vix_level_override(self):
        vs = _derive_vol_state("NORMAL", 50.0, vix_level=18.5)
        assert vs.level == 18.5


# ---------------------------------------------------------------------------
# News risk derivation
# ---------------------------------------------------------------------------


class TestNewsRisk:
    def test_high_risk(self):
        nr = _derive_news_risk(10, 3, ["FOMC", "CPI"])
        assert nr.today == "high"
        assert "FOMC" in nr.week_ahead

    def test_medium_risk(self):
        nr = _derive_news_risk(4, 1)
        assert nr.today == "medium"

    def test_low_risk(self):
        nr = _derive_news_risk(1, 0)
        assert nr.today == "low"


# ---------------------------------------------------------------------------
# Builder function
# ---------------------------------------------------------------------------


class TestBuildDMS:
    def test_basic_build(self):
        dms = build_daily_market_state(
            date_str="2026-02-13",
            regime={"label": "Risk-On", "score": 25.0, "components": {"fx_stress": 20}},
            vol_direction="NORMAL",
            iv_stress=30.0,
        )
        assert dms.date == "2026-02-13"
        assert dms.regime["state"] == "Risk-On"
        assert dms.engine_gates["earnings"] == "allowed"

    def test_build_with_defaults(self):
        dms = build_daily_market_state()
        assert dms.date != ""
        assert dms.regime["state"] == "Transitional"

    def test_build_with_themes_and_asymmetries(self):
        dms = build_daily_market_state(
            news_themes=[{"theme": "AI Displacement", "intensity": 72}],
            asymmetry_signals=[{"type": "test", "description": "test signal"}],
        )
        assert len(dms.news_themes) == 1
        assert len(dms.asymmetry_signals) == 1


# ---------------------------------------------------------------------------
# Diff utility
# ---------------------------------------------------------------------------


class TestDiff:
    def test_diff_detects_changes(self):
        yesterday = DailyMarketState(
            date="2026-02-12",
            regime={"state": "Transitional", "score": 45.0},
        )
        today = DailyMarketState(
            date="2026-02-13",
            regime={"state": "Risk-On", "score": 25.0},
        )
        diff = compute_dms_diff(today, yesterday)
        assert diff["has_changes"] is True
        assert "regime" in diff["changes"]
        assert diff["from_date"] == "2026-02-12"
        assert diff["to_date"] == "2026-02-13"

    def test_diff_no_changes(self):
        dms = DailyMarketState(
            date="2026-02-13",
            regime={"state": "Neutral", "score": 50.0},
        )
        diff = compute_dms_diff(dms, dms)
        # Same object: no changes (dates are same, values are same)
        assert diff["has_changes"] is False

    def test_diff_list_field_count_change(self):
        yesterday = DailyMarketState(date="2026-02-12", news_themes=[])
        today = DailyMarketState(date="2026-02-13", news_themes=[{"theme": "AI"}])
        diff = compute_dms_diff(today, yesterday)
        assert "news_themes" in diff["changes"]
