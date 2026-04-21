"""Engine 15 v2 — Wing Console handoff tests.

When the scenario body carries ``wingConsoleCacheKey`` + ``placementRank``,
the router's ``_hydrate_from_wing_console`` pre-processor fills missing
strikes + creditReceived from the cached E1 :class:`ScoringContext` for
the same ticker + event.
"""
from __future__ import annotations

import pytest

from backend.engine1 import (
    MAEDistribution, ScoringContext, WingConsoleWeights,
    clear_shared_cache, store_scoring_context,
)
from backend.routers import engine15_earnings_ic as mod


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    from backend.engine1.wing_console import _scoring_ctx_cache, _scoring_ctx_lock
    clear_shared_cache()
    with _scoring_ctx_lock:
        _scoring_ctx_cache.clear()
    # Publish a fresh scoring context every test.
    ctx = ScoringContext(
        ticker="NVDA", event_date="2026-05-28", event_timing="AMC",
        spot=200.0, implied_move_pct=5.0,
        events=[{"signedMovePct": r * 5.0, "impliedMovePct": 5.0}
                for r in [0.3, 0.5, 0.7, 0.4, 0.6, 0.8, 1.1]],
        mae=MAEDistribution(n=5, p50=3, p75=5, p90=7, p95=9, max=10,
                            source="daily_ohlc_proxy"),
        theta=None,
        median_credit_pts=1.2,
        weights=WingConsoleWeights(),
    )
    store_scoring_context(ctx)
    yield
    # Teardown: clear the scoring context cache so subsequent test
    # files don't inherit our NVDA stub state.
    with _scoring_ctx_lock:
        _scoring_ctx_cache.clear()
    clear_shared_cache()


def test_handoff_fills_missing_strikes():
    body = {
        "ticker": "NVDA",
        "earningsDate": "2026-05-28",
        "earningsTiming": "AMC",
        "wingConsoleCacheKey": "any-non-empty",
        "placementRank": 0,
    }
    out = mod._hydrate_from_wing_console(body)
    assert out["shortPut"] > 0
    assert out["longPut"] < out["shortPut"]
    assert out["shortCall"] > out["shortPut"]
    assert out["longCall"] > out["shortCall"]
    assert out["creditReceived"] > 0
    assert "_wingConsoleHandoff" in out
    meta = out["_wingConsoleHandoff"]
    assert meta["placementRank"] == 0
    assert meta["compositeScore"] > 0


def test_handoff_respects_user_strikes():
    body = {
        "ticker": "NVDA",
        "earningsDate": "2026-05-28",
        "earningsTiming": "AMC",
        "wingConsoleCacheKey": "any-non-empty",
        "shortPut": 195.0,
        "longPut": 190.0,
        "shortCall": 205.0,
        "longCall": 210.0,
        "creditReceived": 2.0,
    }
    out = mod._hydrate_from_wing_console(body)
    # User-supplied values survive — the hydrator only fills gaps.
    assert out["shortPut"] == 195.0
    assert out["longPut"] == 190.0
    assert out["shortCall"] == 205.0
    assert out["longCall"] == 210.0
    assert out["creditReceived"] == 2.0


def test_handoff_alternate_rank():
    body_rank0 = {
        "ticker": "NVDA", "earningsDate": "2026-05-28", "earningsTiming": "AMC",
        "wingConsoleCacheKey": "x", "placementRank": 0,
    }
    # Pick a deeper rank so the grid has genuinely different placements.
    body_rank5 = {
        "ticker": "NVDA", "earningsDate": "2026-05-28", "earningsTiming": "AMC",
        "wingConsoleCacheKey": "x", "placementRank": 5,
    }
    out0 = mod._hydrate_from_wing_console(body_rank0)
    out5 = mod._hydrate_from_wing_console(body_rank5)
    meta0 = out0["_wingConsoleHandoff"]
    meta5 = out5["_wingConsoleHandoff"]
    # Ranks are distinct ints; scoring context guarantees >=6 placements.
    assert meta0["placementRank"] == 0
    assert meta5["placementRank"] == 5
    # At least one of EM / wings / composite differs between ranks.
    sig0 = (meta0["emMult"], meta0["wingPts"], round(meta0["compositeScore"], 2))
    sig5 = (meta5["emMult"], meta5["wingPts"], round(meta5["compositeScore"], 2))
    assert sig0 != sig5


def test_handoff_no_cache_key_is_no_op():
    body = {
        "ticker": "NVDA",
        "earningsDate": "2026-05-28",
        "earningsTiming": "AMC",
    }
    out = mod._hydrate_from_wing_console(body)
    # No strikes added.
    assert "shortPut" not in out
    assert "_wingConsoleHandoff" not in out


def test_handoff_missing_context_falls_through():
    clear_shared_cache()
    # scoring_context was published in the fixture; now clear scoring context
    # cache specifically
    from backend.engine1.wing_console import _scoring_ctx_cache, _scoring_ctx_lock
    with _scoring_ctx_lock:
        _scoring_ctx_cache.clear()
    body = {
        "ticker": "UNKNOWN",
        "earningsDate": "2026-05-28",
        "earningsTiming": "AMC",
        "wingConsoleCacheKey": "x",
    }
    out = mod._hydrate_from_wing_console(body)
    # Silent no-op — no strikes added and no handoff meta.
    assert "shortPut" not in out
    assert "_wingConsoleHandoff" not in out
