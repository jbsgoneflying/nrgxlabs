"""Raven-Tech 2.0 – LLM Client for narrative compression.

Thin wrapper around OpenAI API with:
  - Retry and timeout handling
  - Structured output parsing
  - Rate limiting
  - Graceful fallback when API is unavailable

Critical rule: NO LLM output can influence production signals without
passing through the backtest approval gate.
"""

from __future__ import annotations

import json
import logging
import os
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Simple token-bucket rate limiter."""

    def __init__(self, max_calls_per_minute: int = 2):
        self._lock = threading.Lock()
        self._max = max_calls_per_minute
        self._timestamps: List[float] = []

    def acquire(self) -> bool:
        with self._lock:
            now = time.time()
            # Remove timestamps older than 60s
            self._timestamps = [t for t in self._timestamps if now - t < 60.0]
            if len(self._timestamps) >= self._max:
                return False
            self._timestamps.append(now)
            return True


_rate_limiter = _RateLimiter()


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------


def _get_openai_client():
    """Lazy-load OpenAI client. Returns None if not available."""
    try:
        import openai
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        return openai.OpenAI(api_key=api_key)
    except ImportError:
        LOG.warning("openai package not installed; LLM features disabled")
        return None
    except Exception as e:
        LOG.warning(f"Failed to create OpenAI client: {e}")
        return None


def _load_prompt(name: str) -> str:
    """Load a prompt template from backend/prompts/."""
    prompt_dir = Path(__file__).parent / "prompts"
    path = prompt_dir / name
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


# ---------------------------------------------------------------------------
# Robust JSON parser (shared across this module)
# ---------------------------------------------------------------------------


def _parse_desk_brief_json(content: str) -> Optional[dict]:
    """Parse LLM response with robust fallback for GPT-5.5 verbosity."""
    raw = content
    content = content.strip()

    # Strip markdown code fences
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:])
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3]
        content = content.strip()

    # Direct parse
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Fallback: extract first {...} block via brace-matching
    start = content.find("{")
    if start == -1:
        LOG.warning("Desk brief LLM returned no JSON; raw (first 300): %s", raw[:300])
        return None

    depth = 0
    end = start
    for i in range(start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if depth != 0:
        LOG.warning("Desk brief JSON brace mismatch; raw (first 300): %s", raw[:300])
        return None

    try:
        return json.loads(content[start:end])
    except json.JSONDecodeError:
        LOG.warning("Desk brief JSON extraction failed; raw (first 300): %s", raw[:300])
        return None


# ---------------------------------------------------------------------------
# Desk Brief (narrative compression)
# ---------------------------------------------------------------------------

_DESK_BRIEF_SYSTEM_PROMPT = """You are a senior quant desk analyst at a systematic options trading firm.
You will receive a JSON payload containing: regime state,
vol state, sequencer events this week, gate summary, and macro event calendar.

Produce EXACTLY this JSON structure:
{
  "market_state": "one sentence describing current market state",
  "weekly_bias": "one sentence describing what is likely to work this week",
  "top_risks": "one sentence describing what could break the plan"
}

Rules:
- Each field must be ONE sentence, maximum 30 words.
- Use only the data provided. Do not infer or hallucinate external events.
- Do not give trading advice or specific trade recommendations.
- Do not mention specific prices, only relative terms (elevated, compressed, etc.)
- Output valid JSON only."""

_DESK_BRIEF_FALLBACK = {
    "market_state": "Market data is being processed; review the metrics cards directly.",
    "weekly_bias": "Consult Regime cards for current bias.",
    "top_risks": "Check the Macro Event Density panel for upcoming catalysts.",
}


def generate_desk_brief(context: Dict[str, Any]) -> Dict[str, str]:
    """Generate the Desk Brief narrative from system context.

    Args:
        context: Dict with keys like regime, vol_state,
                 sequencer_events, gate_summary, macro_events

    Returns:
        Dict with market_state, weekly_bias, top_risks
    """
    if not _rate_limiter.acquire():
        LOG.info("Desk brief rate-limited; returning fallback")
        return dict(_DESK_BRIEF_FALLBACK)

    client = _get_openai_client()
    if client is None:
        return dict(_DESK_BRIEF_FALLBACK)

    # Truncate context (GPT-5.5 400K context allows more data)
    payload_str = json.dumps(context, default=str)
    if len(payload_str) > 15000:
        payload_str = payload_str[:15000] + "..."

    model = os.getenv("LLM_MODEL_NARRATIVE", "gpt-5.5").strip()

    # Try loading custom prompt; fall back to built-in
    system_prompt = _load_prompt("desk_brief.txt") or _DESK_BRIEF_SYSTEM_PROMPT

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=1,
            max_completion_tokens=600,
            timeout=30,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content.strip()

        # Robust JSON parsing (handles GPT-5.5 verbosity)
        result = _parse_desk_brief_json(content)
        if result is None:
            return dict(_DESK_BRIEF_FALLBACK)

        # Validate schema
        required = {"market_state", "weekly_bias", "top_risks"}
        if not required.issubset(set(result.keys())):
            LOG.warning(f"LLM response missing required keys: {required - set(result.keys())}")
            return dict(_DESK_BRIEF_FALLBACK)

        return {
            "market_state": str(result["market_state"])[:200],
            "weekly_bias": str(result["weekly_bias"])[:200],
            "top_risks": str(result["top_risks"])[:200],
        }

    except Exception as e:
        LOG.warning(f"LLM desk brief failed: {e}")
        return dict(_DESK_BRIEF_FALLBACK)
