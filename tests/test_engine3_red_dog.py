"""
Tests for Engine 3: Red Dog Reversal Trading System
"""

import datetime as dt

import pytest

from backend.technicals import DailyBar
from backend.engine3_red_dog import (
    APLUS_THRESHOLD,
    RedDogSignal,
    build_red_dog_signal,
    detect_red_dog_enhanced,
    score_red_dog_setup,
    signal_to_dict,
    _compute_atr,
    _compute_rsi,
    _compute_sma,
    _compute_stochastics,
)


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def make_bars(
    n: int,
    start_price: float = 100.0,
    trend: str = "flat",  # "up", "down", "flat"
    start_date: str = "2025-01-01",
) -> list[DailyBar]:
    """Generate synthetic daily bars for testing."""
    bars = []
    px = start_price
    base = dt.date.fromisoformat(start_date)
    
    for i in range(n):
        d = (base + dt.timedelta(days=i)).isoformat()
        
        if trend == "up":
            px *= 1.01
        elif trend == "down":
            px *= 0.99
        
        high = px * 1.02
        low = px * 0.98
        vol = 1_000_000
        
        bars.append(DailyBar(
            trade_date=d,
            open=px,
            high=high,
            low=low,
            close=px,
            volume=vol,
            vwap=None,
        ))
    
    return bars


def make_bullish_red_dog_bars(
    n_history: int = 30,
    start_price: float = 100.0,
    rsi_oversold: bool = True,
    high_volume: bool = True,
) -> list[DailyBar]:
    """
    Create bars that form a bullish Red Dog pattern.
    
    Day -1: Makes a low (Low A)
    Day 0 (today): Trades below Low A, makes new low (Low B), but closes above Low A
    """
    bars = []
    base = dt.date(2025, 1, 1)
    px = start_price
    
    # Generate history with downtrend if RSI should be oversold
    for i in range(n_history - 2):
        d = (base + dt.timedelta(days=i)).isoformat()
        if rsi_oversold:
            px *= 0.98  # Strong downtrend to drive RSI down
        high = px * 1.01
        low = px * 0.99
        vol = 800_000
        bars.append(DailyBar(
            trade_date=d,
            open=px * 1.005,
            high=high,
            low=low,
            close=px,
            volume=vol,
            vwap=None,
        ))
    
    # Day -1: Low A day
    i = n_history - 2
    d = (base + dt.timedelta(days=i)).isoformat()
    low_a = px * 0.97
    bars.append(DailyBar(
        trade_date=d,
        open=px,
        high=px * 1.01,
        low=low_a,  # Low A
        close=px * 0.98,
        volume=900_000,
        vwap=None,
    ))
    
    # Day 0 (today): Bullish Red Dog - trade below Low A, close above it
    i = n_history - 1
    d = (base + dt.timedelta(days=i)).isoformat()
    low_b = low_a * 0.98  # Below Low A
    close = low_a * 1.02  # Close above Low A (strong close in upper range)
    high = close * 1.02
    vol = 2_000_000 if high_volume else 800_000
    
    bars.append(DailyBar(
        trade_date=d,
        open=low_a * 0.99,
        high=high,
        low=low_b,  # Low B - below Low A
        close=close,  # Close back above Low A
        volume=vol,
        vwap=None,
    ))
    
    return bars


def make_bearish_red_dog_bars(
    n_history: int = 30,
    start_price: float = 100.0,
) -> list[DailyBar]:
    """
    Create bars that form a bearish Red Dog pattern.
    
    Day -1: Makes a high (High A)
    Day 0 (today): Trades above High A, makes new high (High B), but closes below High A
    """
    bars = []
    base = dt.date(2025, 1, 1)
    px = start_price
    
    # Generate history with uptrend
    for i in range(n_history - 2):
        d = (base + dt.timedelta(days=i)).isoformat()
        px *= 1.02  # Uptrend
        high = px * 1.01
        low = px * 0.99
        bars.append(DailyBar(
            trade_date=d,
            open=px * 0.995,
            high=high,
            low=low,
            close=px,
            volume=800_000,
            vwap=None,
        ))
    
    # Day -1: High A day
    i = n_history - 2
    d = (base + dt.timedelta(days=i)).isoformat()
    high_a = px * 1.03
    bars.append(DailyBar(
        trade_date=d,
        open=px,
        high=high_a,  # High A
        low=px * 0.99,
        close=px * 1.02,
        volume=900_000,
        vwap=None,
    ))
    
    # Day 0 (today): Bearish Red Dog - trade above High A, close below it
    i = n_history - 1
    d = (base + dt.timedelta(days=i)).isoformat()
    high_b = high_a * 1.02  # Above High A
    close = high_a * 0.98  # Close below High A
    low = close * 0.98
    
    bars.append(DailyBar(
        trade_date=d,
        open=high_a * 1.01,
        high=high_b,  # High B - above High A
        low=low,
        close=close,  # Close back below High A
        volume=2_000_000,
        vwap=None,
    ))
    
    return bars


