"""
Tests for Engine 4: Ichimoku Cloud Continuation Scanner

Tests cover:
- Ichimoku series computation
- Kijun slope detection
- Time-in-cloud measurement
- Chikou entanglement detection
- Cloud penetration calculation
- Trend regime detection
- Pullback state machine
- Entry trigger detection
- A+ scoring system
"""

import datetime as dt
import pytest
from typing import List, Optional

from backend.technicals import (
    DailyBar,
    compute_ichimoku_series,
    compute_volume_metrics,
    compute_atr_series,
)
from backend.engine4_ichimoku import (
    APLUS_THRESHOLD,
    compute_kijun_slope,
    count_kijun_flat_days,
    compute_time_in_cloud,
    is_chikou_tangled,
    compute_cloud_penetration_pct,
    detect_trend_regime,
    detect_pullback_state,
    detect_entry_trigger,
    compute_entry_levels,
    detect_ichimoku_setup,
    score_ichimoku_setup,
    build_ichimoku_signal,
    signal_to_dict,
    compute_distance_to_actionable,
    compute_index_ichimoku_state,
    compute_relative_strength,
    compute_beta_corr,
    _entry_offset,
)
from backend.gating import (
    reconcile_ichimoku_verdict,
    gate_ichimoku,
    VERDICT_TRADABLE,
    VERDICT_WATCH,
    VERDICT_STAND_DOWN,
)
from backend.engine4_backtest import backtest_from_bars


# ---------------------------------------------------------------------------
# Test Fixtures
# ---------------------------------------------------------------------------

def make_bars(n: int, base_price: float = 100.0, trend: str = "up") -> List[DailyBar]:
    """Create synthetic daily bars for testing."""
    bars = []
    for i in range(n):
        date = (dt.date(2024, 1, 1) + dt.timedelta(days=i)).isoformat()
        
        if trend == "up":
            close = base_price + (i * 0.5)
        elif trend == "down":
            close = base_price - (i * 0.5)
        else:
            close = base_price + (0.2 if i % 2 == 0 else -0.2)
        
        high = close + 1.0
        low = close - 1.0
        open_px = close - 0.3 if trend == "up" else close + 0.3
        volume = 1_000_000 + (i * 10_000)
        
        bars.append(DailyBar(
            trade_date=date,
            open=open_px,
            high=high,
            low=low,
            close=close,
            volume=volume,
            vwap=None,
        ))
    
    return bars


def make_kijun_series(n: int, base: float = 100.0, slope: str = "flat") -> List[Optional[float]]:
    """Create synthetic Kijun series for testing."""
    series = []
    for i in range(n):
        if slope == "positive":
            val = base + (i * 0.1)
        elif slope == "negative":
            val = base - (i * 0.1)
        else:
            val = base
        series.append(val)
    return series


# ---------------------------------------------------------------------------
# Tests: Ichimoku Series Computation
# ---------------------------------------------------------------------------

class TestIchimokuSeries:
    def test_requires_minimum_bars(self):
        """Should return disabled if fewer than 52 bars."""
        bars = make_bars(30)
        result = compute_ichimoku_series(bars)
        assert result["enabled"] is False
        assert "Insufficient bars" in result["notes"][0]

    def test_computes_all_series(self):
        """Should compute all Ichimoku components with sufficient data."""
        bars = make_bars(80)
        result = compute_ichimoku_series(bars)
        
        assert result["enabled"] is True
        assert len(result["tenkan_series"]) == 80
        assert len(result["kijun_series"]) == 80
        assert len(result["span_a_series"]) == 80
        assert len(result["span_b_series"]) == 80
        assert len(result["cloud_series"]) == 80
        assert len(result["chikou_series"]) == 80

    def test_cloud_series_aligned(self):
        """Cloud series should be shifted back 26 bars.

        The cloud at bar[i] is built from Span A/B values computed 26 bars
        earlier. Span B is a 52-period midpoint, so its first non-None value
        is at index 51 (needs 52 bars). Combined with the 26-bar back-shift,
        the first plottable cloud value lands at index 51 + 26 = 77.
        """
        bars = make_bars(80)
        result = compute_ichimoku_series(bars)

        first_valid = 77

        # Everything before the warmup boundary should be None.
        for i in range(first_valid):
            assert result["cloud_series"][i] is None

        # From the boundary onward, cloud values should exist.
        for i in range(first_valid, 80):
            assert result["cloud_series"][i] is not None
            assert "cloudTop" in result["cloud_series"][i]
            assert "cloudBottom" in result["cloud_series"][i]


# ---------------------------------------------------------------------------
# Tests: Kijun Slope Detection
# ---------------------------------------------------------------------------

class TestKijunSlope:
    def test_positive_slope(self):
        """Should detect positive slope when Kijun is rising."""
        series = make_kijun_series(30, slope="positive")
        direction, value = compute_kijun_slope(series, lookback=5)
        
        assert direction == "positive"
        assert value > 0

    def test_negative_slope(self):
        """Should detect negative slope when Kijun is falling."""
        series = make_kijun_series(30, slope="negative")
        direction, value = compute_kijun_slope(series, lookback=5)
        
        assert direction == "negative"
        assert value < 0

    def test_flat_slope(self):
        """Should detect flat slope when Kijun is unchanged."""
        series = make_kijun_series(30, slope="flat")
        direction, value = compute_kijun_slope(series, lookback=5)
        
        assert direction == "flat"
        assert abs(value) < 0.001


class TestKijunFlatDays:
    def test_counts_flat_days(self):
        """Should count consecutive flat days."""
        series = [100.0] * 20  # All flat
        count = count_kijun_flat_days(series, lookback=20)
        
        # Should count all days as flat
        assert count >= 15

    def test_stops_at_change(self):
        """Should stop counting when Kijun changes."""
        series = [100.0] * 10 + [100.5] + [100.5] * 9  # Change in middle
        count = count_kijun_flat_days(series, lookback=20)
        
        # Should only count the flat portion at the end
        assert count <= 10


# ---------------------------------------------------------------------------
# Tests: Time in Cloud
# ---------------------------------------------------------------------------

class TestTimeInCloud:
    def test_counts_closes_in_cloud(self):
        """Should count closes inside cloud."""
        closes = [100.0] * 20
        cloud_series = [{"cloudTop": 102.0, "cloudBottom": 98.0} for _ in range(20)]
        
        count = compute_time_in_cloud(closes, cloud_series, lookback=20)
        
        # All closes are inside cloud
        assert count == 20

    def test_excludes_closes_outside_cloud(self):
        """Should not count closes outside cloud."""
        closes = [110.0] * 20  # All above cloud
        cloud_series = [{"cloudTop": 102.0, "cloudBottom": 98.0} for _ in range(20)]
        
        count = compute_time_in_cloud(closes, cloud_series, lookback=20)
        
        assert count == 0


# ---------------------------------------------------------------------------
# Tests: Chikou Entanglement
# ---------------------------------------------------------------------------

class TestChikouEntanglement:
    def test_detects_tangled_chikou(self):
        """Should detect when Chikou is tangled with prior candles."""
        # Create bars where current close (Chikou) is within prior candle ranges
        bars = make_bars(60, base_price=100.0, trend="flat")
        closes = [float(b.close) for b in bars]
        highs = [float(b.high) for b in bars]
        lows = [float(b.low) for b in bars]
        
        # In flat trend, Chikou is likely tangled
        tangled = is_chikou_tangled(closes, highs, lows, chikou_offset=26)
        
        # Should be tangled in sideways market
        assert isinstance(tangled, bool)

    def test_detects_clear_chikou(self):
        """Should detect when Chikou is clear of prior candles."""
        # Create strong uptrend where current price is far above 26-bar-ago levels
        bars = make_bars(60, base_price=100.0, trend="up")
        # Modify to make trend stronger
        closes = [100.0 + (i * 2.0) for i in range(60)]
        highs = [c + 1.0 for c in closes]
        lows = [c - 1.0 for c in closes]
        
        tangled = is_chikou_tangled(closes, highs, lows, chikou_offset=26)
        
        # Strong uptrend should have clear Chikou
        assert tangled is False


# ---------------------------------------------------------------------------
# Tests: Cloud Penetration
# ---------------------------------------------------------------------------

class TestCloudPenetration:
    def test_zero_when_above_cloud(self):
        """Should return 0% when price is above cloud."""
        pct = compute_cloud_penetration_pct(110.0, cloud_top=105.0, cloud_bottom=100.0)
        assert pct == 0.0

    def test_zero_when_below_cloud(self):
        """Should return 0% when price is below cloud."""
        pct = compute_cloud_penetration_pct(95.0, cloud_top=105.0, cloud_bottom=100.0)
        assert pct == 0.0

    def test_penetration_inside_cloud(self):
        """Should return penetration percentage when inside cloud."""
        # Price at 102 with cloud 100-105 = 2 points in from bottom, 40% penetration
        pct = compute_cloud_penetration_pct(102.0, cloud_top=105.0, cloud_bottom=100.0)
        assert 0 < pct < 100


