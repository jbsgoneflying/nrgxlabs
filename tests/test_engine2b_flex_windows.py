"""Tests for backend.engine2b.flex_windows.

Pinning the desk-facing example: Fri 2026-05-22 -> Tue 2026-05-26 with
Memorial Day (Mon 2026-05-25) sitting in the middle. The flex window
builder must (a) recognise the gap contains a real NYSE holiday, (b)
return analogues from prior years that share the same shape, and (c)
drop candidates without matching ORATS bars.
"""
from __future__ import annotations

import datetime as dt
from typing import List

import pytest

from backend.engine2b.flex_windows import (
    FlexWindow,
    build_flex_windows,
    derive_target_shape,
)


def _all_trading_days_in_years(years: List[int]) -> List[str]:
    """Helper: every weekday from Jan 1 of the earliest year through Dec 31
    of the latest year, minus the small subset we want to skip in tests.
    """
    from backend.engine15.trading_calendar import is_trading_day

    out: List[str] = []
    start = dt.date(min(years), 1, 1)
    end = dt.date(max(years), 12, 31)
    d = start
    while d <= end:
        if is_trading_day(d):
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def test_derive_target_shape_memorial_day_2026():
    shape = derive_target_shape(
        entry_date=dt.date(2026, 5, 22),
        expiry_date=dt.date(2026, 5, 26),
    )
    assert shape["entryWeekday"] == 4   # Friday
    assert shape["expiryWeekday"] == 1  # Tuesday
    assert shape["dteCalendarDays"] == 4
    # Sessions held after entry: Mon 5/25 is Memorial Day (closed), Tue 5/26
    # is the only trading session held.
    assert shape["dteSessions"] == 1
    assert shape["spansHoliday"] is True


def test_derive_target_shape_normal_fri_to_tue_does_not_span_holiday():
    """A normal Fri -> Tue with no holiday must NOT flag spans_holiday."""
    shape = derive_target_shape(
        entry_date=dt.date(2026, 5, 15),
        expiry_date=dt.date(2026, 5, 19),
    )
    assert shape["dteCalendarDays"] == 4
    assert shape["dteSessions"] == 2  # Mon + Tue both held
    assert shape["spansHoliday"] is False


def test_derive_target_shape_rejects_expiry_before_entry():
    with pytest.raises(ValueError):
        derive_target_shape(
            entry_date=dt.date(2026, 5, 26),
            expiry_date=dt.date(2026, 5, 22),
        )


def test_memorial_day_holiday_analogues_2024_2025():
    """The May-22-2026 -> May-26-2026 shape must match Fri-before-Memorial-Day
    analogues in 2024 and 2025 once we feed the builder enough trade-date
    history.
    """
    trade_dates = _all_trading_days_in_years([2023, 2024, 2025, 2026])
    windows = build_flex_windows(
        trade_dates=trade_dates,
        target_entry_weekday=4,   # Friday
        target_sessions=1,
        target_calendar_days=4,
        years=4,
        today=dt.date(2026, 5, 22),
    )
    assert windows, "expected at least one analogue window"
    # All windows must match the requested shape.
    for w in windows:
        assert w.entry_date.weekday() == 4
        assert w.dte_calendar_days == 4
        assert w.dte_sessions == 1
        # If sessions=1 and cal_days=4, the gap must contain a real NYSE
        # holiday (Sat + Sun + holiday-Mon, then Tue is the held session).
        assert w.spans_holiday is True

    # Specifically check the Fri-before-Memorial-Day shape shows up
    # for 2024 (Fri 5/24 -> Tue 5/28) and 2025 (Fri 5/23 -> Tue 5/27).
    pairs = {(w.entry_date, w.expiry_date) for w in windows}
    assert (dt.date(2024, 5, 24), dt.date(2024, 5, 28)) in pairs
    assert (dt.date(2025, 5, 23), dt.date(2025, 5, 27)) in pairs


def test_drops_windows_with_missing_trade_bars():
    """If we strip an analogue's entry bar from the trade-date index, the
    builder must skip it instead of returning a phantom window.
    """
    full = _all_trading_days_in_years([2024, 2025, 2026])
    missing_entry = dt.date(2025, 5, 23).isoformat()
    stripped = [d for d in full if d != missing_entry]

    windows_full = build_flex_windows(
        trade_dates=full,
        target_entry_weekday=4,
        target_sessions=1,
        target_calendar_days=4,
        years=3,
        today=dt.date(2026, 5, 22),
    )
    windows_stripped = build_flex_windows(
        trade_dates=stripped,
        target_entry_weekday=4,
        target_sessions=1,
        target_calendar_days=4,
        years=3,
        today=dt.date(2026, 5, 22),
    )
    full_pairs = {(w.entry_date, w.expiry_date) for w in windows_full}
    stripped_pairs = {(w.entry_date, w.expiry_date) for w in windows_stripped}
    assert (dt.date(2025, 5, 23), dt.date(2025, 5, 27)) in full_pairs
    assert (dt.date(2025, 5, 23), dt.date(2025, 5, 27)) not in stripped_pairs


def test_normal_fri_to_mon_does_not_match_holiday_shape():
    """A normal Fri -> Mon (3 cal days) must not appear under the Memorial-Day
    target shape (4 cal days).
    """
    trade_dates = _all_trading_days_in_years([2024, 2025, 2026])
    windows = build_flex_windows(
        trade_dates=trade_dates,
        target_entry_weekday=4,
        target_sessions=1,
        target_calendar_days=4,
        years=3,
        today=dt.date(2026, 5, 22),
        calendar_days_tol=0,  # strict, so 3-day weekends do not slip in
    )
    for w in windows:
        assert w.dte_calendar_days == 4


def test_flex_window_to_weekly_window_adapter():
    fw = FlexWindow(
        entry_date=dt.date(2025, 5, 23),
        expiry_date=dt.date(2025, 5, 27),
        dte_sessions=1,
        dte_calendar_days=4,
        spans_holiday=True,
    )
    ww = fw.to_weekly_window()
    assert ww.entry_date == fw.entry_date
    assert ww.expiry_date == fw.expiry_date
    assert ww.dte_sessions == 1
    assert ww.dte_calendar_days == 4
