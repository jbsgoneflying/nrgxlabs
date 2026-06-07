"""Phase 0 smoke tests for the v2 FastAPI service.

These tests intentionally only cover the contract every downstream
piece of v2 (frontend, ship-and-verify, uptime monitors) depends on:

    1. /api/v2/health is public and returns ok=True
    2. /api/v2/version reports a sensible version + foundation flags
    3. The Phase 0 stub endpoints return shape, not 500s
    4. The auth gate doesn't accidentally block the public endpoints
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    # Force public mode so we don't need an invite cookie inside CI.
    os.environ["PUBLIC_ACCESS"] = "1"
    # Point auth-gate redirects at a stable host even if AUTH_SECRET is set elsewhere.
    os.environ.setdefault("AUTH_SECRET", "test-secret-not-real")
    from v2_app.main import app

    return TestClient(app)


def test_health_is_public_and_ok(client: TestClient) -> None:
    res = client.get("/api/v2/health")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["service"] == "nrgxlabs-v2"
    assert "version" in body and body["version"]
    assert isinstance(body["ts"], int) and body["ts"] > 0


def test_version_reports_foundation_flags(client: TestClient) -> None:
    res = client.get("/api/v2/version")
    assert res.status_code == 200
    body = res.json()
    assert body["service"]
    assert body["version"]
    foundation = body["foundation"]
    expected = {
        "regime_encoder",
        "contrastive_analogues",
        "conformal_calibration",
        "path_generator",
        "learned_ranker",
        "agent_committee",
    }
    assert expected.issubset(foundation.keys())
    # Phase 1 modules 1-5 are real and on by default.
    assert foundation["conformal_calibration"] is True
    assert foundation["contrastive_analogues"] is True
    assert foundation["regime_encoder"] is True
    assert foundation["agent_committee"] is True
    assert foundation["path_generator"] is True
    # Only the learned ranker remains a phase 0 stub.
    online = {
        "conformal_calibration", "contrastive_analogues",
        "regime_encoder", "agent_committee", "path_generator",
    }
    pending = {k: v for k, v in foundation.items() if k not in online}
    assert all(v is False for v in pending.values())


def test_regime_embed_legacy_get_advertises_phase1(client: TestClient) -> None:
    res = client.get("/api/v2/regime/embed")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "phase1_mvp_active"
    assert isinstance(body["feature_names"], list)
    assert len(body["feature_names"]) > 0


def test_analogues_search_advertises_shape(client: TestClient) -> None:
    """Legacy GET search endpoint still works and advertises Phase 1 status."""
    res = client.get("/api/v2/analogues/search", params={"ticker": "NVDA", "k": 80})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "phase1_mvp_active"
    assert body["query"]["ticker"] == "NVDA"
    assert body["query"]["k"] == 80
    assert body["query"]["cross_ticker"] is True
    assert "feature-space" in str(body["embedding_space"]["dim"])


def test_counterfactual_log_accepts_payload(client: TestClient) -> None:
    payload = {
        "engine": "e15",
        "v1_verdict": {"verdict": "GO", "confidence": 0.7},
        "v2_verdict": {"verdict": "PASS", "confidence": 0.55},
        "delta_summary": "v2 dissent on regime cluster",
    }
    res = client.post("/api/v2/counterfactual/log", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    # stream_id may be None in local dev (no redis) — that's acceptable.
    assert "stream_id" in body
    assert "logged" in body


def test_counterfactual_recent_no_redis_returns_empty(client: TestClient) -> None:
    """Without Redis the endpoint must degrade to an empty list, never 500."""
    res = client.get("/api/v2/counterfactual/recent")
    assert res.status_code == 200
    body = res.json()
    assert body["entries"] == []
    assert body["n_returned"] == 0
    assert body["n_disagreements"] == 0


def test_counterfactual_recent_with_fake_redis(client: TestClient, monkeypatch) -> None:
    """When Redis returns entries, the endpoint normalizes and ranks them."""
    fake_entries = [
        (
            "1700000002000-0",
            {
                "ts": "2026-05-08T11:30:00Z",
                "engine": "e15",
                "request_id": "req-2",
                "agree": "0",
                "delta_summary": "v2 dissent on cluster",
                "v1_verdict": '{"verdict":"GO","confidence":0.7,"chatter":"ignored"}',
                "v2_verdict": '{"verdict":"PASS","confidence":0.55}',
            },
        ),
        (
            "1700000001000-0",
            {
                "ts": "2026-05-08T11:25:00Z",
                "engine": "e14",
                "request_id": "req-1",
                "agree": "1",
                "delta_summary": "",
                "v1_verdict": '{"verdict":"HOLD"}',
                "v2_verdict": '{"verdict":"HOLD"}',
            },
        ),
    ]

    class FakeRedis:
        def xrevrange(self, name, count):  # noqa: ARG002
            return fake_entries[:count]

    from v2_app import counterfactual_logger

    monkeypatch.setattr(counterfactual_logger, "_redis_client", lambda: FakeRedis())

    res = client.get("/api/v2/counterfactual/recent", params={"n": 5})
    assert res.status_code == 200
    body = res.json()
    assert body["n_returned"] == 2
    assert body["n_disagreements"] == 1

    first = body["entries"][0]
    assert first["engine"] == "e15"
    assert first["agree"] is False
    # Verdicts are summarized to known keys only — "chatter" must be dropped.
    assert "chatter" not in first["v1_verdict"]
    assert first["v1_verdict"] == {"verdict": "GO", "confidence": 0.7}
    assert first["v2_verdict"] == {"verdict": "PASS", "confidence": 0.55}


def test_counterfactual_recent_clamps_n(client: TestClient) -> None:
    """Caller-supplied n is clamped to [1, 200]; FastAPI Query handles bounds."""
    res = client.get("/api/v2/counterfactual/recent", params={"n": 999})
    assert res.status_code == 422  # FastAPI rejects out-of-range
    res = client.get("/api/v2/counterfactual/recent", params={"n": 0})
    assert res.status_code == 422


def test_landing_page_renders_v2_brand(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    html = res.text
    # The single most distinctive v2 marker — the wordmark + tag.
    assert "v2 · foundation brain" in html
    assert "NRGX Labs" in html
    # Confirms the v2.css token system is wired.
    assert "/static/v2.css" in html


def test_engine_pages_share_one_template(client: TestClient) -> None:
    for slug in ("e1", "e2", "e14", "e15", "mi"):
        res = client.get(f"/{slug}")
        assert res.status_code == 200, f"{slug} returned {res.status_code}"
        assert "/static/engine.js" in res.text


def test_favicon_served(client: TestClient) -> None:
    res = client.get("/favicon.ico")
    # Either the file exists (200) or we degrade gracefully (204), never 500.
    assert res.status_code in (200, 204)