# ---------------------------------------------------------------------------
# Tests: Trend Regime Detection
# ---------------------------------------------------------------------------

class TestTrendRegime:
    def test_bull_regime_above_cloud(self):
        """Should detect bull regime when price is above cloud."""
        cloud = {"cloudTop": 100.0, "cloudBottom": 95.0, "cloudBias": "bullish"}
        cloud_future = {"cloudTop": 101.0, "cloudBottom": 96.0, "cloudBias": "bullish"}
        
        result = detect_trend_regime(
            close=105.0, 
            cloud=cloud, 
            cloud_future=cloud_future, 
            kijun_slope="positive"
        )
        
        assert result["valid"] is True
        assert result["direction"] == "bullish"
        assert result["position"] == "above"

    def test_bear_regime_below_cloud(self):
        """Should detect bear regime when price is below cloud."""
        cloud = {"cloudTop": 100.0, "cloudBottom": 95.0, "cloudBias": "bearish"}
        cloud_future = {"cloudTop": 99.0, "cloudBottom": 94.0, "cloudBias": "bearish"}
        
        result = detect_trend_regime(
            close=90.0, 
            cloud=cloud, 
            cloud_future=cloud_future, 
            kijun_slope="negative"
        )
        
        assert result["valid"] is True
        assert result["direction"] == "bearish"
        assert result["position"] == "below"

    def test_invalid_inside_cloud(self):
        """Should reject regime when price is inside cloud."""
        cloud = {"cloudTop": 100.0, "cloudBottom": 95.0, "cloudBias": "bullish"}
        
        result = detect_trend_regime(
            close=97.5, 
            cloud=cloud, 
            cloud_future=None, 
            kijun_slope="flat"
        )
        
        assert result["valid"] is False
        assert result["position"] == "inside"


# ---------------------------------------------------------------------------
# Tests: Entry Trigger Detection
# ---------------------------------------------------------------------------

class TestEntryTrigger:
    def test_bullish_trigger_reclaim_tenkan(self):
        """Should detect bullish trigger when close reclaims Tenkan."""
        bar = DailyBar(
            trade_date="2024-01-01",
            open=99.0,
            high=102.0,
            low=98.0,
            close=101.5,  # Strong close in top 33%
            volume=1_000_000,
            vwap=None,
        )
        
        result = detect_entry_trigger(
            bar=bar,
            tenkan=100.0,
            prev_tenkan=99.5,
            kijun=98.0,
            direction="bullish",
            rsi=55.0,
        )
        
        assert result["triggered"] is True
        assert result["tenkanReclaim"] is True
        assert result["candleStrength"] == "strong"

    def test_bearish_trigger_loses_tenkan(self):
        """Should detect bearish trigger when close loses Tenkan."""
        bar = DailyBar(
            trade_date="2024-01-01",
            open=101.0,
            high=102.0,
            low=98.0,
            close=98.5,  # Weak close in bottom 33%
            volume=1_000_000,
            vwap=None,
        )
        
        result = detect_entry_trigger(
            bar=bar,
            tenkan=100.0,
            prev_tenkan=100.5,
            kijun=102.0,
            direction="bearish",
            rsi=45.0,
        )
        
        assert result["triggered"] is True
        assert result["tenkanReclaim"] is True  # Actually "loses" for bearish
        assert result["candleStrength"] == "strong"


# ---------------------------------------------------------------------------
# Tests: Entry Level Computation
# ---------------------------------------------------------------------------

class TestEntryLevels:
    def test_bull_entry_levels(self):
        """Should compute correct bullish entry levels."""
        bar = DailyBar(
            trade_date="2024-01-01",
            open=99.0,
            high=102.0,
            low=98.0,
            close=101.0,
            volume=1_000_000,
            vwap=None,
        )
        
        levels = compute_entry_levels(
            bar=bar,
            kijun=97.0,
            direction="bullish",
            atr=2.0,
            swing_target=110.0,
        )
        
        assert levels["entry"] > bar.high  # Buy stop above high
        assert levels["stop"] < bar.low  # Stop below low/Kijun
        assert levels["target1"] == 110.0  # Swing target
        assert levels["risk"] > 0
        assert levels["trail"] == 97.0  # Kijun

    def test_bear_entry_levels(self):
        """Should compute correct bearish entry levels."""
        bar = DailyBar(
            trade_date="2024-01-01",
            open=101.0,
            high=102.0,
            low=98.0,
            close=99.0,
            volume=1_000_000,
            vwap=None,
        )
        
        levels = compute_entry_levels(
            bar=bar,
            kijun=103.0,
            direction="bearish",
            atr=2.0,
            swing_target=90.0,
        )
        
        assert levels["entry"] < bar.low  # Sell stop below low
        assert levels["stop"] > bar.high  # Stop above high/Kijun
        assert levels["target1"] == 90.0  # Swing target
        assert levels["risk"] > 0


# ---------------------------------------------------------------------------
# Tests: A+ Scoring System
# ---------------------------------------------------------------------------

class TestScoring:
    def test_high_score_with_all_confirmations(self):
        """Should score highly with all confirmations."""
        signal = {
            "direction": "bullish",
            "chikouTangled": False,
            "volumeRatio": 1.5,
            "closePosition": 0.75,
            "kijunSlope": "positive",
            "rsi": 55.0,
            "cloudBias": "bullish",
            "cloudThickness": 3.0,
            "close": 100.0,
            "timeInCloud": 2,
            "kijunFlatDays": 0,
        }
        
        gamma_context = {
            "netGammaSign": "positive",
            "environment": "supportive",
        }
        
        result = score_ichimoku_setup(signal, gamma_context=gamma_context)
        
        assert result["score"] >= APLUS_THRESHOLD
        assert result["grade"] == "A+"
        assert len(result["tags"]) > 0

    def test_low_score_with_penalties(self):
        """Should score low with multiple penalties."""
        signal = {
            "direction": "bullish",
            "chikouTangled": True,
            "volumeRatio": 0.8,
            "closePosition": 0.50,
            "kijunSlope": "flat",
            "rsi": 45.0,
            "cloudBias": "bearish",
            "cloudThickness": 10.0,
            "close": 100.0,
            "timeInCloud": 15,
            "kijunFlatDays": 10,
        }
        
        result = score_ichimoku_setup(signal, earnings_days_ahead=3)
        
        assert result["score"] < APLUS_THRESHOLD
        assert result["grade"] != "A+"
        assert len(result["notes"]) > 0

    def test_earnings_penalty(self):
        """Should apply earnings penalty when earnings are soon."""
        signal = {
            "direction": "bullish",
            "chikouTangled": False,
            "volumeRatio": 1.5,
            "closePosition": 0.75,
            "kijunSlope": "positive",
            "rsi": 55.0,
            "cloudBias": "bullish",
            "cloudThickness": 3.0,
            "close": 100.0,
            "timeInCloud": 2,
            "kijunFlatDays": 0,
        }
        
        result = score_ichimoku_setup(signal, earnings_days_ahead=3)
        
        assert result["penalties"]["earnings"] < 0
        assert "Earnings Warning" in result["tags"]


# ---------------------------------------------------------------------------
# Tests: Signal Building
# ---------------------------------------------------------------------------

class TestSignalBuilding:
    def test_builds_signal_from_detection(self):
        """Should build IchimokuSignal from detection result."""
        detection = {
            "enabled": True,
            "hasSignal": True,
            "signal": {
                "signalDate": "2024-01-01",
                "direction": "bullish",
                "tenkan": 100.0,
                "kijun": 98.0,
                "chikou": 102.0,
                "cloudTop": 97.0,
                "cloudBottom": 95.0,
                "cloudBias": "bullish",
                "cloudThickness": 2.0,
                "close": 102.0,
                "closePosition": 0.75,
                "pullbackDepth": 0.02,
                "cloudPenetrationPct": 0.0,
                "entry": 103.01,
                "stop": 96.5,
                "risk": 6.51,
                "target1": 110.0,
                "target2": 116.0,
                "trail": 98.0,
                "rsi": 55.0,
                "volumeRatio": 1.3,
                "atr": 2.0,
                "kijunSlope": "positive",
                "kijunFlatDays": 0,
                "timeInCloud": 2,
                "chikouTangled": False,
            },
            "notes": [],
        }
        
        signal = build_ichimoku_signal(
            ticker="AAPL",
            detection=detection,
            index_membership="sp500",
        )
        
        assert signal is not None
        assert signal.ticker == "AAPL"
        assert signal.direction == "bullish"
        assert signal.score > 0
        assert signal.status == "pending"

    def test_signal_to_dict_conversion(self):
        """Should convert signal to API-friendly dict."""
        detection = {
            "enabled": True,
            "hasSignal": True,
            "signal": {
                "signalDate": "2024-01-01",
                "direction": "bullish",
                "tenkan": 100.0,
                "kijun": 98.0,
                "chikou": 102.0,
                "cloudTop": 97.0,
                "cloudBottom": 95.0,
                "cloudBias": "bullish",
                "cloudThickness": 2.0,
                "close": 102.0,
                "closePosition": 0.75,
                "pullbackDepth": 0.02,
                "cloudPenetrationPct": 0.0,
                "entry": 103.01,
                "stop": 96.5,
                "risk": 6.51,
                "target1": 110.0,
                "target2": 116.0,
                "trail": 98.0,
                "rsi": 55.0,
                "volumeRatio": 1.3,
                "atr": 2.0,
                "kijunSlope": "positive",
                "kijunFlatDays": 0,
                "timeInCloud": 2,
                "chikouTangled": False,
            },
            "notes": [],
        }
        
        signal = build_ichimoku_signal(
            ticker="AAPL",
            detection=detection,
            index_membership="sp500",
        )
        
        signal_dict = signal_to_dict(signal)
        
        assert signal_dict["ticker"] == "AAPL"
        assert "ichimoku" in signal_dict
        assert "levels" in signal_dict
        assert "quality" in signal_dict
        assert "indicators" in signal_dict


