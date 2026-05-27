"""Tests for Engine 2 AI Trade Advisor — trades CRUD, tracking, and advisor module."""

import datetime as dt
import json
from unittest.mock import MagicMock, patch

import pytest

from backend.config import FeatureFlags
from backend.engine2_advisor import (
    _extract_dms_context,
    _parse_llm_json,
    _sanitize_e2_for_llm,
    compute_desk_consensus,
    compute_news_gate_score,
    compute_trade_tracking,
    generate_checkin_analysis,
    generate_trade_analysis,
)
from backend.engine2_trades import (
    _TRADE_INDEX_KEY,
    _TRADE_KEY_PREFIX,
    add_checkin,
    close_trade,
    compute_trade_performance_digest,
    get_trade,
    list_active_trades,
    list_closed_trades,
    log_trade,
    promote_to_live,
)
from backend.news_theme_intelligence import (
    compute_market_adjusted_intensity,
    get_theme_impact_weight,
)


# ---------------------------------------------------------------------------
# Fake Redis store for trade tests
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self._data = {}

    def set_json(self, key, value, ttl_s=None):
        self._data[key] = json.loads(json.dumps(value, default=str))
        return True

    def get_json(self, key):
        return self._data.get(key)


# ---------------------------------------------------------------------------
# Trade CRUD tests
# ---------------------------------------------------------------------------

class TestTradeCRUD:
    def _make_flags(self):
        return FeatureFlags(
            ENGINE2_TRADE_TTL_S=3600,
            ENGINE2_TRADE_MAX_INDEX=10,
            ENGINE2_ADVISOR_ENABLED=True,
        )

    def test_log_trade_returns_id(self):
        store = FakeStore()
        flags = self._make_flags()
        trade_data = {
            "source": "advisor",
            "entry": {"underlying": "SPX", "shortPutStrike": 6300, "shortCallStrike": 6600, "wingWidth": 10},
            "entryContext": {"regimeScore": 45, "regimeBucket": "MODERATE"},
        }
        tid = log_trade(trade_data, store=store, flags=flags)
        assert tid is not None
        assert tid.startswith("e2-")
        assert "SPX" in tid

    def test_log_trade_persists_to_store(self):
        store = FakeStore()
        flags = self._make_flags()
        trade_data = {
            "entry": {"underlying": "SPX", "shortPutStrike": 6300},
        }
        tid = log_trade(trade_data, store=store, flags=flags)
        assert store.get_json(f"{_TRADE_KEY_PREFIX}{tid}") is not None
        index = store.get_json(_TRADE_INDEX_KEY) or []
        assert tid in index

    def test_list_active_trades(self):
        store = FakeStore()
        flags = self._make_flags()
        log_trade({"entry": {"underlying": "SPX"}}, store=store, flags=flags)
        log_trade({"entry": {"underlying": "SPX"}}, store=store, flags=flags)
        trades = list_active_trades(store=store)
        assert len(trades) == 2
        assert all(t["status"] == "active" for t in trades)
        assert all("mode" in t for t in trades)

    def test_log_trade_defaults_to_tracked_mode(self):
        store = FakeStore()
        flags = self._make_flags()
        tid = log_trade({"entry": {"underlying": "SPX"}}, store=store, flags=flags)
        trade = get_trade(tid, store=store)
        assert trade is not None
        assert trade["mode"] == "tracked"

    def test_promote_to_live(self):
        store = FakeStore()
        flags = self._make_flags()
        tid = log_trade({"entry": {"underlying": "SPX"}, "mode": "tracked"}, store=store, flags=flags)
        updated = promote_to_live(tid, store=store, flags=flags)
        assert updated is not None
        assert updated["mode"] == "live"

    def test_close_trade(self):
        store = FakeStore()
        flags = self._make_flags()
        tid = log_trade({"entry": {"underlying": "SPX"}}, store=store, flags=flags)
        result = close_trade(tid, close_data={"reason": "target_hit"}, store=store, flags=flags)
        assert result is not None
        assert result["status"] == "closed"
        assert result["closeReason"] == "target_hit"
        active = list_active_trades(store=store)
        assert len(active) == 0

    def test_add_checkin(self):
        store = FakeStore()
        flags = self._make_flags()
        tid = log_trade({"entry": {"underlying": "SPX"}}, store=store, flags=flags)
        checkin = {"status": "on_track", "headline": "All good"}
        result = add_checkin(tid, checkin, store=store, flags=flags)
        assert result is not None
        assert len(result["checkIns"]) == 1
        assert result["checkIns"][0]["status"] == "on_track"

    def test_checkin_adjust_sets_monitoring(self):
        store = FakeStore()
        flags = self._make_flags()
        tid = log_trade({"entry": {"underlying": "SPX"}}, store=store, flags=flags)
        add_checkin(tid, {"status": "adjust"}, store=store, flags=flags)
        trade = get_trade(tid, store=store)
        assert trade["status"] == "monitoring"

    def test_log_trade_no_store_returns_none(self):
        tid = log_trade({"entry": {}}, store=None)
        assert tid is None

    def test_index_capped(self):
        store = FakeStore()
        flags = FeatureFlags(ENGINE2_TRADE_TTL_S=3600, ENGINE2_TRADE_MAX_INDEX=3, ENGINE2_ADVISOR_ENABLED=True)
        for _ in range(5):
            log_trade({"entry": {"underlying": "SPX"}}, store=store, flags=flags)
        index = store.get_json(_TRADE_INDEX_KEY)
        assert len(index) == 3


