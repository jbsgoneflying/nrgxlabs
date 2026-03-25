import datetime as dt
import threading

import pytest

from backend.config import FeatureFlags
from backend.spx_ic import compute_engine2_spx_ic
from backend.spx_ic.backtest import backtest_weekly_ic_risk, beta_binomial_mean, pctile
from backend.spx_ic.utils import _pick_weekly_close_expiry_date


class FakeResp:
    def __init__(self, rows):
        self.rows = rows
        self.raw = rows


class FakeOratsClient:
    """
    Minimal ORATS client stub for Engine 2:
    - hist_dailies: provides clsPx by date OR a date-range (start,end)
    - hist_monies_implied: provides vol50 by date with dte field (used by backtest_weekly_ic_risk tests)
    """

    def __init__(self):
        self._dailies = {}
        self._iv = {}

    def add_close(self, ticker: str, date: str, close: float):
        self._dailies[(ticker, date)] = {"tradeDate": date, "clsPx": float(close), "close": float(close)}

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
                dd = str(d)[:10]
                if a <= dd <= b:
                    rows.append(row)
            rows.sort(key=lambda r: str(r.get("tradeDate") or ""))
            return FakeResp(rows)
        row = self._dailies.get((ticker, td[:10]))
        return FakeResp([row] if row else [])

    def hist_monies_implied(self, *, ticker: str, trade_date: str, fields: str, dte: str | None = None):
        row = self._iv.get((ticker, trade_date))
        # Return one row; Engine picks nearest dte anyway.
        return FakeResp([row] if row else [])


def test_backtest_weekly_ic_risk_basic():
    c = FakeOratsClient()

    # Build 3 weeks of Mon->Fri closes for SPY in Jan 2025.
    # Week 1: flat
    c.add_close("SPY", "2025-01-06", 100.0)  # Mon
    c.add_close("SPY", "2025-01-10", 100.0)  # Fri
    c.set_iv("SPY", "2025-01-06", dte=4, vol50=20.0)

    # Week 2: +2%
    c.add_close("SPY", "2025-01-13", 100.0)
    c.add_close("SPY", "2025-01-17", 102.0)
    c.set_iv("SPY", "2025-01-13", dte=4, vol50=20.0)

    # Week 3: -3%
    c.add_close("SPY", "2025-01-20", 100.0)
    c.add_close("SPY", "2025-01-24", 97.0)
    c.set_iv("SPY", "2025-01-20", dte=4, vol50=20.0)

    out = backtest_weekly_ic_risk(
        c,
        ticker="SPY",
        years=1,
        entry_dow=0,
        widths=[0.8, 1.0],
        today=dt.date(2025, 1, 25),
    )

    assert out["rowsUsed"] == 3
    assert len(out["byWidth"]) == 2

    # Deterministic: widths sorted ascending
    assert out["byWidth"][0]["w"] == 0.8
    assert out["byWidth"][1]["w"] == 1.0


def test_compute_engine2_spx_ic_enabled_flag_and_proxy_fallback():
    c = FakeOratsClient()

    # Provide SPY closes for "today" probe, but not SPX, so engine falls back to SPY.
    c.add_close("SPY", "2024-12-30", 98.0)
    c.add_close("SPY", "2024-12-31", 99.0)
    c.add_close("SPY", "2025-01-02", 99.5)
    c.add_close("SPY", "2025-01-03", 100.0)
    c.add_close("SPY", "2025-02-03", 500.0)

    # Provide one backtest week so payload is non-empty.
    c.add_close("SPY", "2025-01-06", 100.0)
    c.add_close("SPY", "2025-01-10", 101.0)

    flags = FeatureFlags(ENABLE_ENGINE2_SPX_IC=True)
    out = compute_engine2_spx_ic(
        client=c,
        benzinga_client=None,
        flags=flags,
        entry_day="mon",
        years=1,
        widths=[1.0],
        risk_target_breach_pct=25.0,
        seasonality_mode="none",
        today=dt.date(2025, 2, 3),
    )

    assert out["enabled"] is True
    assert out["underlying"]["symbol"] == "SPY"
    assert out["underlying"]["isProxy"] is True
    # Payload shape: liveContext includes both weekly and nearest views (may be None if live endpoints absent)
    assert "liveContext" in out
    assert "weeklyFriday" in out["liveContext"]
    assert "nearestDaily" in out["liveContext"]