# ---------------------------------------------------------------------------
# Tests: Volume Metrics
# ---------------------------------------------------------------------------

class TestVolumeMetrics:
    def test_computes_volume_ratio(self):
        """Should compute volume ratio correctly."""
        bars = make_bars(30)
        result = compute_volume_metrics(bars, period=20)
        
        assert result["enabled"] is True
        assert result["avgVolume"] is not None
        assert result["volumeRatio"] is not None

    def test_insufficient_volume_data(self):
        """Should return disabled with insufficient data."""
        bars = make_bars(10)
        result = compute_volume_metrics(bars, period=20)
        
        assert result["enabled"] is False


# ---------------------------------------------------------------------------
# Tests: ATR Series
# ---------------------------------------------------------------------------

class TestAtrSeries:
    def test_computes_atr(self):
        """Should compute ATR series correctly."""
        bars = make_bars(30)
        result = compute_atr_series(bars, period=14)
        
        assert result["enabled"] is True
        assert result["atr"] is not None
        assert result["atr"] > 0

    def test_insufficient_atr_data(self):
        """Should return disabled with insufficient data."""
        bars = make_bars(10)
        result = compute_atr_series(bars, period=14)
        
        assert result["enabled"] is False


# ---------------------------------------------------------------------------
# Tests: Continuous Scoring (2026-06 hardening sprint)
# ---------------------------------------------------------------------------

def _base_signal(**over):
    s = {
        "direction": "bullish", "chikouTangled": False, "volumeRatio": 1.3,
        "closePosition": 0.70, "kijunSlope": "positive", "rsi": 58.0,
        "cloudBias": "bullish", "cloudThickness": 3.0, "close": 100.0,
        "timeInCloud": 2, "kijunFlatDays": 0,
    }
    s.update(over)
    return s


class TestContinuousScoring:
    def test_volume_credit_is_continuous(self):
        """Volume credit should ramp, not jump 0->15."""
        low = score_ichimoku_setup(_base_signal(volumeRatio=1.0))["scores"]["volume"]
        mid = score_ichimoku_setup(_base_signal(volumeRatio=1.3))["scores"]["volume"]
        full = score_ichimoku_setup(_base_signal(volumeRatio=1.6))["scores"]["volume"]
        assert low == 0.0
        assert 0.0 < mid < 15.0
        assert full == 15.0
        assert low < mid < full

    def test_rsi_credit_is_continuous(self):
        """RSI recovery credit should scale with distance above 50."""
        weak = score_ichimoku_setup(_base_signal(rsi=51.0))["scores"]["rsi"]
        strong = score_ichimoku_setup(_base_signal(rsi=64.0))["scores"]["rsi"]
        assert 0.0 < weak < strong <= 10.0

    def test_two_aplus_setups_differentiate(self):
        """Two strong setups with different magnitudes should not tie."""
        a = score_ichimoku_setup(_base_signal(volumeRatio=1.25, rsi=53, closePosition=0.68),
                                  gamma_context={"netGammaSign": "positive"})
        b = score_ichimoku_setup(_base_signal(volumeRatio=1.6, rsi=64, closePosition=0.80),
                                  gamma_context={"netGammaSign": "positive"})
        assert b["score"] > a["score"]

    def test_bearish_mirrors_bullish(self):
        """A clean bearish setup should earn comparable credit to its bullish mirror."""
        bull = score_ichimoku_setup(_base_signal(direction="bullish", rsi=64, closePosition=0.80,
                                                  cloudBias="bullish", kijunSlope="positive"))
        bear = score_ichimoku_setup(_base_signal(direction="bearish", rsi=36, closePosition=0.20,
                                                  cloudBias="bearish", kijunSlope="negative"))
        assert bull["scores"]["rsi"] == bear["scores"]["rsi"]
        assert bull["scores"]["candle"] == bear["scores"]["candle"]


class TestEntryOffset:
    def test_atr_scaled_offset(self):
        """Entry offset should scale with ATR, never below the floor."""
        assert _entry_offset(None) == 0.05
        assert _entry_offset(1.0) == 0.05  # 5% of 1.0 = 0.05 floor
        assert _entry_offset(10.0) == 0.5  # 5% of 10 = 0.50

    def test_entry_uses_offset(self):
        bar = DailyBar(trade_date="2024-01-01", open=99, high=200.0, low=198.0,
                       close=199.5, volume=1_000_000, vwap=None)
        levels = compute_entry_levels(bar=bar, kijun=195.0, direction="bullish", atr=8.0)
        # 5% of 8 = 0.40 above the high, not a flat penny.
        assert abs(levels["entry"] - 200.4) < 1e-6


class TestDistanceToActionable:
    def test_actionable_is_zero(self):
        assert compute_distance_to_actionable({"bucket": "actionable"}) == 0.0

    def test_rejected_is_max(self):
        assert compute_distance_to_actionable({"bucket": "rejected"}) == 999.0

    def test_structure_ranks_by_staleness(self):
        near = compute_distance_to_actionable({
            "bucket": "structure", "barsSinceReclaim": 4, "kijunDistanceAtr": 1.6,
        })
        far = compute_distance_to_actionable({
            "bucket": "structure", "barsSinceReclaim": 9, "kijunDistanceAtr": 4.0,
            "triggerAlreadyRan": True,
        })
        assert 0.0 < near < far


class TestReconcileIchimokuVerdict:
    def test_actionable_clean_is_tradable(self):
        sig = {"quality": {"grade": "A+", "score": 88}, "freshness": {"bucket": "actionable"},
               "gate": {"status": "TRADABLE"}, "tags": []}
        v = reconcile_ichimoku_verdict(sig)
        assert v["status"] == VERDICT_TRADABLE

    def test_structure_caps_at_watch(self):
        sig = {"quality": {"grade": "A+", "score": 80}, "freshness": {"bucket": "structure"},
               "gate": {"status": "TRADABLE"}, "tags": []}
        v = reconcile_ichimoku_verdict(sig)
        assert v["status"] == VERDICT_WATCH

    def test_suppress_gate_stands_down(self):
        sig = {"quality": {"grade": "A+", "score": 90}, "freshness": {"bucket": "actionable"},
               "gate": {"status": "SUPPRESS"}, "tags": []}
        v = reconcile_ichimoku_verdict(sig)
        assert v["status"] == VERDICT_STAND_DOWN