# ---------------------------------------------------------------------------
# Trade tracking tests
# ---------------------------------------------------------------------------

class TestTradeTracking:
    def _make_trade(self, sp=6300, sc=6600, spot_entry=6450, wing=10,
                    entry_date=None, expiry_date=None):
        if entry_date is None or expiry_date is None:
            today = dt.date.today()
            entry_date = (today - dt.timedelta(days=1)).isoformat()
            expiry_date = (today + dt.timedelta(days=4)).isoformat()
        return {
            "entry": {
                "underlying": "SPX",
                "shortPutStrike": sp,
                "shortCallStrike": sc,
                "spotAtEntry": spot_entry,
                "wingWidth": wing,
                "entryDate": entry_date,
                "expiryDate": expiry_date,
            },
            "entryContext": {
                "regimeScore": 40,
                "regimeBucket": "MODERATE",
                "volPressureState": "NEUTRAL",
            },
        }

    def test_on_track(self):
        trade = self._make_trade()
        tracking = compute_trade_tracking(trade, current_spot=6450)
        assert tracking["deterministicStatus"] == "on_track"
        assert tracking["distPutPts"] > 0
        assert tracking["distCallPts"] > 0

    def test_caution_when_approaching_strike(self):
        trade = self._make_trade(sp=6300, sc=6600, spot_entry=6450)
        # Spot at 6360 -- 60 pts from short put (was 150 away at entry)
        # Proximity = (1 - 60/150) * 100 = 60%
        tracking = compute_trade_tracking(trade, current_spot=6360)
        assert tracking["deterministicStatus"] == "caution"

    def test_adjust_when_very_close(self):
        trade = self._make_trade(sp=6300, sc=6600, spot_entry=6450)
        # Spot at 6320 -- 20 pts from short put (was 150 away)
        # Proximity = (1 - 20/150) * 100 = 86.7%
        tracking = compute_trade_tracking(trade, current_spot=6320)
        assert tracking["deterministicStatus"] == "adjust"

    def test_exit_when_breached(self):
        trade = self._make_trade(sp=6300, sc=6600, spot_entry=6450)
        tracking = compute_trade_tracking(trade, current_spot=6290)
        assert tracking["deterministicStatus"] == "exit"

    def test_regime_drift_detection(self):
        trade = self._make_trade()
        tracking = compute_trade_tracking(
            trade,
            current_spot=6450,
            current_regime={"score": 70, "bucket": "ELEVATED"},
        )
        assert tracking["regimeDriftScore"] == 30.0
        assert tracking["regimeDriftBucket"] == "ELEVATED"

    def test_vol_shift_detection(self):
        trade = self._make_trade()
        tracking = compute_trade_tracking(
            trade,
            current_spot=6450,
            current_vol_pressure="BID",
        )
        assert tracking["volShift"] == "NEUTRAL -> BID"


# ---------------------------------------------------------------------------
# Advisor module unit tests
# ---------------------------------------------------------------------------

class TestParseJson:
    def test_plain_json(self):
        assert _parse_llm_json('{"a": 1}') == {"a": 1}

    def test_markdown_fences(self):
        text = '```json\n{"verdict": "TRADE"}\n```'
        assert _parse_llm_json(text) == {"verdict": "TRADE"}

    def test_preamble_text(self):
        text = 'Here is the result:\n{"verdict": "PASS"}'
        assert _parse_llm_json(text) == {"verdict": "PASS"}

    def test_returns_none_on_garbage(self):
        assert _parse_llm_json("not json at all") is None


