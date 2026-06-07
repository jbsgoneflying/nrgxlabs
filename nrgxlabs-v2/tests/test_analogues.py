"""Tests for the Phase 1 module 2 analogue index (feature-space MVP).

Math layer:
- feature extraction with fallback paths
- z-score standardization with masked-NaN handling
- masked cosine similarity
- end-to-end build → search round-trip on a synthetic mini-corpus

Endpoint layer:
- POST /build with mocked v1 redis
- POST /search returns sorted neighbors with outcome summary
- GET /stats and /features
- legacy GET /search still works (back-compat)
"""

from __future__ import annotations

import json
import math
import os

import pytest
from fastapi.testclient import TestClient


from v2_app.foundation.analogues import (
    AnalogueIndex,
    build_index_from_v1_trades,
    extract_features,
    feature_names,
)
from v2_app.foundation.vectorspace import (
    Standardizer,
    masked_cosine as _masked_cosine,
)


# ── Feature extraction ────────────────────────────────────


def test_feature_names_per_engine() -> None:
    e1 = feature_names("e1")
    assert "vrpScore" in e1 and "breachPct" in e1 and "isAmc" in e1
    e2 = feature_names("e2")
    assert "regimeScore" in e2 and "breachPct" in e2
    assert feature_names("garbage") == []


def test_extract_uses_first_non_null_path() -> None:
    trade = {
        "entryContext": {"vrpScore": None, "breachPct": 22.0},
        "vrpSnapshot": {"vrpScore": 1.4, "ivRank": 78.0},
        "entry": {"emMultiple": 1.5, "daysToExpiry": 1, "timing": "AMC"},
    }
    feats = extract_features(trade, "e1")
    assert feats["vrpScore"] == 1.4
    assert feats["breachPct"] == 22.0
    assert feats["emMultiple"] == 1.5
    assert feats["isAmc"] == 1.0


def test_extract_handles_bmo_categorical() -> None:
    trade = {"entry": {"timing": "BMO"}}
    feats = extract_features(trade, "e1")
    assert feats["isAmc"] == 0.0


def test_extract_returns_none_for_missing_features() -> None:
    feats = extract_features({}, "e1")
    assert all(v is None for v in feats.values())


def test_extract_skips_nan_inf() -> None:
    trade = {"entryContext": {"vrpScore": float("nan")},
             "vrpSnapshot": {"vrpScore": float("inf")}}
    feats = extract_features(trade, "e1")
    assert feats["vrpScore"] is None


# ── Standardizer ──────────────────────────────────────────


def test_standardizer_fits_and_transforms() -> None:
    rows = [
        {"a": 10.0, "b": 100.0},
        {"a": 20.0, "b": 200.0},
        {"a": 30.0, "b": 300.0},
    ]
    s = Standardizer()
    s.fit(rows)
    assert s.means["a"] == pytest.approx(20.0)
    assert s.stds["a"] > 0

    z, mask, names = s.transform({"a": 20.0, "b": 200.0})
    # The mean row z-scores to (0, 0).
    assert z[names.index("a")] == pytest.approx(0.0)
    assert z[names.index("b")] == pytest.approx(0.0)
    assert mask == [1, 1]


def test_standardizer_handles_missing_at_transform() -> None:
    rows = [{"a": 1.0}, {"a": 2.0}, {"a": 3.0}]
    s = Standardizer()
    s.fit(rows)
    z, mask, _ = s.transform({"a": None})
    assert z == [0.0]
    assert mask == [0]


def test_standardizer_handles_zero_variance() -> None:
    rows = [{"a": 5.0}, {"a": 5.0}, {"a": 5.0}]
    s = Standardizer()
    s.fit(rows)
    # std forced to 1.0 so transform doesn't divide by zero.
    z, _, _ = s.transform({"a": 5.0})
    assert z == [0.0]


def test_standardizer_round_trip_through_json() -> None:
    s = Standardizer(means={"a": 1.5, "b": 2.0}, stds={"a": 0.5, "b": 1.0})
    s2 = Standardizer.from_json(s.to_json())
    assert s2.means == s.means and s2.stds == s.stds


# ── Cosine similarity ─────────────────────────────────────