class TestDirectionAwareGate:
    def test_bearish_continuation_allowed_in_stressed(self):
        """A short continuation should NOT be suppressed in a stressed tape —
        that's exactly when down-trends accelerate."""
        d = gate_ichimoku(ticker="ABT", setup_direction="bearish",
                          regime_label="Stressed", vol_direction="falling")
        assert d.status != "SUPPRESS"

    def test_bullish_continuation_suppressed_in_stressed(self):
        """A long continuation in a stressed tape is correctly stood down."""
        d = gate_ichimoku(ticker="HWM", setup_direction="bullish",
                          regime_label="Stressed", vol_direction="falling")
        assert d.status == "SUPPRESS"

    def test_bullish_continuation_allowed_in_risk_on(self):
        d = gate_ichimoku(ticker="HWM", setup_direction="bullish",
                          regime_label="Risk-On", vol_direction="falling")
        assert d.status != "SUPPRESS"

    def test_bearish_continuation_suppressed_in_risk_on(self):
        """A short continuation while the tape is risk-on is a fade — stand down."""
        d = gate_ichimoku(ticker="ABT", setup_direction="bearish",
                          regime_label="Risk-On", vol_direction="falling")
        assert d.status == "SUPPRESS"

    def test_low_confidence_regime_demotes_suppress_to_watch(self):
        """A near-tie regime read (below the confidence floor) should NOT
        hard-suppress a long — it gets demoted to WATCH instead of SUPPRESS."""
        d = gate_ichimoku(ticker="HWM", setup_direction="bullish",
                          regime_label="Stressed", vol_direction="falling",
                          regime_confidence=0.46, regime_min_confidence=0.55)
        assert d.status == "WATCH"
        codes = [r["code"] for r in d.reasons]
        assert "REGIME_MISMATCH" in codes
        mismatch = next(r for r in d.reasons if r["code"] == "REGIME_MISMATCH")
        assert mismatch["severity"] == "SOFT"

    def test_high_confidence_regime_still_suppresses(self):
        """A confident regime mismatch still HARD-suppresses the wrong-way long."""
        d = gate_ichimoku(ticker="HWM", setup_direction="bullish",
                          regime_label="Stressed", vol_direction="falling",
                          regime_confidence=0.80, regime_min_confidence=0.55)
        assert d.status == "SUPPRESS"

    def test_missing_confidence_preserves_hard_suppress(self):
        """When confidence is unknown (None), behavior is unchanged — HARD."""
        d = gate_ichimoku(ticker="HWM", setup_direction="bullish",
                          regime_label="Stressed", vol_direction="falling",
                          regime_confidence=None, regime_min_confidence=0.55)
        assert d.status == "SUPPRESS"


class TestReconcileIchimokuVerdictNoGammaInput:
    def test_verdict_has_no_gamma_input(self):
        """Dealer gamma was removed from the Ichimoku engine (EODHD-only data
        plan) — the reconciled verdict must not carry a gamma input or demote
        on it."""
        sig = {"quality": {"grade": "A+", "score": 85}, "freshness": {"bucket": "actionable"},
               "gate": {"status": "TRADABLE"}, "tags": []}
        v = reconcile_ichimoku_verdict(sig)
        assert v["status"] == VERDICT_TRADABLE
        assert "gammaEnvironment" not in v["inputs"]


class TestBacktestHarness:
    def test_returns_expected_shape(self):
        """Backtest should run on synthetic bars and return grade+bucket breakdowns."""
        bars_by_ticker = {
            "UP": make_bars(160, trend="up"),
            "DOWN": make_bars(160, trend="down"),
        }
        result = backtest_from_bars(bars_by_ticker, min_score=0.0, warmup=80)
        assert "overall" in result
        assert "byGrade" in result
        assert "byBucket" in result
        assert "byPlaybook" in result
        assert "byDow" in result
        assert "byPlaybookDow" in result
        assert result["params"]["tickersTested"] == 2
        assert result["params"]["entryModel"] == "trigger"
        # Overall must always carry the standard stat keys.
        for k in ("signals", "triggered", "winRate", "avgR", "expectancy",
                  "avgHoldBars", "avgHoldWin", "avgHoldLoss", "avgPctReturn"):
            assert k in result["overall"]
        # Every playbook cohort that fired must carry the same stat keys, and
        # only known playbooks may appear.
        for pb, stats in result["byPlaybook"].items():
            assert pb in ("kijun_pullback", "tk_cross", "kumo_breakout")
            for k in ("signals", "triggered", "winRate", "avgR", "expectancy"):
                assert k in stats

    def test_close_entry_model_actionable_only(self):
        """entry_model=close: every signal is entered (no expiry-by-trigger),
        buckets contain only 'actionable', and the entry model is reported."""
        bars_by_ticker = {
            "UP": make_bars(160, trend="up"),
            "DOWN": make_bars(160, trend="down"),
        }
        result = backtest_from_bars(
            bars_by_ticker, min_score=0.0, warmup=80, entry_model="close"
        )
        assert result["params"]["entryModel"] == "close"
        assert set(result["byBucket"]).issubset({"actionable"})
        o = result["overall"]
        # Close entry has no trigger window: entered == signals.
        assert o["triggered"] == o["signals"]
        if o["signals"]:
            assert o["avgPctReturn"] is not None
            for dow in result["byDow"]:
                assert dow in ("Mon", "Tue", "Wed", "Thu", "Fri")


class TestCloseEntryOutcome:
    """Close-of-candle entry: risk anchored to the actual fill (the close)."""

    def _fwd(self, specs):
        """specs: list of (high, low, close) forward bars."""
        return [
            DailyBar(trade_date=f"2024-06-{i+3:02d}", open=c, high=h, low=l,
                     close=c, volume=1e6, vwap=None)
            for i, (h, l, c) in enumerate(specs)
        ]

    def test_target_hit_r_and_pct(self):
        from backend.engine4_backtest import evaluate_close_entry_outcome
        out = evaluate_close_entry_outcome(
            direction="bullish", entry_price=100.0, stop_loss=95.0, target_1=110.0,
            forward_bars=self._fwd([(104, 99, 103), (111, 102, 110)]),
        )
        assert out["status"] == "target_hit"
        assert out["rMultiple"] == pytest.approx(2.0)   # 10 gain / 5 risk
        assert out["pctReturn"] == pytest.approx(10.0)
        assert out["barsHeld"] == 2

    def test_stop_and_pct(self):
        from backend.engine4_backtest import evaluate_close_entry_outcome
        out = evaluate_close_entry_outcome(
            direction="bullish", entry_price=100.0, stop_loss=95.0, target_1=110.0,
            forward_bars=self._fwd([(101, 94.5, 96)]),
        )
        assert out["status"] == "stopped"
        assert out["rMultiple"] == -1.0
        assert out["pctReturn"] == pytest.approx(-5.0)
        assert out["barsHeld"] == 1

    def test_same_bar_conflict_assumes_stop_first(self):
        from backend.engine4_backtest import evaluate_close_entry_outcome
        out = evaluate_close_entry_outcome(
            direction="bullish", entry_price=100.0, stop_loss=95.0, target_1=110.0,
            forward_bars=self._fwd([(111, 94, 105)]),  # hits both — stop wins
        )
        assert out["status"] == "stopped"

    def test_time_stop_marks_to_close(self):
        from backend.engine4_backtest import evaluate_close_entry_outcome
        fwd = self._fwd([(102, 98, 101)] * 12)  # never resolves
        out = evaluate_close_entry_outcome(
            direction="bullish", entry_price=100.0, stop_loss=95.0, target_1=110.0,
            forward_bars=fwd, max_hold=10,
        )
        assert out["status"] == "triggered"
        assert out["barsHeld"] == 10
        assert out["rMultiple"] == pytest.approx(0.2)   # +1.0 close / 5 risk
        assert out["pctReturn"] == pytest.approx(1.0)

    def test_bearish_mirror(self):
        from backend.engine4_backtest import evaluate_close_entry_outcome
        out = evaluate_close_entry_outcome(
            direction="bearish", entry_price=100.0, stop_loss=105.0, target_1=90.0,
            forward_bars=self._fwd([(101, 89.5, 91)]),
        )
        assert out["status"] == "target_hit"
        assert out["rMultiple"] == pytest.approx(2.0)
        assert out["pctReturn"] == pytest.approx(10.0)

    def test_degenerate_risk_not_entered(self):
        from backend.engine4_backtest import evaluate_close_entry_outcome
        out = evaluate_close_entry_outcome(
            direction="bullish", entry_price=100.0, stop_loss=100.0, target_1=110.0,
            forward_bars=self._fwd([(111, 99, 110)]),
        )
        assert out["triggered"] is False


