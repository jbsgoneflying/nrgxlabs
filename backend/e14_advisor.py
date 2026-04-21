"""Engine 14 — IC Scenario LLM Advisor.

Single-purpose advisor for the E14 Command Deck. Narrates the
scenario simulator's outcome distribution + forward MC + exit
optimiser recommendation + MI v2 regime into a desk-ready verdict
(GO / HOLD / PASS) + structured risk bullets.

Separate from :mod:`backend.engine2_advisor` (which ``/reconcile``
still calls for back-compat) so the E14 prompt can foreground
scenario-specific fields (path-dependent replay outcomes, MAE pool,
exit grid) without getting lost in E2's weekly-scan context.

Reuses E1's OpenAI plumbing + rate-limiter for timeout / JSON-parse
guards, but maintains its own rate counter so E14 desk use doesn't
starve E1 and vice versa.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from backend.config import FeatureFlags, get_flags
from backend.e1_earnings_advisor import (
    _get_openai_client,
    _load_prompt,
    _parse_llm_json,
)

LOG = logging.getLogger("engine14.advisor")

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
# Rate limiter (dedicated so E14 use doesn't starve E1/E15)
# ---------------------------------------------------------------------------


class _E14RateLimiter:
    def __init__(self, max_calls_per_minute: int = 4):
        self._lock = threading.Lock()
        self._max = max_calls_per_minute
        self._timestamps: List[float] = []

    def update_max(self, new_max: int) -> None:
        with self._lock:
            self._max = max(1, int(new_max))

    def acquire(self) -> bool:
        with self._lock:
            now = time.time()
            self._timestamps = [t for t in self._timestamps if now - t < 60.0]
            if len(self._timestamps) >= self._max:
                return False
            self._timestamps.append(now)
            return True


_rate_limiter = _E14RateLimiter()


# ---------------------------------------------------------------------------
# Context shaping — keep payload compact
# ---------------------------------------------------------------------------


def _compact_scenario(sc: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only the fields the advisor actually reads.

    The full E14 payload can exceed 80 kB with all analogue rows +
    MTM timeline + Greeks attribution; we ship ~3 kB of decision-
    relevant context to the LLM.
    """
    entry = sc.get("entryState") or {}
    rm = sc.get("regimeMatchQuality") or {}
    dist = sc.get("outcomeDistribution") or {}
    dist_ci = sc.get("outcomeDistributionCI") or {}
    adj = sc.get("adjustedOutcomeDistribution") or {}
    conditioning = sc.get("conditioningModifiers") or {}
    exit_opt = sc.get("exitRulesOptimization") or {}
    sizing = sc.get("sizing") or {}
    mc = sc.get("mcResults") or {}
    regime_mi_v2 = (sc.get("regime") or {}).get("mi_v2") or entry.get("regimeMiV2")

    return {
        "request": sc.get("request"),
        "analoguesUsed":  sc.get("analoguesUsed"),
        "entryState": {
            "userSpot":     entry.get("userSpot"),
            "userEmPct":    entry.get("userEmPct"),
            "wingWidth":    entry.get("wingWidth"),
            "regimeBucket": entry.get("regimeBucket"),
            "regimeSource": entry.get("regimeSource"),
            "regimeMiV2":   regime_mi_v2,
        },
        "outcomeDistribution":  dist,
        "outcomeDistributionCI": dist_ci,
        "adjustedOutcomeDistribution": adj,
        "conditioningSummary":  sc.get("conditioningSummary"),
        "conditioningModifiers": {
            "netTailMultiplier":   conditioning.get("netTailMultiplier"),
            "netWinRateShiftPct":  conditioning.get("netWinRateShiftPct"),
            "notes":               conditioning.get("notes") or [],
        },
        "expectedValue":  sc.get("expectedValue"),
        "exitRulesOptimization": {
            "recommended":   exit_opt.get("recommended"),
            "topThree":      exit_opt.get("topThree"),
        },
        "sizing": {
            "consensus":     sizing.get("consensus"),
            "kelly":         sizing.get("kelly"),
            "fixedFractional": sizing.get("fixedFractional"),
        },
        "mcResults": {
            "n_sims":             mc.get("n_sims"),
            "mode":               mc.get("mode"),
            "conditioning_used":  mc.get("conditioning_used"),
            "pool_size_used":     mc.get("pool_size_used"),
            "pool_size_total":    mc.get("pool_size_total"),
            "notes":              mc.get("notes") or [],
        } if mc else {},
        "regimeMatchQuality": {
            "source": rm.get("source"),
            "bucket": rm.get("bucket"),
            "n":      rm.get("n"),
        },
    }


