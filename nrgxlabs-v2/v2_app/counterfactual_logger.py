"""Counterfactual logger sidecar.

Phase 0 stub: exposes a fire-and-forget API that v1 (or v2 itself) can call
with paired inputs/outputs. Every payload lands on a Redis stream
``v2:counterfactual`` for later analysis.

Once the v2 engines are real, the v1 routes will tee every request through
this logger so we accumulate a clean v1-vs-v2 dataset before the desk ever
sees v2 verdicts.

Schema (per stream entry)::

    {
        "ts": ISO8601,
        "engine": "e1" | "e2" | "e14" | "e15" | "mi",
        "request_id": <uuid>,
        "v1_verdict": <free-form dict>,
        "v2_verdict": <free-form dict>,
        "agree": bool,
        "delta_summary": <optional str describing the disagreement>,
    }
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Iterable, Mapping

LOG = logging.getLogger("v2.counterfactual")


def _redis_client():
    """Lazy-import redis so v2 boots even if redis is unavailable in dev."""
    try:
        import redis  # type: ignore
    except ImportError:
        return None
    url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    try:
        return redis.from_url(url, decode_responses=True, socket_connect_timeout=2.0)
    except Exception as exc:
        LOG.warning("redis client init failed: %s", exc)
        return None


def log_counterfactual(
    engine: str,
    v1_verdict: Mapping[str, Any] | None,
    v2_verdict: Mapping[str, Any] | None,
    *,
    stream_name: str = "v2:counterfactual",
    request_id: str | None = None,
    delta_summary: str | None = None,
    max_stream_len: int = 50_000,
) -> str | None:
    """Write a counterfactual entry to the Redis stream.

    Returns the stream entry ID if it landed, else None.
    """
    rid = request_id or str(uuid.uuid4())
    agree = _shallow_agree(v1_verdict, v2_verdict)
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine": str(engine or "unknown"),
        "request_id": rid,
        "v1_verdict": json.dumps(v1_verdict or {}, default=str)[:8000],
        "v2_verdict": json.dumps(v2_verdict or {}, default=str)[:8000],
        "agree": "1" if agree else "0",
        "delta_summary": delta_summary or "",
    }

    client = _redis_client()
    if client is None:
        LOG.info("counterfactual (no redis): engine=%s agree=%s", engine, agree)
        return None
    try:
        sid = client.xadd(stream_name, entry, maxlen=max_stream_len, approximate=True)
        return str(sid)
    except Exception as exc:
        LOG.warning("xadd failed for %s: %s", stream_name, exc)
        return None


def recent_counterfactuals(
    *,
    n: int = 24,
    stream_name: str = "v2:counterfactual",
) -> list[dict[str, Any]]:
    """Read the last ``n`` entries from the counterfactual Redis stream.

    Newest-first. Each entry includes the parsed verdict dicts (limited to a
    few well-known keys) plus the timestamps the logger writes. Returns ``[]``
    if Redis is unavailable or the stream is empty so callers can render a
    "still waiting" state without exception handling.
    """
    n = max(1, min(int(n or 0), 200))
    client = _redis_client()
    if client is None:
        return []
    try:
        # XREVRANGE returns newest-first when called with "+ -" bounds.
        raw = client.xrevrange(stream_name, count=n)
    except Exception as exc:
        LOG.warning("xrevrange failed for %s: %s", stream_name, exc)
        return []

    return [_normalize_entry(sid, fields) for sid, fields in raw or []]


def _normalize_entry(sid: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    def _parse(name: str) -> dict[str, Any]:
        try:
            return json.loads(fields.get(name) or "{}")
        except Exception:
            return {}

    agree_raw = str(fields.get("agree") or "0").lower()
    return {
        "id": str(sid),
        "ts": fields.get("ts") or "",
        "engine": fields.get("engine") or "unknown",
        "request_id": fields.get("request_id") or "",
        "agree": agree_raw in ("1", "true", "yes"),
        "delta_summary": fields.get("delta_summary") or "",
        "v1_verdict": _summarize(_parse("v1_verdict")),
        "v2_verdict": _summarize(_parse("v2_verdict")),
    }


_VERDICT_KEYS: Iterable[str] = ("verdict", "stance", "recommendation", "label", "go", "confidence")


def _summarize(verdict: Mapping[str, Any]) -> dict[str, Any]:
    """Return only well-known verdict keys so the UI doesn't leak large blobs."""
    if not verdict:
        return {}
    out: dict[str, Any] = {}
    for k in _VERDICT_KEYS:
        if k in verdict:
            out[k] = verdict[k]
    return out


def _shallow_agree(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> bool:
    """Best-effort agreement check on a verdict-shaped dict.

    We compare a small set of well-known keys (``verdict``, ``stance``,
    ``recommendation``) case-insensitively. Anything else is treated as
    "agree" so the logger isn't noisy by default.
    """
    if not a or not b:
        return a == b
    keys = ("verdict", "stance", "recommendation", "go", "label")
    for k in keys:
        if k in a and k in b:
            return str(a.get(k)).lower() == str(b.get(k)).lower()
    return True