def test_masked_cosine_basic() -> None:
    a = [1.0, 0.0]
    b = [1.0, 0.0]
    assert _masked_cosine(a, [1, 1], b, [1, 1]) == pytest.approx(1.0)
    assert _masked_cosine(a, [1, 1], [-1.0, 0.0], [1, 1]) == pytest.approx(-1.0)
    assert _masked_cosine(a, [1, 1], [0.0, 1.0], [1, 1]) == pytest.approx(0.0)


def test_masked_cosine_ignores_masked_dims() -> None:
    """If a dim is masked on either side, it must not contribute to the score."""
    a = [3.0, 100.0]
    b = [3.0, -100.0]
    # Without masking, vectors near-orthogonal because dim 1 dominates.
    raw = _masked_cosine(a, [1, 1], b, [1, 1])
    # With dim 1 masked on the query side, only dim 0 contributes → score = 1.0.
    masked = _masked_cosine(a, [1, 0], b, [1, 1])
    assert masked == pytest.approx(1.0)
    assert raw < 0.5


def test_masked_cosine_returns_zero_when_no_overlap() -> None:
    assert _masked_cosine([1.0, 1.0], [1, 0], [1.0, 1.0], [0, 1]) == 0.0
    assert _masked_cosine([], [], [], []) == 0.0


# ── End-to-end build + search ────────────────────────────


def _make_trade(
    *,
    tid: str,
    ticker: str,
    vrp: float,
    breach: float,
    em: float,
    iv: float = 70.0,
    timing: str = "AMC",
    outcome: str = "win",
    pnl: float = 50.0,
) -> dict:
    return {
        "tradeId": tid,
        "ticker": ticker,
        "status": "closed",
        "closedAt": "2026-04-01T20:00:00Z",
        "entry": {"emMultiple": em, "daysToExpiry": 1, "timing": timing, "underlying": ticker},
        "entryContext": {"vrpScore": vrp, "breachPct": breach, "ivRank": iv},
        "vrpSnapshot": {"ivRank": iv, "ivPercentile": iv},
        "marketSnapshot": {"vix": 16.0},
        "outcome": {
            "outcomeClass": outcome,
            "realizedPnl": pnl,
            "holdDurationDays": 1,
            "maxBreachProximity": 60.0,
        },
    }


def test_build_index_from_synthetic_corpus() -> None:
    trades = [
        _make_trade(tid="t1", ticker="AAPL", vrp=1.5, breach=18, em=1.0, outcome="win",  pnl=100),
        _make_trade(tid="t2", ticker="NVDA", vrp=1.4, breach=20, em=1.0, outcome="win",  pnl=80),
        _make_trade(tid="t3", ticker="META", vrp=1.6, breach=15, em=1.0, outcome="loss", pnl=-200),
        _make_trade(tid="t4", ticker="GOOG", vrp=0.8, breach=45, em=1.5, outcome="loss", pnl=-150),
        _make_trade(tid="t5", ticker="TSLA", vrp=0.7, breach=50, em=1.5, outcome="loss", pnl=-180),
        _make_trade(tid="t6", ticker="MSFT", vrp=0.9, breach=42, em=1.5, outcome="scratch", pnl=0),
    ]
    idx, stats = build_index_from_v1_trades(engine="e1", trades=trades)

    assert stats["ok"] is True
    assert stats["n_trades_seen"] == 6
    assert stats["n_indexed"] == 6
    assert stats["feature_coverage"]["vrpScore"] == 1.0
    assert idx.n_indexed == 6


def test_search_returns_setups_in_same_cluster_first() -> None:
    """The two synthetic clusters should be retrieved correctly:
    - 'high vrp / low breach / em=1.0' bucket (winners)
    - 'low vrp / high breach / em=1.5' bucket (losers)
    A query at the center of the winners cluster must return the winners
    ahead of the losers."""
    trades = [
        _make_trade(tid="t1", ticker="AAPL", vrp=1.5, breach=18, em=1.0, outcome="win",  pnl=100),
        _make_trade(tid="t2", ticker="NVDA", vrp=1.4, breach=20, em=1.0, outcome="win",  pnl=80),
        _make_trade(tid="t3", ticker="META", vrp=1.6, breach=15, em=1.0, outcome="win",  pnl=60),
        _make_trade(tid="t4", ticker="GOOG", vrp=0.8, breach=45, em=1.5, outcome="loss", pnl=-150),
        _make_trade(tid="t5", ticker="TSLA", vrp=0.7, breach=50, em=1.5, outcome="loss", pnl=-180),
        _make_trade(tid="t6", ticker="MSFT", vrp=0.9, breach=42, em=1.5, outcome="loss", pnl=-200),
    ]
    idx, _ = build_index_from_v1_trades(engine="e1", trades=trades)

    # Query mirrors the winners cluster centroid.
    query = {"vrpScore": 1.5, "breachPct": 18.0, "emMultiple": 1.0,
             "ivRank": 70.0, "ivPercentile": 70.0,
             "daysToExpiry": 1, "vix": 16.0, "isAmc": 1.0}
    neighbors = idx.search(query, k=3)
    assert len(neighbors) == 3
    # All three top neighbors must come from the winners cluster.
    top_tickers = {n["ticker"] for n in neighbors}
    assert top_tickers == {"AAPL", "NVDA", "META"}


