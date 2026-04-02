from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

try:
    import redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None


DEFAULT_TTL_S = int(float(os.getenv("ASKRAVEN_TTL_S") or (2 * 60 * 60)))  # 2 hours


@dataclass(frozen=True)
class RedisStore:
    url: str

    def _client(self):
        if redis is None:
            return None
        # decode_responses=False => store bytes, we handle json ourselves
        return redis.Redis.from_url(self.url, decode_responses=False)

    def ping(self) -> bool:
        c = self._client()
        if c is None:
            return False
        try:
            return bool(c.ping())
        except Exception:
            return False

    def set_json(self, key: str, value: Any, ttl_s: int = DEFAULT_TTL_S) -> bool:
        c = self._client()
        if c is None:
            return False
        try:
            raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            c.set(name=str(key), value=raw, ex=int(ttl_s))
            return True
        except Exception:
            return False

    def get_json(self, key: str) -> Optional[Any]:
        c = self._client()
        if c is None:
            return None
        try:
            raw = c.get(str(key))
            if not raw:
                return None
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def set_bytes(self, key: str, value: bytes, ttl_s: int = DEFAULT_TTL_S) -> bool:
        c = self._client()
        if c is None:
            return False
        try:
            c.set(name=str(key), value=value, ex=int(ttl_s))
            return True
        except Exception:
            return False

    def get_bytes(self, key: str) -> Optional[bytes]:
        c = self._client()
        if c is None:
            return None
        try:
            v = c.get(str(key))
            if not v:
                return None
            if isinstance(v, str):
                return v.encode("utf-8")
            return v
        except Exception:
            return None

    def delete_key(self, key: str) -> bool:
        """Delete a single key from Redis. Returns True on success."""
        c = self._client()
        if c is None:
            return False
        try:
            c.delete(str(key))
            return True
        except Exception:
            return False

    def scan_keys(self, pattern: str) -> list:
        """Return all keys matching a glob pattern using non-blocking SCAN."""
        c = self._client()
        if c is None:
            return []
        try:
            keys = []
            cursor = 0
            while True:
                cursor, batch = c.scan(cursor=cursor, match=pattern, count=200)
                keys.extend(
                    k.decode("utf-8") if isinstance(k, bytes) else k
                    for k in batch
                )
                if cursor == 0:
                    break
            return keys
        except Exception:
            return []


def get_store_optional() -> Optional[RedisStore]:
    url = str(os.getenv("REDIS_URL") or "").strip()
    if not url:
        return None
    return RedisStore(url=url)


def now_s() -> int:
    return int(time.time())


