"""Engine 15 v2 — E1 WingMAE cross-check badge tests."""
from __future__ import annotations

import pytest

from backend.engine15.simulator import _compute_e1_wing_mae_crosscheck


def test_convergent_both_low():
    # E1 MAE p95 = 5% → expected WK+breach ~10%; actual 12% → divergence ~0.05
    out = _compute_e1_wing_mae_crosscheck(
        {"e1WingMAE": {"p95": 5.0}},
        {"whiteKnuckle": {"pct": 10.0}, "breach": {"pct": 2.0}},
    )
    assert out["source"] == "convergent"
    assert out["divergence"] < 0.25


def test_convergent_both_high():
    # E1 MAE p95 = 15% → expected WK+breach ~30%; actual 28% → divergence ~0.05
    out = _compute_e1_wing_mae_crosscheck(
        {"e1WingMAE": {"p95": 15.0}},
        {"whiteKnuckle": {"pct": 20.0}, "breach": {"pct": 8.0}},
    )
    assert out["source"] == "convergent"


def test_divergent_e1_low_e15_high():
    # E1 thinks tail is small but E15's event pool says otherwise.
    out = _compute_e1_wing_mae_crosscheck(
        {"e1WingMAE": {"p95": 3.0}},
        {"whiteKnuckle": {"pct": 40.0}, "breach": {"pct": 25.0}},
    )
    assert out["source"] == "divergent"
    assert out["divergence"] >= 0.5
    assert "divergence" in out["note"].lower() or "re-scan" in out["note"].lower()


def test_mild_divergence():
    # Moderate gap between pools.
    out = _compute_e1_wing_mae_crosscheck(
        {"e1WingMAE": {"p95": 8.0}},
        {"whiteKnuckle": {"pct": 20.0}, "breach": {"pct": 15.0}},
    )
    assert out["source"] in ("mild_divergence", "divergent")


def test_missing_e1_mae_returns_missing_inputs():
    out = _compute_e1_wing_mae_crosscheck(
        {"e1WingMAE": {}},
        {"whiteKnuckle": {"pct": 10.0}, "breach": {"pct": 5.0}},
    )
    assert out["source"] == "missing_inputs"
    assert out["divergence"] is None


def test_missing_outcome_distribution_returns_missing_inputs():
    out = _compute_e1_wing_mae_crosscheck(
        {"e1WingMAE": {"p95": 10.0}},
        {},
    )
    assert out["source"] == "missing_inputs"


def test_output_shape_stable():
    out = _compute_e1_wing_mae_crosscheck(
        {"e1WingMAE": {"p95": 10.0}},
        {"whiteKnuckle": {"pct": 15.0}, "breach": {"pct": 5.0}},
    )
    for k in ("e1_mae_p95_pct", "e15_white_knuckle_pct",
              "e15_breach_pct", "divergence", "note", "source"):
        assert k in out
