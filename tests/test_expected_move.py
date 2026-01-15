"""
Unit tests for the expected_move module (ATM-forward straddle algorithm).

Tests cover:
1. Core algorithm with synthetic option chain data
2. Helper functions (weighted median, linear interpolation, etc.)
3. Strike targets computation
4. Edge cases and fallbacks
"""

import datetime as dt
import pytest

from backend.expected_move import (
    StrikeQuote,
    compute_expected_move_from_chain,
    compute_strike_targets,
    _weighted_median,
    _linear_interp,
    _yearfrac,
    _discount_factor,
    _infer_forward_price,
    _interpolate_atm_straddle,
    # Earnings Hold Risk imports
    HoldRiskEvent,
    BreachRateResult,
    compute_breach_rate,
    filter_flat_open_events,
    compute_unconditional_breach_rates,
    compute_conditional_breach_rates,
    compute_drift_rates,
    compute_earnings_hold_risk,
    _compute_breach,
    _is_flat_open,
    DEFAULT_FLAT_OPEN_GATE,
    HOLD_RISK_K_VALUES,
)


class TestHelperFunctions:
    """Test helper/utility functions."""

    def test_yearfrac_basic(self):
        t0 = dt.date(2026, 1, 13)
        t_exp = dt.date(2026, 1, 17)
        result = _yearfrac(t0, t_exp)
        assert result == pytest.approx(4 / 365.0, abs=1e-9)

    def test_yearfrac_same_day(self):
        t0 = dt.date(2026, 1, 13)
        result = _yearfrac(t0, t0)
        assert result == 0.0

    def test_yearfrac_past_date(self):
        t0 = dt.date(2026, 1, 13)
        t_exp = dt.date(2026, 1, 10)  # Past
        result = _yearfrac(t0, t_exp)
        assert result == 0.0  # Clamped to 0

    def test_discount_factor(self):
        # At 5% rate for 1 year
        df = _discount_factor(0.05, 1.0)
        assert df == pytest.approx(0.951229, abs=1e-5)

    def test_discount_factor_short_term(self):
        # 4 days at 5%
        T = 4 / 365.0
        df = _discount_factor(0.05, T)
        assert df == pytest.approx(0.99945, abs=1e-4)

    def test_weighted_median_basic(self):
        values = [(10.0, 1.0), (20.0, 1.0), (30.0, 1.0)]
        result = _weighted_median(values)
        assert result == 20.0

    def test_weighted_median_weighted(self):
        # Heavy weight on 10, should pull median down
        values = [(10.0, 10.0), (20.0, 1.0), (30.0, 1.0)]
        result = _weighted_median(values)
        assert result == 10.0

    def test_weighted_median_empty(self):
        result = _weighted_median([])
        assert result is None

    def test_linear_interp_basic(self):
        # Interpolate at x=1.5 between (1, 10) and (2, 20)
        result = _linear_interp(1.5, 1.0, 2.0, 10.0, 20.0)
        assert result == pytest.approx(15.0, abs=1e-9)

    def test_linear_interp_at_endpoints(self):
        assert _linear_interp(1.0, 1.0, 2.0, 10.0, 20.0) == pytest.approx(10.0)
        assert _linear_interp(2.0, 1.0, 2.0, 10.0, 20.0) == pytest.approx(20.0)

    def test_linear_interp_same_x(self):
        # Edge case: x0 == x1, should return average
        result = _linear_interp(1.0, 1.0, 1.0, 10.0, 20.0)
        assert result == pytest.approx(15.0)


class TestStrikeQuote:
    """Test StrikeQuote dataclass."""

    def test_strike_quote_mid_computation(self):
        q = StrikeQuote(
            strike=100.0,
            call_bid=5.0,
            call_ask=5.20,
            put_bid=4.80,
            put_ask=5.00,
        )
        assert q.call_mid == pytest.approx(5.10)
        assert q.put_mid == pytest.approx(4.90)
        assert q.call_spread == pytest.approx(0.20)
        assert q.put_spread == pytest.approx(0.20)

    def test_strike_quote_usable(self):
        q = StrikeQuote(
            strike=100.0,
            call_bid=5.0,
            call_ask=5.20,
            put_bid=4.80,
            put_ask=5.00,
        )
        assert q.is_usable() is True

    def test_strike_quote_not_usable_zero_bid(self):
        q = StrikeQuote(
            strike=100.0,
            call_bid=0.0,
            call_ask=5.20,
            put_bid=4.80,
            put_ask=5.00,
        )
        assert q.is_usable() is False

    def test_strike_quote_not_usable_none(self):
        q = StrikeQuote(
            strike=100.0,
            call_bid=None,
            call_ask=None,
            put_bid=None,
            put_ask=None,
        )
        assert q.is_usable() is False