class TestSanitize:
    def test_sanitize_extracts_key_fields(self):
        payload = {
            "asOfDate": "2026-03-25",
            "params": {"entryDay": "mon"},
            "underlying": {"symbol": "SPX"},
            "current": {"regime": {"score": 45}},
            "expectedMove": {"enabled": True},
            "strikeTargets": {},
            "oddsLikeNow": {},
            "recommendation": {"recommended": None},
            "recSimple": {"width": 10},
            "liveContext": {"volPressure": {"state": "NEUTRAL"}},
            "technicals": {"rsi": {"value": 55}},
            "telemetry": {"should": "be excluded"},
        }
        sanitized = _sanitize_e2_for_llm(payload)
        assert "asOfDate" in sanitized
        assert "telemetry" not in sanitized
        assert sanitized.get("liveContextSummary", {}).get("volPressure") == {"state": "NEUTRAL"}

    def test_dms_context_extraction(self):
        dms = {
            "regime": {"label": "Transitional"},
            "vol_state": {"level": 22},
            "cross_asset_stress": {"composite_score": 0.5, "composite_label": "elevated"},
            "news_themes": [
                {"theme": "Tariffs", "key": "geopolitical_escalation", "intensity": 40, "acceleration": 5},
                {"theme": "Minor", "key": "ai_displacement", "intensity": 5, "acceleration": 0},
            ],
        }
        ctx = _extract_dms_context(dms)
        assert ctx["composite_stress"] == 0.5
        assert len(ctx["active_themes"]) == 1
        t = ctx["active_themes"][0]
        assert t["theme"] == "Tariffs"
        assert t["adjustedIntensity"] == 40.0
        assert t["spxImpactWeight"] == 1.0

    def test_dms_context_adjusted_intensity_applied(self):
        dms = {
            "news_themes": [
                {"theme": "AI Displacement", "key": "ai_displacement", "intensity": 86.7, "acceleration": "rising"},
                {"theme": "Geopolitical Escalation", "key": "geopolitical_escalation", "intensity": 50, "acceleration": "stable"},
            ],
        }
        ctx = _extract_dms_context(dms)
        themes = ctx["active_themes"]
        assert len(themes) == 2
        ai = next(t for t in themes if t["key"] == "ai_displacement")
        geo = next(t for t in themes if t["key"] == "geopolitical_escalation")
        assert ai["adjustedIntensity"] == pytest.approx(21.7, abs=0.1)
        assert ai["spxImpactWeight"] == 0.25
        assert geo["adjustedIntensity"] == pytest.approx(50.0)
        assert geo["spxImpactWeight"] == 1.0

    def test_dms_context_includes_news_gate(self):
        dms = {
            "news_themes": [
                {"theme": "Geopolitical Escalation", "key": "geopolitical_escalation", "intensity": 70, "acceleration": "rising"},
            ],
        }
        ctx = _extract_dms_context(dms)
        assert "newsGate" in ctx
        assert ctx["newsGate"]["gate"] == "elevated"
        assert ctx["newsGate"]["maxAdjustedIntensity"] == 70.0


class TestAdvisorFallback:
    def test_disabled_returns_fallback(self):
        flags = FeatureFlags(ENGINE2_ADVISOR_ENABLED=False)
        result = generate_trade_analysis({}, flags=flags)
        assert result["_source"] == "fallback"
        assert result["verdict"] == "PASS"

    def test_checkin_disabled_returns_fallback(self):
        flags = FeatureFlags(ENGINE2_ADVISOR_ENABLED=False)
        result = generate_checkin_analysis({}, {"deterministicStatus": "on_track"}, flags=flags)
        assert result["_source"] == "fallback"
        assert result["status"] == "on_track"


# ---------------------------------------------------------------------------
# Multi-wing grid (config test)
# ---------------------------------------------------------------------------

class TestMultiWingConfig:
    def test_multi_wing_flag_default_true(self):
        flags = FeatureFlags()
        assert flags.ENGINE2_MULTI_WING is True

    def test_wing_pts_config_parsed(self):
        flags = FeatureFlags(ENGINE2_WING_WIDTH_PTS="5,10,15,20,25")
        parts = [p.strip() for p in flags.ENGINE2_WING_WIDTH_PTS.split(",")]
        assert len(parts) == 5

    def test_cache_key_includes_multi_wing(self):
        f1 = FeatureFlags(ENGINE2_MULTI_WING=True)
        f2 = FeatureFlags(ENGINE2_MULTI_WING=False)
        assert f1.cache_key_engine2() != f2.cache_key_engine2()


