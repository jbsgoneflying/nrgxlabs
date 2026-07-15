"""PR 2 — universe PIT + corporate-action property tests."""
from __future__ import annotations

import pytest

from backend.repricing_lab import store
from backend.repricing_lab.bars import bars_from_eod_rows, upsert_bars
from backend.repricing_lab.corporate_actions import (
    apply_split_to_price,
    parse_split_ratio,
    splits_to_rows,
    verify_adjusted_consistency,
)
from backend.repricing_lab.instruments import ensure_instrument
from backend.repricing_lab.qa import run_qa
from backend.repricing_lab.universe_pit import build_universe_snapshot, classify_tier


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = str(tmp_path / "lab.db")
    monkeypatch.setenv("REPRICING_LAB_SQLITE_PATH", p)
    monkeypatch.setenv("REPRICING_LAB_RAW_DIR", str(tmp_path / "raw"))
    return p


def test_parse_split_ratio():
    assert parse_split_ratio("10.000000/1.000000") == 10.0
    assert parse_split_ratio("4/1") == 4.0
    assert parse_split_ratio("bogus") is None


def test_split_price_consistency():
    raw = 1000.0
    ratio = 10.0
    adj = apply_split_to_price(raw, ratio, forward=True)
    assert adj == 100.0
    assert verify_adjusted_consistency(raw, adj, ratio)


def test_classify_tier_reason_codes(monkeypatch):
    monkeypatch.setenv("REPRICING_LAB_UNIVERSE_MIN_PRICE", "5")
    monkeypatch.setenv("REPRICING_LAB_T1_MIN_ADV_USD", "10000000")
    monkeypatch.setenv("REPRICING_LAB_T2_MIN_ADV_USD", "2000000")
    tier, reasons, elig, _ = classify_tier(
        price=3.0, adv20=50_000_000, adv60=50_000_000,
        min_price=5.0, t1_min_adv=10_000_000, t2_min_adv=2_000_000,
    )
    assert tier is None and "price_below_floor" in reasons and not elig

    tier, reasons, elig, short = classify_tier(
        price=50.0, adv20=50_000_000, adv60=50_000_000,
        min_price=5.0, t1_min_adv=10_000_000, t2_min_adv=2_000_000,
    )
    assert tier == "tier1_liquid_core" and elig and short is False

    tier, _, elig, _ = classify_tier(
        price=50.0, adv20=3_000_000, adv60=3_000_000,
        min_price=5.0, t1_min_adv=10_000_000, t2_min_adv=2_000_000,
    )
    assert tier == "tier2_satellite"


def test_universe_snapshot_from_bars(db):
    with store.connect() as conn:
        iid = ensure_instrument(conn, "AAPL")
        eod = []
        for i in range(1, 31):
            day = f"2024-03-{i:02d}" if i <= 31 else "2024-03-01"
            # Only weekdays-ish: use March 2024 calendar days; store accepts any date.
            eod.append({
                "date": f"2024-02-{i:02d}" if i <= 28 else f"2024-03-{i-28:02d}",
                "open": 180, "high": 182, "low": 179, "close": 181,
                "adjusted_close": 181, "volume": 60_000_000,
            })
        upsert_bars(conn, bars_from_eod_rows(iid, eod))
        n = build_universe_snapshot(conn, snapshot_date="2024-03-01")
        assert n == 1
        rows = store.read_rows(conn, "universe_snapshot")
        assert rows[0]["universe_tier"] == "tier1_liquid_core"
        assert rows[0]["eligible_long"] == 1
        assert rows[0]["eligible_short"] == 0


def test_qa_critical_on_empty(db):
    with store.connect() as conn:
        report = run_qa(conn)
        assert report.has_critical_failures()
        codes = {f.code for f in report.findings}
        assert "no_instruments" in codes
        assert "no_bars" in codes


def test_qa_clean_after_seed(db):
    with store.connect() as conn:
        iid = ensure_instrument(conn, "MSFT")
        upsert_bars(conn, bars_from_eod_rows(iid, [
            {"date": "2024-01-02", "open": 370, "high": 375, "low": 368,
             "close": 372, "adjusted_close": 372, "volume": 30_000_000},
        ]))
        report = run_qa(conn)
        assert not report.has_critical_failures()
        assert report.coverage["dailyBars"] == 1


def test_splits_to_rows_idempotent(db):
    with store.connect() as conn:
        iid = ensure_instrument(conn, "NVDA")
        rows = splits_to_rows(iid, [{"date": "2024-06-10", "split": "10.000000/1.000000"}])
        assert store.upsert(conn, "corporate_action", rows) == 1
        assert store.upsert(conn, "corporate_action", rows) == 1
        assert len(store.read_rows(conn, "corporate_action")) == 1
