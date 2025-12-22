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

LOG = logging.getLogger(__name__)


BENZINGA_BASE_URL_V2 = "https://api.benzinga.com/api/v2"
BENZINGA_BASE_URL_V21 = "https://api.benzinga.com/api/v2.1"
BENZINGA_BASE_URL_V1 = "https://api.benzinga.com/api/v1"


class BenzingaError(RuntimeError):
    pass


@dataclass(frozen=True)
class BenzingaResponse:
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


def _normalize_rows(data: Any) -> list[dict]:
    # Benzinga commonly returns:
    # - list of objects (e.g., /news, /signal/option_activity)
    # - wrapper {"earnings":[...]} / {"economics":[...]} / {"ratings":[...]} for calendar endpoints
    if data is None:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("earnings", "economics", "ratings", "items", "data", "rows", "result", "results"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        # sometimes a single object
        if all(isinstance(k, str) for k in data.keys()):
            return [data]
    return []


class BenzingaClient:
    """
    Benzinga API client with caching + retry/backoff.

    Primary references:
    - Calendar v2 endpoints use /api/v2/calendar/... with token and parameters[...] query params.
    - Newsfeed v2 endpoint uses /api/v2/news with token and filters like tickers/dateFrom/dateTo/channels.
    - Signals option activity uses /api/v1/signal/option_activity with token and parameters[...] query params.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url_v2: str = BENZINGA_BASE_URL_V2,
        base_url_v21: str = BENZINGA_BASE_URL_V21,
        base_url_v1: str = BENZINGA_BASE_URL_V1,
        timeout_s: float = 20.0,
        cache_ttl_s: int = 60 * 60,
        cache_maxsize: int = 25_000,
        prefer_v21_calendar: bool = False,
    ) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._api_key = api_key
        self._base_url_v2 = base_url_v2.rstrip("/")
        self._base_url_v21 = base_url_v21.rstrip("/")
        self._base_url_v1 = base_url_v1.rstrip("/")
        self._timeout_s = float(timeout_s)
        self._prefer_v21_calendar = bool(prefer_v21_calendar)
        self._session = _make_session()
        self._cache = TTLCache(maxsize=int(cache_maxsize), ttl=int(cache_ttl_s))
        self._cache_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "BenzingaClient":
        api_key = os.getenv("BENZINGA_API_KEY")
        if not api_key:
            raise BenzingaError("Missing required env var BENZINGA_API_KEY")
        # Optional overrides
        prefer_v21 = str(os.getenv("BENZINGA_PREFER_V21_CALENDAR") or "").strip().lower() in ("1", "true", "yes", "y", "on")
        base_v2 = os.getenv("BENZINGA_BASE_URL_V2") or BENZINGA_BASE_URL_V2
        base_v21 = os.getenv("BENZINGA_BASE_URL_V21") or BENZINGA_BASE_URL_V21
        base_v1 = os.getenv("BENZINGA_BASE_URL_V1") or BENZINGA_BASE_URL_V1
        timeout_s = float(os.getenv("BENZINGA_TIMEOUT_S") or 20.0)
        cache_ttl_s = int(float(os.getenv("BENZINGA_CACHE_TTL_S") or (60 * 60)))
        cache_maxsize = int(float(os.getenv("BENZINGA_CACHE_MAXSIZE") or 25_000))
        logging.getLogger(cls.__name__).info("Loaded BENZINGA_API_KEY from environment (len=%d)", len(api_key))
        return cls(
            api_key=api_key,
            base_url_v2=base_v2,
            base_url_v21=base_v21,
            base_url_v1=base_v1,
            timeout_s=timeout_s,
            cache_ttl_s=cache_ttl_s,
            cache_maxsize=cache_maxsize,
            prefer_v21_calendar=prefer_v21,
        )

    @classmethod
    def from_env_optional(cls) -> Optional["BenzingaClient"]:
        try:
            api_key = os.getenv("BENZINGA_API_KEY")
            if not api_key:
                return None
            return cls.from_env()
        except Exception:
            return None

    def _cache_get(self, key: Tuple[Any, ...]) -> Optional[BenzingaResponse]:
        with self._cache_lock:
            return self._cache.get(key)

    def _cache_set(self, key: Tuple[Any, ...], value: BenzingaResponse) -> None:
        with self._cache_lock:
            self._cache[key] = value

    def get(self, *, base: str, path: str, params: Dict[str, Any]) -> BenzingaResponse:
        """
        GET a Benzinga endpoint.

        - base: "v2" | "v21" | "v1"
        - path: "/calendar/earnings" or "/news" or "/signal/option_activity"
        """
        base_key = str(base).lower().strip()
        if base_key not in ("v2", "v21", "v1"):
            raise BenzingaError(f"Invalid base={base!r}")

        # cache key should not include api key
        key = ("GET", base_key, path, tuple(sorted((k, str(v)) for k, v in params.items() if v is not None)))
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        if base_key == "v1":
            base_url = self._base_url_v1
        elif base_key == "v21":
            base_url = self._base_url_v21
        else:
            base_url = self._base_url_v2

        url = f"{base_url}/{path.lstrip('/')}"
        q = dict(params)
        q["token"] = self._api_key

        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                resp = self._session.get(
                    url,
                    params=q,
                    timeout=self._timeout_s,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "Breach-Algo/1.0",
                    },
                )
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    sleep_s = float(retry_after) if retry_after else min(2.0 * attempt, 6.0)
                    self._log.warning("Benzinga 429 rate-limited; sleeping %.1fs (attempt %d/3)", sleep_s, attempt)
                    time.sleep(sleep_s)
                    continue

                if resp.status_code == 404:
                    # Treat as empty dataset (useful for date-range probes).
                    out = BenzingaResponse(rows=[], raw={"status": 404, "body": resp.text[:500]})
                    self._cache_set(key, out)
                    return out

                if resp.status_code in (401, 403):
                    snippet = resp.text[:500]
                    raise BenzingaError(f"Benzinga auth/entitlement error {resp.status_code} for {path}: {snippet}")

                if resp.status_code >= 400:
                    snippet = resp.text[:500]
                    raise BenzingaError(f"Benzinga error {resp.status_code} for {path}: {snippet}")

                data = resp.json()
                rows = _normalize_rows(data)
                out = BenzingaResponse(rows=rows, raw=data)
                self._cache_set(key, out)
                return out
            except (requests.RequestException, ValueError, BenzingaError) as e:
                last_err = e
                # Don't retry auth/entitlement errors.
                if isinstance(e, BenzingaError) and ("auth/entitlement error 401" in str(e) or "auth/entitlement error 403" in str(e)):
                    raise
                time.sleep(min(0.5 * (2 ** (attempt - 1)), 3.0))

        raise BenzingaError(f"Failed Benzinga request after retries: base={base} path={path} params={params}") from last_err

    # ---- Convenience wrappers (used by our app) ----
    def calendar_earnings(
        self,
        *,
        tickers: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        importance: int | None = None,
        updated: int | None = None,
        page: int | None = None,
        pagesize: int | None = None,
    ) -> BenzingaResponse:
        params: Dict[str, Any] = {}
        if page is not None:
            params["page"] = int(page)
        if pagesize is not None:
            params["pagesize"] = int(pagesize)
        if tickers:
            params["parameters[tickers]"] = str(tickers)
        if date:
            params["parameters[date]"] = str(date)[:10]
        if date_from:
            params["parameters[date_from]"] = str(date_from)[:10]
        if date_to:
            params["parameters[date_to]"] = str(date_to)[:10]
        if importance is not None:
            params["parameters[importance]"] = int(importance)
        if updated is not None:
            params["parameters[updated]"] = int(updated)
        base = "v21" if self._prefer_v21_calendar else "v2"
        try:
            return self.get(base=base, path="/calendar/earnings", params=params)
        except BenzingaError:
            # Some accounts/plans have calendar support only on v2.
            if base != "v2":
                return self.get(base="v2", path="/calendar/earnings", params=params)
            raise

    def calendar_economics(
        self,
        *,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        importance: int | None = None,
        updated: int | None = None,
        page: int | None = None,
        pagesize: int | None = None,
        country: str | None = None,
    ) -> BenzingaResponse:
        params: Dict[str, Any] = {}
        if page is not None:
            params["page"] = int(page)
        if pagesize is not None:
            params["pagesize"] = int(pagesize)
        if date:
            params["parameters[date]"] = str(date)[:10]
        if date_from:
            params["parameters[date_from]"] = str(date_from)[:10]
        if date_to:
            params["parameters[date_to]"] = str(date_to)[:10]
        if importance is not None:
            params["parameters[importance]"] = int(importance)
        if updated is not None:
            params["parameters[updated]"] = int(updated)
        if country:
            params["parameters[country]"] = str(country)
        base = "v21" if self._prefer_v21_calendar else "v2"
        try:
            return self.get(base=base, path="/calendar/economics", params=params)
        except BenzingaError:
            if base != "v2":
                return self.get(base="v2", path="/calendar/economics", params=params)
            raise

    def calendar_ratings(
        self,
        *,
        tickers: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        importance: int | None = None,
        updated: int | None = None,
        page: int | None = None,
        pagesize: int | None = None,
    ) -> BenzingaResponse:
        params: Dict[str, Any] = {}
        if page is not None:
            params["page"] = int(page)
        if pagesize is not None:
            params["pagesize"] = int(pagesize)
        if tickers:
            params["parameters[tickers]"] = str(tickers)
        if date:
            params["parameters[date]"] = str(date)[:10]
        if date_from:
            params["parameters[date_from]"] = str(date_from)[:10]
        if date_to:
            params["parameters[date_to]"] = str(date_to)[:10]
        if importance is not None:
            params["parameters[importance]"] = int(importance)
        if updated is not None:
            params["parameters[updated]"] = int(updated)
        base = "v21" if self._prefer_v21_calendar else "v2"
        try:
            return self.get(base=base, path="/calendar/ratings", params=params)
        except BenzingaError:
            if base != "v2":
                return self.get(base="v2", path="/calendar/ratings", params=params)
            raise

    def news(
        self,
        *,
        tickers: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        channels: str | None = None,
        topics: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
        display_output: str | None = None,
        updated_since: int | None = None,
        published_since: int | None = None,
        sort: str | None = None,
    ) -> BenzingaResponse:
        params: Dict[str, Any] = {}
        if page is not None:
            params["page"] = int(page)
        if page_size is not None:
            params["pageSize"] = int(page_size)
        if display_output:
            params["displayOutput"] = str(display_output)
        if tickers:
            params["tickers"] = str(tickers)
        if channels:
            params["channels"] = str(channels)
        if topics:
            params["topics"] = str(topics)
        if date:
            params["date"] = str(date)[:10]
        if date_from:
            params["dateFrom"] = str(date_from)[:10]
        if date_to:
            params["dateTo"] = str(date_to)[:10]
        if updated_since is not None:
            params["updatedSince"] = int(updated_since)
        if published_since is not None:
            params["publishedSince"] = int(published_since)
        if sort:
            params["sort"] = str(sort)
        return self.get(base="v2", path="/news", params=params)

    def signal_option_activity(
        self,
        *,
        tickers: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        updated: int | None = None,
        ids: str | None = None,
        page: int | None = None,
        pagesize: int | None = None,
    ) -> BenzingaResponse:
        params: Dict[str, Any] = {}
        if page is not None:
            params["page"] = int(page)
        if pagesize is not None:
            params["pagesize"] = int(pagesize)
        if tickers:
            params["parameters[tickers]"] = str(tickers)
        if ids:
            params["parameters[id]"] = str(ids)
        if date:
            params["parameters[date]"] = str(date)[:10]
        if date_from:
            params["parameters[date_from]"] = str(date_from)[:10]
        if date_to:
            params["parameters[date_to]"] = str(date_to)[:10]
        if updated is not None:
            params["parameters[updated]"] = int(updated)
        return self.get(base="v1", path="/signal/option_activity", params=params)


