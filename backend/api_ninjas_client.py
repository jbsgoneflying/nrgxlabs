"""
API Ninjas Client for Earnings Calendar and Transcripts

Premium features used:
- /v1/upcomingearnings - Query by date range, exchange, ticker
- earnings_timing field - before_market, during_market, after_market
- earnings_call_timestamp - Unix timestamp for exact timing
- /v1/earningstranscriptsearch - Search available transcripts
- /v1/earningstranscript - Get full transcript text

API Documentation: 
- https://api-ninjas.com/api/earningscalendar
- https://api-ninjas.com/api/earningscalltranscript
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

    def get_historical_timing(self, ticker: str) -> Optional[str]:
        """
        Get the most recent historical earnings_timing for a ticker.
        
        Since future earnings don't have timing info, we look at past earnings
        to predict the timing (companies almost always stick to their BMO/AMC pattern).
        
        Args:
            ticker: Stock symbol
            
        Returns:
            'before_market', 'after_market', 'during_market', or None
        """
        ticker = str(ticker).upper().strip()
        try:
            # Get last 10 earnings for this ticker to find one with timing
            resp = self.get("/earningscalendar", {
                "ticker": ticker,
                "limit": 10,
            })
            
            rows = resp.rows or []
            
            for row in rows:
                timing = row.get("earnings_timing")
                # Check if timing is valid (not None, not "null" string, not empty)
                if timing is not None and str(timing).strip().lower() not in ("null", "none", ""):
                    result = str(timing).strip().lower()
                    self._log.debug(f"Found historical timing for {ticker}: {result}")
                    return result
            
            # Log if we couldn't find timing
            self._log.debug(f"No historical timing found for {ticker} in {len(rows)} rows")
            return None
            
        except ApiNinjasError as e:
            self._log.warning(f"API error getting historical timing for {ticker}: {e}")
            return None
        except Exception as e:
            self._log.warning(f"Failed to get historical timing for {ticker}: {type(e).__name__}: {e}")
            return None

    def fetch_all_upcoming_earnings(
        self,
        *,
        start_date: str,
        end_date: str,
        exchange: Optional[str] = None,
        max_results: int = 1000,
    ) -> List[dict]:
        """
        Fetch all upcoming earnings in date range with historical timing lookup.
        
        Strategy: 
        1. Use /upcomingearnings for bulk fetching upcoming dates
        2. For each ticker, look up historical timing from /earningscalendar
           (since future earnings have NULL timing, but past ones have real values)
        3. Use historical timing as predictor (companies rarely change BMO/AMC pattern)
        
        Args:
            start_date: Start date YYYY-MM-DD
            end_date: End date YYYY-MM-DD
            exchange: Optional exchange filter
            max_results: Maximum total results to fetch
            
        Returns:
            List of earnings dicts with earnings_timing field populated from history
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        all_rows: List[dict] = []
        offset = 0
        page_size = 100

        # Step 1: Fetch upcoming earnings (timing will be NULL for future dates)
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
                self._log.warning(f"Error fetching upcomingearnings page {offset}: {e}")
                break
            except Exception as e:
                self._log.warning(f"Unexpected error fetching upcomingearnings: {e}")
                break

        self._log.info(f"Fetched {len(all_rows)} upcoming earnings from {start_date} to {end_date}")
        
        if not all_rows:
            return []
        
        # Step 2: Get unique tickers that need timing lookup
        tickers_needing_timing = set()
        for row in all_rows:
            timing = row.get("earnings_timing")
            if not timing or str(timing).lower() in ("null", "none", ""):
                ticker = str(row.get("ticker") or "").upper()
                if ticker:
                    tickers_needing_timing.add(ticker)
        
        self._log.info(f"Looking up historical timing for {len(tickers_needing_timing)} tickers")
        
        # Step 3: Fetch historical timing in parallel (limit concurrent to avoid rate limits)
        timing_cache: Dict[str, str] = {}
        failed_lookups: List[str] = []
        
        if tickers_needing_timing:
            tickers_to_lookup = list(tickers_needing_timing)[:200]
            self._log.info(f"Starting historical timing lookups for {len(tickers_to_lookup)} tickers")
            
            # Use fewer workers to avoid rate limiting
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {
                    executor.submit(self.get_historical_timing, t): t 
                    for t in tickers_to_lookup
                }
                for future in as_completed(futures):
                    ticker = futures[future]
                    try:
                        timing = future.result()
                        if timing:
                            timing_cache[ticker] = timing
                        else:
                            failed_lookups.append(ticker)
                    except Exception as e:
                        self._log.warning(f"Historical timing lookup exception for {ticker}: {e}")
                        failed_lookups.append(ticker)
            
            if failed_lookups:
                self._log.info(f"Failed to find timing for {len(failed_lookups)} tickers: {failed_lookups[:10]}...")
        
        self._log.info(f"Found historical timing for {len(timing_cache)}/{len(tickers_needing_timing)} tickers")
        
        # Log sample timings
        if timing_cache:
            samples = list(timing_cache.items())[:5]
            self._log.info(f"Sample historical timings: {samples}")
        
        # Step 4: Enrich rows with historical timing
        for row in all_rows:
            current_timing = row.get("earnings_timing")
            if not current_timing or str(current_timing).lower() in ("null", "none", ""):
                ticker = str(row.get("ticker") or "").upper()
                historical = timing_cache.get(ticker)
                if historical:
                    row["earnings_timing"] = historical
                    row["timing_source"] = "historical"
        
        return all_rows[:max_results]

    def get_market_cap(self, ticker: str) -> Optional[float]:
        """
        Get market cap for a single ticker.
        
        Args:
            ticker: Stock symbol (e.g., AAPL)
            
        Returns:
            Market cap in dollars, or None if not found
        """
        try:
            resp = self.get("/marketcap", {"ticker": str(ticker).upper()})
            if resp.rows and len(resp.rows) > 0:
                row = resp.rows[0]
                mcap = row.get("market_cap")
                if mcap is not None:
                    return float(mcap)
        except Exception as e:
            self._log.debug(f"Failed to get market cap for {ticker}: {e}")
        return None

    def get_market_caps_batch(self, tickers: List[str], max_workers: int = 10) -> Dict[str, float]:
        """
        Get market caps for multiple tickers using parallel requests.
        
        Note: API Ninjas marketcap endpoint only supports single ticker,
        so we make parallel requests with ThreadPoolExecutor.
        
        Args:
            tickers: List of stock symbols
            max_workers: Number of parallel requests (default 10)
            
        Returns:
            Dict of ticker -> market cap
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        result: Dict[str, float] = {}
        
        # Dedupe and limit to prevent too many API calls
        tickers_to_fetch = list(set(str(t).upper() for t in tickers))[:500]
        
        self._log.info(f"Fetching market caps for {len(tickers_to_fetch)} tickers (parallel, {max_workers} workers)")
        
        def fetch_single(ticker: str) -> tuple:
            try:
                resp = self.get("/marketcap", {"ticker": ticker})
                if resp.rows and len(resp.rows) > 0:
                    row = resp.rows[0]
                    mcap = row.get("market_cap")
                    if mcap is not None:
                        return (ticker, float(mcap))
            except Exception as e:
                self._log.debug(f"Failed to get market cap for {ticker}: {e}")
            return (ticker, None)
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(fetch_single, t): t for t in tickers_to_fetch}
            for future in as_completed(futures):
                try:
                    ticker, mcap = future.result()
                    if mcap is not None:
                        result[ticker] = mcap
                except Exception as e:
                    self._log.debug(f"Market cap fetch error: {e}")
        
        self._log.info(f"Fetched market caps: {len(result)}/{len(tickers_to_fetch)} tickers successful")
        
        # Log some samples for debugging
        if result:
            samples = list(result.items())[:5]
            self._log.info(f"Sample market caps: {[(t, f'${m/1e9:.1f}B') for t, m in samples]}")
        
        return result

    # =========================================================================
    # EARNINGS CALL TRANSCRIPT METHODS
    # =========================================================================
    
    def search_transcripts(self, ticker: str) -> List[dict]:
        """
        Search for available earnings call transcript year/quarter combinations.
        
        Args:
            ticker: Stock symbol (e.g., AAPL)
            
        Returns:
            List of dicts with 'year' and 'quarter' keys
        """
        try:
            resp = self.get("/earningstranscriptsearch", {"ticker": str(ticker).upper()})
            return resp.rows or []
        except Exception as e:
            self._log.warning(f"Failed to search transcripts for {ticker}: {e}")
            return []
    
    def get_transcript(
        self,
        ticker: str,
        year: int,
        quarter: int,
    ) -> Optional[dict]:
        """
        Get full earnings call transcript for a specific quarter.
        
        Args:
            ticker: Stock symbol (e.g., AAPL)
            year: Earnings year (e.g., 2025)
            quarter: Earnings quarter (1-4)
            
        Returns:
            Dict with transcript data including:
            - date: Date of earnings call
            - ticker: Stock symbol
            - year: Year
            - quarter: Quarter
            - earnings_timing: before_market, during_market, after_market
            - transcript: Full transcript text
        """
        try:
            resp = self.get("/earningstranscript", {
                "ticker": str(ticker).upper(),
                "year": int(year),
                "quarter": int(quarter),
            })
            if resp.rows and len(resp.rows) > 0:
                return resp.rows[0]
            return None
        except Exception as e:
            self._log.warning(f"Failed to get transcript for {ticker} {year}Q{quarter}: {e}")
            return None
    
    def get_latest_transcripts(self, ticker: str, limit: int = 4) -> List[dict]:
        """
        Get the most recent earnings call transcripts for a ticker.
        
        Args:
            ticker: Stock symbol
            limit: Number of recent transcripts to fetch (default 4)
            
        Returns:
            List of transcript metadata (year, quarter) sorted newest first
        """
        available = self.search_transcripts(ticker)
        if not available:
            return []
        
        # Sort by year desc, quarter desc to get most recent first
        def sort_key(item):
            try:
                return (int(item.get("year", 0)), int(item.get("quarter", 0)))
            except:
                return (0, 0)
        
        sorted_transcripts = sorted(available, key=sort_key, reverse=True)
        return sorted_transcripts[:limit]

    # =========================================================================
    # INSIDER TRADING METHODS (Engine 9: Credit Stress Drift)
    # =========================================================================

    def get_insider_transactions(
        self,
        ticker: str,
        *,
        transaction_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        """
        Get SEC Form 4 insider transactions for a ticker.

        Args:
            ticker: Stock symbol (e.g., ARCC)
            transaction_type: Optional filter -- 'buy', 'sell', or None for all
            limit: Max results per page (1-100)
            offset: Pagination offset

        Returns:
            List of dicts with keys: filing_date, trade_date, ticker, owner_name,
            transaction_type, shares_traded, price, shares_owned, company_name, etc.
        """
        params: Dict[str, Any] = {
            "ticker": str(ticker).upper(),
            "limit": min(100, max(1, int(limit))),
        }
        if offset > 0:
            params["offset"] = int(offset)
        if transaction_type:
            params["transaction_type"] = str(transaction_type).lower()
        try:
            resp = self.get("/insidertransactions", params)
            return resp.rows or []
        except Exception as e:
            self._log.warning("Failed to get insider transactions for %s: %s", ticker, e)
            return []

    def get_insider_net_selling(
        self,
        ticker: str,
        days: int = 90,
    ) -> Dict[str, Any]:
        """
        Compute net insider selling ($ value) over a rolling window.

        Returns dict with buy_total, sell_total, net_selling, transaction_count,
        and the raw transactions list.
        """
        from datetime import datetime, timedelta as td

        cutoff = (datetime.utcnow() - td(days=days)).strftime("%Y-%m-%d")
        rows = self.get_insider_transactions(ticker, limit=100)

        buy_total = 0.0
        sell_total = 0.0
        count = 0
        filtered: List[dict] = []

        for row in rows:
            trade_date = row.get("transaction_date") or row.get("trade_date") or row.get("filing_date") or ""
            if trade_date < cutoff:
                continue
            filtered.append(row)
            count += 1

            shares = abs(float(row.get("shares_traded") or row.get("amount") or 0))
            price = float(row.get("price") or row.get("share_price") or 0)
            dollar = shares * price

            tx_type = str(row.get("transaction_type") or "").lower()
            if "buy" in tx_type or "purchase" in tx_type:
                buy_total += dollar
            elif "sell" in tx_type or "sale" in tx_type or "disposition" in tx_type:
                sell_total += dollar

        return {
            "ticker": ticker,
            "days": days,
            "buy_total": round(buy_total, 2),
            "sell_total": round(sell_total, 2),
            "net_selling": round(sell_total - buy_total, 2),
            "transaction_count": count,
            "transactions": filtered,
        }

    # =========================================================================
    # ETF INFO METHODS (Engine 9: Credit Stress Drift)
    # =========================================================================

    def get_etf_info(self, ticker: str) -> Optional[dict]:
        """
        Get ETF information including AUM, expense ratio, holdings.

        Args:
            ticker: ETF symbol (e.g., HYG, JNK, BKLN)

        Returns:
            Dict with etf_name, aum, expense_ratio, holdings_count, etc.
        """
        try:
            resp = self.get("/etf", {"name": str(ticker).upper()})
            if resp.rows:
                return resp.rows[0]
            return None
        except Exception as e:
            self._log.warning("Failed to get ETF info for %s: %s", ticker, e)
            return None

    def get_transcript_history(
        self,
        ticker: str,
        quarters: int = 4,
    ) -> List[dict]:
        """
        Fetch full transcript text for the most recent N quarters.

        Used by Engine 9 NLP delta-of-language signal to compare tone
        across quarters.

        Args:
            ticker: Stock symbol
            quarters: Number of recent quarters to fetch (default 4)

        Returns:
            List of full transcript dicts (with 'transcript' text), newest first.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        meta = self.get_latest_transcripts(ticker, limit=quarters)
        if not meta:
            return []

        results: List[dict] = []

        def _fetch(item: dict) -> Optional[dict]:
            y = int(item.get("year", 0))
            q = int(item.get("quarter", 0))
            if y and q:
                return self.get_transcript(ticker, y, q)
            return None

        with ThreadPoolExecutor(max_workers=min(4, len(meta))) as pool:
            futs = {pool.submit(_fetch, m): m for m in meta}
            for fut in as_completed(futs):
                try:
                    result = fut.result()
                    if result:
                        results.append(result)
                except Exception as exc:
                    self._log.debug("Transcript fetch error: %s", exc)

        def _sort_key(t: dict) -> tuple:
            return (int(t.get("year", 0)), int(t.get("quarter", 0)))

        results.sort(key=_sort_key, reverse=True)
        return results
