import datetime as dt

from backend.earnings_logic import DailyBar, find_trading_day


def test_find_trading_day_prior_skips_weekend():
    # Monday 2025-01-06; prior trading day should be Friday 2025-01-03
    bars = {
        "2025-01-03": DailyBar(tradeDate="2025-01-03", open=100.0, clsPx=101.0),
        # weekend missing
        "2025-01-04": None,
        "2025-01-05": None,
    }

    def get_bar(d: str):
        return bars.get(d)

    out = find_trading_day(get_bar, start=dt.date(2025, 1, 5), direction=-1, max_steps=5)
    assert out is not None
    assert out.tradeDate == "2025-01-03"


def test_find_trading_day_next_skips_holiday_gap():
    # simulate missing data for a few days; ensure it advances
    bars = {
        "2025-01-02": None,
        "2025-01-03": None,
        "2025-01-04": DailyBar(tradeDate="2025-01-04", open=10.0, clsPx=11.0),
    }

    def get_bar(d: str):
        return bars.get(d)

    out = find_trading_day(get_bar, start=dt.date(2025, 1, 2), direction=+1, max_steps=5)
    assert out is not None
    assert out.tradeDate == "2025-01-04"


