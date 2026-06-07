"""Redis-backed persistence + v1 trade reader for the analogue index.

Each engine's serialized index lives at ``v2:analogues:index:{engine}`` as a
single JSON blob (small enough — even 10K trades × 8 floats × ~16 bytes is
~1.3 MB which Redis handles trivially). The on-disk shape is whatever
``AnalogueIndex.to_json()`` writes.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable

from .analogues import AnalogueIndex, build_index_from_v1_trades

LOG = logging.getLogger("v2.analogues_store")

INDEX_KEY_PREFIX = "v2:analogues:index"
V1_TRADE_SOURCES = {
    "e1": {"index_key": "e1:trades:index", "trade_prefix": "e1:trades:"},
    "e2": {"index_key": "e2:trades:index", "trade_prefix": "e2:trades:"},
}


def _redis_client() -> Any:
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


def _index_key(engine: str) -> str:
    return f"{INDEX_KEY_PREFIX}:{(engine or 'unknown').lower()}"


# ── Public API ──


def save_index(index: AnalogueIndex) -> bool:
    client = _redis_client()
    if client is None:
        return False
    try:
        client.set(_index_key(index.engine), index.to_json())
        return True
    except Exception as exc:
        LOG.warning("save_index failed for %s: %s", index.engine, exc)
        return False


def load_index(engine: str) -> AnalogueIndex | None:
    client = _redis_client()
    if client is None:
        return None
    try:
        blob = client.get(_index_key(engine))
    except Exception as exc:
        LOG.warning("load_index failed for %s: %s", engine, exc)
        return None
    return AnalogueIndex.from_json(blob)


def fetch_v1_trades(engine: str, *, cap: int = 5000) -> list[dict[str, Any]]:
    """Pull every v1 trade for ``engine`` from Redis, newest-last.

    Returns ``[]`` if Redis is unreachable or the engine has no journal.
    Stripped trades (loads to None or non-dict) are silently dropped.
    """
    if engine not in V1_TRADE_SOURCES:
        return []
    client = _redis_client()
    if client is None:
        return []

    src = V1_TRADE_SOURCES[engine]
    try:
        index_raw = client.get(src["index_key"])
        ids = json.loads(index_raw) if index_raw else []
    except Exception as exc:
        LOG.warning("fetch_v1_trades: index read failed for %s: %s", engine, exc)
        return []

    out: list[dict[str, Any]] = []
    for tid in list(ids)[-cap:]:
        try:
            blob = client.get(f"{src['trade_prefix']}{tid}")
            if not blob:
                continue
            doc = json.loads(blob)
            if isinstance(doc, dict):
                out.append(doc)
        except Exception:
            continue
    return out


def build_and_persist(engine: str, *, cap: int = 5000) -> dict[str, Any]:
    """Read v1 trades for ``engine``, build the analogue index, persist it."""
    if engine not in V1_TRADE_SOURCES:
        return {"ok": False, "reason": f"unknown engine {engine!r}"}
    trades = fetch_v1_trades(engine, cap=cap)
    index, stats = build_index_from_v1_trades(engine=engine, trades=trades)
    persisted = save_index(index) if stats.get("n_indexed", 0) > 0 else False
    stats["persisted"] = persisted
    stats["redis_available"] = _redis_client() is not None
    return stats


def index_summaries() -> list[dict[str, Any]]:
    """List per-engine index summaries (n_indexed, feature names) without
    loading the full row buffer to the client."""
    out: list[dict[str, Any]] = []
    for engine in V1_TRADE_SOURCES:
        idx = load_index(engine)
        if idx is None:
            continue
        out.append(
            {
                "engine": engine,
                "n_indexed": idx.n_indexed,
                "feature_names": idx.feature_names,
                "tickers": _tickers_summary(idx),
            }
        )
    return out


def _tickers_summary(idx: AnalogueIndex, top: int = 12) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for r in idx.rows:
        counts[r.ticker] = counts.get(r.ticker, 0) + 1
    pairs = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top]
    return [{"ticker": t, "n": n} for t, n in pairs]
