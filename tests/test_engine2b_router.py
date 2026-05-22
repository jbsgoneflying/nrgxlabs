"""Tests for /api/spx-ic/flex (backend.routers.engine2b_flex_ic).

Validates the gate flag, parameter validation, and the happy-path
plumbing (router -> compute_engine2b_flex_ic). The engine itself is
monkeypatched so we don't need ORATS for these tests.
"""
from __future__ import annotations

import datetime as dt
import threading

import pytest


@pytest.fixture
def patched_router(monkeypatch):
    from backend.routers import engine2b_flex_ic as router

    class _Flags:
        ENABLE_ENGINE2_SPX_IC = True
        ENABLE_E2B_FLEX_EXPIRY = True
        ENGINE2_POLICY_MAX_BREACH_PCT = 25.0
        ENGINE2_POLICY_MAX_OUTSIDE_WINGS_PCT = 10.0
        ENGINE2_POLICY_MAX_MAE95_X_WING = 1.0

        def cache_key_engine2(self):
            return ("k",)

    calls = {"n": 0, "last_kwargs": None}

    def _fake_compute(**kwargs):
        calls["n"] += 1
        calls["last_kwargs"] = kwargs
        return {
            "asOfDate": "2026-05-22",
            "underlying": {"symbol": "SPX", "isProxy": False, "notes": []},
            "flexExpiry": {
                "entryDate": kwargs["entry_date"].isoformat(),
                "expiryDate": kwargs["expiry_date"].isoformat(),
                "spansHoliday": True,
                "dteSessions": 1,
                "dteCalendarDays": 4,
            },
            "riskGrid": {"cells": [], "count": 0},
            "weeks": [],
        }

    monkeypatch.setattr(router, "get_flags", lambda: _Flags())
    monkeypatch.setattr(router, "is_us_equity_market_open", lambda: True)
    monkeypatch.setattr(router, "compute_engine2b_flex_ic", _fake_compute)
    monkeypatch.setattr(router, "get_client", lambda: object())
    monkeypatch.setattr(router, "get_benzinga_client_optional", lambda: None)
    monkeypatch.setattr(router, "flex_cache", {})
    monkeypatch.setattr(router, "flex_cache_lock", threading.Lock())
    return router, calls, _Flags


def test_router_happy_path(patched_router):
    router, calls, _ = patched_router
    payload = router.spx_ic_flex(
        underlying="SPX",
        entry_date="2026-05-22",
        expiry="2026-05-26",
        years=2,
        widths="1.0,1.5,2.0,2.5",
        risk_target_breach_pct=25.0,
    )
    assert calls["n"] == 1
    assert payload["flexExpiry"]["entryDate"] == "2026-05-22"
    assert payload["flexExpiry"]["expiryDate"] == "2026-05-26"
    kwargs = calls["last_kwargs"]
    assert kwargs["entry_date"] == dt.date(2026, 5, 22)
    assert kwargs["expiry_date"] == dt.date(2026, 5, 26)
    assert kwargs["widths"] == [1.0, 1.5, 2.0, 2.5]


def test_router_400_on_inverted_dates(patched_router):
    router, _, _ = patched_router
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        router.spx_ic_flex(
            underlying="SPX",
            entry_date="2026-05-26",
            expiry="2026-05-22",
            years=2,
            widths="1.0",
            risk_target_breach_pct=25.0,
        )
    assert exc.value.status_code == 400
    assert "after entry_date" in exc.value.detail.lower()


def test_router_400_on_unparseable_date(patched_router):
    router, _, _ = patched_router
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        router.spx_ic_flex(
            underlying="SPX",
            entry_date="not-a-date",
            expiry="2026-05-26",
            years=2,
            widths="1.0",
            risk_target_breach_pct=25.0,
        )
    assert exc.value.status_code == 400
    assert "entry_date" in exc.value.detail


def test_router_400_on_bad_underlying(patched_router):
    router, _, _ = patched_router
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        router.spx_ic_flex(
            underlying="TSLA",
            entry_date="2026-05-22",
            expiry="2026-05-26",
            years=2,
            widths="1.0",
            risk_target_breach_pct=25.0,
        )
    assert exc.value.status_code == 400


def test_router_400_on_span_too_long(patched_router):
    router, _, _ = patched_router
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        router.spx_ic_flex(
            underlying="SPX",
            entry_date="2026-01-01",
            expiry="2026-12-01",
            years=2,
            widths="1.0",
            risk_target_breach_pct=25.0,
        )
    assert exc.value.status_code == 400


def test_router_404_when_flex_disabled(monkeypatch):
    from backend.routers import engine2b_flex_ic as router

    class _Flags:
        ENABLE_ENGINE2_SPX_IC = True
        ENABLE_E2B_FLEX_EXPIRY = False  # disabled

        def cache_key_engine2(self):
            return ("k",)

    monkeypatch.setattr(router, "get_flags", lambda: _Flags())

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        router.spx_ic_flex(
            underlying="SPX",
            entry_date="2026-05-22",
            expiry="2026-05-26",
            years=2,
            widths="1.0",
            risk_target_breach_pct=25.0,
        )
    assert exc.value.status_code == 404


def test_router_404_when_engine2_disabled(monkeypatch):
    from backend.routers import engine2b_flex_ic as router

    class _Flags:
        ENABLE_ENGINE2_SPX_IC = False  # parent gate off
        ENABLE_E2B_FLEX_EXPIRY = True

        def cache_key_engine2(self):
            return ("k",)

    monkeypatch.setattr(router, "get_flags", lambda: _Flags())

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        router.spx_ic_flex(
            underlying="SPX",
            entry_date="2026-05-22",
            expiry="2026-05-26",
            years=2,
            widths="1.0",
            risk_target_breach_pct=25.0,
        )
    assert exc.value.status_code == 404


def test_router_caches_when_market_closed(monkeypatch):
    from backend.routers import engine2b_flex_ic as router

    class _Flags:
        ENABLE_ENGINE2_SPX_IC = True
        ENABLE_E2B_FLEX_EXPIRY = True

        def cache_key_engine2(self):
            return ("k",)

    calls = {"n": 0}

    def _fake_compute(**kwargs):
        calls["n"] += 1
        return {"flexExpiry": {"entryDate": "x", "expiryDate": "y"}, "riskGrid": {"cells": []}, "weeks": []}

    monkeypatch.setattr(router, "get_flags", lambda: _Flags())
    monkeypatch.setattr(router, "is_us_equity_market_open", lambda: False)
    monkeypatch.setattr(router, "compute_engine2b_flex_ic", _fake_compute)
    monkeypatch.setattr(router, "get_client", lambda: object())
    monkeypatch.setattr(router, "get_benzinga_client_optional", lambda: None)
    monkeypatch.setattr(router, "flex_cache", {})
    monkeypatch.setattr(router, "flex_cache_lock", threading.Lock())

    router.spx_ic_flex(
        underlying="SPX",
        entry_date="2026-05-22",
        expiry="2026-05-26",
        years=2,
        widths="1.0",
        risk_target_breach_pct=25.0,
    )
    router.spx_ic_flex(
        underlying="SPX",
        entry_date="2026-05-22",
        expiry="2026-05-26",
        years=2,
        widths="1.0",
        risk_target_breach_pct=25.0,
    )
    assert calls["n"] == 1, "second call should hit the cache when market is closed"