# ---------------------------------------------------------------------------
# Theme impact weighting tests
# ---------------------------------------------------------------------------

class TestThemeImpactWeighting:
    def test_get_weight_by_key(self):
        assert get_theme_impact_weight("geopolitical_escalation") == 1.0
        assert get_theme_impact_weight("ai_displacement") == 0.25
        assert get_theme_impact_weight("liquidity_shock") == 0.95

    def test_get_weight_by_label(self):
        assert get_theme_impact_weight("Geopolitical Escalation") == 1.0
        assert get_theme_impact_weight("AI Displacement") == 0.25
        assert get_theme_impact_weight("Credit Stress") == 0.80

    def test_get_weight_unknown_returns_default(self):
        assert get_theme_impact_weight("Unknown Theme") == 0.5
        assert get_theme_impact_weight("") == 0.5

    def test_adjusted_intensity_ai_discounted(self):
        adj = compute_market_adjusted_intensity(86.7, "ai_displacement")
        assert adj == pytest.approx(21.7, abs=0.1)

    def test_adjusted_intensity_geo_full_weight(self):
        adj = compute_market_adjusted_intensity(50.0, "geopolitical_escalation")
        assert adj == pytest.approx(50.0)

    def test_adjusted_intensity_by_label(self):
        adj = compute_market_adjusted_intensity(60.0, "Labor Stress")
        assert adj == pytest.approx(30.0)

    def test_adjusted_intensity_zero(self):
        assert compute_market_adjusted_intensity(0.0, "geopolitical_escalation") == 0.0


# ---------------------------------------------------------------------------
# News gate scoring tests
# ---------------------------------------------------------------------------

class TestNewsGateScore:
    def test_empty_themes_ok(self):
        result = compute_news_gate_score([])
        assert result["gate"] == "ok"
        assert result["maxAdjustedIntensity"] == 0
        assert result["dominantTheme"] is None

    def test_low_adjusted_ok(self):
        themes = [{"theme": "AI Displacement", "adjustedIntensity": 21.7}]
        result = compute_news_gate_score(themes)
        assert result["gate"] == "ok"

    def test_caution_threshold(self):
        themes = [{"theme": "Labor Stress", "adjustedIntensity": 35.0}]
        result = compute_news_gate_score(themes)
        assert result["gate"] == "caution"
        assert result["dominantTheme"] == "Labor Stress"

    def test_elevated_threshold(self):
        themes = [{"theme": "Credit Stress", "adjustedIntensity": 64.0}]
        result = compute_news_gate_score(themes)
        assert result["gate"] == "elevated"

    def test_block_threshold(self):
        themes = [{"theme": "Geopolitical Escalation", "adjustedIntensity": 85.0}]
        result = compute_news_gate_score(themes)
        assert result["gate"] == "block"
        assert result["dominantTheme"] == "Geopolitical Escalation"

    def test_dominant_is_highest_adjusted(self):
        themes = [
            {"theme": "AI Displacement", "adjustedIntensity": 21.7},
            {"theme": "Geopolitical Escalation", "adjustedIntensity": 50.0},
        ]
        result = compute_news_gate_score(themes)
        assert result["dominantTheme"] == "Geopolitical Escalation"
        assert result["maxAdjustedIntensity"] == 50.0
        assert result["gate"] == "caution"
        assert result["themeCount"] == 2


# ---------------------------------------------------------------------------
# Desk consensus pre-score tests
# ---------------------------------------------------------------------------

