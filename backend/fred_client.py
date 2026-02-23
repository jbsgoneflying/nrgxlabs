"""
FRED (Federal Reserve Economic Data) API Client

Free API -- no key required for basic JSON access.
Used by Engine 9 (Credit Stress Drift) for credit spread and yield curve data.

Series tracked:
- BAMLH0A0HYM2  ICE BofA US High Yield OAS (daily)
- BAMLC0A4CBBB  ICE BofA BBB US Corporate OAS
- BAMLC0A0CM    ICE BofA US Corporate Master OAS (IG)
- DGS2          2-Year Treasury Constant Maturity Rate
- DGS10         10-Year Treasury Constant Maturity Rate
- FEDFUNDS      Federal Funds Effective Rate
- DRTSCILM      Senior Loan Officer Survey -- tightening % (quarterly)
"""
from __future__ import annotations

import json
import logging
import ssl
import os
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from cachetools import TTLCache

FRED_BASE_URL = "https://api.stlouisfed.org/fred"

SERIES_HY_OAS = "BAMLH0A0HYM2"
SERIES_BBB_OAS = "BAMLC0A4CBBB"
SERIES_IG_OAS = "BAMLC0A0CM"
SERIES_DGS2 = "DGS2"
SERIES_DGS10 = "DGS10"
SERIES_FEDFUNDS = "FEDFUNDS"
SERIES_SLOOS = "DRTSCILM"

ALL_CREDIT_SERIES = [SERIES_HY_OAS, SERIES_BBB_OAS, SERIES_IG_OAS]
ALL_YIELD_SERIES = [SERIES_DGS2, SERIES_DGS10]


class FredError(RuntimeError):
    pass


@dataclass(frozen=True)
class FredObservation:
    date: str
    value: Optional[float]


@dataclass(frozen=True)
class FredSeriesResult:
    series_id: str
    observations: List[FredObservation]
    raw: Any


def _build_ssl_context() -> ssl.SSLContext:
    cafile = os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE")
    if cafile and os.path.exists(cafile):
        return ssl.create_default_context(cafile=cafile)
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class FredClient:
    """
    Lightweight FRED API client with TTL caching.

    FRED's free tier allows ~120 requests/minute without a key, but providing
    one (env var FRED_API_KEY) increases limits and is recommended for production.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = FRED_BASE_URL,
        timeout_s: float = 20.0,
        cache_ttl_s: int = 3600,
        cache_maxsize: int = 500,
    ) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._cache: TTLCache = TTLCache(maxsize=cache_maxsize, ttl=cache_ttl_s)
        self._cache_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "FredClient":
        key = os.getenv("FRED_API_KEY")
        if not key:
            logging.getLogger(cls.__name__).info(
                "FRED_API_KEY not set -- using keyless access (rate-limited)"
            )
        return cls(api_key=key or None)

    def _cache_get(self, key: str) -> Any:
        with self._cache_lock:
            return self._cache.get(key)

    def _cache_set(self, key: str, value: Any) -> None:
        with self._cache_lock:
            self._cache[key] = value

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        params = dict(params)
        params["file_type"] = "json"
        if self._api_key:
            params["api_key"] = self._api_key

        cache_key = f"fred:{path}:{json.dumps({k: v for k, v in sorted(params.items()) if k != 'api_key'}, sort_keys=True)}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        q = urllib.parse.urlencode({k: str(v) for k, v in params.items() if v is not None})
        url = f"{self._base_url}/{path.lstrip('/')}?{q}"

        req = urllib.request.Request(url, method="GET", headers={
            "Accept": "application/json",
            "User-Agent": "Breach-Algo/1.0",
        })
        ctx = _build_ssl_context()

        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s, context=ctx) as resp:
                body = resp.read() or b""
                data = json.loads(body.decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            snippet = ""
            try:
                snippet = (e.read() or b"").decode("utf-8", errors="ignore")[:500]
            except Exception:
                pass
            raise FredError(f"FRED HTTP {e.code} for {path}: {snippet}") from e
        except urllib.error.URLError as e:
            raise FredError(f"FRED URL error for {path}: {e.reason}") from e
        except Exception as e:
            raise FredError(f"FRED request failed for {path}: {type(e).__name__}: {e}") from e

        self._cache_set(cache_key, data)
        return data

    def get_series(
        self,
        series_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 10000,
    ) -> FredSeriesResult:
        """
        Fetch observations for a FRED series.

        Returns FredSeriesResult with parsed observations (date, value).
        Dots ('.') in FRED data are treated as missing (None).
        """
        if not start_date:
            start_date = (date.today() - timedelta(days=365)).isoformat()
        if not end_date:
            end_date = date.today().isoformat()

        params: Dict[str, Any] = {
            "series_id": series_id,
            "observation_start": start_date,
            "observation_end": end_date,
            "sort_order": "asc",
            "limit": limit,
        }

        data = self._get("series/observations", params)

        raw_obs = data.get("observations", []) if isinstance(data, dict) else []
        observations: List[FredObservation] = []
        for obs in raw_obs:
            d = obs.get("date", "")
            v_str = obs.get("value", ".")
            v: Optional[float] = None
            if v_str not in (".", "", None):
                try:
                    v = float(v_str)
                except (ValueError, TypeError):
                    pass
            observations.append(FredObservation(date=d, value=v))

        self._log.debug(
            "FRED %s: %d observations (%s to %s)",
            series_id, len(observations), start_date, end_date,
        )
        return FredSeriesResult(series_id=series_id, observations=observations, raw=data)

    def get_credit_spreads(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, FredSeriesResult]:
        """Convenience: fetch HY OAS, BBB OAS, and IG OAS together."""
        results = {}
        for sid in ALL_CREDIT_SERIES:
            try:
                results[sid] = self.get_series(sid, start_date, end_date)
            except FredError as e:
                self._log.warning("Failed to fetch %s: %s", sid, e)
        return results

    def get_yield_curve(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, FredSeriesResult]:
        """Convenience: fetch 2Y and 10Y treasury yields."""
        results = {}
        for sid in ALL_YIELD_SERIES:
            try:
                results[sid] = self.get_series(sid, start_date, end_date)
            except FredError as e:
                self._log.warning("Failed to fetch %s: %s", sid, e)
        return results

    def get_fed_funds(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> FredSeriesResult:
        """Fetch federal funds effective rate."""
        return self.get_series(SERIES_FEDFUNDS, start_date, end_date)

    def get_sloos(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> FredSeriesResult:
        """Fetch Senior Loan Officer Survey (tightening percentage)."""
        return self.get_series(SERIES_SLOOS, start_date, end_date)
