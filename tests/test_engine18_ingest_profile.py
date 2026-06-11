"""Engine 18 — single-ticker report lookup for the manual PEAD profile.

Covers the three EPS sources in priority order (EODHD -> FMP -> manual
overrides) plus the explicit not-found reasons.
"""
from __future__ import annotations

import datetime as dt

from backend.engine18.ingest import fetch_report_for_ticker

AS_OF = dt.date(2026, 6, 11)


class _Resp:
    def __init__(self, rows):
        self.rows = rows


class EodhdWithActual:
    def get_calendar_earnings(self, *, from_date=None, to_date=None, symbols=None):
        return _Resp([
            {"code": "ORCL.US", "report_date": "2026-06-10",
             "before_after_market": "AfterMarket", "actual": 1.30, "estimate": 1.00},
        ])


class EodhdStaleRow:
    """The live ORCL failure: calendar row exists but actual not posted."""

    def get_calendar_earnings(self, *, from_date=None, to_date=None, symbols=None):
        return _Resp([
            {"code": "ORCL.US", "report_date": "2026-06-16",
             "before_after_market": "AfterMarket", "actual": None, "estimate": 1.95},
        ])


class EodhdEmpty:
    def get_calendar_earnings(self, *, from_date=None, to_date=None, symbols=None):
        return _Resp([])


class FmpWithActual:
    def earnings_calendar(self, *, date_from=None, date_to=None, limit=None):
        return _Resp([
            {"symbol": "ORCL", "date": "2026-06-10", "time": "amc",
             "epsActual": 1.50, "epsEstimated": 1.00},
            {"symbol": "OTHR", "date": "2026-06-10", "epsActual": 9.9, "epsEstimated": 1.0},
        ])


class FmpEmpty:
    def earnings_calendar(self, *, date_from=None, date_to=None, limit=None):
        return _Resp([])


def test_eodhd_primary_source():
    rep, source, reason = fetch_report_for_ticker(
        "ORCL", as_of=AS_OF, client=EodhdWithActual(), fmp_client=FmpEmpty(),
    )
    assert source == "eodhd" and reason == ""
    assert rep.report_date == "2026-06-10"
    assert rep.timing == "amc"
    assert abs(rep.surprise_pct - 0.30) < 1e-9


def test_fmp_fallback_when_eodhd_actual_missing():
    rep, source, reason = fetch_report_for_ticker(
        "ORCL", as_of=AS_OF, client=EodhdStaleRow(), fmp_client=FmpWithActual(),
    )
    assert source == "fmp" and reason == ""
    assert rep.ticker == "ORCL"          # OTHR row filtered out
    assert abs(rep.surprise_pct - 0.50) < 1e-9
    assert rep.timing == "amc"


def test_manual_overrides_last_resort():
    rep, source, reason = fetch_report_for_ticker(
        "ORCL", as_of=AS_OF, client=EodhdStaleRow(), fmp_client=FmpEmpty(),
        overrides={"actual_eps": 2.10, "estimate_eps": 1.95,
                   "report_date": "2026-06-10", "timing": "amc"},
    )
    assert source == "manual" and reason == ""
    assert rep.report_date == "2026-06-10"
    assert abs(rep.surprise_pct - (2.10 - 1.95) / 1.95) < 1e-9


def test_stale_row_reason_mentions_pending_actual():
    rep, source, reason = fetch_report_for_ticker(
        "ORCL", as_of=AS_OF, client=EodhdStaleRow(), fmp_client=FmpEmpty(),
    )
    assert rep is None and source == ""
    assert "no actual EPS posted yet" in reason


def test_nothing_found_reason():
    rep, source, reason = fetch_report_for_ticker(
        "NOPE", as_of=AS_OF, client=EodhdEmpty(), fmp_client=FmpEmpty(),
    )
    assert rep is None
    assert "No earnings report found" in reason
