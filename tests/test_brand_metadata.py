"""Smoke tests for NRGX Labs brand metadata.

After the Raven Tech → NRGX Labs rebrand, three surfaces need to stay
consistent or the new brand stops appearing in browser tabs, link
previews, and search-engine policies:

  1. FastAPI app title (powers /docs OpenAPI page) → "NRGX Labs"
  2. /robots.txt → Disallow: /  (the platform is invite-only)
  3. /login HTML → ships description / application-name / robots meta
     so saved-to-home-screen shortcuts and crawl bots see the right
     thing even before login.

These are tiny, deterministic, and cheap. They guard against silent
regressions when someone touches backend/app.py or the login template.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    from backend.app import app
    return TestClient(app)


def test_fastapi_app_title_is_nrgx_labs() -> None:
    from backend.app import app
    assert app.title == "NRGX Labs", (
        "FastAPI app title drives /docs and /openapi.json — must be NRGX Labs."
    )


def test_robots_txt_disallows_all(client: TestClient) -> None:
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text.strip().splitlines()
    assert "User-agent: *" in body
    assert "Disallow: /" in body


def test_login_page_carries_nrgx_brand_meta(client: TestClient) -> None:
    r = client.get("/login")
    assert r.status_code == 200
    html = r.text
    # Title / description / application-name all reference the new brand.
    assert "<title>NRGX Labs — Access</title>" in html
    assert 'name="description"' in html
    assert 'content="NRGX Labs' in html
    assert 'name="application-name" content="NRGX Labs"' in html
    assert 'name="apple-mobile-web-app-title" content="NRGX Labs"' in html
    # Login page must not be indexed.
    assert 'name="robots" content="noindex, nofollow"' in html
    # Old brand must not survive on this page.
    assert "Raven-Tech.co" not in html