class TestForwardInference:
    """Test forward price inference via put-call parity."""

    def test_infer_forward_basic(self):
        # Create quotes near spot=100
        quotes = [
            StrikeQuote(strike=98.0, call_bid=4.0, call_ask=4.20, put_bid=2.0, put_ask=2.20),
            StrikeQuote(strike=100.0, call_bid=3.0, call_ask=3.20, put_bid=3.0, put_ask=3.20),
            StrikeQuote(strike=102.0, call_bid=2.0, call_ask=2.20, put_bid=4.0, put_ask=4.20),
        ]
        df = 0.999  # Near 1 for short-term
        forward, n_used, warnings = _infer_forward_price(quotes, df)

        assert forward is not None
        assert n_used == 3
        # Forward should be close to 100 for ATM quotes with symmetric C/P
        assert forward == pytest.approx(100.0, abs=1.0)

    def test_infer_forward_empty(self):
        forward, n_used, warnings = _infer_forward_price([], 0.999)
        assert forward is None
        assert n_used == 0
        assert len(warnings) > 0


class TestATMInterpolation:
    """Test ATM straddle interpolation."""

    def test_interpolate_basic(self):
        quotes = [
            StrikeQuote(strike=98.0, call_bid=4.0, call_ask=4.20, put_bid=2.0, put_ask=2.20),
            StrikeQuote(strike=100.0, call_bid=3.0, call_ask=3.20, put_bid=3.0, put_ask=3.20),
            StrikeQuote(strike=102.0, call_bid=2.0, call_ask=2.20, put_bid=4.0, put_ask=4.20),
        ]
        forward = 100.0
        c_f, p_f, warnings = _interpolate_atm_straddle(quotes, forward)

        # At forward=100, should match the 100 strike exactly
        assert c_f == pytest.approx(3.10, abs=0.01)  # Mid of call at 100
        assert p_f == pytest.approx(3.10, abs=0.01)  # Mid of put at 100

    def test_interpolate_between_strikes(self):
        quotes = [
            StrikeQuote(strike=98.0, call_bid=4.0, call_ask=4.20, put_bid=2.0, put_ask=2.20),
            StrikeQuote(strike=102.0, call_bid=2.0, call_ask=2.20, put_bid=4.0, put_ask=4.20),
        ]
        forward = 100.0  # Between 98 and 102
        c_f, p_f, warnings = _interpolate_atm_straddle(quotes, forward)

        # Should interpolate between the two strikes
        assert c_f is not None
        assert p_f is not None
        # Midway between call mids (4.10 and 2.10) = 3.10
        assert c_f == pytest.approx(3.10, abs=0.01)


class TestComputeExpectedMoveFromChain:
    """Test the core expected move computation."""

    def test_basic_chain(self):
        # Simulate a simple option chain
        rows = [
            {
                "strike": 95.0,
                "spotPrice": 100.0,
                "callBidPrice": 6.0,
                "callAskPrice": 6.20,
                "putBidPrice": 1.0,
                "putAskPrice": 1.20,
            },
            {
                "strike": 100.0,
                "spotPrice": 100.0,
                "callBidPrice": 3.0,
                "callAskPrice": 3.20,
                "putBidPrice": 3.0,
                "putAskPrice": 3.20,
            },
            {
                "strike": 105.0,
                "spotPrice": 100.0,
                "callBidPrice": 1.0,
                "callAskPrice": 1.20,
                "putBidPrice": 6.0,
                "putAskPrice": 6.20,
            },
        ]

        result = compute_expected_move_from_chain(
            rows,
            spot=100.0,
            expiry=dt.date(2026, 1, 17),
            as_of=dt.date(2026, 1, 13),
            risk_free_rate=0.05,
        )

        assert result["spotPrice"] == 100.0
        assert result["dte"] == 4
        assert result["forwardPrice"] is not None
        assert result["expectedMoveDollars"] is not None
        assert result["expectedMovePct"] is not None

        # Expected move should be roughly the straddle price (~6.20 for ATM)
        # EM % should be around 6.2% for a 100 spot
        assert result["expectedMovePct"] == pytest.approx(6.2, abs=1.0)

    def test_empty_chain(self):
        result = compute_expected_move_from_chain(
            [],
            spot=100.0,
            expiry=dt.date(2026, 1, 17),
            as_of=dt.date(2026, 1, 13),
        )
        assert result["expectedMoveDollars"] is None
        assert result["expectedMovePct"] is None
        assert len(result["warnings"]) > 0

    def test_expired(self):
        rows = [
            {
                "strike": 100.0,
                "spotPrice": 100.0,
                "callBidPrice": 3.0,
                "callAskPrice": 3.20,
                "putBidPrice": 3.0,
                "putAskPrice": 3.20,
            },
        ]

        # Expiry in the past
        result = compute_expected_move_from_chain(
            rows,
            spot=100.0,
            expiry=dt.date(2026, 1, 10),  # Past
            as_of=dt.date(2026, 1, 13),
        )
        assert result["expectedMoveDollars"] is None
        assert "past" in str(result["warnings"]).lower()


