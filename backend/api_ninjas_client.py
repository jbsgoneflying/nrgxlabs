"""
API Ninjas Client for Earnings Calendar

Premium features used:
- /v1/upcomingearnings - Query by date range, exchange, ticker
- earnings_timing field - before_market, during_market, after_market
- earnings_call_timestamp - Unix timestamp for exact timing

API Documentation: https://api-ninjas.com/api/earningscalendar
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


API_NINJAS_BASE_URL = "https://api.api-ninjas.com/v1"


class ApiNinjasError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApiNinjasResponse:
    rows: List[dict]
    raw: Any


def _env_truthy(name: str) -> bool:
    v = os.getenv(name)
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("1", "true", "t", "yes", "y", "on")


def _build_ssl_context() -> ssl.SSLContext:
    # Dev-only escape hatch
    if os.getenv("API_NINJAS_SSL_VERIFY") is not None and not _env_truthy("API_NINJAS_SSL_VERIFY"):
        return ssl._create_unverified_context()
    cafile = os.getenv("API_NINJAS_CA_BUNDLE") or os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE")
    if cafile and os.path.exists(cafile):
        return ssl.create_default_context(cafile=cafile)
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _http_get_with_header_auth(
    url: str,
    params: Dict[str, Any],
    api_key: str,
    timeout_s: float,
) -> tuple[int, Dict[str, str], bytes]:
    """HTTP GET with API key in X-Api-Key header (API Ninjas auth method)."""
    q = urllib.parse.urlencode({k: str(v) for k, v in (params or {}).items() if v is not None})
    full = f"{url}?{q}" if q else url
    headers = {
        "Accept": "application/json",
        "User-Agent": "Breach-Algo/1.0",
        "X-Api-Key": api_key,
    }
    req = urllib.request.Request(full, method="GET", headers=headers)
    ctx = _build_ssl_context()
    with urllib.request.urlopen(req, timeout=float(timeout_s), context=ctx) as resp:
        status = int(getattr(resp, "status", 200))
        resp_headers = {str(k): str(v) for k, v in (getattr(resp, "headers", {}) or {}).items()}
        body = resp.read() or b""
        return status, resp_headers, body


class ApiNinjasClient:
    """
    API Ninjas client for Earnings Calendar (Premium features).
    
    Premium endpoints used:
    - /upcomingearnings - Get upcoming earnings with date range filtering
    
    Premium fields:
    - earnings_timing: before_market, during_market, after_market
    - earnings_call_timestamp: Unix timestamp
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = API_NINJAS_BASE_URL,
        timeout_s: float = 30.0,
    ) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._api_key = str(api_key)
        self._base_url = str(base_url).rstrip("/")
        self._timeout_s = float(timeout_s)

    @classmethod
    def from_env(cls) -> "ApiNinjasClient":
        key = os.getenv("API_NINJAS_API_KEY")
        if not key:
            raise ApiNinjasError("Missing required env var API_NINJAS_API_KEY")
        timeout = float(os.getenv("API_NINJAS_TIMEOUT_S") or 30.0)
        logging.getLogger(cls.__name__).info(
            "Loaded API_NINJAS_API_KEY from environment (len=%d)", len(key)
        )
        return cls(api_key=key, timeout_s=timeout)

    @staticmethod
    def _normalize_rows(data: Any) -> List[dict]:
        """Normalize API response to list of dicts."""
        if data is None:
            return []
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            # API Ninjas may return data wrapped in various keys
            for key in ("data", "rows", "result", "results", "earnings"):
                v = data.get(key)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
            # Single object response
            if all(isinstance(k, str) for k in data.keys()):
                return [data]
        return []

    def get(self, path: str, params: Dict[str, Any]) -> ApiNinjasResponse:
        """Make GET request to API Ninjas."""
        url = f"{self._base_url}/{path.lstrip('/')}"
        try:
            status, _headers, body = _http_get_with_header_auth(
                url, params, self._api_key, self._timeout_s
            )
        except urllib.error.HTTPError as e:
            snippet = ""
            try:
                snippet = (e.read() or b"").decode("utf-8", errors="ignore")[:500]
            except Exception:
                pass
            raise ApiNinjasError(f"API Ninjas HTTP error {e.code} for {path}: {snippet}") from e
        except urllib.error.URLError as e:
            raise ApiNinjasError(f"API Ninjas URL error for {path}: {e.reason}") from e

        try:
            data = json.loads(body.decode("utf-8") or "null")
        except Exception as e:
            raise ApiNinjasError(
                f"API Ninjas returned non-JSON response for {path}: {type(e).__name__}"
            ) from e

        if status >= 400:
            snippet = (body.decode("utf-8", errors="ignore") or "")[:500]
            raise ApiNinjasError(f"API Ninjas error {status} for {path}: {snippet}")

        rows = self._normalize_rows(data)
        return ApiNinjasResponse(rows=rows, raw=data)

    def upcoming_earnings(
        self,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        exchange: Optional[str] = None,
        ticker: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> ApiNinjasResponse:
        """
        Get upcoming earnings dates with filtering (Premium endpoint).
        
        Args:
            start_date: Start date YYYY-MM-DD (defaults to today)
            end_date: End date YYYY-MM-DD (defaults to 10 years from today)
            exchange: Filter by exchange (NASDAQ, NYSE, etc.)
            ticker: Filter by specific ticker
            limit: Max results (1-100, default 100)
            offset: Pagination offset
            
        Returns:
            ApiNinjasResponse with rows containing:
            - ticker: Stock symbol
            - date: Earnings date YYYY-MM-DD
            - eps_estimated: Estimated EPS (may be null)
            - revenue_estimated: Estimated revenue (may be null)
            - exchange: Exchange code
            - earnings_timing: before_market, during_market, after_market (Premium)
            - earnings_call_timestamp: Unix timestamp (Premium)
        """
        params: Dict[str, Any] = {
            "limit": min(100, max(1, int(limit))),
            "offset": max(0, int(offset)),
        }
        if start_date:
            params["start_date"] = str(start_date)[:10]
        if end_date:
            params["end_date"] = str(end_date)[:10]
        if exchange:
            params["exchange"] = str(exchange).upper()
        if ticker:
            params["ticker"] = str(ticker).upper()

        return self.get("/upcomingearnings", params)

    def earnings_calendar(
        self,
        *,
        ticker: Optional[str] = None,
        date: Optional[str] = None,
        show_upcoming: bool = True,
        limit: int = 10,
        offset: int = 0,
    ) -> ApiNinjasResponse:
        """
        Get earnings calendar data for a ticker or date.
        
        Args:
            ticker: Company ticker symbol (e.g., MSFT)
            date: Date YYYY-MM-DD to get all earnings for that date
            show_upcoming: Whether to include upcoming dates (Premium)
            limit: Max results (1-10, default 10)
            offset: Pagination offset (Premium)
            
        Returns:
            ApiNinjasResponse with rows containing:
            - date: Earnings date
            - ticker: Stock symbol
            - actual_eps: Actual EPS (for past earnings)
            - estimated_eps: Estimated EPS
            - actual_revenue: Actual revenue
            - estimated_revenue: Estimated revenue
            - earnings_call_timestamp: Unix timestamp (Premium)
            - earnings_timing: before_market, during_market, after_market (Premium)
        """
        params: Dict[str, Any] = {
            "limit": min(10, max(1, int(limit))),
        }
        if ticker:
            params["ticker"] = str(ticker).upper()
        if date:
            params["date"] = str(date)[:10]
        if show_upcoming:
            params["show_upcoming"] = "true"
        if offset > 0:
            params["offset"] = int(offset)

        return self.get("/earningscalendar", params)

    def fetch_all_upcoming_earnings(
        self,
        *,
        start_date: str,
        end_date: str,
        exchange: Optional[str] = None,
        max_results: int = 1000,
    ) -> List[dict]:
        """
        Fetch all upcoming earnings in date range with pagination.
        
        Args:
            start_date: Start date YYYY-MM-DD
            end_date: End date YYYY-MM-DD
            exchange: Optional exchange filter
            max_results: Maximum total results to fetch
            
        Returns:
            List of earnings dicts
        """
        all_rows: List[dict] = []
        offset = 0
        page_size = 100

        while len(all_rows) < max_results:
            try:
                resp = self.upcoming_earnings(
                    start_date=start_date,
                    end_date=end_date,
                    exchange=exchange,
                    limit=page_size,
                    offset=offset,
                )
                batch = resp.rows or []
                if not batch:
                    break
                all_rows.extend(batch)
                if len(batch) < page_size:
                    break
                offset += page_size
            except ApiNinjasError as e:
                self._log.warning(f"Error fetching upcoming earnings page {offset}: {e}")
                break

        self._log.info(
            f"Fetched {len(all_rows)} upcoming earnings from {start_date} to {end_date}"
        )
        return all_rows[:max_results]
