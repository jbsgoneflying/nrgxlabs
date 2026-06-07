"""Raven Desk Insight v2 — core generator.

The single entry point that turns a (engine, card_type, card_data,
scenario_context) tuple into a nine-section desk-friendly tooltip, grounded
in an authoritative catalog spec and produced by gpt-5.5 (with a
deterministic static fallback so the UI is never empty).

The nine sections are the Raven canonical contract:

1. ``what_this_shows``     — two sentences, plain English
2. ``how_to_read_it``      — 2-3 sentences, references live numbers
3. ``quant_mechanics``     — the math / stats underneath
4. ``how_to_use_it``       — concrete decision action
5. ``example_scenario``    — a worked mini-case with plausible numbers
6. ``watch_for``           — failure mode / footgun
7. ``common_mistakes``     — what junior desks get wrong
8. ``related_cards``       — cross-link chips ({engine, slug, label})
9. ``desk_takeaway``       — one sentence accent-green punchline

All cache keys and fallbacks include the engine_id so two engines using the
same card_type slug never collide. Rate limit and cache are process-wide
but namespaced per engine in the key.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from cachetools import TTLCache

from backend.llm_client import _get_openai_client, _parse_desk_brief_json

LOG = logging.getLogger("desk_insight.core")

# ---------------------------------------------------------------------------
# Canonical output contract
# ---------------------------------------------------------------------------

#: Nine sections, rendered in this order (related_cards pinned to footer in UI).
OUTPUT_KEYS: List[str] = [
    "what_this_shows",
    "how_to_read_it",
    "quant_mechanics",
    "how_to_use_it",
    "example_scenario",
    "watch_for",
    "common_mistakes",
    "related_cards",
    "desk_takeaway",
]

OUTPUT_LABELS: Dict[str, str] = {
    "what_this_shows":  "What This Shows",
    "how_to_read_it":   "How To Read It",
    "quant_mechanics":  "Quant Mechanics",
    "how_to_use_it":    "How To Use It",
    "example_scenario": "Example Scenario",
    "watch_for":        "Watch For",
    "common_mistakes":  "Common Mistakes",
    "related_cards":    "Related Cards",
    "desk_takeaway":    "Desk Takeaway",
}

# Prose fields are capped per-call to keep popups scannable.
_PROSE_FIELD_CAP_CHARS = 900
#: related_cards is a list of {engine, slug, label}; cap count.
_RELATED_CARDS_CAP = 5
#: Max payload JSON sent to LLM.
_USER_PAYLOAD_CAP_CHARS = 12_000

# ---------------------------------------------------------------------------
# System prompt — parameterized per engine (fixes the E15-posing-as-E14 bug)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TMPL = """You are a senior quant options strategist briefing a new desk hire during morning prep. You are calm, precise, and never salesy. You reference live numbers. You never recommend a specific trade, price, or position size.

The trader is on:

Product:     {engine_name}
Description: {engine_description}
Asset class: {asset_class}

The card the trader is asking about:

{card_title}

AUTHORITATIVE SPEC (use as ground truth — do NOT contradict):
---
{card_spec}
---

Canonical related_cards (suggest 2-5 of these, or propose equivalents the trader could pivot to for context):
{related_cards_hint}

You will receive a JSON payload:
{{
  "card_data":        <the live numbers this card is rendering>,
  "scenario_context": <high-level context: ticker, strikes, credit, regime, etc.>
}}

Produce EXACTLY this JSON (no extra fields, no markdown, no code fences):
{{
  "what_this_shows":  "<one or two sentences, plain English>",
  "how_to_read_it":   "<how to interpret the numbers/labels on THIS card — reference live values from card_data>",
  "quant_mechanics":  "<2-3 sentences on the math/stats/estimator underneath the card — NOT marketing copy>",
  "how_to_use_it":    "<concrete action: how this changes a sizing/entry/exit decision>",
  "example_scenario": "<3-4 sentence worked mini-case using plausible numbers; make the decision logic visible>",
  "watch_for":        "<failure mode / footgun / when to distrust this card>",
  "common_mistakes":  "<what junior desks get wrong here>",
  "related_cards":    [ {{"engine": "eN", "slug": "other_card", "label": "Human readable"}}, ... 2-5 items ],
  "desk_takeaway":    "<one sentence, 20-30 words — THE single takeaway for THIS specific scenario>"
}}

