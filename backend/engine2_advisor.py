"""Engine 2 — AI Trade Advisor.

LLM-powered iron-condor trade analysis and check-in advisory.
Uses structured prompts from backend/prompts/ with DailyMarketState context.
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
from backend.daily_market_state import DailyMarketState, load_dms
from backend.news_theme_intelligence import compute_market_adjusted_intensity, get_theme_impact_weight
from backend.redis_store import get_store_optional

LOG = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_ADVISOR_REQUIRED_KEYS = {
    "verdict", "confidence", "tradeTicket", "wingWidthRationale",
    "riskContext", "entryPlan", "managementPlan", "exitRules",
    "keyRisks", "deskNote",
}

_CHECKIN_REQUIRED_KEYS = {
    "status", "headline", "spotAnalysis", "regimeDrift",
    "riskUpdate", "recommendation", "adjustmentIfNeeded", "deskNote",
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
# OpenAI client (lazy)
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
    """Parse JSON from LLM response, handling markdown fences."""
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
# DMS integration
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
    themes_raw = dms_dict.get("news_themes") or []
    active_themes = []
    for t in themes_raw:
        raw_i = float(t.get("intensity", 0))
        if raw_i <= 10:
            continue
        key = t.get("key", "")
        label = t.get("theme", "")
        adj = float(t.get("adjusted_intensity", 0))
        if adj <= 0:
            adj = compute_market_adjusted_intensity(raw_i, key or label)
        weight = float(t.get("spx_impact_weight", 0))
        if weight <= 0:
            weight = get_theme_impact_weight(key or label)
        active_themes.append({
            "theme": label,
            "key": key,
            "intensity": raw_i,
            "adjustedIntensity": round(adj, 1),
            "spxImpactWeight": round(weight, 2),
            "acceleration": t.get("acceleration"),
        })

    news_gate = compute_news_gate_score(active_themes)

    return {
        "regime": dms_dict.get("regime", {}),
        "vol_state": dms_dict.get("vol_state", {}),
        "composite_stress": (dms_dict.get("cross_asset_stress") or {}).get("composite_score"),
        "composite_label": (dms_dict.get("cross_asset_stress") or {}).get("composite_label"),
        "engine_gates": dms_dict.get("engine_gates", {}),
        "active_themes": active_themes,
        "newsGate": news_gate,
        "news_risk": dms_dict.get("news_risk", {}),
        "sequencer_summary": dms_dict.get("sequencer_summary"),
    }


def compute_news_gate_score(themes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministic news gate scoring from market-adjusted theme intensities.

    Returns a gate status (ok/caution/elevated/block) and the dominant theme.
    Mirrors Engine12's classify_event_severity pattern.
    """
    if not themes:
        return {"maxAdjustedIntensity": 0, "gate": "ok", "dominantTheme": None, "themeCount": 0}

    max_adj = 0.0
    dominant = None
    for t in themes:
        adj = float(t.get("adjustedIntensity", 0))
        if adj > max_adj:
            max_adj = adj
            dominant = t.get("theme") or t.get("key")

    if max_adj >= 80:
        gate = "block"
    elif max_adj >= 60:
        gate = "elevated"
    elif max_adj >= 30:
        gate = "caution"
    else:
        gate = "ok"

    return {
        "maxAdjustedIntensity": round(max_adj, 1),
        "gate": gate,
        "dominantTheme": dominant,
        "themeCount": len(themes),
    }


