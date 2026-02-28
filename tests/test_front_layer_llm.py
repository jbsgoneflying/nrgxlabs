"""Tests for the Front Layer LLM pipeline.

Covers:
  - Asymmetry Radar detection (deterministic)
  - Prompt loading
  - DMS sanitization
  - Morning Brief fallback behavior
  - Weekly Roadmap fallback behavior
  - Guardrail enforcement
"""

import datetime as dt

import pytest

from backend.front_layer_llm import (
    detect_asymmetries,
    _sanitize_dms,
    _recursive_strip,
    _add_timestamp,
    _MORNING_BRIEF_FALLBACK,
    _WEEKLY_ROADMAP_FALLBACK,
    _MORNING_BRIEF_REQUIRED_KEYS,
    _WEEKLY_ROADMAP_REQUIRED_KEYS,
    _load_prompt,
)


# ---------------------------------------------------------------------------
# Asymmetry Radar (deterministic)
# ---------------------------------------------------------------------------


class TestAsymmetryRadar:
    def test_no_signals_on_calm_market(self):
        dms = {
            "regime": {"state": "Risk-On", "score": 25.0},
            "vol_state": {"level": 15.0, "skew": "neutral"},
            "cross_asset_stress": {"composite_score": 30.0, "readings": []},
            "news_themes": [],
        }
        signals = detect_asymmetries(dms)
        assert len(signals) == 0

    def test_vol_underpricing_detected(self):
        """Rising themes + low vol = vol underpricing alert."""
        dms = {
            "regime": {"state": "Transitional", "score": 45.0},
            "vol_state": {"level": 12.0, "skew": "low"},
            "cross_asset_stress": {"composite_score": 45.0, "readings": []},
            "news_themes": [
                {"theme": "AI Displacement", "intensity": 72.0, "acceleration": "rising", "persistence_days": 3},
                {"theme": "Geopolitical", "intensity": 65.0, "acceleration": "rising", "persistence_days": 2},
            ],
        }
        signals = detect_asymmetries(dms)
        types = [s["type"] for s in signals]
        assert "vol_underpricing_vs_narrative" in types

    def test_fx_stress_no_equity_reaction(self):
        """High FX stress + neutral equities = divergence."""
        dms = {
            "regime": {"state": "Transitional", "score": 45.0},
            "vol_state": {"level": 18.0, "skew": "neutral"},
            "cross_asset_stress": {
                "composite_score": 55.0,
                "readings": [
                    {"asset_class": "fx", "stress_score": 75.0},
                    {"asset_class": "fx", "stress_score": 70.0},
                ],
            },
            "news_themes": [],
        }
        signals = detect_asymmetries(dms)
        types = [s["type"] for s in signals]
        assert "fx_stress_no_equity_reaction" in types

    def test_commodity_spike_muted(self):
        """High commodity stress + low regime score = muted response."""
        dms = {
            "regime": {"state": "Transitional", "score": 40.0},
            "vol_state": {"level": 18.0, "skew": "neutral"},
            "cross_asset_stress": {
                "composite_score": 60.0,
                "readings": [
                    {"asset_class": "commodity", "stress_score": 75.0},
                    {"asset_class": "commodity", "stress_score": 70.0},
                ],
            },
            "news_themes": [],
        }
        signals = detect_asymmetries(dms)
        types = [s["type"] for s in signals]
        assert "commodity_spike_muted_index" in types

    def test_regime_flow_divergence(self):
        """Risk-Off regime + Risk-On flow = divergence."""
        dms = {
            "regime": {"state": "Risk-Off", "score": 60.0},
            "vol_state": {"level": 20.0, "skew": "neutral"},
            "cross_asset_stress": {"composite_score": 50.0, "readings": []},
            "news_themes": [],
        }
        signals = detect_asymmetries(dms)
        types = [s["type"] for s in signals]
        assert "regime_flow_divergence" in types

    def test_persistent_theme_no_vol(self):
        """Persistent themes + low vol skew = complacency."""
        dms = {
            "regime": {"state": "Transitional", "score": 45.0},
            "vol_state": {"level": 14.0, "skew": "low"},
            "cross_asset_stress": {"composite_score": 45.0, "readings": []},
            "news_themes": [
                {"theme": "Credit Stress", "intensity": 55.0, "persistence_days": 7, "acceleration": "stable"},
            ],
        }
        signals = detect_asymmetries(dms)
        types = [s["type"] for s in signals]
        assert "persistent_theme_no_vol" in types

    def test_all_signals_have_required_fields(self):
        dms = {
            "regime": {"state": "Risk-Off", "score": 60.0},
            "vol_state": {"level": 12.0, "skew": "low"},
            "cross_asset_stress": {
                "composite_score": 60.0,
                "readings": [
                    {"asset_class": "fx", "stress_score": 75.0},
                    {"asset_class": "commodity", "stress_score": 70.0},
                ],
            },
            "news_themes": [
                {"theme": "AI", "intensity": 72.0, "acceleration": "rising", "persistence_days": 6},
            ],
        }
        signals = detect_asymmetries(dms)
        for s in signals:
            assert "type" in s
            assert "description" in s
            assert "action" in s
            assert "sources" in s
            # All actions should contain safety language
            action_lower = s["action"].lower()
            assert any(phrase in action_lower for phrase in ["monitor", "await", "no action"])

    def test_empty_dms(self):
        assert detect_asymmetries({}) == []
        assert detect_asymmetries(None) == []


