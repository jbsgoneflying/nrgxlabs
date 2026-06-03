"""
Engine 4: Ichimoku Cloud Continuation Universe Scanner

Scans the SP500 + Nasdaq100 universe for Ichimoku continuation setups
with caching, parallel processing, and segmented gamma context.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from cachetools import TTLCache

from backend.config import get_flags
from backend.dealer_gamma_context import compute_dealer_gamma_context
from backend.engine4_ichimoku import (
    APLUS_THRESHOLD,
    IchimokuSignal,
    build_ichimoku_signal,
    detect_ichimoku_setup,
    signal_to_dict,
)
from backend.orats_client import OratsClient
from backend.technicals import (
    DailyBar,
    fetch_daily_bars_range,
    fetch_live_price_context_optional,
)
from backend.universe import load_universe_sp500_and_nasdaq100


LOG = logging.getLogger("engine4_screener")


# ---------------------------------------------------------------------------
# Index Membership Data
# ---------------------------------------------------------------------------

def _read_index_file(path: Path) -> Set[str]:
    """Read tickers from an index file."""
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8", errors="ignore")
    tickers = set()
    for line in text.splitlines():
        s = line.strip().upper()
        if s and not s.startswith("#"):
            tickers.add(s)
    return tickers


def load_index_memberships(repo_root: Optional[Path] = None) -> Dict[str, str]:
    """
    Load index membership for each ticker.
    
    Returns:
        Dict mapping ticker -> "sp500", "nasdaq100", or "both"
    """
    root = repo_root or Path(__file__).resolve().parent.parent
    base = root / "data" / "universe"
    
    sp500 = _read_index_file(base / "sp500.txt")
    nasdaq100 = _read_index_file(base / "nasdaq100.txt")
    
    memberships: Dict[str, str] = {}
    all_tickers = sp500 | nasdaq100
    
    for ticker in all_tickers:
        in_sp = ticker in sp500
        in_ndx = ticker in nasdaq100
        
        if in_sp and in_ndx:
            memberships[ticker] = "both"
        elif in_sp:
            memberships[ticker] = "sp500"
        else:
            memberships[ticker] = "nasdaq100"
    
    return memberships


# ---------------------------------------------------------------------------
# Cache Configuration
# ---------------------------------------------------------------------------

# Full scan cache (structure only; live pricing is overlaid per-request).
# TTL kept short so reloads through the day surface new setups quickly.
try:
    _SCAN_TTL_S = int(get_flags().ENGINE4_CACHE_TTL_SCAN)
except Exception:
    _SCAN_TTL_S = 5 * 60
try:
    _BARS_TTL_S = int(get_flags().ENGINE4_CACHE_TTL_BARS)
except Exception:
    _BARS_TTL_S = 6 * 60 * 60

_scan_cache: TTLCache = TTLCache(maxsize=10, ttl=_SCAN_TTL_S)
_scan_cache_lock = threading.Lock()

# Per-ticker bars cache.
_bars_cache: TTLCache = TTLCache(maxsize=600, ttl=_BARS_TTL_S)
_bars_cache_lock = threading.Lock()

# Signal persistence store (Redis-aware, in-memory fallback). Mirrors the
# Red Dog tracker so E4/E5 lifecycle handling is identical.
_signal_store: Dict[str, Dict[str, Any]] = {}
_signal_store_lock = threading.Lock()

_SIGNAL_TTL_S = 21 * 24 * 3600  # keep tracked signals ~3 weeks
_REDIS_PREFIX = "engine4:signal:"
_REDIS_INDEX = "engine4:signal:index"

# Auto-evaluated lifecycle states (driven by price action).
_TERMINAL_STATUSES = {"target_hit", "stopped", "invalidated", "expired"}
# Desk-managed states (set by the trader, never overwritten by auto-eval).
DESK_STATUSES = {"watching", "entered", "working", "broken", "exited"}


def _signal_key(ticker: str, signal_date: str) -> str:
    return f"{ticker}:{signal_date}"


def _cache_key_scan(as_of: str, min_score: int, direction: Optional[str]) -> str:
    """Generate cache key for full scan results."""
    flags = get_flags()
    flag_hash = hashlib.md5(str(flags.cache_key()).encode()).hexdigest()[:8]
    dir_key = direction or "all"
    return f"e4_scan:{as_of}:{min_score}:{dir_key}:{flag_hash}"


def _cache_key_bars(ticker: str, as_of: str) -> str:
    """Generate cache key for ticker bars."""
    return f"e4_bars:{ticker}:{as_of}"


# ---------------------------------------------------------------------------
# Data Fetching
# ---------------------------------------------------------------------------

def fetch_bars_for_ticker(
    client: OratsClient,
    *,
    ticker: str,
    as_of_date: dt.date,
    lookback_days: int = 150,
    use_cache: bool = True,
) -> List[DailyBar]:
    """
    Fetch daily bars for a ticker with caching.
    Ichimoku needs 52+ bars for Span B, plus 26 bars for cloud projection alignment,
    so we request 150 calendar days to ensure ~100 trading days.

    Set ``use_cache=False`` to force a fresh pull (e.g. the desk hitting "Scan"
    for a current read), bypassing the 6-hour bars cache on read but still
    refreshing it on write.
    """
    as_of_str = as_of_date.isoformat()
    cache_key = _cache_key_bars(ticker, as_of_str)
    
    if use_cache:
        with _bars_cache_lock:
            cached = _bars_cache.get(cache_key)
            if cached is not None:
                return cached
    
    start = as_of_date - dt.timedelta(days=lookback_days)
    bars = fetch_daily_bars_range(client, ticker=ticker, start=start, end=as_of_date)
    
    with _bars_cache_lock:
        _bars_cache[cache_key] = bars
    
    return bars


def fetch_earnings_days_ahead(
    ticker: str,
    as_of_date: dt.date,
    benzinga_client: Any = None,
) -> Optional[int]:
    """
    Check if earnings are upcoming for a ticker.
    Returns days until earnings, or None if unknown/not soon.
    """
    # Try to use Benzinga client if available
    if benzinga_client is not None:
        try:
            from backend.earnings_calendar import benzinga_next_earnings
            earn_date = benzinga_next_earnings(benzinga_client, ticker=ticker)
            if earn_date:
                earn_dt = dt.date.fromisoformat(str(earn_date)[:10])
                days = (earn_dt - as_of_date).days
                if 0 <= days <= 10:
                    return days
        except Exception:
            pass
    
    return None


# ---------------------------------------------------------------------------
# Gamma Context
# ---------------------------------------------------------------------------

def fetch_gamma_context_spx(
    client: OratsClient,
    trade_date: dt.date,
) -> Dict[str, Any]:
    """
    Fetch SPX gamma context for S&P 500 names.
    """
    return _fetch_gamma_context_for_symbol(client, trade_date, symbols=["SPX", "SPXW"])


def fetch_gamma_context_ndx(
    client: OratsClient,
    trade_date: dt.date,
) -> Dict[str, Any]:
    """
    Fetch NDX/QQQ gamma context for Nasdaq 100 names.
    """
    return _fetch_gamma_context_for_symbol(client, trade_date, symbols=["QQQ", "NDX"])


def _fetch_gamma_context_for_symbol(
    client: OratsClient,
    trade_date: dt.date,
    symbols: List[str],
) -> Dict[str, Any]:
    """
    Fetch gamma context for given symbols with robust fallback logic.
    
    Strategy:
    1. Try live strikes first (market hours)
    2. Fall back to EOD hist_strikes
    3. Walk back up to 5 trading days to find data
    """
    fields = "ticker,tradeDate,expirDate,strike,spotPrice,stockPrice,gamma,callOpenInterest,putOpenInterest,callVolume,putVolume"
    
    # Find next Friday for weekly expiry
    now = dt.datetime.now()
    days_until_friday = (4 - trade_date.weekday()) % 7
    if days_until_friday == 0 and now.hour >= 16:
        days_until_friday = 7
    target_friday = trade_date + dt.timedelta(days=days_until_friday if days_until_friday > 0 else 7)
    
    strikes = None
    expiry_used = None
    data_source = "unknown"
    
    # STRATEGY 1: Try live strikes first (market hours)
    for symbol in symbols:
        try:
            resp = client.live_strikes_by_expiry(
                ticker=symbol,
                expiry=target_friday.isoformat(),
                fields=fields,
            )
            live_rows = resp.rows if hasattr(resp, "rows") else []
            if live_rows and len(live_rows) > 10:
                strikes = live_rows
                expiry_used = target_friday.isoformat()
                data_source = "live"
                LOG.info(f"Using live {symbol} strikes ({len(strikes)} rows)")
                break
        except Exception as e:
            LOG.debug(f"Live strikes for {symbol} failed: {e}")
            continue
    
    # STRATEGY 2: Try live_strikes without specific expiry
    if not strikes or len(strikes) < 10:
        for symbol in symbols:
            try:
                resp = client.live_strikes(
                    ticker=symbol,
                    fields=fields,
                )
                live_rows = resp.rows if hasattr(resp, "rows") else []
                if live_rows and len(live_rows) > 10:
                    strikes = live_rows
                    expiry_used = live_rows[0].get("expirDate", "")[:10] if live_rows else None
                    data_source = "live"
                    LOG.info(f"Using live {symbol} strikes without expiry filter ({len(strikes)} rows)")
                    break
            except Exception as e:
                LOG.debug(f"Live strikes (no expiry) for {symbol} failed: {e}")
                continue
    
    # STRATEGY 3: Fall back to EOD hist_strikes (after hours / weekends)
    if not strikes or len(strikes) < 10:
        LOG.info(f"Live strikes unavailable for {symbols}, falling back to EOD hist_strikes")
        
        dte_range = "3,21"  # 3-21 DTE
        
        # Walk back up to 5 trading days
        for days_back in range(0, 6):
            check_date = trade_date - dt.timedelta(days=days_back)
            # Skip weekends
            if check_date.weekday() >= 5:
                continue
            
            for symbol in symbols:
                try:
                    resp = client.get(
                        "hist/strikes",
                        ticker=symbol,
                        tradeDate=check_date.isoformat(),
                        dte=dte_range,
                        fields=fields,
                    )
                    rows = resp.rows if hasattr(resp, "rows") else []
                    if rows and len(rows) > 10:
                        # Pick expiry closest to target Friday
                        expiries = set(str(r.get("expirDate", ""))[:10] for r in rows if r.get("expirDate"))
                        if expiries:
                            chosen = min(expiries, key=lambda e: abs((dt.date.fromisoformat(e) - target_friday).days))
                            filtered = [r for r in rows if str(r.get("expirDate", ""))[:10] == chosen]
                            if len(filtered) > 10:
                                strikes = filtered
                                expiry_used = chosen
                                data_source = f"eod:{check_date.isoformat()}"
                                LOG.info(f"Using EOD {symbol} strikes from {check_date} ({len(strikes)} rows)")
                                break
                except Exception as e:
                    LOG.debug(f"EOD strikes for {symbol} on {check_date} failed: {e}")
                    continue
            
            if strikes and len(strikes) > 10:
                break
    
    # Process the strikes if we have them
    if not strikes or len(strikes) < 10:
        return {
            "available": False,
            "environment": "unknown",
            "recommendation": "Gamma context unavailable.",
            "warnings": [f"Could not fetch gamma data for {symbols}."],
        }
    
    gamma = compute_dealer_gamma_context(strikes, expiry=expiry_used)
    
    # Add environment classification for continuation setups
    net_sign = gamma.get("netGammaSign")
    if net_sign == "positive":
        gamma["environment"] = "supportive"
        gamma["recommendation"] = "Positive gamma supports pullback continuation setups."
    elif net_sign == "negative":
        gamma["environment"] = "challenging"
        gamma["recommendation"] = "Negative gamma can accelerate moves - be selective with entries."
    else:
        gamma["environment"] = "unknown"
        gamma["recommendation"] = "Gamma context unclear - proceed with standard criteria."
    
    # Add source metadata
    gamma["symbol"] = symbols[0] if symbols else "unknown"
    gamma["dataSource"] = data_source
    
    # Add note if using historical data
    if data_source.startswith("eod:"):
        eod_date = data_source.split(":")[1]
        gamma["recommendation"] = f"[EOD data from {eod_date}] " + gamma["recommendation"]
    
    return gamma


# ---------------------------------------------------------------------------
# Single Ticker Scan
# ---------------------------------------------------------------------------

def _compute_dollar_adv(bars: List[DailyBar], lookback: int = 20) -> Optional[float]:
    """20-day average dollar volume (close * volume) — a liquidity floor."""
    window = bars[-lookback:] if len(bars) >= lookback else bars
    vals = []
    for b in window:
        try:
            if b.close and b.volume:
                vals.append(float(b.close) * float(b.volume))
        except (TypeError, ValueError):
            continue
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


def scan_ticker(
    client: OratsClient,
    *,
    ticker: str,
    as_of_date: dt.date,
    index_membership: str,
    gamma_context: Optional[Dict[str, Any]] = None,
    benzinga_client: Any = None,
    min_dollar_adv: float = 0.0,
    use_cache: bool = True,
) -> Optional[IchimokuSignal]:
    """
    Scan a single ticker for Ichimoku continuation setup.
    Returns IchimokuSignal if found, None otherwise.
    """
    try:
        bars = fetch_bars_for_ticker(client, ticker=ticker, as_of_date=as_of_date, use_cache=use_cache)
        
        if not bars or len(bars) < 60:
            return None
        
        # Item 7: liquidity filter — skip names that can't absorb desk size.
        dollar_adv = _compute_dollar_adv(bars)
        if min_dollar_adv > 0 and (dollar_adv is None or dollar_adv < min_dollar_adv):
            return None
        
        # Check earnings
        earnings_days = fetch_earnings_days_ahead(ticker, as_of_date, benzinga_client)
        
        # Detect setup
        detection = detect_ichimoku_setup(
            bars,
            ticker=ticker,
            index_membership=index_membership,
            gamma_context=gamma_context,
            earnings_days_ahead=earnings_days,
        )
        
        if not detection.get("hasSignal"):
            return None
        
        # Compute Ichimoku series for freshness classification
        from backend.technicals import compute_ichimoku_series
        ich_series = compute_ichimoku_series(bars)
        closes = ich_series.get("closes", [])
        tenkan_series = ich_series.get("tenkan_series", [])
        
        # Build scored signal with freshness classification
        signal = build_ichimoku_signal(
            ticker=ticker,
            detection=detection,
            bars=bars,
            closes=closes,
            tenkan_series=tenkan_series,
            gamma_context=gamma_context,
            earnings_days_ahead=earnings_days,
            index_membership=index_membership,
            dollar_adv=dollar_adv,
        )
        
        return signal
        
    except Exception as e:
        LOG.warning(f"Error scanning {ticker}: {e}")
        return None


def scan_single_ticker(
    client: OratsClient,
    *,
    ticker: str,
    as_of_date: Optional[str] = None,
    benzinga_client: Any = None,
) -> Dict[str, Any]:
    """
    Full analysis for a single ticker (for detail endpoint).
    """
    t = str(ticker).strip().upper()
    today = dt.date.today()
    if as_of_date:
        try:
            today = dt.date.fromisoformat(str(as_of_date)[:10])
        except Exception:
            today = dt.date.today()
    
    # Determine index membership
    memberships = load_index_memberships()
    index_membership = memberships.get(t, "sp500")
    
    # Fetch appropriate gamma context
    if index_membership == "nasdaq100":
        gamma_context = fetch_gamma_context_ndx(client, today)
    else:
        gamma_context = fetch_gamma_context_spx(client, today)
    
    # Fetch bars
    bars = fetch_bars_for_ticker(client, ticker=t, as_of_date=today)
    
    if not bars or len(bars) < 60:
        return {
            "enabled": False,
            "ticker": t,
            "asOfDate": today.isoformat(),
            "notes": ["Insufficient data (need 60+ bars)."],
        }
    
    # Check earnings
    earnings_days = fetch_earnings_days_ahead(t, today, benzinga_client)
    
    # Full detection
    detection = detect_ichimoku_setup(
        bars,
        ticker=t,
        index_membership=index_membership,
        gamma_context=gamma_context,
        earnings_days_ahead=earnings_days,
    )
    
    result = {
        "enabled": detection.get("enabled", False),
        "ticker": t,
        "asOfDate": today.isoformat(),
        "hasSignal": detection.get("hasSignal", False),
        "signal": None,
        "trend": detection.get("trend"),
        "pullback": detection.get("pullback"),
        "trigger": detection.get("trigger"),
        "indicators": detection.get("indicators"),
        "gammaContext": gamma_context,
        "indexMembership": index_membership,
        "earningsDaysAhead": earnings_days,
        "notes": detection.get("notes", []),
    }
    
    if detection.get("hasSignal"):
        from backend.technicals import compute_ichimoku_series
        ich_series = compute_ichimoku_series(bars)
        signal = build_ichimoku_signal(
            ticker=t,
            detection=detection,
            bars=bars,
            closes=ich_series.get("closes", []),
            tenkan_series=ich_series.get("tenkan_series", []),
            gamma_context=gamma_context,
            earnings_days_ahead=earnings_days,
            index_membership=index_membership,
            dollar_adv=_compute_dollar_adv(bars),
        )
        if signal:
            result["signal"] = signal_to_dict(signal)
    
    return result


# ---------------------------------------------------------------------------
# Full Universe Scan
# ---------------------------------------------------------------------------

def run_universe_scan(
    client: OratsClient,
    *,
    as_of_date: Optional[str] = None,
    min_score: int = 50,
    direction: Optional[str] = None,
    benzinga_client: Any = None,
    max_workers: int = 10,
    use_cache: bool = True,
    persist: bool = True,
) -> Dict[str, Any]:
    """
    Scan the full SP500 + Nasdaq100 universe for Ichimoku setups.
    
    Args:
        client: ORATS client
        as_of_date: Scan date (YYYY-MM-DD), defaults to today
        min_score: Minimum score to include (0-100)
        direction: Filter by direction ("bullish", "bearish", or None for both)
        benzinga_client: Optional Benzinga client for earnings check
        max_workers: Number of parallel workers
        use_cache: When False, bypass the scan + bars caches and pull fresh
            (used by the "Scan" button and the cron breadth job).
        persist: When False, do not seed the desk tracker store with scanned
            names (used by the breadth job so it can't pollute the tracker with
            names the desk never looked at).
    
    Returns:
        Dict with scan results, stats, and gamma context
    """
    start_time = time.time()
    
    today = dt.date.today()
    if as_of_date:
        try:
            today = dt.date.fromisoformat(str(as_of_date)[:10])
        except Exception:
            today = dt.date.today()
    
    as_of_str = today.isoformat()
    
    flags = get_flags()
    min_dollar_adv = float(getattr(flags, "ENGINE4_MIN_DOLLAR_ADV", 0.0) or 0.0)
    structure_max = int(getattr(flags, "ENGINE4_STRUCTURE_MAX", 16) or 16)
    min_rr = float(getattr(flags, "ENGINE4_MIN_RR", 1.0) or 0.0)
    
    # Check cache (structure scan only; live pricing is overlaid per-request
    # by the router so a cache hit still reflects the current market).
    cache_key = _cache_key_scan(as_of_str, min_score, direction)
    if use_cache:
        with _scan_cache_lock:
            cached = _scan_cache.get(cache_key)
            if cached is not None:
                return cached
    
    # Load universe and memberships
    universe = load_universe_sp500_and_nasdaq100()
    memberships = load_index_memberships()
    
    # Fetch gamma contexts (once per index)
    gamma_spx = fetch_gamma_context_spx(client, today)
    gamma_ndx = fetch_gamma_context_ndx(client, today)
    
    # Scan in parallel
    signals: List[IchimokuSignal] = []
    errors: List[str] = []
    
    def _scan_one(ticker: str) -> Optional[IchimokuSignal]:
        membership = memberships.get(ticker, "sp500")
        
        # Select appropriate gamma context
        if membership == "nasdaq100":
            gamma = gamma_ndx
        else:
            gamma = gamma_spx
        
        return scan_ticker(
            client,
            ticker=ticker,
            as_of_date=today,
            index_membership=membership,
            gamma_context=gamma,
            benzinga_client=benzinga_client,
            min_dollar_adv=min_dollar_adv,
            use_cache=use_cache,
        )
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ticker = {executor.submit(_scan_one, t): t for t in universe}
        
        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            try:
                signal = future.result()
                if signal is not None:
                    signals.append(signal)
            except Exception as e:
                errors.append(f"{ticker}: {str(e)}")
    
    # Filter to A+ only (score >= 75) and by direction if specified
    aplus_signals = []
    sub_rr_count = 0
    for s in signals:
        if s.score < APLUS_THRESHOLD:
            continue
        if direction and s.direction != direction:
            continue
        # Risk:reward floor. reward_1r / risk_dollars must clear min_rr (default
        # 1:1). A setup can be technically perfect yet have Target 1 nearer than
        # the stop, which makes for a losing-expectancy trade the desk shouldn't
        # see. Guard against a zero/None risk so we never divide by zero.
        if min_rr > 0:
            risk = s.risk_dollars or 0.0
            reward = s.reward_1r or 0.0
            if risk <= 0 or (reward / risk) < min_rr:
                sub_rr_count += 1
                continue
        aplus_signals.append(s)
    
    # Sort by score descending
    aplus_signals.sort(key=lambda x: x.score, reverse=True)
    
    # Classify A+ signals into freshness buckets
    actionable = []
    structure = []
    rejected_count = 0
    
    for s in aplus_signals:
        if s.freshness_bucket == "actionable":
            actionable.append(s)
        elif s.freshness_bucket == "structure":
            structure.append(s)
        elif s.freshness_bucket == "rejected":
            rejected_count += 1
            # Don't include rejected signals in output
    
    # Item 1: trim structure to a tight, ranked "Approaching" shortlist.
    # Sort by distance-to-actionable (closest first), then score; cap the list
    # so the desk isn't drowning in names that are days away from a trigger.
    structure.sort(
        key=lambda x: ((x.distance_to_actionable if x.distance_to_actionable is not None else 999.0), -x.score)
    )
    structure_total = len(structure)
    if structure_max > 0:
        structure = structure[:structure_max]
    
    # Persist actionable + structure to the tracker store (Redis-aware,
    # preserves desk overrides). Only fresh actionable names auto-enter as
    # "pending"; structure stays as a watch surface. NOTE: _persist_signals
    # expects API dicts, not IchimokuSignal dataclasses.
    # The cron breadth job passes persist=False so it can't seed the desk
    # tracker with names nobody looked at.
    if persist:
        _persist_signals([signal_to_dict(s) for s in (actionable + structure)])
    
    elapsed_ms = int((time.time() - start_time) * 1000)
    
    result = {
        "asOfDate": as_of_str,
        "scannedCount": len(universe),
        "totalAPlus": len(aplus_signals),
        "actionableCount": len(actionable),
        "structureCount": len(structure),
        "structureTotal": structure_total,
        "rejectedCount": rejected_count,
        "actionable": [signal_to_dict(s) for s in actionable],
        "structure": [signal_to_dict(s) for s in structure],
        "marketGamma": {
            "spx": gamma_spx,
            "ndx": gamma_ndx,
        },
        "meta": {
            "scanDurationMs": elapsed_ms,
            "direction": direction,
            "minDollarAdv": min_dollar_adv,
            "structureMax": structure_max,
            "minRR": min_rr,
            "subRRRejected": sub_rr_count,
            "errors": errors[:10] if errors else [],
        },
    }
    
    # Cache result
    with _scan_cache_lock:
        _scan_cache[cache_key] = result
    
    return result


# ---------------------------------------------------------------------------
# Live Re-Pricing Overlay
# ---------------------------------------------------------------------------

def compute_live_state(
    *,
    direction: str,
    price: float,
    entry_trigger: Optional[float],
    stop_loss: Optional[float],
    target_1: Optional[float],
    atr: Optional[float],
) -> Dict[str, Any]:
    """Re-evaluate a setup's *entry trigger* against the current price.

    The Ichimoku structure (Tenkan/Kijun/cloud, and therefore the trigger and
    stop levels) is fixed for the day once the prior bar closes. What moves
    intraday is price relative to those fixed levels — which is exactly what
    tells the desk "is this still 0.29 away, or did it already trigger?".

    ``toTrigger`` is the signed distance the price must still travel to reach
    the entry trigger (positive = not yet triggered). ``state`` is one of:
    pending | triggered | stopped | target1.
    """
    is_bull = direction == "bullish"
    out: Dict[str, Any] = {
        "price": round(float(price), 4),
        "toTrigger": None,
        "toTriggerPct": None,
        "toTriggerAtr": None,
        "state": "pending",
    }

    if entry_trigger is not None and price > 0:
        # Distance the price must still move to hit the trigger.
        to_trigger = (entry_trigger - price) if is_bull else (price - entry_trigger)
        out["toTrigger"] = round(to_trigger, 4)
        out["toTriggerPct"] = round((to_trigger / price) * 100.0, 3)
        if atr and atr > 0:
            out["toTriggerAtr"] = round(to_trigger / atr, 2)

    # A continuation entry is a stop order beyond the trigger level, so the
    # stop/target only mean anything *after* the trigger fires. Before it
    # fires, an adverse move through the stop level means the setup broke down
    # before triggering → "invalidated" (not "stopped").
    triggered = False
    if entry_trigger is not None:
        triggered = (price >= entry_trigger) if is_bull else (price <= entry_trigger)

    state = "pending"
    if triggered:
        state = "triggered"
        if target_1 is not None and ((price >= target_1) if is_bull else (price <= target_1)):
            state = "target1"
        elif stop_loss is not None and ((price <= stop_loss) if is_bull else (price >= stop_loss)):
            state = "stopped"
    else:
        # Not yet triggered: flag a pre-trigger breakdown through the stop.
        if stop_loss is not None and ((price <= stop_loss) if is_bull else (price >= stop_loss)):
            state = "invalidated"
    out["state"] = state
    return out


def overlay_signal_list(
    sigs: List[Dict[str, Any]],
    client: OratsClient,
    *,
    max_workers: int = 10,
) -> int:
    """Annotate a flat list of signal dicts with a fresh ``live`` block.

    Each signal must carry ``ticker``, ``direction``, ``levels`` and
    ``indicators``. One live quote per distinct ticker. Returns the number of
    signals annotated.
    """
    sigs = [s for s in (sigs or []) if isinstance(s, dict)]
    if not sigs:
        return 0

    # De-dup tickers so a name in multiple buckets is only quoted once.
    by_ticker: Dict[str, List[Dict[str, Any]]] = {}
    for s in sigs:
        t = str(s.get("ticker") or "").upper()
        if t:
            by_ticker.setdefault(t, []).append(s)

    def _quote(ticker: str) -> Optional[Dict[str, Any]]:
        try:
            return fetch_live_price_context_optional(client, ticker=ticker)
        except Exception:
            return None

    contexts: Dict[str, Optional[Dict[str, Any]]] = {}
    if by_ticker:
        workers = max(1, min(max_workers, len(by_ticker)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_quote, t): t for t in by_ticker}
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    contexts[t] = fut.result()
                except Exception:
                    contexts[t] = None

    annotated = 0
    now_iso = dt.datetime.utcnow().isoformat() + "Z"
    for ticker, members in by_ticker.items():
        ctx = contexts.get(ticker) or {}
        price = ctx.get("price")
        if price is None or float(price) <= 0:
            for s in members:
                s["live"] = {"available": False, "asOf": now_iso}
            continue
        for s in members:
            levels = s.get("levels", {}) or {}
            indicators = s.get("indicators", {}) or {}
            state = compute_live_state(
                direction=s.get("direction", "bullish"),
                price=float(price),
                entry_trigger=levels.get("entryTrigger"),
                stop_loss=levels.get("stopLoss"),
                target_1=levels.get("target1"),
                atr=indicators.get("atr"),
            )
            state["available"] = True
            state["asOf"] = now_iso
            state["marketOpen"] = bool(ctx.get("marketOpen"))
            state["source"] = ctx.get("source")
            s["live"] = state
            annotated += 1

    return annotated


def apply_live_price_overlay(
    result: Dict[str, Any],
    client: OratsClient,
    *,
    max_workers: int = 10,
) -> int:
    """Annotate the surfaced scan signals (actionable + structure) in-place
    with a fresh ``live`` block.

    Runs on every request (even a structure-cache hit), so reloading the page
    through the day always re-prices the displayed names against the current
    market without re-running the expensive universe scan.
    """
    if not isinstance(result, dict):
        return 0
    sigs: List[Dict[str, Any]] = []
    for key in ("actionable", "structure"):
        block = result.get(key)
        if isinstance(block, list):
            sigs.extend([s for s in block if isinstance(s, dict)])
    return overlay_signal_list(sigs, client, max_workers=max_workers)


def overlay_tracker_signals(
    signals: Dict[str, Any],
    client: OratsClient,
    *,
    max_workers: int = 10,
) -> int:
    """Annotate every tracked record (across all lifecycle buckets) with a
    fresh ``live`` block, so the desk book reflects current pricing even on a
    plain (non-refresh) load.
    """
    if not isinstance(signals, dict):
        return 0
    flat: List[Dict[str, Any]] = []
    for v in signals.values():
        if isinstance(v, list):
            flat.extend([s for s in v if isinstance(s, dict)])
    return overlay_signal_list(flat, client, max_workers=max_workers)


# ---------------------------------------------------------------------------
# Desk Trade Tracker (Redis-aware lifecycle + desk-managed states)
# ---------------------------------------------------------------------------

def _persist_signals(signal_dicts: List[Dict[str, Any]]) -> None:
    """Persist freshly scanned signals for later outcome tracking.

    Never downgrades a signal that already has a terminal/triggered status or
    a desk-managed state — the trader's view of an in-flight position wins.
    """
    if not signal_dicts:
        return
    from backend.redis_store import get_store_optional

    # Defensive: accept IchimokuSignal dataclasses too, so a caller that
    # forgets to serialize can't 500 the whole scan.
    signal_dicts = [
        d if isinstance(d, dict) else signal_to_dict(d)
        for d in signal_dicts
    ]

    store = get_store_optional()
    protected = _TERMINAL_STATUSES | {"triggered"} | DESK_STATUSES
    with _signal_store_lock:
        index_keys = set()
        if store:
            index_keys = set(store.get_json(_REDIS_INDEX) or [])
        for d in signal_dicts:
            ticker = d.get("ticker", "")
            sig_date = d.get("signalDate", "")
            if not ticker or not sig_date:
                continue
            key = _signal_key(ticker, sig_date)

            # Redis is the source of truth across gunicorn workers; the
            # per-worker in-memory store is only a no-Redis fallback. Reading
            # in-memory first would let a stale worker copy clobber a desk
            # state another worker just wrote.
            if store:
                prior = store.get_json(_REDIS_PREFIX + key)
                if prior is None:
                    prior = _signal_store.get(key)
            else:
                prior = _signal_store.get(key)
            if prior and prior.get("status") in protected:
                # Refresh the scored snapshot but keep desk/lifecycle state.
                merged = dict(d)
                for fld in ("status", "deskNotes", "outcome", "statusUpdatedAt",
                            "trackedAt", "pinned", "invalidationReason"):
                    if fld in prior:
                        merged[fld] = prior[fld]
                _signal_store[key] = merged
                if store:
                    store.set_json(_REDIS_PREFIX + key, merged, ttl_s=_SIGNAL_TTL_S)
                    index_keys.add(key)
                continue

            record = dict(d)
            record.setdefault("status", "pending")
            record["trackedAt"] = dt.datetime.utcnow().isoformat() + "Z"
            _signal_store[key] = record
            if store:
                store.set_json(_REDIS_PREFIX + key, record, ttl_s=_SIGNAL_TTL_S)
                index_keys.add(key)
        if store:
            store.set_json(_REDIS_INDEX, sorted(index_keys), ttl_s=_SIGNAL_TTL_S)


def _all_records() -> List[Dict[str, Any]]:
    """Return every tracked signal, preferring Redis when present."""
    from backend.redis_store import get_store_optional

    store = get_store_optional()
    records: Dict[str, Dict[str, Any]] = {}
    if store:
        for key in (store.get_json(_REDIS_INDEX) or []):
            rec = store.get_json(_REDIS_PREFIX + key)
            if rec:
                records[key] = rec
    with _signal_store_lock:
        for key, rec in _signal_store.items():
            records.setdefault(key, rec)
    return list(records.values())


def _write_record(key: str, record: Dict[str, Any]) -> None:
    with _signal_store_lock:
        _signal_store[key] = record
    from backend.redis_store import get_store_optional

    store = get_store_optional()
    if store:
        store.set_json(_REDIS_PREFIX + key, record, ttl_s=_SIGNAL_TTL_S)
        index = set(store.get_json(_REDIS_INDEX) or [])
        index.add(key)
        store.set_json(_REDIS_INDEX, sorted(index), ttl_s=_SIGNAL_TTL_S)


def _find_record(ticker: str, signal_date: Optional[str]) -> Optional[Dict[str, Any]]:
    """Locate a tracked record by ticker (+ optional signal date)."""
    ticker = (ticker or "").upper()
    candidates = [r for r in _all_records() if (r.get("ticker") or "").upper() == ticker]
    if not candidates:
        return None
    if signal_date:
        for r in candidates:
            if str(r.get("signalDate", ""))[:10] == str(signal_date)[:10]:
                return r
    # Most recent signal date wins.
    candidates.sort(key=lambda r: str(r.get("signalDate", "")), reverse=True)
    return candidates[0]


def get_all_signals() -> Dict[str, Any]:
    """Group tracked Ichimoku signals by lifecycle + desk-managed status."""
    records = _all_records()
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "pending": [], "triggered": [], "target_hit": [], "stopped": [],
        "invalidated": [], "expired": [],
        "watching": [], "entered": [], "working": [], "broken": [], "exited": [],
    }
    for rec in records:
        status = rec.get("status", "pending")
        buckets.setdefault(status, []).append(rec)

    resolved = buckets["target_hit"] + buckets["stopped"]
    wins = len(buckets["target_hit"])
    win_rate = round(100.0 * wins / len(resolved), 1) if resolved else None
    # "Desk book" = anything the trader is actively managing.
    desk_book = sum(len(buckets[s]) for s in DESK_STATUSES)

    return {
        "totalSignals": len(records),
        "counts": {k: len(v) for k, v in buckets.items()},
        "winRate": win_rate,
        "resolvedCount": len(resolved),
        "deskBookCount": desk_book,
        **buckets,
    }


def set_desk_status(
    ticker: str,
    *,
    desk_status: str,
    signal_date: Optional[str] = None,
    note: Optional[str] = None,
    pinned: Optional[bool] = None,
) -> Dict[str, Any]:
    """Desk override: mark a name watching/entered/working/broken/exited.

    Returns {ok, record|error}. Desk states survive scan refreshes and are
    never clobbered by the auto-evaluator.
    """
    desk_status = (desk_status or "").strip().lower()
    if desk_status not in DESK_STATUSES:
        return {"ok": False, "error": f"Invalid desk status '{desk_status}'. Allowed: {sorted(DESK_STATUSES)}"}

    rec = _find_record(ticker, signal_date)
    if rec is None:
        return {"ok": False, "error": f"No tracked signal for {ticker}."}

    rec = dict(rec)
    rec["status"] = desk_status
    rec["statusUpdatedAt"] = dt.datetime.utcnow().isoformat() + "Z"
    if pinned is not None:
        rec["pinned"] = bool(pinned)
    if note:
        notes = list(rec.get("deskNotes", []))
        notes.append({"ts": rec["statusUpdatedAt"], "status": desk_status, "note": note})
        rec["deskNotes"] = notes

    _write_record(_signal_key(rec.get("ticker", ""), rec.get("signalDate", "")), rec)
    return {"ok": True, "record": rec}


def remove_signal(ticker: str, signal_date: Optional[str] = None) -> Dict[str, Any]:
    """Remove a tracked signal from the desk book entirely (e.g. a mis-click)."""
    rec = _find_record(ticker, signal_date)
    if rec is None:
        return {"ok": False, "error": f"No tracked signal for {ticker}."}
    key = _signal_key(rec.get("ticker", ""), rec.get("signalDate", ""))

    with _signal_store_lock:
        _signal_store.pop(key, None)
    from backend.redis_store import get_store_optional

    store = get_store_optional()
    if store:
        store.delete_key(_REDIS_PREFIX + key)
        index = set(store.get_json(_REDIS_INDEX) or [])
        index.discard(key)
        store.set_json(_REDIS_INDEX, sorted(index), ttl_s=_SIGNAL_TTL_S)
    return {"ok": True, "removed": key}


def refresh_signal_statuses(
    client: OratsClient,
    as_of_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Re-evaluate every auto-tracked signal against forward price action.

    Desk-managed states are left untouched (the trader owns those); only
    pending/triggered signals get auto-promoted to target_hit/stopped/expired.
    """
    from backend.engine3_red_dog import evaluate_outcome

    today = dt.date.today()
    if as_of_date:
        try:
            today = dt.date.fromisoformat(str(as_of_date)[:10])
        except Exception:
            today = dt.date.today()

    updated = 0
    changed = 0
    for rec in _all_records():
        status = rec.get("status")
        if status in _TERMINAL_STATUSES or status in DESK_STATUSES:
            continue
        ticker = rec.get("ticker", "")
        sig_date = rec.get("signalDate", "")
        levels = rec.get("levels", {}) or {}
        direction = rec.get("direction", "")
        if not ticker or not sig_date:
            continue

        try:
            bars = fetch_bars_for_ticker(client, ticker=ticker, as_of_date=today, use_cache=False)
        except Exception:
            continue
        forward = [b for b in bars if str(b.trade_date)[:10] > str(sig_date)[:10]]

        # Fold in the live price as a synthetic current-day bar so an intraday
        # trigger/stop is caught immediately instead of waiting for the daily
        # bar to close. Without this, a name can read "pending" all session
        # even though price already blew through the entry.
        try:
            ctx = fetch_live_price_context_optional(client, ticker=ticker)
            live_px = ctx.get("price") if isinstance(ctx, dict) else None
        except Exception:
            live_px = None
        if live_px is not None and float(live_px) > 0:
            today_str = today.isoformat()
            has_today = any(str(b.trade_date)[:10] == today_str for b in forward)
            if not has_today and today_str > str(sig_date)[:10]:
                px = float(live_px)
                forward.append(DailyBar(
                    trade_date=today_str, open=px, high=px, low=px,
                    close=px, volume=None, vwap=None,
                ))

        if not forward:
            continue

        outcome = evaluate_outcome(
            direction=direction,
            entry_trigger=float(levels.get("entryTrigger") or 0.0),
            stop_loss=float(levels.get("stopLoss") or 0.0),
            target_1=float(levels.get("target1") or 0.0),
            forward_bars=forward,
        )
        new_status = outcome["status"]
        updated += 1
        if new_status != status:
            rec = dict(rec)
            rec["status"] = new_status
            rec["outcome"] = outcome
            rec["statusUpdatedAt"] = dt.datetime.utcnow().isoformat() + "Z"
            _write_record(_signal_key(ticker, sig_date), rec)
            changed += 1

    return {
        "updated": updated,
        "changed": changed,
        "asOfDate": today.isoformat(),
    }