class TestStrikeTargets:
    """Test strike targets computation."""

    def test_basic_targets(self):
        # 2.5% expected move on a $100 stock
        result = compute_strike_targets(2.5, 100.0)

        # White = 2.5% * 100 * 2 = 5.0
        assert result["whitePts"] == pytest.approx(5.0)

        # Blue = White * 1.5 = 7.5
        assert result["bluePts"] == pytest.approx(7.5)

        # Red = White * 2 = 10.0
        assert result["redPts"] == pytest.approx(10.0)

    def test_targets_with_large_em(self):
        # 10% expected move on a $50 stock
        result = compute_strike_targets(10.0, 50.0)

        # White = 10% * 50 * 2 = 10.0
        assert result["whitePts"] == pytest.approx(10.0)

        # Blue = 15.0
        assert result["bluePts"] == pytest.approx(15.0)

        # Red = 20.0
        assert result["redPts"] == pytest.approx(20.0)

    def test_targets_metadata(self):
        result = compute_strike_targets(5.0, 200.0)
        assert result["whiteMultiple"] == 1.0
        assert result["blueMultiple"] == 1.5
        assert result["redMultiple"] == 2.0
        assert result["basedOnEmPct"] == 5.0
        assert result["basedOnSpot"] == 200.0


# =============================================================================
# EARNINGS HOLD RISK TESTS
# =============================================================================


class TestHoldRiskEvent:
    """Test HoldRiskEvent dataclass validation methods."""

    def test_valid_for_unconditional_all_fields(self):
        ev = HoldRiskEvent(
            earn_date="2026-01-13",
            timing="BMO",
            prior_close=100.0,
            earnings_day_open=102.0,
            earnings_day_close=103.0,
            next_day_close=104.0,
            expected_move_pct=5.0,
        )
        assert ev.is_valid_for_unconditional() is True
        assert ev.is_valid_for_conditional() is True
        assert ev.is_valid_for_next_day() is True
        assert ev.is_valid_for_drift() is True

    def test_invalid_missing_prior_close(self):
        ev = HoldRiskEvent(
            earn_date="2026-01-13",
            timing="BMO",
            prior_close=None,  # Missing
            earnings_day_open=102.0,
            earnings_day_close=103.0,
            next_day_close=104.0,
            expected_move_pct=5.0,
        )
        assert ev.is_valid_for_unconditional() is False

    def test_invalid_zero_em(self):
        ev = HoldRiskEvent(
            earn_date="2026-01-13",
            timing="BMO",
            prior_close=100.0,
            earnings_day_open=102.0,
            earnings_day_close=103.0,
            next_day_close=104.0,
            expected_move_pct=0.0,  # Zero EM
        )
        assert ev.is_valid_for_unconditional() is False

    def test_valid_for_conditional_requires_open(self):
        ev = HoldRiskEvent(
            earn_date="2026-01-13",
            timing="BMO",
            prior_close=100.0,
            earnings_day_open=None,  # Missing open
            earnings_day_close=103.0,
            next_day_close=104.0,
            expected_move_pct=5.0,
        )
        assert ev.is_valid_for_unconditional() is True
        assert ev.is_valid_for_conditional() is False

    def test_valid_for_next_day_requires_nc(self):
        ev = HoldRiskEvent(
            earn_date="2026-01-13",
            timing="BMO",
            prior_close=100.0,
            earnings_day_open=102.0,
            earnings_day_close=103.0,
            next_day_close=None,  # Missing NC
            expected_move_pct=5.0,
        )
        assert ev.is_valid_for_unconditional() is True
        assert ev.is_valid_for_next_day() is False