def test_search_ticker_exclude() -> None:
    trades = [
        _make_trade(tid="t1", ticker="AAPL", vrp=1.5, breach=18, em=1.0),
        _make_trade(tid="t2", ticker="NVDA", vrp=1.5, breach=18, em=1.0),
    ]
    idx, _ = build_index_from_v1_trades(engine="e1", trades=trades)
    query = {"vrpScore": 1.5, "breachPct": 18.0, "emMultiple": 1.0}
    n = idx.search(query, k=5, ticker_exclude="AAPL")
    assert all(x["ticker"] != "AAPL" for x in n)


def test_outcome_summary_aggregates_correctly() -> None:
    idx = AnalogueIndex(engine="e1", standardizer=Standardizer(), feature_names=[])
    neighbors = [
        {"outcome": {"outcomeClass": "win",  "realizedPnl": 100}},
        {"outcome": {"outcomeClass": "win",  "realizedPnl": 60}},
        {"outcome": {"outcomeClass": "loss", "realizedPnl": -200}},
        {"outcome": {"outcomeClass": "scratch", "realizedPnl": 0}},
    ]
    s = idx.outcome_summary(neighbors)
    assert s["n"] == 4 and s["wins"] == 2 and s["losses"] == 1 and s["scratches"] == 1
    assert s["win_rate"] == pytest.approx(2 / 3)  # decisive only
    assert s["avg_pnl"] == pytest.approx(-10.0)  # (100+60-200+0)/4


def test_index_skips_unclosed_or_outcomeless() -> None:
    trades = [
        _make_trade(tid="closed1", ticker="AAPL", vrp=1.5, breach=18, em=1.0),
        {**_make_trade(tid="active1", ticker="NVDA", vrp=1.4, breach=20, em=1.0),
         "status": "active"},
        {**_make_trade(tid="no_outcome", ticker="META", vrp=1.6, breach=15, em=1.0),
         "outcome": None},
    ]
    idx, stats = build_index_from_v1_trades(engine="e1", trades=trades)
    assert stats["n_indexed"] == 1
    assert stats["skipped"]["not_closed"] == 1
    assert stats["skipped"]["no_outcome"] == 1


def test_index_round_trip_through_json() -> None:
    trades = [
        _make_trade(tid=f"t{i}", ticker="AAPL", vrp=1.0 + 0.1 * i, breach=20 + i, em=1.0)
        for i in range(5)
    ]
    idx, _ = build_index_from_v1_trades(engine="e1", trades=trades)
    blob = idx.to_json()
    idx2 = AnalogueIndex.from_json(blob)
    assert idx2 is not None
    assert idx2.n_indexed == idx.n_indexed
    # Searching on either index gives identical top-1.
    q = {"vrpScore": 1.0, "breachPct": 20.0, "emMultiple": 1.0}
    a = idx.search(q, k=1)
    b = idx2.search(q, k=1)
    assert a[0]["trade_id"] == b[0]["trade_id"]
    assert math.isclose(a[0]["similarity"], b[0]["similarity"], abs_tol=1e-6)


# ── Endpoint contracts ────────────────────────────────────


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value):
        self.kv[key] = value if isinstance(value, str) else json.dumps(value)


def _seed_e1_corpus(fake: FakeRedis, trades: list[dict]) -> None:
    ids: list[str] = []
    for t in trades:
        tid = t["tradeId"]
        ids.append(tid)
        fake.set(f"e1:trades:{tid}", json.dumps(t))
    fake.set("e1:trades:index", json.dumps(ids))


