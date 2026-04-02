"""Weekly Trade Review Engine — LLM-powered institutional memory builder.

Runs weekly (Sunday evening cron) to review all trades closed in the past week,
generate a structured review, and store it in Redis for the desk.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_REDIS_KEY_PREFIX = "trade_review:weekly:"

_REVIEW_REQUIRED_KEYS = {
    "weekOf", "performanceSummary", "bestTrade", "worstTrade",
    "recommendations", "deskMorale",
}


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


def _load_prompt() -> Optional[str]:
    path = _PROMPTS_DIR / "weekly_trade_review.txt"
    if not path.exists():
        LOG.warning("Weekly review prompt not found: %s", path)
        return None
    return path.read_text(encoding="utf-8").strip()


def _trades_closed_this_week(
    trades: List[Dict[str, Any]],
    week_start: dt.date,
    week_end: dt.date,
) -> List[Dict[str, Any]]:
    """Filter to trades closed between week_start and week_end (inclusive)."""
    result = []
    for t in trades:
        closed_at = t.get("closedAt", "")
        if not closed_at:
            continue
        try:
            closed_date = dt.date.fromisoformat(closed_at[:10])
        except Exception:
            continue
        if week_start <= closed_date <= week_end:
            result.append(t)
    return result


def _compact_trade_for_review(t: Dict[str, Any]) -> Dict[str, Any]:
    """Trim a trade document to the fields relevant for the weekly review."""
    return {
        "tradeId": t.get("tradeId"),
        "ticker": t.get("ticker"),
        "entryDate": t.get("entry", {}).get("entryDate") or (t.get("loggedAt") or "")[:10],
        "closedAt": t.get("closedAt"),
        "emMultiple": t.get("entry", {}).get("emMultiple"),
        "wingWidth": t.get("entry", {}).get("wingWidth"),
        "entryCredit": t.get("entry", {}).get("entryCredit"),
        "outcome": t.get("outcome"),
        "closeReason": t.get("closeReason"),
        "postMortem": t.get("postMortem"),
        "tags": ((t.get("outcome") or {}).get("autoTags") or [])
               + ((t.get("outcome") or {}).get("userTags") or []),
    }


def generate_weekly_review(
    *,
    week_end: Optional[dt.date] = None,
    store: Any = None,
    model: str = "gpt-5.4",
) -> Dict[str, Any]:
    """Generate the weekly trade review via LLM.

    week_end defaults to today. Reviews trades from (week_end - 6 days) to week_end.
    """
    if week_end is None:
        week_end = dt.date.today()
    week_start = week_end - dt.timedelta(days=6)

    prompt = _load_prompt()
    if not prompt:
        return {"error": "Prompt file missing"}

    client = _get_openai_client()
    if client is None:
        return {"error": "OpenAI client unavailable"}

    # Gather E2 trades
    e2_closed_week: List[Dict[str, Any]] = []
    e2_digest: Dict[str, Any] = {}
    try:
        from backend.engine2_trades import list_closed_trades, compute_trade_performance_digest
        all_e2 = list_closed_trades(store=store, limit=200)
        e2_closed_week = _trades_closed_this_week(all_e2, week_start, week_end)
        e2_digest = compute_trade_performance_digest(store=store)
    except Exception as exc:
        LOG.warning("Weekly review: E2 data unavailable: %s", exc)

    # Gather E1 trades
    e1_closed_week: List[Dict[str, Any]] = []
    e1_digest: Dict[str, Any] = {}
    try:
        from backend.e1_earnings_trades import list_closed_trades as e1_list, compute_e1_trade_performance_digest
        all_e1 = e1_list(store=store, limit=200)
        e1_closed_week = _trades_closed_this_week(all_e1, week_start, week_end)
        e1_digest = compute_e1_trade_performance_digest(store=store)
    except Exception as exc:
        LOG.warning("Weekly review: E1 data unavailable: %s", exc)

    if not e2_closed_week and not e1_closed_week:
        return {
            "weekOf": week_start.isoformat(),
            "performanceSummary": "No trades closed this week.",
            "bestTrade": None,
            "worstTrade": None,
            "recommendations": [],
            "deskMorale": "Quiet week — no trades to review.",
            "_source": "no_data",
        }

    # Market context
    market_ctx: Dict[str, Any] = {}
    try:
        from backend.trade_memory import capture_market_snapshot
        market_ctx = capture_market_snapshot(store=store)
    except Exception:
        pass

    context: Dict[str, Any] = {
        "period": {"weekStart": week_start.isoformat(), "weekEnd": week_end.isoformat()},
        "engine2": {
            "closedThisWeek": [_compact_trade_for_review(t) for t in e2_closed_week],
            "digest": e2_digest,
        },
        "engine1": {
            "closedThisWeek": [_compact_trade_for_review(t) for t in e1_closed_week],
            "digest": e1_digest,
        },
        "marketContext": market_ctx,
    }

    payload_str = json.dumps(context, default=str)
    if len(payload_str) > 40000:
        payload_str = payload_str[:40000]

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=0.3,
            max_completion_tokens=1500,
            timeout=60,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content.strip()
        result = _parse_llm_json(content)

        if result is None or not _REVIEW_REQUIRED_KEYS.issubset(set(result.keys())):
            LOG.warning("Weekly review: LLM response missing required keys")
            return {"error": "LLM returned invalid JSON", "_raw": content[:500]}

        result["_source"] = "llm"
        result["_model"] = model
        result["_generatedAt"] = dt.datetime.utcnow().isoformat() + "Z"
        result["_tradeCount"] = {
            "e1": len(e1_closed_week),
            "e2": len(e2_closed_week),
        }

        # Persist to Redis
        if store is not None:
            try:
                key = f"{_REDIS_KEY_PREFIX}{week_start.isoformat()}"
                store.set_json(key, result, ttl_s=180 * 86400)
            except Exception as exc:
                LOG.warning("Weekly review: Redis persist failed: %s", exc)

        return result

    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning("Weekly review LLM call failed: %s", reason)
        return {"error": reason}


def get_weekly_review(
    week_of: str,
    *,
    store: Any = None,
) -> Optional[Dict[str, Any]]:
    """Load a previously generated weekly review from Redis."""
    if store is None:
        from backend.redis_store import get_store_optional
        store = get_store_optional()
    if store is None:
        return None
    return store.get_json(f"{_REDIS_KEY_PREFIX}{week_of}")


def list_weekly_reviews(
    *,
    store: Any = None,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    """List available weekly review dates (most recent first)."""
    if store is None:
        from backend.redis_store import get_store_optional
        store = get_store_optional()
    if store is None:
        return []

    reviews = []
    today = dt.date.today()
    for i in range(limit * 7):
        d = today - dt.timedelta(days=i)
        if d.weekday() == 0:
            key = f"{_REDIS_KEY_PREFIX}{d.isoformat()}"
            review = store.get_json(key)
            if review:
                reviews.append({
                    "weekOf": d.isoformat(),
                    "generatedAt": review.get("_generatedAt"),
                    "tradeCount": review.get("_tradeCount"),
                    "deskMorale": review.get("deskMorale"),
                })
            if len(reviews) >= limit:
                break
    return reviews