class TestBreachComputation:
    """Test the core breach computation logic."""

    def test_breach_at_threshold_exact(self):
        # EM = 5% of 100 = 5. k=1.0 threshold = 5
        # Move of exactly 5 should breach (>= not >)
        assert _compute_breach(100.0, 105.0, 5.0, 1.0) is True
        assert _compute_breach(100.0, 95.0, 5.0, 1.0) is True

    def test_no_breach_under_threshold(self):
        # Move of 4.99 should not breach 5.0 threshold
        assert _compute_breach(100.0, 104.99, 5.0, 1.0) is False
        assert _compute_breach(100.0, 95.01, 5.0, 1.0) is False

    def test_breach_with_k_multiple(self):
        # EM = 5%, k=1.5, threshold = 7.5%
        assert _compute_breach(100.0, 107.5, 5.0, 1.5) is True
        assert _compute_breach(100.0, 107.0, 5.0, 1.5) is False

    def test_breach_k2(self):
        # EM = 5%, k=2.0, threshold = 10%
        assert _compute_breach(100.0, 110.0, 5.0, 2.0) is True
        assert _compute_breach(100.0, 109.9, 5.0, 2.0) is False

    def test_invalid_baseline_zero(self):
        assert _compute_breach(0.0, 105.0, 5.0, 1.0) is False

    def test_invalid_em_zero(self):
        assert _compute_breach(100.0, 105.0, 0.0, 1.0) is False


class TestFlatOpenGating:
    """Test flat open detection logic."""

    def test_flat_open_exact_threshold(self):
        # EM = 4%, gate = 0.25, threshold = 1% of PC
        # PC = 100, open = 101 -> gap = 1% -> exactly at threshold
        assert _is_flat_open(100.0, 101.0, 4.0, 0.25) is True

    def test_flat_open_under_threshold(self):
        # PC = 100, open = 100.5 -> gap = 0.5% < 1% threshold
        assert _is_flat_open(100.0, 100.5, 4.0, 0.25) is True

    def test_not_flat_over_threshold(self):
        # PC = 100, open = 101.5 -> gap = 1.5% > 1% threshold
        assert _is_flat_open(100.0, 101.5, 4.0, 0.25) is False

    def test_flat_open_down_gap(self):
        # Negative gap should also be flat if small
        assert _is_flat_open(100.0, 99.5, 4.0, 0.25) is True
        assert _is_flat_open(100.0, 98.0, 4.0, 0.25) is False

    def test_default_gate_value(self):
        # Verify default gate is 0.25
        assert DEFAULT_FLAT_OPEN_GATE == 0.25


class TestComputeBreachRate:
    """Test breach rate computation."""

    def test_all_breach(self):
        events = [
            HoldRiskEvent("2026-01-01", "BMO", 100.0, 102.0, 110.0, 115.0, 5.0),
            HoldRiskEvent("2026-01-02", "BMO", 100.0, 102.0, 112.0, 118.0, 5.0),
        ]
        result = compute_breach_rate(events, "prior_close", "earnings_day_close", 1.0)
        assert result.rate == 1.0
        assert result.sample_size == 2
        assert result.breach_count == 2

    def test_no_breach(self):
        events = [
            HoldRiskEvent("2026-01-01", "BMO", 100.0, 102.0, 102.0, 103.0, 5.0),
            HoldRiskEvent("2026-01-02", "BMO", 100.0, 102.0, 103.0, 104.0, 5.0),
        ]
        result = compute_breach_rate(events, "prior_close", "earnings_day_close", 1.0)
        assert result.rate == 0.0
        assert result.sample_size == 2
        assert result.breach_count == 0

    def test_partial_breach(self):
        events = [
            HoldRiskEvent("2026-01-01", "BMO", 100.0, 102.0, 110.0, 115.0, 5.0),  # Breach
            HoldRiskEvent("2026-01-02", "BMO", 100.0, 102.0, 103.0, 104.0, 5.0),  # No breach
        ]
        result = compute_breach_rate(events, "prior_close", "earnings_day_close", 1.0)
        assert result.rate == 0.5
        assert result.sample_size == 2
        assert result.breach_count == 1

    def test_empty_events(self):
        result = compute_breach_rate([], "prior_close", "earnings_day_close", 1.0)
        assert result.rate is None
        assert result.sample_size == 0

    def test_filters_invalid_events(self):
        events = [
            HoldRiskEvent("2026-01-01", "BMO", 100.0, 102.0, 110.0, 115.0, 5.0),  # Valid
            HoldRiskEvent("2026-01-02", "BMO", None, 102.0, 103.0, 104.0, 5.0),  # Invalid PC
        ]
        result = compute_breach_rate(events, "prior_close", "earnings_day_close", 1.0)
        assert result.sample_size == 1


