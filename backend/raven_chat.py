"""Raven Chat — Senior Quant Trader conversational advisor.

Streaming, context-aware chatbot that layers:
  1. System prompt (Senior Quant persona + engine knowledge)
  2. Market snapshot (DMS + news themes from Redis)
  3. Engine context (current engine's scan payload from frontend)
  4. Conversation history (client-side messages array)
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

LOG = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

ENGINE_LABELS = {
    "engine1": "Engine 1 — Earnings Hold Risk (Breach)",
    "engine2": "Engine 2 — SPX Iron Condor Scanner",
    "engine3": "Engine 3 — Global Lead-Lag Regime",
    "engine4": "Engine 4 — Mean-Reversion (Red Dog)",
    "engine5": "Engine 5 — Trend-Continuation (Ichimoku)",
    "engine6": "Engine 6 — Thematic Pairs Scanner",
    "engine7": "Engine 7 — Post-Event Extension Evaluator",
    "engine8": "Engine 8 — Credit Stress Drift Detection",
    "engine9": "Engine 9 — Earnings Calendar & Intelligence",
    "engine10": "Engine 10 — Multi-Ticker Compare",
    "engine11": "Engine 11 — Macro Events & Headline Risk",
    "engine12": "Engine 12 — VIX Spike Fade",
    "engine13": "Engine 13 — Gap Regime Scanner",
    "engine14": "Engine 14 — Iron Condor Scenario Simulator",
    "engine15": "Engine 15 — Earnings IC Scenario Advisor",
    "market-intel":         "Market Intelligence — Cross-Asset HMM Front Layer",
    "market-intelligence":  "Market Intelligence — Cross-Asset HMM Front Layer",
    "calendar":             "Earnings Calendar",
    "compare":              "Multi-Ticker Compare",
}


# ---------------------------------------------------------------------------
# Rate limiter (token-bucket, configurable rpm)
# ---------------------------------------------------------------------------

class _RateLimiter:
    def __init__(self, max_per_minute: int = 10):
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

    def reconfigure(self, max_per_minute: int) -> None:
        with self._lock:
            self._max = float(max_per_minute)
            self._rate = float(max_per_minute) / 60.0


_rate_limiter = _RateLimiter(10)


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


# ---------------------------------------------------------------------------
# Context builder — DMS + themes from Redis, engine data from frontend
# ---------------------------------------------------------------------------

def _load_market_snapshot() -> Optional[Dict[str, Any]]:
    """Load the latest DMS and theme snapshot from Redis.

    Augments the persisted DMS with a live ``regime_snapshot()`` from
    Market Intelligence v2 so chat answers always see today's HMM regime
    probabilities + factor contributions even when the persisted DMS is
    yesterday's. Falls back gracefully when MI v2 is disabled or fails.
    """
    try:
        from backend.redis_store import get_store_optional
        from backend.daily_market_state import load_dms
    except ImportError:
        return None

    store = get_store_optional()
    if store is None:
        return None

    today_str = dt.date.today().isoformat()
    dms = load_dms(today_str, store)
    if dms is None:
        yesterday_str = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        dms = load_dms(yesterday_str, store)

    if dms is None:
        # Even with no persisted DMS, return the live MI v2 regime if available
        # so chat has *something* to ground on.
        try:
            from backend.market_intel import regime_snapshot
            mi = regime_snapshot()
            return {"market_intel": _safe_mi_block(mi)}
        except Exception:
            return None

    d = dms.to_dict()
    snapshot: Dict[str, Any] = {}
    for key in ("date", "regime", "vol_state", "engine_gates", "news_risk",
                "cross_asset_stress", "news_themes", "asymmetry_signals",
                "earnings_candidates", "market_intel"):
        if d.get(key):
            snapshot[key] = d[key]

    # Augment with live MI v2 regime if the persisted DMS doesn't already
    # carry it (legacy persisted DMS from before MI v2 won't).
    if "market_intel" not in snapshot:
        try:
            from backend.market_intel import regime_snapshot
            mi = regime_snapshot()
            snapshot["market_intel"] = _safe_mi_block(mi)
        except Exception:
            pass

    return snapshot if snapshot else None


def _safe_mi_block(mi_snap: Any) -> Dict[str, Any]:
    """Squeeze a RegimeSnapshot into a chat-friendly dict (skip large arrays)."""
    try:
        return {
            "as_of":               getattr(mi_snap, "as_of", ""),
            "regime_label":        getattr(mi_snap, "label", "Transitional"),
            "regime_probs":        getattr(mi_snap, "probs", {}),
            "confidence":          getattr(mi_snap, "confidence", 0.0),
            "transition_risk_1d":  getattr(mi_snap, "transition_risk_1d", 0.0),
            "anomaly_score":       getattr(mi_snap, "anomaly_score", 0.0),
            "data_quality":        getattr(mi_snap, "data_quality", {}),
            "vol_state":           getattr(mi_snap, "vol_state", {}),
            "factor_contributions": getattr(mi_snap, "factor_contributions", {}),
            "model_version":       getattr(mi_snap, "model_version", ""),
            "source":              getattr(mi_snap, "source", ""),
        }
    except Exception:
        return {}


def _trim_engine_data(engine_data: Optional[Dict[str, Any]], max_chars: int = 15000) -> Optional[Dict[str, Any]]:
    """Trim engine data to fit within context budget without slicing JSON (which breaks json.loads)."""

    def _serialized_len(obj: Any) -> int:
        return len(json.dumps(obj, default=str))

    if not engine_data:
        return None

    if not isinstance(engine_data, dict):
        blob = json.dumps(engine_data, default=str)
        if len(blob) <= max_chars:
            return engine_data  # type: ignore[return-value]
        return {
            "_truncated": True,
            "type": type(engine_data).__name__,
            "preview": blob[: max(0, max_chars - 120)] + "…",
        }

    data: Dict[str, Any] = copy.deepcopy(engine_data)

    while _serialized_len(data) > max_chars:
        if len(data) > 1:
            largest_key = max(data.keys(), key=lambda k: _serialized_len(data[k]))
            del data[largest_key]
            continue
        key, val = next(iter(data.items()))
        inner = json.dumps(val, default=str)
        lo, hi = 0, len(inner)
        best_frag = "…"
        while lo <= hi:
            mid = (lo + hi) // 2
            suffix = "…" if mid < len(inner) else ""
            fragment = inner[:mid] + suffix
            trial = {key: fragment}
            if _serialized_len(trial) <= max_chars:
                best_frag = fragment
                lo = mid + 1
            else:
                hi = mid - 1
        data[key] = best_frag
        break

    return data


def build_chat_context(
    engine_id: Optional[str],
    engine_data: Optional[Dict[str, Any]],
    flags: Any = None,
) -> str:
    """Assemble the context block that gets injected as a system-adjacent message."""
    from backend.config import get_flags, FeatureFlags
    f: FeatureFlags = flags or get_flags()
    max_chars = getattr(f, "RAVEN_CHAT_MAX_CONTEXT_CHARS", 40000)

    parts: List[str] = []

    market = _load_market_snapshot()
    if market:
        market_str = json.dumps(market, default=str)
        if len(market_str) > max_chars // 2:
            market_str = market_str[:max_chars // 2]
        parts.append(f"## Live Market Snapshot (DMS)\n```json\n{market_str}\n```")

    if engine_id and engine_data:
        label = ENGINE_LABELS.get(engine_id, engine_id)
        trimmed = _trim_engine_data(engine_data, max_chars=max_chars // 3)
        if trimmed:
            engine_str = json.dumps(trimmed, default=str)
            parts.append(f"## Current Engine Data — {label}\n```json\n{engine_str}\n```")
    elif engine_id:
        label = ENGINE_LABELS.get(engine_id, engine_id)
        parts.append(f"## Current Engine: {label}\nNo scan data loaded yet — the trader has not run a scan on this engine.")

    if not parts:
        parts.append("No market snapshot or engine data available. Answer based on your trading knowledge and ask the trader for specifics if needed.")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Conversation trimmer — sliding window
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    return len(text) // 3

def trim_conversation(
    messages: List[Dict[str, str]],
    max_tokens: int = 20000,
) -> List[Dict[str, str]]:
    """Keep the most recent messages within the token budget."""
    if not messages:
        return []

    total = sum(_estimate_tokens(m.get("content", "")) for m in messages)
    if total <= max_tokens:
        return messages

    trimmed = list(messages)
    while len(trimmed) > 1 and total > max_tokens:
        removed = trimmed.pop(0)
        total -= _estimate_tokens(removed.get("content", ""))

    return trimmed


# ---------------------------------------------------------------------------
# Streaming LLM caller
# ---------------------------------------------------------------------------

def stream_chat_response(
    messages: List[Dict[str, str]],
    context: str,
    *,
    flags: Any = None,
) -> Generator[str, None, None]:
    """Stream chat response chunks from OpenAI.

    Yields SSE-formatted lines: 'data: {"chunk":"..."}\n\n'
    Final yield: 'data: {"done":true}\n\n'
    """
    from backend.config import get_flags, FeatureFlags
    f: FeatureFlags = flags or get_flags()

    rpm = getattr(f, "RAVEN_CHAT_RATE_LIMIT", 10)
    _rate_limiter.reconfigure(rpm)

    if not _rate_limiter.acquire():
        yield 'data: {"error":"Rate limited. Wait a moment and try again."}\n\n'
        return

    system_prompt = _load_prompt("raven_chat.txt")
    if not system_prompt:
        yield 'data: {"error":"System prompt missing."}\n\n'
        return

    client = _get_openai_client()
    if client is None:
        yield 'data: {"error":"OpenAI client unavailable."}\n\n'
        return

    model = str(getattr(f, "RAVEN_CHAT_MODEL", "gpt-5.5") or "gpt-5.5").strip()
    max_turns = getattr(f, "RAVEN_CHAT_MAX_TURNS", 30)

    user_messages = trim_conversation(messages, max_tokens=20000)
    if len(user_messages) > max_turns * 2:
        user_messages = user_messages[-(max_turns * 2):]

    llm_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"[CONTEXT — do not repeat this back, use it to inform your answers]\n\n{context}"},
        {"role": "assistant", "content": "Understood. I have the live market snapshot and engine data. Ready to advise."},
    ]
    llm_messages.extend(user_messages)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=llm_messages,
            temperature=1,
            max_completion_tokens=2000,
            timeout=90,
            stream=True,
        )

        for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                text = delta.content
                escaped = json.dumps(text)
                yield f'data: {{"chunk":{escaped}}}\n\n'

        yield 'data: {"done":true}\n\n'

    except Exception as e:
        LOG.warning("Raven Chat streaming failed: %s", e)
        yield f'data: {{"error":"{type(e).__name__}: {e}"}}\n\n'