class TestDeskConsensus:
    def test_low_risk_all_clear(self):
        result = compute_desk_consensus(
            regime_score=30.0,
            regime_bucket="LOW",
            macro_multiplier=1.0,
            news_gate={"gate": "ok", "maxAdjustedIntensity": 10},
            dealer_gamma_sign="positive",
            vol_pressure_state="ASK",
        )
        assert result["riskLevel"] == "low"
        assert result["suggestedEmFloor"] == 1.0
        assert result["suggestedEmLabel"] == "aggressive"
        assert result["macroFlag"] is False
        assert result["gammaFlag"] is False
        assert result["newsFlag"] is False
        assert len(result["flags"]) == 0

    def test_moderate_risk_macro_elevated(self):
        result = compute_desk_consensus(
            regime_score=50.0,
            regime_bucket="MODERATE",
            macro_multiplier=1.6,
            news_gate={"gate": "ok"},
            dealer_gamma_sign="positive",
            vol_pressure_state="NEUTRAL",
        )
        assert result["riskLevel"] == "moderate"
        assert result["suggestedEmFloor"] == 1.5
        assert result["macroFlag"] is True
        assert result["gammaFlag"] is False
        assert any("Macro" in f for f in result["flags"])

    def test_elevated_risk_multiple_flags(self):
        result = compute_desk_consensus(
            regime_score=50.0,
            regime_bucket="MODERATE",
            macro_multiplier=1.9,
            news_gate={"gate": "caution", "dominantTheme": "Geopolitical Escalation"},
            dealer_gamma_sign="negative",
            vol_pressure_state="NEUTRAL",
        )
        assert result["riskLevel"] in ("elevated", "high")
        assert result["suggestedEmFloor"] == 2.0
        assert result["macroFlag"] is True
        assert result["gammaFlag"] is True
        assert result["newsFlag"] is True
        assert len(result["flags"]) >= 3

    def test_high_risk_no_trade_regime(self):
        result = compute_desk_consensus(
            regime_score=80.0,
            regime_bucket="NO_TRADE",
            macro_multiplier=2.5,
        )
        assert result["riskLevel"] == "high"
        assert result["suggestedEmFloor"] == 2.0
        assert result["regimeFlag"] is True
        assert result["macroFlag"] is True

    def test_high_risk_news_block(self):
        result = compute_desk_consensus(
            regime_score=40.0,
            regime_bucket="LOW",
            macro_multiplier=1.0,
            news_gate={"gate": "block", "dominantTheme": "Geopolitical Escalation", "maxAdjustedIntensity": 85},
        )
        assert result["riskLevel"] in ("elevated", "high")
        assert result["suggestedEmFloor"] == 2.0
        assert result["newsFlag"] is True

    def test_breach_all_high_forces_high_risk(self):
        result = compute_desk_consensus(
            regime_score=30.0,
            regime_bucket="LOW",
            macro_multiplier=1.0,
            em_breach_summary={"1.0": 40.0, "1.5": 38.0, "2.0": 36.0},
        )
        assert result["riskLevel"] == "high"
        assert result["suggestedEmFloor"] == 2.0
        assert any("breach > 35%" in f for f in result["flags"])

    def test_breach_not_all_high_unaffected(self):
        result = compute_desk_consensus(
            regime_score=30.0,
            regime_bucket="LOW",
            macro_multiplier=1.0,
            em_breach_summary={"1.0": 50.0, "1.5": 25.0, "2.0": 10.0},
        )
        assert result["riskLevel"] == "low"
        assert result["suggestedEmFloor"] == 1.0

    def test_defaults_produce_moderate(self):
        result = compute_desk_consensus()
        assert result["riskLevel"] == "low"
        assert result["suggestedEmFloor"] == 1.0
        assert isinstance(result["flags"], list)
        assert isinstance(result["riskPoints"], float)

    def test_sanitizer_passes_desk_consensus(self):
        payload = {
            "asOfDate": "2026-03-25",
            "deskConsensus": {"riskLevel": "elevated", "suggestedEmFloor": 2.0, "flags": ["test"]},
        }
        sanitized = _sanitize_e2_for_llm(payload)
        assert "deskConsensus" in sanitized
        assert sanitized["deskConsensus"]["riskLevel"] == "elevated"


# ---------------------------------------------------------------------------
# E2 depth upgrade — edgeAnalysis + emBreachSummary + emPreference in sanitizer
# ---------------------------------------------------------------------------

