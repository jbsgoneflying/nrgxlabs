"""Redis-backed return-corpus store for the path generator.

A "corpus" is a list of (date, log_return) pairs for one underlying
ticker. The desk seeds the corpus once (POST a JSON list of pairs)
and the path-generator endpoints resample from it.

This is the MVP storage layer. Phase 2 swaps in a streaming EOD
ingest from EODHD, with regime-tag pre-computed at write time.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

try:
    import redis  # type: ignore
except Exception:  # pragma: no cover - tests inject a fake client
    redis = None  # type: ignore

LOG = logging.getLogger("v2.paths_store")


CORPUS_KEY_PREFIX = "v2:paths:corpus"
CORPUS_INDEX_KEY = "v2:paths:corpus:index"


def _redis_client():
    if redis is None:
        raise RuntimeError("redis package not available")
    url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    return redis.Redis.from_url(url, decode_responses=True)


def _key(ticker: str) -> str:
    return f"{CORPUS_KEY_PREFIX}:{ticker.upper()}"


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_corpus(
    ticker: str,
    rows: list[dict[str, Any]],
    *,
    redis_client: Any = None,
) -> dict[str, Any]:
    """Persist a return corpus for ``ticker``.

    ``rows`` is a list of ``{"date": "YYYY-MM-DD", "log_return": <float>}``.
    Returns a small status dict with the persisted count.
    """
    client = redis_client or _redis_client()
    cleaned: list[dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        d = r.get("date")
        x = r.get("log_return")
        if d is None or x is None:
            continue
        try:
            x = float(x)
        except (TypeError, ValueError):
            continue
        cleaned.append({"date": str(d), "log_return": x})
    payload = {
        "ticker": ticker.upper(),
        "n": len(cleaned),
        "saved_at": now_ts(),
        "rows": cleaned,
    }
    client.set(_key(ticker), json.dumps(payload))
    # Track which tickers have corpora.
    try:
        idx_raw = client.get(CORPUS_INDEX_KEY)
        idx = set(json.loads(idx_raw)) if idx_raw else set()
    except Exception:
        idx = set()
    idx.add(ticker.upper())
    client.set(CORPUS_INDEX_KEY, json.dumps(sorted(idx)))
    return {"ok": True, "ticker": ticker.upper(), "n": len(cleaned), "saved_at": payload["saved_at"]}


def load_corpus(ticker: str, *, redis_client: Any = None) -> dict[str, Any] | None:
    client = redis_client or _redis_client()
    try:
        raw = client.get(_key(ticker))
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def list_corpora(*, redis_client: Any = None) -> dict[str, Any]:
    client = redis_client or _redis_client()
    try:
        idx_raw = client.get(CORPUS_INDEX_KEY)
        tickers = json.loads(idx_raw) if idx_raw else []
    except Exception:
        tickers = []
    summary: list[dict[str, Any]] = []
    for t in tickers:
        doc = load_corpus(t, redis_client=client)
        if not doc:
            continue
        summary.append({
            "ticker": doc.get("ticker"),
            "n": doc.get("n", 0),
            "saved_at": doc.get("saved_at"),
        })
    return {"tickers": summary, "n_total": sum(s["n"] for s in summary)}
