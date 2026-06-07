"""Tests for the v1→v2 conformal calibration mirror."""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient


# ── Fake Redis with the exact v1 schema we mirror ──


class FakeRedis:
    """In-memory stand-in for redis.from_url(...).

    Implements only the subset the mirror touches: ``get``, ``set``, plus the
    ``sadd`` / ``smembers`` that conformal_store uses for its index.
    """

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value):
        self.kv[key] = value if isinstance(value, str) else json.dumps(value)

    def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    def smembers(self, key):
        return set(self.sets.get(key, set()))


def _seed_e1_trades(fake: FakeRedis) -> list[str]:
    """Seed five E1 closed trades + one active that should be skipped."""
    trades = [
        # (id, breachPct (0-100), outcomeClass)
        ("AAPL_20260201_AMC_001", 18.0, "win"),
        ("NVDA_20260205_AMC_002", 32.0, "loss"),
        ("META_20260208_AMC_003", 22.0, "win"),
        ("GOOG_20260210_AMC_004", 41.0, "loss"),
        ("TSLA_20260215_AMC_005", 14.0, "scratch"),
    ]
    ids: list[str] = []
    for tid, bp, oc in trades:
        ids.append(tid)
        fake.set(
            f"e1:trades:{tid}",
            json.dumps(
                {
                    "tradeId": tid,
                    "status": "closed",
                    "loggedAt": "2026-02-01T13:30:00Z",
                    "closedAt": "2026-02-02T20:00:00Z",
                    "entry": {"underlying": tid.split("_")[0]},
                    "entryContext": {"breachPct": bp, "vrpScore": 1.4},
                    "outcome": {
                        "outcomeClass": oc,
                        "realizedPnl": -100.0 if oc == "loss" else (50.0 if oc == "win" else 0.0),
                        "maxBreachProximity": 100.0 if oc == "loss" else 60.0,
                    },
                }
            ),
        )

    # An active trade — must be skipped.
    fake.set(
        "e1:trades:AAPL_20260301_AMC_active",
        json.dumps(
            {
                "tradeId": "AAPL_20260301_AMC_active",
                "status": "active",
                "entryContext": {"breachPct": 25.0},
                "outcome": None,
            }
        ),
    )
    ids.append("AAPL_20260301_AMC_active")

    fake.set("e1:trades:index", json.dumps(ids))
    return ids


def _seed_e2_trades(fake: FakeRedis) -> list[str]:
    """Seed three E2 closed trades."""
    trades = [
        ("SPX_20260205_001", 12.0, "win"),
        ("SPX_20260207_002", 28.0, "loss"),
        ("SPY_20260209_003", 19.0, "win"),
    ]
    ids: list[str] = []
    for tid, bp, oc in trades:
        ids.append(tid)
        fake.set(
            f"e2:trades:{tid}",
            json.dumps(
                {
                    "tradeId": tid,
                    "status": "closed",
                    "loggedAt": "2026-02-05T13:30:00Z",
                    "closedAt": "2026-02-06T20:00:00Z",
                    "entry": {"underlying": tid.split("_")[0]},
                    "entryContext": {"breachPct": bp},
                    "outcome": {
                        "outcomeClass": oc,
                        "realizedPnl": -200.0 if oc == "loss" else 80.0,
                    },
                }
            ),
        )
    fake.set("e2:trades:index", json.dumps(ids))
    return ids


# ── Pure mirror tests ──


def test_mirror_replays_closed_trades(monkeypatch) -> None:
    fake = FakeRedis()
    _seed_e1_trades(fake)
    _seed_e2_trades(fake)

    from v2_app.foundation import conformal_store, v1_mirror

    monkeypatch.setattr(conformal_store, "_redis_client", lambda: fake)
    monkeypatch.setattr(v1_mirror, "_redis_client", lambda: fake)

    summary = v1_mirror.mirror_v1_breach_probability(reset=True)

    assert summary["ok"] is True
    assert summary["redis_available"] is True
    assert summary["metric"] == "breach_probability"
    assert summary["reset"] is True
    assert summary["n_trades_seen"] == 6 + 3
    assert summary["n_observations_logged"] == 5 + 3

    e1 = summary["engines"]["e1"]
    assert e1["n_closed"] == 5
    assert e1["n_observations_logged"] == 5
    assert e1["skips"]["not_closed"] == 1
    assert e1["final_n_calibration"] == 5

    e2 = summary["engines"]["e2"]
    assert e2["n_observations_logged"] == 3
    assert e2["final_n_calibration"] == 3


