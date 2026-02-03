"""
Engine 3: Red Dog Reversal Universe Scanner

Scans the SP100 + Nasdaq100 universe for Red Dog Reversal setups
with caching and parallel processing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cachetools import TTLCache

from backend.config import get_flags
from backend.orats_client import OratsClient
from backend.technicals import DailyBar, fetch_daily_bars_range
from backend.universe import load_universe_sp500_and_nasdaq100
from backend.dealer_gamma_context import compute_dealer_gamma_context
from backend.engine3_red_dog import (
    APLUS_THRESHOLD,
    RedDogSignal,
    build_red_dog_signal,
    detect_red_dog_enhanced,
    signal_to_dict,
)


LOG = logging.getLogger("engine3_screener")


# ---------------------------------------------------------------------------
# Cache Configuration
# ---------------------------------------------------------------------------

# Full scan cache (30 minutes - refreshes mid-day and EOD)
_scan_cache: TTLCache = TTLCache(maxsize=10, ttl=30 * 60)
_scan_cache_lock = threading.Lock()

# Per-ticker bars cache (6 hours - daily data doesn't change much)
_bars_cache: TTLCache = TTLCache(maxsize=500, ttl=6 * 60 * 60)
_bars_cache_lock = threading.Lock()


def _cache_key_scan(as_of: str, min_score: int, direction: Optional[str]) -> str:
    """Generate cache key for full scan results."""
    flags = get_flags()
    flag_hash = hashlib.md5(str(flags.cache_key()).encode()).hexdigest()[:8]
    dir_key = direction or "all"
    return f"e3_scan:{as_of}:{min_score}:{dir_key}:{flag_hash}"


def _cache_key_bars(ticker: str, as_of: str) -> str:
    """Generate cache key for ticker bars."""
    return f"e3_bars:{ticker}:{as_of}"


# ---------------------------------------------------------------------------
# Data Fetching
# ---------------------------------------------------------------------------

def fetch_bars_for_ticker(
    client: OratsClient,
    *,
    ticker: str,
    as_of_date: dt.date,
    lookback_days: int = 60,
) -> List[DailyBar]:
    """
    Fetch daily bars for a ticker with caching.
    """
    as_of_str = as_of_date.isoformat()
    cache_key = _cache_key_bars(ticker, as_of_str)
    
    with _bars_cache_lock:
        cached = _bars_cache.get(cache_key)
        if cached is not None:
            return cached
    
    start = as_of_date - dt.timedelta(days=lookback_days)
    bars = fetch_daily_bars_range(client, ticker=ticker, start=start, end=as_of_date)
    
    with _bars_cache_lock:
        _bars_cache[cache_key] = bars
    
    return bars


def scan_ticker(
    client: OratsClient,
    *,
    ticker: str,
    as_of_date: dt.date,
) -> Optional[RedDogSignal]:
    """
    Scan a single ticker for Red Dog setup.
    Returns RedDogSignal if found, None otherwise.
    """
    try:
        bars = fetch_bars_for_ticker(client, ticker=ticker, as_of_date=as_of_date)
        
        if not bars or len(bars) < 21:
            return None
        
        detection = detect_red_dog_enhanced(bars, ticker=ticker)
        
        if not detection.get("bullish") and not detection.get("bearish"):
            return None
        
        signal = build_red_dog_signal(
            ticker=ticker,
            detection=detection,
            near_support_resistance=False,  # TODO: Add S/R detection
        )
        
        return signal
        
    except Exception as e:
        LOG.warning(f"Error scanning {ticker}: {e}")
        return None


# ---------------------------------------------------------------------------
# Market Gamma Context
# ---------------------------------------------------------------------------

def _fetch_eod_strikes_for_gamma(client: OratsClient, trade_date: dt.date) -> Tuple[List[dict], Optional[str]]:
    """
    Fetch EOD strikes data for gamma calculation.
    Returns (strikes_rows, expiry_used).
    """
    # Fields needed for gamma calculation
    fields = "ticker,tradeDate,expirDate,strike,spotPrice,stockPrice,gamma,callOpenInterest,putOpenInterest,callVolume,putVolume"
    
    # Find a Friday expiry near the trade date (prefer nearest future Friday)
    days_until_friday = (4 - trade_date.weekday()) % 7
    target_friday = trade_date + dt.timedelta(days=days_until_friday if days_until_friday > 0 else 7)
    
    # Calculate DTE range for near-term expiries (7-14 DTE works well)
    dte_range = "3,21"  # 3-21 DTE captures weekly/monthly expiries
    
    for symbol in ("SPX", "SPXW"):
        try:
            resp = client.hist_strikes(
                ticker=symbol,
                trade_date=trade_date.isoformat(),
                fields=fields,
                dte=dte_range,
            )
            rows = resp.rows or []
            if rows and len(rows) > 10:
                # Get expiry from first row
                expiry = rows[0].get("expirDate", "")[:10] if rows else None
                return rows, expiry
        except Exception:
            continue
    
    return [], None


def fetch_spx_gamma_context(client: OratsClient, as_of_date: Optional[dt.date] = None) -> Dict[str, Any]:
    """
    Fetch SPX dealer gamma context for Red Dog overlay.
    
    Works both during market hours (live data) and outside market hours (EOD data).
    Returns a simplified gamma context with trading implications.
    """
    try:
        now = dt.datetime.now()
        scan_date = as_of_date or dt.date.today()
        
        # Find next Friday for weekly expiry (for live data targeting)
        days_until_friday = (4 - now.weekday()) % 7
        if days_until_friday == 0 and now.hour >= 16:
            days_until_friday = 7
        next_friday = (now + dt.timedelta(days=days_until_friday)).date()
        
        strikes = None
        expiry_used = None
        data_source = "unknown"
        
        # STRATEGY 1: Try live strikes first (market hours)
        for symbol in ("SPXW", "SPX"):
            try:
                resp = client.live_strikes_by_expiry(
                    ticker=symbol,
                    expiry=next_friday.isoformat(),
                    fields="ticker,tradeDate,expirDate,strike,spotPrice,stockPrice,gamma,callOpenInterest,putOpenInterest,callVolume,putVolume",
                )
                live_rows = resp.rows or []
                if live_rows and len(live_rows) > 10:
                    strikes = live_rows
                    expiry_used = next_friday.isoformat()
                    data_source = "live"
                    break
            except Exception:
                continue
        
        # Fallback: try live_strikes without specific expiry
        if not strikes or len(strikes) < 10:
            try:
                resp = client.live_strikes(
                    ticker="SPX",
                    fields="ticker,tradeDate,expirDate,strike,spotPrice,stockPrice,gamma,callOpenInterest,putOpenInterest,callVolume,putVolume",
                )
                live_rows = resp.rows or []
                if live_rows and len(live_rows) > 10:
                    strikes = live_rows
                    expiry_used = live_rows[0].get("expirDate", "")[:10] if live_rows else None
                    data_source = "live"
            except Exception:
                pass
        
        # STRATEGY 2: Fall back to EOD hist_strikes (after hours / weekends)
        if not strikes or len(strikes) < 10:
            LOG.info("Live SPX strikes unavailable, falling back to EOD hist_strikes")
            
            # Try recent trading days (walk back up to 5 days to find data)
            for days_back in range(0, 6):
                check_date = scan_date - dt.timedelta(days=days_back)
                # Skip weekends
                if check_date.weekday() >= 5:
                    continue
                
                eod_rows, eod_expiry = _fetch_eod_strikes_for_gamma(client, check_date)
                if eod_rows and len(eod_rows) > 10:
                    strikes = eod_rows
                    expiry_used = eod_expiry
                    data_source = f"eod:{check_date.isoformat()}"
                    LOG.info(f"Using EOD SPX strikes from {check_date} ({len(strikes)} rows)")
                    break
        
        if not strikes or len(strikes) < 10:
            return _empty_gamma_context("Unable to fetch SPX option chain (live or EOD)")
        
        # Compute dealer gamma
        gamma_ctx = compute_dealer_gamma_context(
            strikes,
            expiry=expiry_used,
            band_pct=0.03,  # ±3% band around spot
            top_n=5,
        )
        
        # Interpret for Red Dog trading
        net_sign = gamma_ctx.get("netGammaSign", "unknown")
        magnitude = gamma_ctx.get("magnitudeBucket", "unknown")
        spot = gamma_ctx.get("spot")
        
        # Trading interpretation
        if net_sign == "positive":
            environment = "supportive"
            red_dog_bias = "favorable"
            explanation = (
                "Dealers are net LONG gamma. They will buy dips and sell rips, "
                "which naturally supports mean reversion patterns like Red Dog. "
                "Failed breakdowns have dealer flow as a tailwind."
            )
            recommendation = "Green light for Red Dog setups. Dealers are your ally."
        else:
            environment = "challenging"
            red_dog_bias = "caution"
            explanation = (
                "Dealers are net SHORT gamma. They will sell into weakness and buy into strength, "
                "which can amplify directional moves. Mean reversion patterns face headwinds "
                "as dealer hedging works against the reversal."
            )
            recommendation = (
                "Exercise caution with Red Dog setups. Require higher scores (A+ only) "
                "or additional confluence before entering."
            )
        
        # Add EOD note to explanation if using historical data
        if data_source.startswith("eod:"):
            eod_date = data_source.split(":")[1]
            explanation = f"[Using EOD data from {eod_date}] " + explanation
        
        return {
            "available": True,
            "spot": spot,
            "expiry": expiry_used,
            "dataSource": data_source,
            "netGammaSign": net_sign,
            "magnitude": magnitude,
            "environment": environment,
            "redDogBias": red_dog_bias,
            "explanation": explanation,
            "recommendation": recommendation,
            "callsGex": gamma_ctx.get("callsGex"),
            "putsGex": gamma_ctx.get("putsGex"),
            "netGex": gamma_ctx.get("netGex"),
            "warnings": gamma_ctx.get("warnings", []),
        }
        
    except Exception as e:
        LOG.warning(f"Failed to fetch SPX gamma context: {e}")
        return _empty_gamma_context(f"Error: {str(e)}")


def _empty_gamma_context(reason: str) -> Dict[str, Any]:
    """Return empty gamma context with explanation."""
    return {
        "available": False,
        "spot": None,
        "expiry": None,
        "dataSource": None,
        "netGammaSign": None,
        "magnitude": None,
        "environment": "unknown",
        "redDogBias": "neutral",
        "explanation": reason,
        "recommendation": "Gamma context unavailable. Proceed based on pattern quality alone.",
        "callsGex": None,
        "putsGex": None,
        "netGex": None,
        "warnings": [reason],
    }


# ---------------------------------------------------------------------------
# SPX 21 EMA Trend Filter
# ---------------------------------------------------------------------------

def _compute_ema(values: List[float], period: int) -> Optional[float]:
    """Compute Exponential Moving Average."""
    if len(values) < period:
        return None
    
    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period  # Start with SMA
    
    for price in values[period:]:
        ema = (price - ema) * multiplier + ema
    
    return ema


def fetch_spx_trend_context(client: OratsClient, as_of_date: Optional[dt.date] = None) -> Dict[str, Any]:
    """
    Fetch SPX 21 EMA trend context for Red Dog filtering.
    
    Works both during market hours and outside market hours using EOD daily bars.
    Returns trend status and trading implications.
    """
    try:
        scan_date = as_of_date or dt.date.today()
        data_source = "unknown"
        bars_date_used = None
        
        # Use SPY as SPX proxy (more liquid, same direction)
        # Try fetching bars - this uses hist_dailies which is EOD data
        bars = fetch_bars_for_ticker(client, ticker="SPY", as_of_date=scan_date, lookback_days=60)
        
        # If no bars for scan_date, try walking back a few days (weekend handling)
        if not bars or len(bars) < 25:
            for days_back in range(1, 6):
                check_date = scan_date - dt.timedelta(days=days_back)
                if check_date.weekday() >= 5:  # Skip weekends
                    continue
                bars = fetch_bars_for_ticker(client, ticker="SPY", as_of_date=check_date, lookback_days=60)
                if bars and len(bars) >= 25:
                    bars_date_used = check_date.isoformat()
                    break
        
        if not bars or len(bars) < 25:
            return _empty_trend_context("Insufficient SPX price history for 21 EMA calculation")
        
        # Track data source
        last_bar_date = bars[-1].trade_date if bars else None
        data_source = f"eod:{last_bar_date}" if last_bar_date else "eod"
        
        # Extract closes (most recent last)
        closes = [float(b.close) for b in bars if b.close is not None and b.close > 0]
        
        if len(closes) < 25:
            return _empty_trend_context("Insufficient closing prices for 21 EMA")
        
        # Calculate 21 EMA
        ema_21 = _compute_ema(closes, 21)
        current_price = closes[-1]
        
        if ema_21 is None:
            return _empty_trend_context("Unable to calculate 21 EMA")
        
        # Determine trend
        above_ema = current_price > ema_21
        distance_pct = ((current_price - ema_21) / ema_21) * 100
        
        # Trend strength
        if abs(distance_pct) < 0.5:
            trend_strength = "neutral"
        elif abs(distance_pct) < 2.0:
            trend_strength = "moderate"
        else:
            trend_strength = "strong"
        
        # Trading implications
        if above_ema:
            trend_direction = "bullish"
            bullish_bias = "aligned"
            bearish_bias = "counter"
            explanation = (
                f"SPX is trading {abs(distance_pct):.1f}% ABOVE its 21 EMA. "
                "This indicates an uptrend environment. Bullish Red Dog setups (failed breakdowns) "
                "are trading WITH the trend and have higher probability. Bearish setups are counter-trend."
            )
            recommendation = (
                "FAVOR BULLISH setups. Failed breakdowns in an uptrend have natural tailwinds. "
                "Bearish setups require extra caution — only take with A+ scores and tight stops."
            )
        else:
            trend_direction = "bearish"
            bullish_bias = "counter"
            bearish_bias = "aligned"
            explanation = (
                f"SPX is trading {abs(distance_pct):.1f}% BELOW its 21 EMA. "
                "This indicates a downtrend environment. Bearish Red Dog setups (failed breakouts) "
                "are trading WITH the trend and have higher probability. Bullish setups are counter-trend."
            )
            recommendation = (
                "FAVOR BEARISH setups. Failed breakouts in a downtrend have natural tailwinds. "
                "Bullish setups require extra caution — only take with A+ scores and tight stops."
            )
        
        # Add EOD note to explanation if not from today
        if data_source.startswith("eod:") and last_bar_date != scan_date.isoformat():
            explanation = f"[Based on EOD data from {last_bar_date}] " + explanation
        
        return {
            "available": True,
            "currentPrice": round(current_price, 2),
            "ema21": round(ema_21, 2),
            "aboveEma": above_ema,
            "distancePct": round(distance_pct, 2),
            "trendDirection": trend_direction,
            "trendStrength": trend_strength,
            "bullishBias": bullish_bias,  # "aligned" or "counter"
            "bearishBias": bearish_bias,  # "aligned" or "counter"
            "dataSource": data_source,
            "explanation": explanation,
            "recommendation": recommendation,
            "asOfDate": scan_date.isoformat(),
            "dataAsOfDate": last_bar_date,
        }
        
    except Exception as e:
        LOG.warning(f"Failed to fetch SPX trend context: {e}")
        return _empty_trend_context(f"Error: {str(e)}")


def _empty_trend_context(reason: str) -> Dict[str, Any]:
    """Return empty trend context with explanation."""
    return {
        "available": False,
        "currentPrice": None,
        "ema21": None,
        "aboveEma": None,
        "distancePct": None,
        "trendDirection": "unknown",
        "trendStrength": "unknown",
        "bullishBias": "unknown",
        "bearishBias": "unknown",
        "dataSource": None,
        "explanation": reason,
        "recommendation": "Trend filter unavailable. Use pattern quality and gamma context for decisions.",
        "asOfDate": None,
        "dataAsOfDate": None,
    }


def get_signal_trend_alignment(signal_direction: str, trend_context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Determine if a signal is aligned with or counter to the trend.
    
    Returns alignment status and trading guidance.
    """
    if not trend_context.get("available"):
        return {
            "alignment": "unknown",
            "label": "Trend N/A",
            "color": "neutral",
            "guidance": "Trend filter unavailable — proceed based on pattern quality.",
        }
    
    direction = signal_direction.lower()
    trend_dir = trend_context.get("trendDirection", "unknown")
    
    if direction == "bullish":
        bias = trend_context.get("bullishBias", "unknown")
    else:
        bias = trend_context.get("bearishBias", "unknown")
    
    if bias == "aligned":
        return {
            "alignment": "aligned",
            "label": "With Trend ✓",
            "color": "positive",
            "guidance": f"This {direction} setup aligns with the {trend_dir} SPX trend. Higher probability.",
        }
    elif bias == "counter":
        return {
            "alignment": "counter",
            "label": "Counter-Trend ⚠",
            "color": "caution",
            "guidance": f"This {direction} setup is counter to the {trend_dir} SPX trend. Require A+ quality and tighter risk.",
        }
    else:
        return {
            "alignment": "unknown",
            "label": "Trend N/A",
            "color": "neutral",
            "guidance": "Trend alignment unknown — proceed based on pattern quality.",
        }


