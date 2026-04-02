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

    model = str(f.E1_ADVISOR_MODEL or "gpt-5.4").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=0.3,
            max_completion_tokens=1800,
            timeout=45,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content.strip()
        result = _parse_llm_json(content)

        if result is None or not _ADVISOR_REQUIRED_KEYS.issubset(set(result.keys())):
            LOG.warning("E1 advisor: LLM response missing required keys")
            fallback["_fallback_reason"] = "LLM returned invalid JSON"
            return fallback

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

    model = str(f.E1_ADVISOR_MODEL or "gpt-5.4").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=0.3,
            max_completion_tokens=800,
            timeout=30,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content.strip()
        result = _parse_llm_json(content)

        if result is None or not _POST_MORTEM_REQUIRED_KEYS.issubset(set(result.keys())):
            LOG.warning("E1 post-mortem: LLM response missing required keys")
            fallback["_fallback_reason"] = "LLM returned invalid JSON"
            return fallback

        result["_source"] = "llm"
        result["_model"] = model
        result["_generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return result

    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning("E1 post-mortem LLM call failed: %s", reason)
        fallback["_fallback_reason"] = reason
        return fallback
