"""Tests for backend.engine2b.advisor.

The advisor itself talks to OpenAI and is gated behind a feature flag,
so we pin only what we can verify deterministically:

- ``sanitize_flex_for_llm`` includes the flex-only blocks
  (``flexExpiry``, ``flexAnalytics``, ``liveChain``) plus a capped
  ``samples`` list so the LLM can quote dates.
- The legacy advisor sanitizer (``_sanitize_e2_for_llm``) does NOT
  include these blocks — the regression we are guarding against.
- The fallback shell is returned when the advisor is disabled.
- The ticket-expiry guard rewrites Friday-defaulted dates back to the
  flex expiry.
"""
from __future__ import annotations

from typing import Any, Dict

from backend.engine2b.advisor import (
    _fallback_shell,
    _FLEX_REQUIRED_KEYS,
    generate_flex_advisor,
    sanitize_flex_for_llm,
)


def _flex_payload() -> Dict[str, Any]:
    return {
        "asOfDate": "2026-05-22",
        "params": {"underlying": "SPX", "entryDate": "2026-05-22", "expiryDate": "2026-05-26"},
        "underlying": {"symbol": "SPX"},
        "flexExpiry": {
            "entryDate": "2026-05-22",
            "expiryDate": "2026-05-26",
            "spansHoliday": True,
            "holidayLabel": "Memorial Day",
            "dteSessions": 1,
            "dteCalendarDays": 4,
            "analoguesFound": 17,
            "rowsWithBars": 17,
        },
        "flexAnalytics": {
            "primary": "holidayClass",
            "subsamples": {
                "all": {"n": 17, "breach": [], "openGap": {"n": 17}},
                "regimeMacro": {"n": 5, "breach": [], "openGap": {"n": 5}},
                "holidayClass": {"n": 17, "breach": [], "openGap": {"n": 17}},
                "exactHoliday": {"n": 3, "breach": [], "openGap": {"n": 3}, "holidayLabel": "Memorial Day"},
            },
            "notes": [],
        },
        "liveChain": {
            "enabled": True,
            "symbolUsed": "SPXW",
            "spotPrice": 5550.0,
            "emPct": 1.0,
            "targets": [{"emMult": 2.5, "wingWidthPts": 5}],
            "warnings": [],
        },
        "expectedMove": {"enabled": True, "expectedMovePct": 1.0, "expiry": "2026-05-26"},
        "current": {"regime": {}, "macro": {}},
        "deskConsensus": {"riskLevel": "moderate"},
        "weeks": [
            {"entryDate": f"2024-{m:02d}-{d:02d}", "expiryDate": f"2024-{m:02d}-{d+4:02d}"}
            for (m, d) in [(5, 24), (8, 30), (1, 12), (2, 16), (9, 1)]
        ],
    }


def test_sanitize_flex_for_llm_includes_flex_only_blocks():
    p = _flex_payload()
    ctx = sanitize_flex_for_llm(p)
    # The flex-specific blocks must be in the LLM context.
    assert "flexExpiry" in ctx
    assert "flexAnalytics" in ctx
    assert "liveChain" in ctx
    assert ctx["flexAnalytics"]["primary"] == "holidayClass"
    assert ctx["flexExpiry"]["holidayLabel"] == "Memorial Day"
    # Samples should be capped and present.
    assert "samples" in ctx
    assert ctx["samplesTotal"] == 5
    assert len(ctx["samples"]) <= 15


def test_legacy_e2_sanitizer_excludes_flex_blocks():
    """Regression guard: the Friday-engine advisor must NOT smuggle the
    flex-only blocks through (that would force a Friday prompt to score
    a Tuesday trade)."""
    from backend.engine2_advisor import _sanitize_e2_for_llm

    p = _flex_payload()
    ctx = _sanitize_e2_for_llm(p)
    assert "flexExpiry" not in ctx
    assert "flexAnalytics" not in ctx
    assert "liveChain" not in ctx


def test_fallback_shell_has_required_keys():
    fb = _fallback_shell("test reason")
    assert fb["_source"] == "fallback"
    assert fb["_fallback_reason"] == "test reason"
    assert fb["verdict"] == "PASS"
    for k in _FLEX_REQUIRED_KEYS:
        assert k in fb


def test_generate_flex_advisor_returns_fallback_when_disabled():
    """If the advisor flag is off, we must not call OpenAI."""

    class _Flags:
        ENGINE2_ADVISOR_ENABLED = False
        ENGINE2_ADVISOR_MODEL = "gpt-5.5"

    out = generate_flex_advisor(_flex_payload(), flags=_Flags())
    assert out["_source"] == "fallback"
    assert "disabled" in (out.get("_fallback_reason") or "").lower()


def test_generate_flex_advisor_overrides_friday_expiry_in_ticket(monkeypatch):
    """Even if the LLM emits ``expiry: <some Friday>`` (the legacy
    template still ships in some prompts), the advisor must overwrite
    it with the desk-specified flex expiry before returning."""

    class _Flags:
        ENGINE2_ADVISOR_ENABLED = True
        ENGINE2_ADVISOR_MODEL = "gpt-5.5"

    # Pretend the LLM returns a structurally valid but Friday-expiry'd ticket.
    bad_llm_result = {k: "x" for k in _FLEX_REQUIRED_KEYS}
    bad_llm_result["verdict"] = "TRADE"
    bad_llm_result["confidence"] = 60
    bad_llm_result["keyRisks"] = []
    bad_llm_result["tradeTicket"] = {
        "underlying": "SPX",
        "entry": "2026-05-22",
        "expiry": "2026-05-29",  # Friday default — must be rewritten to 2026-05-26.
        "shortPutStrike": 5410,
        "longPutStrike": 5405,
        "shortCallStrike": 5690,
        "longCallStrike": 5695,
        "wingWidth": 5,
        "emMultiple": 2.5,
    }
    bad_llm_result["cohortUsed"] = {"name": "holidayClass", "n": 6}
    bad_llm_result["edgeAssessment"] = "x"

    # Patch the dependency funnel so generate_flex_advisor returns our canned result.
    import backend.engine2b.advisor as adv

    class _Resp:
        choices = [type("C", (), {"message": type("M", (), {"content": "{}"})})]

    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    # Return a faux response whose content parses to bad_llm_result.
                    import json
                    return type("R", (), {
                        "choices": [type("C", (), {
                            "message": type("M", (), {"content": json.dumps(bad_llm_result)})
                        })]
                    })

    monkeypatch.setattr(adv, "_get_openai_client", lambda: _Client())
    monkeypatch.setattr(adv, "_load_prompt", lambda *a, **k: "irrelevant prompt")
    monkeypatch.setattr(adv, "_load_todays_dms", lambda: {})
    monkeypatch.setattr(adv, "_extract_dms_context", lambda *a, **k: {})

    out = adv.generate_flex_advisor(_flex_payload(), flags=_Flags())
    assert out["_source"] == "llm"
    assert out["tradeTicket"]["expiry"] == "2026-05-26", (
        f"expected expiry rewritten to flex value, got {out['tradeTicket']['expiry']}"
    )