# ---------------------------------------------------------------------------
# Universe Scanning
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    """Results from a full universe scan."""
    as_of_date: str
    scanned_count: int
    setups_found: int
    a_plus: List[Dict[str, Any]]      # Score >= 75
    standard: List[Dict[str, Any]]    # Score 50-74
    below_threshold: List[Dict[str, Any]]  # Score < 50
    errors: List[str]
    scan_duration_ms: int
    market_gamma: Optional[Dict[str, Any]] = None  # SPX gamma context
    market_trend: Optional[Dict[str, Any]] = None  # SPX 21 EMA trend context
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "asOfDate": self.as_of_date,
            "scannedCount": self.scanned_count,
            "setupsFound": self.setups_found,
            "aPlus": self.a_plus,
            "standard": self.standard,
            "belowThreshold": self.below_threshold,
            "watchlist": sorted(
                self.a_plus + self.standard,
                key=lambda x: x.get("quality", {}).get("score", 0),
                reverse=True,
            ),
            "marketGamma": self.market_gamma,
            "marketTrend": self.market_trend,
            "meta": {
                "scanDurationMs": self.scan_duration_ms,
                "errorCount": len(self.errors),
            },
        }


def compute_engine3_scan(
    client: OratsClient,
    *,
    as_of_date: Optional[str] = None,
    min_score: int = 50,
    direction: Optional[str] = None,  # "bullish", "bearish", or None for both
    max_workers: int = 10,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """
    Scan the full universe for Red Dog setups.
    
    Args:
        client: ORATS client
        as_of_date: Date to scan (default: today)
        min_score: Minimum score to include in results (default: 50)
        direction: Filter by direction ("bullish", "bearish", or None)
        max_workers: Parallel workers for scanning
        use_cache: Whether to use cached results
    
    Returns:
        ScanResult as dict
    """
    # Parse date
    if as_of_date:
        try:
            scan_date = dt.date.fromisoformat(str(as_of_date)[:10])
        except ValueError:
            scan_date = dt.date.today()
    else:
        scan_date = dt.date.today()
    
    as_of_str = scan_date.isoformat()
    
    # Check cache
    if use_cache:
        cache_key = _cache_key_scan(as_of_str, min_score, direction)
        with _scan_cache_lock:
            cached = _scan_cache.get(cache_key)
            if cached is not None:
                LOG.info(f"Engine 3 scan cache hit for {as_of_str}")
                return cached
    
    start_time = time.time()
    
    # Fetch SPX gamma context first (lightweight, informs trading)
    # Pass scan_date so EOD fallback uses correct date
    market_gamma = fetch_spx_gamma_context(client, as_of_date=scan_date)
    LOG.info(f"Engine 3 SPX gamma: {market_gamma.get('netGammaSign', 'unknown')} ({market_gamma.get('environment', 'unknown')}) [source: {market_gamma.get('dataSource', 'unknown')}]")
    
    # Fetch SPX 21 EMA trend context
    market_trend = fetch_spx_trend_context(client, as_of_date=scan_date)
    LOG.info(f"Engine 3 SPX trend: {market_trend.get('trendDirection', 'unknown')} (EMA21: {market_trend.get('ema21', 'N/A')}) [source: {market_trend.get('dataSource', 'unknown')}]")
    
    # Load universe
    universe = load_universe_sp500_and_nasdaq100()
    LOG.info(f"Engine 3 scanning {len(universe)} tickers for {as_of_str}")
    
    # Scan in parallel
    signals: List[RedDogSignal] = []
    errors: List[str] = []
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(scan_ticker, client, ticker=ticker, as_of_date=scan_date): ticker
            for ticker in universe
        }
        
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                signal = future.result()
                if signal is not None:
                    # Apply direction filter
                    if direction:
                        if direction.lower() != signal.direction:
                            continue
                    
                    # Apply score filter
                    if signal.score >= min_score:
                        signals.append(signal)
                        
            except Exception as e:
                errors.append(f"{ticker}: {str(e)}")
    
    # Sort by score descending
    signals.sort(key=lambda s: s.score, reverse=True)
    
    # Check if gamma is supportive (positive gamma = supportive for mean reversion)
    gamma_supportive = market_gamma.get("environment") == "supportive"
    
    # Convert signals to dicts and add trend alignment
    def enrich_signal(s: RedDogSignal) -> Dict[str, Any]:
        d = signal_to_dict(s)
        d["trendAlignment"] = get_signal_trend_alignment(s.direction, market_trend)
        return d
    
    def is_aligned(s: RedDogSignal) -> bool:
        """Check if signal is fully aligned (gamma + trend)."""
        if not gamma_supportive:
            return False
        trend_info = get_signal_trend_alignment(s.direction, market_trend)
        return trend_info.get("alignment") == "aligned"
    
    # Categorize - ONLY include aligned signals
    a_plus = [enrich_signal(s) for s in signals if s.score >= APLUS_THRESHOLD and is_aligned(s)]
    standard = [enrich_signal(s) for s in signals if 50 <= s.score < APLUS_THRESHOLD and is_aligned(s)]
    below_threshold = [enrich_signal(s) for s in signals if s.score < 50 and is_aligned(s)]
    
    duration_ms = int((time.time() - start_time) * 1000)
    
    total_aligned = len(a_plus) + len(standard) + len(below_threshold)
    
    result = ScanResult(
        as_of_date=as_of_str,
        scanned_count=len(universe),
        setups_found=total_aligned,  # Only count aligned setups
        a_plus=a_plus,
        standard=standard,
        below_threshold=below_threshold,
        errors=errors[:10],  # Limit error reporting
        scan_duration_ms=duration_ms,
        market_gamma=market_gamma,
        market_trend=market_trend,
    )
    
    result_dict = result.to_dict()
    
    # Cache result
    if use_cache:
        cache_key = _cache_key_scan(as_of_str, min_score, direction)
        with _scan_cache_lock:
            _scan_cache[cache_key] = result_dict
    
    LOG.info(
        f"Engine 3 scan complete: {len(signals)} raw setups, {total_aligned} aligned "
        f"({len(a_plus)} A+, {len(standard)} standard) in {duration_ms}ms"
    )
    
    return result_dict


