"""Engine 1 — Earnings IC (Vol Crush) LLM Trade Advisor.

Mirrors the Engine 2 advisor pattern but purpose-built for single-name
earnings premium harvesting.  Uses VRP analysis, EM x Wing grid, entry
quality, and cross-ticker learning journal.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.config import FeatureFlags, get_flags
from backend.daily_market_state import load_dms
from backend.redis_store import get_store_optional

LOG = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_ADVISOR_REQUIRED_KEYS = {
    "verdict", "confidence", "tradeTicket", "vrpRationale",
    "wingWidthRationale", "riskContext", "entryPlan",
    "managementPlan", "exitRules", "keyRisks", "deskNote",
}


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _AdvisorRateLimiter:
    def __init__(self, max_calls_per_minute: int = 4):
        self._lock = threading.Lock()
        self._max = max_calls_per_minute
        self._timestamps: List[float] = []

    def acquire(self) -> bool:
        with self._lock:
            now = time.time()
            self._timestamps = [t for t in self._timestamps if now - t < 60.0]
            if len(self._timestamps) >= self._max:
                return False
            self._timestamps.append(now)
            return True


_rate_limiter = _AdvisorRateLimiter()


# ---------------------------------------------------------------------------
# OpenAI client (lazy singleton)
# ---------------------------------------------------------------------------

def _get_openai_client():
    try:
        import openai  # type: ignore
    except Exception:
        return None
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        return openai.OpenAI(api_key=api_key)
    except Exception:
        return None


def _parse_llm_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start < 0:
            return None
        depth = 0
        for i in range(start, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cleaned[start : i + 1])
                    except json.JSONDecodeError:
                        return None
    return None


def _load_prompt(filename: str) -> Optional[str]:
    path = _PROMPTS_DIR / filename
    if not path.exists():
        LOG.warning("Prompt file not found: %s", path)
        return None
    return path.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# DMS integration (shared with E2 advisor)
# ---------------------------------------------------------------------------

def _load_todays_dms() -> Optional[Dict[str, Any]]:
    store = get_store_optional()
    if store is None:
        return None
    today_str = dt.date.today().strftime("%Y-%m-%d")
    dms = load_dms(today_str, store)
    if dms is None:
        return None
    return dms.to_dict()


def _extract_dms_context(dms_dict: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not dms_dict:
        return {}
    return {
        "regime": dms_dict.get("regime", {}),
        "vol_state": dms_dict.get("vol_state", {}),
        "composite_stress": (dms_dict.get("cross_asset_stress") or {}).get("composite_score"),
        "composite_label": (dms_dict.get("cross_asset_stress") or {}).get("composite_label"),
        "active_themes": dms_dict.get("news_themes", []),
    }


# ---------------------------------------------------------------------------
# Context sanitization (trim payload for LLM token budget)
# ---------------------------------------------------------------------------

def _sanitize_breach_for_llm(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Trim the full breach payload to the fields the LLM needs."""
    out: Dict[str, Any] = {}
    for key in (
        "ticker", "summary", "summaryDecision", "baseline",
        "current", "regime", "gapVsCtc", "nextEvent",
    ):
        if key in payload:
            out[key] = payload[key]

    # Skew: just the current snapshot
    skew = payload.get("skewOverlay")
    if isinstance(skew, dict):
        out["skewCurrent"] = skew.get("current")

    # Earnings hold risk: top-level summary only
    ehr = payload.get("earningsHoldRisk")
    if isinstance(ehr, dict):
        out["earningsHoldRisk"] = {
            k: ehr[k] for k in ("unconditional", "conditional_flat_open", "drift", "sample_size")
            if k in ehr
        }

    # Dealer gamma (ticker level)
    tdg = payload.get("tickerDealerGamma")
    if isinstance(tdg, dict) and tdg.get("enabled"):
        dg = tdg.get("dealerGamma")
        if dg:
            out["tickerDealerGamma"] = {
                "netGammaSign": dg.get("netGammaSign"),
                "flipPoint": dg.get("flipPoint"),
            }

    # Technicals snapshot — compact signals + narrative summary + key levels
    tech = payload.get("technicals")
    if isinstance(tech, dict) and tech.get("enabled"):
        t_snap: Dict[str, Any] = {}
        sigs = tech.get("signals")
        if isinstance(sigs, dict) and sigs.get("enabled"):
            t_snap["signals"] = sigs
        narr = tech.get("narrative")
        if isinstance(narr, dict):
            t_snap["narrativeSummary"] = narr.get("summary")
            t_snap["narrativeBullets"] = narr.get("bullets")
        rsi = tech.get("rsi")
        if isinstance(rsi, dict) and rsi.get("enabled"):
            t_snap["rsi14"] = rsi.get("value")
            t_snap["rsiState"] = rsi.get("state")
        macd = tech.get("macd")
        if isinstance(macd, dict) and macd.get("enabled"):
            t_snap["macd"] = macd.get("macd")
            t_snap["macdSignal"] = macd.get("signalLine")
            t_snap["macdHist"] = macd.get("hist")
        boll = tech.get("bollinger")
        if isinstance(boll, dict) and boll.get("enabled"):
            t_snap["bollingerMid"] = boll.get("mid")
            t_snap["bollingerUpper"] = boll.get("upper")
            t_snap["bollingerLower"] = boll.get("lower")
            t_snap["bollingerPctB"] = boll.get("percentB")
        dist = tech.get("distances")
        if isinstance(dist, dict):
            lvls = dist.get("levels")
            if isinstance(lvls, dict):
                sr: Dict[str, Any] = {}
                for lk in ("ema50", "ema200", "bbMid", "bbUpper", "bbLower"):
                    lv = lvls.get(lk)
                    if isinstance(lv, dict):
                        sr[lk] = {"price": lv.get("price"), "distPct": lv.get("distancePct")}
                if sr:
                    t_snap["supportResistance"] = sr
        if t_snap:
            out["technicals"] = t_snap

    return out


