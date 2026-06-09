"""Unit tests for the decision gate (offline)."""
from __future__ import annotations

from backend.research.decision_gate import build_decision_gate, render_decision_gate_text


def _scorecard(rows):
    return {"rows": rows}


def test_gate_promotes_alive_strategies():
    sc = _scorecard([
        {"strategy": "PEAD", "alive": True, "oos_avg_net_return": 0.01, "oos_t_stat": 3.0, "oos_n": 200},
        {"strategy": "Reversal", "alive": True, "oos_avg_net_return": 0.004, "oos_t_stat": 2.0, "oos_n": 500},
        {"strategy": "Dead", "alive": False, "oos_avg_net_return": -0.001, "oos_t_stat": -0.5, "oos_n": 100},
    ])
    gate = build_decision_gate(sc, max_promote=2)
    promoted = {p["strategy"] for p in gate["promote"]}
    assert promoted == {"PEAD", "Reversal"}
    assert gate["drop"][0]["strategy"] == "Dead"
    assert gate["recommendation"].startswith("BUILD")


def test_gate_watch_when_overlay_adds_no_edge():
    sc = _scorecard([
        {"strategy": "PEAD", "alive": True, "oos_avg_net_return": 0.01, "oos_t_stat": 3.0, "oos_n": 200},
    ])
    overlay = {"PEAD": {"adds_incremental_edge": False}}
    gate = build_decision_gate(sc, overlay)
    assert gate["promote"] == []
    assert gate["watch"][0]["strategy"] == "PEAD"
    assert gate["recommendation"].startswith("HOLD")


def test_gate_promotes_when_overlay_confirms():
    sc = _scorecard([
        {"strategy": "PEAD", "alive": True, "oos_avg_net_return": 0.01, "oos_t_stat": 3.0, "oos_n": 200},
    ])
    overlay = {"PEAD": {"adds_incremental_edge": True}}
    gate = build_decision_gate(sc, overlay)
    assert gate["promote"][0]["strategy"] == "PEAD"
    assert gate["promote"][0]["overlay_adds_edge"] is True


def test_gate_stop_when_nothing_alive():
    sc = _scorecard([
        {"strategy": "X", "alive": False, "oos_avg_net_return": -0.01, "oos_t_stat": -1.0, "oos_n": 50},
    ])
    gate = build_decision_gate(sc)
    assert gate["recommendation"].startswith("STOP")
    # render should not raise
    assert "DECISION GATE" in render_decision_gate_text(gate)
