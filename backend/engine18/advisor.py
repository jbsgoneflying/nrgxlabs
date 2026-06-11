"""Engine 18 advisor — narrative-only LLM desk note over the current scan.

The advisor never changes sizing or candidate selection (those are
deterministic); it provides desk color: which candidates deserve capital,
open-trade exit hygiene, and concentration warnings.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)

_PROMPT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts", "engine18_advisor.txt")


def _load_prompt() -> str:
    try:
        with open(_PROMPT_PATH, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def generate_drift_advisor(
    scan_payload: Dict[str, Any],
    *,
    open_trades: Optional[List[Dict[str, Any]]] = None,
    model: str = "gpt-5.5",
) -> Dict[str, Any]:
    """Return {"narrative": str, "_source": "llm"|"fallback", ...}."""
    fallback = {
        "narrative": "Advisor unavailable — act on the deterministic sizing tiers directly.",
        "_source": "fallback",
    }
    system_prompt = _load_prompt()
    if not system_prompt:
        fallback["_fallback_reason"] = "prompt file missing"
        return fallback
    try:
        import openai

        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            fallback["_fallback_reason"] = "OPENAI_API_KEY not set"
            return fallback
        client = openai.OpenAI(api_key=key)

        context = {
            "scan": {
                "asOf": scan_payload.get("asOf"),
                "summary": scan_payload.get("summary"),
                "candidates": (scan_payload.get("candidates") or [])[:20],
                "meta": scan_payload.get("meta"),
                "validation": scan_payload.get("validation"),
            },
            "openTrades": (open_trades or [])[:20],
        }
        payload_str = json.dumps(context, default=str)
        if len(payload_str) > 25000:
            payload_str = payload_str[:25000]

        # gpt-5.5 rejects non-default temperature (see 225f89b).
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=1,
        )
        narrative = (resp.choices[0].message.content or "").strip()
        if not narrative:
            fallback["_fallback_reason"] = "empty LLM response"
            return fallback
        return {"narrative": narrative, "_source": "llm", "model": model}
    except Exception as exc:  # pragma: no cover - network path
        LOG.warning("engine18 advisor failed: %s", exc)
        fallback["_fallback_reason"] = str(exc)
        return fallback