def test_mirror_handles_missing_redis(monkeypatch) -> None:
    from v2_app.foundation import v1_mirror
    monkeypatch.setattr(v1_mirror, "_redis_client", lambda: None)
    summary = v1_mirror.mirror_v1_breach_probability()
    assert summary["ok"] is False
    assert summary["redis_available"] is False
    assert summary["engines"] == {}


def test_mirror_only_engine_filter(monkeypatch) -> None:
    fake = FakeRedis()
    _seed_e1_trades(fake)
    _seed_e2_trades(fake)
    from v2_app.foundation import conformal_store, v1_mirror
    monkeypatch.setattr(conformal_store, "_redis_client", lambda: fake)
    monkeypatch.setattr(v1_mirror, "_redis_client", lambda: fake)

    summary = v1_mirror.mirror_v1_breach_probability(only_engine="e2")
    assert "e2" in summary["engines"] and "e1" not in summary["engines"]
    assert summary["n_observations_logged"] == 3


def test_mirror_skips_malformed_predictions(monkeypatch) -> None:
    fake = FakeRedis()
    bad_ids = ["bad_pct", "string_pct", "no_outcome", "out_of_range"]
    fake.set(
        "e1:trades:bad_pct",
        json.dumps({"status": "closed", "entryContext": {"breachPct": None},
                    "outcome": {"outcomeClass": "loss"}}),
    )
    fake.set(
        "e1:trades:string_pct",
        json.dumps({"status": "closed", "entryContext": {"breachPct": "n/a"},
                    "outcome": {"outcomeClass": "loss"}}),
    )
    fake.set(
        "e1:trades:no_outcome",
        json.dumps({"status": "closed", "entryContext": {"breachPct": 25},
                    "outcome": {"outcomeClass": "pending"}}),
    )
    fake.set(
        "e1:trades:out_of_range",
        json.dumps({"status": "closed", "entryContext": {"breachPct": 250},
                    "outcome": {"outcomeClass": "loss"}}),
    )
    fake.set("e1:trades:index", json.dumps(bad_ids))
    fake.set("e2:trades:index", json.dumps([]))

    from v2_app.foundation import conformal_store, v1_mirror
    monkeypatch.setattr(conformal_store, "_redis_client", lambda: fake)
    monkeypatch.setattr(v1_mirror, "_redis_client", lambda: fake)

    summary = v1_mirror.mirror_v1_breach_probability()
    e1 = summary["engines"]["e1"]
    assert e1["n_observations_logged"] == 0
    # The extractor tries every known path; trades that have *no* valid value
    # in any path land in no_breach_prediction. The out-of-range trade also
    # ends up there because the extractor falls through bad values rather
    # than bailing — there's just no other path for that fixture.
    assert e1["skips"]["no_breach_prediction"] == 3
    assert e1["skips"]["no_outcome_class"] == 1
    assert e1["skips"]["out_of_range_prediction"] == 0


def test_mirror_reset_replaces_existing_state(monkeypatch) -> None:
    fake = FakeRedis()
    _seed_e1_trades(fake)
    fake.set("e2:trades:index", json.dumps([]))

    from v2_app.foundation import conformal_store, v1_mirror
    monkeypatch.setattr(conformal_store, "_redis_client", lambda: fake)
    monkeypatch.setattr(v1_mirror, "_redis_client", lambda: fake)

    s1 = v1_mirror.mirror_v1_breach_probability(reset=True)
    assert s1["engines"]["e1"]["final_n_calibration"] == 5

    # Re-run with reset=True — should still land at 5, not 10.
    s2 = v1_mirror.mirror_v1_breach_probability(reset=True)
    assert s2["engines"]["e1"]["final_n_calibration"] == 5

    # Re-run with reset=False — appends, lands at 10.
    s3 = v1_mirror.mirror_v1_breach_probability(reset=False)
    assert s3["engines"]["e1"]["final_n_calibration"] == 10


# ── Endpoint contract ──


@pytest.fixture(scope="module")
def client() -> TestClient:
    os.environ["PUBLIC_ACCESS"] = "1"
    os.environ.setdefault("AUTH_SECRET", "test-secret-not-real")
    from v2_app.main import app
    return TestClient(app)