class TestSanitizerDepthUpgrade:
    """The advisor depth upgrade promotes several deterministic blocks
    from nested/embedded fields into top-level LLM context. These tests
    pin that contract so the LLM always sees them."""

    def test_sanitizer_passes_edge_analysis(self):
        payload = {
            "asOfDate": "2026-03-25",
            "edgeAnalysis": {
                "edgeScore": 78.0,
                "label": "STRONG",
                "components": {"regimeAlignment": 85.0, "macroProximity": 75.0},
                "flags": [],
                "confidence": "HIGH",
            },
        }
        sanitized = _sanitize_e2_for_llm(payload)
        assert "edgeAnalysis" in sanitized
        assert sanitized["edgeAnalysis"]["edgeScore"] == 78.0
        assert sanitized["edgeAnalysis"]["label"] == "STRONG"

    def test_sanitizer_passes_em_breach_summary_and_preference(self):
        payload = {
            "asOfDate": "2026-03-25",
            "emBreachSummary": {"1.0": 28.0, "1.5": 14.0, "2.0": 6.0},
            "emPreference": {"emPreference": 1.5, "label": "standard"},
        }
        sanitized = _sanitize_e2_for_llm(payload)
        assert sanitized["emBreachSummary"]["1.5"] == 14.0
        assert sanitized["emPreference"]["emPreference"] == 1.5

    def test_sanitizer_passes_history_breaker_risk(self):
        payload = {
            "asOfDate": "2026-03-25",
            "historyBreakerRisk": {"score": 0.42, "label": "moderate"},
        }
        sanitized = _sanitize_e2_for_llm(payload)
        assert "historyBreakerRisk" in sanitized
        assert sanitized["historyBreakerRisk"]["score"] == 0.42

    def test_sanitizer_includes_ichimoku_and_candles_in_technicals(self):
        payload = {
            "technicals": {
                "rsi": {"value": 55},
                "ichimoku": {"kijun": 5400, "tenkan": 5410},
                "candles": {"engulfing": False},
            },
        }
        sanitized = _sanitize_e2_for_llm(payload)
        techs = sanitized["technicalsSummary"]
        assert "ichimoku" in techs
        assert "candles" in techs
        assert techs["ichimoku"]["kijun"] == 5400


class TestAdvisorRequiredKeys:
    """The depth upgrade adds edgeRationale + positionGuidance to the
    required-key set. The fallback object must also carry both so the
    frontend always has a valid (if empty) positionGuidance dict."""

    def test_required_keys_include_edge_rationale_and_position_guidance(self):
        from backend.engine2_advisor import _ADVISOR_REQUIRED_KEYS
        assert "edgeRationale" in _ADVISOR_REQUIRED_KEYS
        assert "positionGuidance" in _ADVISOR_REQUIRED_KEYS

    def test_fallback_initialises_position_guidance_dict(self):
        flags = FeatureFlags(ENGINE2_ADVISOR_ENABLED=False)
        result = generate_trade_analysis({}, flags=flags)
        assert isinstance(result.get("positionGuidance"), dict)
        assert result["positionGuidance"]["sizePct"] == 0
        assert result["positionGuidance"]["maxContracts"] == 0
        # All required keys must be present in the fallback shell
        from backend.engine2_advisor import _ADVISOR_REQUIRED_KEYS
        for k in _ADVISOR_REQUIRED_KEYS:
            assert k in result, f"fallback missing required key: {k}"


# ---------------------------------------------------------------------------
# Adjusted trade tests
# ---------------------------------------------------------------------------

class TestAdjustedTrade:
    def _make_flags(self):
        return FeatureFlags(
            ENGINE2_TRADE_TTL_S=3600,
            ENGINE2_TRADE_MAX_INDEX=10,
            ENGINE2_ADVISOR_ENABLED=True,
        )

    def test_adjusted_trade_stores_original_ticket(self):
        store = FakeStore()
        flags = self._make_flags()
        trade_data = {
            "source": "adjusted",
            "entry": {
                "underlying": "SPX",
                "shortPutStrike": 6290,
                "shortCallStrike": 6610,
                "wingWidth": 10,
                "entryCredit": 2.50,
            },
            "originalTicket": {
                "shortPutStrike": 6300,
                "shortCallStrike": 6600,
                "wingWidth": 10,
                "estimatedCredit": "~$2.80",
            },
            "adjustmentNote": "Better fill at wider put",
        }
        tid = log_trade(trade_data, store=store, flags=flags)
        trade = get_trade(tid, store=store)
        assert trade["source"] == "adjusted"
        assert trade["originalTicket"]["shortPutStrike"] == 6300
        assert trade["adjustmentNote"] == "Better fill at wider put"
        assert trade["entry"]["shortPutStrike"] == 6290

    def test_advisor_trade_has_no_original_ticket(self):
        store = FakeStore()
        flags = self._make_flags()
        trade_data = {
            "source": "advisor",
            "entry": {"underlying": "SPX", "shortPutStrike": 6300},
        }
        tid = log_trade(trade_data, store=store, flags=flags)
        trade = get_trade(tid, store=store)
        assert trade["source"] == "advisor"
        assert trade["originalTicket"] is None
        assert trade["adjustmentNote"] is None


# ---------------------------------------------------------------------------
# Structured close / outcome tests
# ---------------------------------------------------------------------------

