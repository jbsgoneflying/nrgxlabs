import datetime as dt

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
        # Handle range queries (e.g., "2025-01-01,2025-03-31") for bulk fetch
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
        if path == "/hist/cores":
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


def test_compute_breach_stats_mocked():
    client = FakeOratsClient()
    # Pin "today" so current-quarter selection is deterministic in tests
    out = compute_breach_stats(client=client, ticker="TST", n=20, years=5, k=1.0, today=dt.date(2025, 3, 1))

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

    # Phase 1 directional summary aggregates
    assert out["summary"]["upBreaches"] == 1
    assert out["summary"]["downBreaches"] == 1
    assert out["summary"]["upBreachRatePct"] == 25.0
    assert out["summary"]["downBreachRatePct"] == 25.0
    assert out["summary"]["avgUpOvershootPct"] == 50.0
    assert out["summary"]["avgDownOvershootPct"] == 300.0
    # Overshoot asymmetry is extreme here: bias should be DOWN
    assert out["summary"]["tailBias"] == "DOWN"

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

    # Phase 1 directional quarter metrics (Q1 has 1 UP breach, 0 DOWN breaches)
    assert q["Q1"]["quarterUpBreachRatePct"] == 33.33
    assert q["Q1"]["quarterDownBreachRatePct"] == 0.0
    assert q["Q1"]["quarterAvgUpOvershootPct"] == 50.0
    assert q["Q1"]["quarterAvgDownOvershootPct"] is None
    # Deltas vs baseline (allowed because Q1 has 3 usable events)
    assert q["Q1"]["quarterUpBreachDeltaPP"] == 8.33
    assert q["Q1"]["quarterDownBreachDeltaPP"] == -25.0
    assert q["Q1"]["quarterAvgUpOvershootDeltaPP"] == 0.0
    assert q["Q1"]["quarterAvgDownOvershootDeltaPP"] is None

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
        "signedMovePct",
        "moveDirection",
        "upBreach",
        "downBreach",
        "breachSide",
        "upOvershootPct",
        "downOvershootPct",
        "breach",
        "aboveBreachPct",
        "aboveBreachPctVsK",
        "pricingDateShiftDays",
        "realizedWindowShiftDays",
        "notes",
    ):
        assert k in ev

    # Telemetry sanity: no substitution in this mocked dataset (all dates exist as requested)
    for e in out["events"]:
        if e["timing"] in ("AMC", "BMO"):
            assert e["pricingDateShiftDays"] in (0, None)
            assert e["realizedWindowShiftDays"] in (0, None)

    # Phase 1 per-event directional correctness (AMC + BMO)
    ev_by_date = {e["earnDate"]: e for e in out["events"]}
    amc = ev_by_date["2025-03-01"]
    assert amc["timing"] == "AMC"
    assert amc["signedMovePct"] == 3.0
    assert amc["moveDirection"] == "UP"
    assert amc["upBreach"] is False
    assert amc["downBreach"] is False
    assert amc["breachSide"] is None

    bmo = ev_by_date["2025-02-05"]
    assert bmo["timing"] == "BMO"
    assert bmo["signedMovePct"] == -3.6
    assert bmo["moveDirection"] == "DOWN"
    assert bmo["upBreach"] is False
    assert bmo["downBreach"] is False
    assert bmo["breachSide"] is None

    up_breach = ev_by_date["2025-01-30"]
    assert up_breach["upBreach"] is True
    assert up_breach["downBreach"] is False
    assert up_breach["breachSide"] == "UP"
    assert up_breach["upOvershootPct"] == 50.0
    assert up_breach["downOvershootPct"] is None

    down_breach = ev_by_date["2024-10-31"]
    assert down_breach["upBreach"] is False
    assert down_breach["downBreach"] is True
    assert down_breach["breachSide"] == "DOWN"
    assert down_breach["upOvershootPct"] is None
    assert down_breach["downOvershootPct"] == 300.0

    # Phase 2 wingRecommendation (history + regime only; skew missing)
    wr = out.get("wingRecommendation")
    assert isinstance(wr, dict)
    assert wr["quality"] == "MISSING"
    assert wr["quarterKey"] == "Q1"
    assert wr["quarterRecommendation"] == "Wide"
    assert wr["structureMode"] == "AUTO_EQUAL_DELTA"
    assert wr["recommendationLabel"] == "WIDEN_PUTS_TIGHTEN_CALLS"
    assert wr["confidence"] == "LOW"
    assert wr["baseWingMultiple"] == 1.5
    assert wr["putWingMultiple"] == 2.03
    assert wr["callWingMultiple"] == 0.98
    # TAS should be negative for downside tail dominance
    assert wr["tas"] < 0

    # Phase 4 skew overlay scaffolding should degrade safely
    so = out.get("skewOverlay")
    assert isinstance(so, dict)
    assert so["current"]["skewQuality"] == "MISSING"
    assert "Skew unavailable" in (so["current"]["notes"] or "")