def test_mirror_endpoint(client: TestClient, monkeypatch) -> None:
    fake = FakeRedis()
    _seed_e1_trades(fake)
    _seed_e2_trades(fake)
    from v2_app.foundation import conformal_store, v1_mirror
    monkeypatch.setattr(conformal_store, "_redis_client", lambda: fake)
    monkeypatch.setattr(v1_mirror, "_redis_client", lambda: fake)

    res = client.post("/api/v2/conformal/mirror", json={"reset": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["n_observations_logged"] == 8
    assert set(body["engines"]) == {"e1", "e2"}

    # And the calibrators should now be reachable through /list.
    lst = client.get("/api/v2/conformal/list").json()
    seen = {(c["engine"], c["metric"]) for c in lst["calibrators"]}
    assert ("e1", "breach_probability") in seen
    assert ("e2", "breach_probability") in seen


def test_mirror_extracts_from_fallback_paths(monkeypatch) -> None:
    """When entryContext.breachPct is null, fall back to breachSnapshot etc."""
    fake = FakeRedis()
    ids = ["fb_breach_snapshot", "fb_em_summary", "fb_entry_em", "no_pred"]

    fake.set("e1:trades:fb_breach_snapshot", json.dumps({
        "status": "closed",
        "entryContext": {"breachPct": None},
        "breachSnapshot": {"breachRatePct": 22.0},
        "outcome": {"outcomeClass": "win"},
    }))
    fake.set("e1:trades:fb_em_summary", json.dumps({
        "status": "closed",
        "entryContext": {"breachPct": None, "emBreachSummary": {"1.0": 28.0, "1.5": 14.0, "2.0": 8.0}},
        "outcome": {"outcomeClass": "loss"},
    }))
    fake.set("e1:trades:fb_entry_em", json.dumps({
        "status": "closed",
        "entryContext": {"breachPct": None},
        "entry": {"emBreachPct": 17.5},
        "outcome": {"outcomeClass": "scratch"},
    }))
    fake.set("e1:trades:no_pred", json.dumps({
        "status": "closed",
        "entryContext": {"breachPct": None},
        "outcome": {"outcomeClass": "win"},
    }))
    fake.set("e1:trades:index", json.dumps(ids))
    fake.set("e2:trades:index", json.dumps([]))

    from v2_app.foundation import conformal_store, v1_mirror
    monkeypatch.setattr(conformal_store, "_redis_client", lambda: fake)
    monkeypatch.setattr(v1_mirror, "_redis_client", lambda: fake)

    summary = v1_mirror.mirror_v1_breach_probability()
    e1 = summary["engines"]["e1"]
    assert e1["n_observations_logged"] == 3
    assert e1["skips"]["no_breach_prediction"] == 1
    # Sources audit: each fallback path used exactly once.
    assert e1["sources"]["breachSnapshot.breachRatePct"] == 1
    assert e1["sources"]["entryContext.emBreachSummary[min]"] == 1
    assert e1["sources"]["entry.emBreachPct"] == 1


def test_extract_helper_priority_order() -> None:
    from v2_app.foundation.v1_mirror import _extract_breach_prediction
    # entryContext.breachPct wins when present — even if breachSnapshot has a value.
    doc = {
        "entryContext": {"breachPct": 25.0},
        "breachSnapshot": {"breachRatePct": 99.0},
    }
    pred, src = _extract_breach_prediction(doc)
    assert pred == pytest.approx(0.25)
    assert src == "entryContext.breachPct"

    # Out-of-range values are skipped and we fall through to the next.
    doc = {
        "entryContext": {"breachPct": 250.0},
        "breachSnapshot": {"breachRatePct": 18.0},
    }
    pred, src = _extract_breach_prediction(doc)
    assert pred == pytest.approx(0.18)
    assert src == "breachSnapshot.breachRatePct"

    # All-null returns (None, "none").
    pred, src = _extract_breach_prediction({"entryContext": {}})
    assert pred is None and src == "none"


def test_mirror_endpoint_only_engine(client: TestClient, monkeypatch) -> None:
    fake = FakeRedis()
    _seed_e1_trades(fake)
    _seed_e2_trades(fake)
    from v2_app.foundation import conformal_store, v1_mirror
    monkeypatch.setattr(conformal_store, "_redis_client", lambda: fake)
    monkeypatch.setattr(v1_mirror, "_redis_client", lambda: fake)

    res = client.post("/api/v2/conformal/mirror", json={"only_engine": "e1"})
    assert res.status_code == 200
    body = res.json()
    assert "e1" in body["engines"]
    assert "e2" not in body["engines"]