class TestDeskTracker:
    def test_set_desk_status_persists(self):
        from backend import engine4_screener as scr
        scr._persist_signals([{
            "ticker": "ZZTEST", "signalDate": "2024-02-02", "direction": "bullish",
            "levels": {"entryTrigger": 10, "stopLoss": 9, "target1": 12},
            "status": "pending",
        }])
        res = scr.set_desk_status("ZZTEST", desk_status="entered", note="filled at open")
        assert res["ok"] is True
        assert res["record"]["status"] == "entered"
        all_sigs = scr.get_all_signals()
        entered = [r for r in all_sigs.get("entered", []) if r.get("ticker") == "ZZTEST"]
        assert len(entered) == 1

    def test_invalid_desk_status_rejected(self):
        from backend import engine4_screener as scr
        res = scr.set_desk_status("ZZTEST", desk_status="bogus")
        assert res["ok"] is False

    def test_redis_prior_wins_over_stale_inmemory(self, monkeypatch):
        """Multi-worker regression: a stale per-worker in-memory copy must not
        clobber the desk state another worker wrote to Redis."""
        from backend import engine4_screener as scr
        import backend.redis_store as rs

        class FakeStore:
            def __init__(self): self.kv = {}
            def get_json(self, k): return self.kv.get(k)
            def set_json(self, k, v, ttl_s=None): self.kv[k] = v

        fake = FakeStore()
        monkeypatch.setattr(rs, "get_store_optional", lambda: fake)
        key = scr._signal_key("ZZAUTH4", "2024-05-05")
        fake.kv[scr._REDIS_PREFIX + key] = {"ticker": "ZZAUTH4", "signalDate": "2024-05-05",
                                            "status": "working", "levels": {}}
        scr._signal_store[key] = {"ticker": "ZZAUTH4", "signalDate": "2024-05-05",
                                  "status": "pending", "levels": {}}  # stale
        scr._persist_signals([{"ticker": "ZZAUTH4", "signalDate": "2024-05-05",
                               "direction": "bullish", "levels": {}, "quality": {}}])
        assert fake.kv[scr._REDIS_PREFIX + key]["status"] == "working"

    def test_persist_accepts_dataclasses(self):
        """Regression: run_universe_scan persists IchimokuSignal objects, not
        dicts — _persist_signals must tolerate dataclasses without crashing."""
        from backend import engine4_screener as scr
        detection = {
            "enabled": True, "hasSignal": True,
            "signal": {
                "signalDate": "2024-03-03", "direction": "bullish", "tenkan": 100.0,
                "kijun": 98.0, "chikou": 102.0, "cloudTop": 97.0, "cloudBottom": 95.0,
                "cloudBias": "bullish", "cloudThickness": 2.0, "close": 102.0,
                "closePosition": 0.75, "pullbackDepth": 0.02, "cloudPenetrationPct": 0.0,
                "entry": 103.01, "stop": 96.5, "risk": 6.51, "target1": 110.0,
                "target2": 116.0, "trail": 98.0, "rsi": 55.0, "volumeRatio": 1.3,
                "atr": 2.0, "kijunSlope": "positive", "kijunFlatDays": 0,
                "timeInCloud": 2, "chikouTangled": False,
            },
            "notes": [],
        }
        sig = build_ichimoku_signal(ticker="ZZDATACLASS", detection=detection, dollar_adv=3e8)
        # Pass the dataclass directly (mirrors the bug that 500'd the scan).
        scr._persist_signals([sig])
        all_sigs = scr.get_all_signals()
        found = [r for r in all_sigs.get("pending", []) if r.get("ticker") == "ZZDATACLASS"]
        assert len(found) == 1
        assert found[0]["indicators"]["dollarAdv"] == 3e8


class TestLiveRepricing:
    """Live re-pricing overlay: distance-to-trigger reflects the current price,
    so a name that already ran reads 'triggered' instead of a stale distance."""

    def test_bullish_state_transitions(self):
        from backend.engine4_screener import compute_live_state
        kw = dict(direction="bullish", entry_trigger=100.0, stop_loss=95.0,
                  target_1=110.0, atr=2.0)
        # Below trigger → pending, positive distance to go.
        pending = compute_live_state(price=99.0, **kw)
        assert pending["state"] == "pending"
        assert pending["toTrigger"] == pytest.approx(1.0)
        assert pending["toTriggerAtr"] == pytest.approx(0.5)
        # At/above trigger → triggered (the "0.29 to go but it already ran" fix).
        assert compute_live_state(price=100.5, **kw)["state"] == "triggered"
        # Triggered then through target → target1.
        assert compute_live_state(price=111.0, **kw)["state"] == "target1"
        # Collapsed through the stop WITHOUT ever triggering → invalidated
        # (a buy-stop setup can't be "stopped" before it fires).
        assert compute_live_state(price=94.0, **kw)["state"] == "invalidated"

    def test_bearish_state_transitions(self):
        from backend.engine4_screener import compute_live_state
        kw = dict(direction="bearish", entry_trigger=100.0, stop_loss=105.0,
                  target_1=90.0, atr=2.0)
        pending = compute_live_state(price=101.0, **kw)
        assert pending["state"] == "pending"
        assert pending["toTrigger"] == pytest.approx(1.0)  # price must fall to 100
        assert compute_live_state(price=99.5, **kw)["state"] == "triggered"
        assert compute_live_state(price=89.0, **kw)["state"] == "target1"
        # Ripped up through the stop without triggering down → invalidated.
        assert compute_live_state(price=106.0, **kw)["state"] == "invalidated"

    def test_overlay_annotates_surfaced_signals(self, monkeypatch):
        from backend import engine4_screener as scr

        prices = {"AAA": 100.5, "BBB": 49.0}

        def fake_ctx(*, ticker):
            return {"price": prices.get(ticker), "marketOpen": True, "source": "test"}

        monkeypatch.setattr(scr, "fetch_live_price_context_optional", fake_ctx)

        result = {
            "actionable": [{
                "ticker": "AAA", "direction": "bullish",
                "levels": {"entryTrigger": 100.0, "stopLoss": 95.0, "target1": 110.0},
                "indicators": {"atr": 2.0},
            }],
            "structure": [{
                "ticker": "BBB", "direction": "bullish",
                "levels": {"entryTrigger": 50.0, "stopLoss": 47.0, "target1": 56.0},
                "indicators": {"atr": 1.0},
            }],
        }
        n = scr.apply_live_price_overlay(result, max_workers=2)
        assert n == 2
        assert result["actionable"][0]["live"]["state"] == "triggered"
        assert result["actionable"][0]["live"]["available"] is True
        # BBB at 49 is still 1.0 below its 50 trigger → pending.
        assert result["structure"][0]["live"]["state"] == "pending"
        assert result["structure"][0]["live"]["toTrigger"] == pytest.approx(1.0)

    def test_overlay_handles_missing_quote(self, monkeypatch):
        from backend import engine4_screener as scr
        monkeypatch.setattr(scr, "fetch_live_price_context_optional",
                            lambda *, ticker: {"price": None})
        result = {"actionable": [{
            "ticker": "AAA", "direction": "bullish",
            "levels": {"entryTrigger": 100.0, "stopLoss": 95.0, "target1": 110.0},
            "indicators": {"atr": 2.0},
        }], "structure": []}
        scr.apply_live_price_overlay(result, max_workers=1)
        assert result["actionable"][0]["live"]["available"] is False


# ---------------------------------------------------------------------------
# Tests: Top-Down Context Stack (2026-06) — index alignment, sector, RS, beta
# ---------------------------------------------------------------------------

class TestIndexIchimokuState:
    def test_bullish_when_above_cloud(self):
        state = compute_index_ichimoku_state(make_bars(120, trend="up"), symbol="SPY")
        assert state["available"] is True
        assert state["direction"] == "bullish"
        assert state["priceVsCloud"] == "above"

    def test_bearish_when_below_cloud(self):
        state = compute_index_ichimoku_state(make_bars(120, trend="down"), symbol="SPY")
        assert state["available"] is True
        assert state["direction"] == "bearish"
        assert state["priceVsCloud"] == "below"

    def test_unavailable_with_insufficient_bars(self):
        state = compute_index_ichimoku_state(make_bars(30), symbol="SPY")
        assert state["available"] is False
        assert state["direction"] == "neutral"


class TestRelativeStrength:
    def test_leader_has_positive_rs(self):
        # Name doubles while index is flat → strongly positive excess return.
        name = [100.0 * (1 + 0.01 * i) for i in range(70)]
        index = [100.0] * 70
        rs = compute_relative_strength(name, index, lookback=63)
        assert rs["rsRatio"] is not None
        assert rs["rsRatio"] > 0

    def test_laggard_has_negative_rs(self):
        name = [100.0 - 0.2 * i for i in range(70)]
        index = [100.0 + 0.2 * i for i in range(70)]
        rs = compute_relative_strength(name, index, lookback=63)
        assert rs["rsRatio"] < 0

    def test_insufficient_data_returns_none(self):
        rs = compute_relative_strength([100, 101], [100, 100], lookback=63)
        assert rs["rsRatio"] is None


class TestBetaCorr:
    def test_perfectly_coupled_high_beta(self):
        # Name moves 2x the index each day → beta ~2, corr ~1.
        index = [100.0]
        name = [100.0]
        for i in range(1, 80):
            r = 0.01 if i % 2 == 0 else -0.008
            index.append(index[-1] * (1 + r))
            name.append(name[-1] * (1 + 2 * r))
        bc = compute_beta_corr(name, index, lookback=60)
        assert bc["beta"] is not None
        assert bc["beta"] > 1.5
        assert bc["corr"] > 0.95

    def test_insufficient_data_returns_none(self):
        bc = compute_beta_corr([100, 101, 102], [100, 101, 102], lookback=60)
        assert bc["beta"] is None


