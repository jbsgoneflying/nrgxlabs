"""Repricing Lab — feature-flag defaults must be inert (PR 1 contract)."""
from __future__ import annotations

from backend.config import get_flags


def test_lab_flags_default_inert(monkeypatch):
    for name in (
        "REPRICING_LAB_ENABLED",
        "REPRICING_LAB_SHORT_ENABLED",
        "REPRICING_LAB_LLM_EXTRACTION_ENABLED",
        "DESK_BRAIN_INTENTS_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    flags = get_flags()
    assert flags.REPRICING_LAB_ENABLED is False
    assert flags.REPRICING_LAB_SHORT_ENABLED is False
    assert flags.REPRICING_LAB_LLM_EXTRACTION_ENABLED is False
    assert flags.DESK_BRAIN_INTENTS_ENABLED is False
    assert flags.REPRICING_LAB_SHADOW_ONLY is True


def test_lab_flags_env_overrides(monkeypatch):
    monkeypatch.setenv("REPRICING_LAB_ENABLED", "1")
    monkeypatch.setenv("REPRICING_LAB_SQLITE_PATH", "/tmp/lab_alt.db")
    monkeypatch.setenv("REPRICING_LAB_RISK_PCT", "0.5")
    monkeypatch.setenv("REPRICING_LAB_MAX_POSITIONS", "8")
    flags = get_flags()
    assert flags.REPRICING_LAB_ENABLED is True
    assert flags.REPRICING_LAB_SQLITE_PATH == "/tmp/lab_alt.db"
    assert flags.REPRICING_LAB_RISK_PCT == 0.5
    assert flags.REPRICING_LAB_MAX_POSITIONS == 8


def test_lab_risk_defaults_match_plan():
    flags = get_flags()
    assert flags.REPRICING_LAB_RISK_PCT == 0.25
    assert flags.REPRICING_LAB_GAP_STRESS_Q == 0.90
    assert flags.REPRICING_LAB_T1_MIN_ADV_USD == 10_000_000.0
    assert flags.REPRICING_LAB_T2_MIN_ADV_USD == 2_000_000.0
    # Deliberately below Desk Brain's 1% per-trade baseline.
    assert flags.REPRICING_LAB_RISK_PCT < flags.DESK_BRAIN_PER_TRADE_RISK_PCT
