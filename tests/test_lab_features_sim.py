"""Lab features, labels, geometry, intents, simulator goldens."""
from __future__ import annotations

import json

import pytest

from backend.repricing_lab import store
from backend.repricing_lab.bars import bars_from_eod_rows, upsert_bars
from backend.repricing_lab.events import decision_session_for, earnings_rows_from_calendar
from backend.repricing_lab.features.price_trend import atr_wilder, compute_price_trend
from backend.repricing_lab.features.snapshot import build_features_for_instrument
from backend.repricing_lab.gap_stress import gap_stress_quantile, quantile
from backend.repricing_lab.geometry import atr_stop, size_shares
from backend.repricing_lab.instruments import ensure_instrument
from backend.repricing_lab.intents import PositionIntent, merge_intents
from backend.repricing_lab.labels import label_path
from backend.repricing_lab.simulator.constraints import ConstraintConfig, check_entry
from backend.repricing_lab.simulator.engine import run_simulation
from backend.repricing_lab.simulator.fills import fill_stop
from backend.research.cost_model import CostModel


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("REPRICING_LAB_SQLITE_PATH", str(tmp_path / "lab.db"))
    monkeypatch.setenv("REPRICING_LAB_RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("REPRICING_LAB_RUNS_DIR", str(tmp_path / "runs"))
    return str(tmp_path / "lab.db")


def _bars(n=30, start_px=100.0):
    rows = []
    px = start_px
    for i in range(n):
        d = f"2024-01-{i+1:02d}" if i < 28 else f"2024-02-{i-27:02d}"
        o, c = px, px * 1.01
        rows.append({
            "date": d, "open": o, "high": max(o, c) * 1.01,
            "low": min(o, c) * 0.99, "close": c, "adjusted_close": c,
            "volume": 5_000_000,
        })
        px = c
    return rows


def test_decision_session_amc_next_day():
    # 2024-03-05 is Tuesday; AMC → next session Wed
    assert decision_session_for("2024-03-05", "amc") == "2024-03-06"


def test_no_lookahead_features(db):
    with store.connect() as conn:
        iid = ensure_instrument(conn, "AAA")
        upsert_bars(conn, bars_from_eod_rows(iid, _bars(40)))
        # as_of mid-series
        as_of = "2024-01-15T23:59:59Z"
        feats = build_features_for_instrument(conn, iid, as_of=as_of)
        assert "ret_1m" in feats
        # All bars used must be available
        bars = store.read_available(conn, "daily_bar", as_of=as_of, where="instrument_id=?", params=(iid,))
        assert all(b["available_at"] <= as_of for b in bars)


def test_label_stop_wins_same_bar():
    bars = [{
        "session_date": "2024-01-02", "open": 100, "high": 110, "low": 94, "close": 105,
        "adjusted_close": 105,
    }]
    lab = label_path(side="long", entry_price=100, stop_price=95, forward_bars=bars)
    assert lab.status == "stopped"
    assert lab.realized_r == -1.0


def test_label_gap_through_stop():
    bars = [{
        "session_date": "2024-01-02", "open": 90, "high": 92, "low": 88, "close": 91,
        "adjusted_close": 91,
    }]
    lab = label_path(side="long", entry_price=100, stop_price=95, forward_bars=bars)
    assert lab.status == "gap_stopped"
    assert lab.adverse_gap_hit


def test_label_2r_before_stop():
    bars = [
        {"session_date": "2024-01-02", "open": 100, "high": 106, "low": 99, "close": 105, "adjusted_close": 105},
        {"session_date": "2024-01-03", "open": 105, "high": 112, "low": 104, "close": 110, "adjusted_close": 110},
    ]
    lab = label_path(side="long", entry_price=100, stop_price=95, forward_bars=bars, max_hold=10)
    assert lab.hit_before_stop.get("2R") is True


def test_wider_stop_never_more_shares():
    a = size_shares(account_size=25_000, risk_pct=0.25, risk_per_share=1.0,
                    gap_stress=0.5, slippage=0.1, adv20_usd=None, price=100)
    b = size_shares(account_size=25_000, risk_pct=0.25, risk_per_share=2.0,
                    gap_stress=0.5, slippage=0.1, adv20_usd=None, price=100)
    assert b["shares"] <= a["shares"]


def test_gap_stress_quantile():
    bars = []
    px = 100.0
    for i in range(50):
        # alternate down gaps
        o = px * (0.97 if i % 3 == 0 else 1.0)
        bars.append({"open": o, "adjusted_close": o * 1.01, "close": o * 1.01})
        px = bars[-1]["close"]
    q = gap_stress_quantile(bars, q=0.9)
    assert q is not None and q > 0
    assert quantile([1, 2, 3, 4], 0.5) == 2.5


def test_fill_gap_through_stop():
    fr = fill_stop({"open": 90, "high": 92, "low": 88}, side="long", stop=95)
    assert fr.status == "filled" and fr.fill_price == 90 and fr.reason == "gap_through_stop"


def test_constraint_duplicate_ticker():
    rej = check_entry(
        open_positions=[{"instrument_id": "eodhd:AAPL.US", "notional": 1000, "sector": "Tech"}],
        instrument_id="eodhd:AAPL.US", sector="Tech", notional=1000,
        config=ConstraintConfig(),
    )
    assert rej and rej.code == "duplicate_ticker"


def test_simulator_gap_stop_golden(db):
    sessions = ["2024-01-02", "2024-01-03", "2024-01-04"]
    bars = {
        "eodhd:T.US": [
            {"session_date": "2024-01-02", "open": 100, "high": 101, "low": 99, "close": 100, "adjusted_close": 100},
            {"session_date": "2024-01-03", "open": 100, "high": 102, "low": 99, "close": 101, "adjusted_close": 101},
            {"session_date": "2024-01-04", "open": 90, "high": 91, "low": 89, "close": 90, "adjusted_close": 90},
        ]
    }
    cand = {
        "candidate_id": "c1",
        "instrument_id": "eodhd:T.US",
        "side": "long",
        "decision_session": "2024-01-02",
        "entry_plan_json": json.dumps({"entry_price": 100, "session": "2024-01-03"}),
        "stop_plan_json": json.dumps({"stop_price": 95, "risk_per_share": 5}),
        "shares": 10,
    }
    out = run_simulation(candidates=[cand], bars_by_instrument=bars, sessions=sessions, run_id="g1")
    assert out["n_closed"] == 1
    assert out["closed"][0]["exit_reason"] in ("gap_through_stop", "stop")


def test_intent_merge_does_not_multiply_risk():
    a = PositionIntent("AAPL", "long", 18, "E18", 0.25, entry_price=100, stop_price=95, conviction=70)
    b = PositionIntent("AAPL", "long", 5, "Ichimoku", 0.20, entry_price=100, stop_price=93, conviction=60)
    merged = merge_intents([a, b])
    assert len(merged) == 1
    assert merged[0].risk_pct == 0.25  # max, not sum
    assert merged[0].stop_price == 93  # widest for long


def test_intent_conflict_opposite_side():
    a = PositionIntent("AAPL", "long", 18, "E18", 0.25)
    b = PositionIntent("AAPL", "short", 5, "Ichimoku", 0.25)
    merged = merge_intents([a, b])
    assert all(m.conflict for m in merged)


def test_cost_model_for_adv():
    assert CostModel.for_adv(50_000_000).per_side_bps == 8.0
    assert CostModel.for_adv(100_000).per_side_bps == 45.0


def test_earnings_rows_and_atr(db):
    with store.connect() as conn:
        iid = ensure_instrument(conn, "AAPL")
        rows = earnings_rows_from_calendar(iid, [{
            "report_date": "2024-01-25", "before_after_market": "AfterMarket",
            "actual": 2.1, "estimate": 2.0,
        }])
        assert rows[0]["timing"] == "amc"
        assert rows[0]["decision_session"] == "2024-01-26"
        upsert_bars(conn, bars_from_eod_rows(iid, _bars(30)))
        bars = store.read_rows(conn, "daily_bar", where="instrument_id=?", params=(iid,))
        assert atr_wilder(bars, 14) is not None
        trend = compute_price_trend(bars)
        assert trend["last_price"] is not None
