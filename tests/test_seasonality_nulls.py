from backend.earnings_logic import compute_breach_stats


class FakeResp:
    def __init__(self, rows):
        self.rows = rows
        self.raw = rows


class NoBreachClient:
    """All usable events have realized < implied => 0 breaches overall and per quarter."""

    def __init__(self):
        self._earnings = [
            {"earnDate": "2025-03-01", "anncTod": "1630"},  # Q1 AMC
            {"earnDate": "2025-02-05", "anncTod": "0830"},  # Q1 BMO
            {"earnDate": "2024-10-31", "anncTod": "0830"},  # Q4 BMO
        ]

        self._dailies = {
            ("TST", "2025-03-01"): {"tradeDate": "2025-03-01", "clsPx": 100.0, "open": 100.0},
            ("TST", "2025-03-02"): {"tradeDate": "2025-03-02", "clsPx": 101.0, "open": 101.0},

            ("TST", "2025-02-04"): {"tradeDate": "2025-02-04", "clsPx": 100.0, "open": 100.0},
            ("TST", "2025-02-05"): {"tradeDate": "2025-02-05", "clsPx": 99.0, "open": 99.5},

            ("TST", "2024-10-30"): {"tradeDate": "2024-10-30", "clsPx": 200.0, "open": 200.0},
            ("TST", "2024-10-31"): {"tradeDate": "2024-10-31", "clsPx": 199.0, "open": 199.0},
        }

        self._cores = {
            ("TST", "2025-03-01"): {"tradeDate": "2025-03-01", "impErnMv": 5.0},   # 5% implied
            ("TST", "2025-02-04"): {"tradeDate": "2025-02-04", "impErnMv": 4.0},   # 4% implied
            ("TST", "2024-10-30"): {"tradeDate": "2024-10-30", "impErnMv": 3.0},   # 3% implied
        }

    def hist_earnings(self, ticker: str):
        return FakeResp(self._earnings if ticker == "TST" else [])

    def hist_dailies(self, ticker: str, trade_date: str, fields: str):
        row = self._dailies.get((ticker, trade_date))
        return FakeResp([row] if row else [])

    def hist_cores(self, ticker: str, trade_date: str, fields: str):
        row = self._cores.get((ticker, trade_date))
        return FakeResp([row] if row else [])


def test_no_breaches_baseline_and_overshoot_null():
    out = compute_breach_stats(client=NoBreachClient(), ticker="TST", n=20, years=5, k=1.0)
    assert out["summary"]["breaches"] == 0
    assert out["baseline"]["avg_above_breach_pct"] is None

    # Q1 has >=2 events but <3 => low-sample gating on seasonality
    q1 = out["quarters"]["Q1"]
    assert q1["events_used"] == 2
    assert q1["recommendation"].startswith("Avoid")
    assert q1["seasonality"]["breach_delta_pp"] is None

    # Q4 has 1 event => low-sample gating too
    q4 = out["quarters"]["Q4"]
    assert q4["events_used"] == 1
    assert q4["seasonality"]["overshoot_delta_pp"] is None


