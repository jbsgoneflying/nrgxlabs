"""Shared Engine 1 breach-stats cache tests.

E1 and E15 should share a single cached payload so a scan -> scenario
flow on the same ticker + event pair doesn't pay ORATS cost twice.
"""
from __future__ import annotations

import pytest

from backend.engine1 import (
    clear_shared_cache,
    get_or_compute_breach_stats,
    get_shared_cache_stats,
    reset_shared_cache_stats,
)


class _FakeResp:
    def __init__(self, rows):
        self.rows = rows


class _StubClient:
    def __init__(self):
        self.hist_earnings_calls = 0

    def hist_earnings(self, ticker):
        self.hist_earnings_calls += 1
        return _FakeResp([])

    def hist_dailies(self, *a, **kw):
        return _FakeResp([])

    def hist_cores(self, *a, **kw):
        return _FakeResp([])

    def cores(self, *a, **kw):
        return _FakeResp([])

    def live_summaries(self, *a, **kw):
        return _FakeResp([])


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch):
    """Isolate the shared cache + make compute_breach_stats deterministic."""
    clear_shared_cache()
    reset_shared_cache_stats()

    def _fake_compute(**kw):
        return {
            "ticker": kw.get("ticker"),
            "events": [
                {"signedMovePct": r * 5.0, "impliedMovePct": 5.0}
                for r in [0.3, 0.5, 0.8, 0.4, 0.6]
            ],
            "current": {"stockPrice": 100.0, "impliedMovePct": 5.0, "asOfDate": "2026-04-21"},
            "nextEvent": {},
            "summary": {"events_used": 5, "events_found": 5},
            "regime": {"label": "Normal"},
        }

    monkeypatch.setattr(
        "backend.earnings_logic.compute_breach_stats", _fake_compute,
    )
    # Stub out the enrichment so we don't need benzinga / goNoGo clients.
    monkeypatch.setattr(
        "backend.engine1.shared_cache._enrich_payload",
        lambda **kw: kw["payload"],
    )
    yield
    clear_shared_cache()
    reset_shared_cache_stats()


def test_second_call_hits_cache():
    from backend.config import get_flags
    flags = get_flags()
    client = _StubClient()
    out1 = get_or_compute_breach_stats(
        ticker="NVDA", n=20, years=5, event_date="2026-05-28", event_timing="AMC",
        client=client, flags=flags,
    )
    out2 = get_or_compute_breach_stats(
        ticker="NVDA", n=20, years=5, event_date="2026-05-28", event_timing="AMC",
        client=client, flags=flags,
    )
    stats = get_shared_cache_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert out1 is out2


def test_different_event_dates_bust_cache():
    from backend.config import get_flags
    flags = get_flags()
    client = _StubClient()
    get_or_compute_breach_stats(
        ticker="NVDA", n=20, years=5, event_date="2026-05-28", event_timing="AMC",
        client=client, flags=flags,
    )
    get_or_compute_breach_stats(
        ticker="NVDA", n=20, years=5, event_date="2026-08-28", event_timing="AMC",
        client=client, flags=flags,
    )
    stats = get_shared_cache_stats()
    assert stats["misses"] == 2
    assert stats["hits"] == 0


def test_different_timing_busts_cache():
    from backend.config import get_flags
    flags = get_flags()
    client = _StubClient()
    get_or_compute_breach_stats(
        ticker="NVDA", n=20, years=5, event_date="2026-05-28", event_timing="AMC",
        client=client, flags=flags,
    )
    get_or_compute_breach_stats(
        ticker="NVDA", n=20, years=5, event_date="2026-05-28", event_timing="BMO",
        client=client, flags=flags,
    )
    stats = get_shared_cache_stats()
    assert stats["misses"] == 2


def test_different_trade_builder_inputs_bust_cache():
    from backend.config import get_flags
    flags = get_flags()
    client = _StubClient()
    get_or_compute_breach_stats(
        ticker="NVDA", n=20, years=5, event_date="2026-05-28", event_timing="AMC",
        trade_builder_inputs={"wing_width": 5.0},
        client=client, flags=flags,
    )
    get_or_compute_breach_stats(
        ticker="NVDA", n=20, years=5, event_date="2026-05-28", event_timing="AMC",
        trade_builder_inputs={"wing_width": 10.0},
        client=client, flags=flags,
    )
    stats = get_shared_cache_stats()
    assert stats["misses"] == 2


def test_force_refresh_bypasses_cache():
    from backend.config import get_flags
    flags = get_flags()
    client = _StubClient()
    get_or_compute_breach_stats(
        ticker="NVDA", n=20, years=5, event_date="2026-05-28", event_timing="AMC",
        client=client, flags=flags,
    )
    get_or_compute_breach_stats(
        ticker="NVDA", n=20, years=5, event_date="2026-05-28", event_timing="AMC",
        client=client, flags=flags, force_refresh=True,
    )
    stats = get_shared_cache_stats()
    assert stats["misses"] == 2


def test_e15_simulator_uses_shared_cache(monkeypatch):
    """Simulator's _run_engine1 is now a thin wrapper — prove it routes
    through the shared cache."""
    from backend.config import get_flags
    from backend.engine15 import simulator as e15_sim
    flags = get_flags()
    client = _StubClient()
    e15_sim._run_engine1(
        ticker="NVDA", n=20, years=5,
        client=client, benzinga_client=None, flags=flags,
        event_date="2026-05-28", event_timing="AMC",
    )
    stats = get_shared_cache_stats()
    assert stats["misses"] == 1
    # And a second call with same key -> hit.
    e15_sim._run_engine1(
        ticker="NVDA", n=20, years=5,
        client=client, benzinga_client=None, flags=flags,
        event_date="2026-05-28", event_timing="AMC",
    )
    stats = get_shared_cache_stats()
    assert stats["hits"] == 1
