"""Engine 18 — router tests: flag gating, scan shape, tracker CRUD.

Redis is replaced with an in-memory fake; the heavy pipeline is never
invoked (the GET path reads the store, the tracker takes an explicit store).
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.engine18 import store as e18_store
from backend.engine18 import trades as e18_trades


class FakeStore:
    def __init__(self):
        self.data: Dict[str, Any] = {}

    def get_json(self, key):
        return self.data.get(key)

    def set_json(self, key, value, ttl_s=0):
        self.data[key] = value
        return True

    def scan_keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in self.data if k.startswith(prefix)]


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def fake_store(monkeypatch):
    fs = FakeStore()
    monkeypatch.setattr("backend.engine18.store._store", lambda: fs)
    monkeypatch.setattr("backend.engine18.trades.get_store_optional", lambda: fs)
    return fs


_SCAN = {
    "engine": 18,
    "asOf": "2026-06-09T12:45:00Z",
    "summary": {"candidates": 1, "actionable": 1, "fullSize": 1, "halfSize": 0},
    "candidates": [{
        "ticker": "BIGB",
        "bucket": "beat_large",
        "sizing": "full",
        "entry_date": "2026-06-09",
        "exit_date": "2026-06-23",
        "hold_days": 10,
        "report": {"ticker": "BIGB", "report_date": "2026-06-08", "surprise_pct": 0.30},
        "grade": {"score": 0.9, "quintile": "Q5", "source": "llm"},
        "expected": {"bucketAvgNetPct": 1.05},
    }],
    "meta": {},
    "validation": None,
}


# ---------------------------------------------------------------------------
# Flag gating
# ---------------------------------------------------------------------------


def test_flag_gating_404_when_disabled(client, monkeypatch):
    monkeypatch.setenv("ENABLE_ENGINE18", "0")
    assert client.get("/api/engine18").status_code == 404
    assert client.get("/api/engine18/status").status_code == 404
    assert client.get("/api/engine18/trades").status_code == 404
    assert client.post("/api/engine18/refresh").status_code == 404
    assert client.post("/api/engine18/profile", json={"ticker": "ORCL"}).status_code == 404
    assert client.get("/earnings-drift").status_code == 404


def test_page_served_when_enabled(client, monkeypatch):
    monkeypatch.setenv("ENABLE_ENGINE18", "1")
    r = client.get("/earnings-drift")
    assert r.status_code == 200
    assert "Earnings Drift" in r.text


# ---------------------------------------------------------------------------
# Scan GET
# ---------------------------------------------------------------------------


def test_get_scan_serves_cached_snapshot(client, fake_store, monkeypatch):
    monkeypatch.setenv("ENABLE_ENGINE18", "1")
    fake_store.data["e18:scan:latest"] = dict(_SCAN)
    r = client.get("/api/engine18")
    assert r.status_code == 200
    doc = r.json()
    assert doc["cached"] is True
    assert doc["summary"]["fullSize"] == 1
    assert doc["candidates"][0]["ticker"] == "BIGB"


def test_get_scan_attaches_validation(client, fake_store, monkeypatch):
    monkeypatch.setenv("ENABLE_ENGINE18", "1")
    fake_store.data["e18:scan:latest"] = dict(_SCAN)
    fake_store.data["e18:validation:latest"] = {"rolling6mAvgNetPct": -0.2, "degraded": True}
    doc = client.get("/api/engine18").json()
    assert doc["validation"]["degraded"] is True


def test_get_scan_empty_payload_when_nothing_stored(client, fake_store, monkeypatch):
    monkeypatch.setenv("ENABLE_ENGINE18", "1")
    doc = client.get("/api/engine18").json()
    assert doc["summary"]["candidates"] == 0
    assert "note" in doc


def test_status_endpoint(client, fake_store, monkeypatch):
    monkeypatch.setenv("ENABLE_ENGINE18", "1")
    fake_store.data["e18:last_run"] = {"ok": True, "candidates": 3}
    doc = client.get("/api/engine18/status").json()
    assert doc["engine"] == 18
    assert doc["lastRun"]["candidates"] == 3
    assert doc["backgroundRunning"] is False


def test_evidence_endpoint(client, fake_store, monkeypatch):
    monkeypatch.setenv("ENABLE_ENGINE18", "1")
    fake_store.data["e18:evidence:BIGB"] = {"report": {"ticker": "BIGB"}, "grade": {"score": 0.9}}
    doc = client.get("/api/engine18/evidence/bigb").json()
    assert doc["found"] is True
    assert doc["evidence"]["grade"]["score"] == 0.9
    assert client.get("/api/engine18/evidence/NOPE").json()["found"] is False


# ---------------------------------------------------------------------------
# Manual profile endpoint
# ---------------------------------------------------------------------------


def test_profile_requires_valid_ticker(client, monkeypatch):
    monkeypatch.setenv("ENABLE_ENGINE18", "1")
    assert client.post("/api/engine18/profile", json={}).status_code == 422
    assert client.post("/api/engine18/profile", json={"ticker": ""}).status_code == 422
    assert client.post("/api/engine18/profile", json={"ticker": "BAD TICKER!"}).status_code == 422
    assert client.post("/api/engine18/profile", json={"ticker": "WAYTOOLONGNAME"}).status_code == 422


def test_profile_happy_path(client, monkeypatch):
    monkeypatch.setenv("ENABLE_ENGINE18", "1")
    captured = {}

    def fake_build_profile(ticker, *, overrides=None, **kwargs):
        captured["ticker"] = ticker
        captured["overrides"] = overrides
        return {"found": True, "verdict": "candidate", "source": "manual",
                "candidate": {"ticker": ticker, "sizing": "full", "origin": "manual"}}

    monkeypatch.setattr("backend.engine18.pipeline.build_profile", fake_build_profile)
    r = client.post("/api/engine18/profile", json={
        "ticker": "orcl", "actual_eps": 2.1, "estimate_eps": 1.95,
        "report_date": "2026-06-10", "timing": "amc",
    })
    assert r.status_code == 200
    doc = r.json()
    assert doc["engine"] == 18
    assert doc["ticker"] == "ORCL"
    assert doc["verdict"] == "candidate"
    assert captured["ticker"] == "ORCL"
    assert captured["overrides"] == {
        "actual_eps": 2.1, "estimate_eps": 1.95,
        "report_date": "2026-06-10", "timing": "amc",
    }


def test_profile_passes_through_verdicts(client, monkeypatch):
    monkeypatch.setenv("ENABLE_ENGINE18", "1")
    monkeypatch.setattr(
        "backend.engine18.pipeline.build_profile",
        lambda ticker, **kw: {"found": False, "verdict": "no_report", "reason": "nothing in window"},
    )
    doc = client.post("/api/engine18/profile", json={"ticker": "GHOST"}).json()
    assert doc["found"] is False
    assert doc["verdict"] == "no_report"


# ---------------------------------------------------------------------------
# Tracker CRUD
# ---------------------------------------------------------------------------


def test_tracker_crud_roundtrip(client, fake_store, monkeypatch):
    monkeypatch.setenv("ENABLE_ENGINE18", "1")

    # Create.
    r = client.post("/api/engine18/trade", json={
        "ticker": "BIGB",
        "entryDate": "2026-06-09",
        "holdDays": 10,
        "entryPrice": 100.0,
        "sizing": "full",
        "signalSnapshot": _SCAN["candidates"][0],
    })
    assert r.status_code == 200
    trade_id = r.json()["tradeId"]
    assert trade_id.startswith("e18-")

    # List: planned exit auto-derived from entry + 10 trading days.
    doc = client.get("/api/engine18/trades").json()
    assert doc["count"] == 1
    t = doc["trades"][0]
    assert t["ticker"] == "BIGB"
    assert t["status"] == "active"
    assert t["plannedExitDate"] == "2026-06-23"

    # Check-in.
    r = client.post(f"/api/engine18/trade/{trade_id}/checkin", json={"note": "drifting nicely"})
    assert r.status_code == 200
    assert r.json()["checkIns"][0]["note"] == "drifting nicely"

    # Close with outcome math.
    r = client.post(f"/api/engine18/trade/{trade_id}/close", json={
        "exitPrice": 103.0, "exitDate": "2026-06-23",
    })
    assert r.status_code == 200
    closed = r.json()
    assert closed["status"] == "closed"
    assert abs(closed["outcome"]["returnPct"] - 0.03) < 1e-9

    # Status filters.
    assert client.get("/api/engine18/trades?status=active").json()["count"] == 0
    assert client.get("/api/engine18/trades?status=closed").json()["count"] == 1


def test_tracker_validation_errors(client, fake_store, monkeypatch):
    monkeypatch.setenv("ENABLE_ENGINE18", "1")
    assert client.post("/api/engine18/trade", json={}).status_code == 400
    assert client.post("/api/engine18/trade/nope/close", json={}).status_code == 404
    assert client.post("/api/engine18/trade/nope/checkin", json={}).status_code == 404


def test_tracker_index_rebuild(fake_store):
    trade_id = e18_trades.log_trade({"ticker": "BIGB", "entryPrice": 100.0}, store=fake_store)
    assert trade_id
    # Simulate index expiry, then rebuild from the surviving trade keys.
    del fake_store.data["e18:trades:index"]
    assert e18_trades.rebuild_index_if_missing(store=fake_store) is True
    assert fake_store.data["e18:trades:index"] == [trade_id]


def test_store_trailing_grades_window(fake_store):
    e18_store.append_trailing_grades([0.1, 0.2], max_len=3, ttl_s=60, store=fake_store)
    e18_store.append_trailing_grades([0.3, 0.4], max_len=3, ttl_s=60, store=fake_store)
    assert e18_store.get_trailing_grades(store=fake_store) == [0.2, 0.3, 0.4]
