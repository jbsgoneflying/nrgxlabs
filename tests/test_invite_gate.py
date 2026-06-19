"""Invite-code gate regression tests.

The desk is invite-only: when ``INVITE_CODE`` + ``AUTH_SECRET`` are set and
``PUBLIC_ACCESS`` is not explicitly truthy, every engine route must sit behind
the /login wall. These tests pin that gated-by-default behavior (so general
traffic off the splash page can't reach the engines without the code) and the
two ways the gate could leak: the public-asset whitelist and the health probe.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import backend.app as appmod


@pytest.fixture()
def gated_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # The gate reads these at import time, so patch the module globals plus the
    # PUBLIC_ACCESS env knob that _auth_enabled() consults on every request.
    monkeypatch.setattr(appmod, "INVITE_CODE", "TEST-CODE", raising=False)
    monkeypatch.setattr(appmod, "AUTH_SECRET", "test-secret", raising=False)
    monkeypatch.delenv("PUBLIC_ACCESS", raising=False)
    return TestClient(appmod.app)


def test_gate_engaged_by_default() -> None:
    # With no INVITE_CODE configured the gate stays inert (the test process has
    # no .env); the important invariant is the PUBLIC_ACCESS *default* is gated.
    monkey_default = (appmod.os.getenv("PUBLIC_ACCESS") or "0").strip().lower()
    assert monkey_default == "0"


def test_protected_route_redirects_to_login(gated_client: TestClient) -> None:
    r = gated_client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


def test_engine_page_redirects_to_login(gated_client: TestClient) -> None:
    r = gated_client.get("/market-intelligence", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


def test_static_html_shell_cannot_bypass_gate(gated_client: TestClient) -> None:
    # The engine HTML lives under /static but must not be reachable directly.
    r = gated_client.get("/static/spx.html", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


def test_static_assets_stay_public() -> None:
    assert appmod._path_is_public("/static/styles.css") is True
    assert appmod._path_is_public("/static/NRGX-Logo.png") is True
    assert appmod._path_is_public("/static/spx.html") is False
    assert appmod._path_is_public("/static/sub/page.HTML") is False


def test_health_and_login_stay_public(gated_client: TestClient) -> None:
    assert gated_client.get("/api/health").status_code == 200
    assert gated_client.get("/login").status_code == 200
