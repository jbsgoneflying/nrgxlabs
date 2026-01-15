"""
End-to-end mock test for Earnings Hold Risk Extension.

This test validates:
1. Sample size integrity for unconditional and conditional metrics
2. Breach rate computations at all k-levels
3. Flat open gating behavior
4. Drift metrics computation
5. Schema integrity per master plan

Reference: engine_1_earnings_hold_risk_master_plan.md
"""

import datetime as dt
import pytest

from backend.earnings_logic import compute_breach_stats


class FakeResp:
    """Mock ORATS response container."""
    def __init__(self, rows):
        self.rows = rows
        self.raw = rows


class HoldRiskMockOratsClient:
    """
    Mock ORATS client with complete price anchor data for hold risk testing.
    
    Test scenarios:
    1. BMO event with flat open -> should be included in conditional metrics
    2. AMC event with flat open -> should be included in conditional metrics
    3. BMO event with non-flat open -> excluded from conditional metrics
    4. AMC event with breach -> validates breach computation
    
    Price anchors per master plan:
    - PC = Prior Close
    - EO = Earnings Day Open
    - EC = Earnings Day Close
    - NC = Next Day Close
    """
    
    def __init__(self):
        # 6 earnings events for robust sample testing
        self._earnings = [
            # BMO events
            {"earnDate": "2025-03-15", "anncTod": "0830"},  # BMO flat open, no breach
            {"earnDate": "2025-02-15", "anncTod": "0830"},  # BMO flat open, breach at k=1.0
            {"earnDate": "2025-01-15", "anncTod": "0830"},  # BMO non-flat open, breach
            # AMC events
            {"earnDate": "2024-12-15", "anncTod": "1630"},  # AMC flat open, no breach
            {"earnDate": "2024-11-15", "anncTod": "1630"},  # AMC flat open, breach at k=1.0
            {"earnDate": "2024-10-15", "anncTod": "1630"},  # AMC non-flat open, breach
        ]
        
        # Daily bars with all required price anchors
        # Format: (ticker, date) -> {tradeDate, open, clsPx}
        self._dailies = {
            # === BMO 2025-03-15 ===
            # PC = 2025-03-14 close = 100
            # EO = 2025-03-15 open = 100.5 (flat, gap = 0.5%)
            # EC = 2025-03-15 close = 103 (move = 3% < 5% EM, no breach)
            # NC = 2025-03-16 close = 104
            ("TST", "2025-03-14"): {"tradeDate": "2025-03-14", "open": 100.0, "clsPx": 100.0},
            ("TST", "2025-03-15"): {"tradeDate": "2025-03-15", "open": 100.5, "clsPx": 103.0},
            ("TST", "2025-03-16"): {"tradeDate": "2025-03-16", "open": 103.0, "clsPx": 104.0},
            
            # === BMO 2025-02-15 ===
            # PC = 2025-02-14 close = 100
            # EO = 2025-02-15 open = 100.8 (flat, gap = 0.8%)
            # EC = 2025-02-15 close = 108 (move = 8% > 5% EM, BREACH)
            # NC = 2025-02-16 close = 112 (move = 12% > 5% EM, BREACH)
            ("TST", "2025-02-14"): {"tradeDate": "2025-02-14", "open": 100.0, "clsPx": 100.0},
            ("TST", "2025-02-15"): {"tradeDate": "2025-02-15", "open": 100.8, "clsPx": 108.0},
            ("TST", "2025-02-16"): {"tradeDate": "2025-02-16", "open": 108.0, "clsPx": 112.0},
            
            # === BMO 2025-01-15 ===
            # PC = 2025-01-14 close = 100
            # EO = 2025-01-15 open = 106 (NOT flat, gap = 6%)
            # EC = 2025-01-15 close = 112 (move = 12% > 5% EM, BREACH)
            # NC = 2025-01-16 close = 115
            ("TST", "2025-01-14"): {"tradeDate": "2025-01-14", "open": 100.0, "clsPx": 100.0},
            ("TST", "2025-01-15"): {"tradeDate": "2025-01-15", "open": 106.0, "clsPx": 112.0},
            ("TST", "2025-01-16"): {"tradeDate": "2025-01-16", "open": 112.0, "clsPx": 115.0},
            
            # === AMC 2024-12-15 ===
            # PC = 2024-12-15 close = 100 (before earnings)
            # EO = 2024-12-16 open = 100.3 (flat, gap = 0.3%)
            # EC = 2024-12-16 close = 102 (move = 2% < 5% EM, no breach)
            # NC = 2024-12-17 close = 103
            ("TST", "2024-12-15"): {"tradeDate": "2024-12-15", "open": 99.0, "clsPx": 100.0},
            ("TST", "2024-12-16"): {"tradeDate": "2024-12-16", "open": 100.3, "clsPx": 102.0},
            ("TST", "2024-12-17"): {"tradeDate": "2024-12-17", "open": 102.0, "clsPx": 103.0},
            
            # === AMC 2024-11-15 ===
            # PC = 2024-11-15 close = 100 (before earnings)
            # EO = 2024-11-16 open = 99.5 (flat, gap = -0.5%)
            # EC = 2024-11-16 close = 92 (move = -8% > 5% EM, BREACH)
            # NC = 2024-11-17 close = 88 (move = -12% > 5% EM, BREACH)
            ("TST", "2024-11-15"): {"tradeDate": "2024-11-15", "open": 99.0, "clsPx": 100.0},
            ("TST", "2024-11-16"): {"tradeDate": "2024-11-16", "open": 99.5, "clsPx": 92.0},
            ("TST", "2024-11-17"): {"tradeDate": "2024-11-17", "open": 92.0, "clsPx": 88.0},
            
            # === AMC 2024-10-15 ===
            # PC = 2024-10-15 close = 100 (before earnings)
            # EO = 2024-10-16 open = 108 (NOT flat, gap = 8%)
            # EC = 2024-10-16 close = 115 (move = 15% > 5% EM, BREACH)
            # NC = 2024-10-17 close = 118
            ("TST", "2024-10-15"): {"tradeDate": "2024-10-15", "open": 99.0, "clsPx": 100.0},
            ("TST", "2024-10-16"): {"tradeDate": "2024-10-16", "open": 108.0, "clsPx": 115.0},
            ("TST", "2024-10-17"): {"tradeDate": "2024-10-17", "open": 115.0, "clsPx": 118.0},
        }
        
        # Cores data with impErnMv (5% for all events)
        self._cores = {
            # BMO pricing dates (prior day)
            ("TST", "2025-03-14"): {"tradeDate": "2025-03-14", "impErnMv": 5.0},
            ("TST", "2025-02-14"): {"tradeDate": "2025-02-14", "impErnMv": 5.0},
            ("TST", "2025-01-14"): {"tradeDate": "2025-01-14", "impErnMv": 5.0},
            # AMC pricing dates (earnDate)
            ("TST", "2024-12-15"): {"tradeDate": "2024-12-15", "impErnMv": 5.0},
            ("TST", "2024-11-15"): {"tradeDate": "2024-11-15", "impErnMv": 5.0},
            ("TST", "2024-10-15"): {"tradeDate": "2024-10-15", "impErnMv": 5.0},
        }
    
    def hist_earnings(self, ticker: str):
        return FakeResp(self._earnings if ticker == "TST" else [])
    
    def hist_dailies(self, ticker: str, trade_date: str, fields: str):
        # Handle range queries (e.g., "2024-10-01,2025-03-31")
        if "," in trade_date:
            start, end = trade_date.split(",")
            rows = []
            for (t, d), row in self._dailies.items():
                if t == ticker and start <= d <= end:
                    rows.append(row)
            return FakeResp(rows)
        
        row = self._dailies.get((ticker, trade_date))
        return FakeResp([row] if row else [])
    
    def hist_cores(self, ticker: str, trade_date: str, fields: str):
        row = self._cores.get((ticker, trade_date))
        return FakeResp([row] if row else [])
    
    def get(self, path: str, params: dict):
        """Support for range queries in bulk fetch."""
        if path == "/hist/dailies":
            ticker = params.get("ticker", "")
            from_date = params.get("fromDate", "")
            to_date = params.get("toDate", "")
            if from_date and to_date:
                return self.hist_dailies(ticker, f"{from_date},{to_date}", "")
        elif path == "/hist/cores":
            ticker = params.get("ticker", "")
            from_date = params.get("fromDate", "")
            to_date = params.get("toDate", "")
            if from_date and to_date:
                rows = []
                for (t, d), row in self._cores.items():
                    if t == ticker and from_date <= d <= to_date:
                        rows.append(row)
                return FakeResp(rows)
        return FakeResp([])


