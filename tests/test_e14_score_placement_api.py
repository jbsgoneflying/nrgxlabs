"""Engine 14 v2 — /api/ic-scenario/wing-console/score-placement tests."""
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
    rng = random.Random(17)
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
                entry_date=ed, expiry_date=xp,
                dte_sessions=4, dte_calendar_days=4,
                entry_close=dict(closes_sorted).get(ed, 5000.0),
                entry_em_pct=1.5, entry_iv_pct=18.0,
                rv20=0.15, rv20_pct=0.5, regime_bucket="MODERATE",
            ))
        return out
    return _fn


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    bars = _synth_bars()

    def _fake_fetch(client, *, ticker, start, end):
        return [b for b in bars if start.isoformat() <= b.trade_date <= end.isoformat()]

    monkeypatch.setattr("backend.spx_ic.ohlc.fetch_dailies_ohlc_range", _fake_fetch)
    class _D: ...
    monkeypatch.setattr("backend.routers.engine14_ic_scenario.get_client", lambda: _D())
    monkeypatch.setattr(
        "backend.routers.engine14_ic_scenario.get_benzinga_client_optional",
        lambda: None,
    )
    monkeypatch.setattr(
        "backend.engine14.analogue_matcher.build_analogue_universe",
        _fake_analogue_builder(bars),
    )
    from backend.config import get_flags
    f = get_flags()
    monkeypatch.setattr(
        "backend.routers.engine14_ic_scenario.get_flags",
        lambda: replace(
            f, ENABLE_E14_V2=True, ENABLE_ENGINE14_IC_SCENARIO=True,
        ),
    )
    from backend.engine14.scoring_context import clear_scoring_cache
    clear_scoring_cache()


def test_score_placement_cold_start_builds_context(client):
    entry  = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    expiry = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    r = client.post("/api/ic-scenario/wing-console/score-placement", json={
        "underlying": "SPX", "entry_date": entry, "expiry_date": expiry,
        "em_mult": 1.35, "wing_pts": 12.5,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["context_source"] in ("rebuilt_context", "cached_context")
    p = body["placement"]
    assert abs(p["em_mult"] - 1.35) < 1e-3
    assert abs(p["wing_pts"] - 12.5) < 1e-3
    assert 0.0 <= p["composite_score"] <= 100.0


def test_score_placement_cached_after_warmup(client):
    entry  = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    expiry = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    r1 = client.post("/api/ic-scenario/wing-console", json={
        "underlying": "SPX", "entry_date": entry, "expiry_date": expiry,
    })
    assert r1.status_code == 200, r1.text
    as_of = r1.json()["wingConsole"]["as_of_date"]

    r2 = client.post("/api/ic-scenario/wing-console/score-placement", json={
        "underlying": "SPX", "entry_date": entry, "expiry_date": expiry,
        "as_of_date": as_of, "em_mult": 1.4, "wing_pts": 12,
    })
    assert r2.status_code == 200, r2.text
    assert r2.json()["context_source"] == "cached_context"


def test_score_placement_validation_bounds(client):
    entry  = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    expiry = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    r = client.post("/api/ic-scenario/wing-console/score-placement", json={
        "underlying": "SPX", "entry_date": entry, "expiry_date": expiry,
        "em_mult": 5.0, "wing_pts": 10.0,
    })
    assert r.status_code == 400
    r2 = client.post("/api/ic-scenario/wing-console/score-placement", json={
        "underlying": "SPX", "entry_date": entry, "expiry_date": expiry,
        "em_mult": 1.5, "wing_pts": 0.1,
    })
    assert r2.status_code == 400


def test_score_placement_rejects_non_numeric(client):
    entry  = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    expiry = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    r = client.post("/api/ic-scenario/wing-console/score-placement", json={
        "underlying": "SPX", "entry_date": entry, "expiry_date": expiry,
        "em_mult": "abc", "wing_pts": 10.0,
    })
    assert r.status_code == 400
