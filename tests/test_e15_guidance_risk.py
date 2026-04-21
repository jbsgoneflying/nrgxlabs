"""Engine 15 v2 — guidance-risk modifier tests.

Scans Benzinga headlines in the pre-event window for keyword signals
(SEC subpoena, guidance cut, activist, etc.) and combines with E1's
eventRisk score. Caps the tail-multiplier bump at
GUIDANCE_RISK_MAX_BUMP_PCT.
"""
from __future__ import annotations

import pytest

from backend.engine15.conditioning import (
    _scan_headlines_for_guidance_risk,
    guidance_risk_modifier,
)


# ---------------------------------------------------------------------------
# Headline scanner unit tests
# ---------------------------------------------------------------------------


def test_empty_headlines_returns_zero():
    s = _scan_headlines_for_guidance_risk([])
    assert s["score"] == 0.0
    assert s["hits"] == []


def test_high_severity_keyword_fires():
    rows = [{"title": "Acme receives SEC subpoena", "teaser": "accounting irregular", "created": "2026-05-20"}]
    s = _scan_headlines_for_guidance_risk(rows)
    assert s["score"] >= 35
    assert any("subpoena" in hit["keywords"] for hit in s["hits"])


def test_medium_severity_accumulates():
    rows = [
        {"title": "Analyst lowers guidance ahead of earnings", "teaser": "downgrade", "created": "2026-05-20"},
        {"title": "Company issues profit warning", "teaser": "revenue warning", "created": "2026-05-21"},
    ]
    s = _scan_headlines_for_guidance_risk(rows)
    # multiple medium keywords -> >= 30
    assert s["score"] >= 30


def test_score_capped_at_100():
    rows = [
        {"title": "Acme SEC investigation + subpoena + fraud + going concern", "teaser": "massive warning", "created": "2026-05-20"},
    ] * 4
    s = _scan_headlines_for_guidance_risk(rows)
    assert s["score"] == 100.0


def test_irrelevant_headlines_ignored():
    rows = [
        {"title": "Acme announces new product line", "teaser": "growth story", "created": "2026-05-20"},
        {"title": "Routine quarterly dividend increase", "teaser": "", "created": "2026-05-22"},
    ]
    s = _scan_headlines_for_guidance_risk(rows)
    assert s["score"] == 0.0


# ---------------------------------------------------------------------------
# Full modifier tests
# ---------------------------------------------------------------------------


class _FakeBzResp:
    def __init__(self, rows):
        self.rows = rows


class _FakeBzClient:
    def __init__(self, rows=None):
        self._rows = rows or []

    def news(self, **kw):
        return _FakeBzResp(self._rows)


def test_modifier_unavailable_without_inputs():
    m = guidance_risk_modifier({})
    assert m.status == "unavailable"


def test_modifier_neutral_with_no_signals():
    bz = _FakeBzClient([])
    m = guidance_risk_modifier(
        {"eventRisk": {}},
        benzinga_client=bz, ticker="NVDA", earnings_date="2026-05-28",
    )
    assert m.status == "ok"
    assert m.tail_multiplier == 1.0
    assert m.win_rate_shift_pct == 0.0


def test_modifier_applies_tail_bump_with_high_score():
    bz = _FakeBzClient([
        {"title": "Acme receives SEC subpoena over accounting", "created": "2026-05-20"},
        {"title": "Analyst lowers guidance ahead of earnings", "created": "2026-05-22"},
    ])
    m = guidance_risk_modifier(
        {"eventRisk": {}},
        benzinga_client=bz, ticker="NVDA", earnings_date="2026-05-28",
        max_bump_pct=15.0,
    )
    assert m.status == "ok"
    assert m.tail_multiplier > 1.0
    assert m.tail_multiplier <= 1.15 + 1e-6  # capped at max_bump_pct
    assert m.win_rate_shift_pct < 0.0


def test_modifier_uses_e1_event_risk_when_no_benzinga():
    # No benzinga client -> falls back to E1 eventRisk score only.
    m = guidance_risk_modifier(
        {"eventRisk": {"score": 60}},
        benzinga_client=None,
    )
    assert m.status == "ok"
    assert m.tail_multiplier > 1.0


def test_modifier_combines_e1_and_headline_max():
    bz = _FakeBzClient([{"title": "Analyst downgrade", "created": "2026-05-22"}])
    m = guidance_risk_modifier(
        {"eventRisk": {"score": 40}},
        benzinga_client=bz, ticker="NVDA", earnings_date="2026-05-28",
        max_bump_pct=15.0,
    )
    combined = (m.details or {}).get("combinedScore")
    # Combined uses max(), so should be at least 40 (the E1 score).
    assert combined is not None and combined >= 40.0


def test_modifier_bump_cap_honoured():
    # Huge score should still cap at 1 + max_bump_pct/100.
    bz = _FakeBzClient([{"title": "SEC fraud subpoena accounting", "created": "2026-05-20"}] * 3)
    m = guidance_risk_modifier(
        {"eventRisk": {"score": 100}},
        benzinga_client=bz, ticker="NVDA", earnings_date="2026-05-28",
        max_bump_pct=10.0,
    )
    assert m.tail_multiplier <= 1.10 + 1e-6