class TestStructuredClose:
    def _make_flags(self):
        return FeatureFlags(
            ENGINE2_TRADE_TTL_S=3600,
            ENGINE2_TRADE_MAX_INDEX=10,
            ENGINE2_ADVISOR_ENABLED=True,
        )

    def test_close_with_exit_credit_computes_pnl(self):
        store = FakeStore()
        flags = self._make_flags()
        tid = log_trade(
            {"entry": {"underlying": "SPX", "entryCredit": 3.00}},
            store=store, flags=flags,
        )
        result = close_trade(
            tid,
            close_data={"reason": "closed_early", "exitCredit": 0.80},
            store=store, flags=flags,
        )
        assert result["outcome"]["realizedPnl"] == 2.20
        assert result["outcome"]["outcomeClass"] == "win"
        assert result["outcome"]["entryCredit"] == 3.00
        assert result["outcome"]["exitCredit"] == 0.80

    def test_close_expired_worthless_is_full_win(self):
        store = FakeStore()
        flags = self._make_flags()
        tid = log_trade(
            {"entry": {"underlying": "SPX", "entryCredit": 2.50}},
            store=store, flags=flags,
        )
        result = close_trade(
            tid,
            close_data={"reason": "expired_worthless", "exitCredit": 0, "expiredWorthless": True},
            store=store, flags=flags,
        )
        assert result["outcome"]["realizedPnl"] == 2.50
        assert result["outcome"]["outcomeClass"] == "win"
        assert result["outcome"]["expiredWorthless"] is True

    def test_close_with_loss(self):
        store = FakeStore()
        flags = self._make_flags()
        tid = log_trade(
            {"entry": {"underlying": "SPX", "entryCredit": 1.50}},
            store=store, flags=flags,
        )
        result = close_trade(
            tid,
            close_data={"reason": "stopped_out", "exitCredit": 5.00},
            store=store, flags=flags,
        )
        assert result["outcome"]["realizedPnl"] == -3.50
        assert result["outcome"]["outcomeClass"] == "loss"

    def test_close_with_explicit_outcome_class(self):
        store = FakeStore()
        flags = self._make_flags()
        tid = log_trade(
            {"entry": {"underlying": "SPX", "entryCredit": 1.00}},
            store=store, flags=flags,
        )
        result = close_trade(
            tid,
            close_data={"reason": "manual", "exitCredit": 1.02, "outcomeClass": "scratch"},
            store=store, flags=flags,
        )
        assert result["outcome"]["outcomeClass"] == "scratch"

    def test_close_with_notes(self):
        store = FakeStore()
        flags = self._make_flags()
        tid = log_trade(
            {"entry": {"underlying": "SPX", "entryCredit": 2.00}},
            store=store, flags=flags,
        )
        result = close_trade(
            tid,
            close_data={"reason": "closed_early", "exitCredit": 0.5, "notes": "Took profits at 75%"},
            store=store, flags=flags,
        )
        assert result["outcome"]["notes"] == "Took profits at 75%"


# ---------------------------------------------------------------------------
# Trade history and performance digest tests
# ---------------------------------------------------------------------------