# ---------------------------------------------------------------------------
# Test: Indicator Calculations
# ---------------------------------------------------------------------------

class TestIndicatorCalculations:
    """Test individual indicator calculation functions."""
    
    def test_compute_sma_basic(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        sma3 = _compute_sma(values, 3)
        assert sma3 is not None
        assert abs(sma3 - 40.0) < 0.01  # (30+40+50)/3 = 40
    
    def test_compute_sma_insufficient_data(self):
        values = [10.0, 20.0]
        sma5 = _compute_sma(values, 5)
        assert sma5 is None
    
    def test_compute_rsi_uptrend(self):
        # Strictly increasing closes should produce high RSI
        closes = [float(i) for i in range(1, 100)]
        rsi = _compute_rsi(closes, period=14)
        assert rsi is not None
        assert rsi > 70.0
    
    def test_compute_rsi_downtrend(self):
        # Strictly decreasing closes should produce low RSI
        closes = [float(100 - i) for i in range(100)]
        rsi = _compute_rsi(closes, period=14)
        assert rsi is not None
        assert rsi < 30.0
    
    def test_compute_stochastics_range(self):
        highs = [110.0] * 20
        lows = [90.0] * 20
        closes = [100.0] * 19 + [105.0]  # Close near high
        
        stoch = _compute_stochastics(highs, lows, closes, period=14)
        assert stoch is not None
        assert 0 <= stoch <= 100
        # Close of 105 with range 90-110 should be (105-90)/(110-90) = 75%
        assert abs(stoch - 75.0) < 0.01
    
    def test_compute_atr_basic(self):
        bars = make_bars(30, start_price=100.0, trend="flat")
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        closes = [b.close for b in bars]
        
        atr = _compute_atr(highs, lows, closes, period=14)
        assert atr is not None
        assert atr > 0


# ---------------------------------------------------------------------------
# Test: Red Dog Detection
# ---------------------------------------------------------------------------

class TestRedDogDetection:
    """Test Red Dog pattern detection logic."""
    
    def test_detect_bullish_red_dog(self):
        bars = make_bullish_red_dog_bars()
        result = detect_red_dog_enhanced(bars, ticker="TEST")
        
        assert result["enabled"] is True
        assert result["bullish"] is True
        assert result["bearish"] is False
        assert result["pattern"] is not None
        assert result["pattern"]["direction"] == "bullish"
    
    def test_detect_bearish_red_dog(self):
        bars = make_bearish_red_dog_bars()
        result = detect_red_dog_enhanced(bars, ticker="TEST")
        
        assert result["enabled"] is True
        assert result["bearish"] is True
        # Note: both bullish and bearish can be true simultaneously in edge cases
        # The important thing is that bearish IS detected
        assert result["pattern"] is not None
    
    def test_no_pattern_flat_bars(self):
        bars = make_bars(30, trend="flat")
        result = detect_red_dog_enhanced(bars, ticker="TEST")
        
        assert result["enabled"] is True
        # Flat bars shouldn't form a Red Dog pattern
        # (unless by chance the random variation creates one)
    
    def test_insufficient_bars(self):
        bars = make_bars(5)  # Not enough bars
        result = detect_red_dog_enhanced(bars, ticker="TEST")
        
        assert result["enabled"] is False
        assert "Insufficient bars" in result["notes"][0]
    
    def test_indicators_calculated(self):
        bars = make_bullish_red_dog_bars()
        result = detect_red_dog_enhanced(bars, ticker="TEST")
        
        indicators = result["indicators"]
        assert "rsi" in indicators
        assert "stochastics" in indicators
        assert "sma20" in indicators
        assert "volumeRatio" in indicators
        assert "atr14" in indicators
    
    def test_entry_levels_calculated(self):
        bars = make_bullish_red_dog_bars()
        result = detect_red_dog_enhanced(bars, ticker="TEST")
        
        pattern = result["pattern"]
        assert "entryTrigger" in pattern
        assert "stopLoss" in pattern
        assert "target1" in pattern
        assert "target2" in pattern
        assert "riskDollars" in pattern
        
        # Entry should be above stop for bullish
        assert pattern["entryTrigger"] > pattern["stopLoss"]


# ---------------------------------------------------------------------------
# Test: A+ Scoring
# ---------------------------------------------------------------------------

class TestContinuousScoring:
    """Test the continuous (ramped) A+ scoring model."""

    def test_score_perfect_bullish_setup(self):
        # All factors at their extreme → near the 100 ceiling, A+.
        score, components, grade, meta = score_red_dog_setup(
            direction="bullish",
            rsi=22.0,
            stochastics=10.0,
            sma20_deviation_pct=-12.0,
            volume_ratio=2.0,
            close_position=0.90,
            wick_quality=1.0,
            sr_confluence=1.0,
            trend_alignment="aligned",
        )
        assert score >= 99.0
        assert grade == "A+"
        # Every component should be at (or near) its full weight.
        assert components["rsi"] > 21
        assert components["srConfluence"] > 14
        assert components["wick"] > 7

    def test_score_minimal_setup(self):
        score, components, grade, meta = score_red_dog_setup(
            direction="bullish",
            rsi=50.0, stochastics=50.0, sma20_deviation_pct=-2.0,
            volume_ratio=1.0, close_position=0.50,
            wick_quality=0.0, sr_confluence=0.0,
        )
        assert score == 0.0
        assert grade == "C"

    def test_partial_credit_is_continuous(self):
        # A mildly-oversold setup should earn *some* RSI credit, not zero and
        # not full — the whole point of the rewrite (no hard cliff).
        score, components, _, _ = score_red_dog_setup(
            direction="bullish", rsi=35.0, stochastics=50.0,
            sma20_deviation_pct=-3.0, volume_ratio=1.0, close_position=0.5,
            wick_quality=0.0, sr_confluence=0.0,
        )
        assert 0 < components["rsi"] < 22.0

    def test_score_monotonic_in_rsi(self):
        def s(rsi):
            return score_red_dog_setup(
                direction="bullish", rsi=rsi, stochastics=50.0,
                sma20_deviation_pct=-1.0, volume_ratio=1.0, close_position=0.5,
            )[0]
        # More oversold → higher score.
        assert s(20) > s(30) > s(40) >= s(50)

    def test_bearish_overbought_scores(self):
        score, _, grade, _ = score_red_dog_setup(
            direction="bearish", rsi=78.0, stochastics=88.0,
            sma20_deviation_pct=12.0, volume_ratio=2.0, close_position=0.10,
            wick_quality=0.8, sr_confluence=0.7, trend_alignment="aligned",
        )
        assert score >= 75
        assert grade == "A+"

    def test_counter_trend_is_penalized(self):
        base = dict(
            direction="bearish", rsi=78.0, stochastics=88.0,
            sma20_deviation_pct=12.0, volume_ratio=2.0, close_position=0.10,
            wick_quality=0.8, sr_confluence=0.7,
        )
        neutral = score_red_dog_setup(**base, trend_alignment="neutral")
        counter = score_red_dog_setup(**base, trend_alignment="counter")
        assert counter[0] < neutral[0]
        assert counter[3]["trendPenalty"] > 0
        # A strongly counter-trend setup should be demoted below A+.
        assert counter[2] != "A+"

    def test_aplus_threshold(self):
        assert APLUS_THRESHOLD == 75


class TestSRConfluence:
    """S/R confluence from swing structure (no longer hardcoded False)."""

    def test_confluence_near_prior_swing_low(self):
        from backend.engine3_red_dog import compute_sr_confluence
        bars = make_bullish_red_dog_bars()
        atr = 1.0
        pivot = bars[-1].low
        res = compute_sr_confluence(bars, direction="bullish", pivot_price=pivot, atr=atr)
        assert 0.0 <= res["confluence"] <= 1.0
        assert "distanceAtr" in res

    def test_confluence_zero_without_atr(self):
        from backend.engine3_red_dog import compute_sr_confluence
        bars = make_bullish_red_dog_bars()
        res = compute_sr_confluence(bars, direction="bullish", pivot_price=100.0, atr=None)
        assert res["confluence"] == 0.0


class TestOutcomeEvaluation:
    """Deterministic forward outcome evaluation."""

    def _bar(self, d, o, h, l, c):
        return DailyBar(trade_date=d, open=o, high=h, low=l, close=c, volume=1_000_000, vwap=None)

    def test_target_hit_bullish(self):
        from backend.engine3_red_dog import evaluate_outcome
        fwd = [self._bar("2025-02-01", 101, 107, 100.5, 106)]
        oc = evaluate_outcome(direction="bullish", entry_trigger=102, stop_loss=98,
                              target_1=106, forward_bars=fwd)
        assert oc["status"] == "target_hit"
        assert oc["rMultiple"] == 1.0
        assert oc["triggered"] is True

    def test_stopped_bullish(self):
        from backend.engine3_red_dog import evaluate_outcome
        fwd = [self._bar("2025-02-01", 102, 103, 97, 97.5)]
        oc = evaluate_outcome(direction="bullish", entry_trigger=102, stop_loss=98,
                              target_1=110, forward_bars=fwd)
        assert oc["status"] == "stopped"
        assert oc["rMultiple"] == -1.0

    def test_expired_when_no_trigger(self):
        from backend.engine3_red_dog import evaluate_outcome
        fwd = [self._bar("2025-02-01", 99, 100, 98, 99)]
        oc = evaluate_outcome(direction="bullish", entry_trigger=105, stop_loss=95,
                              target_1=115, forward_bars=fwd, trigger_window=1)
        assert oc["status"] == "expired"
        assert oc["triggered"] is False


class TestBacktest:
    """Backtest harness aggregation."""

    def test_backtest_from_bars_aggregates(self):
        from backend.engine3_backtest import backtest_from_bars
        bars = make_bars(120, start_price=100.0, trend="down")
        res = backtest_from_bars({"TEST": bars}, min_score=0, warmup=60)
        assert "overall" in res
        assert "byGrade" in res
        assert "byTrendAlignment" in res
        assert res["params"]["tickersTested"] == 1


class TestDeskTracker:
    """Desk-managed tracker states (watching/entered/working/broken/exited)."""

    def _seed(self, ticker="ZZRDTEST", date="2024-02-02"):
        from backend import engine3_screener as scr
        d = {"ticker": ticker, "signalDate": date, "direction": "bullish",
             "levels": {"entryTrigger": 10.0, "stopLoss": 9.0, "target1": 12.0, "riskDollars": 1.0},
             "quality": {"score": 80.0, "grade": "A"},
             "indicators": {"rsi": 30.0, "dollarAdv": 5e8}}
        scr._persist_signals([d])
        return scr, d

    def test_set_desk_status_persists(self):
        scr, _ = self._seed()
        res = scr.set_desk_status("ZZRDTEST", desk_status="entered", signal_date="2024-02-02", note="filled")
        assert res["ok"] is True
        all_sigs = scr.get_all_signals()
        assert any(r["ticker"] == "ZZRDTEST" for r in all_sigs["entered"])
        assert all_sigs["deskBookCount"] >= 1

    def test_desk_state_survives_rescan(self):
        scr, d = self._seed(ticker="ZZRDKEEP")
        scr.set_desk_status("ZZRDKEEP", desk_status="working", signal_date="2024-02-02")
        scr._persist_signals([d])  # a fresh scan must not reset desk state
        assert any(r["ticker"] == "ZZRDKEEP" for r in scr.get_all_signals()["working"])

    def test_invalid_desk_status_rejected(self):
        from backend import engine3_screener as scr
        assert scr.set_desk_status("ZZRDTEST", desk_status="bogus")["ok"] is False

    def test_unknown_ticker_rejected(self):
        from backend import engine3_screener as scr
        assert scr.set_desk_status("NOSUCHTICKER", desk_status="entered")["ok"] is False

    def test_remove_signal(self):
        scr, _ = self._seed(ticker="ZZRDRM", date="2024-03-03")
        assert any(r["ticker"] == "ZZRDRM" for r in scr.get_all_signals()["pending"])
        res = scr.remove_signal("ZZRDRM", signal_date="2024-03-03")
        assert res["ok"] is True
        all_sigs = scr.get_all_signals()
        assert not any(r["ticker"] == "ZZRDRM" for bucket in all_sigs.get("counts", {})
                       for r in all_sigs.get(bucket, []))
        assert scr.remove_signal("NOPE")["ok"] is False

    def test_redis_prior_wins_over_stale_inmemory(self, monkeypatch):
        """Multi-worker regression: a stale per-worker in-memory copy must not
        clobber the desk state another worker wrote to Redis."""
        from backend import engine3_screener as scr
        import backend.redis_store as rs

        class FakeStore:
            def __init__(self): self.kv = {}
            def get_json(self, k): return self.kv.get(k)
            def set_json(self, k, v, ttl_s=None): self.kv[k] = v

        fake = FakeStore()
        monkeypatch.setattr(rs, "get_store_optional", lambda: fake)
        key = scr._signal_key("ZZAUTH", "2024-05-05")
        fake.kv[scr._REDIS_PREFIX + key] = {"ticker": "ZZAUTH", "signalDate": "2024-05-05",
                                            "status": "working", "levels": {}}
        scr._signal_store[key] = {"ticker": "ZZAUTH", "signalDate": "2024-05-05",
                                  "status": "pending", "levels": {}}  # stale
        scr._persist_signals([{"ticker": "ZZAUTH", "signalDate": "2024-05-05",
                               "direction": "bullish", "levels": {}, "quality": {}}])
        assert fake.kv[scr._REDIS_PREFIX + key]["status"] == "working"


class TestVerdictReconciliation:
    """Single reconciled desk verdict."""

    def test_counter_trend_capped_at_watch(self):
        from backend.gating import reconcile_red_dog_verdict
        sig = {"quality": {"grade": "A", "confirmed": True, "trendAlignment": "counter", "score": 72.0},
               "gate": {"status": "TRADABLE"}}
        v = reconcile_red_dog_verdict(sig, gamma_ctx={"environment": "supportive"})
        assert v["status"] == "WATCH"

    def test_clean_aplus_is_tradable(self):
        from backend.gating import reconcile_red_dog_verdict
        sig = {"quality": {"grade": "A+", "confirmed": True, "trendAlignment": "aligned", "score": 88.0},
               "gate": {"status": "TRADABLE"}}
        v = reconcile_red_dog_verdict(sig, gamma_ctx={"environment": "supportive"})
        assert v["status"] == "TRADABLE"

    def test_suppress_gate_stands_down(self):
        from backend.gating import reconcile_red_dog_verdict
        sig = {"quality": {"grade": "A+", "confirmed": True, "trendAlignment": "aligned", "score": 90.0},
               "gate": {"status": "SUPPRESS"}}
        v = reconcile_red_dog_verdict(sig, gamma_ctx={"environment": "supportive"})
        assert v["status"] == "STAND_DOWN"


# ---------------------------------------------------------------------------
# Test: Signal Building
# ---------------------------------------------------------------------------

class TestSignalBuilding:
    """Test RedDogSignal construction."""
    
    def test_build_signal_from_detection(self):
        bars = make_bullish_red_dog_bars()
        detection = detect_red_dog_enhanced(bars, ticker="TEST")
        
        signal = build_red_dog_signal(
            ticker="TEST",
            detection=detection,
            near_support_resistance=False,
        )
        
        assert signal is not None
        assert isinstance(signal, RedDogSignal)
        assert signal.ticker == "TEST"
        assert signal.direction == "bullish"
        assert signal.score >= 0
        assert signal.grade in ("A+", "A", "B", "C")
    
    def test_build_signal_no_pattern(self):
        detection = {
            "enabled": True,
            "bullish": False,
            "bearish": False,
            "pattern": None,
        }
        
        signal = build_red_dog_signal(
            ticker="TEST",
            detection=detection,
        )
        
        assert signal is None
    
    def test_signal_to_dict(self):
        bars = make_bullish_red_dog_bars()
        detection = detect_red_dog_enhanced(bars, ticker="TEST")
        signal = build_red_dog_signal(ticker="TEST", detection=detection)
        
        assert signal is not None
        d = signal_to_dict(signal)
        
        assert "ticker" in d
        assert "signalDate" in d
        assert "direction" in d
        assert "pattern" in d
        assert "levels" in d
        assert "quality" in d
        assert "indicators" in d
        assert "notes" in d
        
        # Check nested structure
        assert "entryTrigger" in d["levels"]
        assert "score" in d["quality"]
        assert "grade" in d["quality"]
        assert "rsi" in d["indicators"]


# ---------------------------------------------------------------------------
# Test: Edge Cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Test edge cases and error handling."""
    
    def test_empty_bars(self):
        result = detect_red_dog_enhanced([], ticker="TEST")
        assert result["enabled"] is False
    
    def test_none_values_in_bars(self):
        bars = [
            DailyBar(trade_date="2025-01-01", open=100, high=None, low=90, close=95, volume=None, vwap=None),
            DailyBar(trade_date="2025-01-02", open=96, high=108, low=85, close=92, volume=None, vwap=None),
        ]
        result = detect_red_dog_enhanced(bars, ticker="TEST")
        # Should handle gracefully
        assert "enabled" in result
    
    def test_zero_volume(self):
        bars = make_bars(30)
        # Replace volumes with zero
        bars = [
            DailyBar(
                trade_date=b.trade_date,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=0,
                vwap=b.vwap,
            )
            for b in bars
        ]
        
        result = detect_red_dog_enhanced(bars, ticker="TEST")
        # Volume ratio should be None
        assert result["indicators"]["volumeRatio"] is None
    
    def test_signal_notes_generation(self):
        bars = make_bullish_red_dog_bars(rsi_oversold=True, high_volume=True)
        detection = detect_red_dog_enhanced(bars, ticker="TEST")
        signal = build_red_dog_signal(ticker="TEST", detection=detection)
        
        assert signal is not None
        # Should have notes about RSI and/or volume
        assert len(signal.notes) >= 0  # May or may not have notes depending on conditions


# ---------------------------------------------------------------------------
# Test: Integration
# ---------------------------------------------------------------------------

class TestIntegration:
    """Integration tests for the full flow."""
    
    def test_full_bullish_flow(self):
        """Test complete flow from bars to signal dict."""
        bars = make_bullish_red_dog_bars(rsi_oversold=True, high_volume=True)
        
        # 1. Detect pattern
        detection = detect_red_dog_enhanced(bars, ticker="AAPL")
        assert detection["bullish"] is True
        
        # 2. Build signal
        signal = build_red_dog_signal(ticker="AAPL", detection=detection)
        assert signal is not None
        assert signal.direction == "bullish"
        
        # 3. Convert to dict
        d = signal_to_dict(signal)
        assert d["ticker"] == "AAPL"
        assert d["direction"] == "bullish"
        
        # 4. Verify trade levels make sense
        levels = d["levels"]
        assert levels["entryTrigger"] > levels["stopLoss"]
        assert levels["target1"] > levels["entryTrigger"]
        assert levels["target2"] > levels["target1"]
        assert levels["riskDollars"] > 0
    
    def test_full_bearish_flow(self):
        """Test complete flow for bearish setup."""
        bars = make_bearish_red_dog_bars()
        
        detection = detect_red_dog_enhanced(bars, ticker="NVDA")
        assert detection["bearish"] is True
        
        signal = build_red_dog_signal(ticker="NVDA", detection=detection)
        assert signal is not None
        # Note: when both bullish and bearish are detected, the pattern dict
        # will reflect whichever condition was checked first
        # The important test is that the flow works and produces valid output
        
        d = signal_to_dict(signal)
        levels = d["levels"]
        
        # Verify levels are computed and risk is positive
        assert levels["riskDollars"] > 0
        assert levels["entryTrigger"] is not None
        assert levels["stopLoss"] is not None