class TestEarningsHoldRiskE2E:
    """End-to-end tests for earnings hold risk payload."""
    
    def test_hold_risk_payload_exists(self):
        """Verify earningsHoldRisk key is present in output."""
        client = HoldRiskMockOratsClient()
        out = compute_breach_stats(
            client=client,
            ticker="TST",
            n=20,
            years=5,
            k=1.0,
            today=dt.date(2025, 3, 20),
        )
        
        assert "earningsHoldRisk" in out
        assert out["earningsHoldRisk"] is not None
    
    def test_schema_integrity(self):
        """Verify schema matches master plan specification."""
        client = HoldRiskMockOratsClient()
        out = compute_breach_stats(
            client=client,
            ticker="TST",
            n=20,
            years=5,
            k=1.0,
            today=dt.date(2025, 3, 20),
        )
        
        hr = out["earningsHoldRisk"]
        
        # Top-level keys per master plan
        assert "em_source" in hr
        assert "flat_open_gate" in hr
        assert "lookback" in hr
        assert "sample_size" in hr
        assert "unconditional" in hr
        assert "conditional_flat_open" in hr
        assert "drift" in hr
        
        # Sample size structure
        assert "unconditional" in hr["sample_size"]
        assert "flat_open" in hr["sample_size"]
        
        # Unconditional structure
        assert "earnings_close" in hr["unconditional"]
        assert "next_day_close" in hr["unconditional"]
        
        # K-values in each metric group
        for k_val in ["1.0", "1.5", "2.0"]:
            assert k_val in hr["unconditional"]["earnings_close"]
            assert k_val in hr["unconditional"]["next_day_close"]
            assert k_val in hr["conditional_flat_open"]["earnings_close"]
            assert k_val in hr["conditional_flat_open"]["next_day_close"]
            assert k_val in hr["drift"]["earnings_intraday"]
            assert k_val in hr["drift"]["next_day"]
    
    def test_sample_size_unconditional(self):
        """Verify unconditional sample size counts valid events."""
        client = HoldRiskMockOratsClient()
        out = compute_breach_stats(
            client=client,
            ticker="TST",
            n=20,
            years=5,
            k=1.0,
            today=dt.date(2025, 3, 20),
        )
        
        hr = out["earningsHoldRisk"]
        
        # All 6 events have valid timing (BMO/AMC), PC, EC, and EM
        # Unconditional sample should include all of them
        assert hr["sample_size"]["unconditional"] == 6
    
    def test_sample_size_flat_open(self):
        """Verify flat open sample size only counts flat-gated events."""
        client = HoldRiskMockOratsClient()
        out = compute_breach_stats(
            client=client,
            ticker="TST",
            n=20,
            years=5,
            k=1.0,
            today=dt.date(2025, 3, 20),
        )
        
        hr = out["earningsHoldRisk"]
        
        # Flat open gate = 0.25 * EM = 1.25%
        # Flat events (gap <= 1.25%):
        # - 2025-03-15 BMO: gap = 0.5% ✓
        # - 2025-02-15 BMO: gap = 0.8% ✓
        # - 2025-01-15 BMO: gap = 6% ✗
        # - 2024-12-15 AMC: gap = 0.3% ✓
        # - 2024-11-15 AMC: gap = 0.5% ✓
        # - 2024-10-15 AMC: gap = 8% ✗
        assert hr["sample_size"]["flat_open"] == 4
    
    def test_unconditional_breach_rates(self):
        """Verify unconditional breach rate computation."""
        client = HoldRiskMockOratsClient()
        out = compute_breach_stats(
            client=client,
            ticker="TST",
            n=20,
            years=5,
            k=1.0,
            today=dt.date(2025, 3, 20),
        )
        
        hr = out["earningsHoldRisk"]
        
        # Unconditional EC breach at k=1.0 (threshold = 5%):
        # - 2025-03-15: |103 - 100| / 100 = 3% < 5% -> NO
        # - 2025-02-15: |108 - 100| / 100 = 8% >= 5% -> YES
        # - 2025-01-15: |112 - 100| / 100 = 12% >= 5% -> YES
        # - 2024-12-15: |102 - 100| / 100 = 2% < 5% -> NO
        # - 2024-11-15: |92 - 100| / 100 = 8% >= 5% -> YES
        # - 2024-10-15: |115 - 100| / 100 = 15% >= 5% -> YES
        # Breach rate = 4/6 = 0.666...
        assert hr["unconditional"]["earnings_close"]["1.0"] == pytest.approx(4/6, abs=0.01)
        
        # At k=1.5 (threshold = 7.5%):
        # Breaches: 8%, 12%, 8%, 15% >= 7.5% -> 4/6
        assert hr["unconditional"]["earnings_close"]["1.5"] == pytest.approx(4/6, abs=0.01)
        
        # At k=2.0 (threshold = 10%):
        # Breaches: 12%, 15% >= 10% -> 2/6
        assert hr["unconditional"]["earnings_close"]["2.0"] == pytest.approx(2/6, abs=0.01)
    
    def test_conditional_breach_rates(self):
        """Verify conditional (flat open) breach rate computation."""
        client = HoldRiskMockOratsClient()
        out = compute_breach_stats(
            client=client,
            ticker="TST",
            n=20,
            years=5,
            k=1.0,
            today=dt.date(2025, 3, 20),
        )
        
        hr = out["earningsHoldRisk"]
        
        # Flat open events only (4 events):
        # - 2025-03-15: EC breach = 3% < 5% -> NO
        # - 2025-02-15: EC breach = 8% >= 5% -> YES
        # - 2024-12-15: EC breach = 2% < 5% -> NO
        # - 2024-11-15: EC breach = 8% >= 5% -> YES
        # Conditional breach rate = 2/4 = 0.5
        assert hr["conditional_flat_open"]["earnings_close"]["1.0"] == pytest.approx(0.5, abs=0.01)
    
    def test_drift_rates(self):
        """Verify post-event drift rate computation."""
        client = HoldRiskMockOratsClient()
        out = compute_breach_stats(
            client=client,
            ticker="TST",
            n=20,
            years=5,
            k=1.0,
            today=dt.date(2025, 3, 20),
        )
        
        hr = out["earningsHoldRisk"]
        
        # Earnings intraday drift: |EC - EO| >= k * EM (baseline = EO)
        # - 2025-03-15: |103 - 100.5| = 2.5, threshold = 5% of 100.5 = 5.025 -> NO
        # - 2025-02-15: |108 - 100.8| = 7.2, threshold = 5% of 100.8 = 5.04 -> YES
        # - 2025-01-15: |112 - 106| = 6, threshold = 5% of 106 = 5.3 -> YES
        # - 2024-12-15: |102 - 100.3| = 1.7, threshold = 5% of 100.3 = 5.015 -> NO
        # - 2024-11-15: |92 - 99.5| = 7.5, threshold = 5% of 99.5 = 4.975 -> YES
        # - 2024-10-15: |115 - 108| = 7, threshold = 5% of 108 = 5.4 -> YES
        # Drift rate = 4/6
        assert hr["drift"]["earnings_intraday"]["1.0"] == pytest.approx(4/6, abs=0.01)
    
    def test_em_source_label(self):
        """Verify EM source is labeled correctly."""
        client = HoldRiskMockOratsClient()
        out = compute_breach_stats(
            client=client,
            ticker="TST",
            n=20,
            years=5,
            k=1.0,
            today=dt.date(2025, 3, 20),
        )
        
        hr = out["earningsHoldRisk"]
        assert hr["em_source"] == "ORATS_EARNINGS_IMPLIED"
    
    def test_flat_open_gate_value(self):
        """Verify flat open gate is the default 0.25."""
        client = HoldRiskMockOratsClient()
        out = compute_breach_stats(
            client=client,
            ticker="TST",
            n=20,
            years=5,
            k=1.0,
            today=dt.date(2025, 3, 20),
        )
        
        hr = out["earningsHoldRisk"]
        assert hr["flat_open_gate"] == 0.25
    
    def test_lookback_label(self):
        """Verify lookback label reflects actual event count."""
        client = HoldRiskMockOratsClient()
        out = compute_breach_stats(
            client=client,
            ticker="TST",
            n=20,
            years=5,
            k=1.0,
            today=dt.date(2025, 3, 20),
        )
        
        hr = out["earningsHoldRisk"]
        # Lookback should show the number of hold risk events built
        assert "6_events" in hr["lookback"]
    
    def test_no_events_scenario(self):
        """Verify graceful handling when no events available."""
        client = HoldRiskMockOratsClient()
        out = compute_breach_stats(
            client=client,
            ticker="NODATA",  # No events for this ticker
            n=20,
            years=5,
            k=1.0,
            today=dt.date(2025, 3, 20),
        )
        
        hr = out["earningsHoldRisk"]
        assert hr["sample_size"]["unconditional"] == 0
        assert hr["sample_size"]["flat_open"] == 0
        assert hr["unconditional"]["earnings_close"]["1.0"] is None


