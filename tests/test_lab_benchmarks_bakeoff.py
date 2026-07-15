"""Benchmark + bake-off smoke tests (synthetic, offline)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def lab_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("REPRICING_LAB_SQLITE_PATH", str(tmp_path / "lab.db"))
    monkeypatch.setenv("REPRICING_LAB_RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("REPRICING_LAB_RUNS_DIR", str(tmp_path / "runs"))
    return tmp_path


def test_benchmarks_synthetic(lab_dirs):
    from backend.repricing_lab.benchmarks import run_benchmarks
    path = run_benchmarks(synthetic=True)
    assert Path(path).exists()
    data = json.loads(Path(path).read_text())
    assert "B0_pead" in data
    assert data["B0_pead"]["n_trades"] > 0
    assert "B1_hold_variants" in data


def test_bakeoff_synthetic(lab_dirs):
    from backend.repricing_lab.cohorts import run_bakeoff
    path = run_bakeoff(start="2021-01-01", end="2025-12-31", synthetic=True)
    assert Path(path).exists()
    data = json.loads(Path(path).read_text())
    assert "scorecards" in data
    assert "event_only" in data["scorecards"]


def test_promotion_report(lab_dirs):
    from backend.repricing_lab import store
    from backend.repricing_lab.runs import start_run, finish_run, write_promotion_report
    with store.connect() as conn:
        rid = start_run(conn, kind="bakeoff", config={"strategy_version": "t"}, seed=1)
        finish_run(conn, rid, ok=True, result={"expectancy_r": 0.05, "n_closed": 3})
    path = write_promotion_report(run_id=rid, decision="insufficient_data")
    assert Path(path).exists()
    data = json.loads(Path(path).read_text())
    assert data["decision"] == "insufficient_data"


def test_lab_health_disabled():
    from backend.routers.equity_repricing import lab_health
    h = lab_health()
    assert h["engineRegistered"] is False
    assert "enabled" in h


def test_decay_check():
    from backend.repricing_lab.signals import decay_check
    assert decay_check(expectancy_r=-0.1, n=40)["demoteToShadowOnly"] is True
    assert decay_check(expectancy_r=-0.1, n=5)["demoteToShadowOnly"] is False
