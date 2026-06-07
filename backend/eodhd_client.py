from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from cachetools import TTLCache
import urllib.parse
import urllib.request


EODHD_BASE_URL = "https://eodhd.com/api"

LOG = logging.getLogger(__name__)


class EodhdError(RuntimeError):
    pass


@dataclass(frozen=True)
class EodhdResponse:
    """Normalized EODHD response container.

    Always exposes `.rows` as a list of dicts, regardless of upstream format.
    """

    rows: list[dict]
    raw: Any


# ---------------------------------------------------------------------------
# SSL helpers (mirrors orats_client.py pattern for macOS compatibility)
# ---------------------------------------------------------------------------

def _env_truthy(name: str) -> bool:
    v = os.getenv(name)
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "t", "yes", "y", "on")


def _build_ssl_context() -> ssl.SSLContext:
    if os.getenv("EODHD_SSL_VERIFY") is not None and not _env_truthy("EODHD_SSL_VERIFY"):
        return ssl._create_unverified_context()

    cafile = (
        os.getenv("EODHD_CA_BUNDLE")
        or os.getenv("SSL_CERT_FILE")
        or os.getenv("REQUESTS_CA_BUNDLE")
    )
    if cafile and os.path.exists(cafile):
        return ssl.create_default_context(cafile=cafile)

    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# ---------------------------------------------------------------------------
# Low-level HTTP
# ---------------------------------------------------------------------------