class TestFilterFlatOpenEvents:
    """Test flat open event filtering."""

    def test_filters_non_flat_opens(self):
        events = [
            HoldRiskEvent("2026-01-01", "BMO", 100.0, 100.5, 105.0, 106.0, 4.0),  # Flat (0.5% < 1%)
            HoldRiskEvent("2026-01-02", "BMO", 100.0, 105.0, 110.0, 115.0, 4.0),  # Not flat (5% > 1%)
            HoldRiskEvent("2026-01-03", "BMO", 100.0, 99.8, 104.0, 105.0, 4.0),   # Flat (-0.2% < 1%)
        ]
        flat = filter_flat_open_events(events, gate=0.25)
        assert len(flat) == 2
        assert flat[0].earn_date == "2026-01-01"
        assert flat[1].earn_date == "2026-01-03"

    def test_requires_valid_for_conditional(self):
        events = [
            HoldRiskEvent("2026-01-01", "BMO", 100.0, None, 105.0, 106.0, 4.0),  # Missing open
            HoldRiskEvent("2026-01-02", "BMO", 100.0, 100.5, 105.0, 106.0, 4.0),  # Valid
        ]
        flat = filter_flat_open_events(events, gate=0.25)
        assert len(flat) == 1


class TestUnconditionalBreachRates:
    """Test unconditional breach rate computation."""

    def test_computes_all_k_values(self):
        events = [
            HoldRiskEvent("2026-01-01", "BMO", 100.0, 102.0, 108.0, 112.0, 5.0),
            HoldRiskEvent("2026-01-02", "BMO", 100.0, 102.0, 106.0, 108.0, 5.0),
        ]
        result = compute_unconditional_breach_rates(events)
        
        assert "earnings_close" in result
        assert "next_day_close" in result
        assert "1.0" in result["earnings_close"]
        assert "1.5" in result["earnings_close"]
        assert "2.0" in result["earnings_close"]

    def test_earnings_close_vs_prior_close(self):
        # Both events: EC - PC = 8% and 6%, EM = 5%
        # k=1.0: both breach (8% >= 5%, 6% >= 5%)
        # k=1.5: one breach (8% >= 7.5%, 6% < 7.5%)
        # k=2.0: none breach (8% < 10%, 6% < 10%)
        events = [
            HoldRiskEvent("2026-01-01", "BMO", 100.0, 102.0, 108.0, 112.0, 5.0),
            HoldRiskEvent("2026-01-02", "BMO", 100.0, 102.0, 106.0, 108.0, 5.0),
        ]
        result = compute_unconditional_breach_rates(events)
        
        assert result["earnings_close"]["1.0"].rate == 1.0
        assert result["earnings_close"]["1.5"].rate == 0.5
        assert result["earnings_close"]["2.0"].rate == 0.0


class TestConditionalBreachRates:
    """Test conditional (flat open) breach rate computation."""

    def test_only_includes_flat_opens(self):
        events = [
            # Flat open (gap = 0.5%), EC - PC = 8% -> breach at k=1.0
            HoldRiskEvent("2026-01-01", "BMO", 100.0, 100.5, 108.0, 112.0, 4.0),
            # Non-flat open (gap = 5%), EC - PC = 8% -> breach but excluded
            HoldRiskEvent("2026-01-02", "BMO", 100.0, 105.0, 108.0, 112.0, 4.0),
        ]
        result = compute_conditional_breach_rates(events, gate=0.25)
        
        # Only the first event should be included
        assert result["earnings_close"]["1.0"].sample_size == 1
        assert result["earnings_close"]["1.0"].rate == 1.0


