"""Tests for trade-log breachPct enrichment.

The v2 conformal calibrator can only observe (prediction, realized) pairs
when ``entryContext.breachPct`` is populated on the closed trade. The
log_trade enrichment in trade_memory.py guarantees that property by
deriving the value from breachSnapshot / predictionSnapshot / etc. when
the frontend payload omits it.
"""

from __future__ import annotations

import pytest

from backend.trade_memory import (
    derive_breach_pct,
    enrich_trade_log_payload,
)


# ── derive_breach_pct ──


def test_derive_canonical_path_wins() -> None:
    payload = {
        "entryContext": {"breachPct": 22.5},
        "breachSnapshot": {"breachRatePct": 99.0},  # should NOT win
    }
    pct, src = derive_breach_pct(payload)
    assert pct == 22.5
    assert src == "entryContext.breachPct"


def test_derive_falls_back_to_breach_snapshot() -> None:
    payload = {
        "entryContext": {"breachPct": None},
        "breachSnapshot": {"breachRatePct": 18.4},
    }
    pct, src = derive_breach_pct(payload)
    assert pct == 18.4
    assert src == "breachSnapshot.breachRatePct"


def test_derive_falls_back_to_prediction_snapshot_breach_prob() -> None:
    """predictionSnapshot.breachProb is on [0, 1] — must be scaled to percent."""
    payload = {
        "predictionSnapshot": {"breachProb": 0.27},
    }
    pct, src = derive_breach_pct(payload)
    assert pct == 27.0
    assert src == "predictionSnapshot.breachProb"


def test_derive_em_summary_takes_min_value() -> None:
    """E1 EM-bucketed summary → use the tightest-wing value."""
    payload = {
        "entryContext": {
            "breachPct": None,
            "emBreachSummary": {"1.0": 28.0, "1.5": 14.0, "2.0": 7.5},
        },
    }
    pct, src = derive_breach_pct(payload)
    assert pct == 7.5
    assert src == "entryContext.emBreachSummary[min]"


def test_derive_skips_out_of_range_values() -> None:
    """Garbage in entryContext shouldn't poison the result."""
    payload = {
        "entryContext": {"breachPct": 250.0},  # invalid
        "breachSnapshot": {"breachRatePct": 19.0},  # valid fallback
    }
    pct, src = derive_breach_pct(payload)
    assert pct == 19.0
    assert src == "breachSnapshot.breachRatePct"


def test_derive_returns_none_when_no_path_has_value() -> None:
    payload = {"entryContext": {}, "breachSnapshot": {}}
    pct, src = derive_breach_pct(payload)
    assert pct is None
    assert src == "none"


def test_derive_handles_string_inputs() -> None:
    """JSON-roundtrip can leave numeric fields as strings; coerce safely."""
    payload = {"breachSnapshot": {"breachRatePct": "23.0"}}
    pct, src = derive_breach_pct(payload)
    assert pct == 23.0
    assert src == "breachSnapshot.breachRatePct"


# ── enrich_trade_log_payload ──


def test_enrichment_populates_missing_field() -> None:
    payload = {
        "entryContext": {"vrpScore": 1.4},
        "breachSnapshot": {"breachRatePct": 17.5},
    }
    enrich_trade_log_payload(payload, engine="e1")
    assert payload["entryContext"]["breachPct"] == 17.5
    assert payload["entryContext"]["breachPctSource"] == "breachSnapshot.breachRatePct"


def test_enrichment_leaves_existing_value_alone() -> None:
    payload = {
        "entryContext": {"breachPct": 12.0},
        "breachSnapshot": {"breachRatePct": 99.0},
    }
    enrich_trade_log_payload(payload, engine="e2")
    assert payload["entryContext"]["breachPct"] == 12.0
    # The "source" sentinel is only set when we actually enriched.
    assert "breachPctSource" not in payload["entryContext"]


def test_enrichment_no_op_when_no_source(caplog) -> None:
    payload = {"entryContext": {}, "breachSnapshot": {}}
    with caplog.at_level("WARNING", logger="backend.trade_memory"):
        result = enrich_trade_log_payload(payload, engine="e1")
    assert result is payload  # mutates in place + returns for chaining
    assert "breachPct" not in payload["entryContext"]
    # Should have logged a warning so we surface FE/advisor bugs.
    msgs = [r.message for r in caplog.records]
    assert any("no breach prediction found" in m for m in msgs)


def test_enrichment_creates_entry_context_if_missing() -> None:
    payload = {"breachSnapshot": {"breachRatePct": 21.0}}
    enrich_trade_log_payload(payload, engine="e1")
    assert payload["entryContext"]["breachPct"] == 21.0


def test_enrichment_idempotent() -> None:
    payload = {"breachSnapshot": {"breachRatePct": 14.0}}
    enrich_trade_log_payload(payload, engine="e2")
    snapshot = dict(payload["entryContext"])
    enrich_trade_log_payload(payload, engine="e2")
    assert payload["entryContext"] == snapshot