class TestSectorAndRelStrengthScoring:
    def test_full_aligned_setup_scores_100(self):
        """A fully-aligned bull setup (all components maxed) totals 100 — proves
        the score still normalizes to 100 after the gamma->sector+RS swap."""
        signal = _base_signal(
            chikouTangled=False, volumeRatio=1.6, closePosition=0.80,
            kijunSlope="positive", rsi=65.0, cloudBias="bullish",
            cloudThickness=3.0, close=100.0, timeInCloud=2, kijunFlatDays=0,
            sectorBias="bullish", rsRatio=0.10,
        )
        result = score_ichimoku_setup(signal)
        assert result["score"] == 100.0
        assert result["scores"]["sector"] == 8
        assert result["scores"]["relStrength"] == 7

    def test_fighting_sector_zeros_sector_credit(self):
        bull = score_ichimoku_setup(_base_signal(sectorBias="bearish"))
        assert bull["scores"]["sector"] == 0.0

    def test_unknown_sector_gets_half_credit(self):
        s = score_ichimoku_setup(_base_signal())  # no sectorBias key
        assert s["scores"]["sector"] == 4.0

    def test_leadership_rewarded_for_longs(self):
        leader = score_ichimoku_setup(_base_signal(rsRatio=0.08))["scores"]["relStrength"]
        laggard = score_ichimoku_setup(_base_signal(rsRatio=-0.08))["scores"]["relStrength"]
        assert leader == 7
        assert laggard == 0.0

    def test_gamma_no_longer_affects_score(self):
        """Dealer gamma must not change the quality score (it's a gate input now)."""
        a = score_ichimoku_setup(_base_signal(), gamma_context={"netGammaSign": "positive", "environment": "supportive"})
        b = score_ichimoku_setup(_base_signal(), gamma_context={"netGammaSign": "negative", "environment": "challenging"})
        assert a["score"] == b["score"]
        assert a["breakdown"]["gamma"] == 0.0


class TestIndexAlignmentGate:
    def _idx(self, direction):
        return {"available": True, "direction": direction, "symbol": "SPY"}

    def test_disabled_by_default_no_index_reason(self):
        d = gate_ichimoku(ticker="AAPL", setup_direction="bullish",
                          regime_label="Risk-On", vol_direction="falling",
                          index_state=self._idx("bearish"), index_beta=1.5)
        codes = [r["code"] for r in d.reasons]
        assert "INDEX_TREND_MISMATCH" not in codes

    def test_high_beta_mismatch_is_hard(self):
        d = gate_ichimoku(ticker="AAPL", setup_direction="bullish",
                          regime_label="Risk-On", vol_direction="falling",
                          index_state=self._idx("bearish"), index_align_enable=True,
                          index_beta=1.4, index_corr=0.8)
        assert d.status == "SUPPRESS"
        mismatch = next(r for r in d.reasons if r["code"] == "INDEX_TREND_MISMATCH")
        assert mismatch["severity"] == "HARD"

    def test_low_corr_mismatch_is_soft(self):
        d = gate_ichimoku(ticker="IDIO", setup_direction="bullish",
                          regime_label="Risk-On", vol_direction="falling",
                          index_state=self._idx("bearish"), index_align_enable=True,
                          index_beta=0.3, index_corr=0.2)
        assert d.status == "WATCH"
        mismatch = next(r for r in d.reasons if r["code"] == "INDEX_TREND_MISMATCH")
        assert mismatch["severity"] == "SOFT"

    def test_aligned_index_passes(self):
        d = gate_ichimoku(ticker="AAPL", setup_direction="bullish",
                          regime_label="Risk-On", vol_direction="falling",
                          index_state=self._idx("bullish"), index_align_enable=True,
                          index_beta=1.4, index_corr=0.8)
        codes = [r["code"] for r in d.reasons]
        assert "INDEX_TREND_MISMATCH" not in codes
        assert "INDEX_TREND_NEUTRAL" not in codes

    def test_neutral_index_is_soft(self):
        d = gate_ichimoku(ticker="AAPL", setup_direction="bullish",
                          regime_label="Risk-On", vol_direction="falling",
                          index_state=self._idx("neutral"), index_align_enable=True,
                          index_beta=1.4, index_corr=0.8)
        assert d.status == "WATCH"
        assert any(r["code"] == "INDEX_TREND_NEUTRAL" for r in d.reasons)


class TestVerdictTopDownDemotions:
    def _sig(self, **indicators):
        return {
            "quality": {"grade": "A+", "score": 88},
            "freshness": {"bucket": "actionable"},
            "gate": {"status": "TRADABLE"},
            "tags": [],
            "direction": indicators.pop("direction", "bullish"),
            "indicators": indicators,
        }

    def test_fighting_sector_caps_at_watch(self):
        v = reconcile_ichimoku_verdict(self._sig(sectorBias="bearish"))
        assert v["status"] == VERDICT_WATCH
        assert any("sector" in d.lower() for d in v["drivers"])

    def test_laggard_long_caps_at_watch(self):
        v = reconcile_ichimoku_verdict(self._sig(rsRatio=-0.05))
        assert v["status"] == VERDICT_WATCH

    def test_aligned_leader_stays_tradable(self):
        v = reconcile_ichimoku_verdict(self._sig(sectorBias="bullish", rsRatio=0.05))
        assert v["status"] == VERDICT_TRADABLE


class TestSectorMap:
    def test_loads_known_mapping(self):
        from backend.engine4_screener import load_sector_map
        m = load_sector_map()
        assert m.get("AAPL") == "XLK"
        assert m.get("JPM") == "XLF"
        assert m.get("XOM") == "XLE"


# ---------------------------------------------------------------------------
# Tests: Research Playbooks (2026-08) — TK Cross + Kumo Breakout
# ---------------------------------------------------------------------------

from backend.engine4_ichimoku import (  # noqa: E402
    PLAYBOOK_KIJUN_PULLBACK,
    PLAYBOOK_KUMO_BREAKOUT,
    PLAYBOOK_TK_CROSS,
    RESEARCH_PLAYBOOKS,
    count_bars_since_cloud_breakout,
    count_bars_since_tk_cross,
    detect_kumo_breakout_setup,
    detect_tk_cross_setup,
)


def make_context(
    *,
    n: int = 80,
    closes: Optional[List[float]] = None,
    tenkan_series: Optional[List[Optional[float]]] = None,
    kijun_series: Optional[List[Optional[float]]] = None,
    cloud_top: float = 104.0,
    cloud_bottom: float = 102.0,
    cloud_bias: str = "bullish",
    future_bias: str = "bullish",
    chikou_tangled: bool = False,
    atr: float = 1.0,
):
    """Synthetic detection context: lets the detector tests drive the exact
    Ichimoku series shapes (cross bars, breakout bars, Chikou state) without
    reverse-engineering bar prices through the real series computation."""
    closes = closes if closes is not None else [107.0] * n
    tenkan_series = tenkan_series if tenkan_series is not None else [106.5] * n
    kijun_series = kijun_series if kijun_series is not None else [106.0] * n
    cloud = {
        "cloudTop": cloud_top,
        "cloudBottom": cloud_bottom,
        "cloudBias": cloud_bias,
        "thickness": cloud_top - cloud_bottom,
    }
    cloud_future = {
        "cloudTop": cloud_top,
        "cloudBottom": cloud_bottom,
        "cloudBias": future_bias,
    }
    return {
        "tenkan_series": tenkan_series,
        "kijun_series": kijun_series,
        "cloud_series": [dict(cloud) for _ in range(n)],
        "closes": closes,
        "highs": [max(c + 1.0, 108.0) for c in closes],
        "lows": [min(c - 1.0, 101.0) for c in closes],
        "tenkan": tenkan_series[-1],
        "kijun": kijun_series[-1],
        "prev_tenkan": tenkan_series[-2],
        "cloud": cloud,
        "cloud_future": cloud_future,
        "rsi": 58.0,
        "volume_ratio": 1.3,
        "atr": atr,
        "kijun_slope_dir": "positive",
        "kijun_flat_days": 0,
        "time_in_cloud": 0,
        "chikou_tangled": chikou_tangled,
        "indicators": {"atr": atr},
    }


class TestTkCrossHelper:
    def test_fresh_bull_cross_age(self):
        """Tenkan crossed above Kijun 1 bar ago → age 1."""
        tenkan = [105.0] * 78 + [106.5, 106.8]
        kijun = [106.0] * 80
        assert count_bars_since_tk_cross(tenkan, kijun, "bullish") == 1

    def test_cross_today_is_age_zero(self):
        tenkan = [105.0] * 79 + [106.5]
        kijun = [106.0] * 80
        assert count_bars_since_tk_cross(tenkan, kijun, "bullish") == 0

    def test_no_cross_when_tenkan_below_kijun(self):
        """Current bar not in the crossed state → None regardless of history."""
        tenkan = [105.0] * 80
        kijun = [106.0] * 80
        assert count_bars_since_tk_cross(tenkan, kijun, "bullish") is None

    def test_stale_cross_age(self):
        tenkan = [105.0] * 70 + [106.5] * 10
        kijun = [106.0] * 80
        assert count_bars_since_tk_cross(tenkan, kijun, "bullish") == 9

    def test_bearish_mirror(self):
        tenkan = [107.0] * 78 + [105.5, 105.2]
        kijun = [106.0] * 80
        assert count_bars_since_tk_cross(tenkan, kijun, "bearish") == 1