def test_engine2_weekly_expiry_roll_pre_and_post_close():
    # Expiries include this Friday and next Friday.
    exp_dates = ["2025-01-03", "2025-01-06", "2025-01-10"]
    today = dt.date(2025, 1, 3)  # Friday

    # Before 4:15pm ET (3:00pm ET == 20:00 UTC in winter)
    pre_close_utc = dt.datetime(2025, 1, 3, 20, 0, tzinfo=dt.timezone.utc)
    assert _pick_weekly_close_expiry_date(exp_dates, today=today, now_dt=pre_close_utc) == "2025-01-03"

    # After 4:15pm ET (5:00pm ET == 22:00 UTC in winter)
    post_close_utc = dt.datetime(2025, 1, 3, 22, 0, tzinfo=dt.timezone.utc)
    assert _pick_weekly_close_expiry_date(exp_dates, today=today, now_dt=post_close_utc) == "2025-01-10"


def test_beta_binomial_mean_and_pctile_helpers():
    assert beta_binomial_mean(k=0, n=10, alpha=1, beta=1) == pytest.approx(1 / 12)
    assert beta_binomial_mean(k=10, n=10, alpha=1, beta=1) == pytest.approx(11 / 12)
    assert pctile([1, 2, 3, 4, 5], 0) == 1
    assert pctile([1, 2, 3, 4, 5], 100) == 5
    assert pctile([1, 2, 3, 4, 5], 50) == 3


def test_spx_ic_router_bypasses_cache_when_market_open(monkeypatch):
    from backend.routers import engine2_spx_ic as router

    class _Flags:
        ENABLE_ENGINE2_SPX_IC = True

        def cache_key_engine2(self):
            return ("k",)

    calls = {"n": 0}

    def _compute(**kwargs):
        calls["n"] += 1
        return {"weeks": {"rows": [], "count": 0}, "riskGrid": {"cells": []}, "underlying": {"symbol": "SPX"}}

    monkeypatch.setattr(router, "get_flags", lambda: _Flags())
    monkeypatch.setattr(router, "is_us_equity_market_open", lambda: True)
    monkeypatch.setattr(router, "compute_engine2_spx_ic", _compute)
    monkeypatch.setattr(router, "get_client", lambda: object())
    monkeypatch.setattr(router, "get_benzinga_client_optional", lambda: None)
    monkeypatch.setattr(router, "spx_ic_cache_key", lambda params, fp: ("same",))
    monkeypatch.setattr(router, "spx_ic_cache", {})
    monkeypatch.setattr(router, "spx_ic_cache_lock", threading.Lock())

    router.spx_ic(
        underlying="SPX",
        entry_day="mon",
        years=2,
        widths="1.0",
        risk_target_breach_pct=25.0,
        seasonality_mode="none",
        weeks_offset=0,
        weeks_limit=120,
        grid_limit=0,
    )
    router.spx_ic(
        underlying="SPX",
        entry_day="mon",
        years=2,
        widths="1.0",
        risk_target_breach_pct=25.0,
        seasonality_mode="none",
        weeks_offset=0,
        weeks_limit=120,
        grid_limit=0,
    )

    assert calls["n"] == 2
    assert router.spx_ic_cache == {}


