"""EODHD client — Repricing Lab endpoint additions (splits/dividends/symbol list).

Network-free: patches the module-level ``_http_get`` and asserts URL routing,
query-parameter handling, and row normalization for the three new methods.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import pytest

import backend.eodhd_client as eodhd_mod
from backend.eodhd_client import EodhdClient


class _FakeHttp:
    """Captures requests and returns canned 200 responses per URL substring."""

    def __init__(self, responses: Dict[str, Any]):
        self.responses = responses
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def __call__(self, url: str, params: Dict[str, Any], timeout_s: float):
        self.calls.append((url, dict(params)))
        for fragment, payload in self.responses.items():
            if fragment in url:
                return 200, {}, json.dumps(payload).encode()
        return 404, {}, b"{}"


@pytest.fixture()
def client():
    return EodhdClient(token="test-token")


def test_get_splits_routes_and_normalizes(client, monkeypatch):
    fake = _FakeHttp({
        "/splits/NVDA.US": [{"date": "2024-06-10", "split": "10.000000/1.000000"}],
    })
    monkeypatch.setattr(eodhd_mod, "_http_get", fake)

    resp = client.get_splits("NVDA.US", from_date="2024-01-01", to_date="2024-12-31")
    assert resp.rows == [{"date": "2024-06-10", "split": "10.000000/1.000000"}]

    url, params = fake.calls[0]
    assert url.endswith("/splits/NVDA.US")
    assert params["from"] == "2024-01-01"
    assert params["to"] == "2024-12-31"
    assert params["fmt"] == "json"
    assert params["api_token"] == "test-token"


def test_get_splits_omits_dates_when_absent(client, monkeypatch):
    fake = _FakeHttp({"/splits/AAPL.US": []})
    monkeypatch.setattr(eodhd_mod, "_http_get", fake)

    resp = client.get_splits("AAPL.US")
    assert resp.rows == []
    _url, params = fake.calls[0]
    assert "from" not in params and "to" not in params


def test_get_dividends_routes_and_normalizes(client, monkeypatch):
    row = {
        "date": "2024-02-09", "declarationDate": "2024-02-01",
        "recordDate": "2024-02-12", "paymentDate": "2024-02-15",
        "period": "Quarterly", "value": 0.24, "unadjustedValue": 0.24,
        "currency": "USD",
    }
    fake = _FakeHttp({"/div/AAPL.US": [row]})
    monkeypatch.setattr(eodhd_mod, "_http_get", fake)

    resp = client.get_dividends("AAPL.US", from_date="2024-01-01")
    assert resp.rows == [row]
    url, params = fake.calls[0]
    assert url.endswith("/div/AAPL.US")
    assert params["from"] == "2024-01-01"
    assert "to" not in params


def test_get_exchange_symbols_active_by_default(client, monkeypatch):
    fake = _FakeHttp({
        "/exchange-symbol-list/US": [
            {"Code": "AAPL", "Name": "Apple Inc", "Exchange": "NASDAQ", "Type": "Common Stock"},
        ],
    })
    monkeypatch.setattr(eodhd_mod, "_http_get", fake)

    resp = client.get_exchange_symbols("US")
    assert resp.rows[0]["Code"] == "AAPL"
    _url, params = fake.calls[0]
    assert "delisted" not in params
    assert "type" not in params


def test_get_exchange_symbols_delisted_flag_and_type(client, monkeypatch):
    fake = _FakeHttp({
        "/exchange-symbol-list/US": [
            {"Code": "ATVI", "Name": "Activision Blizzard", "Exchange": "NASDAQ"},
        ],
    })
    monkeypatch.setattr(eodhd_mod, "_http_get", fake)

    resp = client.get_exchange_symbols("US", delisted=True, security_type="common_stock")
    assert resp.rows[0]["Code"] == "ATVI"
    url, params = fake.calls[0]
    assert url.endswith("/exchange-symbol-list/US")
    assert params["delisted"] == 1
    assert params["type"] == "common_stock"


def test_new_endpoints_share_ttl_cache(client, monkeypatch):
    fake = _FakeHttp({"/splits/AAPL.US": [{"date": "2020-08-31", "split": "4.000000/1.000000"}]})
    monkeypatch.setattr(eodhd_mod, "_http_get", fake)

    first = client.get_splits("AAPL.US")
    second = client.get_splits("AAPL.US")
    assert first is second  # served from the client's TTL cache
    assert len(fake.calls) == 1


def test_auth_error_raises(client, monkeypatch):
    def _forbidden(url, params, timeout_s):
        return 403, {}, b'{"message": "forbidden"}'

    monkeypatch.setattr(eodhd_mod, "_http_get", _forbidden)
    with pytest.raises(eodhd_mod.EodhdError, match="auth error 403"):
        client.get_exchange_symbols("US", delisted=True)