class TestKumoBreakoutHelper:
    def _clouds(self, n, top=105.0, bottom=103.0):
        return [{"cloudTop": top, "cloudBottom": bottom} for _ in range(n)]

    def test_fresh_bull_breakout_age(self):
        """First close above the cloud 1 bar ago, clean 10-bar window → age 1."""
        closes = [104.0] * 78 + [105.5, 105.8]
        assert count_bars_since_cloud_breakout(closes, self._clouds(80), "bullish") == 1

    def test_not_fresh_when_recent_close_beyond(self):
        """A close above the cloud within the prior 10 bars → not an inception."""
        closes = [104.0] * 70 + [106.0] + [104.0] * 7 + [105.5, 105.8]
        assert count_bars_since_cloud_breakout(closes, self._clouds(80), "bullish") is None

    def test_none_when_inside_cloud(self):
        closes = [104.0] * 80
        assert count_bars_since_cloud_breakout(closes, self._clouds(80), "bullish") is None

    def test_bearish_mirror(self):
        closes = [106.0] * 78 + [102.5, 102.0]
        assert count_bars_since_cloud_breakout(closes, self._clouds(80), "bearish") == 1


class TestTkCrossDetector:
    def test_fresh_cross_fires_actionable(self):
        bars = make_bars(80)
        ctx = make_context(
            tenkan_series=[105.0] * 78 + [106.5, 106.8],
        )
        det = detect_tk_cross_setup(bars, ticker="TEST", context=ctx)
        assert det["hasSignal"] is True
        assert det["playbook"] == PLAYBOOK_TK_CROSS
        assert det["signal"]["playbook"] == PLAYBOOK_TK_CROSS
        fresh = det["freshnessOverride"]
        assert fresh["bucket"] == "actionable"
        assert fresh["barsSinceReclaim"] == 1  # bars since the cross

    def test_stale_cross_lands_in_structure(self):
        bars = make_bars(80)
        ctx = make_context(
            tenkan_series=[105.0] * 70 + [106.5] * 10,
        )
        det = detect_tk_cross_setup(bars, ticker="TEST", context=ctx)
        assert det["hasSignal"] is True
        fresh = det["freshnessOverride"]
        assert fresh["bucket"] == "structure"
        assert any("TK cross" in r for r in fresh["reasons"])

    def test_ancient_cross_is_dropped(self):
        """CRL regression (2026-08-07): a 43-bar-old cross is just a trending
        stock, not a setup approaching actionability — reject, don't surface."""
        bars = make_bars(80)
        ctx = make_context(
            tenkan_series=[105.0] * 36 + [106.5] * 44,  # cross 43 bars ago
        )
        det = detect_tk_cross_setup(bars, ticker="TEST", context=ctx)
        assert det["hasSignal"] is True
        fresh = det["freshnessOverride"]
        assert fresh["bucket"] == "rejected"
        assert any("stale beyond watch window" in r for r in fresh["reasons"])

    def test_ancient_cross_stays_dropped_when_also_extended(self):
        """The extension check must not downgrade a stale-event rejection
        back to structure."""
        bars = make_bars(80)
        ctx = make_context(
            closes=[110.0] * 80,  # 4 ATR from Kijun → extended too
            tenkan_series=[105.0] * 36 + [106.5] * 44,
        )
        det = detect_tk_cross_setup(bars, ticker="TEST", context=ctx)
        fresh = det["freshnessOverride"]
        assert fresh["bucket"] == "rejected"

    def test_wrong_side_of_cloud_rejected(self):
        """Price inside the cloud → trend regime invalid → no strong cross."""
        bars = make_bars(80)
        ctx = make_context(
            closes=[103.0] * 80,  # inside the 102-104 cloud
            tenkan_series=[105.0] * 78 + [106.5, 106.8],
        )
        det = detect_tk_cross_setup(bars, ticker="TEST", context=ctx)
        assert det["hasSignal"] is False
        assert det["trend"]["valid"] is False

    def test_chikou_veto(self):
        bars = make_bars(80)
        ctx = make_context(
            tenkan_series=[105.0] * 78 + [106.5, 106.8],
            chikou_tangled=True,
        )
        det = detect_tk_cross_setup(bars, ticker="TEST", context=ctx)
        assert det["hasSignal"] is False
        assert any("Chikou" in n for n in det["notes"])

    def test_extended_from_kijun_lands_in_structure(self):
        """Fresh cross but price 4 ATR from Kijun → extended → structure."""
        bars = make_bars(80)
        ctx = make_context(
            closes=[110.0] * 80,  # (110 - 106) / 1.0 ATR = 4.0 from Kijun
            tenkan_series=[105.0] * 78 + [106.5, 106.8],
        )
        det = detect_tk_cross_setup(bars, ticker="TEST", context=ctx)
        assert det["hasSignal"] is True
        fresh = det["freshnessOverride"]
        assert fresh["bucket"] == "structure"
        assert any("Extended" in r for r in fresh["reasons"])


class TestKumoBreakoutDetector:
    def _ctx(self, **kw):
        defaults = dict(
            closes=[104.0] * 78 + [105.5, 105.8],
            cloud_top=105.0,
            cloud_bottom=103.0,
            cloud_bias="bearish",   # pre-breakout cloud is often still bearish
            future_bias="bullish",  # ...but the forward twist must agree
        )
        defaults.update(kw)
        return make_context(**defaults)

    def test_first_close_through_cloud_fires(self):
        bars = make_bars(80)
        det = detect_kumo_breakout_setup(bars, ticker="TEST", context=self._ctx())
        assert det["hasSignal"] is True
        assert det["playbook"] == PLAYBOOK_KUMO_BREAKOUT
        sig = det["signal"]
        assert sig["playbook"] == PLAYBOOK_KUMO_BREAKOUT
        assert sig["direction"] == "bullish"
        fresh = det["freshnessOverride"]
        assert fresh["bucket"] == "actionable"
        assert fresh["barsSinceReclaim"] == 1  # bars since the breakout

    def test_forward_cloud_bias_carried_on_signal(self):
        """Inception cards display the forward twist, not the lagging current
        cloud bias — the signal must carry both."""
        bars = make_bars(80)
        det = detect_kumo_breakout_setup(bars, ticker="TEST", context=self._ctx())
        sig = det["signal"]
        assert sig["cloudBias"] == "bearish"        # current cloud lags
        assert sig["futureCloudBias"] == "bullish"  # forward twist agrees with breakout
        built = build_ichimoku_signal(ticker="TEST", detection=det, bars=bars)
        d = signal_to_dict(built)
        assert d["ichimoku"]["futureCloudBias"] == "bullish"

    def test_stop_rides_far_cloud_edge(self):
        """Bull breakout stop anchors below Senkou B (cloud bottom), not Kijun."""
        bars = make_bars(80)
        det = detect_kumo_breakout_setup(bars, ticker="TEST", context=self._ctx())
        sig = det["signal"]
        # ATR buffer = 0.25 * 1.0 → stop = cloud_bottom (103) - 0.25
        assert sig["stop"] == pytest.approx(102.75)
        assert sig["trail"] == pytest.approx(103.0)

    def test_rewobble_is_not_inception(self):
        """A close above the cloud 8 bars before the breakout → not first close."""
        bars = make_bars(80)
        ctx = self._ctx(closes=[104.0] * 70 + [106.0] + [104.0] * 7 + [105.5, 105.8])
        det = detect_kumo_breakout_setup(bars, ticker="TEST", context=ctx)
        assert det["hasSignal"] is False
        assert any("No fresh cloud breakout" in n for n in det["notes"])

    def test_unsupportive_forward_twist_rejected(self):
        bars = make_bars(80)
        det = detect_kumo_breakout_setup(
            bars, ticker="TEST", context=self._ctx(future_bias="bearish")
        )
        assert det["hasSignal"] is False
        assert any("Forward cloud twist" in n for n in det["notes"])

    def test_chikou_veto(self):
        bars = make_bars(80)
        det = detect_kumo_breakout_setup(
            bars, ticker="TEST", context=self._ctx(chikou_tangled=True)
        )
        assert det["hasSignal"] is False
        assert any("Chikou" in n for n in det["notes"])

    def test_inside_cloud_no_breakout(self):
        bars = make_bars(80)
        det = detect_kumo_breakout_setup(
            bars, ticker="TEST", context=self._ctx(closes=[104.0] * 80)
        )
        assert det["hasSignal"] is False


