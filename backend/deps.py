"""Shared singletons, caches, and helper functions used by multiple routers."""
from __future__ import annotations

import logging
import os
import threading

from cachetools import TTLCache

from backend.config import get_flags
from backend.orats_client import OratsClient
from backend.benzinga_client import BenzingaClient
from backend.fmp_client import FmpClient
from backend.api_ninjas_client import ApiNinjasClient

LOG = logging.getLogger("app")

# ---------------------------------------------------------------------------
# Client singletons (thread-safe lazy init)
# ---------------------------------------------------------------------------

_client_lock = threading.Lock()
_client: OratsClient | None = None

_bz_client_lock = threading.Lock()
_bz_client: BenzingaClient | None = None

_fmp_client_lock = threading.Lock()
_fmp_client: FmpClient | None = None

_api_ninjas_client_lock = threading.Lock()
_api_ninjas_client: ApiNinjasClient | None = None

_fred_client_lock = threading.Lock()
_fred_client = None


def get_client() -> OratsClient:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = OratsClient.from_env()
    return _client


def get_client_optional() -> OratsClient | None:
    try:
        return get_client()
    except Exception:
        return None


def get_benzinga_client_optional() -> BenzingaClient | None:
    if not get_flags().ENABLE_BENZINGA:
        return None
    global _bz_client
    if _bz_client is not None:
        return _bz_client
    with _bz_client_lock:
        if _bz_client is None:
            _bz_client = BenzingaClient.from_env_optional()
    return _bz_client


def get_fmp_client_optional() -> FmpClient | None:
    global _fmp_client
    try:
        if _fmp_client is not None:
            return _fmp_client
        with _fmp_client_lock:
            if _fmp_client is None:
                if not (os.getenv("FMP_API_KEY") or "").strip():
                    return None
                _fmp_client = FmpClient.from_env()
        return _fmp_client
    except Exception:
        return None


def get_api_ninjas_client_optional() -> ApiNinjasClient | None:
    global _api_ninjas_client
    try:
        if _api_ninjas_client is not None:
            return _api_ninjas_client
        with _api_ninjas_client_lock:
            if _api_ninjas_client is None:
                if not (os.getenv("API_NINJAS_API_KEY") or "").strip():
                    return None
                _api_ninjas_client = ApiNinjasClient.from_env()
        return _api_ninjas_client
    except Exception:
        return None


def get_fred_client_optional():
    global _fred_client
    try:
        if _fred_client is not None:
            return _fred_client
        with _fred_client_lock:
            if _fred_client is None:
                from backend.fred_client import FredClient
                _fred_client = FredClient.from_env()
        return _fred_client
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Caches (TTLCache instances, each with its own lock)
# ---------------------------------------------------------------------------

breach_cache = TTLCache(maxsize=512, ttl=6 * 60 * 60)
breach_cache_lock = threading.Lock()

spx_ic_cache = TTLCache(maxsize=128, ttl=30 * 60)
spx_ic_cache_lock = threading.Lock()

spx_levels_cache = TTLCache(maxsize=128, ttl=60)
spx_levels_cache_lock = threading.Lock()

levels_cache = TTLCache(maxsize=256, ttl=60)
levels_cache_lock = threading.Lock()

calendar_cache = TTLCache(maxsize=128, ttl=10 * 60)
calendar_cache_lock = threading.Lock()

engine1_elig_cache = TTLCache(maxsize=50_000, ttl=24 * 60 * 60)
engine1_elig_cache_lock = threading.Lock()

condor_rank_cache = TTLCache(maxsize=1024, ttl=6 * 60 * 60)
condor_rank_cache_lock = threading.Lock()

macro_stats_cache = TTLCache(maxsize=256, ttl=6 * 60 * 60)
macro_stats_cache_lock = threading.Lock()

engine3_cache = TTLCache(maxsize=20, ttl=30 * 60)
engine3_cache_lock = threading.Lock()

try:
    _engine4_scan_ttl = int(get_flags().ENGINE4_CACHE_TTL_SCAN)
except Exception:
    _engine4_scan_ttl = 5 * 60
engine4_cache = TTLCache(maxsize=20, ttl=_engine4_scan_ttl)
engine4_cache_lock = threading.Lock()

engine7_cache = TTLCache(maxsize=20, ttl=30 * 60)
engine7_cache_lock = threading.Lock()

engine9_cache = TTLCache(maxsize=64, ttl=5 * 60)
engine9_cache_lock = threading.Lock()

dms_cache = TTLCache(maxsize=4, ttl=5 * 60)
morning_brief_cache = TTLCache(maxsize=2, ttl=60 * 60)
weekly_roadmap_cache = TTLCache(maxsize=2, ttl=60 * 60)
front_layer_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cache-key helpers
# ---------------------------------------------------------------------------

def breach_cache_key(
    ticker: str,
    n: int,
    years: int,
    k: float,
    flags_fp: tuple | None = None,
    *,
    event_date: str | None = None,
    event_timing: str | None = None,
) -> tuple:
    """Cache key for /api/breach — now includes the (optional) manual earnings
    date + timing so stale nextEvent no longer survives cache hits.
    ``event_date``/``event_timing`` default to empty strings when omitted to
    keep legacy callers compatible.
    """
    fp = flags_fp if flags_fp is not None else get_flags().cache_fingerprint()
    ed = (event_date or "").strip()[:10]
    et = (event_timing or "").strip().upper()
    return (ticker.strip().upper(), int(n), int(years), float(k), fp, ed, et)


def spx_ic_cache_key(params: dict, flags_fp: tuple) -> tuple:
    items = tuple(sorted((k, str(v)) for k, v in (params or {}).items()))
    return ("spx_ic", items, flags_fp)


def spx_levels_cache_key(params: dict, flags_fp: tuple) -> tuple:
    items = tuple(sorted((k, str(v)) for k, v in (params or {}).items()))
    return ("spx_levels", items, flags_fp)


def levels_cache_key(ticker: str, params: dict, flags_fp: tuple) -> tuple:
    items = tuple(sorted((k, str(v)) for k, v in (params or {}).items()))
    return ("levels", str(ticker or "").strip().upper(), items, flags_fp)