class TestTradePerformanceDigest:
    def _make_flags(self):
        return FeatureFlags(
            ENGINE2_TRADE_TTL_S=3600,
            ENGINE2_TRADE_MAX_INDEX=100,
            ENGINE2_ADVISOR_ENABLED=True,
        )

    def _create_closed_trade(self, store, flags, entry_credit, exit_credit, em=1.5, wing=10, regime="MODERATE", verdict="TRADE"):
        td = {
            "entry": {
                "underlying": "SPX",
                "entryCredit": entry_credit,
                "emMultiple": em,
                "wingWidth": wing,
            },
            "entryContext": {"regimeBucket": regime},
            "advisorVerdict": {"verdict": verdict},
        }
        tid = log_trade(td, store=store, flags=flags)
        close_trade(tid, close_data={"reason": "manual", "exitCredit": exit_credit}, store=store, flags=flags)
        return tid

    def test_list_closed_trades(self):
        store = FakeStore()
        flags = self._make_flags()
        self._create_closed_trade(store, flags, 3.00, 0.50)
        self._create_closed_trade(store, flags, 2.00, 4.00)
        log_trade({"entry": {"underlying": "SPX"}}, store=store, flags=flags)
        closed = list_closed_trades(store=store)
        assert len(closed) == 2
        assert all(t["status"] == "closed" for t in closed)

    def test_digest_empty_returns_no_data(self):
        store = FakeStore()
        digest = compute_trade_performance_digest(store=store)
        assert digest["totalClosed"] == 0
        assert digest["hasData"] is False

    def test_digest_basic_stats(self):
        store = FakeStore()
        flags = self._make_flags()
        self._create_closed_trade(store, flags, 3.00, 0.50)
        self._create_closed_trade(store, flags, 2.00, 0.00)
        self._create_closed_trade(store, flags, 1.50, 5.00)

        digest = compute_trade_performance_digest(store=store)
        assert digest["hasData"] is True
        assert digest["totalClosed"] == 3
        assert digest["wins"] == 2
        assert digest["losses"] == 1
        assert digest["winRate"] == pytest.approx(66.7, abs=0.1)
        assert digest["totalPnl"] == pytest.approx(1.00, abs=0.01)

    def test_digest_by_em_breakdown(self):
        store = FakeStore()
        flags = self._make_flags()
        self._create_closed_trade(store, flags, 3.00, 0.50, em=1.0)
        self._create_closed_trade(store, flags, 2.00, 0.50, em=1.0)
        self._create_closed_trade(store, flags, 1.50, 5.00, em=2.0)

        digest = compute_trade_performance_digest(store=store)
        assert "1.0" in digest["byEm"]
        assert digest["byEm"]["1.0"]["winRate"] == 100.0
        assert digest["byEm"]["1.0"]["n"] == 2
        assert "2.0" in digest["byEm"]
        assert digest["byEm"]["2.0"]["winRate"] == 0.0
        assert digest["byEm"]["2.0"]["n"] == 1

    def test_digest_by_wing_breakdown(self):
        store = FakeStore()
        flags = self._make_flags()
        self._create_closed_trade(store, flags, 3.00, 0.50, wing=5)
        self._create_closed_trade(store, flags, 2.00, 0.50, wing=10)
        self._create_closed_trade(store, flags, 1.50, 5.00, wing=10)

        digest = compute_trade_performance_digest(store=store)
        assert "$5" in digest["byWing"]
        assert digest["byWing"]["$5"]["winRate"] == 100.0
        assert "$10" in digest["byWing"]
        assert digest["byWing"]["$10"]["winRate"] == 50.0

    def test_digest_risk_tendency_too_conservative(self):
        store = FakeStore()
        flags = self._make_flags()
        for _ in range(10):
            self._create_closed_trade(store, flags, 0.50, 0.10)

        digest = compute_trade_performance_digest(store=store)
        assert digest["winRate"] == 100.0
        assert digest["riskTendency"] == "too_conservative"

    def test_digest_risk_tendency_too_aggressive(self):
        store = FakeStore()
        flags = self._make_flags()
        for _ in range(5):
            self._create_closed_trade(store, flags, 1.00, 8.00)
        self._create_closed_trade(store, flags, 3.00, 0.50)

        digest = compute_trade_performance_digest(store=store)
        assert digest["winRate"] < 40
        assert digest["riskTendency"] == "too_aggressive"

    def test_digest_verdict_calibration(self):
        store = FakeStore()
        flags = self._make_flags()
        self._create_closed_trade(store, flags, 3.00, 0.50, verdict="TRADE")
        self._create_closed_trade(store, flags, 1.50, 5.00, verdict="TRADE")
        self._create_closed_trade(store, flags, 2.00, 0.50, verdict="LEAN_PASS")

        digest = compute_trade_performance_digest(store=store)
        assert "TRADE" in digest["verdictCalibration"]
        assert digest["verdictCalibration"]["TRADE"]["total"] == 2
        assert digest["verdictCalibration"]["TRADE"]["win"] == 1
        assert digest["verdictCalibration"]["TRADE"]["loss"] == 1
        assert "LEAN_PASS" in digest["verdictCalibration"]
        assert digest["verdictCalibration"]["LEAN_PASS"]["win"] == 1

    def test_digest_counts_adjusted_trades(self):
        store = FakeStore()
        flags = self._make_flags()
        td = {
            "source": "adjusted",
            "entry": {"underlying": "SPX", "entryCredit": 2.50, "emMultiple": 1.5, "wingWidth": 10},
            "originalTicket": {"shortPutStrike": 6300},
            "adjustmentNote": "Better fill",
            "entryContext": {"regimeBucket": "MODERATE"},
            "advisorVerdict": {"verdict": "TRADE"},
        }
        tid = log_trade(td, store=store, flags=flags)
        close_trade(tid, close_data={"reason": "manual", "exitCredit": 0.5}, store=store, flags=flags)

        digest = compute_trade_performance_digest(store=store)
        assert digest["adjustedCount"] == 1
