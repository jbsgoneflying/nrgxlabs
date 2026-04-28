"""Engine 13 — Gap Regime Advisor (LLM desk note).

Follows the same pattern as engine2_advisor.py: sanitise deterministic
payload, build LLM context, call OpenAI, validate required keys, return
structured JSON.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

LOG = logging.getLogger(__name__)

_ADVISOR_REQUIRED_KEYS = {
    "verdict", "confidence", "reasoning", "historicalContext",
    "optionsRead", "technicalRead", "riskWarning", "actionPlan",
}

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


# ---------------------------------------------------------------------------
# Rate limiter (simple token-bucket, 4 rpm default)
# ---------------------------------------------------------------------------

class _RateLimiter:
    def __init__(self, max_per_minute: int = 4):
        self._lock = threading.Lock()
        self._tokens: float = float(max_per_minute)
        self._max: float = float(max_per_minute)
        self._last: float = time.monotonic()
        self._rate: float = float(max_per_minute) / 60.0

    def acquire(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._max, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


_rate_limiter = _RateLimiter(4)


def _load_prompt(name: str) -> Optional[str]:
    try:
        p = _PROMPTS_DIR / name
        return p.read_text(encoding="utf-8").strip() if p.exists() else None
    except Exception:
        return None


def _get_openai_client():
    try:
        import openai
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
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Sanitiser — extract LLM-relevant slice from scan payload
# ---------------------------------------------------------------------------

def _sanitize_scan_for_llm(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Trim large fields to keep the LLM context lean."""
    ctx: Dict[str, Any] = {}

    for key in ("gap", "scenarios", "vixBehaviour", "geopoliticalAnalogues", "catalystFragility"):
        if key in payload:
            ctx[key] = payload[key]

    # Historical analogues — keep stats + outcome distribution, trim individual events
    hist = payload.get("historicalAnalogues") or {}
    ctx["historicalAnalogues"] = {
        "count": hist.get("count"),
        "stats": hist.get("stats"),
        "outcomeDistribution": hist.get("outcomeDistribution"),
        "medianIntradayGapFill": hist.get("medianIntradayGapFill"),
        "topEvents": (hist.get("events") or [])[:5],
    }

    # Options — keep structured summaries
    opts = payload.get("optionsMicrostructure") or {}
    ctx["optionsMicrostructure"] = {
        "dealerGamma": opts.get("dealerGamma"),
        "skew": opts.get("skew"),
        "termStructure": opts.get("termStructure"),
        "unusualFlow": {
            k: v for k, v in (opts.get("unusualFlow") or {}).items()
            if k != "topItems"
        } if opts.get("unusualFlow") else None,
        "oiClusters": None,
    }
    oi = opts.get("oiClusters") or {}
    if isinstance(oi, dict) and oi.get("clusters"):
        ctx["optionsMicrostructure"]["oiClusters"] = {
            "aboveSpot": oi.get("aboveSpot"),
            "belowSpot": oi.get("belowSpot"),
            "clusters": (oi.get("clusters") or [])[:5],
        }

    # Technicals — keep summary fields only
    tech = payload.get("technicals") or {}
    ctx["technicals"] = {
        k: tech.get(k)
        for k in ("ema", "rsi", "macd", "bollinger", "signals", "narrative", "distances", "livePrice", "lastDailyClose")
        if tech.get(k) is not None
    }

    return ctx


# ---------------------------------------------------------------------------
# Position context — derive actionable metrics from optional IC inputs
# ---------------------------------------------------------------------------

def _compute_position_context(
    position: Optional[Dict[str, Any]],
    scan_payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Derive strike distances, DTE, and breach flags from optional position data."""
    if not position:
        return None

    ctx: Dict[str, Any] = {}
    live_price = (scan_payload.get("gap") or {}).get("livePrice")
    today = dt.date.today()

    short_call = position.get("shortCallStrike")
    short_put = position.get("shortPutStrike")
    exp_str = position.get("expirationDate")

    if short_call and live_price:
        dist = short_call - live_price
        ctx["shortCallStrike"] = short_call
        ctx["shortCallDistance"] = round(dist, 2)
        ctx["shortCallDistancePct"] = round(dist / live_price * 100, 2)
        ctx["shortCallBreached"] = live_price >= short_call

    if short_put and live_price:
        dist = live_price - short_put
        ctx["shortPutStrike"] = short_put
        ctx["shortPutDistance"] = round(dist, 2)
        ctx["shortPutDistancePct"] = round(dist / live_price * 100, 2)
        ctx["shortPutBreached"] = live_price <= short_put

    if exp_str:
        try:
            exp_date = dt.date.fromisoformat(exp_str)
            dte = (exp_date - today).days
            ctx["expirationDate"] = exp_str
            ctx["dte"] = dte
            ctx["expiresThisWeek"] = dte <= 5
            ctx["expiresNextSession"] = dte <= 1
        except ValueError:
            pass

    return ctx if ctx else None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_gap_regime_analysis(
    scan_payload: Dict[str, Any],
    *,
    position: Optional[Dict[str, Any]] = None,
    flags: Any = None,
) -> Dict[str, Any]:
    """Run the Engine 13 advisor: sanitise → prompt → LLM → validate."""
    from backend.config import get_flags, FeatureFlags
    f: FeatureFlags = flags or get_flags()

    fallback: Dict[str, Any] = {k: None for k in _ADVISOR_REQUIRED_KEYS}
    fallback["_source"] = "fallback"
    fallback["verdict"] = "HOLD"
    fallback["confidence"] = 0
    fallback["actionPlan"] = "Advisor unavailable — use manual judgment."

    if not getattr(f, "ENABLE_ENGINE13_GAP_REGIME", True):
        fallback["_fallback_reason"] = "Engine 13 disabled"
        return fallback

    system_prompt = _load_prompt("engine13_advisor.txt")
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

    context = _sanitize_scan_for_llm(scan_payload)
    pos_ctx = _compute_position_context(position, scan_payload)
    if pos_ctx:
        context["position"] = pos_ctx
    payload_str = json.dumps(context, default=str)
    if len(payload_str) > 25000:
        payload_str = payload_str[:25000]

    model = str(getattr(f, "ENGINE13_ADVISOR_MODEL", "gpt-5.5") or "gpt-5.5").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=1,
            max_completion_tokens=1500,
            timeout=45,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content.strip()
        result = _parse_llm_json(content)

        if result is None or not _ADVISOR_REQUIRED_KEYS.issubset(set(result.keys())):
            LOG.warning("Engine13 advisor: LLM response missing required keys")
            fallback["_fallback_reason"] = "LLM returned invalid JSON"
            return fallback

        result["_source"] = "llm"
        result["_model"] = model
        result["_generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return result

    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning("Engine13 advisor LLM call failed: %s", reason)
        fallback["_fallback_reason"] = reason
        return fallback