def test_spx_ic_router_uses_cache_when_market_closed(monkeypatch):
    from backend.routers import engine2_spx_ic as router

    class _Flags:
        ENABLE_ENGINE2_SPX_IC = True

        def cache_key_engine2(self):
            return ("k",)

    calls = {"n": 0}

    def _compute(**kwargs):
        calls["n"] += 1
        return {"weeks": {"rows": [], "count": 0}, "riskGrid": {"cells": []}, "underlying": {"symbol": "SPX"}}

    monkeypatch.setattr(router, "get_flags", lambda: _Flags())
    monkeypatch.setattr(router, "is_us_equity_market_open", lambda: False)
    monkeypatch.setattr(router, "compute_engine2_spx_ic", _compute)
    monkeypatch.setattr(router, "get_client", lambda: object())
    monkeypatch.setattr(router, "get_benzinga_client_optional", lambda: None)
    monkeypatch.setattr(router, "spx_ic_cache_key", lambda params, fp: ("same",))
    monkeypatch.setattr(router, "spx_ic_cache", {})
    monkeypatch.setattr(router, "spx_ic_cache_lock", threading.Lock())

    router.spx_ic(
        underlying="SPX",
        entry_day="mon",
        years=2,
        widths="1.0",
        risk_target_breach_pct=25.0,
        seasonality_mode="none",
        weeks_offset=0,
        weeks_limit=120,
        grid_limit=0,
    )
    router.spx_ic(
        underlying="SPX",
        entry_day="mon",
        years=2,
        widths="1.0",
        risk_target_breach_pct=25.0,
        seasonality_mode="none",
        weeks_offset=0,
        weeks_limit=120,
        grid_limit=0,
    )

    assert calls["n"] == 1
    assert ("same",) in router.spx_ic_cache


def test_engine2_strike_targets_prefer_orats_em(monkeypatch):
    c = FakeOratsClient()
    c.add_close("SPY", "2024-12-30", 98.0)
    c.add_close("SPY", "2024-12-31", 99.0)
    c.add_close("SPY", "2025-01-02", 99.5)
    c.add_close("SPY", "2025-01-03", 100.0)
    c.add_close("SPY", "2025-02-03", 500.0)
    c.add_close("SPY", "2025-01-06", 100.0)
    c.add_close("SPY", "2025-01-10", 101.0)

    monkeypatch.setattr(
        "backend.spx_ic.engine.compute_expected_move_weekly",
        lambda *args, **kwargs: {
            "expectedMovePct": 7.8,
            "expectedMoveDollars": 7.8,
            "spotPrice": 100.0,
            "smartSpotPrice": 100.0,
            "oratsExpectedMovePct": 4.9,
            "oratsExpectedMoveSource": "delayed",
        },
    )

    flags = FeatureFlags(ENABLE_ENGINE2_SPX_IC=True)
    out = compute_engine2_spx_ic(
        client=c,
        benzinga_client=None,
        flags=flags,
        entry_day="mon",
        years=1,
        widths=[1.0],
        risk_target_breach_pct=25.0,
        seasonality_mode="none",
        today=dt.date(2025, 2, 3),
    )

    st = out.get("strikeTargets") or {}
    assert st.get("basedOnEmPct") == pytest.approx(4.9)
    assert st.get("emSource") == "delayed"