Rules:
- Ground every claim in the spec and the live card_data. Do not invent data.
- Reference live numbers where relevant (e.g. "with fullCollect at 62% here...").
- Never recommend a specific trade, dollar amount, or position size.
- Each prose field 1-3 sentences (desk_takeaway: one sentence; example_scenario: 3-4 sentences).
- related_cards MUST be an array of objects with engine + slug + label. Prefer items from the hint list.
- Output valid JSON only."""

# ---------------------------------------------------------------------------
# Cache + rate limit
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: TTLCache = TTLCache(maxsize=1024, ttl=10 * 60)

# ---------------------------------------------------------------------------
# In-process telemetry counters — exposed by /api/desk-insight/stats so the
# desk can see cache hit rate, rate-limit pressure, and most-clicked cards
# during content iteration. All counters are process-local (not shared
# across gunicorn workers) — good enough for content feedback, not a
# replacement for real APM.
# ---------------------------------------------------------------------------

_stats_lock = threading.Lock()
_stats: Dict[str, Any] = {
    "requests_total":       0,
    "cache_hits":           0,
    "llm_calls":            0,
    "fallback_calls":       0,
    "rate_limited":         0,
    "llm_errors":           0,
    "parse_errors":         0,
    "missing_field_errors": 0,
    "by_engine":            {},   # engine_id -> counter dict
    "by_card":              {},   # "engine:slug" -> counter dict
    "last_request_utc":     None,
    "started_at_utc":       time.time(),
}


def _bump(kind: str, engine_id: str = "", card_type: str = "") -> None:
    """Thread-safe counter increment with per-engine + per-card breakouts."""
    with _stats_lock:
        _stats[kind] = int(_stats.get(kind, 0)) + 1
        if engine_id:
            eng = _stats["by_engine"].setdefault(engine_id, {
                "requests_total": 0, "cache_hits": 0, "llm_calls": 0,
                "fallback_calls": 0, "rate_limited": 0,
            })
            eng[kind] = int(eng.get(kind, 0)) + 1
            if card_type:
                key = f"{engine_id}:{card_type}"
                card = _stats["by_card"].setdefault(key, {
                    "requests_total": 0, "cache_hits": 0, "llm_calls": 0,
                    "fallback_calls": 0,
                })
                card[kind] = int(card.get(kind, 0)) + 1
        _stats["last_request_utc"] = time.time()


def get_stats_snapshot() -> Dict[str, Any]:
    """Return a deep copy of the current counters for the stats endpoint."""
    with _stats_lock:
        import copy
        snap = copy.deepcopy(_stats)
    # Derived metrics.
    total = snap["requests_total"]
    snap["cache_hit_rate"]    = (snap["cache_hits"] / total) if total else 0.0
    snap["llm_rate"]          = (snap["llm_calls"] / total) if total else 0.0
    snap["fallback_rate"]     = (snap["fallback_calls"] / total) if total else 0.0
    snap["rate_limit_rate"]   = (snap["rate_limited"] / total) if total else 0.0
    snap["uptime_seconds"]    = int(time.time() - snap["started_at_utc"])
    return snap


def reset_stats() -> None:
    """Test helper — zero the counters."""
    with _stats_lock:
        for k in list(_stats.keys()):
            if k in ("by_engine", "by_card"):
                _stats[k] = {}
            elif k == "started_at_utc":
                _stats[k] = time.time()
            elif k == "last_request_utc":
                _stats[k] = None
            else:
                _stats[k] = 0


class _RateLimiter:
    """Token-bucket rate limiter keyed to a wall-clock 60s window."""

    def __init__(self, max_calls_per_minute: int = 60) -> None:
        self._lock = threading.Lock()
        self._max = int(max_calls_per_minute)
        self._timestamps: List[float] = []

    def acquire(self) -> bool:
        with self._lock:
            now = time.time()
            self._timestamps = [t for t in self._timestamps if now - t < 60.0]
            if len(self._timestamps) >= self._max:
                return False
            self._timestamps.append(now)
            return True

    def configure(self, max_calls_per_minute: int) -> None:
        with self._lock:
            self._max = int(max_calls_per_minute)


def _rate_limit_budget() -> int:
    try:
        return max(1, int(float(os.getenv("DESK_INSIGHT_RATE_LIMIT_PER_MIN", "60"))))
    except (TypeError, ValueError):
        return 60


_rate_limiter = _RateLimiter(_rate_limit_budget())

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cache_key(
    engine_id: str, card_type: str, card_data: Any, scenario_context: Any
) -> str:
    try:
        payload = json.dumps(
            {"d": card_data, "s": scenario_context}, default=str, sort_keys=True
        )
    except Exception:
        payload = repr((card_data, scenario_context))
    h = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"{engine_id}:{card_type}:{h}"


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _first_sentence(text: str) -> str:
    for sep in (". ", ".\n", "? ", "! "):
        idx = text.find(sep)
        if idx != -1:
            return text[: idx + 1].strip()
    return text[:280].strip()


def _sanitize_related_cards(raw: Any) -> List[Dict[str, str]]:
    """Coerce related_cards output into a clean list of {engine, slug, label}."""
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        engine = str(item.get("engine") or "").strip().lower()
        slug = str(item.get("slug") or "").strip()
        label = str(item.get("label") or slug).strip()
        if not engine or not slug:
            continue
        key = (engine, slug)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {"engine": engine[:24], "slug": slug[:64], "label": label[:80]}
        )
        if len(out) >= _RELATED_CARDS_CAP:
            break
    return out


def _static_fallback(
    *,
    engine_id: str,
    card_type: str,
    card_title: str,
    spec: str,
    related_cards: List[Dict[str, str]],
    reason: str,
) -> Dict[str, Any]:
    """Deterministic nine-field fallback derived from the authoritative spec.

    Triggered when the LLM is unavailable, rate-limited, or returns invalid
    JSON. Every section is populated so the UI never shows an empty state.
    """
    summary = _first_sentence(spec) or f"{card_title} overview."
    return {
        "what_this_shows":  summary,
        "how_to_read_it":   _truncate(
            spec or "Reference the card labels directly.", 700
        ),
        "quant_mechanics":  (
            "Spec fallback — narrative LLM unavailable. See the spec text for "
            "the underlying methodology; the live values on this card are "
            "still authoritative."
        ),
        "how_to_use_it":    (
            "Cross-check against the adjacent cards and related sections "
            "before leaning on this signal; the full LLM desk note is "
            "temporarily offline."
        ),
        "example_scenario": (
            "(No worked example — LLM narrative offline. Refer to recent "
            "desk journal entries for analogue situations.)"
        ),
        "watch_for":        (
            "This is a spec-based fallback — the grounded LLM narrative "
            "isn't available right now, so the text is generic rather than "
            "tuned to today's specific values."
        ),
        "common_mistakes":  (
            "Junior traders often over-interpret a single card in isolation. "
            "Use the Related Cards below to triangulate."
        ),
        "related_cards":    list(related_cards or [])[:_RELATED_CARDS_CAP],
        "desk_takeaway":    "Spec fallback — refer to the raw card values.",
        "_source":          "fallback",
        "_engine":          engine_id,
        "_card_type":       card_type,
        "_fallback_reason": reason,
        "_meta": {
            "card_title":    card_title,
            "spec_version":  1,
        },
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_desk_insight(
    *,
    engine_id: str,
    card_type: str,
    card_data: Any,
    scenario_context: Optional[Dict[str, Any]] = None,
    catalog: Dict[str, Dict[str, Any]],
    engine_meta: Dict[str, Any],
) -> Dict[str, Any]:
    """Generate a nine-section desk insight for a single card.

    Parameters are keyword-only. The ``catalog`` is passed explicitly (no
    module-level monkey-patching); caller is responsible for supplying the
    right engine's catalog + metadata.

    Returns a dict with all nine OUTPUT_KEYS plus ``_source``, ``_engine``,
    ``_card_type``, ``_meta`` (and ``_fallback_reason`` on fallback).
    """
    engine_id = (engine_id or "").strip().lower()
    card_type = (card_type or "").strip()
    scenario_context = scenario_context or {}

    if not engine_id:
        return _static_fallback(
            engine_id="unknown",
            card_type=card_type,
            card_title=card_type or "Unknown card",
            spec="",
            related_cards=[],
            reason="engine_id is required",
        )

    spec_entry = (catalog or {}).get(card_type)
    if not isinstance(spec_entry, dict):
        return _static_fallback(
            engine_id=engine_id,
            card_type=card_type,
            card_title=card_type or "Unknown card",
            spec="",
            related_cards=[],
            reason=f"Unknown card_type {card_type!r} for engine {engine_id!r}",
        )

    card_title = str(spec_entry.get("title") or card_type)
    spec = str(spec_entry.get("spec") or "").strip()
    canonical_related = list(spec_entry.get("related_cards") or [])[
        :_RELATED_CARDS_CAP
    ]

    engine_name = str(engine_meta.get("name") or f"Engine {engine_id.upper()}")
    engine_description = str(
        engine_meta.get("description")
        or "NRGX Labs quant analytics surface."
    )
    asset_class = str(engine_meta.get("asset_class") or "multi-asset")

    _bump("requests_total", engine_id, card_type)

    # Cache probe.
    ckey = _cache_key(engine_id, card_type, card_data, scenario_context)
    with _cache_lock:
        cached = _cache.get(ckey)
    if cached is not None:
        _bump("cache_hits", engine_id, card_type)
        return cached

    # Rate-limit.
    if not _rate_limiter.acquire():
        _bump("rate_limited", engine_id, card_type)
        _bump("fallback_calls", engine_id, card_type)
        return _static_fallback(
            engine_id=engine_id,
            card_type=card_type,
            card_title=card_title,
            spec=spec,
            related_cards=canonical_related,
            reason="Rate limited (DESK_INSIGHT_RATE_LIMIT_PER_MIN).",
        )

    client = _get_openai_client()
    if client is None:
        _bump("fallback_calls", engine_id, card_type)
        return _static_fallback(
            engine_id=engine_id,
            card_type=card_type,
            card_title=card_title,
            spec=spec,
            related_cards=canonical_related,
            reason="OPENAI_API_KEY not configured",
        )

    model = (
        os.getenv("DESK_INSIGHT_MODEL")
        or os.getenv("LLM_MODEL_NARRATIVE")
        or "gpt-5.5"
    ).strip()

    # Compose prompt.
    related_hint_lines = []
    for rc in canonical_related:
        if isinstance(rc, dict):
            related_hint_lines.append(
                f"- engine={rc.get('engine')} slug={rc.get('slug')} "
                f"label={rc.get('label')}"
            )
    related_hint = (
        "\n".join(related_hint_lines)
        if related_hint_lines
        else "(no canonical cross-links — suggest 2-3 that make sense)"
    )

    system_prompt = _SYSTEM_PROMPT_TMPL.format(
        engine_name=engine_name,
        engine_description=engine_description,
        asset_class=asset_class,
        card_title=card_title,
        card_spec=spec,
        related_cards_hint=related_hint,
    )

    user_payload = {
        "card_data":        card_data,
        "scenario_context": scenario_context,
    }
    try:
        user_str = json.dumps(user_payload, default=str)
    except Exception as e:
        user_str = json.dumps({
            "card_data":        repr(card_data),
            "scenario_context": repr(scenario_context),
            "_serialize_error": str(e),
        })
    if len(user_str) > _USER_PAYLOAD_CAP_CHARS:
        user_str = user_str[:_USER_PAYLOAD_CAP_CHARS] + "…(truncated)"

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_str},
            ],
            temperature=1,
            max_completion_tokens=4000,  # gpt-5.5 reasoning needs headroom on top of 9-field output
            timeout=40,
            response_format={"type": "json_object"},
        )
        content = (response.choices[0].message.content or "").strip()
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning(
            "desk_insight LLM failed engine=%s card=%s: %s",
            engine_id, card_type, reason,
        )
        _bump("llm_errors", engine_id, card_type)
        _bump("fallback_calls", engine_id, card_type)
        return _static_fallback(
            engine_id=engine_id,
            card_type=card_type,
            card_title=card_title,
            spec=spec,
            related_cards=canonical_related,
            reason=reason,
        )

    parsed = _parse_desk_brief_json(content)
    if parsed is None:
        LOG.warning(
            "desk_insight parse fail engine=%s card=%s",
            engine_id, card_type,
        )
        _bump("parse_errors", engine_id, card_type)
        _bump("fallback_calls", engine_id, card_type)
        return _static_fallback(
            engine_id=engine_id,
            card_type=card_type,
            card_title=card_title,
            spec=spec,
            related_cards=canonical_related,
            reason="LLM returned invalid JSON",
        )

    # Validate + sanitize.
    prose_keys = [
        "what_this_shows", "how_to_read_it", "quant_mechanics",
        "how_to_use_it", "example_scenario", "watch_for",
        "common_mistakes", "desk_takeaway",
    ]
    result: Dict[str, Any] = {}
    missing: List[str] = []
    for k in prose_keys:
        v = parsed.get(k)
        if not isinstance(v, str) or not v.strip():
            missing.append(k)
            result[k] = ""
        else:
            result[k] = _truncate(v.strip(), _PROSE_FIELD_CAP_CHARS)

    # related_cards may be absent/malformed — fall back to catalog canonical.
    related = _sanitize_related_cards(parsed.get("related_cards"))
    if not related:
        related = list(canonical_related)[:_RELATED_CARDS_CAP]
    result["related_cards"] = related

    if missing:
        LOG.warning(
            "desk_insight missing fields engine=%s card=%s missing=%s",
            engine_id, card_type, ",".join(missing),
        )
        _bump("missing_field_errors", engine_id, card_type)
        _bump("fallback_calls", engine_id, card_type)
        return _static_fallback(
            engine_id=engine_id,
            card_type=card_type,
            card_title=card_title,
            spec=spec,
            related_cards=canonical_related,
            reason=f"LLM output missing: {', '.join(missing)}",
        )

    _bump("llm_calls", engine_id, card_type)

    result["_source"]    = "llm"
    result["_engine"]    = engine_id
    result["_card_type"] = card_type
    result["_meta"] = {
        "card_title":   card_title,
        "model":        model,
        "spec_version": 1,
    }

    with _cache_lock:
        _cache[ckey] = result
    return result


def clear_cache() -> None:
    """Test/admin helper — wipe the in-process cache."""
    with _cache_lock:
        _cache.clear()


def reconfigure_rate_limit(max_calls_per_minute: int) -> None:
    """Test/admin helper — reset the rate-limit budget mid-process."""
    _rate_limiter.configure(max_calls_per_minute)