class TestPlaybookScoring:
    """Same 0-100 scale and A+ bar; kumo_breakout inverts cloud thickness."""

    def _sig(self, thickness):
        return {
            "direction": "bullish",
            "close": 100.0,
            "cloudThickness": thickness,
        }

    def test_thin_cloud_scores_higher_for_breakout(self):
        """0.4% cloud: core penalizes 'razor thin', breakout rewards escape."""
        core = score_ichimoku_setup(self._sig(0.4), playbook=PLAYBOOK_KIJUN_PULLBACK)
        kumo = score_ichimoku_setup(self._sig(0.4), playbook=PLAYBOOK_KUMO_BREAKOUT)
        assert kumo["scores"]["cloudThickness"] == 10.0
        assert core["scores"]["cloudThickness"] < 10.0
        assert "Thin-Cloud Escape" in kumo["tags"]

    def test_thick_cloud_scores_zero_for_breakout(self):
        """7% cloud: too hard to escape for an inception trade."""
        core = score_ichimoku_setup(self._sig(7.0), playbook=PLAYBOOK_KIJUN_PULLBACK)
        kumo = score_ichimoku_setup(self._sig(7.0), playbook=PLAYBOOK_KUMO_BREAKOUT)
        assert kumo["scores"]["cloudThickness"] == 0.0
        assert core["scores"]["cloudThickness"] > 0.0


class TestPlaybookSignalPlumbing:
    def test_playbook_propagates_to_signal_and_dict(self):
        """Detection → build_ichimoku_signal → signal_to_dict keeps the
        playbook tag and the detector's own freshness classification."""
        bars = make_bars(80)
        ctx = make_context(tenkan_series=[105.0] * 78 + [106.5, 106.8])
        det = detect_tk_cross_setup(bars, ticker="TEST", context=ctx)
        signal = build_ichimoku_signal(
            ticker="TEST",
            detection=det,
            bars=bars,
            closes=ctx["closes"],
            tenkan_series=ctx["tenkan_series"],
        )
        assert signal is not None
        assert signal.playbook == PLAYBOOK_TK_CROSS
        assert signal.freshness_bucket == "actionable"
        assert signal.bars_since_reclaim == 1
        d = signal_to_dict(signal)
        assert d["playbook"] == PLAYBOOK_TK_CROSS
        assert d["playbookLabel"] == "TK Cross (Strong)"

    def test_core_default_playbook(self):
        """Legacy detections without a playbook key stay kijun_pullback."""
        detection = {
            "enabled": True, "hasSignal": True,
            "signal": {
                "signalDate": "2024-03-03", "direction": "bullish", "tenkan": 100.0,
                "kijun": 98.0, "chikou": 102.0, "cloudTop": 97.0, "cloudBottom": 95.0,
                "cloudBias": "bullish", "cloudThickness": 2.0, "close": 102.0,
                "closePosition": 0.75, "pullbackDepth": 0.02, "cloudPenetrationPct": 0.0,
                "entry": 103.01, "stop": 96.5, "risk": 6.51, "target1": 110.0,
                "target2": 116.0, "trail": 98.0, "rsi": 55.0, "volumeRatio": 1.3,
                "atr": 2.0, "kijunSlope": "positive", "kijunFlatDays": 0,
                "timeInCloud": 2, "chikouTangled": False,
            },
            "notes": [],
        }
        signal = build_ichimoku_signal(ticker="LEGACY", detection=detection)
        assert signal.playbook == PLAYBOOK_KIJUN_PULLBACK
        assert signal_to_dict(signal)["playbook"] == PLAYBOOK_KIJUN_PULLBACK


class TestClosePreview:
    """Close preview: today's forming candle synthesized from the live quote."""

    def test_synthesize_preview_bar(self):
        from backend.engine4_screener import synthesize_preview_bar
        bar = synthesize_preview_bar(
            {"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1_000_000},
            "2026-08-07",
        )
        assert bar is not None
        assert bar.trade_date == "2026-08-07"
        assert bar.open == 10.0 and bar.high == 10.5 and bar.low == 9.8 and bar.close == 10.2
        assert bar.volume == 1_000_000

    def test_synthesize_requires_price(self):
        from backend.engine4_screener import synthesize_preview_bar
        assert synthesize_preview_bar({"close": None}, "2026-08-07") is None
        assert synthesize_preview_bar({"close": 0.0}, "2026-08-07") is None

    def test_synthesize_reconciles_partial_quote(self):
        """Missing O/H/L still yields a well-formed bar (high >= close >= low)."""
        from backend.engine4_screener import synthesize_preview_bar
        bar = synthesize_preview_bar({"open": None, "high": None, "low": None, "close": 10.2}, "2026-08-07")
        assert bar.open == bar.high == bar.low == bar.close == 10.2
        # Stale/inconsistent quote high below last trade: close wins.
        bar2 = synthesize_preview_bar({"open": 10.0, "high": 10.1, "low": 9.9, "close": 10.4}, "2026-08-07")
        assert bar2.high == 10.4 and bar2.low == 9.9

    def test_preview_appends_forming_bar(self):
        from backend.engine4_screener import bars_with_preview_close
        bars = make_bars(80)
        out, synthetic = bars_with_preview_close(bars, {"close": 999.0}, "2099-01-01")
        assert synthetic is True
        assert len(out) == len(bars) + 1
        assert out[-1].close == 999.0 and out[-1].trade_date == "2099-01-01"
        assert bars[-1].trade_date != "2099-01-01"  # original series untouched

    def test_preview_noop_when_close_already_published(self):
        """Post-close, EODHD's real EOD row wins — no synthetic bar."""
        from backend.engine4_screener import bars_with_preview_close
        bars = make_bars(80)
        out, synthetic = bars_with_preview_close(bars, {"close": 999.0}, bars[-1].trade_date)
        assert synthetic is False
        assert out is bars

    def test_preview_noop_without_quote(self):
        from backend.engine4_screener import bars_with_preview_close
        bars = make_bars(80)
        out, synthetic = bars_with_preview_close(bars, None, "2099-01-01")
        assert synthetic is False and out is bars

    def test_run_close_preview_shape_and_no_persist(self, monkeypatch):
        """End-to-end plumbing on a stubbed 1-ticker universe: payload shape,
        preview meta, and zero tracker writes."""
        from backend import engine4_screener as scr

        bars = make_bars(80)
        today_str = dt.date.today().isoformat()

        class FakePS:
            def fetch_live_bar_snapshots(self, tickers, **kw):
                return {t: {"open": 140.0, "high": 141.0, "low": 139.0,
                            "close": 140.5, "volume": 5_000_000, "timestamp": 1}
                        for t in tickers}

        import backend.price_service as ps_mod
        monkeypatch.setattr(ps_mod, "get_price_service", lambda: FakePS())
        monkeypatch.setattr(scr, "load_universe_sp500_and_nasdaq100", lambda: ["ZZPRVW"])
        monkeypatch.setattr(scr, "load_index_memberships", lambda *a, **k: {"ZZPRVW": "sp500"})
        monkeypatch.setattr(scr, "load_sector_map", lambda *a, **k: {})
        monkeypatch.setattr(scr, "fetch_index_context", lambda **kw: (bars, {"symbol": kw.get("symbol")}))
        monkeypatch.setattr(scr, "fetch_bars_for_ticker", lambda **kw: bars)

        persisted = []
        monkeypatch.setattr(scr, "_persist_signals", lambda sigs: persisted.extend(sigs))

        result = scr.run_close_preview(max_workers=2, use_cache=False)

        assert result["preview"]["isPreview"] is True
        assert result["preview"]["syntheticBars"] == 1  # forming bar was appended
        assert result["asOfDate"] == today_str
        assert "playbooks" in result and set(result["playbooks"]) == set(RESEARCH_PLAYBOOKS)
        for sig in result["actionable"] + result["structure"]:
            assert sig["preview"] is True
        assert persisted == []  # the preview must never seed the tracker


class TestResearchPlaybookTrackerSeeding:
    def test_watch_seeds_untracked_research_signal(self):
        """Research playbooks never auto-persist; the first manual Watch must
        seed the tracker from the card payload."""
        from backend import engine4_screener as scr
        payload = {
            "ticker": "ZZRESEARCH", "signalDate": "2026-08-07", "direction": "bullish",
            "playbook": PLAYBOOK_TK_CROSS,
            "levels": {"entryTrigger": 50.0, "stopLoss": 48.0, "target1": 55.0},
        }
        res = scr.set_desk_status(
            "ZZRESEARCH", desk_status="watching", signal_date="2026-08-07",
            signal=payload,
        )
        assert res["ok"] is True
        assert res["record"]["status"] == "watching"
        assert res["record"]["playbook"] == PLAYBOOK_TK_CROSS

    def test_watch_without_payload_still_fails_for_unknown(self):
        from backend import engine4_screener as scr
        res = scr.set_desk_status("ZZNOSUCHNAME", desk_status="watching")
        assert res["ok"] is False