class TestHoldRiskRateConsistency:
    """Tests to verify rate computation consistency and logical constraints."""
    
    def test_conditional_sample_leq_unconditional(self):
        """Flat open sample size should never exceed unconditional."""
        client = HoldRiskMockOratsClient()
        out = compute_breach_stats(
            client=client,
            ticker="TST",
            n=20,
            years=5,
            k=1.0,
            today=dt.date(2025, 3, 20),
        )
        
        hr = out["earningsHoldRisk"]
        assert hr["sample_size"]["flat_open"] <= hr["sample_size"]["unconditional"]
    
    def test_breach_rate_bounds(self):
        """Breach rates should be between 0 and 1."""
        client = HoldRiskMockOratsClient()
        out = compute_breach_stats(
            client=client,
            ticker="TST",
            n=20,
            years=5,
            k=1.0,
            today=dt.date(2025, 3, 20),
        )
        
        hr = out["earningsHoldRisk"]
        
        for metric_group in [hr["unconditional"], hr["conditional_flat_open"], hr["drift"]]:
            for metric_name, k_rates in metric_group.items():
                for k_val, rate in k_rates.items():
                    if rate is not None:
                        assert 0.0 <= rate <= 1.0, f"Rate out of bounds: {metric_name}[{k_val}] = {rate}"
    
    def test_higher_k_lower_breach_rate(self):
        """Higher k-multiples should have lower or equal breach rates."""
        client = HoldRiskMockOratsClient()
        out = compute_breach_stats(
            client=client,
            ticker="TST",
            n=20,
            years=5,
            k=1.0,
            today=dt.date(2025, 3, 20),
        )
        
        hr = out["earningsHoldRisk"]
        
        # For unconditional earnings_close
        rates = hr["unconditional"]["earnings_close"]
        if rates["1.0"] is not None and rates["1.5"] is not None:
            assert rates["1.0"] >= rates["1.5"]
        if rates["1.5"] is not None and rates["2.0"] is not None:
            assert rates["1.5"] >= rates["2.0"]
