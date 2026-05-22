"""Tests for backend.engine2b.engine.compute_engine2b_flex_ic.

Focus: payload-shape parity with /api/spx-ic + the flexExpiry block.
Heavy signal helpers (regime / EM / sector dispersion) are monkeypatched
so the test is fast and deterministic. The grid loop and shape derivation
run against a real FakeOratsClient.
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Tuple

import pytest

from backend.config import FeatureFlags
from backend.engine2b import compute_engine2b_flex_ic


class _Resp:
    def __init__(self, rows: List[dict]):
        self.rows = rows
        self.raw = rows


class FakeOratsClient:
    """Minimal stub: hist_dailies (range), hist_monies_implied; no live methods."""

    def __init__(self):
        self._dailies: Dict[Tuple[str, str], dict] = {}
        self._iv: Dict[Tuple[str, str], dict] = {}

    def add_close(self, ticker: str, date: str, close: float, *, high: float | None = None, low: float | None = None):
        self._dailies[(ticker, date)] = {
            "tradeDate": date,
            "clsPx": float(close),
            "close": float(close),
            "hiPx": float(high if high is not None else close * 1.005),
            "loPx": float(low if low is not None else close * 0.995),
        }

    def set_iv(self, ticker: str, trade_date: str, dte: int, vol50: float):
        self._iv[(ticker, trade_date)] = {"tradeDate": trade_date, "dte": int(dte), "vol50": float(vol50)}

    def hist_dailies(self, ticker: str, trade_date: str, fields: str):
        td = str(trade_date)
        if "," in td:
            a, b = [x.strip()[:10] for x in td.split(",", 1)]
            rows = []
            for (t, d), row in self._dailies.items():
                if t != ticker:
                    continue
                if a <= str(d)[:10] <= b:
                    rows.append(row)
            rows.sort(key=lambda r: str(r.get("tradeDate") or ""))
            return _Resp(rows)
        row = self._dailies.get((ticker, td[:10]))
        return _Resp([row] if row else [])

    def hist_monies_implied(self, *, ticker: str, trade_date: str, fields: str, dte: str | None = None):
        row = self._iv.get((ticker, trade_date))
        return _Resp([row] if row else [])


def _seed_spy_history(client: FakeOratsClient, *, end: dt.date, days: int = 1100):
    """Populate ~days calendar days of synthetic SPY closes ending at `end`."""
    from backend.engine15.trading_calendar import is_trading_day

    px = 400.0
    d = end - dt.timedelta(days=days)
    bias = -1
    while d <= end:
        if is_trading_day(d):
            bias = -bias
            px = max(50.0, px * (1.0 + 0.001 * bias))
            client.add_close("SPY", d.isoformat(), round(px, 2))
        d += dt.timedelta(days=1)


@pytest.fixture
def stub_signals(monkeypatch):
    """Skip the heavy regime / sector / live EM computations."""

    def _fake_regime(client, **kwargs):
        return {
            "asOfDate": kwargs.get("as_of").isoformat() if kwargs.get("as_of") else "",
            "score100": 50.0,
            "bucket": "MODERATE",
            "label": "Stubbed",
            "components": {},
            "inputs": {},
        }

    monkeypatch.setattr("backend.engine2b.engine.compute_regime_score_for_date", _fake_regime)
    monkeypatch.setattr("backend.engine2b.engine.compute_sector_dispersion_series", lambda *args, **kwargs: {})

    def _fake_em_flex(client, **kwargs):
        return {
            "ticker": "SPX",
            "asOfDate": kwargs.get("today").isoformat(),
            "expiry": kwargs.get("expiry").isoformat(),
            "dte": (kwargs.get("expiry") - kwargs.get("today")).days,
            "source": None,
            "spotPrice": None,
            "expectedMovePct": None,
            "warnings": ["Stubbed — no live chain in test."],
            "notes": [],
        }

    monkeypatch.setattr("backend.engine2b.engine.compute_expected_move_flex", _fake_em_flex)


def test_flex_engine_payload_shape_fri_to_tue_memorial_day(stub_signals):
    """Fri 2026-05-22 -> Tue 2026-05-26 must produce a payload mirroring
    /api/spx-ic keys plus a flexExpiry block with spans_holiday=True."""
    today = dt.date(2026, 5, 22)
    client = FakeOratsClient()
    _seed_spy_history(client, end=today, days=900)

    flags = FeatureFlags(ENABLE_ENGINE2_SPX_IC=True, ENABLE_E2B_FLEX_EXPIRY=True)
    out = compute_engine2b_flex_ic(
        client=client,
        benzinga_client=None,
        flags=flags,
        underlying_preference="SPX",
        entry_date=today,
        expiry_date=dt.date(2026, 5, 26),
        years=2,
        widths=[1.0, 1.5, 2.0, 2.5],
        risk_target_breach_pct=25.0,
        today=today,
    )

    # Core E2-parity keys must all be present.
    for k in (
        "asOfDate",
        "params",
        "underlying",
        "flexExpiry",
        "current",
        "regime",
        "liveContext",
        "expectedMove",
        "oddsLikeNow",
        "backtest",
        "recommendation",
        "recSimple",
        "riskGrid",
        "widthComparison",
        "emPreference",
        "emBreachSummary",
        "deskConsensus",
        "weeks",
        "telemetry",
    ):
        assert k in out, f"missing top-level key: {k}"

    flex = out["flexExpiry"]
    assert flex["entryDate"] == "2026-05-22"
    assert flex["expiryDate"] == "2026-05-26"
    assert flex["entryWeekday"] == 4   # Friday
    assert flex["expiryWeekday"] == 1  # Tuesday
    assert flex["dteCalendarDays"] == 4
    assert flex["dteSessions"] == 1
    assert flex["spansHoliday"] is True
    assert flex["holiday"]["date"] == "2026-05-25"
    assert "Memorial" in (flex["holiday"]["label"] or "")
    assert flex["analoguesFound"] >= 1, "expected at least one historical analogue"

    # Underlying selection (proxy fallback to SPY since FakeClient has no SPX).
    assert out["underlying"]["symbol"] in ("SPX", "SPY")
    if out["underlying"]["symbol"] == "SPY":
        assert out["underlying"]["isProxy"] is True

    # Risk grid should be non-empty when we have at least one window with bars.
    risk_grid = out["riskGrid"]
    assert risk_grid["count"] >= 1
    assert isinstance(risk_grid["cells"], list)

    # Recommendation block has the expected scaffold.
    rec = out["recommendation"]
    assert rec["entryDay"] == "fri"
    assert rec["seasonBucket"] == "ALL"
    assert "policy" in rec
    assert "emPreference" in rec


def test_flex_engine_validation_rejects_inverted_dates(stub_signals):
    """expiry <= entry must raise ValueError so the router can 400."""
    flags = FeatureFlags(ENABLE_ENGINE2_SPX_IC=True, ENABLE_E2B_FLEX_EXPIRY=True)
    client = FakeOratsClient()
    _seed_spy_history(client, end=dt.date(2026, 5, 22), days=100)
    with pytest.raises(ValueError):
        compute_engine2b_flex_ic(
            client=client,
            benzinga_client=None,
            flags=flags,
            underlying_preference="SPX",
            entry_date=dt.date(2026, 5, 26),
            expiry_date=dt.date(2026, 5, 22),
            years=1,
            today=dt.date(2026, 5, 22),
        )


def test_flex_engine_widths_include_2_5_by_default(stub_signals):
    """The desk uses 2.5× EM placement for holiday-weekend trades; default grid must include it."""
    today = dt.date(2026, 5, 22)
    client = FakeOratsClient()
    _seed_spy_history(client, end=today, days=600)
    flags = FeatureFlags(ENABLE_ENGINE2_SPX_IC=True, ENABLE_E2B_FLEX_EXPIRY=True)
    out = compute_engine2b_flex_ic(
        client=client,
        benzinga_client=None,
        flags=flags,
        underlying_preference="SPX",
        entry_date=today,
        expiry_date=dt.date(2026, 5, 26),
        years=1,
        widths=None,  # use default
        today=today,
    )
    em_mults = out["params"]["emMults"]
    assert 2.5 in em_mults, f"expected 2.5 in default em_mults, got {em_mults}"
