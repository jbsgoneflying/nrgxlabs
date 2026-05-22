"""Engine 2b — Flex-Expiry AI Trade Advisor.

Sibling of :mod:`backend.engine2_advisor` purpose-built for the
flex-expiry payload. We deliberately do NOT reuse
:func:`backend.engine2_advisor.generate_trade_analysis` because the
Friday-engine advisor:

- Loads the Friday-only prompt (says "weekly SPX iron condor" + ticket
  template says ``"expiry": "<next Friday YYYY-MM-DD>"``).
- Sanitizes the payload by dropping ``flexExpiry`` / ``flexAnalytics`` /
  ``liveChain`` — the LLM never sees the cohort breakdown or the live
  broker prices.
- Doesn't surface the open-gap / holiday-class subsamples that are the
  whole point of the flex view.

This module reuses the LLM client, rate limiter, JSON parser, and the
DMS context loader from ``engine2_advisor`` (those parts are
flex-mode-agnostic) but ships its own sanitizer + prompt so the LLM
gets a complete flex briefing.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.config import FeatureFlags, get_flags
from backend.engine2_advisor import (
    _ADVISOR_REQUIRED_KEYS as _LEGACY_REQUIRED,
    _build_journal_context,
    _extract_dms_context,
    _get_openai_client,
    _load_todays_dms,
    _parse_llm_json,
    _rate_limiter,
)

LOG = logging.getLogger("engine2b.advisor")

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Flex advisor required keys — we add `cohortUsed` + `edgeAssessment` on
# top of the legacy ticket fields. ``tradeTicket.expiry`` is enforced by
# the prompt (must match ``flexExpiry.expiryDate``) but we do not require
# it as a schema field; missing keys just trip a fallback.
_FLEX_REQUIRED_KEYS = {
    "verdict",
    "confidence",
    "tradeTicket",
    "cohortUsed",
    "edgeAssessment",
    "wingWidthRationale",
    "riskContext",
    "entryPlan",
    "managementPlan",
    "exitRules",
    "keyRisks",
    "deskNote",
}


def _load_prompt(filename: str) -> Optional[str]:
    path = _PROMPTS_DIR / filename
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def sanitize_flex_for_llm(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build the LLM context block from a flex-engine payload.

    The shape mirrors what the prompt expects. We include the full
    ``flexAnalytics`` and ``liveChain`` blocks plus a small ``samples``
    list with the most relevant historical analogue rows so the LLM
    can quote specific dates if needed.
    """
    out: Dict[str, Any] = {}
    for key in (
        "asOfDate",
        "params",
        "underlying",
        "flexExpiry",
        "flexAnalytics",
        "liveChain",
        "weekendStress",
        "expectedMove",
        "strikeTargets",
        "current",
        "regime",
        "oddsLikeNow",
        "deskConsensus",
        "recommendation",
        "recSimple",
        "emPreference",
        "emBreachSummary",
    ):
        if key in payload:
            out[key] = payload[key]

    weeks = payload.get("weeks") or []
    if isinstance(weeks, list):
        # Sort by recency (most recent analogue first) and cap at 15 so the
        # context stays well under the 30k-char budget.
        try:
            sorted_weeks = sorted(weeks, key=lambda r: str(r.get("entryDate") or ""), reverse=True)
        except Exception:
            sorted_weeks = list(weeks)
        out["samples"] = sorted_weeks[:15]
        out["samplesTotal"] = len(weeks)

    return out


def _fallback_shell(reason: str) -> Dict[str, Any]:
    fb: Dict[str, Any] = {k: None for k in _FLEX_REQUIRED_KEYS}
    fb["_source"] = "fallback"
    fb["_fallback_reason"] = reason
    fb["verdict"] = "PASS"
    fb["confidence"] = 0
    fb["keyRisks"] = []
    fb["tradeTicket"] = {}
    fb["cohortUsed"] = {}
    return fb


def generate_flex_advisor(
    payload: Dict[str, Any],
    *,
    flags: Optional[FeatureFlags] = None,
) -> Dict[str, Any]:
    """Run the flex-expiry advisor.

    Returns a dict that mirrors the legacy advisor shape but adds
    ``cohortUsed`` + ``edgeAssessment`` so the desk can immediately see
    which subsample drove the verdict and whether there is a real edge
    vs a coin flip with theta.

    Always returns — never raises. On any failure the fallback shell is
    returned with ``_source = "fallback"`` and ``_fallback_reason`` set.
    """
    f = flags or get_flags()
    if not getattr(f, "ENGINE2_ADVISOR_ENABLED", False):
        return _fallback_shell("Advisor disabled (ENGINE2_ADVISOR_ENABLED=0)")

    prompt = _load_prompt("engine2b_advisor.txt")
    if not prompt:
        return _fallback_shell("Flex advisor prompt file missing")

    if not _rate_limiter.acquire():
        return _fallback_shell("Rate limited. Wait a moment and try again.")

    client = _get_openai_client()
    if client is None:
        return _fallback_shell("OpenAI client unavailable")

    dms = _load_todays_dms()
    # Trade journal is reused — it's a real-trades calibration signal,
    # not Friday-specific.
    journal_ctx = None
    try:
        from backend.engine2_trades import compute_trade_performance_digest
        perf = compute_trade_performance_digest()
        journal_ctx = _build_journal_context(perf) if perf.get("hasData") else None
    except Exception:
        journal_ctx = None

    ctx: Dict[str, Any] = {
        "flex": sanitize_flex_for_llm(payload),
        "market": _extract_dms_context(dms),
    }
    if journal_ctx:
        ctx["tradeJournal"] = journal_ctx

    payload_str = json.dumps(ctx, default=str)
    if len(payload_str) > 30000:
        payload_str = payload_str[:30000]

    model = str(getattr(f, "ENGINE2_ADVISOR_MODEL", "gpt-5.5") or "gpt-5.5").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=1,
            max_completion_tokens=4000,
            timeout=90,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content.strip()
        result = _parse_llm_json(content)
        if result is None:
            return _fallback_shell("LLM returned invalid JSON")
        missing = _FLEX_REQUIRED_KEYS - set(result.keys())
        if missing:
            LOG.warning("Engine 2b flex advisor: missing keys %s", sorted(missing))
            return _fallback_shell(f"LLM response missing keys: {sorted(missing)}")
        result["_source"] = "llm"
        result["_model"] = model
        result["_generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Hard guard: enforce that the ticket expiry matches the flex
        # expiry. The LLM occasionally still emits a Friday date.
        try:
            flex = payload.get("flexExpiry") or {}
            wanted = str(flex.get("expiryDate") or "")[:10]
            ticket = result.get("tradeTicket") or {}
            if wanted and isinstance(ticket, dict):
                got = str(ticket.get("expiry") or "")[:10]
                if got and got != wanted:
                    LOG.warning(
                        "engine2b advisor: ticket expiry %s != flex expiry %s; overriding.",
                        got, wanted,
                    )
                    ticket["expiry"] = wanted
                    result["tradeTicket"] = ticket
        except Exception:
            pass
        return result
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning("Engine 2b flex advisor LLM call failed: %s", reason)
        return _fallback_shell(reason)


__all__ = ["generate_flex_advisor", "sanitize_flex_for_llm"]
