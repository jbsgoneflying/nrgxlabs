#!/usr/bin/env python3
"""Engine 5 – Nightly Global EOD Refresh Script.

Fetches global EOD data from EODHD, normalizes bars, computes lead-lag signals,
classifies regime, translates to US biases, and writes snapshots to Redis.

Two-tier Redis storage:
- engine5:latest:*   -- TTL 48h, overwritten each run (fast reads for API)
- engine5:history:*  -- TTL 180d, append-only per symbol+date (durable lookback)

Usage:
    python scripts/refresh_engine5_snapshot.py [--force]
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# Ensure repo root is on sys.path for cron-friendly execution.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.config import get_flags
from backend.eodhd_client import EodhdClient, EodhdError
from backend.engine5_global_intake import (
    GlobalAssetBar,
    YieldSnapshot,
    all_eod_symbols,
    build_yield_snapshot,
    load_universe,
    normalize_bars,
)
from backend.engine5_lead_lag import compute_lead_lag_signals
from backend.engine5_regime import compute_regime_from_bars
from backend.engine5_translation import translate_signals_to_us
from backend.redis_store import get_store_optional

LOG = logging.getLogger("engine5_refresh")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _previous_trading_day(ref: dt.date) -> dt.date:
    """Return the most recent weekday before `ref`."""
    d = ref - dt.timedelta(days=1)
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d -= dt.timedelta(days=1)
    return d


def _load_history_from_redis(
    store: Any,
    symbols: List[str],
    ttl_history: int,
) -> Dict[str, List[dict]]:
    """Load durable bar history from Redis for all symbols."""
    history: Dict[str, List[dict]] = {}
    for sym in symbols:
        key = f"engine5:history:{sym}"
        data = store.get_json(key)
        if isinstance(data, list):
            history[sym] = data
        elif isinstance(data, dict):
            # Convert dict-by-date format to list
            history[sym] = list(data.values()) if data else []
        else:
            history[sym] = []
    return history


def _append_bar_to_history(
    store: Any,
    symbol: str,
    bar: dict,
    ttl_history: int,
    max_days: int = 200,
) -> None:
    """Append a single bar to the durable history in Redis."""
    key = f"engine5:history:{symbol}"
    existing = store.get_json(key)
    if not isinstance(existing, list):
        existing = []

    # Deduplicate by date
    bar_date = bar.get("date", "")
    existing = [b for b in existing if b.get("date") != bar_date]
    existing.append(bar)

    # Sort and prune to max_days
    existing.sort(key=lambda b: str(b.get("date", "")))
    if len(existing) > max_days:
        existing = existing[-max_days:]

    store.set_json(key, existing, ttl_s=ttl_history)


# ---------------------------------------------------------------------------
# Data freshness guard
# ---------------------------------------------------------------------------


def _check_freshness(
    client: EodhdClient,
    sentinel_symbol: str,
    expected_date: dt.date,
    retries: int = 3,
    interval_s: int = 900,
) -> Tuple[bool, List[dict]]:
    """Check that the sentinel ticker has data for the expected trading date.

    Returns (is_fresh, bars).
    """
    for attempt in range(1, retries + 1):
        try:
            resp = client.get_eod(
                sentinel_symbol,
                from_date=(expected_date - dt.timedelta(days=5)).isoformat(),
                to_date=expected_date.isoformat(),
            )
            bars = resp.rows
            if bars:
                latest_date = max(str(b.get("date", ""))[:10] for b in bars)
                if latest_date >= expected_date.isoformat():
                    LOG.info("Freshness OK: %s has data for %s", sentinel_symbol, expected_date)
                    return True, bars
                LOG.warning(
                    "Freshness check %d/%d: latest=%s, expected=%s",
                    attempt, retries, latest_date, expected_date,
                )
            else:
                LOG.warning("Freshness check %d/%d: no bars returned for %s", attempt, retries, sentinel_symbol)
        except EodhdError as e:
            LOG.warning("Freshness check %d/%d error: %s", attempt, retries, e)

        if attempt < retries:
            LOG.info("Retrying in %d seconds...", interval_s)
            time.sleep(interval_s)

    LOG.error("Data freshness guard FAILED after %d retries", retries)
    return False, []


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_pipeline(force: bool = False) -> int:
    """Execute the full nightly Engine 5 pipeline.

    Returns 0 on success, 1 on failure.
    """
    flags = get_flags()

    if not flags.ENABLE_ENGINE5_LEAD_LAG:
        LOG.info("Engine 5 is disabled (ENABLE_ENGINE5_LEAD_LAG=0). Exiting.")
        return 0

    store = get_store_optional()
    if store is None:
        LOG.error("Missing REDIS_URL; cannot run Engine 5 pipeline.")
        return 2
    if not store.ping():
        LOG.error("Redis ping failed; cannot run Engine 5 pipeline.")
        return 3

    # Gate check: skip if last run < 20h ago (unless --force)
    if not force:
        last_refresh = store.get_json("engine5:last_refresh")
        if last_refresh:
            try:
                last_ts = float(last_refresh.get("timestamp", 0))
                if time.time() - last_ts < 20 * 3600:
                    LOG.info("Skipping: last refresh was %.1fh ago", (time.time() - last_ts) / 3600)
                    return 0
            except Exception:
                pass

    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    expected_date = _previous_trading_day(today)

    LOG.info("Starting Engine 5 pipeline for expected_date=%s", expected_date)

    # Load universe
    try:
        universe = load_universe()
    except Exception as e:
        LOG.error("Failed to load universe: %s", e)
        return 1

    sentinel = universe.get("sentinel_ticker", "STOXX50E.INDX")

    # Initialize EODHD client
    try:
        eodhd = EodhdClient.from_env()
    except EodhdError as e:
        LOG.error("EODHD client init failed: %s", e)
        return 1

    # Step 2: Fetch global EOD bars
    LOG.info("Fetching global EOD bars...")
    eod_symbols = all_eod_symbols(universe)
    raw_bars: Dict[str, List[dict]] = {}
    fetch_from = (expected_date - dt.timedelta(days=10)).isoformat()
    fetch_to = expected_date.isoformat()

    def _fetch_one(entry: dict) -> Tuple[str, List[dict]]:
        sym = entry["symbol"]
        try:
            resp = eodhd.get_eod(sym, from_date=fetch_from, to_date=fetch_to)
            return sym, resp.rows
        except EodhdError as e:
            # Try fallback if available
            fallback = entry.get("fallback")
            if fallback:
                try:
                    resp = eodhd.get_eod(fallback, from_date=fetch_from, to_date=fetch_to)
                    LOG.info("Used fallback %s for %s", fallback, sym)
                    return sym, resp.rows
                except EodhdError:
                    pass
            LOG.warning("Failed to fetch %s: %s", sym, e)
            return sym, []

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_fetch_one, entry): entry for entry in eod_symbols}
        for fut in as_completed(futures):
            sym, bars = fut.result()
            if bars:
                raw_bars[sym] = bars

    LOG.info("Fetched %d/%d symbols", len(raw_bars), len(eod_symbols))

    # Step 3: Data freshness guard
    is_fresh, _ = _check_freshness(
        eodhd,
        sentinel,
        expected_date,
        retries=flags.ENGINE5_FRESHNESS_RETRY_COUNT,
        interval_s=flags.ENGINE5_FRESHNESS_RETRY_INTERVAL_S,
    )
    if not is_fresh:
        store.set_json("engine5:latest:status", {
            "status": "STALE",
            "expected_date": expected_date.isoformat(),
            "timestamp": time.time(),
        }, ttl_s=flags.ENGINE5_CACHE_TTL_LATEST)
        LOG.error("STALE data; skipping signal computation.")
        return 1

    # Step 4: Fetch US yield curve
    LOG.info("Fetching US yield curve...")
    ust_rows: List[dict] = []
    try:
        ust_resp = eodhd.get_ust_yield_rates()
        ust_rows = ust_resp.rows
    except EodhdError as e:
        LOG.warning("UST yield fetch failed: %s", e)

    # Fetch non-US sovereign yields (already in raw_bars if GBOND tickers work)
    de_10y_bars = raw_bars.get("DE10Y.GBOND", [])
    jp_10y_bars = raw_bars.get("JP10Y.GBOND", [])

    # Real yields
    real_yield_rows: List[dict] = []
    try:
        real_resp = eodhd.get_ust_real_yield_rates()
        real_yield_rows = real_resp.rows
    except EodhdError as e:
        LOG.warning("UST real yield fetch failed: %s", e)

    yield_snapshot = build_yield_snapshot(ust_rows, de_10y_bars, jp_10y_bars, real_yield_rows)

    # Step 5: Extract FX rates for USD conversion
    fx_rates: Dict[str, float] = {}
    for fx_entry in universe.get("fx", []):
        sym = fx_entry["symbol"]
        bars = raw_bars.get(sym, [])
        if bars:
            latest = sorted(bars, key=lambda b: str(b.get("date", "")))[-1]
            close = latest.get("adjusted_close") or latest.get("close")
            if close:
                try:
                    fx_rates[sym] = float(close)
                except (TypeError, ValueError):
                    pass
    LOG.info("FX rates: %s", fx_rates)

    # Step 6: Normalize bars
    LOG.info("Normalizing bars...")
    all_symbols = [e["symbol"] for e in eod_symbols]
    history = _load_history_from_redis(store, all_symbols, flags.ENGINE5_CACHE_TTL_HISTORY)

    normalized = normalize_bars(raw_bars, fx_rates, history, universe)
    LOG.info("Normalized %d bars", len(normalized))

    # Step 7: Write bars to Redis (both tiers)
    LOG.info("Writing bars to Redis...")
    latest_bars_json = [b.to_dict() for b in normalized]
    store.set_json("engine5:latest:bars", latest_bars_json, ttl_s=flags.ENGINE5_CACHE_TTL_LATEST)

    for bar in normalized:
        _append_bar_to_history(
            store, bar.symbol, bar.to_dict(),
            ttl_history=flags.ENGINE5_CACHE_TTL_HISTORY,
        )

    if yield_snapshot:
        store.set_json("engine5:latest:yields", yield_snapshot.to_dict(), ttl_s=flags.ENGINE5_CACHE_TTL_LATEST)
        # Also append yield history
        yield_hist_key = "engine5:history:yields"
        yield_hist = store.get_json(yield_hist_key)
        if not isinstance(yield_hist, list):
            yield_hist = []
        yield_hist = [y for y in yield_hist if y.get("date") != yield_snapshot.date]
        yield_hist.append(yield_snapshot.to_dict())
        yield_hist.sort(key=lambda y: str(y.get("date", "")))
        if len(yield_hist) > 200:
            yield_hist = yield_hist[-200:]
        store.set_json(yield_hist_key, yield_hist, ttl_s=flags.ENGINE5_CACHE_TTL_HISTORY)

    # Reload history with today's bars included
    history = _load_history_from_redis(store, all_symbols, flags.ENGINE5_CACHE_TTL_HISTORY)

    # Step 8: Compute lead-lag signals
    LOG.info("Computing lead-lag signals...")
    leader_returns: Dict[str, List[Tuple[str, float]]] = {}
    follower_returns: Dict[str, List[Tuple[str, float]]] = {}
    mapping: Dict[str, List[str]] = {}

    for entry in universe.get("equity_indices", []):
        sym = entry["symbol"]
        targets = entry.get("us_targets", [])
        mapping[sym] = targets
        hist = history.get(sym, [])
        returns = []
        for b in sorted(hist, key=lambda b: str(b.get("date", ""))):
            r = b.get("return_1d_local")
            d = b.get("date", "")
            if r is not None and d:
                try:
                    returns.append((d, float(r)))
                except (TypeError, ValueError):
                    pass
        if returns:
            leader_returns[sym] = returns

    # SPY as follower reference
    spy_hist = history.get("GSPC.INDX", [])
    spy_returns = []
    for b in sorted(spy_hist, key=lambda b: str(b.get("date", ""))):
        r = b.get("return_1d_local")
        d = b.get("date", "")
        if r is not None and d:
            try:
                spy_returns.append((d, float(r)))
            except (TypeError, ValueError):
                pass
    if spy_returns:
        follower_returns["SPY"] = spy_returns

    signals = compute_lead_lag_signals(
        leader_returns=leader_returns,
        follower_returns=follower_returns,
        mapping=mapping,
        corr_window=flags.ENGINE5_CORR_WINDOW,
        max_lag_days=flags.ENGINE5_MAX_LAG_DAYS,
        corr_threshold=flags.ENGINE5_CORR_THRESHOLD,
        z_significant=flags.ENGINE5_Z_SIGNIFICANT,
        lookback_days=flags.ENGINE5_LOOKBACK_DAYS,
        date=expected_date.isoformat(),
    )
    LOG.info("Computed %d lead-lag signals", len(signals))
    signals_json = [s.to_dict() for s in signals]
    store.set_json("engine5:latest:signals", signals_json, ttl_s=flags.ENGINE5_CACHE_TTL_LATEST)

    # Step 9: Classify regime
    LOG.info("Classifying regime...")
    yield_hist = store.get_json("engine5:history:yields")
    if not isinstance(yield_hist, list):
        yield_hist = []

    regime = compute_regime_from_bars(
        date=expected_date.isoformat(),
        bars_history=history,
        yield_snapshots=yield_hist,
        spy_iv_rank=None,  # Will be filled by ORATS at API request time
        stressed_threshold=flags.ENGINE5_REGIME_STRESSED_THRESHOLD,
        risk_off_threshold=flags.ENGINE5_REGIME_RISK_OFF_THRESHOLD,
        transitional_threshold=flags.ENGINE5_REGIME_TRANSITIONAL_THRESHOLD,
    )
    LOG.info("Regime: %s (score=%.1f)", regime.label, regime.score)
    store.set_json("engine5:latest:regime", regime.to_dict(), ttl_s=flags.ENGINE5_CACHE_TTL_LATEST)

    # Step 10: Translate to US bias
    LOG.info("Translating to US biases...")
    fx_bar_history: Dict[str, List[dict]] = {}
    for fx_entry in universe.get("fx", []):
        sym = fx_entry["symbol"]
        fx_bar_history[sym] = history.get(sym, [])

    sector_biases, index_biases = translate_signals_to_us(
        signals=signals_json,
        regime=regime.to_dict(),
        yield_snapshot=yield_snapshot.to_dict() if yield_snapshot else None,
        fx_bars=fx_bar_history,
    )
    LOG.info("Generated %d sector biases, %d index biases", len(sector_biases), len(index_biases))
    store.set_json("engine5:latest:us_bias", {
        "sectorBiases": [b.to_dict() for b in sector_biases],
        "indexBiases": [b.to_dict() for b in index_biases],
    }, ttl_s=flags.ENGINE5_CACHE_TTL_LATEST)

    # Step 11: Log completion
    store.set_json("engine5:last_refresh", {
        "timestamp": time.time(),
        "date": expected_date.isoformat(),
        "status": "OK",
        "signals_count": len(signals),
        "regime_label": regime.label,
        "regime_score": regime.score,
    }, ttl_s=flags.ENGINE5_CACHE_TTL_LATEST)

    store.set_json("engine5:latest:status", {
        "status": "OK",
        "expected_date": expected_date.isoformat(),
        "timestamp": time.time(),
    }, ttl_s=flags.ENGINE5_CACHE_TTL_LATEST)

    LOG.info("Engine 5 pipeline complete. date=%s regime=%s signals=%d",
             expected_date, regime.label, len(signals))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    force = "--force" in sys.argv
    return run_pipeline(force=force)


if __name__ == "__main__":
    raise SystemExit(main())
