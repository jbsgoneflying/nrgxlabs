"""Unified OHLCV Price Service — backed by EODHD.

Provides a single interface for all engines to fetch historical daily bars
and latest prices from EODHD, replacing the previous ORATS ``hist_dailies``
and ``live_summaries`` usage for pure price data.

ORATS remains the sole source for options-specific data (IV, greeks, strikes,
skew, earnings calendar).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from cachetools import TTLCache

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        if not math.isfinite(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _fmt_date(d: dt.date) -> str:
    return d.isoformat()


# ---------------------------------------------------------------------------
# Symbol mapping  (ORATS bare ticker → EODHD qualified symbol)
# ---------------------------------------------------------------------------

# Index tickers that need special EODHD symbols.  Everything else gets ".US".
_INDEX_MAP: Dict[str, str] = {
    "SPX": "GSPC.INDX",
    "$SPX": "GSPC.INDX",
    "GSPC": "GSPC.INDX",
    "NDX": "NDX.INDX",
    "$NDX": "NDX.INDX",
    "DJI": "DJI.INDX",
    "$DJI": "DJI.INDX",
    "RUT": "RUT.INDX",
    "$RUT": "RUT.INDX",
    "VIX": "VIX.INDX",
    "$VIX": "VIX.INDX",
}


def _to_eodhd_symbol(ticker: str) -> str:
    """Convert an ORATS-style bare ticker to EODHD qualified symbol."""
    t = ticker.strip().upper()
    # Already qualified (e.g. "AAPL.US", "GSPC.INDX")
    if "." in t:
        return t
    mapped = _INDEX_MAP.get(t)
    if mapped:
        return mapped
    return f"{t}.US"


def _bare_ticker(eodhd_symbol: str) -> str:
    """Strip the exchange suffix to recover the bare ticker."""
    return eodhd_symbol.rsplit(".", 1)[0] if "." in eodhd_symbol else eodhd_symbol


# ---------------------------------------------------------------------------
# Canonical DailyBar  (defined here to avoid circular imports with technicals.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DailyBar:
    trade_date: str
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[float]
    vwap: Optional[float]


def _rows_to_bars(rows: list[dict]) -> List[DailyBar]:
    """Normalise EODHD EOD rows into fully split-adjusted DailyBar objects.

    EODHD only provides ``adjusted_close``; raw open/high/low are unadjusted.
    We compute the adjustment ratio (adjusted_close / close) and apply it to
    all OHLC fields so the bar is internally consistent.  This is critical for
    any calculation that compares open-to-close on the same day or across days
    (e.g. Engine 1 earnings breach, Red Dog reversals).
    """
    out: List[DailyBar] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        d0 = str(r.get("date") or "")[:10]
        if not d0:
            continue

        raw_close = _to_float(r.get("close"))
        adj_close = _to_float(r.get("adjusted_close"))

        # Determine usable close and adjustment factor
        if adj_close is not None and adj_close > 0:
            c = adj_close
            # Compute ratio to adjust open/high/low consistently
            if raw_close is not None and raw_close > 0:
                adj_factor = adj_close / raw_close
            else:
                adj_factor = 1.0
        elif raw_close is not None and raw_close > 0:
            c = raw_close
            adj_factor = 1.0
        else:
            continue  # skip bars with no usable close

        raw_o = _to_float(r.get("open"))
        raw_h = _to_float(r.get("high"))
        raw_lo = _to_float(r.get("low"))
        vol = _to_float(r.get("volume"))

        # Apply adjustment factor to open/high/low so all OHLC are consistent
        o = round(raw_o * adj_factor, 4) if raw_o is not None else None
        h = round(raw_h * adj_factor, 4) if raw_h is not None else None
        lo = round(raw_lo * adj_factor, 4) if raw_lo is not None else None

        out.append(DailyBar(trade_date=d0, open=o, high=h, low=lo, close=c, volume=vol, vwap=None))
    out.sort(key=lambda b: b.trade_date)
    return out


# ---------------------------------------------------------------------------
# Module-level singleton (lazy)
# ---------------------------------------------------------------------------

_instance: Optional["PriceService"] = None
_instance_lock = threading.Lock()


def get_price_service() -> Optional["PriceService"]:
    """Return the module-level PriceService singleton, or None if EODHD is
    not configured."""
    global _instance
    if _instance is not None:
        return _instance
    with _instance_lock:
        if _instance is not None:
            return _instance
        token = os.getenv("EODHD_API_TOKEN")
        if not token:
            LOG.debug("PriceService unavailable — EODHD_API_TOKEN not set")
            return None
        try:
            from backend.eodhd_client import EodhdClient
            client = EodhdClient(token=token)
            _instance = PriceService(client)
            LOG.info("PriceService initialised (EODHD)")
            return _instance
        except Exception as exc:
            LOG.warning("PriceService init failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# PriceService
# ---------------------------------------------------------------------------


class PriceService:
    """Thin wrapper around EodhdClient focused on OHLCV bar data.

    All public methods accept bare ORATS-style tickers (``AAPL``, ``SPY``)
    and handle the EODHD symbol translation internally.
    """

    def __init__(self, eodhd_client: Any) -> None:
        self._eodhd = eodhd_client
        # Per-ticker bar cache (6 h TTL, mirrors old ORATS cache behaviour)
        self._bar_cache: TTLCache = TTLCache(maxsize=10_000, ttl=6 * 60 * 60)
        self._bar_lock = threading.Lock()
        # Bulk-day cache (keyed by date string)
        self._bulk_cache: TTLCache = TTLCache(maxsize=500, ttl=6 * 60 * 60)
        self._bulk_lock = threading.Lock()

    # -- cache helpers -------------------------------------------------------

    def _cache_get(self, cache: TTLCache, lock: threading.Lock, key: Any) -> Any:
        with lock:
            return cache.get(key)

    def _cache_set(self, cache: TTLCache, lock: threading.Lock, key: Any, value: Any) -> None:
        with lock:
            cache[key] = value

    # -----------------------------------------------------------------------
    # fetch_daily_bars  —  replaces ORATS hist_dailies for OHLCV
    # -----------------------------------------------------------------------

    def fetch_daily_bars(
        self,
        ticker: str,
        start: dt.date,
        end: dt.date,
    ) -> List[DailyBar]:
        """Fetch daily OHLCV bars for *ticker* over [start, end].

        Returns a list of ``DailyBar`` sorted by date ascending.
        """
        if end < start:
            return []

        key = ("bars", ticker.upper(), _fmt_date(start), _fmt_date(end))
        cached = self._cache_get(self._bar_cache, self._bar_lock, key)
        if cached is not None:
            return cached

        sym = _to_eodhd_symbol(ticker)
        failed = False
        try:
            resp = self._eodhd.get_eod(
                sym,
                from_date=_fmt_date(start),
                to_date=_fmt_date(end),
            )
            bars = _rows_to_bars(resp.rows or [])
        except Exception as exc:
            LOG.warning("PriceService.fetch_daily_bars(%s) failed: %s", ticker, exc)
            bars = []
            failed = True

        if not failed:
            self._cache_set(self._bar_cache, self._bar_lock, key, bars)
        return bars

    # -----------------------------------------------------------------------
    # fetch_live_price  —  replaces ORATS live_summaries for spot price
    # -----------------------------------------------------------------------

    def fetch_live_price(self, ticker: str) -> Optional[float]:
        """Return the latest available close price for *ticker*.

        EODHD is EOD-only; this returns the most recent adjusted close.
        Suitable for NRGX Labs's EOD-centric architecture.
        """
        today = dt.date.today()
        # Look back up to 7 days to handle weekends / holidays
        start = today - dt.timedelta(days=7)
        bars = self.fetch_daily_bars(ticker, start, today)
        if bars:
            return bars[-1].close
        return None

    def fetch_intraday_price(self, ticker: str) -> Optional[float]:
        """Return an intraday snapshot price from EODHD live endpoints."""
        sym = _to_eodhd_symbol(ticker)
        try:
            resp = self._eodhd.get_live_quote(sym)
            rows = resp.rows or []
            row = rows[0] if rows and isinstance(rows[0], dict) else None
            if isinstance(row, dict):
                for key in ("close", "lastTradePrice", "last", "price"):
                    px = _to_float(row.get(key))
                    if px is not None and px > 0:
                        return float(px)
        except Exception:
            return None
        return None

    # -----------------------------------------------------------------------
    # fetch_daily_bars_batch  —  efficient multi-ticker fetch
    # -----------------------------------------------------------------------

    def fetch_daily_bars_batch(
        self,
        tickers: List[str],
        start: dt.date,
        end: dt.date,
        *,
        max_workers: int = 8,
    ) -> Dict[str, List[DailyBar]]:
        """Fetch daily bars for many tickers in parallel.

        For single-day requests the bulk endpoint is used (1 API credit for
        all US equities).  For multi-day ranges, individual ``get_eod`` calls
        run in a thread pool.
        """
        if end < start or not tickers:
            return {}

        # Single-day optimisation: use bulk endpoint
        if start == end:
            return self._fetch_bulk_single_day(tickers, start)

        result: Dict[str, List[DailyBar]] = {}

        def _fetch_one(t: str) -> Tuple[str, List[DailyBar]]:
            return t, self.fetch_daily_bars(t, start, end)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_one, t): t for t in tickers}
            for fut in as_completed(futures):
                try:
                    sym, bars = fut.result()
                    result[sym] = bars
                except Exception as exc:
                    t = futures[fut]
                    LOG.warning("Batch fetch failed for %s: %s", t, exc)
                    result[t] = []

        return result

    # -- internal: bulk single-day fetch ------------------------------------

    def _fetch_bulk_single_day(
        self,
        tickers: List[str],
        date: dt.date,
    ) -> Dict[str, List[DailyBar]]:
        """Use ``get_eod_bulk("US")`` for a single date — very efficient."""
        ds = _fmt_date(date)
        cache_key = ("bulk", ds)
        cached_bulk = self._cache_get(self._bulk_cache, self._bulk_lock, cache_key)

        if cached_bulk is None:
            try:
                resp = self._eodhd.get_eod_bulk("US", date=ds)
                bulk_rows = resp.rows or []
            except Exception as exc:
                LOG.warning("PriceService bulk fetch for %s failed: %s", ds, exc)
                bulk_rows = []

            # Build lookup keyed by bare ticker
            cached_bulk: Dict[str, dict] = {}
            for r in bulk_rows:
                code = str(r.get("code") or r.get("ticker") or r.get("symbol") or "").upper()
                if code:
                    cached_bulk[code] = r
            self._cache_set(self._bulk_cache, self._bulk_lock, cache_key, cached_bulk)

        result: Dict[str, List[DailyBar]] = {}
        want = {t.strip().upper() for t in tickers}
        for t in want:
            row = cached_bulk.get(t)
            if not row:
                result[t] = []
                continue

            raw_close = _to_float(row.get("close"))
            adj_close = _to_float(row.get("adjusted_close"))

            if adj_close is not None and adj_close > 0:
                c = adj_close
                adj_factor = (adj_close / raw_close) if (raw_close and raw_close > 0) else 1.0
            elif raw_close is not None and raw_close > 0:
                c = raw_close
                adj_factor = 1.0
            else:
                result[t] = []
                continue

            raw_o = _to_float(row.get("open"))
            raw_h = _to_float(row.get("high"))
            raw_lo = _to_float(row.get("low"))

            bar = DailyBar(
                trade_date=str(row.get("date") or ds)[:10],
                open=round(raw_o * adj_factor, 4) if raw_o is not None else None,
                high=round(raw_h * adj_factor, 4) if raw_h is not None else None,
                low=round(raw_lo * adj_factor, 4) if raw_lo is not None else None,
                close=c,
                volume=_to_float(row.get("volume")),
                vwap=None,
            )
            result[t] = [bar]

        return result
