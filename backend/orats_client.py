from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests
from cachetools import TTLCache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ORATS_BASE_URL = "https://api.orats.io/datav2"


class OratsError(RuntimeError):
    pass


@dataclass(frozen=True)
class OratsResponse:
    """Normalized ORATS response container.

    ORATS endpoints sometimes return a raw list or an object wrapper; this keeps
    calling code simple by always exposing `.rows` as a list.
    """

    rows: list[dict]
    raw: Any


def _make_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class OratsClient:
    """ORATS Delayed Data API v2 client with caching + rate-limit awareness."""

    def __init__(
        self,
        token: str,
        base_url: str = ORATS_BASE_URL,
        timeout_s: float = 20.0,
        cache_ttl_s: int = 6 * 60 * 60,
        cache_maxsize: int = 10_000,
    ) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._session = _make_session()
        self._cache = TTLCache(maxsize=cache_maxsize, ttl=cache_ttl_s)
        self._cache_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "OratsClient":
        token = os.getenv("ORATS_TOKEN")
        if not token:
            raise OratsError("Missing required env var ORATS_TOKEN")
        logging.getLogger(cls.__name__).info("Loaded ORATS_TOKEN from environment (len=%d)", len(token))
        return cls(token=token)

    def _cache_get(self, key: Tuple[Any, ...]) -> Optional[OratsResponse]:
        with self._cache_lock:
            return self._cache.get(key)

    def _cache_set(self, key: Tuple[Any, ...], value: OratsResponse) -> None:
        with self._cache_lock:
            self._cache[key] = value

    def get(self, path: str, params: Dict[str, Any]) -> OratsResponse:
        """GET an ORATS v2 endpoint under /datav2.

        Adds token query param (never returned to frontend).
        Caches the normalized response.
        """
        # cache key should not include token
        key = ("GET", path, tuple(sorted((k, str(v)) for k, v in params.items())))
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        url = f"{self._base_url}/{path.lstrip('/')}"
        q = dict(params)
        q["token"] = self._token

        # Extra rate-limit friendliness: if ORATS returns 429 without Retry-After,
        # do a small manual backoff and retry a couple times.
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                resp = self._session.get(url, params=q, timeout=self._timeout_s)
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    sleep_s = float(retry_after) if retry_after else min(2.0 * attempt, 6.0)
                    self._log.warning("ORATS 429 rate-limited; sleeping %.1fs (attempt %d/3)", sleep_s, attempt)
                    time.sleep(sleep_s)
                    continue

                # For certain hist endpoints, ORATS returns 404 on dates with no data (weekends/holidays).
                # We treat that as "empty result" so callers can probe for the nearest trading day.
                if resp.status_code == 404 and path in ("/hist/dailies", "/hist/cores"):
                    try:
                        data = resp.json()
                    except ValueError:
                        data = {"data": []}
                    # Force empty rows even if the body is a dict like {"message":"Not Found."}
                    out = OratsResponse(rows=[], raw=data)
                    self._cache_set(key, out)
                    return out

                # Auth / entitlement errors should not be retried.
                if resp.status_code in (401, 403):
                    snippet = resp.text[:500]
                    raise OratsError(f"ORATS auth/entitlement error {resp.status_code} for {path}: {snippet}")

                if resp.status_code >= 400:
                    # include a small response snippet to help debugging
                    snippet = resp.text[:500]
                    raise OratsError(f"ORATS error {resp.status_code} for {path}: {snippet}")

                data = resp.json()
                rows = self._normalize_rows(data)
                out = OratsResponse(rows=rows, raw=data)
                self._cache_set(key, out)
                return out
            except (requests.RequestException, ValueError, OratsError) as e:
                last_err = e
                # Don't retry auth/entitlement errors.
                if isinstance(e, OratsError) and ("auth/entitlement error 401" in str(e) or "auth/entitlement error 403" in str(e)):
                    raise
                # brief exponential backoff; urllib3 Retry already does some, but this
                # helps for JSON decode edge cases and intermittent failures
                time.sleep(min(0.5 * (2**(attempt - 1)), 3.0))

        raise OratsError(f"Failed ORATS request after retries: {path} params={params}") from last_err

    @staticmethod
    def _normalize_rows(data: Any) -> list[dict]:
        # ORATS sometimes returns:
        # - a list of dicts
        # - an object wrapper like {"data": [...]} or {"rows": [...]}
        if data is None:
            return []
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("data", "rows", "result", "results"):
                v = data.get(key)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
            # if it's a single-row dict, wrap it
            if all(isinstance(k, str) for k in data.keys()):
                return [data]
        return []

    # Convenience wrappers for the three required endpoints
    def hist_earnings(self, ticker: str) -> OratsResponse:
        return self.get("/hist/earnings", {"ticker": ticker})

    def hist_cores(self, ticker: str, trade_date: str, fields: str) -> OratsResponse:
        return self.get("/hist/cores", {"ticker": ticker, "tradeDate": trade_date, "fields": fields})

    def hist_dailies(self, ticker: str, trade_date: str, fields: str) -> OratsResponse:
        return self.get("/hist/dailies", {"ticker": ticker, "tradeDate": trade_date, "fields": fields})


