"""Engine 15 — Earnings IC Scenario LLM Advisor.

Single LLM call that reads both the Engine 1 payload (ticker-level VRP /
entry quality) and the Engine 15 replay payload (user's proposed wings +
historical analogues) and emits a unified desk verdict.

Design: reuse Engine 1's OpenAI plumbing (``_get_openai_client``,
``_parse_llm_json``, ``_AdvisorRateLimiter``) so we share the same
timeout, rate-limit behavior, and JSON-parse guards. Only the context
assembly + prompt are new.

The returned shape is guaranteed by a whitelist + deterministic
fallback: downstream UI code can rely on every top-level key existing.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, Dict, List, Optional

from backend.config import FeatureFlags, get_flags
from backend.e1_earnings_advisor import (
    _get_openai_client,
    _load_prompt,
    _parse_llm_json,
    _rate_limiter,
)

LOG = logging.getLogger("engine15.advisor")

_ADVISOR_VERSION = "0.1.0"

_REQUIRED_KEYS = {
    "verdict",
    "confidence",
    "stance",
    "narrative",
    "keyPoints",
    "risks",
    "suggestedAdjustments",
    "deskNote",
    "plannedExitNote",
}

_VERDICTS = {"GO", "HOLD", "PASS"}
_STANCES = {"bullish_for_trade", "neutral", "bearish_for_trade"}


# ---------------------------------------------------------------------------
# Context shaping — keep payload compact
# ---------------------------------------------------------------------------


def _compact_engine1(e1: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Reduce the E1 full payload to a prompt-friendly summary.

    Large arrays (per-event history, skew ladders, technical series) are
    dropped: the simulator payload already encodes the historical signal
    by replaying them. We only keep tagged scalars the LLM reasons over.
    """
    if not e1:
        return {}
    current = e1.get("current") or {}
    vrp = e1.get("vrpAnalysis") or {}
    dc = e1.get("deskConsensus") or {}
    em_breach = e1.get("emBreachSummary") or {}
    next_event = e1.get("nextEvent") or {}
    eq = e1.get("entryQuality") or {}
    regime = e1.get("regime") or {}
    return {
        "vrpScore": vrp.get("vrpScore"),
        "meanRatio": vrp.get("meanRatio"),
        "stdRatio": vrp.get("stdRatio"),
        "ivElevation": vrp.get("ivElevation"),
        "sampleSize": vrp.get("sampleSize"),
        "confidence": vrp.get("confidence"),
        "emPct": current.get("impliedMovePct"),
        "stockPrice": current.get("stockPrice"),
        "anncTod": next_event.get("anncTod") or next_event.get("timing"),
        "nextEventDate": next_event.get("earnDate") or next_event.get("date"),
        "deskConsensus": dc.get("verdict") or dc.get("consensus"),
        "deskConsensusScore": dc.get("score"),
        "deskConsensusReasons": (dc.get("reasons") or [])[:4],
        "emBreachPct": em_breach.get("breachRatePct") or em_breach.get("breachPct"),
        "emBreachN": em_breach.get("n"),
        "entryQualityScore": eq.get("entryQuality") or eq.get("score"),
        "entryQualityFlags": eq.get("flags") or [],
        "regimeBucket": regime.get("regime") or regime.get("bucket"),
        "historyN": len(e1.get("events") or []),
    }


def _compact_scenario(sc: Dict[str, Any]) -> Dict[str, Any]:
    """Trim the scenario payload so we stay well under the LLM context budget.

    We drop the raw ``engine1`` echo (already compacted by caller), the
    ``outcomeDistributionCI`` bootstrap details, and the full analogue
    list (keep the first 10 events). All summary-level statistics are
    preserved verbatim so the LLM still reasons over the full picture.
    """
    keep: Dict[str, Any] = {
        "request": sc.get("request"),
        "eventsUsed": sc.get("eventsUsed"),
        "eventsConsidered": sc.get("eventsConsidered"),
        "entryState": sc.get("entryState"),
        "plannedExit": sc.get("plannedExit"),
        "fillModel": sc.get("fillModel"),
        "outcomeDistribution": sc.get("outcomeDistribution"),
        "adjustedOutcomeDistribution": sc.get("adjustedOutcomeDistribution"),
        "conditioningModifiers": sc.get("conditioningModifiers"),
        "conditioningSummary": sc.get("conditioningSummary"),
        "mtmTimeline": sc.get("mtmTimeline"),
        "expectedValue": sc.get("expectedValue"),
        "exitRulesOptimization": sc.get("exitRulesOptimization"),
        "sizing": sc.get("sizing"),
        "greeksAttribution": sc.get("greeksAttribution"),
        "dataQuality": {
            k: (sc.get("dataQuality") or {}).get(k)
            for k in (
                "eventsConsidered",
                "eventsWithFullChain",
                "pathsPriced",
                "minEventsMet",
            )
        },
        "notes": (sc.get("notes") or [])[:8],
    }
    events = sc.get("matchedEvents") or []
    keep["matchedEvents"] = events[:10]
    if len(events) > 10:
        keep["matchedEventsNoteTruncated"] = f"(+{len(events) - 10} more not shown)"
    drops = sc.get("droppedEvents") or []
    if drops:
        keep["droppedEventCount"] = len(drops)
        keep["droppedReasonsSample"] = list({
            (d.get("reason") or "").split(" (")[0] for d in drops[:10] if d.get("reason")
        })
    return keep