def _http_get(url: str, params: Dict[str, Any], timeout_s: float) -> tuple[int, Dict[str, str], bytes]:
    q = urllib.parse.urlencode({k: str(v) for k, v in (params or {}).items() if v is not None})
    full = f"{url}?{q}" if q else url
    req = urllib.request.Request(
        full,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "NRGXLabs/1.0"},
    )
    try:
        ctx = _build_ssl_context()
        with urllib.request.urlopen(req, timeout=float(timeout_s), context=ctx) as resp:
            status = int(getattr(resp, "status", 200))
            headers = {str(k): str(v) for k, v in (getattr(resp, "headers", {}) or {}).items()}
            body = resp.read() or b""
            return status, headers, body
    except urllib.error.HTTPError as e:
        status = int(getattr(e, "code", 500))
        headers = {str(k): str(v) for k, v in (getattr(e, "headers", {}) or {}).items()}
        body = e.read() if hasattr(e, "read") else b""
        return status, headers, body
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise EodhdError(
                "SSL certificate verification failed while calling EODHD. "
                "Fix: install certifi, or set SSL_CERT_FILE / EODHD_CA_BUNDLE. "
                "Dev-only fallback: set EODHD_SSL_VERIFY=0."
            ) from e
        raise


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class EodhdClient:
    """EODHD All-In-One API client with caching and retry logic.

    Covers:
    - End-of-day historical prices (equities, indices, FX, commodities, bonds)
    - Live / delayed stock quotes (real-time OHLCV + extended US quotes)
    - US Treasury yield/bill/long-term rate endpoints
    - Macro indicator endpoint
    """

    def __init__(
        self,
        token: str,
        base_url: str = EODHD_BASE_URL,
        timeout_s: float = 30.0,
        cache_ttl_s: int = 6 * 60 * 60,
        cache_maxsize: int = 5_000,
        live_cache_ttl_s: int = 30,
        live_cache_maxsize: int = 500,
    ) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._cache = TTLCache(maxsize=cache_maxsize, ttl=cache_ttl_s)
        self._cache_lock = threading.Lock()
        self._live_cache = TTLCache(maxsize=live_cache_maxsize, ttl=live_cache_ttl_s)
        self._live_cache_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "EodhdClient":
        token = os.getenv("EODHD_API_TOKEN")
        if not token:
            raise EodhdError("Missing required env var EODHD_API_TOKEN")
        logging.getLogger(cls.__name__).info(
            "Loaded EODHD_API_TOKEN from environment (len=%d)", len(token)
        )
        return cls(token=token)

    # -- cache helpers -------------------------------------------------------

    def _cache_get(self, key: Tuple[Any, ...]) -> Optional[EodhdResponse]:
        with self._cache_lock:
            return self._cache.get(key)

    def _cache_set(self, key: Tuple[Any, ...], value: EodhdResponse) -> None:
        with self._cache_lock:
            self._cache[key] = value

    def _live_cache_get(self, key: Tuple[Any, ...]) -> Optional[EodhdResponse]:
        with self._live_cache_lock:
            return self._live_cache.get(key)

    def _live_cache_set(self, key: Tuple[Any, ...], value: EodhdResponse) -> None:
        with self._live_cache_lock:
            self._live_cache[key] = value

    # -- generic GET with retry + rate-limit handling ------------------------

    def _get(self, url: str, params: Dict[str, Any]) -> EodhdResponse:
        """Low-level GET with retry on 429 and normalization."""
        key = ("GET", url, tuple(sorted((k, str(v)) for k, v in params.items() if k != "api_token")))
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        q = dict(params)
        q["api_token"] = self._token

        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                status, headers, body = _http_get(url, q, self._timeout_s)
            except Exception as exc:
                last_err = exc
                time.sleep(min(2.0 * attempt, 8.0))
                continue

            if status == 429:
                retry_after = headers.get("Retry-After")
                sleep_s = float(retry_after) if retry_after else min(2.0 * attempt, 8.0)
                self._log.warning(
                    "EODHD 429 rate-limited; sleeping %.1fs (attempt %d/3)", sleep_s, attempt
                )
                time.sleep(sleep_s)
                continue

            if status in (401, 403):
                snippet = (body.decode("utf-8", errors="ignore") or "")[:500]
                raise EodhdError(f"EODHD auth error {status}: {snippet}")

            if status >= 400:
                snippet = (body.decode("utf-8", errors="ignore") or "")[:500]
                raise EodhdError(f"EODHD error {status} for {url}: {snippet}")

            data = json.loads(body.decode("utf-8") or "[]")
            rows = self._normalize_rows(data)
            out = EodhdResponse(rows=rows, raw=data)
            self._cache_set(key, out)
            return out

        if last_err:
            raise EodhdError(f"EODHD request failed after 3 attempts: {last_err}") from last_err
        raise EodhdError("EODHD request failed after 3 attempts (unknown)")

    @staticmethod
    def _normalize_rows(data: Any) -> list[dict]:
        """Turn any EODHD response shape into a flat list of dicts."""
        if data is None:
            return []
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            # UST endpoints wrap in {"meta": ..., "data": [...]}
            for key in ("data", "rows", "items", "result", "results"):
                v = data.get(key)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
            # Single-object response
            return [data]
        return []

    # -- live GET with short-TTL cache ---------------------------------------

    def _get_live(self, url: str, params: Dict[str, Any]) -> EodhdResponse:
        """GET with short-TTL live cache (30s) for real-time / delayed quotes."""
        key = ("GET_LIVE", url, tuple(sorted((k, str(v)) for k, v in params.items() if k != "api_token")))
        cached = self._live_cache_get(key)
        if cached is not None:
            return cached

        q = dict(params)
        q["api_token"] = self._token

        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                status, headers, body = _http_get(url, q, self._timeout_s)
            except Exception as exc:
                last_err = exc
                time.sleep(min(2.0 * attempt, 8.0))
                continue

            if status == 429:
                retry_after = headers.get("Retry-After")
                sleep_s = float(retry_after) if retry_after else min(2.0 * attempt, 8.0)
                self._log.warning("EODHD 429 rate-limited (live); sleeping %.1fs (attempt %d/3)", sleep_s, attempt)
                time.sleep(sleep_s)
                continue

            if status in (401, 403):
                snippet = (body.decode("utf-8", errors="ignore") or "")[:500]
                raise EodhdError(f"EODHD auth error {status}: {snippet}")

            if status >= 400:
                snippet = (body.decode("utf-8", errors="ignore") or "")[:500]
                raise EodhdError(f"EODHD error {status} for {url}: {snippet}")

            data = json.loads(body.decode("utf-8") or "[]")
            rows = self._normalize_rows(data)
            out = EodhdResponse(rows=rows, raw=data)
            self._live_cache_set(key, out)
            return out

        if last_err:
            raise EodhdError(f"EODHD live request failed after 3 attempts: {last_err}") from last_err
        raise EodhdError("EODHD live request failed after 3 attempts (unknown)")

    # -----------------------------------------------------------------------
    # Public API: Live / Delayed Stock Quotes
    # -----------------------------------------------------------------------

    def get_live_quote(
        self,
        symbol: str,
        *,
        extra_symbols: Optional[str] = None,
    ) -> EodhdResponse:
        """Live (delayed) OHLCV snapshot for one or more tickers.

        GET /api/real-time/{SYMBOL}?s=SYM2,SYM3&fmt=json
        Returns: code, timestamp, gmtoffset, open, high, low, close, volume,
                 previousClose, change, change_p
        Delay: 15-20 min for stocks, ~1 min for FX.
        Cost: 1 API call per ticker.
        """
        url = f"{self._base_url}/real-time/{symbol}"
        params: Dict[str, Any] = {"fmt": "json"}
        if extra_symbols:
            params["s"] = extra_symbols
        return self._get_live(url, params)

    def get_us_quote_delayed(
        self,
        symbols: str,
    ) -> EodhdResponse:
        """Extended delayed quote for US equities with bid/ask, volume, MAs.

        GET /api/us-quote-delayed?s=AAPL.US,TSLA.US&fmt=json
        Returns per symbol: lastTradePrice, bidPrice/Size, askPrice/Size,
        open, high, low, volume, previousClosePrice, change, changePercent,
        fiftyDayAveragePrice, twoHundredDayAveragePrice, averageVolume,
        fiftyTwoWeekHigh, fiftyTwoWeekLow, marketCap, pe, etc.
        Cost: 1 API call per ticker.
        """
        url = f"{self._base_url}/us-quote-delayed"
        params: Dict[str, Any] = {"fmt": "json", "s": symbols}
        resp = self._get_live(url, params)
        raw = resp.raw
        if isinstance(raw, dict) and "data" in raw:
            data_obj = raw["data"]
            if isinstance(data_obj, dict):
                return EodhdResponse(rows=list(data_obj.values()), raw=raw)
        return resp

    # -----------------------------------------------------------------------
    # Public API: EOD Historical Prices
    # -----------------------------------------------------------------------

    def get_eod(
        self,
        symbol: str,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        period: str = "d",
    ) -> EodhdResponse:
        """Fetch end-of-day historical data for a single ticker.

        symbol: EODHD format, e.g. "GDAXI.INDX", "EURUSD.FOREX", "GLD.US"
        """
        url = f"{self._base_url}/eod/{symbol}"
        params: Dict[str, Any] = {"fmt": "json", "period": period}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        return self._get(url, params)

    def get_eod_bulk(
        self,
        exchange: str,
        *,
        date: Optional[str] = None,
        symbols: Optional[str] = None,
    ) -> EodhdResponse:
        """Bulk EOD for an entire exchange or specific symbols.

        exchange: e.g. "US", "INDX", "FOREX"
        symbols: comma-separated, e.g. "AAPL,MSFT"
        """
        url = f"{self._base_url}/eod-bulk-last-day/{exchange}"
        params: Dict[str, Any] = {"fmt": "json"}
        if date:
            params["date"] = date
        if symbols:
            params["symbols"] = symbols
        return self._get(url, params)

    # -----------------------------------------------------------------------
    # Public API: US Treasury Rates
    # -----------------------------------------------------------------------

    def get_ust_yield_rates(self, year: Optional[int] = None) -> EodhdResponse:
        """Daily Treasury Par Yield Curve Rates.

        1 API call per request. filter[year] optional, defaults to current year.
        Returns rows with: date, tenor (e.g. "2Y", "10Y"), rate.
        """
        url = f"{self._base_url}/ust/yield-rates"
        params: Dict[str, Any] = {}
        if year is not None:
            params["filter[year]"] = year
        return self._get(url, params)

    def get_ust_long_term_rates(self, year: Optional[int] = None) -> EodhdResponse:
        """Daily Treasury Long-Term Rates.

        1 API call per request. filter[year] optional, defaults to current year.
        """
        url = f"{self._base_url}/ust/long-term-rates"
        params: Dict[str, Any] = {}
        if year is not None:
            params["filter[year]"] = year
        return self._get(url, params)

    def get_ust_real_yield_rates(self, year: Optional[int] = None) -> EodhdResponse:
        """Daily Treasury Par Real Yield Curve Rates.

        1 API call per request. filter[year] optional, defaults to current year.
        """
        url = f"{self._base_url}/ust/real-yield-rates"
        params: Dict[str, Any] = {}
        if year is not None:
            params["filter[year]"] = year
        return self._get(url, params)

    # -----------------------------------------------------------------------
    # Public API: Macro Indicators
    # -----------------------------------------------------------------------

    def get_macro_indicator(
        self,
        country: str,
        indicator: str = "gdp_current_usd",
    ) -> EodhdResponse:
        """Fetch a macroeconomic indicator for a country.

        country: ISO Alpha-3 (e.g. "USA", "DEU", "JPN")
        indicator: e.g. "real_interest_rate", "inflation_consumer_prices_annual"
        Each request consumes 10 API calls.
        """
        url = f"{self._base_url}/macro-indicator/{country}"
        params: Dict[str, Any] = {"fmt": "json", "indicator": indicator}
        return self._get(url, params)

    # -----------------------------------------------------------------------
    # Public API: Calendar (Earnings, IPOs, Splits)
    # -----------------------------------------------------------------------

    def get_calendar_earnings(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        symbols: Optional[str] = None,
    ) -> EodhdResponse:
        """Fetch historical and upcoming earnings dates.

        Query by date window (from/to) **or** by symbols (comma-separated
        EODHD tickers like ``AAPL.US,MSFT.US``).  1 API call per request.

        Each row includes: code, report_date, date, before_after_market,
        currency, actual, estimate, difference, percent.
        """
        url = f"{self._base_url}/calendar/earnings"
        params: Dict[str, Any] = {"fmt": "json"}
        if symbols:
            params["symbols"] = symbols
        else:
            if from_date:
                params["from"] = from_date
            if to_date:
                params["to"] = to_date
        return self._get(url, params)

    def get_calendar_trends(self, *, symbols: str) -> EodhdResponse:
        """Forward-looking earnings trends (consensus EPS, revenue estimates).

        symbols: comma-separated EODHD tickers (e.g. ``AAPL.US,MSFT.US``).
        1 API call per request.  JSON-only.

        Returns nested structure: { type, description, symbols, trends: [[...], ...] }
        where each inner list corresponds to a requested symbol.
        """
        url = f"{self._base_url}/calendar/trends"
        params: Dict[str, Any] = {"fmt": "json", "symbols": symbols}
        return self._get(url, params)

    # -----------------------------------------------------------------------
    # Public API: News
    # -----------------------------------------------------------------------

    def get_news(
        self,
        *,
        ticker: Optional[str] = None,
        topic: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> EodhdResponse:
        """Fetch financial news articles.

        ticker:    EODHD ticker (e.g. "AAPL.US") for company news.
        topic:     Topic tag (e.g. "market", "earnings", "mergers").
                   Use topic **or** ticker, not both.
        from_date: Start date filter (YYYY-MM-DD).
        to_date:   End date filter (YYYY-MM-DD).
        limit:     1-1000 articles per request (default 50).
        offset:    Pagination offset.

        1 API call per request.
        """
        url = f"{self._base_url}/news"
        params: Dict[str, Any] = {
            "fmt": "json",
            "limit": min(max(int(limit), 1), 1000),
            "offset": max(int(offset), 0),
        }
        if ticker:
            params["s"] = ticker
        if topic:
            params["t"] = topic
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        return self._get(url, params)

    # -----------------------------------------------------------------------
    # Public API: Stock Market Screener
    # -----------------------------------------------------------------------

    def get_screener(
        self,
        *,
        filters: Optional[str] = None,
        signals: Optional[str] = None,
        sort: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> EodhdResponse:
        """Screen stocks by fundamental and technical criteria.

        filters: JSON-encoded list of filter triples, e.g.
            '[["market_capitalization",">",100000000000],["exchange","=","us"]]'
        signals: comma-separated signal names (e.g. "200d_new_hi")
        sort: field_name.asc or field_name.desc
        limit: 1-100 (default 50)
        offset: 0-999

        Each request consumes 5 API calls.
        """
        url = f"{self._base_url}/screener"
        params: Dict[str, Any] = {
            "limit": min(max(int(limit), 1), 100),
            "offset": max(int(offset), 0),
        }
        if filters:
            params["filters"] = filters
        if signals:
            params["signals"] = signals
        if sort:
            params["sort"] = sort
        return self._get(url, params)

    # -----------------------------------------------------------------------
    # Public API: Fundamentals
    # -----------------------------------------------------------------------

    def get_fundamentals(self, symbol: str) -> dict:
        """Fetch fundamental data for a ticker (Highlights, Financials, etc.).

        symbol: EODHD format, e.g. "ARCC.US"
        Returns raw dict with keys like Highlights, Financials, etc.
        """
        url = f"{self._base_url}/fundamentals/{symbol}"
        params: Dict[str, Any] = {"fmt": "json"}
        resp = self._get(url, params)
        return resp.raw if resp and hasattr(resp, "raw") else {}

    def get_book_value(self, symbol: str) -> Optional[float]:
        """Extract book value per share from fundamentals Highlights."""
        try:
            data = self.get_fundamentals(symbol)
            highlights = data.get("Highlights") or {}
            bv = highlights.get("BookValue")
            if bv is not None:
                return float(bv)
        except Exception:
            pass
        return None

    def get_etf_nav(self, symbol: str) -> Optional[float]:
        """Extract latest NAV for an ETF from EODHD fundamentals.

        Checks ETF_Data.NetAssetValue first, then falls back to
        Technicals.BookValue as a proxy.
        """
        try:
            data = self.get_fundamentals(symbol)
            etf_data = data.get("ETF_Data") or {}
            nav = etf_data.get("NetAssetValue") or etf_data.get("Net_Assets")
            if nav:
                return float(nav)
            technicals = data.get("Technicals") or {}
            bv = technicals.get("BookValue")
            if bv:
                return float(bv)
        except Exception:
            pass
        return None
