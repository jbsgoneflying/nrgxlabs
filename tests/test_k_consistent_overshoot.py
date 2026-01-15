import datetime as dt

from backend.earnings_logic import compute_breach_stats


class FakeResp:
    def __init__(self, rows):
        self.rows = rows
        self.raw = rows


class OneEventClient:
    """
    One AMC event with a large realized move, used to test k-consistent overshoot.
    """

    def __init__(self):
        self._earnings = [{"earnDate": "2025-03-01", "anncTod": "1630"}]  # AMC
        self._dailies = {
            ("TST", "2025-03-01"): {"tradeDate": "2025-03-01", "clsPx": 100.0, "open": 100.0},
            ("TST", "2025-03-02"): {"tradeDate": "2025-03-02", "clsPx": 110.0, "open": 112.0},  # +12% open
        }
        self._cores = {("TST", "2025-03-01"): {"tradeDate": "2025-03-01", "impErnMv": 5.0}}  # 5% implied

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

    # Minimal range-mode support for regime overlay/backtest (avoid slow fallback loops).
    def get(self, path: str, params: dict):
        ticker = params.get("ticker", "")
        from_date = params.get("fromDate", "")
        to_date = params.get("toDate", "")
        
        if path == "/hist/dailies" and ticker == "SPY":
            to_date_str = str(to_date or "2025-03-01")[:10]
            end = dt.date.fromisoformat(to_date_str)
            rows = []
            px = 100.0
            for i in range(0, 40):
                d = end - dt.timedelta(days=(40 - i))
                px *= 1.001
                rows.append({"ticker": "SPY", "tradeDate": d.isoformat(), "clsPx": px, "open": px})
            return FakeResp(rows)
        if path == "/hist/dailies" and ticker == "TST" and from_date and to_date:
            # Handle range queries for TST dailies (bulk fetch)
            rows = []
            for (t, d), row in self._dailies.items():
                if t == ticker and from_date <= d <= to_date:
                    rows.append(row)
            return FakeResp(rows)
        if path == "/hist/cores" and ticker == "TST":
            # Support both range and single lookups
            if from_date and to_date:
                # Range query for bulk fetch
                rows = []
                for (t, d), row in self._cores.items():
                    if t == ticker and from_date <= d <= to_date:
                        rows.append(row)
                return FakeResp(rows)
            # For regime overlay
            to_date_str = str(to_date or "2025-03-01")[:10]
            end = dt.date.fromisoformat(to_date_str)
            rows = []
            for i in range(0, 40):
                d = end - dt.timedelta(days=(40 - i))
                rows.append({"ticker": "TST", "tradeDate": d.isoformat(), "iv30": 0.30})
            return FakeResp(rows)
        return FakeResp([])


def test_k_consistent_overshoot_matches_threshold_definition(monkeypatch):
    monkeypatch.setenv("ADD_K_CONSISTENT_OVERSHOOT", "true")
    # Keep strict off so the event stays usable
    monkeypatch.setenv("STRICT_REALIZED_WINDOW", "false")

    # implied=5%, realized=12%, k=2 => threshold=10%
    # legacy aboveBreachPct = (12-5)/5 = 140%
    # k-consistent overshoot = (12-10)/10 = 20%
    out = compute_breach_stats(client=OneEventClient(), ticker="TST", n=20, years=5, k=2.0, today=dt.date(2025, 3, 1))
    ev = out["events"][0]
    assert ev["breach"] is True
    assert ev["aboveBreachPct"] == 140.0
    assert ev["aboveBreachPctVsK"] == 20.0
    assert out["summary"]["avg_above_breach_pct_vs_k"] == 20.0


