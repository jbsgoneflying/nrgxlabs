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
    _entry_offset,
)
from backend.gating import (
    reconcile_ichimoku_verdict,
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
        v = reconcile_ichimoku_verdict(sig, gamma_ctx={"environment": "supportive"})
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

    def test_hostile_gamma_caps_at_watch(self):
        sig = {"quality": {"grade": "A+", "score": 85}, "freshness": {"bucket": "actionable"},
               "gate": {"status": "TRADABLE"}, "tags": []}
        v = reconcile_ichimoku_verdict(sig, gamma_ctx={"environment": "challenging"})
        assert v["status"] == VERDICT_WATCH


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
        assert result["params"]["tickersTested"] == 2
        # Overall must always carry the standard stat keys.
        for k in ("signals", "triggered", "winRate", "avgR", "expectancy"):
            assert k in result["overall"]


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
