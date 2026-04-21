"""NYSE trading calendar tests for Engine 15 v2."""
from __future__ import annotations

import datetime as dt

import pytest

from backend.engine15 import trading_calendar as tc


def test_weekends_not_trading():
    assert not tc.is_trading_day(dt.date(2026, 5, 23))  # Sat
    assert not tc.is_trading_day(dt.date(2026, 5, 24))  # Sun


def test_thanksgiving_2026_holiday():
    # Thanksgiving 2026 = Nov 26 (Thursday); day after is half-day
    assert tc.is_holiday("2026-11-26") is True
    assert tc.is_trading_day("2026-11-26") is False
    assert tc.is_half_day("2026-11-27") is True
    assert tc.is_trading_day("2026-11-27") is True  # half day is still trading


def test_christmas_day():
    assert tc.is_holiday("2025-12-25") is True
    assert tc.is_holiday("2026-12-25") is True


def test_july_4_2026_observed_on_3rd():
    # July 4 2026 is Saturday -> NYSE closes Friday July 3
    assert tc.is_holiday("2026-07-03") is True
    # July 4 itself is a weekend, not marked as holiday
    assert tc.is_trading_day("2026-07-04") is False


def test_business_days_skips_thanksgiving():
    days = list(tc.business_days("2026-11-23", "2026-11-27"))
    # Mon, Tue, Wed, (skip Thu Thanksgiving), Fri
    assert [d.isoformat() for d in days] == [
        "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-27",
    ]


def test_business_days_between_semantic():
    # business_days_between(entry, exit) counts days after entry through exit.
    assert tc.business_days_between("2026-11-23", "2026-11-27") == 3


def test_add_business_days_skips_holidays():
    # Mon 11/23 + 3 biz days should land on Fri 11/27 (skip Thanksgiving Thu).
    assert tc.add_business_days("2026-11-23", 3) == dt.date(2026, 11, 27)


def test_add_business_days_negative():
    assert tc.add_business_days("2026-11-27", -3) == dt.date(2026, 11, 23)


def test_add_business_days_zero_snaps_to_trading_day():
    # Weekend input snaps forward to next trading day.
    assert tc.add_business_days("2026-05-23", 0) == dt.date(2026, 5, 26)  # Sat -> Tue (Mon is Memorial Day)


def test_last_calibrated_year_is_present():
    # Hard-coded table runs through at least 2030
    assert tc.last_calibrated_year() >= 2030


def test_biz_shift_wiring_honours_flag(monkeypatch):
    """backend.engine15.event_universe.biz_shift uses the calendar when
    the flag is on."""
    from backend.engine15 import event_universe
    shifted = event_universe.biz_shift(dt.date(2026, 11, 23), 3)
    # With holiday calendar, +3 biz days lands on Friday 11/27 (skip Thanksgiving).
    assert shifted == dt.date(2026, 11, 27)


def test_biz_shift_falls_back_to_mon_fri_when_flag_off(monkeypatch):
    from dataclasses import replace
    from backend.config import get_flags
    from backend.engine15 import event_universe
    flags = replace(get_flags(), ENGINE15_HOLIDAY_CALENDAR=False)
    monkeypatch.setattr("backend.config.get_flags", lambda: flags)
    # Without the calendar, Mon-Fri only: +3 biz days from 11/23 lands on 11/26 (Thu).
    shifted = event_universe.biz_shift(dt.date(2026, 11, 23), 3)
    assert shifted == dt.date(2026, 11, 26)


def test_chain_replay_enumerate_biz_days_respects_holiday():
    from backend.engine15.chain_replay_adapter import _enumerate_biz_days
    days = _enumerate_biz_days(dt.date(2026, 11, 23), dt.date(2026, 11, 27))
    assert "2026-11-26" not in days  # Thanksgiving
    assert "2026-11-27" in days       # half-day still trading