def compute_single_ticker_scan(
    client: OratsClient,
    *,
    ticker: str,
    as_of_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Scan a single ticker for Red Dog setup with full details.
    """
    # Parse date
    if as_of_date:
        try:
            scan_date = dt.date.fromisoformat(str(as_of_date)[:10])
        except ValueError:
            scan_date = dt.date.today()
    else:
        scan_date = dt.date.today()
    
    ticker = ticker.upper().strip()
    
    try:
        bars = fetch_bars_for_ticker(
            client,
            ticker=ticker,
            as_of_date=scan_date,
            lookback_days=60,
        )
        
        if not bars or len(bars) < 21:
            return {
                "ticker": ticker,
                "asOfDate": scan_date.isoformat(),
                "enabled": False,
                "signal": None,
                "notes": ["Insufficient bar history."],
            }
        
        detection = detect_red_dog_enhanced(bars, ticker=ticker)
        signal = build_red_dog_signal(
            ticker=ticker,
            detection=detection,
            near_support_resistance=False,
        )
        
        return {
            "ticker": ticker,
            "asOfDate": scan_date.isoformat(),
            "enabled": True,
            "hasSignal": signal is not None,
            "signal": signal_to_dict(signal) if signal else None,
            "indicators": detection.get("indicators", {}),
            "notes": detection.get("notes", []),
        }
        
    except Exception as e:
        LOG.error(f"Error scanning {ticker}: {e}")
        return {
            "ticker": ticker,
            "asOfDate": scan_date.isoformat(),
            "enabled": False,
            "signal": None,
            "notes": [f"Error: {str(e)}"],
        }


# ---------------------------------------------------------------------------
# Watchlist Generation
# ---------------------------------------------------------------------------

def generate_watchlist(
    scan_result: Dict[str, Any],
    *,
    max_items: int = 20,
    include_standard: bool = True,
) -> List[Dict[str, Any]]:
    """
    Generate a prioritized watchlist from scan results.
    
    Priority order:
    1. A+ bullish (high score first)
    2. A+ bearish (high score first)
    3. Standard (if included)
    """
    watchlist: List[Dict[str, Any]] = []
    
    a_plus = scan_result.get("aPlus", [])
    standard = scan_result.get("standard", []) if include_standard else []
    
    # Combine and sort by score
    all_signals = a_plus + standard
    all_signals.sort(key=lambda x: x.get("quality", {}).get("score", 0), reverse=True)
    
    return all_signals[:max_items]


def format_watchlist_summary(watchlist: List[Dict[str, Any]]) -> str:
    """
    Format watchlist for display/logging.
    """
    if not watchlist:
        return "No Red Dog setups found."
    
    lines = ["Red Dog Reversal Watchlist", "=" * 40]
    
    for item in watchlist:
        ticker = item.get("ticker", "???")
        direction = item.get("direction", "?")[0].upper()
        score = item.get("quality", {}).get("score", 0)
        grade = item.get("quality", {}).get("grade", "?")
        entry = item.get("levels", {}).get("entryTrigger", 0)
        stop = item.get("levels", {}).get("stopLoss", 0)
        
        lines.append(
            f"{ticker:6} {direction} | Score: {score:3} ({grade:2}) | "
            f"Entry: ${entry:.2f} | Stop: ${stop:.2f}"
        )
    
    return "\n".join(lines)