def compute_desk_consensus(
    *,
    regime_score: float = 50.0,
    regime_bucket: str = "MODERATE",
    macro_multiplier: float = 1.0,
    news_gate: Optional[Dict[str, Any]] = None,
    dealer_gamma_sign: str = "unknown",
    vol_pressure_state: str = "NEUTRAL",
    em_breach_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Deterministic desk-consensus risk pre-score.

    Synthesises regime, macro, news, gamma, and vol into a single risk
    assessment that both the EM tooltip and the Trade Advisor receive.
    This ensures a shared "floor" of risk awareness across independent
    LLM systems.  Pure logic — no LLM call.
    """
    flags: List[str] = []
    risk_points = 0.0

    # Macro proximity (1.0 = calm, >=2.0 = extreme)
    macro_flag = macro_multiplier >= 1.5
    if macro_flag:
        risk_points += 25 if macro_multiplier >= 2.0 else 15
        flags.append(f"Macro multiplier {macro_multiplier:.2f} (>= 1.5)")

    # Regime
    regime_flag = regime_bucket in ("ELEVATED", "NO_TRADE") or regime_score >= 65
    if regime_flag:
        risk_points += 20 if regime_bucket == "NO_TRADE" else 10
        flags.append(f"Regime {regime_bucket} (score {regime_score:.0f})")

    # News gate
    ng = news_gate or {}
    ng_gate = str(ng.get("gate", "ok")).lower()
    news_flag = ng_gate in ("caution", "elevated", "block")
    if news_flag:
        pts = {"caution": 10, "elevated": 20, "block": 30}.get(ng_gate, 0)
        risk_points += pts
        dom = ng.get("dominantTheme", "unknown")
        flags.append(f"NewsGate '{ng_gate}' (dominant: {dom})")

    # Dealer gamma
    gamma_flag = dealer_gamma_sign in ("negative", "short")
    if gamma_flag:
        risk_points += 10
        flags.append("Dealer gamma negative (amplified move risk)")

    # Vol pressure
    vol_flag = vol_pressure_state.upper() in ("BID", "SPIKING")
    if vol_flag:
        risk_points += 5
        flags.append(f"Vol pressure {vol_pressure_state}")

    # Map composite risk to level and EM floor
    if risk_points >= 40:
        risk_level, em_floor, em_label = "high", 2.0, "defensive"
    elif risk_points >= 25:
        risk_level, em_floor, em_label = "elevated", 2.0, "defensive"
    elif risk_points >= 15:
        risk_level, em_floor, em_label = "moderate", 1.5, "standard"
    else:
        risk_level, em_floor, em_label = "low", 1.0, "aggressive"

    # Breach sanity check: if ALL EM levels breach > 35%, force high risk
    ebs = em_breach_summary or {}
    all_high_breach = bool(ebs) and all(
        (v is not None and float(v) > 35) for v in ebs.values()
    )
    if all_high_breach:
        risk_level = "high"
        em_floor = 2.0
        em_label = "defensive"
        flags.append("All EM levels show breach > 35%")

    return {
        "riskLevel": risk_level,
        "riskPoints": round(risk_points, 1),
        "suggestedEmFloor": em_floor,
        "suggestedEmLabel": em_label,
        "macroFlag": macro_flag,
        "regimeFlag": regime_flag,
        "newsFlag": news_flag,
        "gammaFlag": gamma_flag,
        "volFlag": vol_flag,
        "flags": flags,
    }


def _sanitize_e2_for_llm(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the LLM-relevant slice from Engine 2 payload, capping size."""
    scan: Dict[str, Any] = {}
    for key in (
        "asOfDate", "params", "underlying", "current", "expectedMove",
        "strikeTargets", "oddsLikeNow", "recommendation", "recSimple",
        "deskConsensus",
    ):
        if key in payload:
            scan[key] = payload[key]

    live = payload.get("liveContext") or {}
    scan["liveContextSummary"] = {
        "volPressure": live.get("volPressure"),
        "weeklyFriday": {
            k: live.get("weeklyFriday", {}).get(k)
            for k in ("dealerGamma", "gammaFlipStrike", "addons")
            if live.get("weeklyFriday", {}).get(k) is not None
        } if isinstance(live.get("weeklyFriday"), dict) else None,
    }

    tech = payload.get("technicals") or {}
    scan["technicalsSummary"] = {
        k: tech.get(k)
        for k in ("rsi", "macd", "bollinger", "signals", "narrative", "distances", "ema")
        if tech.get(k) is not None
    }

    return scan


def _build_journal_context(digest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Distil performance digest into a compact context block for the LLM."""
    if not digest.get("hasData") or digest.get("totalClosed", 0) == 0:
        return None

    ctx: Dict[str, Any] = {
        "totalClosed": digest["totalClosed"],
        "winRate": digest.get("winRate"),
        "avgPnl": digest.get("avgPnl"),
        "avgWin": digest.get("avgWin"),
        "avgLoss": digest.get("avgLoss"),
        "riskTendency": digest.get("riskTendency"),
    }

    by_em = digest.get("byEm", {})
    if by_em:
        ctx["emPerformance"] = {
            k: {"winRate": v["winRate"], "avgPnl": v["avgPnl"], "n": v["n"]}
            for k, v in by_em.items()
        }

    by_wing = digest.get("byWing", {})
    if by_wing:
        ctx["wingPerformance"] = {
            k: {"winRate": v["winRate"], "avgPnl": v["avgPnl"], "n": v["n"]}
            for k, v in by_wing.items()
        }

    cal = digest.get("verdictCalibration", {})
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

    return ctx


# ---------------------------------------------------------------------------
# Deterministic trade tracking
# ---------------------------------------------------------------------------

def compute_trade_tracking(
    trade: Dict[str, Any],
    current_spot: float,
    current_regime: Optional[Dict[str, Any]] = None,
    current_vol_pressure: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute deterministic tracking metrics for an open trade."""
    entry = trade.get("entry", {})
    ctx = trade.get("entryContext", {})

    sp = float(entry.get("shortPutStrike", 0))
    sc = float(entry.get("shortCallStrike", 0))
    spot_at_entry = float(entry.get("spotAtEntry", 0)) or current_spot
    wing = float(entry.get("wingWidth", 5))

    dist_put_pts = current_spot - sp if sp else None
    dist_call_pts = sc - current_spot if sc else None
    dist_put_pct = round(dist_put_pts / current_spot * 100, 2) if (dist_put_pts is not None and current_spot > 0) else None
    dist_call_pct = round(dist_call_pts / current_spot * 100, 2) if (dist_call_pts is not None and current_spot > 0) else None

    entry_dist_put = spot_at_entry - sp if sp else None
    entry_dist_call = sc - spot_at_entry if sc else None

    def _prox(current_dist, entry_dist):
        if current_dist is None or entry_dist is None or entry_dist <= 0:
            return None
        ratio = max(0.0, 1.0 - current_dist / entry_dist)
        return round(min(ratio * 100.0, 100.0), 1)

    breach_prox_put = _prox(dist_put_pts, entry_dist_put)
    breach_prox_call = _prox(dist_call_pts, entry_dist_call)

    entry_date = entry.get("entryDate")
    expiry_date = entry.get("expiryDate")
    dte = None
    time_decay_progress = None
    if entry_date and expiry_date:
        try:
            ed = dt.date.fromisoformat(str(entry_date))
            xd = dt.date.fromisoformat(str(expiry_date))
            total_days = max((xd - ed).days, 1)
            elapsed = (dt.date.today() - ed).days
            dte = max((xd - dt.date.today()).days, 0)
            time_decay_progress = round(min(elapsed / total_days, 1.0), 2)
        except Exception:
            pass

    regime_drift_score = None
    regime_drift_bucket = None
    entry_regime_score = ctx.get("regimeScore")
    entry_regime_bucket = ctx.get("regimeBucket")
    if current_regime and entry_regime_score is not None:
        curr_score = current_regime.get("score")
        if curr_score is not None:
            regime_drift_score = round(float(curr_score) - float(entry_regime_score), 1)
        regime_drift_bucket = current_regime.get("bucket") if current_regime.get("bucket") != entry_regime_bucket else None

    vol_shift = None
    entry_vol = ctx.get("volPressureState")
    if current_vol_pressure and entry_vol:
        vol_shift = f"{entry_vol} -> {current_vol_pressure}" if current_vol_pressure != entry_vol else None

    max_prox = max(breach_prox_put or 0, breach_prox_call or 0)
    bucket_shift = 0
    if regime_drift_bucket:
        _BUCKETS = ["LOW", "MODERATE", "ELEVATED", "NO_TRADE"]
        try:
            old_idx = _BUCKETS.index(str(entry_regime_bucket or "").upper())
            new_idx = _BUCKETS.index(str(current_regime.get("bucket", "")).upper())
            bucket_shift = new_idx - old_idx
        except ValueError:
            pass

    if max_prox > 90 or dte == 0:
        deterministic_status = "exit"
    elif max_prox > 70 or bucket_shift >= 2:
        deterministic_status = "adjust"
    elif max_prox > 50 or bucket_shift >= 1:
        deterministic_status = "caution"
    else:
        deterministic_status = "on_track"

    return {
        "currentSpot": current_spot,
        "distPutPts": round(dist_put_pts, 2) if dist_put_pts is not None else None,
        "distCallPts": round(dist_call_pts, 2) if dist_call_pts is not None else None,
        "distPutPct": dist_put_pct,
        "distCallPct": dist_call_pct,
        "breachProxPut": breach_prox_put,
        "breachProxCall": breach_prox_call,
        "regimeDriftScore": regime_drift_score,
        "regimeDriftBucket": regime_drift_bucket,
        "volShift": vol_shift,
        "dte": dte,
        "timeDecayProgress": time_decay_progress,
        "deterministicStatus": deterministic_status,
    }


# ---------------------------------------------------------------------------
# LLM trade analysis
# ---------------------------------------------------------------------------

def generate_trade_analysis(
    engine2_payload: Dict[str, Any],
    width_analysis: Optional[List[Dict[str, Any]]] = None,
    flags: Optional[FeatureFlags] = None,
) -> Dict[str, Any]:
    """Run the full trade advisor: deterministic context + LLM verdict."""
    f = flags or get_flags()

    fallback: Dict[str, Any] = {k: None for k in _ADVISOR_REQUIRED_KEYS}
    fallback["_source"] = "fallback"
    fallback["verdict"] = "PASS"
    fallback["confidence"] = 0
    fallback["keyRisks"] = []
    fallback["tradeTicket"] = {}

    if not f.ENGINE2_ADVISOR_ENABLED:
        fallback["_fallback_reason"] = "Advisor disabled"
        return fallback

    system_prompt = _load_prompt("engine2_advisor.txt")
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

    from backend.engine2_trades import compute_trade_performance_digest
    perf_digest = compute_trade_performance_digest()
    trade_journal = _build_journal_context(perf_digest) if perf_digest.get("hasData") else None

    context = {
        "scan": _sanitize_e2_for_llm(engine2_payload),
        "market": _extract_dms_context(dms_dict),
        "widthAnalysis": width_analysis or engine2_payload.get("widthComparison", []),
    }
    if trade_journal:
        context["tradeJournal"] = trade_journal

    payload_str = json.dumps(context, default=str)
    if len(payload_str) > 30000:
        payload_str = payload_str[:30000]

    model = str(f.ENGINE2_ADVISOR_MODEL or "gpt-5.4").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=0.3,
            max_completion_tokens=1500,
            timeout=45,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content.strip()
        result = _parse_llm_json(content)

        if result is None or not _ADVISOR_REQUIRED_KEYS.issubset(set(result.keys())):
            LOG.warning("Engine2 advisor: LLM response missing required keys")
            fallback["_fallback_reason"] = "LLM returned invalid JSON"
            return fallback

        result["_source"] = "llm"
        result["_model"] = model
        result["_generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return result

    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning("Engine2 advisor LLM call failed: %s", reason)
        fallback["_fallback_reason"] = reason
        return fallback


# ---------------------------------------------------------------------------
# LLM check-in analysis
# ---------------------------------------------------------------------------

def generate_checkin_analysis(
    trade: Dict[str, Any],
    tracking: Dict[str, Any],
    flags: Optional[FeatureFlags] = None,
) -> Dict[str, Any]:
    """Run the check-in advisor: deterministic tracking + LLM narrative."""
    f = flags or get_flags()

    fallback: Dict[str, Any] = {k: None for k in _CHECKIN_REQUIRED_KEYS}
    fallback["_source"] = "fallback"
    fallback["status"] = tracking.get("deterministicStatus", "on_track")
    fallback["adjustmentIfNeeded"] = {"action": None, "detail": None}

    if not f.ENGINE2_ADVISOR_ENABLED:
        fallback["_fallback_reason"] = "Advisor disabled"
        return fallback

    system_prompt = _load_prompt("engine2_checkin.txt")
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

    trade_journal = None
    try:
        from backend.engine2_trades import compute_trade_performance_digest
        perf_digest = compute_trade_performance_digest()
        trade_journal = _build_journal_context(perf_digest) if perf_digest.get("hasData") else None
    except Exception:
        pass

    context = {
        "trade": {
            "entry": trade.get("entry", {}),
            "entryContext": trade.get("entryContext", {}),
            "advisorVerdict": trade.get("advisorVerdict"),
            "loggedAt": trade.get("loggedAt"),
        },
        "tracking": tracking,
        "market": _extract_dms_context(dms_dict),
    }
    if trade_journal:
        context["tradeJournal"] = trade_journal

    payload_str = json.dumps(context, default=str)
    if len(payload_str) > 25000:
        payload_str = payload_str[:25000]

    model = str(f.ENGINE2_ADVISOR_MODEL or "gpt-5.4").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=0.3,
            max_completion_tokens=1000,
            timeout=30,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content.strip()
        result = _parse_llm_json(content)

        if result is None or not _CHECKIN_REQUIRED_KEYS.issubset(set(result.keys())):
            LOG.warning("Engine2 check-in: LLM response missing required keys")
            fallback["_fallback_reason"] = "LLM returned invalid JSON"
            return fallback

        result["_source"] = "llm"
        result["_model"] = model
        result["_generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return result

    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning("Engine2 check-in LLM call failed: %s", reason)
        fallback["_fallback_reason"] = reason
        return fallback


# ---------------------------------------------------------------------------
# Post-mortem generation
# ---------------------------------------------------------------------------

_POST_MORTEM_REQUIRED_KEYS = {
    "thesisAssessment", "category", "lesson", "confidenceInAssessment", "deskNote",
}


def generate_post_mortem(
    trade: Dict[str, Any],
    *,
    flags: Optional[FeatureFlags] = None,
    journal_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate an LLM post-mortem for a closed Engine 2 trade."""
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

    system_prompt = _load_prompt("engine2_post_mortem.txt")
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
        "entry": trade.get("entry", {}),
        "entryContext": trade.get("entryContext", {}),
        "marketSnapshot": trade.get("marketSnapshot", {}),
        "advisorVerdict": trade.get("advisorVerdict"),
        "checkIns": (trade.get("checkIns") or [])[-5:],
        "outcome": trade.get("outcome", {}),
        "closeReason": trade.get("closeReason"),
    }
    if journal_context:
        context["tradeJournal"] = journal_context

    payload_str = json.dumps(context, default=str)
    if len(payload_str) > 25000:
        payload_str = payload_str[:25000]

    model = str(f.ENGINE2_ADVISOR_MODEL or "gpt-5.4").strip()

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
            LOG.warning("Engine2 post-mortem: LLM response missing required keys")
            fallback["_fallback_reason"] = "LLM returned invalid JSON"
            return fallback

        result["_source"] = "llm"
        result["_model"] = model
        result["_generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return result

    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning("Engine2 post-mortem LLM call failed: %s", reason)
        fallback["_fallback_reason"] = reason
        return fallback