def _fallback_shell(
    *,
    reason: str,
    scenario: Dict[str, Any],
    e1_summary: Dict[str, Any],
    model: str,
) -> Dict[str, Any]:
    """Deterministic structured fallback when the LLM is unavailable.

    Populates the required envelope from the replay payload so the UI
    always renders something useful (verdict mirrors Engine 1's deskConsensus
    where available, otherwise a conservative "HOLD").
    """
    ev = scenario.get("expectedValue") or {}
    outcome = scenario.get("outcomeDistribution") or {}
    events_used = int(scenario.get("eventsUsed") or 0)
    full_pct = float(((outcome.get("fullCollect") or {}).get("pct")) or 0.0)
    early_pct = float(((outcome.get("earlyTarget") or {}).get("pct")) or 0.0)
    breach_pct = float(((outcome.get("breach") or {}).get("pct")) or 0.0)
    stop_pct = float(((outcome.get("stopOut") or {}).get("pct")) or 0.0)
    mean_pnl = float(ev.get("meanPnlPct") or 0.0)

    stance = "neutral"
    verdict = "HOLD"
    if events_used >= 8:
        if mean_pnl > 5 and (full_pct + early_pct) > 55 and breach_pct < 30:
            verdict, stance = "GO", "bullish_for_trade"
        elif mean_pnl < -5 or breach_pct > 45 or stop_pct > 40:
            verdict, stance = "PASS", "bearish_for_trade"

    planned = scenario.get("plannedExit") or {}
    key_points: List[str] = [
        f"Replay used {events_used} events; meanP&L {mean_pnl:+.1f}%",
        f"Full+Early {full_pct + early_pct:.0f}% / Breach {breach_pct:.0f}% / Stop {stop_pct:.0f}%",
    ]
    if e1_summary.get("vrpScore") is not None:
        key_points.append(f"E1 VRP score: {e1_summary.get('vrpScore')}")
    if e1_summary.get("ivElevation") is not None:
        key_points.append(f"IV elevation: {e1_summary.get('ivElevation')}")

    risks: List[str] = []
    if events_used < 8:
        risks.append("Thin sample — replay is advisory only.")
    if breach_pct > 30:
        risks.append(f"Breach rate {breach_pct:.0f}% suggests widening wings.")

    return {
        "verdict": verdict,
        "confidence": 40 if events_used >= 8 else 20,
        "stance": stance,
        "narrative": (
            f"[fallback] Replay used {events_used} same-ticker events; "
            f"mean P&L {mean_pnl:+.1f}%, full+early {full_pct + early_pct:.0f}%, "
            f"breach {breach_pct:.0f}%. LLM advisor unavailable ({reason})."
        )[:600],
        "keyPoints": key_points[:6],
        "risks": risks,
        "suggestedAdjustments": [],
        "deskNote": (
            f"[fallback] {reason[:150]}"
        ),
        "plannedExitNote": (
            planned.get("fidelityCaveat") or ""
        )[:200],
        "_source": "fallback",
        "_fallback_reason": reason,
        "_model": model,
        "_advisorVersion": _ADVISOR_VERSION,
        "_generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _sanitize_llm_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce the LLM's JSON into the agreed envelope with hard guards."""
    out: Dict[str, Any] = {}

    verdict = str(result.get("verdict") or "").strip().upper()
    out["verdict"] = verdict if verdict in _VERDICTS else "HOLD"

    try:
        conf = int(float(result.get("confidence") or 0))
    except (TypeError, ValueError):
        conf = 0
    out["confidence"] = max(0, min(100, conf))

    stance = str(result.get("stance") or "").strip().lower()
    out["stance"] = stance if stance in _STANCES else "neutral"

    out["narrative"] = (str(result.get("narrative") or "")).strip()[:600]
    out["deskNote"] = (str(result.get("deskNote") or "")).strip()[:200]
    out["plannedExitNote"] = (str(result.get("plannedExitNote") or "")).strip()[:200]

    def _list_of_str(v: Any, maxlen: int, cap: int) -> List[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip()[:maxlen] for x in v[:cap] if str(x).strip()]

    out["keyPoints"] = _list_of_str(result.get("keyPoints"), 140, 6)
    out["risks"] = _list_of_str(result.get("risks"), 200, 5)

    adjs_in = result.get("suggestedAdjustments") or []
    adjs_out: List[Dict[str, str]] = []
    if isinstance(adjs_in, list):
        for a in adjs_in[:5]:
            if not isinstance(a, dict):
                continue
            t = str(a.get("type") or "").strip()
            sug = str(a.get("suggestion") or "").strip()
            rat = str(a.get("rationale") or "").strip()
            if not (t and sug):
                continue
            adjs_out.append({
                "type": t[:24],
                "suggestion": sug[:160],
                "rationale": rat[:200],
            })
    out["suggestedAdjustments"] = adjs_out

    return out


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def generate_scenario_analysis(
    *,
    engine1_payload: Optional[Dict[str, Any]] = None,
    scenario_payload: Optional[Dict[str, Any]] = None,
    flags: Optional[FeatureFlags] = None,
) -> Dict[str, Any]:
    """Produce a unified Engine 15 advisor verdict.

    ``engine1_payload`` may be None if the caller didn't retain the full
    E1 body (the scenario always stores a compact ``engine1Summary``
    alongside, which we fall back to).
    """
    f = flags or get_flags()
    model = str(getattr(f, "ENGINE15_ADVISOR_MODEL", "") or "gpt-5.4").strip()
    scenario = dict(scenario_payload or {})
    # Prefer the compact summary the simulator stored; back off to a
    # freshly shaped compact from the raw E1 payload; finally an empty dict.
    e1_summary = (
        dict(scenario.get("engine1Summary") or {})
        or _compact_engine1(engine1_payload)
        or {}
    )

    if not scenario:
        return _fallback_shell(
            reason="No scenario payload provided.",
            scenario={}, e1_summary=e1_summary, model=model,
        )

    if not getattr(f, "ENGINE15_ADVISOR_ENABLED", True):
        return _fallback_shell(
            reason="ENGINE15_ADVISOR_ENABLED=0.",
            scenario=scenario, e1_summary=e1_summary, model=model,
        )

    prompt = _load_prompt("e15_earnings_scenario_advisor.txt")
    if not prompt:
        return _fallback_shell(
            reason="Prompt file missing.",
            scenario=scenario, e1_summary=e1_summary, model=model,
        )

    if not _rate_limiter.acquire():
        return _fallback_shell(
            reason="Advisor rate-limited. Try again in a few seconds.",
            scenario=scenario, e1_summary=e1_summary, model=model,
        )

    client = _get_openai_client()
    if client is None:
        return _fallback_shell(
            reason="OpenAI client unavailable (missing OPENAI_API_KEY?).",
            scenario=scenario, e1_summary=e1_summary, model=model,
        )

    # Build the payload. We avoid json.dumps(..., default=str) calls on
    # custom objects — by this point the router has already produced
    # plain-dict payloads.
    context = {
        "engine1Summary": e1_summary,
        "scenario": _compact_scenario(scenario),
    }
    try:
        context_str = json.dumps(context, default=str)
    except Exception as e:
        return _fallback_shell(
            reason=f"Context serialization failed: {type(e).__name__}: {e}",
            scenario=scenario, e1_summary=e1_summary, model=model,
        )

    # Hard cap to stay within the model's practical context — the
    # Engine 14 advisor uses ~30k, we target similar.
    if len(context_str) > 28000:
        LOG.info("engine15 advisor: context %d bytes, truncating", len(context_str))
        context_str = context_str[:28000]

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": context_str},
            ],
            temperature=0.25,
            max_completion_tokens=1400,
            timeout=45,
            response_format={"type": "json_object"},
        )
        content = (response.choices[0].message.content or "").strip()
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning("engine15 advisor LLM call failed: %s", reason)
        return _fallback_shell(
            reason=reason, scenario=scenario, e1_summary=e1_summary, model=model,
        )

    parsed = _parse_llm_json(content)
    if parsed is None or not _REQUIRED_KEYS.issubset(set(parsed.keys())):
        LOG.warning("engine15 advisor: LLM response missing required keys")
        return _fallback_shell(
            reason="LLM returned invalid JSON.",
            scenario=scenario, e1_summary=e1_summary, model=model,
        )

    sanitized = _sanitize_llm_result(parsed)
    sanitized["_source"] = "llm"
    sanitized["_model"] = model
    sanitized["_advisorVersion"] = _ADVISOR_VERSION
    sanitized["_generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return sanitized