# ---------------------------------------------------------------------------
# DMS Sanitization
# ---------------------------------------------------------------------------


class TestSanitization:
    def test_removes_forbidden_keys(self):
        dms = {
            "date": "2026-02-13",
            "regime": {"state": "Risk-On", "price": 4500.0},
        }
        sanitized = _sanitize_dms(dms)
        assert "price" not in sanitized.get("regime", {})

    def test_allows_valid_keys(self):
        dms = {
            "date": "2026-02-13",
            "regime": {"state": "Risk-On", "score": 25.0},
        }
        sanitized = _sanitize_dms(dms)
        assert sanitized["regime"]["state"] == "Risk-On"
        assert sanitized["regime"]["score"] == 25.0

    def test_strips_non_whitelisted_top_level(self):
        dms = {
            "date": "2026-02-13",
            "secret_key": "should_be_removed",
            "regime": {"state": "Risk-On"},
        }
        sanitized = _sanitize_dms(dms)
        assert "secret_key" not in sanitized

    def test_handles_non_dict(self):
        assert _sanitize_dms("not a dict") == {}
        assert _sanitize_dms(None) == {}


class TestRecursiveStrip:
    def test_strips_nested(self):
        obj = {"a": {"price": 100, "name": "test"}, "b": [{"close": 50, "label": "x"}]}
        result = _recursive_strip(obj, {"price", "close"})
        assert "price" not in result["a"]
        assert "close" not in result["b"][0]
        assert result["a"]["name"] == "test"


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------


class TestTimestamp:
    def test_adds_timestamp(self):
        result = _add_timestamp({"key": "value"})
        assert "_generated_at" in result
        assert result["_generated_at"].endswith("Z")


# ---------------------------------------------------------------------------
# Fallback structures
# ---------------------------------------------------------------------------


class TestFallbacks:
    def test_morning_brief_fallback_has_required_keys(self):
        assert _MORNING_BRIEF_REQUIRED_KEYS.issubset(set(_MORNING_BRIEF_FALLBACK.keys()))

    def test_weekly_roadmap_fallback_has_required_keys(self):
        assert _WEEKLY_ROADMAP_REQUIRED_KEYS.issubset(set(_WEEKLY_ROADMAP_FALLBACK.keys()))


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------


class TestPromptLoading:
    def test_morning_brief_prompt_exists(self):
        prompt = _load_prompt("morning_brief.txt")
        assert len(prompt) > 50
        assert "DailyMarketState" in prompt

    def test_weekly_roadmap_prompt_exists(self):
        prompt = _load_prompt("weekly_roadmap.txt")
        assert len(prompt) > 50
        assert "DailyMarketState" in prompt

    def test_nonexistent_prompt(self):
        prompt = _load_prompt("does_not_exist.txt")
        assert prompt == ""