def test_engine2_width_comparison_2d_em_x_wing(monkeypatch):
    """Width comparison should be a 2D EM x Wing matrix with per-EM breach
    and per-cell outside/ROC metrics."""
    c = FakeOratsClient()

    base = 100.0
    weeks = [
        ("2025-01-06", "2025-01-10", 0.5),
        ("2025-01-13", "2025-01-17", -1.0),
        ("2025-01-20", "2025-01-24", 0.3),
        ("2025-01-27", "2025-01-31", -0.2),
    ]
    for mon, fri, ret in weeks:
        c.add_close("SPY", mon, base)
        c.add_close("SPY", fri, base * (1 + ret / 100.0))
        c.set_iv("SPY", mon, dte=4, vol50=20.0)

    c.add_close("SPY", "2024-12-30", 98.0)
    c.add_close("SPY", "2024-12-31", 99.0)
    c.add_close("SPY", "2025-01-02", 99.5)
    c.add_close("SPY", "2025-01-03", 100.0)
    c.add_close("SPY", "2025-02-03", 500.0)

    flags = FeatureFlags(
        ENABLE_ENGINE2_SPX_IC=True,
        ENGINE2_MULTI_WING=True,
        ENGINE2_WING_WIDTH_PTS="5,10,15",
    )
    out = compute_engine2_spx_ic(
        client=c,
        benzinga_client=None,
        flags=flags,
        entry_day="mon",
        years=1,
        widths=[1.0],
        risk_target_breach_pct=25.0,
        seasonality_mode="none",
        today=dt.date(2025, 2, 3),
    )

    wc = out.get("widthComparison", [])
    # 3 EM multiples (1.0, 1.5, 2.0) x 3 wings (5, 10, 15) = 9 entries
    assert len(wc) == 9

    # Every entry must have both emMult and wingWidthPts
    for entry in wc:
        assert "emMult" in entry, "Missing emMult field"
        assert entry["emMult"] in (1.0, 1.5, 2.0)
        assert entry["wingWidthPts"] in (5, 10, 15)
        assert entry["gridCells"] > 0
        assert entry["breachPct"] is not None
        assert entry["survivalPct"] is not None
        assert "outsidePct" in entry
        assert "expectedLoss" in entry
        assert "fullLossPct" in entry

    # Breach % must be constant across wing widths for a given EM
    em_groups = {}
    for entry in wc:
        em_groups.setdefault(entry["emMult"], []).append(entry)
    for em_val, group in em_groups.items():
        breach_vals = [e["breachPct"] for e in group]
        assert all(abs(b - breach_vals[0]) < 0.01 for b in breach_vals), (
            f"Breach % should be constant across wings at EM {em_val}: {breach_vals}"
        )

    # Breach % should decrease as EM increases (wider short strikes = less breach)
    em_breaches = {em: group[0]["breachPct"] for em, group in em_groups.items()}
    if em_breaches.get(1.0) is not None and em_breaches.get(2.0) is not None:
        assert em_breaches[1.0] >= em_breaches[2.0], (
            f"EM 1.0 breach ({em_breaches[1.0]}) should be >= EM 2.0 ({em_breaches[2.0]})"
        )

    # emBreachSummary and emPreference must be in payload
    assert "emBreachSummary" in out
    assert "emPreference" in out
    em_bs = out["emBreachSummary"]
    assert "1.0" in em_bs or "1" in em_bs
    emp = out["emPreference"]
    assert "emPreference" in emp
    assert emp["emPreference"] in (1.0, 1.5, 2.0)
    assert "compositeScore" in emp
    assert "components" in emp


def test_compute_em_preference_low_risk():
    from backend.spx_ic.engine import _compute_em_preference
    result = _compute_em_preference(
        regime_score=25.0,
        macro_multiplier=1.0,
        news_gate_max_adj=0.0,
        vol_pressure_state="ASK",
        dealer_gamma_sign="positive",
    )
    assert result["emPreference"] == 1.0
    assert result["label"] == "aggressive"
    assert result["compositeScore"] < 35


def test_compute_em_preference_moderate_risk():
    from backend.spx_ic.engine import _compute_em_preference
    result = _compute_em_preference(
        regime_score=50.0,
        macro_multiplier=1.3,
        news_gate_max_adj=25.0,
        vol_pressure_state="NEUTRAL",
        dealer_gamma_sign="unknown",
    )
    assert result["emPreference"] == 1.5
    assert result["label"] == "standard"


def test_compute_em_preference_high_risk():
    from backend.spx_ic.engine import _compute_em_preference
    result = _compute_em_preference(
        regime_score=75.0,
        macro_multiplier=1.8,
        news_gate_max_adj=60.0,
        vol_pressure_state="BID",
        dealer_gamma_sign="negative",
    )
    assert result["emPreference"] == 2.0
    assert result["label"] == "defensive"
    assert result["compositeScore"] >= 60


def test_em_fallback_order():
    from backend.spx_ic.engine import _em_fallback_order
    assert _em_fallback_order(1.5, [1.0, 1.5, 2.0]) == [1.0, 2.0]
    assert _em_fallback_order(1.0, [1.0, 1.5, 2.0]) == [1.5, 2.0]
    assert _em_fallback_order(2.0, [1.0, 1.5, 2.0]) == [1.5, 1.0]


