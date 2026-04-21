"""Engine 14 v2 — /api/ic-scenario/wing-console HTTP tests."""
from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, replace

import pytest
from fastapi.testclient import TestClient


@dataclass
class _Bar:
    trade_date: str
    open:  float
    high:  float
    low:   float
    close: float


@pytest.fixture(scope="module")
def client():
    from backend.app import app
    return TestClient(app)


def _synth_bars():
    rng = random.Random(7)
    start = dt.date.today() - dt.timedelta(days=400)
    bars = []
    spot = 5000.0
    d = start
    while d <= dt.date.today():
        if d.weekday() < 5:
            spot *= (1.0 + rng.gauss(0, 0.008))
            bars.append(_Bar(
                trade_date=d.isoformat(),
                open=spot * 0.999, high=spot * 1.005,
                low=spot * 0.995, close=spot,
            ))
        d += dt.timedelta(days=1)
    return bars


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    bars = _synth_bars()

    def _fake_fetch(client, *, ticker, start, end):
        return [b for b in bars if start.isoformat() <= b.trade_date <= end.isoformat()]

    monkeypatch.setattr(
        "backend.spx_ic.ohlc.fetch_dailies_ohlc_range", _fake_fetch,
    )
    # Stub ORATS client
    class _D: ...
    monkeypatch.setattr(
        "backend.routers.engine14_ic_scenario.get_client", lambda: _D(),
    )
    monkeypatch.setattr(
        "backend.routers.engine14_ic_scenario.get_benzinga_client_optional",
        lambda: None,
    )
    # Stub regime_features KNN to avoid SQLite requirement
    monkeypatch.setattr(
        "backend.engine14.analogue_matcher.build_analogue_universe",
        _fake_analogue_builder(bars),
    )
    from backend.config import get_flags
    f = get_flags()
    monkeypatch.setattr(
        "backend.routers.engine14_ic_scenario.get_flags",
        lambda: replace(
            f,
            ENABLE_E14_V2=True,
            ENABLE_ENGINE14_IC_SCENARIO=True,
        ),
    )
    from backend.engine14.scoring_context import clear_scoring_cache
    clear_scoring_cache()


def _fake_analogue_builder(bars):
    def _fn(*, ticker, closes_sorted, entry_dow, target_dte_calendar_days,
            max_windows):
        from backend.engine14.analogue_matcher import AnalogueWindow
        out = []
        dates = [d for d, _c in closes_sorted]
        for i in range(0, len(dates) - 5, 5):
            ed = dates[i]
            xp = dates[i + 4]
            out.append(AnalogueWindow(
                entry_date=ed,
                expiry_date=xp,
                dte_sessions=4,
                dte_calendar_days=4,
                entry_close=dict(closes_sorted).get(ed, 5000.0),
                entry_em_pct=1.5,
                entry_iv_pct=18.0,
                rv20=0.15, rv20_pct=0.5,
                regime_bucket="MODERATE",
            ))
        return out
    return _fn


def test_wing_console_happy_path(client):
    entry = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    expiry = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    r = client.post("/api/ic-scenario/wing-console", json={
        "underlying": "SPX", "entry_date": entry, "expiry_date": expiry,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wingConsole"]["entry_date"][:10] == entry
    assert len(body["wingConsole"]["placements"]) > 0
    scores = [p["composite_score"] for p in body["wingConsole"]["placements"]]
    assert scores == sorted(scores, reverse=True)
    assert "weightsUsed" in body
    assert body["regime"] is not None


def test_wing_console_rejects_bad_dates(client):
    r = client.post("/api/ic-scenario/wing-console", json={
        "underlying": "SPX", "entry_date": "2026-01-10", "expiry_date": "2026-01-05",
    })
    assert r.status_code == 400


def test_wing_console_rejects_non_spx(client):
    r = client.post("/api/ic-scenario/wing-console", json={
        "underlying": "AAPL", "entry_date": "2026-01-05", "expiry_date": "2026-01-10",
    })
    assert r.status_code == 400


def test_wing_console_404_when_v2_disabled(client, monkeypatch):
    from backend.config import get_flags
    f = get_flags()
    monkeypatch.setattr(
        "backend.routers.engine14_ic_scenario.get_flags",
        lambda: replace(
            f, ENABLE_E14_V2=False, ENABLE_ENGINE14_IC_SCENARIO=True,
        ),
    )
    r = client.post("/api/ic-scenario/wing-console", json={
        "underlying": "SPX",
        "entry_date":  (dt.date.today() + dt.timedelta(days=3)).isoformat(),
        "expiry_date": (dt.date.today() + dt.timedelta(days=7)).isoformat(),
    })
    assert r.status_code == 404


def test_wing_console_custom_weights_flow_through(client):
    entry = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    expiry = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    r = client.post("/api/ic-scenario/wing-console", json={
        "underlying": "SPX", "entry_date": entry, "expiry_date": expiry,
        "weights": {"breach": 0.9, "touch": 0.01, "mae": 0.03, "theta": 0.03, "credit": 0.03},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # breach alias maps to close
    assert body["weightsUsed"]["close"] == 0.9
    assert body["weightsUsed"]["touch"] == 0.01
