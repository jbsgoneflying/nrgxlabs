"""Redis-backed persistence + v1 DMS reader for the regime index.

We share v1's read-only Redis volume, so the historical regime corpus is
literally the rolling window of ``front_layer:dms:{YYYY-MM-DD}`` snapshots
v1 has been writing daily since launch. No re-derivation, no schema fork.
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

from .regime import RegimeIndex, build_index_from_dms_history

LOG = logging.getLogger("v2.regime_store")


# Where v1 writes the canonical daily market state.
V1_DMS_KEY_PREFIX = "front_layer:dms"
V1_DMS_INDEX_KEY = "front_layer:dms:index"

# Where v2 caches its built index. Singleton (one regime model, all of market).
V2_REGIME_INDEX_KEY = "v2:regime:index"
V2_REGIME_BUILT_AT_KEY = "v2:regime:built_at"


def _redis_client():
    if redis is None:
        raise RuntimeError("redis package not available")
    url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    return redis.Redis.from_url(url, decode_responses=True)


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── DMS history reader ─────────────────────────────────────


def fetch_v1_dms_history(
    *, max_days: int = 365, redis_client: Any = None
) -> list[dict[str, Any]]:
    """Pull every available daily market state snapshot from v1's Redis.

    v1 writes ``front_layer:dms:{date}`` with a 120-day TTL plus a rolling
    sorted-set index at ``front_layer:dms:index``. We prefer the index when
    available so we can grab dates in order without scanning, and fall back
    to a SCAN over the prefix when the index is missing.
    """
    client = redis_client or _redis_client()
    docs: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Try the rolling index first (sorted set or list of dates).
    try:
        member_dates = _read_index(client)
    except Exception as exc:  # pragma: no cover - hostile redis only
        LOG.warning("regime_store: index read failed: %s", exc)
        member_dates = []

    for date in member_dates[: max_days]:
        key = f"{V1_DMS_KEY_PREFIX}:{date}"
        if key in seen:
            continue
        seen.add(key)
        doc = _read_dms(client, key)
        if doc is not None:
            docs.append(doc)

    # Fallback / supplement: SCAN for any keys not in the index.
    try:
        for key in client.scan_iter(match=f"{V1_DMS_KEY_PREFIX}:*", count=200):
            if not isinstance(key, str):
                continue
            # The index key itself matches the prefix; skip it.
            if key == V1_DMS_INDEX_KEY:
                continue
            if key in seen:
                continue
            seen.add(key)
            doc = _read_dms(client, key)
            if doc is not None:
                docs.append(doc)
            if len(docs) >= max_days:
                break
    except Exception as exc:  # pragma: no cover
        LOG.warning("regime_store: SCAN over %s failed: %s", V1_DMS_KEY_PREFIX, exc)

    return docs


def _read_index(client: Any) -> list[str]:
    """Return DMS dates from the rolling index, newest first."""
    try:
        # sorted set (date as score)
        members = client.zrevrange(V1_DMS_INDEX_KEY, 0, -1)
        if members:
            return [m for m in members if isinstance(m, str)]
    except Exception:
        pass
    try:
        # list-style fallback
        members = client.lrange(V1_DMS_INDEX_KEY, 0, -1)
        if members:
            return [m for m in members if isinstance(m, str)]
    except Exception:
        pass
    return []


def _read_dms(client: Any, key: str) -> dict[str, Any] | None:
    try:
        raw = client.get(key)
    except Exception:
        return None
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    # Backfill ``date`` from key when the doc itself lacks one.
    if not doc.get("date"):
        try:
            doc["date"] = key.rsplit(":", 1)[-1]
        except Exception:
            pass
    return doc


# ── Index persistence ─────────────────────────────────────


def save_index(index: RegimeIndex, *, redis_client: Any = None) -> None:
    client = redis_client or _redis_client()
    client.set(V2_REGIME_INDEX_KEY, index.to_json())
    client.set(V2_REGIME_BUILT_AT_KEY, now_ts())


def load_index(*, redis_client: Any = None) -> RegimeIndex | None:
    client = redis_client or _redis_client()
    try:
        blob = client.get(V2_REGIME_INDEX_KEY)
    except Exception:
        return None
    return RegimeIndex.from_json(blob)


def index_summary(*, redis_client: Any = None) -> dict[str, Any]:
    """Lightweight stats on the persisted index (without loading it fully
    when the caller only needs counts)."""
    client = redis_client or _redis_client()
    try:
        built_at = client.get(V2_REGIME_BUILT_AT_KEY)
    except Exception:
        built_at = None
    index = load_index(redis_client=client)
    if index is None:
        return {
            "n_indexed": 0,
            "built_at": built_at,
            "feature_names": [],
            "label_distribution": {},
        }
    return {
        "n_indexed": index.n_indexed,
        "built_at": built_at,
        "feature_names": index.feature_names,
        "label_distribution": index.label_distribution(),
    }


def build_and_persist(
    *, max_days: int = 365, redis_client: Any = None
) -> dict[str, Any]:
    """Read v1 DMS history → build a regime index → persist → return stats."""
    client = redis_client or _redis_client()
    docs = fetch_v1_dms_history(max_days=max_days, redis_client=client)
    index, stats = build_index_from_dms_history(docs)
    if index.n_indexed:
        save_index(index, redis_client=client)
    stats["built_at"] = now_ts() if index.n_indexed else None
    return stats
