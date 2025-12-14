from backend.earnings_logic import compute_breach_stats


class FakeResp:
    def __init__(self, rows):
        self.rows = rows
        self.raw = rows


class FakeOratsClient:
    def __init__(self):
        # earnings: 3 usable events in Q1 (to test quarter aggregation) + 1 usable in Q4
        self._earnings = [
            {"earnDate": "2025-03-01", "anncTod": "1630"},  # Q1 AMC
            {"earnDate": "2025-02-05", "anncTod": "0830"},  # Q1 BMO
            {"earnDate": "2025-01-30", "anncTod": "1630"},  # Q1 AMC
            {"earnDate": "2024-10-31", "anncTod": "0830"},  # Q4 BMO
        ]

        # dailies bars
        self._dailies = {
            # Q1 event 2025-03-01 AMC: close 100 -> next open 103 => realized 3%
            ("TST", "2025-03-01"): {"tradeDate": "2025-03-01", "clsPx": 100.0, "open": 100.0},
            ("TST", "2025-03-02"): {"tradeDate": "2025-03-02", "clsPx": 102.0, "open": 103.0},

            # Q1 event 2025-02-05 BMO: prior close 100 -> open 96.4 => realized 3.6%
            ("TST", "2025-02-04"): {"tradeDate": "2025-02-04", "clsPx": 100.0, "open": 100.0},
            ("TST", "2025-02-05"): {"tradeDate": "2025-02-05", "clsPx": 95.0, "open": 96.4},

            # AMC event: close on 2025-01-30, open next trading day 2025-01-31
            ("TST", "2025-01-30"): {"tradeDate": "2025-01-30", "clsPx": 100.0, "open": 99.0},
            ("TST", "2025-01-31"): {"tradeDate": "2025-01-31", "clsPx": 95.0, "open": 112.0},
            # BMO event: prior close 2024-10-30, open on 2024-10-31
            ("TST", "2024-10-30"): {"tradeDate": "2024-10-30", "clsPx": 200.0, "open": 201.0},
            ("TST", "2024-10-31"): {"tradeDate": "2024-10-31", "clsPx": 180.0, "open": 184.0},
        }

        # cores snapshots (impErnMv)
        # store percent-style (e.g. 5.0 means 5%)
        self._cores = {
            ("TST", "2025-03-01"): {"tradeDate": "2025-03-01", "impErnMv": 5.0},   # 5% implied
            ("TST", "2025-02-04"): {"tradeDate": "2025-02-04", "impErnMv": 4.0},   # 4% implied (BMO pricing date)
            ("TST", "2025-01-30"): {"tradeDate": "2025-01-30", "impErnMv": 8.0},
            ("TST", "2024-10-30"): {"tradeDate": "2024-10-30", "impErnMv": 2.0},
        }

    def hist_earnings(self, ticker: str):
        return FakeResp(self._earnings if ticker == "TST" else [])

    def hist_dailies(self, ticker: str, trade_date: str, fields: str):
        row = self._dailies.get((ticker, trade_date))
        return FakeResp([row] if row else [])

    def hist_cores(self, ticker: str, trade_date: str, fields: str):
        row = self._cores.get((ticker, trade_date))
        return FakeResp([row] if row else [])


def test_compute_breach_stats_mocked():
    client = FakeOratsClient()
    out = compute_breach_stats(client=client, ticker="TST", n=20, years=5, k=1.0)

    assert out["ticker"] == "TST"
    assert out["params"]["n"] == 20
    assert out["summary"]["events_found"] == 4
    assert out["summary"]["events_used"] == 4

    # Breaches at k=1.0:
    # - 2025-03-01: implied 5%, realized 3% => no breach
    # - 2025-02-05: implied 4%, realized 3.6% => no breach
    # - 2025-01-30: implied 8%, realized 12% => breach
    # - 2024-10-31: implied 2%, realized 8% => breach
    assert out["summary"]["breaches"] == 2
    assert out["summary"]["breach_rate_pct"] == 50.0

    # Baseline (overall usable set)
    b = out["baseline"]
    assert b["events_used"] == 4
    assert b["breach_rate_pct"] == 50.0
    # ratios overall: 0.6, 0.9, 1.5, 4.0 => avg 1.75
    assert b["avg_ratio_realized_to_implied"] == 1.75
    # overshoot overall: 50 and 300 => avg 175
    assert b["avg_above_breach_pct"] == 175.0

    # Quarter aggregation sanity:
    q = out["quarters"]
    assert q["Q1"]["events_total"] == 3
    assert q["Q1"]["events_used"] == 3
    assert q["Q1"]["breaches"] == 1
    # Near 0.9: ratios are 0.6, 0.9, 1.5 => 2/3 near
    assert q["Q1"]["near_breach_rate_pct"]["0.9"] == 66.67
    # Avg ratio: (0.6 + 0.9 + 1.5)/3 = 1.0
    assert q["Q1"]["avg_ratio_realized_to_implied"] == 1.0
    assert q["Q1"]["max_ratio_realized_to_implied"] == 1.5
    # Only breached overshoot: (12-8)/8 = 50%
    assert q["Q1"]["avg_above_breach_pct"] == 50.0
    # Recommendation should be Wide (breach_rate>=25 or near0.9>=40)
    assert q["Q1"]["recommendation"] == "Wide"
    # Seasonality vs baseline: breach 33.33% vs 50% => -16.67pp
    assert q["Q1"]["seasonality"]["breach_delta_pp"] == -16.67
    assert q["Q1"]["seasonality"]["ratio_delta"] == -0.75
    assert q["Q1"]["seasonality"]["overshoot_delta_pp"] == -125.0

    assert q["Q4"]["events_total"] == 1
    assert q["Q4"]["events_used"] == 1
    assert q["Q4"]["recommendation"] == "Avoid (low sample)"
    assert q["Q4"]["seasonality"]["breach_delta_pp"] is None

    # Events include required keys
    ev = out["events"][0]
    for k in (
        "earnDate",
        "anncTod",
        "timing",
        "pricingDateUsed",
        "impErnMv",
        "impliedMovePct",
        "closeDateUsed",
        "closePx",
        "openDateUsed",
        "openPx",
        "realizedMovePct",
        "breach",
        "aboveBreachPct",
        "notes",
    ):
        assert k in ev


