"""Engine 14 v2 — /api/ic-scenario response shape (v2 additions)."""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from backend.app import app
    return TestClient(app)


def _stub_run_scenario(*args, **kwargs):
    # Return a minimally shaped scenario + populated v2 overlay.
    return {
        "engine": 14,
        "version": "2.0.0",
        "request": {
            "underlying": "SPX",
            "entry_date": "2026-01-05",
            "expiry": "2026-01-10",
            "short_put": 4900, "long_put": 4895,
            "short_call": 5100, "long_call": 5105,
            "credit_received": 1.5,
        },
        "analoguesUsed": 30,
        "analoguesConsidered": 40,
        "entryState": {
            "userSpot": 5000.0, "userEmPct": 1.5, "wingWidth": 5.0,
            "regimeBucket": "MODERATE", "regimeSource": "mi_v2_hmm",
            "regimeScore": 72.0,
            "regimeMiV2": {
                "label": "Transitional",
                "probabilities": {"Transitional": 0.72},
                "vol_state": "stable", "source": "v2_hmm",
            },
        },
        "regime": {
            "label": "MODERATE", "bucket": "MODERATE", "source": "mi_v2_hmm",
            "mi_v2": {
                "label": "Transitional",
                "probabilities": {"Transitional": 0.72},
                "vol_state": "stable", "source": "v2_hmm",
            },
        },
        "outcomeDistribution": {
            "fullCollect": {"pct": 45.0}, "earlyTarget": {"pct": 25.0},
            "whiteKnuckle": {"pct": 10.0}, "stopOut": {"pct": 10.0},
            "breach": {"pct": 10.0},
        },
        "reconcile": {"overall": "PASS", "chips": []},
        "engine2":   {"deskConsensus": {"verdict": "TRADE"}, "recSimple": "TRADE"},
        "expectedValue": {"meanPnlPct": 15.0, "medianPnlPct": 12.0, "sharpeProxy": 1.2},
    }


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(
        "backend.routers.engine14_ic_scenario.run_scenario",
        _stub_run_scenario,
    )
    class _D: ...
    monkeypatch.setattr("backend.routers.engine14_ic_scenario.get_client", lambda: _D())
    monkeypatch.setattr(
        "backend.routers.engine14_ic_scenario.get_benzinga_client_optional",
        lambda: None,
    )
    from backend.config import get_flags
    f = get_flags()
    monkeypatch.setattr(
        "backend.routers.engine14_ic_scenario.get_flags",
        lambda: replace(
            f, ENABLE_E14_V2=True, ENABLE_ENGINE14_IC_SCENARIO=True,
            E14_EMIT_DESK_CONSENSUS=False,
        ),
    )
    # Clear the request-level cache between tests.
    from backend.routers.engine14_ic_scenario import _scenario_cache
    _scenario_cache.clear()


def _valid_body(**overrides):
    body = {
        "underlying": "SPX",
        "entryDate": "2026-01-05",
        "expiry":    "2026-01-10",
        "shortPut":  4900, "longPut": 4895,
        "shortCall": 5100, "longCall": 5105,
        "creditReceived": 1.5,
    }
    body.update(overrides)
    return body


def test_response_carries_v2_fields(client):
    r = client.post("/api/ic-scenario", json=_valid_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sourceChip"] in ("desk_default", "user_override")
    assert "wingConsoleCacheKey" in body
    assert "placementRank" in body
    assert "mcResults" in body
    assert body["regime"]["mi_v2"]["label"] == "Transitional"
    # Verdict-strip: reconcile.overall removed, engine2 verdict fields removed.
    assert "overall" not in body.get("reconcile", {})
    assert "deskConsensus" not in body.get("engine2", {})
    assert "recSimple" not in body.get("engine2", {})


def test_response_preserves_verdict_when_flag_on(client, monkeypatch):
    from backend.config import get_flags
    f = get_flags()
    monkeypatch.setattr(
        "backend.routers.engine14_ic_scenario.get_flags",
        lambda: replace(
            f, ENABLE_E14_V2=True, ENABLE_ENGINE14_IC_SCENARIO=True,
            E14_EMIT_DESK_CONSENSUS=True,
        ),
    )
    # Bust cache so we re-run with the new flags.
    from backend.routers.engine14_ic_scenario import _scenario_cache
    _scenario_cache.clear()
    r = client.post("/api/ic-scenario", json=_valid_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reconcile"]["overall"] == "PASS"
    assert body["engine2"]["deskConsensus"] == {"verdict": "TRADE"}


def test_handoff_fields_echoed_from_body(client):
    r = client.post("/api/ic-scenario", json=_valid_body(
        wingConsoleCacheKey="abc123",
        placementRank=2,
        sourceChip="user_override",
    ))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wingConsoleCacheKey"] == "abc123"
    assert body["placementRank"] == 2
    assert body["sourceChip"] == "user_override"