# ---------------------------------------------------------------------------
# Cross-ticker journal context builder
# ---------------------------------------------------------------------------

def _build_e1_journal_context(digest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Distil cross-ticker performance digest for the LLM."""
    if not digest.get("hasData") or digest.get("totalClosed", 0) == 0:
        return None

    ctx: Dict[str, Any] = {
        "totalClosed": digest["totalClosed"],
        "winRate": digest.get("winRate"),
        "avgPnl": digest.get("avgPnl"),
        "medianPnl": digest.get("medianPnl"),
        "totalPnl": digest.get("totalPnl"),
        "riskTendency": digest.get("riskTendency"),
    }

    for bucket_key in ("byVrpBucket", "byBreachBucket", "byEm", "byWing", "byTiming", "byRegime"):
        val = digest.get(bucket_key)
        if val:
            ctx[bucket_key] = val

    cal = digest.get("verdictCalibration")
    if cal:
        ctx["verdictCalibration"] = cal

    # v2 enrichments
    recent = digest.get("recentTrades")
    if recent:
        ctx["recentTrades"] = recent[:10]

    insights = digest.get("patternInsights")
    if insights:
        ctx["patternInsights"] = insights

    tags = digest.get("tagAnalysis")
    if tags:
        ctx["tagAnalysis"] = tags

    streak = digest.get("streakInfo")
    if streak:
        ctx["streakInfo"] = streak

    trend = digest.get("weeklyTrend")
    if trend:
        ctx["weeklyTrend"] = trend

    vrp_cal = digest.get("vrpCalibration")
    if vrp_cal and vrp_cal.get("hasData"):
        ctx["vrpCalibration"] = vrp_cal

    breach_cal = digest.get("breachCalibrationByEm")
    if breach_cal:
        ctx["breachCalibrationByEm"] = breach_cal

    return ctx


# ---------------------------------------------------------------------------
# Main LLM Trade Analysis
# ---------------------------------------------------------------------------

def generate_e1_trade_analysis(
    *,
    breach_payload: Dict[str, Any],
    vrp_analysis: Dict[str, Any],
    width_analysis: List[Dict[str, Any]],
    entry_quality: Dict[str, Any],
    desk_consensus: Dict[str, Any],
    em_preference: Dict[str, Any],
    flags: Optional[FeatureFlags] = None,
) -> Dict[str, Any]:
    """Run the full earnings IC trade advisor: context assembly + LLM verdict."""
    f = flags or get_flags()

    fallback: Dict[str, Any] = {k: None for k in _ADVISOR_REQUIRED_KEYS}
    fallback["_source"] = "fallback"
    fallback["verdict"] = desk_consensus.get("verdict", "PASS")
    fallback["confidence"] = 0
    fallback["keyRisks"] = []
    fallback["tradeTicket"] = {}

    if not f.E1_ADVISOR_ENABLED:
        fallback["_fallback_reason"] = "E1 Advisor disabled"
        return fallback

    system_prompt = _load_prompt("e1_earnings_advisor.txt")
    if not system_prompt:
        fallback["_fallback_reason"] = "Prompt file missing"
        return fallback

    if not _rate_limiter.acquire():
        fallback["_fallback_reason"] = "Rate limited. Wait a moment and try again."
        return fallback

    client = _get_openai_client()
    if client is None:
        fallback["_fallback_reason"] = "OpenAI client unavailable"
        return fallback

    dms_dict = _load_todays_dms()

    # Cross-ticker performance journal
    trade_journal = None
    try:
        from backend.e1_earnings_trades import compute_e1_trade_performance_digest
        perf_digest = compute_e1_trade_performance_digest()
        trade_journal = _build_e1_journal_context(perf_digest) if perf_digest.get("hasData") else None
    except Exception as e:
        LOG.debug("E1 trade journal unavailable: %s", e)

    context: Dict[str, Any] = {
        "vrpAnalysis": vrp_analysis,
        "widthAnalysis": width_analysis,
        "entryQuality": entry_quality,
        "deskConsensus": desk_consensus,
        "emPreference": em_preference,
        "scan": _sanitize_breach_for_llm(breach_payload),
        "market": _extract_dms_context(dms_dict),
    }
    if trade_journal:
        context["tradeJournal"] = trade_journal

    payload_str = json.dumps(context, default=str)
    if len(payload_str) > 30000:
        payload_str = payload_str[:30000]

    model = str(f.E1_ADVISOR_MODEL or "gpt-5.5").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=1,
            max_completion_tokens=5000,
            timeout=60,
            response_format={"type": "json_object"},
        )

        raw_content = response.choices[0].message.content or ""
        content = raw_content.strip()
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        usage = getattr(response, "usage", None)
        result = _parse_llm_json(content)

        if result is None:
            LOG.warning(
                "E1 advisor: LLM response unparseable; finish=%s usage=%s raw(first400)=%r",
                finish_reason, getattr(usage, "model_dump", lambda: usage)() if usage else None,
                content[:400],
            )
            fallback["_fallback_reason"] = (
                f"LLM returned unparseable JSON (finish={finish_reason}, "
                f"len={len(content)}). Head: {content[:200]!r}"
            )
            return fallback

        # Tolerant schema: gpt-5.5 sometimes omits optional rationale fields.
        # Accept any payload that parsed, fill missing keys from the fallback
        # template, and surface which keys were absent for telemetry.
        missing = _ADVISOR_REQUIRED_KEYS - set(result.keys())
        if missing:
            LOG.info("E1 advisor: LLM omitted keys=%s; backfilling from defaults", sorted(missing))
            for k in missing:
                result[k] = fallback.get(k)
            result["_partial_keys_missing"] = sorted(missing)

        result["_source"] = "llm"
        result["_model"] = model
        result["_generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return result

    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning("E1 advisor LLM call failed: %s", reason)
        fallback["_fallback_reason"] = reason
        return fallback


# ---------------------------------------------------------------------------
# Post-mortem generation
# ---------------------------------------------------------------------------

_POST_MORTEM_REQUIRED_KEYS = {
    "vrpThesis", "category", "lesson", "confidenceInAssessment", "deskNote",
}


def generate_e1_post_mortem(
    trade: Dict[str, Any],
    *,
    flags: Optional[FeatureFlags] = None,
    journal_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate an LLM post-mortem for a closed Engine 1 earnings trade."""
    f = flags or get_flags()
    fallback: Dict[str, Any] = {
        "category": "push",
        "lesson": "Insufficient data for automated post-mortem.",
        "confidenceInAssessment": 0,
        "_source": "fallback",
    }

    if trade.get("status") != "closed":
        fallback["_fallback_reason"] = "Trade not closed"
        return fallback

    system_prompt = _load_prompt("e1_post_mortem.txt")
    if not system_prompt:
        fallback["_fallback_reason"] = "Post-mortem prompt file missing"
        return fallback

    if not _rate_limiter.acquire():
        fallback["_fallback_reason"] = "Rate limited"
        return fallback

    client = _get_openai_client()
    if client is None:
        fallback["_fallback_reason"] = "OpenAI client unavailable"
        return fallback

    context: Dict[str, Any] = {
        "ticker": trade.get("ticker"),
        "entry": trade.get("entry", {}),
        "entryContext": trade.get("entryContext", {}),
        "marketSnapshot": trade.get("marketSnapshot", {}),
        "vrpSnapshot": trade.get("vrpSnapshot", {}),
        "breachSnapshot": trade.get("breachSnapshot", {}),
        "predictionSnapshot": trade.get("predictionSnapshot", {}),
        "advisorVerdict": trade.get("advisorVerdict"),
        "checkIns": (trade.get("checkIns") or [])[-3:],
        "outcome": trade.get("outcome", {}),
        "closeReason": trade.get("closeReason"),
    }
    if journal_context:
        context["tradeJournal"] = journal_context

    payload_str = json.dumps(context, default=str)
    if len(payload_str) > 25000:
        payload_str = payload_str[:25000]

    model = str(f.E1_ADVISOR_MODEL or "gpt-5.5").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=1,
            max_completion_tokens=800,
            timeout=30,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content.strip()
        result = _parse_llm_json(content)

        if result is None:
            LOG.warning("E1 post-mortem: LLM response unparseable; raw (first 400): %s", content[:400])
            fallback["_fallback_reason"] = "LLM returned unparseable JSON"
            return fallback

        missing = _POST_MORTEM_REQUIRED_KEYS - set(result.keys())
        if missing:
            LOG.info("E1 post-mortem: LLM omitted keys=%s; backfilling from defaults", sorted(missing))
            for k in missing:
                result[k] = fallback.get(k)
            result["_partial_keys_missing"] = sorted(missing)

        result["_source"] = "llm"
        result["_model"] = model
        result["_generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return result

    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning("E1 post-mortem LLM call failed: %s", reason)
        fallback["_fallback_reason"] = reason
        return fallback


# ---------------------------------------------------------------------------
# Live Review v2 — phase-tuned narrative for an OPEN earnings IC trade
# ---------------------------------------------------------------------------

_LIVE_REVIEW_REQUIRED_KEYS = {"verdict", "confidence", "narrative", "keyPoints", "riskFactors", "deskNote"}


def _live_review_system_prompt(phase: str) -> str:
    """Phase-tuned system prompt for the live-review LLM call.

    Each phase has a different decision the desk wants from the model:

    - ``pre_event`` (entry → close T-1): the position is already on; the
      desk wants to know if anything has materially changed since entry
      that should make them bail before the print. Bias slightly toward
      HOLD unless the evidence is bad — exiting before the print abandons
      the carry-trade thesis.
    - ``pre_open`` (close T-1 → 9:30 T-0): the print may be out (AMC) or
      pending (BMO). Use AH/PM gap, overnight news, and peer reactions.
      The decision is "what's our open-trade plan once the bell rings?".
    - ``post_open`` (9:30 T-0 → expiry): gap is realized. Decide between
      exiting at the open mid for partial credit, holding for full IV
      crush, or taking the loss now before theta works against us.
    """
    base = (
        "You are a senior vol-crush options desk analyst reviewing an OPEN "
        "earnings iron condor. Be concise, decisive, and cite the numbers. "
        "Return STRICT JSON with keys: verdict (one of HOLD|ADJUST|CUT), "
        "confidence (0.0-1.0 float), narrative (2-3 sentences), "
        "keyPoints (array of 2-4 short bullets), riskFactors (array of 1-3 "
        "short bullets), deskNote (one-sentence trader-style summary). "
        "Do not invent numbers; use the evidence packet."
    )
    if phase == "pre_event":
        return base + (
            " Phase: PRE_EVENT. The desk holds the position now and wants to "
            "know if anything has changed since entry that justifies closing "
            "BEFORE the print. Bias HOLD unless the evidence is materially "
            "negative (regime shift, high-priority news, breached short, "
            "replay P10 deeply negative)."
        )
    if phase == "pre_open":
        return base + (
            " Phase: PRE_OPEN. Print may already be out (AMC) or pending "
            "(BMO). Read AH/PM data, overnight news, and analogues. The "
            "decision is: do we keep the position into the open, cut at "
            "the bell on a mid-fill, or pre-roll? If the AH gap exceeds "
            "the implied move, lean CUT. If gap is contained and analogues "
            "support full crush, lean HOLD."
        )
    if phase == "post_open":
        return base + (
            " Phase: POST_OPEN. Gap is realized. Recommend HOLD only if the "
            "structure is intact and the replay shows continued vol crush "
            "value vs the current PnL mark. Recommend CUT if a short is "
            "breached or replay P10 is materially worse than current mark. "
            "ADJUST is reserved for one-side breach with intact other side."
        )
    return base


def _summarize_evidence_for_llm(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Compact, token-efficient view of the evidence packet for the LLM.

    Strips long arrays (full MTM curves, all news headlines) down to what
    the model actually needs to make the verdict.
    """
    out: Dict[str, Any] = {}
    if isinstance(evidence.get("spot"), dict):
        out["spot"] = evidence["spot"]
    iv = evidence.get("iv")
    if isinstance(iv, dict):
        out["iv"] = {k: iv.get(k) for k in ("atEntry", "now", "crushSoFarPct") if iv.get(k) is not None}
    out["statusChip"] = evidence.get("statusChip")

    regime = evidence.get("regime") or {}
    if isinstance(regime, dict) and regime.get("available", True):
        out["regime"] = {k: regime.get(k) for k in ("atEntry", "now", "score", "drift") if regime.get(k) is not None}

    news = evidence.get("news") or {}
    if isinstance(news, dict) and news.get("available", True):
        # Only top headlines to keep prompt small.
        heads = news.get("headlines") or []
        out["news"] = {
            "counts": news.get("counts") or {},
            "topHeadlines": [
                {"title": h.get("title"), "priority": h.get("priority")}
                for h in heads[:6]
            ],
        }
    macro = evidence.get("macro") or {}
    if isinstance(macro, dict) and macro.get("flags"):
        out["macroFlags"] = macro.get("flags")

    analogues = evidence.get("analogues") or {}
    if isinstance(analogues, dict) and analogues.get("available", True):
        out["analogues"] = {
            "n": analogues.get("nEvents"),
            "ladder": analogues.get("ladder"),
            "rateAtEmPct": analogues.get("rateAtEmPct"),
            "tailBias": analogues.get("tailBias"),
        }
    replay = evidence.get("replay") or {}
    if isinstance(replay, dict) and replay.get("available", True):
        out["replay"] = {
            "pathsCount": replay.get("pathsCount"),
            "p10PnlPct": replay.get("p10PnlPct"),
            "p50PnlPct": replay.get("p50PnlPct"),
            "p90PnlPct": replay.get("p90PnlPct"),
            "fullCollectRate": replay.get("fullCollectRate"),
            "fullLossRate": replay.get("fullLossRate"),
            "creditRichness": (replay.get("creditRichness") or {}).get("verdict") if replay.get("creditRichness") else None,
        }
    return out


def generate_live_review_v2(
    *,
    phase: str,
    ticker: str,
    fields: Dict[str, Any],
    evidence: Dict[str, Any],
    days_to_earnings: Optional[int],
    pre_verdict: str,
    pre_confidence: float,
    flags: Optional[FeatureFlags] = None,
) -> Optional[Dict[str, Any]]:
    """Phase-tuned LLM narrative for the Engine 1 Live Review v2 card.

    Returns ``None`` when the LLM is unavailable or returns invalid JSON
    — the caller falls back to the rule-based pre-verdict and ladder.
    """
    f = flags or get_flags()

    client = _get_openai_client()
    if client is None:
        LOG.info("E1 live review v2: OpenAI client unavailable — using rule-based fallback only")
        return {"_skipReason": "openai_client_unavailable"}

    if not _rate_limiter.acquire():
        LOG.info("E1 live review v2: rate-limited; skipping LLM call")
        return {"_skipReason": "rate_limited"}

    payload = {
        "phase": phase,
        "ticker": ticker,
        "trade": {
            "shortPut": fields.get("shortPut"),
            "longPut": fields.get("longPut"),
            "shortCall": fields.get("shortCall"),
            "longCall": fields.get("longCall"),
            "entryCredit": fields.get("entryCredit"),
            "spotAtEntry": fields.get("spotAtEntry"),
            "ivAtEntry": fields.get("ivAtEntry"),
            "emPctAtEntry": fields.get("emPctAtEntry"),
            "emMultiple": fields.get("emMultiple"),
            "wingWidth": fields.get("wingWidth"),
            "earningsDate": fields.get("earningsDate"),
            "earningsTiming": fields.get("earningsTiming"),
            "expiry": fields.get("expiry"),
            "regimeAtEntry": fields.get("regimeAtEntry"),
        },
        "daysToEarnings": days_to_earnings,
        "evidence": _summarize_evidence_for_llm(evidence),
        "ruleBasedHint": {
            "preVerdict": pre_verdict,
            "preConfidence": pre_confidence,
            "note": (
                "This is a deterministic rule-based pre-verdict. Confirm if "
                "the evidence supports it, or override with reasoning."
            ),
        },
    }
    payload_str = json.dumps(payload, default=str, separators=(",", ":"))

    system_prompt = _live_review_system_prompt(phase)
    # Match the rest of the E1 advisor stack: read from E1_ADVISOR_MODEL and
    # use temperature=1 (gpt-5.5 only accepts the default temperature; any
    # other value triggers an `unsupported_value` 400 that nukes the narrative).
    model = str(getattr(f, "E1_ADVISOR_MODEL", None) or "gpt-5.5").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=1,
            max_completion_tokens=900,
            timeout=30,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content.strip()
        result = _parse_llm_json(content)
        if result is None:
            LOG.warning("E1 live review v2: LLM returned unparseable JSON (raw=%r)", content[:200])
            return {"_skipReason": "unparseable_json", "_rawHead": content[:200]}
        missing = _LIVE_REVIEW_REQUIRED_KEYS - set(result.keys())
        if missing:
            LOG.warning("E1 live review v2: LLM response missing keys: %s", sorted(missing))
            return {
                "_skipReason": "missing_required_keys",
                "_missing": sorted(missing),
                "_partial": result,
            }
        verdict = str(result.get("verdict") or "").upper()
        if verdict not in ("HOLD", "ADJUST", "CUT"):
            LOG.warning("E1 live review v2: LLM verdict %r invalid; coercing to pre-verdict", verdict)
            result["verdict"] = pre_verdict
        else:
            result["verdict"] = verdict
        try:
            result["confidence"] = float(result.get("confidence") or pre_confidence)
        except (TypeError, ValueError):
            result["confidence"] = pre_confidence
        result["_source"] = "llm"
        result["_model"] = model
        result["_phase"] = phase
        result["_generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return result
    except Exception as e:
        LOG.warning("E1 live review v2 LLM call failed: %s: %s", type(e).__name__, e)
        return {"_skipReason": f"exception:{type(e).__name__}", "_error": str(e)[:300]}
