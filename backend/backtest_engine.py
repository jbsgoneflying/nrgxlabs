"""
Backtesting Engine for Engine 3 (Red Dog) and Engine 4 (Ichimoku)

Evaluates historical A+ actionable signals with:
- Next-day entry (signal discarded if entry not triggered)
- Exit at stop loss or target
- Tracking of gamma and trend alignment for segmented results
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

from backend.orats_client import OratsClient
from backend.technicals import DailyBar, fetch_daily_bars_range
from backend.universe import load_universe_sp500_and_nasdaq100
from backend.dealer_gamma_context import compute_dealer_gamma_context

# Engine-specific imports
from backend.engine3_red_dog import (
    APLUS_THRESHOLD as E3_APLUS_THRESHOLD,
    detect_red_dog_enhanced,
    build_red_dog_signal,
    RedDogSignal,
)
from backend.engine4_ichimoku import (
    APLUS_THRESHOLD as E4_APLUS_THRESHOLD,
    detect_ichimoku_setup,
    build_ichimoku_signal,
    IchimokuSignal,
)

LOG = logging.getLogger("backtest_engine")


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    """Record of a single backtest trade."""
    ticker: str
    signal_date: str
    direction: str  # "bullish" or "bearish"
    engine: str  # "engine3" or "engine4"
    
    # Signal levels
    entry_price: float
    stop_price: float
    target_price: float
    
    # Trade execution
    entry_date: Optional[str] = None
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: str = "pending"  # "target", "stop", "not_triggered"
    
    # P/L metrics
    pl_dollars: float = 0.0
    pl_pct: float = 0.0
    r_multiple: float = 0.0
    is_win: bool = False
    
    # Context at signal time
    gamma_supportive: bool = False
    trend_aligned: bool = False
    score: int = 0
    grade: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "signalDate": self.signal_date,
            "direction": self.direction,
            "engine": self.engine,
            "levels": {
                "entry": self.entry_price,
                "stop": self.stop_price,
                "target": self.target_price,
            },
            "execution": {
                "entryDate": self.entry_date,
                "exitDate": self.exit_date,
                "exitPrice": self.exit_price,
                "exitReason": self.exit_reason,
            },
            "performance": {
                "plDollars": round(self.pl_dollars, 2),
                "plPct": round(self.pl_pct, 2),
                "rMultiple": round(self.r_multiple, 2),
                "isWin": self.is_win,
            },
            "context": {
                "gammaSupportive": self.gamma_supportive,
                "trendAligned": self.trend_aligned,
                "score": self.score,
                "grade": self.grade,
            },
        }


@dataclass
class BacktestResult:
    """Aggregated backtest results."""
    engine: str
    trade_count: int
    date_range: Tuple[str, str]
    
    # Overall stats
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    not_triggered: int = 0
    win_rate: float = 0.0
    total_pl_pct: float = 0.0
    avg_r_multiple: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    
    # Segmented by alignment (gamma + trend both aligned)
    aligned_trades: int = 0
    aligned_wins: int = 0
    aligned_win_rate: float = 0.0
    aligned_pl_pct: float = 0.0
    aligned_avg_r: float = 0.0
    
    # Segmented by unaligned
    unaligned_trades: int = 0
    unaligned_wins: int = 0
    unaligned_win_rate: float = 0.0
    unaligned_pl_pct: float = 0.0
    unaligned_avg_r: float = 0.0
    
    # Trade log
    trades: List[TradeRecord] = field(default_factory=list)
    
    # Metadata
    scan_duration_ms: int = 0
    days_scanned: int = 0
    signals_found: int = 0
    api_calls_estimate: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "engine": self.engine,
            "requestedTrades": self.trade_count,
            "dateRange": {"start": self.date_range[0], "end": self.date_range[1]},
            "overall": {
                "totalTrades": self.total_trades,
                "wins": self.wins,
                "losses": self.losses,
                "notTriggered": self.not_triggered,
                "winRate": round(self.win_rate * 100, 1),
                "totalPlPct": round(self.total_pl_pct, 2),
                "avgRMultiple": round(self.avg_r_multiple, 2),
                "avgWinR": round(self.avg_win_r, 2),
                "avgLossR": round(self.avg_loss_r, 2),
            },
            "aligned": {
                "trades": self.aligned_trades,
                "wins": self.aligned_wins,
                "winRate": round(self.aligned_win_rate * 100, 1),
                "plPct": round(self.aligned_pl_pct, 2),
                "avgR": round(self.aligned_avg_r, 2),
            },
            "unaligned": {
                "trades": self.unaligned_trades,
                "wins": self.unaligned_wins,
                "winRate": round(self.unaligned_win_rate * 100, 1),
                "plPct": round(self.unaligned_pl_pct, 2),
                "avgR": round(self.unaligned_avg_r, 2),
            },
            "trades": [t.to_dict() for t in self.trades],
            "meta": {
                "scanDurationMs": self.scan_duration_ms,
                "daysScanned": self.days_scanned,
                "signalsFound": self.signals_found,
                "apiCallsEstimate": self.api_calls_estimate,
            },
        }


# ---------------------------------------------------------------------------
# Historical Data Fetching (Batch Optimized)
# ---------------------------------------------------------------------------

def fetch_historical_bars_batch(
    client: OratsClient,
    tickers: List[str],
    start_date: dt.date,
    end_date: dt.date,
    max_workers: int = 10,
) -> Dict[str, List[DailyBar]]:
    """
    Batch fetch daily bars for all tickers in date range.
    Uses range queries to minimize API calls.
    """
    result: Dict[str, List[DailyBar]] = {}
    errors: List[str] = []
    
    def fetch_single(ticker: str) -> Tuple[str, List[DailyBar]]:
        try:
            bars = fetch_daily_bars_range(
                client,
                ticker=ticker,
                start=start_date,
                end=end_date,
            )
            return (ticker, bars)
        except Exception as e:
            LOG.warning(f"Failed to fetch bars for {ticker}: {e}")
            return (ticker, [])
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_single, t): t for t in tickers}
        
        for future in as_completed(futures):
            ticker, bars = future.result()
            if bars:
                result[ticker] = bars
    
    LOG.info(f"Fetched bars for {len(result)}/{len(tickers)} tickers")
    return result


def fetch_historical_gamma_context(
    client: OratsClient,
    trade_date: dt.date,
) -> Dict[str, Any]:
    """
    Fetch historical SPX gamma context for a specific date.
    Returns simplified supportive/challenging context.
    """
    try:
        # Fields needed for gamma calculation
        fields = "ticker,tradeDate,expirDate,strike,spotPrice,stockPrice,gamma,callOpenInterest,putOpenInterest"
        
        # Find strikes for this date
        for symbol in ("SPX", "SPXW"):
            try:
                resp = client.hist_strikes(
                    ticker=symbol,
                    trade_date=trade_date.isoformat(),
                    fields=fields,
                    dte="3,21",
                )
                rows = resp.rows or []
                if rows and len(rows) > 10:
                    # Compute gamma context
                    gamma_ctx = compute_dealer_gamma_context(
                        rows,
                        expiry=rows[0].get("expirDate", "")[:10] if rows else None,
                        band_pct=0.03,
                        top_n=5,
                    )
                    net_sign = gamma_ctx.get("netGammaSign", "unknown")
                    return {
                        "available": True,
                        "netGammaSign": net_sign,
                        "supportive": net_sign == "positive",
                    }
            except Exception:
                continue
        
        return {"available": False, "netGammaSign": "unknown", "supportive": False}
    except Exception as e:
        LOG.warning(f"Failed to fetch gamma context for {trade_date}: {e}")
        return {"available": False, "netGammaSign": "unknown", "supportive": False}


def compute_ema(values: List[float], period: int) -> Optional[float]:
    """Compute Exponential Moving Average."""
    if len(values) < period:
        return None
    
    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    
    for price in values[period:]:
        ema = (price - ema) * multiplier + ema
    
    return ema


def get_trend_alignment(
    direction: str,
    spy_bars: List[DailyBar],
    as_of_date: dt.date,
) -> Tuple[bool, str]:
    """
    Check if signal direction is aligned with SPY 21 EMA trend.
    Returns (is_aligned, trend_direction).
    """
    # Filter bars up to as_of_date
    valid_bars = [b for b in spy_bars if b.trade_date and b.trade_date <= as_of_date.isoformat()]
    
    if len(valid_bars) < 25:
        return (False, "unknown")
    
    closes = [float(b.close) for b in valid_bars if b.close and b.close > 0]
    
    if len(closes) < 25:
        return (False, "unknown")
    
    ema_21 = compute_ema(closes, 21)
    current_price = closes[-1]
    
    if ema_21 is None:
        return (False, "unknown")
    
    trend_dir = "bullish" if current_price > ema_21 else "bearish"
    is_aligned = direction == trend_dir
    
    return (is_aligned, trend_dir)


# ---------------------------------------------------------------------------
# Signal Detection for Historical Dates
# ---------------------------------------------------------------------------

def detect_signals_for_date_engine3(
    all_bars: Dict[str, List[DailyBar]],
    as_of_date: dt.date,
    min_lookback: int = 21,
) -> List[Tuple[str, RedDogSignal]]:
    """
    Detect Engine 3 Red Dog signals for a specific date using cached bars.
    Returns list of (ticker, signal) tuples.
    """
    signals: List[Tuple[str, RedDogSignal]] = []
    as_of_str = as_of_date.isoformat()
    
    for ticker, bars in all_bars.items():
        # Filter bars up to as_of_date
        valid_bars = [b for b in bars if b.trade_date and b.trade_date <= as_of_str]
        
        if len(valid_bars) < min_lookback:
            continue
        
        try:
            detection = detect_red_dog_enhanced(valid_bars, ticker=ticker)
            
            if not detection.get("bullish") and not detection.get("bearish"):
                continue
            
            signal = build_red_dog_signal(
                ticker=ticker,
                detection=detection,
                near_support_resistance=False,
            )
            
            if signal and signal.score >= E3_APLUS_THRESHOLD:
                signals.append((ticker, signal))
                
        except Exception as e:
            LOG.debug(f"Error detecting E3 signal for {ticker}: {e}")
    
    return signals


def detect_signals_for_date_engine4(
    all_bars: Dict[str, List[DailyBar]],
    as_of_date: dt.date,
    min_lookback: int = 60,
) -> List[Tuple[str, IchimokuSignal]]:
    """
    Detect Engine 4 Ichimoku signals for a specific date using cached bars.
    Returns list of (ticker, signal) tuples for A+ actionable signals only.
    """
    signals: List[Tuple[str, IchimokuSignal]] = []
    as_of_str = as_of_date.isoformat()
    
    for ticker, bars in all_bars.items():
        # Filter bars up to as_of_date
        valid_bars = [b for b in bars if b.trade_date and b.trade_date <= as_of_str]
        
        if len(valid_bars) < min_lookback:
            continue
        
        try:
            detection = detect_ichimoku_setup(valid_bars, ticker=ticker)
            
            if not detection.get("hasSetup"):
                continue
            
            signal = build_ichimoku_signal(
                ticker=ticker,
                detection=detection,
            )
            
            # Only A+ actionable signals
            if signal and signal.score >= E4_APLUS_THRESHOLD and signal.freshness_bucket == "actionable":
                signals.append((ticker, signal))
                
        except Exception as e:
            LOG.debug(f"Error detecting E4 signal for {ticker}: {e}")
    
    return signals


# ---------------------------------------------------------------------------
# Trade Simulation
# ---------------------------------------------------------------------------

def simulate_trade_engine3(
    signal: RedDogSignal,
    all_bars: Dict[str, List[DailyBar]],
    signal_date: dt.date,
    gamma_supportive: bool,
    trend_aligned: bool,
) -> TradeRecord:
    """
    Simulate an Engine 3 trade.
    - Entry: next day if trigger hit
    - Exit: at stop or target (using targetSma20 for mean reversion)
    """
    ticker = signal.ticker
    bars = all_bars.get(ticker, [])
    
    # Get bars after signal date
    future_bars = [
        b for b in bars 
        if b.trade_date and b.trade_date > signal_date.isoformat()
    ]
    
    trade = TradeRecord(
        ticker=ticker,
        signal_date=signal.signal_date,
        direction=signal.direction,
        engine="engine3",
        entry_price=signal.entry_trigger,
        stop_price=signal.stop_loss,
        target_price=signal.target_sma20,  # Use SMA20 target for mean reversion
        gamma_supportive=gamma_supportive,
        trend_aligned=trend_aligned,
        score=signal.score,
        grade=signal.grade,
    )
    
    if not future_bars:
        trade.exit_reason = "no_data"
        return trade
    
    # Day 1: Check if entry trigger hit
    next_day = future_bars[0]
    is_bullish = signal.direction == "bullish"
    
    if is_bullish:
        # Buy stop: need high >= entry
        if next_day.high is None or float(next_day.high) < signal.entry_trigger:
            trade.exit_reason = "not_triggered"
            return trade
        trade.entry_date = next_day.trade_date
    else:
        # Sell stop: need low <= entry
        if next_day.low is None or float(next_day.low) > signal.entry_trigger:
            trade.exit_reason = "not_triggered"
            return trade
        trade.entry_date = next_day.trade_date
    
    # Simulate from day after entry
    for bar in future_bars[1:]:
        if bar.high is None or bar.low is None or bar.close is None:
            continue
        
        h, l, c = float(bar.high), float(bar.low), float(bar.close)
        
        if is_bullish:
            # Check stop first (worst case)
            if l <= signal.stop_loss:
                trade.exit_date = bar.trade_date
                trade.exit_price = signal.stop_loss
                trade.exit_reason = "stop"
                break
            # Check target
            if h >= signal.target_sma20:
                trade.exit_date = bar.trade_date
                trade.exit_price = signal.target_sma20
                trade.exit_reason = "target"
                break
        else:
            # Check stop first
            if h >= signal.stop_loss:
                trade.exit_date = bar.trade_date
                trade.exit_price = signal.stop_loss
                trade.exit_reason = "stop"
                break
            # Check target
            if l <= signal.target_sma20:
                trade.exit_date = bar.trade_date
                trade.exit_price = signal.target_sma20
                trade.exit_reason = "target"
                break
    
    # Calculate P/L if trade was executed
    if trade.exit_price is not None:
        if is_bullish:
            trade.pl_dollars = trade.exit_price - signal.entry_trigger
        else:
            trade.pl_dollars = signal.entry_trigger - trade.exit_price
        
        trade.pl_pct = (trade.pl_dollars / signal.entry_trigger) * 100
        risk = abs(signal.entry_trigger - signal.stop_loss)
        trade.r_multiple = trade.pl_dollars / risk if risk > 0 else 0
        trade.is_win = trade.pl_dollars > 0
    
    return trade


def simulate_trade_engine4(
    signal: IchimokuSignal,
    all_bars: Dict[str, List[DailyBar]],
    signal_date: dt.date,
    gamma_supportive: bool,
    trend_aligned: bool,
) -> TradeRecord:
    """
    Simulate an Engine 4 trade.
    - Entry: next day if trigger hit
    - Exit: at stop or target_1
    """
    ticker = signal.ticker
    bars = all_bars.get(ticker, [])
    
    # Get bars after signal date
    future_bars = [
        b for b in bars 
        if b.trade_date and b.trade_date > signal_date.isoformat()
    ]
    
    trade = TradeRecord(
        ticker=ticker,
        signal_date=signal.signal_date,
        direction=signal.direction,
        engine="engine4",
        entry_price=signal.entry_trigger,
        stop_price=signal.stop_loss,
        target_price=signal.target_1,
        gamma_supportive=gamma_supportive,
        trend_aligned=trend_aligned,
        score=signal.score,
        grade=signal.grade,
    )
    
    if not future_bars:
        trade.exit_reason = "no_data"
        return trade
    
    # Day 1: Check if entry trigger hit
    next_day = future_bars[0]
    is_bullish = signal.direction == "bullish"
    
    if is_bullish:
        if next_day.high is None or float(next_day.high) < signal.entry_trigger:
            trade.exit_reason = "not_triggered"
            return trade
        trade.entry_date = next_day.trade_date
    else:
        if next_day.low is None or float(next_day.low) > signal.entry_trigger:
            trade.exit_reason = "not_triggered"
            return trade
        trade.entry_date = next_day.trade_date
    
    # Simulate from day after entry
    for bar in future_bars[1:]:
        if bar.high is None or bar.low is None or bar.close is None:
            continue
        
        h, l, c = float(bar.high), float(bar.low), float(bar.close)
        
        if is_bullish:
            if l <= signal.stop_loss:
                trade.exit_date = bar.trade_date
                trade.exit_price = signal.stop_loss
                trade.exit_reason = "stop"
                break
            if h >= signal.target_1:
                trade.exit_date = bar.trade_date
                trade.exit_price = signal.target_1
                trade.exit_reason = "target"
                break
        else:
            if h >= signal.stop_loss:
                trade.exit_date = bar.trade_date
                trade.exit_price = signal.stop_loss
                trade.exit_reason = "stop"
                break
            if l <= signal.target_1:
                trade.exit_date = bar.trade_date
                trade.exit_price = signal.target_1
                trade.exit_reason = "target"
                break
    
    # Calculate P/L
    if trade.exit_price is not None:
        if is_bullish:
            trade.pl_dollars = trade.exit_price - signal.entry_trigger
        else:
            trade.pl_dollars = signal.entry_trigger - trade.exit_price
        
        trade.pl_pct = (trade.pl_dollars / signal.entry_trigger) * 100
        risk = abs(signal.entry_trigger - signal.stop_loss)
        trade.r_multiple = trade.pl_dollars / risk if risk > 0 else 0
        trade.is_win = trade.pl_dollars > 0
    
    return trade


# ---------------------------------------------------------------------------
# Main Backtest Function
# ---------------------------------------------------------------------------

def run_backtest(
    client: OratsClient,
    *,
    engine: Literal["engine3", "engine4"] = "engine3",
    trade_count: int = 50,
    start_date: Optional[str] = None,
    max_workers: int = 10,
) -> BacktestResult:
    """
    Run backtest for Engine 3 or Engine 4.
    
    Args:
        client: ORATS client
        engine: "engine3" (Red Dog) or "engine4" (Ichimoku)
        trade_count: Target number of trades (25, 50, 100, 200)
        start_date: Start date for lookback (defaults to today)
        max_workers: Parallel workers for data fetching
    
    Returns:
        BacktestResult with performance metrics and trade log
    """
    start_time = time.time()
    
    # Parse start date
    if start_date:
        try:
            end_date = dt.date.fromisoformat(str(start_date)[:10])
        except ValueError:
            end_date = dt.date.today()
    else:
        end_date = dt.date.today()
    
    # Estimate how far back we need to scan
    # Conservative: assume ~2 A+ signals per day, need 2x for entry filtering
    est_days = max(trade_count * 2, 30)
    lookback_days = min(est_days + 60, 365)  # Cap at 1 year, add buffer for bars
    
    scan_start = end_date - dt.timedelta(days=lookback_days)
    
    LOG.info(f"Running {engine} backtest: {trade_count} trades, {scan_start} to {end_date}")
    
    # Load universe
    universe = load_universe_sp500_and_nasdaq100()
    
    # Batch fetch all historical data (optimized - single API call per ticker)
    LOG.info(f"Fetching historical bars for {len(universe)} tickers...")
    all_bars = fetch_historical_bars_batch(
        client,
        tickers=universe,
        start_date=scan_start,
        end_date=end_date,
        max_workers=max_workers,
    )
    
    # Fetch SPY bars for trend alignment
    spy_bars = fetch_daily_bars_range(client, ticker="SPY", start=scan_start, end=end_date)
    
    # Walk back day by day collecting trades
    trades: List[TradeRecord] = []
    signals_found = 0
    days_scanned = 0
    gamma_cache: Dict[str, Dict[str, Any]] = {}
    
    current_date = end_date - dt.timedelta(days=1)  # Start from yesterday (need future bars)
    
    while len(trades) < trade_count and current_date >= scan_start:
        # Skip weekends
        if current_date.weekday() >= 5:
            current_date -= dt.timedelta(days=1)
            continue
        
        days_scanned += 1
        
        # Fetch gamma context for this date (with caching)
        date_str = current_date.isoformat()
        if date_str not in gamma_cache:
            gamma_cache[date_str] = fetch_historical_gamma_context(client, current_date)
        gamma_ctx = gamma_cache[date_str]
        gamma_supportive = gamma_ctx.get("supportive", False)
        
        # Detect signals for this date
        if engine == "engine3":
            signal_tuples = detect_signals_for_date_engine3(all_bars, current_date)
        else:
            signal_tuples = detect_signals_for_date_engine4(all_bars, current_date)
        
        signals_found += len(signal_tuples)
        
        # Simulate trades
        for ticker, signal in signal_tuples:
            if len(trades) >= trade_count:
                break
            
            # Get trend alignment
            direction = signal.direction
            trend_aligned, _ = get_trend_alignment(direction, spy_bars, current_date)
            
            # Simulate trade
            if engine == "engine3":
                trade = simulate_trade_engine3(
                    signal, all_bars, current_date, gamma_supportive, trend_aligned
                )
            else:
                trade = simulate_trade_engine4(
                    signal, all_bars, current_date, gamma_supportive, trend_aligned
                )
            
            # Only count executed trades (not_triggered doesn't count toward target)
            if trade.exit_reason not in ("not_triggered", "no_data"):
                trades.append(trade)
        
        current_date -= dt.timedelta(days=1)
    
    # Calculate aggregate statistics
    result = BacktestResult(
        engine=engine,
        trade_count=trade_count,
        date_range=(current_date.isoformat(), end_date.isoformat()),
        trades=trades,
        days_scanned=days_scanned,
        signals_found=signals_found,
        api_calls_estimate=len(universe) + days_scanned + 1,  # tickers + gamma + SPY
    )
    
    # Count executed trades
    executed = [t for t in trades if t.exit_reason in ("target", "stop")]
    not_triggered = [t for t in trades if t.exit_reason == "not_triggered"]
    
    result.total_trades = len(executed)
    result.wins = sum(1 for t in executed if t.is_win)
    result.losses = sum(1 for t in executed if not t.is_win)
    result.not_triggered = len(not_triggered)
    
    if result.total_trades > 0:
        result.win_rate = result.wins / result.total_trades
        result.total_pl_pct = sum(t.pl_pct for t in executed)
        result.avg_r_multiple = sum(t.r_multiple for t in executed) / result.total_trades
        
        wins = [t for t in executed if t.is_win]
        losses = [t for t in executed if not t.is_win]
        
        if wins:
            result.avg_win_r = sum(t.r_multiple for t in wins) / len(wins)
        if losses:
            result.avg_loss_r = sum(t.r_multiple for t in losses) / len(losses)
    
    # Segmented by alignment (both gamma AND trend aligned)
    aligned = [t for t in executed if t.gamma_supportive and t.trend_aligned]
    unaligned = [t for t in executed if not (t.gamma_supportive and t.trend_aligned)]
    
    result.aligned_trades = len(aligned)
    result.aligned_wins = sum(1 for t in aligned if t.is_win)
    if aligned:
        result.aligned_win_rate = result.aligned_wins / len(aligned)
        result.aligned_pl_pct = sum(t.pl_pct for t in aligned)
        result.aligned_avg_r = sum(t.r_multiple for t in aligned) / len(aligned)
    
    result.unaligned_trades = len(unaligned)
    result.unaligned_wins = sum(1 for t in unaligned if t.is_win)
    if unaligned:
        result.unaligned_win_rate = result.unaligned_wins / len(unaligned)
        result.unaligned_pl_pct = sum(t.pl_pct for t in unaligned)
        result.unaligned_avg_r = sum(t.r_multiple for t in unaligned) / len(unaligned)
    
    result.scan_duration_ms = int((time.time() - start_time) * 1000)
    
    LOG.info(
        f"Backtest complete: {result.total_trades} trades, "
        f"{result.win_rate*100:.1f}% win rate, "
        f"{result.avg_r_multiple:.2f}R avg in {result.scan_duration_ms}ms"
    )
    
    return result