class TestDriftRates:
    """Test post-event drift rate computation."""

    def test_earnings_intraday_drift(self):
        # Drift: EC - EO, baseline = EO
        # Event 1: EO=102, EC=112 -> drift = 9.8% of 102
        # Event 2: EO=102, EC=104 -> drift = 1.96% of 102
        events = [
            HoldRiskEvent("2026-01-01", "BMO", 100.0, 102.0, 112.0, 115.0, 5.0),
            HoldRiskEvent("2026-01-02", "BMO", 100.0, 102.0, 104.0, 106.0, 5.0),
        ]
        result = compute_drift_rates(events)
        
        # k=1.0 threshold = 5% of EO = 5.1
        # Event 1: |112 - 102| = 10 >= 5.1 -> breach
        # Event 2: |104 - 102| = 2 < 5.1 -> no breach
        assert result["earnings_intraday"]["1.0"].rate == 0.5

    def test_next_day_drift(self):
        # Drift: NC - EC, baseline = EC
        events = [
            HoldRiskEvent("2026-01-01", "BMO", 100.0, 102.0, 100.0, 108.0, 5.0),
            HoldRiskEvent("2026-01-02", "BMO", 100.0, 102.0, 100.0, 102.0, 5.0),
        ]
        result = compute_drift_rates(events)
        
        # k=1.0 threshold = 5% of EC = 5.0
        # Event 1: |108 - 100| = 8 >= 5.0 -> breach
        # Event 2: |102 - 100| = 2 < 5.0 -> no breach
        assert result["next_day"]["1.0"].rate == 0.5


class TestComputeEarningsHoldRisk:
    """Test the main earnings hold risk payload computation."""

    def test_schema_structure(self):
        events = [
            HoldRiskEvent("2026-01-01", "BMO", 100.0, 100.5, 108.0, 112.0, 5.0),
            HoldRiskEvent("2026-01-02", "BMO", 100.0, 100.5, 103.0, 105.0, 5.0),
        ]
        result = compute_earnings_hold_risk(events)
        
        # Verify schema structure per master plan
        assert "em_source" in result
        assert "flat_open_gate" in result
        assert "lookback" in result
        assert "sample_size" in result
        assert "unconditional" in result
        assert "conditional_flat_open" in result
        assert "drift" in result
        
        # Verify nested structure
        assert "unconditional" in result["sample_size"]
        assert "flat_open" in result["sample_size"]
        assert "earnings_close" in result["unconditional"]
        assert "next_day_close" in result["unconditional"]

    def test_sample_sizes(self):
        events = [
            # Flat open
            HoldRiskEvent("2026-01-01", "BMO", 100.0, 100.5, 108.0, 112.0, 4.0),
            # Non-flat open
            HoldRiskEvent("2026-01-02", "BMO", 100.0, 105.0, 108.0, 112.0, 4.0),
        ]
        result = compute_earnings_hold_risk(events)
        
        assert result["sample_size"]["unconditional"] == 2
        assert result["sample_size"]["flat_open"] == 1

    def test_em_source_and_lookback(self):
        events = [
            HoldRiskEvent("2026-01-01", "BMO", 100.0, 100.5, 108.0, 112.0, 5.0),
        ]
        result = compute_earnings_hold_risk(
            events,
            em_source="CUSTOM_SOURCE",
            lookback_label="10_events",
        )
        
        assert result["em_source"] == "CUSTOM_SOURCE"
        assert result["lookback"] == "10_events"

    def test_k_values_in_output(self):
        events = [
            HoldRiskEvent("2026-01-01", "BMO", 100.0, 100.5, 108.0, 112.0, 5.0),
        ]
        result = compute_earnings_hold_risk(events)
        
        for metric in ["earnings_close", "next_day_close"]:
            for k in ["1.0", "1.5", "2.0"]:
                assert k in result["unconditional"][metric]
                assert k in result["conditional_flat_open"][metric]

    def test_empty_events(self):
        result = compute_earnings_hold_risk([])
        
        assert result["sample_size"]["unconditional"] == 0
        assert result["sample_size"]["flat_open"] == 0
        assert result["unconditional"]["earnings_close"]["1.0"] is None


class TestBreachRateResult:
    """Test BreachRateResult dataclass."""

    def test_to_dict(self):
        result = BreachRateResult(rate=0.333333, sample_size=3, breach_count=1)
        d = result.to_dict()
        
        assert d["rate"] == pytest.approx(0.333333, abs=1e-5)
        assert d["sample_size"] == 3
        assert d["breach_count"] == 1

    def test_to_dict_none_rate(self):
        result = BreachRateResult(rate=None, sample_size=0, breach_count=0)
        d = result.to_dict()
        
        assert d["rate"] is None