# ---------------------------------------------------------------------------
# Fallback shell (when LLM unavailable / rate-limited / errors)
# ---------------------------------------------------------------------------


def _fallback_shell(
    *, reason: str, scenario: Dict[str, Any], model: str,
) -> Dict[str, Any]:
    """Deterministic shell the UI can render even without LLM.

    The desk still sees a coherent "verdict + risks" card when
    ``OPENAI_API_KEY`` is missing or the rate-limiter throttles —
    matches the E1 / E15 fallback behaviour.
    """
    dist = scenario.get("outcomeDistribution") or {}
    breach_pct = float(((dist.get("breach") or {}).get("pct") or 0.0))
    full_pct = float(((dist.get("fullCollect") or {}).get("pct") or 0.0))
    early_pct = float(((dist.get("earlyTarget") or {}).get("pct") or 0.0))

    win_pct = full_pct + early_pct
    if win_pct >= 65.0 and breach_pct <= 10.0:
        verdict = "GO"
        stance = "bullish_for_trade"
    elif breach_pct >= 25.0:
        verdict = "PASS"
        stance = "bearish_for_trade"
    else:
        verdict = "HOLD"
        stance = "neutral"

    return {
        "verdict":  verdict,
        "confidence": 0.5,
        "stance":   stance,
        "narrative": (
            f"[Deterministic shell — LLM unavailable: {reason}]. "
            f"Historical analogue win rate {win_pct:.0f}% with "
            f"{breach_pct:.0f}% breach probability."
        ),
        "keyPoints": [
            f"Historical win rate: {win_pct:.0f}% (fullCollect + earlyTarget).",
            f"Breach probability: {breach_pct:.0f}%.",
            "Deterministic shell — desk should re-run when LLM is available.",
        ],
        "risks": [
            "LLM narrative not available — verdict is heuristic only.",
        ],
        "suggestedAdjustments": [],
        "deskNote": "",
        "plannedExitNote": "",
        "_source":     "fallback",
        "_model":      model,
        "_reason":     reason,
        "_advisorVersion": _ADVISOR_VERSION,
        "_generatedAt": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def _sanitize_llm_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Clamp fields to valid enumerations + cap list lengths."""
    out = dict(result)
    v = str(out.get("verdict") or "").strip().upper()
    if v not in _VERDICTS:
        v = "HOLD"
    out["verdict"] = v

    s = str(out.get("stance") or "").strip().lower()
    if s not in _STANCES:
        s = "neutral"
    out["stance"] = s

    try:
        conf = float(out.get("confidence") or 0.5)
    except Exception:
        conf = 0.5
    out["confidence"] = max(0.0, min(1.0, conf))

    for k in ("keyPoints", "risks", "suggestedAdjustments"):
        v_ = out.get(k)
        if isinstance(v_, list):
            out[k] = [str(x) for x in v_ if x][:8]
        else:
            out[k] = []

    for k in ("narrative", "deskNote", "plannedExitNote"):
        v_ = out.get(k)
        out[k] = str(v_) if v_ else ""

    return out


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def generate_scenario_advisor(
    *,
    scenario_payload: Dict[str, Any],
    flags: Optional[FeatureFlags] = None,
) -> Dict[str, Any]:
    """Run the E14 advisor against a scenario payload.

    Returns a dict with the whitelisted keys + ``_source``
    ("llm" | "fallback"), ``_model``, ``_advisorVersion``, and
    ``_generatedAt`` so the frontend can distinguish narrated vs
    heuristic output.
    """
    f = flags or get_flags()
    model = str(getattr(f, "E14_ADVISOR_MODEL", "") or "gpt-5.4").strip()
    scenario = dict(scenario_payload or {})

    if not scenario:
        return _fallback_shell(
            reason="No scenario payload provided.",
            scenario={}, model=model,
        )

    if not getattr(f, "E14_ADVISOR_ENABLED", True):
        return _fallback_shell(
            reason="E14_ADVISOR_ENABLED=0.",
            scenario=scenario, model=model,
        )

    _rate_limiter.update_max(int(getattr(f, "E14_ADVISOR_MAX_CALLS_PER_MINUTE", 4)))

    prompt = _load_prompt("e14_ic_scenario_advisor.txt")
    if not prompt:
        # If the prompt file doesn't exist yet, fall back gracefully.
        # The deployment step ships the prompt file separately.
        prompt = _DEFAULT_PROMPT

    if not _rate_limiter.acquire():
        return _fallback_shell(
            reason="Advisor rate-limited. Try again in a few seconds.",
            scenario=scenario, model=model,
        )

    client = _get_openai_client()
    if client is None:
        return _fallback_shell(
            reason="OpenAI client unavailable (missing OPENAI_API_KEY?).",
            scenario=scenario, model=model,
        )

    context = {"scenario": _compact_scenario(scenario)}
    try:
        context_str = json.dumps(context, default=str)
    except Exception as e:
        return _fallback_shell(
            reason=f"Context serialization failed: {type(e).__name__}: {e}",
            scenario=scenario, model=model,
        )

    if len(context_str) > 28000:
        LOG.info("engine14 advisor: context %d bytes, truncating", len(context_str))
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
        LOG.warning("engine14 advisor LLM call failed: %s", reason)
        return _fallback_shell(
            reason=reason, scenario=scenario, model=model,
        )

    parsed = _parse_llm_json(content)
    if parsed is None or not _REQUIRED_KEYS.issubset(set(parsed.keys())):
        LOG.warning("engine14 advisor: LLM response missing required keys")
        return _fallback_shell(
            reason="LLM returned invalid JSON.",
            scenario=scenario, model=model,
        )

    sanitized = _sanitize_llm_result(parsed)
    sanitized["_source"] = "llm"
    sanitized["_model"] = model
    sanitized["_advisorVersion"] = _ADVISOR_VERSION
    sanitized["_generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return sanitized


# ---------------------------------------------------------------------------
# Default prompt (used when data/prompts/e14_ic_scenario_advisor.txt is absent)
# ---------------------------------------------------------------------------


_DEFAULT_PROMPT = """You are the Raven-Tech Engine 14 desk advisor. You read the output of an IC
Scenario Simulator — a path-dependent replay of a user's specific iron condor
against historical analogues — and produce a concise, desk-ready narration.

Input JSON carries:
- request: user's four strikes + credit + entry/expiry dates.
- entryState: spot, 1-sigma expected-move %, regime (bucket + MI v2 HMM label).
- outcomeDistribution: fraction of analogues landing in each of
  {earlyTarget, fullCollect, whiteKnuckle, stopOut, breach}.
- adjustedOutcomeDistribution: same, after conditioning modifiers
  (macro calendar, dealer gamma, cross-asset stress, gap regime).
- conditioningModifiers: netTailMultiplier, netWinRateShiftPct, notes.
- exitRulesOptimization: recommended PT/SL pair + topThree.
- sizing: consensus of Kelly, fixed-fractional, max-DD methods.
- mcResults: forward Monte Carlo summary (n_sims, mode, pool size, notes).

Your job:
1. Produce a verdict: GO | HOLD | PASS. Only GO if the Command Deck's
   win-rate is >= 60% AND breach probability is <= 12% AND the MI v2 regime
   is not Stressed.
2. Confidence: 0.0–1.0, calibrated to analogue count + MC conditioning tier.
3. Stance: bullish_for_trade | neutral | bearish_for_trade — reflects
   whether the edge is real, not the market direction.
4. narrative: 2–3 sentences for the desk.
5. keyPoints: 3–5 bullets (analogue count, breach %, MAE tail, regime chip).
6. risks: 2–4 bullets — what would make you change the verdict.
7. suggestedAdjustments: up to 3 concrete strike/wing/credit tweaks.
8. deskNote: one-sentence takeaway for the trade journal.
9. plannedExitNote: one sentence on the exit optimizer's recommendation.

Respond ONLY with valid JSON containing those nine keys."""


__all__ = [
    "generate_scenario_advisor",
]