@pytest.fixture(scope="module")
def client() -> TestClient:
    os.environ["PUBLIC_ACCESS"] = "1"
    os.environ.setdefault("AUTH_SECRET", "test-secret-not-real")
    from v2_app.main import app
    return TestClient(app)


def test_endpoint_features(client: TestClient) -> None:
    res = client.get("/api/v2/analogues/features", params={"engine": "e1"})
    assert res.status_code == 200
    assert "vrpScore" in res.json()["feature_names"]


def test_endpoint_build_then_search(client: TestClient, monkeypatch) -> None:
    fake = FakeRedis()
    fake.set("e2:trades:index", json.dumps([]))  # E2 has no journal
    trades = [
        _make_trade(tid="t1", ticker="AAPL", vrp=1.5, breach=18, em=1.0, outcome="win",  pnl=100),
        _make_trade(tid="t2", ticker="NVDA", vrp=1.4, breach=20, em=1.0, outcome="win",  pnl=80),
        _make_trade(tid="t3", ticker="META", vrp=1.6, breach=15, em=1.0, outcome="win",  pnl=60),
        _make_trade(tid="t4", ticker="GOOG", vrp=0.8, breach=45, em=1.5, outcome="loss", pnl=-150),
        _make_trade(tid="t5", ticker="TSLA", vrp=0.7, breach=50, em=1.5, outcome="loss", pnl=-180),
    ]
    _seed_e1_corpus(fake, trades)

    from v2_app.foundation import analogues_store
    monkeypatch.setattr(analogues_store, "_redis_client", lambda: fake)

    # 1. Build the index.
    res = client.post("/api/v2/analogues/build", json={"engine": "e1"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["n_indexed"] == 5
    assert body["persisted"] is True

    # 2. Search for analogues to a winners-cluster query.
    q = {"vrpScore": 1.5, "breachPct": 18.0, "emMultiple": 1.0,
         "ivRank": 70.0, "ivPercentile": 70.0,
         "daysToExpiry": 1, "vix": 16.0, "isAmc": 1.0}
    res = client.post("/api/v2/analogues/search", json={"engine": "e1", "query": q, "k": 3})
    assert res.status_code == 200
    body = res.json()
    assert body["k_returned"] == 3
    assert body["n_indexed"] == 5
    # Top-3 must be the three winners cluster trades.
    top_tickers = {n["ticker"] for n in body["neighbors"]}
    assert top_tickers == {"AAPL", "NVDA", "META"}
    # Outcome summary aggregates over the returned neighbors only.
    assert body["outcome_summary"]["wins"] == 3
    assert body["outcome_summary"]["losses"] == 0
    assert body["outcome_summary"]["win_rate"] == 1.0


def test_endpoint_search_without_index(client: TestClient, monkeypatch) -> None:
    """When no index has been built, search returns an empty result with a
    helpful message rather than 500."""
    from v2_app.foundation import analogues_store
    monkeypatch.setattr(analogues_store, "_redis_client", lambda: FakeRedis())
    res = client.post("/api/v2/analogues/search", json={"engine": "e2", "query": {"breachPct": 20.0}})
    assert res.status_code == 200
    body = res.json()
    assert body["n_indexed"] == 0
    assert body["neighbors"] == []
    assert "no index built yet" in body["message"]


def test_endpoint_unknown_engine_400(client: TestClient) -> None:
    res = client.post("/api/v2/analogues/build", json={"engine": "exotic"})
    assert res.status_code == 400


def test_endpoint_stats(client: TestClient, monkeypatch) -> None:
    fake = FakeRedis()
    fake.set("e1:trades:index", json.dumps([]))
    fake.set("e2:trades:index", json.dumps([]))
    from v2_app.foundation import analogues_store
    monkeypatch.setattr(analogues_store, "_redis_client", lambda: fake)
    res = client.get("/api/v2/analogues/stats")
    assert res.status_code == 200
    body = res.json()
    assert body["engines_supported"] == ["e1", "e2"]
    assert isinstance(body["indexes"], list)


def test_legacy_get_search_still_works(client: TestClient) -> None:
    res = client.get("/api/v2/analogues/search", params={"ticker": "NVDA", "k": 5})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] in ("phase1_mvp_active", "stub")
    assert body["query"]["ticker"] == "NVDA"
