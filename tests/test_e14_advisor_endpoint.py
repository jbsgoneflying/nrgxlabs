"""Engine 14 v2 — /api/ic-scenario/advisor endpoint tests."""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from backend.app import app
    return TestClient(app)


def _scenario_stub():
    return {
        "engine": 14, "version": "2.0.0",
        "request": {"underlying": "SPX"},
        "analoguesUsed": 25,
        "entryState": {"userSpot": 5000, "userEmPct": 1.5,
                        "regimeBucket": "MODERATE", "regimeMiV2": None},
        "regime": {"label": "MODERATE", "mi_v2": None},
        "outcomeDistribution": {
            "fullCollect": {"pct": 40.0}, "earlyTarget": {"pct": 25.0},
            "whiteKnuckle": {"pct": 10.0}, "stopOut": {"pct": 15.0},
            "breach": {"pct": 10.0},
        },
        "expectedValue": {"meanPnlPct": 12.0},
    }


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    from backend.config import get_flags
    f = get_flags()
    # Default: E14 enabled + advisor enabled.
    monkeypatch.setattr(
        "backend.routers.engine14_ic_scenario.get_flags",
        lambda: replace(
            f, ENABLE_E14_V2=True, ENABLE_ENGINE14_IC_SCENARIO=True,
            E14_ADVISOR_ENABLED=True,
        ),
    )


def test_advisor_route_registered():
    from backend.app import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/ic-scenario/advisor" in paths


def test_advisor_with_prebuilt_scenario(client):
    r = client.post("/api/ic-scenario/advisor", json={"scenario": _scenario_stub()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "advisor" in body
    adv = body["advisor"]
    assert adv["verdict"] in ("GO", "HOLD", "PASS")
    assert "narrative" in adv
    # Without OPENAI_API_KEY in tests, we expect the deterministic shell.
    assert adv["_source"] in ("fallback", "llm")
    assert "scenarioEcho" in body
    assert body["scenarioEcho"]["analoguesUsed"] == 25


def test_advisor_404_when_disabled(client, monkeypatch):
    from backend.config import get_flags
    f = get_flags()
    monkeypatch.setattr(
        "backend.routers.engine14_ic_scenario.get_flags",
        lambda: replace(
            f, ENABLE_E14_V2=True, ENABLE_ENGINE14_IC_SCENARIO=True,
            E14_ADVISOR_ENABLED=False,
        ),
    )
    r = client.post("/api/ic-scenario/advisor", json={"scenario": _scenario_stub()})
    assert r.status_code == 404


def test_advisor_requires_scenario_or_request(client):
    r = client.post("/api/ic-scenario/advisor", json={})
    assert r.status_code == 400


def test_advisor_fallback_shell_has_all_keys(client):
    r = client.post("/api/ic-scenario/advisor", json={"scenario": _scenario_stub()})
    assert r.status_code == 200
    adv = r.json()["advisor"]
    for k in ["verdict", "confidence", "stance", "narrative", "keyPoints",
              "risks", "suggestedAdjustments", "deskNote", "plannedExitNote"]:
        assert k in adv
