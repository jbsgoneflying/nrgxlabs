"""
Engine 4: Ichimoku Cloud Continuation Trading System

An EOD screener for Ichimoku-based continuation entries (Kijun pullback + Tenkan reclaim)
with A+ quality scoring for the SP500 + Nasdaq100 universe.

Based on standard Ichimoku settings (9/26/52) with additional filters for:
- Trend qualification (price vs cloud, forward cloud bias, Kijun slope)
- Pullback definition (past Tenkan, near Kijun, no deep cloud penetration)
- Entry triggers (Tenkan reclaim with candle quality)
- Dealer gamma regime context
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.technicals import (
    DailyBar,
    compute_ichimoku_series,
    compute_rsi_series,
    compute_volume_metrics,
    compute_atr_series,
)


# ---------------------------------------------------------------------------
# Constants & Scoring Weights
# ---------------------------------------------------------------------------

# A+ threshold: setups scoring >= 75 are considered high-quality
APLUS_THRESHOLD = 75

# Scoring weights (total possible = 100).
#
# 2026-06 top-down stack: dealer gamma was REMOVED from the quality score
# (it now lives solely in the gate + verdict — see backend/gating.py) to fix a
# double-count, and the freed 15 points were reallocated to two top-down
# context signals that actually move single-name hit-rate: sector-trend
# alignment and relative strength vs the name's index. Total still sums to 100.
SCORE_CHIKOU_CLEAN = 15          # Chikou not tangled in prior candles
SCORE_VOLUME_SURGE = 15          # Trigger volume > 1.25x 20D avg
SCORE_CANDLE_STRENGTH = 15       # Close in top/bottom 33% of range
SCORE_KIJUN_SLOPE = 10           # Kijun slope supportive of direction
SCORE_RSI_RECOVERY = 10          # RSI > 50 (bull) or < 50 (bear)
SCORE_CLOUD_BIAS = 10            # Forward cloud aligned with direction
SCORE_CLOUD_THICKNESS = 10       # Cloud thickness in reasonable range
SCORE_SECTOR_ALIGN = 8           # Name's sector ETF cloud agrees with setup
SCORE_REL_STRENGTH = 7           # Name leads (bull) / lags (bear) its index

# Legacy alias kept so external callers referencing the old constant don't
# break. Gamma no longer contributes to the quality score.
SCORE_GAMMA_SUPPORTIVE = 0

# Downgrade penalties
PENALTY_TIME_IN_CLOUD = -15      # > 10 of 20 closes inside cloud
PENALTY_KIJUN_FLAT = -10         # Kijun flat > 5 days
PENALTY_LOW_VOLUME = -10         # Trigger volume < 1.0x avg
PENALTY_EARNINGS_SOON = -25      # Earnings within 5 sessions
PENALTY_GAMMA_MISMATCH = 0       # Retired: gamma is a gate input, not a score penalty

# Thresholds
VOLUME_SURGE_THRESHOLD = 1.25    # 1.25x average volume for surge
VOLUME_LOW_THRESHOLD = 1.0       # Below this is penalized
CANDLE_STRENGTH_BULLISH = 0.67   # Close in top 33% of range
CANDLE_STRENGTH_BEARISH = 0.33   # Close in bottom 33% of range
CLOUD_PENETRATION_DEEP = 0.35    # > 35% penetration is "deep"
TIME_IN_CLOUD_THRESHOLD = 10     # > 10 of 20 days = choppy
KIJUN_FLAT_THRESHOLD = 5         # > 5 days flat is concerning
ATR_BUFFER_MULT = 0.25           # 0.25x ATR buffer for stops

# Freshness classification thresholds (for Actionable vs Structure buckets)
FRESHNESS_RECLAIM_MAX_BARS = 3       # Max bars since Tenkan reclaim for actionable
FRESHNESS_KIJUN_DISTANCE_ATR = 1.5   # Max distance to Kijun in ATR units for actionable
FRESHNESS_TENKAN_LOOKBACK = 5        # Bars to check for recent Tenkan interaction
IMPULSE_DISPLACEMENT_MULT = 2.5      # TR > 2.5x ATR = impulse bar (hard reject)
TENKAN_PENETRATION_ATR = 0.1         # Tenkan interaction requires 0.1 ATR penetration
TRIGGER_RAN_ATR = 0.75               # If price ran > 0.75 ATR past reclaim bar, downgrade
IMPULSE_RECLAIM_ATR = 1.25           # If reclaim bar range > 1.25 ATR and age=0, downgrade


@dataclass(frozen=True)
class IchimokuSignal:
    """Ichimoku continuation signal with A+ scoring."""
    ticker: str
    signal_date: str
    direction: str  # "bullish" or "bearish"
    
    # Ichimoku state
    tenkan: float
    kijun: float
    chikou: float
    cloud_top: float
    cloud_bottom: float
    cloud_bias: str  # "bullish" or "bearish"
    cloud_thickness: float
    
    # Pattern details
    close: float
    close_position: float  # 0-1, where close is within day's range
    pullback_depth: float  # How far into pullback (distance to Kijun)
    cloud_penetration_pct: float  # 0-100 percentage inside cloud
    
    # Entry/exit levels
    entry_trigger: float   # Buy stop above high (bullish) or sell stop below low (bearish)
    stop_loss: float       # Below Kijun or swing low + ATR buffer
    target_1: float        # Prior swing high/low (1R)
    target_2: float        # 2R target
    trail_level: float     # Kijun for trailing stops
    
    # Risk metrics
    risk_dollars: float    # Entry - Stop
    reward_1r: float       # Target 1 - Entry
    
    # Status tracking
    status: str = "pending"  # "pending", "triggered", "stopped", "target_hit", "invalidated"
    invalidation_reason: Optional[str] = None
    
    # Quality scoring (continuous — see score_ichimoku_setup)
    score: float = 0.0
    grade: str = "C"
    
    # Component scores (for transparency)
    chikou_score: float = 0.0
    volume_score: float = 0.0
    candle_score: float = 0.0
    kijun_slope_score: float = 0.0
    rsi_score: float = 0.0
    cloud_bias_score: float = 0.0
    cloud_thickness_score: float = 0.0
    sector_score: float = 0.0
    rel_strength_score: float = 0.0
    gamma_score: float = 0.0  # retired from scoring; kept at 0 for back-compat
    
    # Penalty scores
    time_in_cloud_penalty: int = 0
    kijun_flat_penalty: int = 0
    low_volume_penalty: int = 0
    earnings_penalty: int = 0
    gamma_mismatch_penalty: int = 0
    
    # Indicator values
    rsi: Optional[float] = None
    volume_ratio: Optional[float] = None
    atr: Optional[float] = None
    kijun_slope: Optional[str] = None  # "positive", "negative", "flat"
    time_in_cloud: Optional[int] = None
    chikou_tangled: Optional[bool] = None
    
    # Metadata
    strength: str = "standard"  # "strong" or "standard"
    tags: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    
    # Index membership for gamma segmentation
    index_membership: str = "sp500"  # "sp500", "nasdaq100", "both"
    
    # Freshness classification (Actionable vs Structure)
    freshness_bucket: str = "actionable"  # "actionable" or "structure"
    freshness_reasons: List[str] = field(default_factory=list)
    bars_since_reclaim: Optional[int] = None
    kijun_distance_atr: Optional[float] = None
    recent_tenkan_touch: Optional[bool] = None
    is_impulse_bar: Optional[bool] = None
    trigger_already_ran: Optional[bool] = None
    trigger_ran_distance_atr: Optional[float] = None
    impulse_reclaim: Optional[bool] = None
    reclaim_bar_range_atr: Optional[float] = None

    # --- Institutional hardening fields (2026-06 sprint) ---
    dollar_adv: Optional[float] = None             # 20d avg dollar volume (liquidity)
    distance_to_actionable: Optional[float] = None  # 0 = actionable; higher = staler structure
    verdict: Optional[Dict[str, Any]] = None        # reconciled desk verdict

    # --- Top-down context stack (2026-06) ---
    sector_etf: Optional[str] = None               # mapped GICS sector ETF (e.g. XLK)
    sector_bias: Optional[str] = None              # sector ETF Ichimoku direction
    index_direction: Optional[str] = None          # SPY/QQQ Ichimoku direction
    rs_ratio: Optional[float] = None               # excess return vs index over RS lookback
    beta: Optional[float] = None                   # daily-return beta to index
    corr: Optional[float] = None                   # daily-return correlation to index


def _ramp(x: Optional[float], lo: float, hi: float) -> float:
    """Linear ramp: 0 at lo, 1 at hi, clamped to [0,1]. Supports lo>hi (descending)."""
    if x is None:
        return 0.0
    if hi == lo:
        return 1.0 if ((x >= hi) if hi >= lo else (x <= hi)) else 0.0
    t = (x - lo) / (hi - lo)
    return max(0.0, min(1.0, t))


def _entry_offset(atr: Optional[float]) -> float:
    """Tick/ATR-scaled trigger offset (a flat $0.01 fills on noise for pricey names)."""
    return max(0.05, round((atr or 0.0) * 0.05, 2))


# Continuous-scoring ramp anchors (bullish framing; bearish mirrors at 100-x / 1-x)
VOL_RAMP_LO, VOL_RAMP_HI = VOLUME_LOW_THRESHOLD, 1.6   # 1.0 → 1.6 for full volume credit
RSI_RAMP_LO, RSI_RAMP_HI = 50.0, 65.0                  # 50 → 65 for full recovery credit
CANDLE_RAMP_LO, CANDLE_RAMP_HI = 0.50, 0.80            # close position within range

# Relative-strength ramp: excess return vs the index over the RS lookback.
# 0% excess → no credit, +8% excess → full credit (bearish mirrors at the
# negative side, i.e. the name underperforming earns the short credit).
RS_RAMP_LO, RS_RAMP_HI = 0.0, 0.08

# Index-trend / context coupling defaults (mirrored in config; kept here so the
# pure functions have sane fallbacks when called without flags).
INDEX_BETA_HARD_DEFAULT = 1.0
INDEX_CORR_HARD_DEFAULT = 0.6
RS_LOOKBACK_DEFAULT = 63
BETA_LOOKBACK_DEFAULT = 60


# ---------------------------------------------------------------------------
# Top-Down Context Helpers (index alignment, relative strength, beta)
# ---------------------------------------------------------------------------

def compute_index_ichimoku_state(
    bars: List[DailyBar],
    *,
    symbol: str = "",
) -> Dict[str, Any]:
    """Reduce a broad-index (SPY/QQQ) or sector-ETF bar series to a one-line
    Ichimoku posture used as a top-down filter.

    Direction is read primarily off price-vs-cloud (the canonical Ichimoku
    trend read): above the cloud = bullish, below = bearish, inside = neutral.
    Tenkan/Kijun cross and the forward cloud bias are surfaced as secondary
    context but do not override the cloud position.
    """
    state: Dict[str, Any] = {
        "symbol": symbol,
        "available": False,
        "direction": "neutral",
        "priceVsCloud": "unknown",
        "tkCross": "neutral",
        "futureBias": None,
        "asOf": None,
    }
    if not bars or len(bars) < 60:
        return state

    ich = compute_ichimoku_series(bars)
    if not ich.get("enabled"):
        return state

    closes = ich.get("closes") or []
    tenkan_series = ich.get("tenkan_series") or []
    kijun_series = ich.get("kijun_series") or []
    cloud_series = ich.get("cloud_series") or []
    span_a_series = ich.get("span_a_series") or []
    span_b_series = ich.get("span_b_series") or []

    if not closes or not cloud_series:
        return state

    close = closes[-1]
    tenkan = tenkan_series[-1] if tenkan_series else None
    kijun = kijun_series[-1] if kijun_series else None
    cloud = cloud_series[-1]
    span_a = span_a_series[-1] if span_a_series else None
    span_b = span_b_series[-1] if span_b_series else None

    if cloud is None or close is None:
        return state

    top = cloud.get("cloudTop")
    bot = cloud.get("cloudBottom")
    if top is None or bot is None:
        return state

    if close > top:
        pvc = "above"
        direction = "bullish"
    elif close < bot:
        pvc = "below"
        direction = "bearish"
    else:
        pvc = "inside"
        direction = "neutral"

    tk = "neutral"
    if tenkan is not None and kijun is not None:
        if tenkan > kijun:
            tk = "bullish"
        elif tenkan < kijun:
            tk = "bearish"

    future_bias = None
    if span_a is not None and span_b is not None:
        future_bias = "bullish" if span_a >= span_b else "bearish"

    state.update({
        "available": True,
        "direction": direction,
        "priceVsCloud": pvc,
        "tkCross": tk,
        "futureBias": future_bias,
        "asOf": bars[-1].trade_date,
        "close": round(float(close), 4),
        "cloudTop": round(float(top), 4),
        "cloudBottom": round(float(bot), 4),
    })
    return state


def _aligned_tail(a: List[float], b: List[float], n: int) -> Tuple[List[float], List[float]]:
    """Tail-align two close series to the same length (min of both, capped n)."""
    m = min(len(a), len(b), n)
    if m <= 0:
        return ([], [])
    return (a[-m:], b[-m:])


def compute_relative_strength(
    name_closes: List[float],
    index_closes: List[float],
    *,
    lookback: int = RS_LOOKBACK_DEFAULT,
) -> Dict[str, Any]:
    """Excess return of the name over its index across the RS lookback.

    rsRatio > 0 → the name is leading its index (a leadership signal for longs);
    rsRatio < 0 → the name is lagging (the signal a short wants).
    """
    out: Dict[str, Any] = {"rsRatio": None, "nameRet": None, "indexRet": None}
    if not name_closes or not index_closes:
        return out

    nc, ic = _aligned_tail(name_closes, index_closes, lookback + 1)
    if len(nc) < 10:
        return out

    n0, n1 = nc[0], nc[-1]
    i0, i1 = ic[0], ic[-1]
    if not n0 or not i0:
        return out

    name_ret = (n1 / n0) - 1.0
    index_ret = (i1 / i0) - 1.0
    out["nameRet"] = round(name_ret, 5)
    out["indexRet"] = round(index_ret, 5)
    out["rsRatio"] = round(name_ret - index_ret, 5)
    return out


def compute_beta_corr(
    name_closes: List[float],
    index_closes: List[float],
    *,
    lookback: int = BETA_LOOKBACK_DEFAULT,
) -> Dict[str, Any]:
    """Daily-return beta and correlation of the name to its index.

    Used to scale how hard the index-trend gate bites: high beta / high
    correlation names are index hostages (mismatch is HARD), low-correlation
    idiosyncratic names earn a softer WATCH.
    """
    out: Dict[str, Any] = {"beta": None, "corr": None}
    if not name_closes or not index_closes:
        return out

    nc, ic = _aligned_tail(name_closes, index_closes, lookback + 1)
    if len(nc) < 20:
        return out

    nr = [nc[i] / nc[i - 1] - 1.0 for i in range(1, len(nc)) if nc[i - 1]]
    ir = [ic[i] / ic[i - 1] - 1.0 for i in range(1, len(ic)) if ic[i - 1]]
    m = min(len(nr), len(ir))
    if m < 15:
        return out
    nr, ir = nr[-m:], ir[-m:]

    mean_n = sum(nr) / m
    mean_i = sum(ir) / m
    cov = sum((nr[i] - mean_n) * (ir[i] - mean_i) for i in range(m)) / m
    var_i = sum((ir[i] - mean_i) ** 2 for i in range(m)) / m
    var_n = sum((nr[i] - mean_n) ** 2 for i in range(m)) / m

    if var_i <= 0:
        return out
    beta = cov / var_i
    out["beta"] = round(beta, 3)
    if var_n > 0:
        out["corr"] = round(cov / math.sqrt(var_n * var_i), 3)
    return out


# ---------------------------------------------------------------------------
# Ichimoku Analysis Functions
# ---------------------------------------------------------------------------

def compute_kijun_slope(
    kijun_series: List[Optional[float]],
    lookback: int = 5,
    flat_threshold: float = 0.001,
) -> Tuple[str, float]:
    """
    Compute Kijun-sen slope direction over lookback period.
    
    Returns:
        (direction, slope_value)
        direction: 'positive', 'negative', or 'flat'
        slope_value: The actual slope as percentage change
    """
    # Get valid values from end of series
    valid_vals = [v for v in kijun_series[-(lookback + 1):] if v is not None]
    
    if len(valid_vals) < 2:
        return ("unknown", 0.0)
    
    if len(valid_vals) < lookback + 1:
        # Use what we have
        start_val = valid_vals[0]
        end_val = valid_vals[-1]
    else:
        start_val = valid_vals[-(lookback + 1)]
        end_val = valid_vals[-1]
    
    if start_val is None or end_val is None or start_val == 0:
        return ("unknown", 0.0)
    
    slope_pct = (end_val - start_val) / start_val
    
    if abs(slope_pct) < flat_threshold:
        return ("flat", slope_pct)
    elif slope_pct > 0:
        return ("positive", slope_pct)
    else:
        return ("negative", slope_pct)


def count_kijun_flat_days(
    kijun_series: List[Optional[float]],
    lookback: int = 20,
    flat_threshold: float = 0.0005,
) -> int:
    """
    Count consecutive days where Kijun has been flat.
    Flat means change < flat_threshold from previous day.
    """
    if not kijun_series or len(kijun_series) < 2:
        return 0
    
    flat_count = 0
    for i in range(len(kijun_series) - 1, max(0, len(kijun_series) - lookback), -1):
        curr = kijun_series[i]
        prev = kijun_series[i - 1]
        
        if curr is None or prev is None or prev == 0:
            break
        
        change_pct = abs(curr - prev) / prev
        if change_pct < flat_threshold:
            flat_count += 1
        else:
            break
    
    return flat_count


def compute_time_in_cloud(
    closes: List[float],
    cloud_series: List[Optional[Dict[str, Any]]],
    lookback: int = 20,
) -> int:
    """
    Count number of closes inside the cloud over lookback period.
    High time-in-cloud indicates choppy/range-bound regime.
    """
    if not closes or not cloud_series:
        return 0
    
    start_idx = max(0, len(closes) - lookback)
    count = 0
    
    for i in range(start_idx, len(closes)):
        if i >= len(cloud_series):
            continue
        
        cloud = cloud_series[i]
        if cloud is None:
            continue
        
        close = closes[i]
        top = cloud.get("cloudTop")
        bot = cloud.get("cloudBottom")
        
        if top is None or bot is None:
            continue
        
        # Inside cloud if between top and bottom
        if bot <= close <= top:
            count += 1
    
    return count


def is_chikou_tangled(
    closes: List[float],
    highs: List[float],
    lows: List[float],
    chikou_offset: int = 26,
    tolerance_pct: float = 0.005,
) -> bool:
    """
    Check if Chikou Span is tangled with price candles from 26 periods ago.
    
    Chikou is today's close plotted 26 bars back. It's "tangled" if it's
    within the high-low range of the candles around that point.
    """
    if len(closes) < chikou_offset + 3:
        return True  # Default to tangled if insufficient data
    
    # Current close (Chikou value)
    chikou = closes[-1]
    
    # Check against candles around 26 bars back (allow some tolerance)
    for offset in range(chikou_offset - 2, chikou_offset + 3):
        if offset <= 0 or offset >= len(closes):
            continue
        
        idx = len(closes) - offset
        if idx < 0 or idx >= len(highs) or idx >= len(lows):
            continue
        
        h = highs[idx]
        l = lows[idx]
        
        # Add tolerance to the range
        tolerance = (h - l) * tolerance_pct if h > l else 0
        range_top = h + tolerance
        range_bot = l - tolerance
        
        if range_bot <= chikou <= range_top:
            return True
    
    return False


def compute_cloud_penetration_pct(
    close: float,
    cloud_top: float,
    cloud_bottom: float,
) -> float:
    """
    Compute percentage of cloud penetration.
    
    Returns:
        0.0 if outside cloud
        0-100 representing how deep into cloud (100 = at opposite edge)
    """
    if close >= cloud_top:
        return 0.0
    if close <= cloud_bottom:
        return 0.0
    
    thickness = cloud_top - cloud_bottom
    if thickness <= 0:
        return 0.0
    
    # Distance from nearest edge
    dist_from_top = cloud_top - close
    dist_from_bot = close - cloud_bottom
    
    # Penetration is the minimum distance as percentage of thickness
    penetration = min(dist_from_top, dist_from_bot)
    return (penetration / thickness) * 100.0


def detect_trend_regime(
    close: float,
    cloud: Optional[Dict[str, Any]],
    cloud_future: Optional[Dict[str, Any]],
    kijun_slope: str,
) -> Dict[str, Any]:
    """
    Determine if price is in a valid trend regime for Engine4.
    
    Bull regime:
    - Close above cloud (required)
    - Cloud ahead is bullish OR current cloud bullish (required)
    - Kijun slope is a quality factor (not hard filter)
    
    Bear regime:
    - Close below cloud (required)
    - Cloud ahead is bearish OR current cloud bearish (required)
    - Kijun slope is a quality factor (not hard filter)
    
    Note: Kijun slope is used for scoring (A+ criteria), not rejection.
    During pullbacks, Kijun may naturally have mild opposite slope.
    """
    if cloud is None:
        return {"valid": False, "direction": None, "reason": "Cloud data unavailable"}
    
    cloud_top = cloud.get("cloudTop")
    cloud_bottom = cloud.get("cloudBottom")
    current_bias = cloud.get("cloudBias")
    
    if cloud_top is None or cloud_bottom is None:
        return {"valid": False, "direction": None, "reason": "Cloud values missing"}
    
    # Determine position relative to cloud
    if close > cloud_top:
        position = "above"
    elif close < cloud_bottom:
        position = "below"
    else:
        position = "inside"
    
    # Get future cloud bias
    future_bias = None
    if cloud_future and isinstance(cloud_future, dict):
        future_bias = cloud_future.get("cloudBias")
    
    result: Dict[str, Any] = {
        "valid": False,
        "direction": None,
        "position": position,
        "currentBias": current_bias,
        "futureBias": future_bias,
        "kijunSlope": kijun_slope,
        "reason": None,
    }
    
    # Bull regime check - Kijun slope is a quality factor, not a hard filter
    # After a pullback, Kijun may have mild negative slope which is acceptable
    if position == "above":
        if future_bias == "bullish" or current_bias == "bullish":
            result["valid"] = True
            result["direction"] = "bullish"
            if kijun_slope == "positive":
                result["reason"] = "Price above cloud with rising Kijun (A+ quality)"
            elif kijun_slope == "flat":
                result["reason"] = "Price above cloud with flat Kijun"
            else:
                result["reason"] = "Price above cloud (Kijun slope negative - monitor)"
        else:
            result["reason"] = "Cloud bias not aligned for bull trend"
    
    # Bear regime check
    elif position == "below":
        if future_bias == "bearish" or current_bias == "bearish":
            result["valid"] = True
            result["direction"] = "bearish"
            if kijun_slope == "negative":
                result["reason"] = "Price below cloud with falling Kijun (A+ quality)"
            elif kijun_slope == "flat":
                result["reason"] = "Price below cloud with flat Kijun"
            else:
                result["reason"] = "Price below cloud (Kijun slope positive - monitor)"
        else:
            result["reason"] = "Cloud bias not aligned for bear trend"
    
    else:
        result["reason"] = "Price inside cloud - regime unclear"
    
    return result


def detect_pullback_state(
    bars: List[DailyBar],
    closes: List[float],
    tenkan_series: List[Optional[float]],
    kijun_series: List[Optional[float]],
    cloud_series: List[Optional[Dict[str, Any]]],
    direction: str,
    lookback: int = 10,
    atr: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Detect pullback state for continuation entry.
    
    Bull pullback:
    - Price pulls back and closes below Tenkan at least once
    - Pullback gets near or touches Kijun (within ATR tolerance)
    - Price does not close deep into cloud (> 35%)
    
    Returns pullback state with trigger readiness.
    """
    if not closes or not tenkan_series or not kijun_series:
        return {"valid": False, "state": "insufficient_data", "notes": []}
    
    n = len(closes)
    if n < lookback + 1:
        return {"valid": False, "state": "insufficient_history", "notes": []}
    
    # Current values
    tenkan = tenkan_series[-1] if tenkan_series[-1] is not None else None
    kijun = kijun_series[-1] if kijun_series[-1] is not None else None
    prev_tenkan = tenkan_series[-2] if len(tenkan_series) > 1 and tenkan_series[-2] is not None else None
    close = closes[-1]
    
    if tenkan is None or kijun is None:
        return {"valid": False, "state": "missing_ichimoku_values", "notes": []}
    
    # ATR tolerance for "near Kijun"
    tolerance = (atr * 0.5) if atr is not None else abs(kijun - tenkan) * 0.3
    
    result: Dict[str, Any] = {
        "valid": False,
        "state": "unknown",
        "tenkan": tenkan,
        "kijun": kijun,
        "close": close,
        "tolerance": tolerance,
        "closedBelowTenkan": False,
        "nearKijun": False,
        "deepCloudPenetration": False,
        "reclaimedTenkan": False,
        "notes": [],
    }
    
    # Check pullback conditions over lookback period
    closed_below_tenkan = False
    touched_kijun = False
    deep_penetration = False
    
    for i in range(n - lookback, n):
        if i < 0:
            continue
        
        c = closes[i]
        t = tenkan_series[i] if i < len(tenkan_series) and tenkan_series[i] is not None else None
        k = kijun_series[i] if i < len(kijun_series) and kijun_series[i] is not None else None
        cloud = cloud_series[i] if i < len(cloud_series) else None
        
        if t is None or k is None:
            continue
        
        if direction == "bullish":
            # Check if closed below Tenkan
            if c < t:
                closed_below_tenkan = True
            
            # Check if near/touched Kijun
            if c <= k + tolerance:
                touched_kijun = True
            
            # Check for deep cloud penetration
            if cloud:
                pen_pct = compute_cloud_penetration_pct(
                    c, cloud.get("cloudTop", 0), cloud.get("cloudBottom", 0)
                )
                if pen_pct > CLOUD_PENETRATION_DEEP * 100:
                    deep_penetration = True
        
        else:  # bearish
            if c > t:
                closed_below_tenkan = True
            if c >= k - tolerance:
                touched_kijun = True
            if cloud:
                pen_pct = compute_cloud_penetration_pct(
                    c, cloud.get("cloudTop", 0), cloud.get("cloudBottom", 0)
                )
                if pen_pct > CLOUD_PENETRATION_DEEP * 100:
                    deep_penetration = True
    
    result["closedBelowTenkan"] = closed_below_tenkan
    result["nearKijun"] = touched_kijun
    result["deepCloudPenetration"] = deep_penetration
    
    # Check if reclaimed Tenkan (current close)
    if direction == "bullish":
        reclaimed = close > tenkan
    else:
        reclaimed = close < tenkan
    result["reclaimedTenkan"] = reclaimed
    
    # Determine state
    # Note: Shallow pullbacks (below Tenkan but not to Kijun) are still valid setups
    # They just score lower in the A+ system. Deep pullbacks to Kijun score higher.
    if deep_penetration:
        result["state"] = "rejected_deep_penetration"
        result["notes"].append("Pullback penetrated too deep into cloud.")
    elif not closed_below_tenkan:
        result["state"] = "no_pullback"
        result["notes"].append("No pullback below Tenkan detected.")
    elif reclaimed:
        result["valid"] = True
        result["state"] = "trigger_ready"
        if touched_kijun:
            result["notes"].append("Strong pullback to Kijun with Tenkan reclaim.")
        else:
            result["notes"].append("Shallow pullback with Tenkan reclaim (Kijun not touched).")
    else:
        result["state"] = "pullback_in_progress"
        result["notes"].append("Pullback ongoing, awaiting Tenkan reclaim.")
    
    return result


def detect_entry_trigger(
    bar: DailyBar,
    tenkan: float,
    prev_tenkan: Optional[float],
    kijun: float,
    direction: str,
    rsi: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Detect if entry trigger conditions are met on the given bar.
    
    A+ Bull entry trigger:
    - EOD close back above Tenkan
    - Close in top 33% of day's range
    - Optional: Tenkan turning up and separating from Kijun
    - Optional: RSI > 50
    
    A+ Bear entry trigger:
    - EOD close back below Tenkan
    - Close in bottom 33% of range
    - Optional: Tenkan turning down
    - Optional: RSI < 50
    """
    if any(v is None for v in (bar.open, bar.high, bar.low, bar.close)):
        return {"triggered": False, "reason": "Missing OHLC"}
    
    o, h, l, c = float(bar.open), float(bar.high), float(bar.low), float(bar.close)
    day_range = max(h - l, 0.0001)
    close_position = (c - l) / day_range  # 0 = at low, 1 = at high
    
    result: Dict[str, Any] = {
        "triggered": False,
        "closePosition": round(close_position, 4),
        "candleStrength": "neutral",
        "tenkanReclaim": False,
        "tenkanSeparating": False,
        "rsiConfirm": None,
        "notes": [],
    }
    
    if direction == "bullish":
        # Check Tenkan reclaim
        tenkan_reclaim = c > tenkan
        result["tenkanReclaim"] = tenkan_reclaim
        
        # Check candle strength (close in top 33%)
        candle_strength = close_position >= CANDLE_STRENGTH_BULLISH
        result["candleStrength"] = "strong" if candle_strength else "weak"
        
        # Check Tenkan separating from Kijun
        if prev_tenkan is not None:
            tenkan_rising = tenkan > prev_tenkan
            tenkan_separating = tenkan > kijun and tenkan_rising
            result["tenkanSeparating"] = tenkan_separating
        
        # RSI confirmation
        if rsi is not None:
            result["rsiConfirm"] = rsi > 50
        
        # Trigger logic
        if tenkan_reclaim and candle_strength:
            result["triggered"] = True
            result["notes"].append("Strong bullish trigger: Tenkan reclaim with strong close.")
        elif tenkan_reclaim:
            result["triggered"] = True
            result["notes"].append("Bullish trigger: Tenkan reclaim (close position moderate).")
    
    else:  # bearish
        tenkan_reclaim = c < tenkan
        result["tenkanReclaim"] = tenkan_reclaim
        
        candle_strength = close_position <= CANDLE_STRENGTH_BEARISH
        result["candleStrength"] = "strong" if candle_strength else "weak"
        
        if prev_tenkan is not None:
            tenkan_falling = tenkan < prev_tenkan
            tenkan_separating = tenkan < kijun and tenkan_falling
            result["tenkanSeparating"] = tenkan_separating
        
        if rsi is not None:
            result["rsiConfirm"] = rsi < 50
        
        if tenkan_reclaim and candle_strength:
            result["triggered"] = True
            result["notes"].append("Strong bearish trigger: Tenkan loss with weak close.")
        elif tenkan_reclaim:
            result["triggered"] = True
            result["notes"].append("Bearish trigger: Tenkan loss (close position moderate).")
    
    return result


def compute_entry_levels(
    bar: DailyBar,
    kijun: float,
    direction: str,
    atr: Optional[float],
    swing_target: Optional[float] = None,
) -> Dict[str, float]:
    """
    Compute entry, stop, and target levels for the signal.
    
    Bull:
    - Entry: Buy stop above today's high
    - Stop: Below Kijun or swing low + ATR buffer
    - Target 1: Prior swing high or 1R
    - Target 2: 2R
    - Trail: Kijun
    
    Bear:
    - Entry: Sell stop below today's low
    - Stop: Above Kijun or swing high + ATR buffer
    """
    if any(v is None for v in (bar.high, bar.low)):
        return {}
    
    h, l = float(bar.high), float(bar.low)
    atr_buffer = (atr * ATR_BUFFER_MULT) if atr else 0.10
    trigger_offset = _entry_offset(atr)  # tick/ATR-scaled, not a flat penny
    
    if direction == "bullish":
        entry = round(h + trigger_offset, 4)  # ATR-scaled above high
        stop = round(min(kijun, l) - atr_buffer, 4)
        risk = entry - stop
        
        if swing_target and swing_target > entry:
            target_1 = round(swing_target, 4)
        else:
            target_1 = round(entry + risk, 4)  # 1R
        
        target_2 = round(entry + (2 * risk), 4)  # 2R
        trail = round(kijun, 4)
    
    else:  # bearish
        entry = round(l - trigger_offset, 4)  # ATR-scaled below low
        stop = round(max(kijun, h) + atr_buffer, 4)
        risk = stop - entry
        
        if swing_target and swing_target < entry:
            target_1 = round(swing_target, 4)
        else:
            target_1 = round(entry - risk, 4)  # 1R
        
        target_2 = round(entry - (2 * risk), 4)  # 2R
        trail = round(kijun, 4)
    
    return {
        "entry": entry,
        "stop": stop,
        "risk": round(abs(risk), 4),
        "target1": target_1,
        "target2": target_2,
        "trail": trail,
    }


def find_swing_target(
    highs: List[float],
    lows: List[float],
    direction: str,
    lookback: int = 20,
) -> Optional[float]:
    """
    Find the prior swing high/low for target setting.
    """
    if not highs or not lows or len(highs) < lookback:
        return None
    
    if direction == "bullish":
        # Find recent swing high (local maximum)
        search_range = highs[-(lookback + 1):-1]  # Exclude current bar
        if search_range:
            return max(search_range)
    else:
        # Find recent swing low
        search_range = lows[-(lookback + 1):-1]
        if search_range:
            return min(search_range)
    
    return None


# ---------------------------------------------------------------------------
# Freshness Classification Functions
# ---------------------------------------------------------------------------

def count_bars_since_tenkan_reclaim(
    closes: List[float],
    tenkan_series: List[Optional[float]],
    direction: str,
) -> Optional[int]:
    """
    Count bars since the most recent Tenkan reclaim crossover.
    
    Bull: close crossed from below Tenkan to above Tenkan
    Bear: close crossed from above Tenkan to below Tenkan
    
    Returns:
        Number of bars since reclaim, or None if no reclaim found in history
    """
    if not closes or not tenkan_series or len(closes) < 2:
        return None
    
    n = min(len(closes), len(tenkan_series))
    
    # Walk backwards from most recent bar
    for i in range(n - 1, 0, -1):
        curr_close = closes[i]
        prev_close = closes[i - 1]
        curr_tenkan = tenkan_series[i]
        prev_tenkan = tenkan_series[i - 1]
        
        if curr_tenkan is None or prev_tenkan is None:
            continue
        
        if direction == "bullish":
            # Looking for: was below Tenkan, now above Tenkan
            if prev_close < prev_tenkan and curr_close > curr_tenkan:
                # Found the reclaim bar
                bars_since = (n - 1) - i
                return bars_since
        else:  # bearish
            # Looking for: was above Tenkan, now below Tenkan
            if prev_close > prev_tenkan and curr_close < curr_tenkan:
                bars_since = (n - 1) - i
                return bars_since
    
    return None


def compute_kijun_distance_atr(
    close: float,
    kijun: float,
    atr: float,
    direction: str,
) -> Optional[float]:
    """
    Compute distance from close to Kijun in ATR units.
    
    Bull: (close - kijun) / ATR (positive when above)
    Bear: (kijun - close) / ATR (positive when below)
    
    Returns:
        Distance in ATR units, or None if inputs invalid
    """
    if atr is None or atr <= 0:
        return None
    
    if direction == "bullish":
        return (close - kijun) / atr
    else:
        return (kijun - close) / atr


def check_recent_tenkan_interaction(
    bars: List[DailyBar],
    tenkan_series: List[Optional[float]],
    direction: str,
    atr: float,
    lookback: int = 5,
) -> bool:
    """
    Check if price has penetrated Tenkan in the last N bars.
    
    Requires actual penetration (not just touch) to filter slow grinders:
    - Bull: low <= tenkan - 0.1 * ATR in at least one bar
    - Bear: high >= tenkan + 0.1 * ATR in at least one bar
    
    Returns:
        True if recent penetration found, False otherwise
    """
    if not bars or not tenkan_series or len(bars) < lookback:
        return False
    
    if atr is None or atr <= 0:
        return False
    
    # Penetration buffer (require 0.1 ATR beyond Tenkan)
    penetration_buffer = TENKAN_PENETRATION_ATR * atr
    
    n = min(len(bars), len(tenkan_series))
    start_idx = max(0, n - lookback)
    
    for i in range(start_idx, n):
        bar = bars[i]
        tenkan = tenkan_series[i]
        
        if tenkan is None:
            continue
        
        low = float(bar.low) if bar.low is not None else None
        high = float(bar.high) if bar.high is not None else None
        
        if direction == "bullish":
            # For bulls, low must penetrate below Tenkan by at least 0.1 ATR
            if low is not None and low <= (tenkan - penetration_buffer):
                return True
        else:  # bearish
            # For bears, high must penetrate above Tenkan by at least 0.1 ATR
            if high is not None and high >= (tenkan + penetration_buffer):
                return True
    
    return False


def check_impulse_displacement(
    bar: DailyBar,
    atr: float,
) -> bool:
    """
    Check if the current bar is an impulse/event candle.
    
    An impulse bar has true range > 2.5x ATR, indicating unusual volatility
    that makes continuation entries unreliable.
    
    Returns:
        True if impulse bar (should reject), False otherwise
    """
    if atr is None or atr <= 0:
        return False
    
    if bar.high is None or bar.low is None:
        return False
    
    true_range = float(bar.high) - float(bar.low)
    
    return true_range > (IMPULSE_DISPLACEMENT_MULT * atr)


def check_trigger_already_ran(
    bars: List[DailyBar],
    closes: List[float],
    tenkan_series: List[Optional[float]],
    direction: str,
    atr: float,
) -> Tuple[bool, Optional[float]]:
    """
    Check if price has already run away from the reclaim bar.
    
    Finds the most recent Tenkan reclaim bar and checks if the current close
    has moved too far from that bar's high/low (> 0.75 ATR):
    - Bull: if close > reclaim_bar_high + 0.75 * ATR → trigger ran
    - Bear: if close < reclaim_bar_low - 0.75 * ATR → trigger ran
    
    Returns:
        Tuple of (trigger_ran: bool, distance_from_reclaim: Optional[float])
    """
    if not bars or not closes or not tenkan_series or len(closes) < 2:
        return (False, None)
    
    if atr is None or atr <= 0:
        return (False, None)
    
    n = min(len(bars), len(closes), len(tenkan_series))
    current_close = closes[-1]
    
    # Find the reclaim bar (most recent Tenkan crossover)
    reclaim_bar_idx = None
    for i in range(n - 1, 0, -1):
        curr_close = closes[i]
        prev_close = closes[i - 1]
        curr_tenkan = tenkan_series[i]
        prev_tenkan = tenkan_series[i - 1]
        
        if curr_tenkan is None or prev_tenkan is None:
            continue
        
        if direction == "bullish":
            # Looking for: was below Tenkan, now above Tenkan
            if prev_close < prev_tenkan and curr_close > curr_tenkan:
                reclaim_bar_idx = i
                break
        else:  # bearish
            # Looking for: was above Tenkan, now below Tenkan
            if prev_close > prev_tenkan and curr_close < curr_tenkan:
                reclaim_bar_idx = i
                break
    
    if reclaim_bar_idx is None:
        return (False, None)
    
    # Get the reclaim bar's high/low
    reclaim_bar = bars[reclaim_bar_idx]
    reclaim_high = float(reclaim_bar.high) if reclaim_bar.high is not None else None
    reclaim_low = float(reclaim_bar.low) if reclaim_bar.low is not None else None
    
    if reclaim_high is None or reclaim_low is None:
        return (False, None)
    
    # Check if price has run away
    run_threshold = TRIGGER_RAN_ATR * atr
    
    if direction == "bullish":
        # Bull: check if close > reclaim_high + 0.75 ATR
        run_level = reclaim_high + run_threshold
        if current_close > run_level:
            distance = (current_close - reclaim_high) / atr
            return (True, round(distance, 2))
    else:  # bearish
        # Bear: check if close < reclaim_low - 0.75 ATR
        run_level = reclaim_low - run_threshold
        if current_close < run_level:
            distance = (reclaim_low - current_close) / atr
            return (True, round(distance, 2))
    
    return (False, None)


def check_impulse_reclaim(
    bars: List[DailyBar],
    closes: List[float],
    tenkan_series: List[Optional[float]],
    direction: str,
    atr: float,
    bars_since_reclaim: Optional[int],
) -> Tuple[bool, Optional[float]]:
    """
    Check if the reclaim bar was an impulse candle that already extended.
    
    If reclaim age = 0 (just happened) AND the reclaim bar:
    - Has range > 1.25 ATR
    - Moved in the direction of the trend (bullish close > open, bearish close < open)
    
    → Downgrade to Structure Only (don't chase the impulse)
    
    Returns:
        Tuple of (is_impulse_reclaim: bool, reclaim_bar_range_atr: Optional[float])
    """
    # Only applies when reclaim just happened (age = 0)
    if bars_since_reclaim is None or bars_since_reclaim != 0:
        return (False, None)
    
    if not bars or not closes or not tenkan_series or len(closes) < 2:
        return (False, None)
    
    if atr is None or atr <= 0:
        return (False, None)
    
    # The reclaim bar is the current bar (since bars_since_reclaim = 0)
    reclaim_bar = bars[-1]
    
    if reclaim_bar.high is None or reclaim_bar.low is None:
        return (False, None)
    if reclaim_bar.open is None or reclaim_bar.close is None:
        return (False, None)
    
    bar_range = float(reclaim_bar.high) - float(reclaim_bar.low)
    bar_range_atr = bar_range / atr
    
    # Check if range exceeds threshold
    if bar_range_atr <= IMPULSE_RECLAIM_ATR:
        return (False, round(bar_range_atr, 2))
    
    # Check if candle direction aligns with trend
    bar_open = float(reclaim_bar.open)
    bar_close = float(reclaim_bar.close)
    
    if direction == "bullish":
        # Bullish setup: check if reclaim bar was a bullish candle (close > open)
        if bar_close > bar_open:
            return (True, round(bar_range_atr, 2))
    else:  # bearish
        # Bearish setup: check if reclaim bar was a bearish candle (close < open)
        if bar_close < bar_open:
            return (True, round(bar_range_atr, 2))
    
    return (False, round(bar_range_atr, 2))


# ---------------------------------------------------------------------------
# Main Detection Function
# ---------------------------------------------------------------------------

def detect_ichimoku_setup(
    bars: List[DailyBar],
    *,
    ticker: str = "",
    index_membership: str = "sp500",
    gamma_context: Optional[Dict[str, Any]] = None,
    earnings_days_ahead: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Detect Ichimoku continuation setup with full analysis.
    
    This is the main entry point for Engine4 analysis on a single ticker.
    
    Returns:
        Dict with detection results, indicators, and optional signal
    """
    result: Dict[str, Any] = {
        "enabled": False,
        "ticker": ticker,
        "hasSignal": False,
        "signal": None,
        "trend": None,
        "pullback": None,
        "trigger": None,
        "indicators": {},
        "notes": [],
    }
    
    if not bars or len(bars) < 60:
        result["notes"].append("Insufficient bars for Ichimoku analysis (need 60+).")
        return result
    
    result["enabled"] = True
    
    # Compute Ichimoku series
    ich_series = compute_ichimoku_series(bars)
    if not ich_series.get("enabled"):
        result["notes"].append("Failed to compute Ichimoku series.")
        return result
    
    tenkan_series = ich_series["tenkan_series"]
    kijun_series = ich_series["kijun_series"]
    cloud_series = ich_series["cloud_series"]
    closes = ich_series["closes"]
    highs = ich_series["highs"]
    lows = ich_series["lows"]
    
    # Current Ichimoku values
    tenkan = tenkan_series[-1] if tenkan_series[-1] is not None else None
    kijun = kijun_series[-1] if kijun_series[-1] is not None else None
    prev_tenkan = tenkan_series[-2] if len(tenkan_series) > 1 and tenkan_series[-2] is not None else None
    cloud = cloud_series[-1] if cloud_series[-1] is not None else None
    
    # Get future cloud (current span values represent 26-day forward projection)
    span_a = ich_series["span_a_series"][-1] if ich_series["span_a_series"][-1] is not None else None
    span_b = ich_series["span_b_series"][-1] if ich_series["span_b_series"][-1] is not None else None
    cloud_future = None
    if span_a is not None and span_b is not None:
        cloud_future = {
            "spanA": span_a,
            "spanB": span_b,
            "cloudTop": max(span_a, span_b),
            "cloudBottom": min(span_a, span_b),
            "cloudBias": "bullish" if span_a >= span_b else "bearish",
        }
    
    if tenkan is None or kijun is None or cloud is None:
        result["notes"].append("Missing current Ichimoku values.")
        return result
    
    # Compute additional indicators
    rsi_series = compute_rsi_series(closes, period=14)
    rsi = rsi_series[-1] if rsi_series and rsi_series[-1] is not None else None
    
    volume_metrics = compute_volume_metrics(bars, period=20)
    volume_ratio = volume_metrics.get("volumeRatio")
    
    atr_data = compute_atr_series(bars, period=14)
    atr = atr_data.get("atr")
    
    # Kijun slope analysis
    kijun_slope_dir, kijun_slope_val = compute_kijun_slope(kijun_series, lookback=5)
    kijun_flat_days = count_kijun_flat_days(kijun_series, lookback=20)
    
    # Time in cloud
    time_in_cloud = compute_time_in_cloud(closes, cloud_series, lookback=20)
    
    # Chikou entanglement
    chikou_tangled = is_chikou_tangled(closes, highs, lows)
    
    # Store indicators
    result["indicators"] = {
        "tenkan": tenkan,
        "kijun": kijun,
        "cloudTop": cloud.get("cloudTop"),
        "cloudBottom": cloud.get("cloudBottom"),
        "cloudBias": cloud.get("cloudBias"),
        "cloudThickness": cloud.get("thickness"),
        "rsi": round(rsi, 2) if rsi is not None else None,
        "volumeRatio": round(volume_ratio, 2) if volume_ratio is not None else None,
        "atr": round(atr, 4) if atr is not None else None,
        "kijunSlope": kijun_slope_dir,
        "kijunSlopeValue": round(kijun_slope_val, 6) if kijun_slope_val else None,
        "kijunFlatDays": kijun_flat_days,
        "timeInCloud": time_in_cloud,
        "chikouTangled": chikou_tangled,
    }
    
    # Detect trend regime
    close = closes[-1]
    trend = detect_trend_regime(close, cloud, cloud_future, kijun_slope_dir)
    result["trend"] = trend
    
    if not trend.get("valid"):
        result["notes"].append(f"Trend not qualified: {trend.get('reason')}")
        return result
    
    direction = trend["direction"]
    
    # Detect pullback state
    pullback = detect_pullback_state(
        bars, closes, tenkan_series, kijun_series, cloud_series,
        direction=direction, lookback=10, atr=atr
    )
    result["pullback"] = pullback
    
    if not pullback.get("valid"):
        result["notes"].append(f"Pullback not valid: {pullback.get('state')}")
        return result
    
    # Detect entry trigger
    trigger = detect_entry_trigger(
        bars[-1], tenkan, prev_tenkan, kijun, direction, rsi
    )
    result["trigger"] = trigger
    
    if not trigger.get("triggered"):
        result["notes"].append("Entry trigger not met.")
        return result
    
    # We have a valid setup - compute entry levels
    swing_target = find_swing_target(highs, lows, direction, lookback=20)
    levels = compute_entry_levels(bars[-1], kijun, direction, atr, swing_target)
    
    if not levels:
        result["notes"].append("Failed to compute entry levels.")
        return result
    
    # Cloud penetration at current close
    cloud_pen_pct = compute_cloud_penetration_pct(
        close, cloud.get("cloudTop", 0), cloud.get("cloudBottom", 0)
    )
    
    # Pullback depth (distance from Kijun)
    pullback_depth = abs(close - kijun) / close if close > 0 else 0
    
    # Build the signal (scoring will be done separately)
    result["hasSignal"] = True
    result["signal"] = {
        "ticker": ticker,
        "signalDate": bars[-1].trade_date,
        "direction": direction,
        "tenkan": tenkan,
        "kijun": kijun,
        "chikou": closes[-1],
        "cloudTop": cloud.get("cloudTop"),
        "cloudBottom": cloud.get("cloudBottom"),
        "cloudBias": cloud.get("cloudBias"),
        "cloudThickness": cloud.get("thickness"),
        "close": close,
        "closePosition": trigger.get("closePosition"),
        "pullbackDepth": round(pullback_depth, 4),
        "cloudPenetrationPct": round(cloud_pen_pct, 2),
        "entry": levels.get("entry"),
        "stop": levels.get("stop"),
        "risk": levels.get("risk"),
        "target1": levels.get("target1"),
        "target2": levels.get("target2"),
        "trail": levels.get("trail"),
        "rsi": rsi,
        "volumeRatio": volume_ratio,
        "atr": atr,
        "kijunSlope": kijun_slope_dir,
        "kijunFlatDays": kijun_flat_days,
        "timeInCloud": time_in_cloud,
        "chikouTangled": chikou_tangled,
        "indexMembership": index_membership,
        "gammaContext": gamma_context,
        "earningsDaysAhead": earnings_days_ahead,
    }
    
    return result


# ---------------------------------------------------------------------------
# A+ Scoring System
# ---------------------------------------------------------------------------

def score_ichimoku_setup(
    signal: Dict[str, Any],
    *,
    gamma_context: Optional[Dict[str, Any]] = None,
    earnings_days_ahead: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Score an Ichimoku setup from 0-100.
    
    Scoring Components (total possible = 100):
    - Chikou clean: 15 points
    - Volume surge: 15 points
    - Candle strength: 15 points
    - Kijun slope: 10 points
    - RSI recovery: 10 points
    - Cloud bias: 10 points
    - Cloud thickness: 10 points
    - Gamma supportive: 15 points
    
    Downgrade Penalties:
    - Time-in-cloud > 10/20: -15
    - Kijun flat > 5 days: -10
    - Low volume: -10
    - Earnings soon: -25
    - Gamma mismatch: -10
    
    Returns:
        Dict with score, grade, and component breakdown
    """
    direction = signal.get("direction", "bullish")
    
    scores: Dict[str, float] = {
        "chikou": 0.0,
        "volume": 0.0,
        "candle": 0.0,
        "kijunSlope": 0.0,
        "rsi": 0.0,
        "cloudBias": 0.0,
        "cloudThickness": 0.0,
        "sector": 0.0,
        "relStrength": 0.0,
    }
    
    penalties: Dict[str, int] = {
        "timeInCloud": 0,
        "kijunFlat": 0,
        "lowVolume": 0,
        "earnings": 0,
        "gammaMismatch": 0,  # retired (always 0); kept for payload back-compat
    }
    
    tags: List[str] = []
    notes: List[str] = []
    
    # --- Chikou clean (15 points) ---
    chikou_tangled = signal.get("chikouTangled")
    if chikou_tangled is False:
        scores["chikou"] = SCORE_CHIKOU_CLEAN
        tags.append("Chikou Clear")
    elif chikou_tangled is True:
        notes.append("Chikou tangled with prior candles.")
    
    # --- Volume surge (15 points, continuous ramp 1.0x→1.6x) ---
    volume_ratio = signal.get("volumeRatio")
    if volume_ratio is not None:
        scores["volume"] = round(_ramp(volume_ratio, VOL_RAMP_LO, VOL_RAMP_HI) * SCORE_VOLUME_SURGE, 2)
        if volume_ratio >= VOLUME_SURGE_THRESHOLD:
            tags.append("Vol Surge")
        elif volume_ratio < VOLUME_LOW_THRESHOLD:
            penalties["lowVolume"] = PENALTY_LOW_VOLUME
            notes.append(f"Low volume on trigger ({volume_ratio:.2f}x avg).")
    
    # --- Candle strength (15 points, continuous close-position ramp) ---
    close_position = signal.get("closePosition")
    if close_position is not None:
        if direction == "bullish":
            candle_credit = _ramp(close_position, CANDLE_RAMP_LO, CANDLE_RAMP_HI)
        else:
            candle_credit = _ramp(close_position, 1.0 - CANDLE_RAMP_LO, 1.0 - CANDLE_RAMP_HI)
        scores["candle"] = round(candle_credit * SCORE_CANDLE_STRENGTH, 2)
        if (direction == "bullish" and close_position >= CANDLE_STRENGTH_BULLISH) or \
           (direction == "bearish" and close_position <= CANDLE_STRENGTH_BEARISH):
            tags.append("Strong Close")
    
    # --- Kijun slope (10 points) ---
    kijun_slope = signal.get("kijunSlope")
    if kijun_slope is not None:
        if direction == "bullish" and kijun_slope == "positive":
            scores["kijunSlope"] = SCORE_KIJUN_SLOPE
            tags.append("Kijun Rising")
        elif direction == "bearish" and kijun_slope == "negative":
            scores["kijunSlope"] = SCORE_KIJUN_SLOPE
            tags.append("Kijun Falling")
    
    # --- RSI recovery (10 points, continuous ramp around 50) ---
    rsi = signal.get("rsi")
    if rsi is not None:
        if direction == "bullish":
            rsi_credit = _ramp(rsi, RSI_RAMP_LO, RSI_RAMP_HI)
        else:
            rsi_credit = _ramp(rsi, 100.0 - RSI_RAMP_LO, 100.0 - RSI_RAMP_HI)
        scores["rsi"] = round(rsi_credit * SCORE_RSI_RECOVERY, 2)
        if (direction == "bullish" and rsi > 50) or (direction == "bearish" and rsi < 50):
            tags.append("RSI Confirm")
    
    # --- Cloud bias (10 points) ---
    cloud_bias = signal.get("cloudBias")
    if cloud_bias is not None:
        if direction == "bullish" and cloud_bias == "bullish":
            scores["cloudBias"] = SCORE_CLOUD_BIAS
            tags.append("Cloud Aligned")
        elif direction == "bearish" and cloud_bias == "bearish":
            scores["cloudBias"] = SCORE_CLOUD_BIAS
            tags.append("Cloud Aligned")
    
    # --- Cloud thickness (10 points) ---
    # Reasonable thickness: not razor thin (< 0.5% of price), not massive (> 5% of price)
    cloud_thickness = signal.get("cloudThickness")
    close = signal.get("close", 1)
    if cloud_thickness is not None and close > 0:
        thickness_pct = (cloud_thickness / close) * 100
        # Smooth "tent": full credit in the [0.5%, 5%] band, ramping to 0 at
        # 0% (razor thin) and 10% (too massive to traverse).
        if 0.5 <= thickness_pct <= 5.0:
            thickness_credit = 1.0
            tags.append("Cloud Optimal")
        elif thickness_pct < 0.5:
            thickness_credit = _ramp(thickness_pct, 0.0, 0.5)
            notes.append(f"Thin cloud ({thickness_pct:.1f}% of price) - less reliable support.")
        else:  # > 5.0
            thickness_credit = _ramp(thickness_pct, 10.0, 5.0)
            notes.append(f"Thick cloud ({thickness_pct:.1f}% of price) may slow progress.")
        scores["cloudThickness"] = round(thickness_credit * SCORE_CLOUD_THICKNESS, 2)
    
    # --- Sector trend alignment (8 points) ---
    # The name's GICS sector ETF cloud direction. Full credit when the sector
    # is trending WITH the setup, zero when it fights it, half when the sector
    # is neutral/unknown (missing data shouldn't silently kill a clean name).
    sector_bias = signal.get("sectorBias")
    if sector_bias in ("bullish", "bearish"):
        if sector_bias == ("bullish" if direction == "bullish" else "bearish"):
            scores["sector"] = SCORE_SECTOR_ALIGN
            tags.append("Sector Aligned")
        else:
            scores["sector"] = 0.0
            notes.append("Setup fights its sector trend.")
    else:
        scores["sector"] = round(SCORE_SECTOR_ALIGN * 0.5, 2)  # neutral/unknown

    # --- Relative strength vs index (7 points, continuous) ---
    # Longs are rewarded for leading the index, shorts for lagging it.
    rs_ratio = signal.get("rsRatio")
    if rs_ratio is not None:
        if direction == "bullish":
            rs_credit = _ramp(rs_ratio, RS_RAMP_LO, RS_RAMP_HI)
        else:
            rs_credit = _ramp(rs_ratio, -RS_RAMP_LO, -RS_RAMP_HI)
        scores["relStrength"] = round(rs_credit * SCORE_REL_STRENGTH, 2)
        if (direction == "bullish" and rs_ratio > 0) or (direction == "bearish" and rs_ratio < 0):
            tags.append("Leadership")
    else:
        scores["relStrength"] = round(SCORE_REL_STRENGTH * 0.5, 2)  # unknown

    # --- PENALTIES ---
    
    # Time in cloud (chop detection)
    time_in_cloud = signal.get("timeInCloud")
    if time_in_cloud is not None and time_in_cloud > TIME_IN_CLOUD_THRESHOLD:
        penalties["timeInCloud"] = PENALTY_TIME_IN_CLOUD
        notes.append(f"High time-in-cloud ({time_in_cloud}/20 days) - choppy regime.")
    
    # Kijun flat
    kijun_flat_days = signal.get("kijunFlatDays")
    if kijun_flat_days is not None and kijun_flat_days > KIJUN_FLAT_THRESHOLD:
        penalties["kijunFlat"] = PENALTY_KIJUN_FLAT
        notes.append(f"Kijun flat for {kijun_flat_days} days - range equilibrium.")
    
    # Earnings soon
    if earnings_days_ahead is not None and 0 < earnings_days_ahead <= 5:
        penalties["earnings"] = PENALTY_EARNINGS_SOON
        notes.append(f"Earnings in {earnings_days_ahead} sessions - gap risk.")
        tags.append("Earnings Warning")
    
    # NOTE: dealer gamma intentionally no longer affects the quality score.
    # It is evaluated once, in the gate + reconciled verdict (backend/gating.py),
    # so a name can't be rewarded (or penalised) for gamma twice.

    # --- Calculate total score ---
    total_positive = sum(scores.values())
    total_penalties = sum(penalties.values())  # Already negative
    total_score = round(max(0.0, min(100.0, total_positive + total_penalties)), 2)
    
    # Determine grade
    if total_score >= APLUS_THRESHOLD:
        grade = "A+"
    elif total_score >= 60:
        grade = "A"
    elif total_score >= 45:
        grade = "B"
    else:
        grade = "C"
    
    # Determine strength
    strength = "strong" if total_score >= APLUS_THRESHOLD else "standard"
    
    return {
        "score": total_score,
        "grade": grade,
        "strength": strength,
        "scores": scores,
        "penalties": penalties,
        "tags": tags,
        "notes": notes,
        "breakdown": {
            "chikou": scores["chikou"],
            "volume": scores["volume"],
            "candle": scores["candle"],
            "kijunSlope": scores["kijunSlope"],
            "rsi": scores["rsi"],
            "cloudBias": scores["cloudBias"],
            "cloudThickness": scores["cloudThickness"],
            "sector": scores["sector"],
            "relStrength": scores["relStrength"],
            "gamma": 0.0,  # retired from scoring; kept for payload back-compat
            "timeInCloudPenalty": penalties["timeInCloud"],
            "kijunFlatPenalty": penalties["kijunFlat"],
            "lowVolumePenalty": penalties["lowVolume"],
            "earningsPenalty": penalties["earnings"],
            "gammaMismatchPenalty": penalties["gammaMismatch"],
        },
    }


def classify_freshness(
    bars: List[DailyBar],
    closes: List[float],
    tenkan_series: List[Optional[float]],
    direction: str,
    close: float,
    kijun: float,
    atr: float,
) -> Dict[str, Any]:
    """
    Classify a signal into Actionable or Structure bucket based on freshness rules.
    
    Rules (all must pass for Actionable):
    1. Tenkan reclaim <= 3 bars ago
    2. Distance to Kijun <= 1.5 ATR
    3. Recent Tenkan penetration in last 5 bars (0.1 ATR beyond Tenkan)
    4. Not an impulse displacement bar (TR > 2.5x ATR = hard reject)
    5. Trigger hasn't already run (price not > 0.75 ATR from reclaim bar)
    6. Reclaim bar wasn't an impulse (if age=0 and range > 1.25 ATR in trend direction)
    
    Returns:
        Dict with bucket, reasons, and individual metrics
    """
    reasons: List[str] = []
    
    # Check impulse displacement first (hard reject)
    is_impulse = check_impulse_displacement(bars[-1], atr) if bars else False
    
    # Check reclaim age
    bars_since_reclaim = count_bars_since_tenkan_reclaim(closes, tenkan_series, direction)
    
    # Check Kijun distance in ATR
    kijun_dist = compute_kijun_distance_atr(close, kijun, atr, direction)
    
    # Check recent Tenkan penetration (now requires 0.1 ATR penetration)
    recent_touch = check_recent_tenkan_interaction(bars, tenkan_series, direction, atr, FRESHNESS_TENKAN_LOOKBACK)
    
    # Check if trigger already ran
    trigger_ran, trigger_ran_dist = check_trigger_already_ran(bars, closes, tenkan_series, direction, atr)
    
    # Check if reclaim bar was an impulse (only applies when reclaim_age = 0)
    impulse_reclaim, reclaim_bar_range = check_impulse_reclaim(
        bars, closes, tenkan_series, direction, atr, bars_since_reclaim
    )
    
    # Determine bucket
    bucket = "actionable"
    
    # Rule 4: Impulse bar is a hard reject (won't be in either bucket)
    if is_impulse:
        bucket = "rejected"
        reasons.append(f"Impulse bar (TR > {IMPULSE_DISPLACEMENT_MULT}x ATR)")
    else:
        # Rule 1: Reclaim age
        if bars_since_reclaim is None:
            bucket = "structure"
            reasons.append("No Tenkan reclaim found")
        elif bars_since_reclaim > FRESHNESS_RECLAIM_MAX_BARS:
            bucket = "structure"
            reasons.append(f"Reclaim {bars_since_reclaim} bars ago (max {FRESHNESS_RECLAIM_MAX_BARS})")
        
        # Rule 2: Extension from Kijun
        if kijun_dist is not None and kijun_dist > FRESHNESS_KIJUN_DISTANCE_ATR:
            bucket = "structure"
            reasons.append(f"Extended {kijun_dist:.1f} ATR from Kijun (max {FRESHNESS_KIJUN_DISTANCE_ATR})")
        
        # Rule 3: Recent Tenkan penetration (stricter - requires 0.1 ATR beyond Tenkan)
        if not recent_touch:
            bucket = "structure"
            reasons.append(f"No Tenkan penetration in last {FRESHNESS_TENKAN_LOOKBACK} bars")
        
        # Rule 5: Trigger already ran
        if trigger_ran:
            bucket = "structure"
            reasons.append(f"Trigger ran {trigger_ran_dist:.1f} ATR from reclaim bar")
        
        # Rule 6: Impulse reclaim suppression (reclaim bar was too big)
        if impulse_reclaim:
            bucket = "structure"
            reasons.append(f"Impulse reclaim ({reclaim_bar_range:.1f} ATR bar)")
    
    return {
        "bucket": bucket,
        "reasons": reasons,
        "barsSinceReclaim": bars_since_reclaim,
        "kijunDistanceAtr": round(kijun_dist, 2) if kijun_dist is not None else None,
        "recentTenkanTouch": recent_touch,
        "isImpulseBar": is_impulse,
        "triggerAlreadyRan": trigger_ran,
        "triggerRanDistanceAtr": trigger_ran_dist,
        "impulseReclaim": impulse_reclaim,
        "reclaimBarRangeAtr": reclaim_bar_range,
    }


def compute_distance_to_actionable(freshness: Dict[str, Any]) -> float:
    """Rank a structure signal by how close it is to becoming actionable.

    0 = actionable today; small = one pullback / a bar or two away (worth
    watching); large = the move already ran (skip). Used to cap + sort the
    structure list so the watch surface stays tight.
    """
    bucket = freshness.get("bucket")
    if bucket == "actionable":
        return 0.0
    if bucket == "rejected":
        return 999.0

    d = 0.0
    bsr = freshness.get("barsSinceReclaim")
    if bsr is None:
        d += 5.0
    elif bsr > FRESHNESS_RECLAIM_MAX_BARS:
        d += float(bsr - FRESHNESS_RECLAIM_MAX_BARS)

    kd = freshness.get("kijunDistanceAtr")
    if kd is not None and kd > FRESHNESS_KIJUN_DISTANCE_ATR:
        d += float(kd - FRESHNESS_KIJUN_DISTANCE_ATR)

    if freshness.get("recentTenkanTouch") is False:
        d += 2.0
    if freshness.get("triggerAlreadyRan"):
        d += 6.0  # the move you wanted already happened — deprioritize hard
    if freshness.get("impulseReclaim"):
        d += 6.0
    return round(d, 2)


def build_ichimoku_signal(
    *,
    ticker: str,
    detection: Dict[str, Any],
    bars: Optional[List[DailyBar]] = None,
    closes: Optional[List[float]] = None,
    tenkan_series: Optional[List[Optional[float]]] = None,
    gamma_context: Optional[Dict[str, Any]] = None,
    earnings_days_ahead: Optional[int] = None,
    index_membership: str = "sp500",
    dollar_adv: Optional[float] = None,
    sector_etf: Optional[str] = None,
    sector_state: Optional[Dict[str, Any]] = None,
    index_state: Optional[Dict[str, Any]] = None,
    rs_ratio: Optional[float] = None,
    beta: Optional[float] = None,
    corr: Optional[float] = None,
) -> Optional[IchimokuSignal]:
    """
    Build a complete IchimokuSignal from detection results.
    """
    if not detection.get("enabled") or not detection.get("hasSignal"):
        return None
    
    signal_data = detection.get("signal")
    if not signal_data:
        return None

    # Fold top-down context into the signal payload so the scorer can read it
    # the same way it reads the rest of the structure.
    sector_bias = None
    if isinstance(sector_state, dict) and sector_state.get("available"):
        d = sector_state.get("direction")
        sector_bias = d if d in ("bullish", "bearish") else None
    index_direction = None
    if isinstance(index_state, dict) and index_state.get("available"):
        index_direction = index_state.get("direction")
    signal_data["sectorBias"] = sector_bias
    signal_data["rsRatio"] = rs_ratio

    # Score the setup
    scoring = score_ichimoku_setup(
        signal_data,
        gamma_context=gamma_context,
        earnings_days_ahead=earnings_days_ahead,
    )
    
    # Build notes from all sources
    notes = []
    notes.extend(detection.get("notes", []))
    notes.extend(scoring.get("notes", []))
    
    # Classify freshness (Actionable vs Structure)
    direction = signal_data.get("direction", "bullish")
    close = signal_data.get("close", 0)
    kijun = signal_data.get("kijun", 0)
    atr = signal_data.get("atr")
    
    freshness = {
        "bucket": "actionable",
        "reasons": [],
        "barsSinceReclaim": None,
        "kijunDistanceAtr": None,
        "recentTenkanTouch": None,
        "isImpulseBar": None,
        "triggerAlreadyRan": None,
        "triggerRanDistanceAtr": None,
        "impulseReclaim": None,
        "reclaimBarRangeAtr": None,
    }
    
    if bars and closes and tenkan_series and atr:
        freshness = classify_freshness(
            bars=bars,
            closes=closes,
            tenkan_series=tenkan_series,
            direction=direction,
            close=close,
            kijun=kijun,
            atr=atr,
        )
    
    return IchimokuSignal(
        ticker=ticker,
        signal_date=signal_data.get("signalDate", ""),
        direction=direction,
        tenkan=signal_data.get("tenkan", 0),
        kijun=kijun,
        chikou=signal_data.get("chikou", 0),
        cloud_top=signal_data.get("cloudTop", 0),
        cloud_bottom=signal_data.get("cloudBottom", 0),
        cloud_bias=signal_data.get("cloudBias", ""),
        cloud_thickness=signal_data.get("cloudThickness", 0),
        close=close,
        close_position=signal_data.get("closePosition", 0.5),
        pullback_depth=signal_data.get("pullbackDepth", 0),
        cloud_penetration_pct=signal_data.get("cloudPenetrationPct", 0),
        entry_trigger=signal_data.get("entry", 0),
        stop_loss=signal_data.get("stop", 0),
        target_1=signal_data.get("target1", 0),
        target_2=signal_data.get("target2", 0),
        trail_level=signal_data.get("trail", 0),
        risk_dollars=signal_data.get("risk", 0),
        reward_1r=abs(signal_data.get("target1", 0) - signal_data.get("entry", 0)),
        status="pending",
        invalidation_reason=None,
        score=scoring.get("score", 0),
        grade=scoring.get("grade", "C"),
        chikou_score=scoring["breakdown"].get("chikou", 0),
        volume_score=scoring["breakdown"].get("volume", 0),
        candle_score=scoring["breakdown"].get("candle", 0),
        kijun_slope_score=scoring["breakdown"].get("kijunSlope", 0),
        rsi_score=scoring["breakdown"].get("rsi", 0),
        cloud_bias_score=scoring["breakdown"].get("cloudBias", 0),
        cloud_thickness_score=scoring["breakdown"].get("cloudThickness", 0),
        sector_score=scoring["breakdown"].get("sector", 0),
        rel_strength_score=scoring["breakdown"].get("relStrength", 0),
        gamma_score=scoring["breakdown"].get("gamma", 0),
        time_in_cloud_penalty=scoring["breakdown"].get("timeInCloudPenalty", 0),
        kijun_flat_penalty=scoring["breakdown"].get("kijunFlatPenalty", 0),
        low_volume_penalty=scoring["breakdown"].get("lowVolumePenalty", 0),
        earnings_penalty=scoring["breakdown"].get("earningsPenalty", 0),
        gamma_mismatch_penalty=scoring["breakdown"].get("gammaMismatchPenalty", 0),
        rsi=signal_data.get("rsi"),
        volume_ratio=signal_data.get("volumeRatio"),
        atr=atr,
        kijun_slope=signal_data.get("kijunSlope"),
        time_in_cloud=signal_data.get("timeInCloud"),
        chikou_tangled=signal_data.get("chikouTangled"),
        strength=scoring.get("strength", "standard"),
        tags=scoring.get("tags", []),
        notes=notes,
        index_membership=index_membership,
        freshness_bucket=freshness["bucket"],
        freshness_reasons=freshness["reasons"],
        bars_since_reclaim=freshness["barsSinceReclaim"],
        kijun_distance_atr=freshness["kijunDistanceAtr"],
        recent_tenkan_touch=freshness["recentTenkanTouch"],
        is_impulse_bar=freshness["isImpulseBar"],
        trigger_already_ran=freshness["triggerAlreadyRan"],
        trigger_ran_distance_atr=freshness["triggerRanDistanceAtr"],
        impulse_reclaim=freshness["impulseReclaim"],
        reclaim_bar_range_atr=freshness["reclaimBarRangeAtr"],
        dollar_adv=dollar_adv,
        distance_to_actionable=compute_distance_to_actionable(freshness),
        sector_etf=sector_etf,
        sector_bias=sector_bias,
        index_direction=index_direction,
        rs_ratio=rs_ratio,
        beta=beta,
        corr=corr,
    )


def signal_to_dict(signal: IchimokuSignal) -> Dict[str, Any]:
    """Convert IchimokuSignal to API-friendly dict."""
    return {
        "ticker": signal.ticker,
        "signalDate": signal.signal_date,
        "direction": signal.direction,
        "status": signal.status,
        "ichimoku": {
            "tenkan": signal.tenkan,
            "kijun": signal.kijun,
            "chikou": signal.chikou,
            "cloudTop": signal.cloud_top,
            "cloudBottom": signal.cloud_bottom,
            "cloudBias": signal.cloud_bias,
            "cloudThickness": signal.cloud_thickness,
        },
        "pattern": {
            "close": signal.close,
            "closePosition": signal.close_position,
            "pullbackDepth": signal.pullback_depth,
            "cloudPenetrationPct": signal.cloud_penetration_pct,
        },
        "levels": {
            "entryTrigger": signal.entry_trigger,
            "stopLoss": signal.stop_loss,
            "target1": signal.target_1,
            "target2": signal.target_2,
            "trailLevel": signal.trail_level,
            "riskDollars": signal.risk_dollars,
            "reward1R": signal.reward_1r,
        },
        "quality": {
            "score": signal.score,
            "grade": signal.grade,
            "strength": signal.strength,
            "components": {
                "chikou": signal.chikou_score,
                "volume": signal.volume_score,
                "candle": signal.candle_score,
                "kijunSlope": signal.kijun_slope_score,
                "rsi": signal.rsi_score,
                "cloudBias": signal.cloud_bias_score,
                "cloudThickness": signal.cloud_thickness_score,
                "sector": signal.sector_score,
                "relStrength": signal.rel_strength_score,
                "gamma": signal.gamma_score,
            },
            "penalties": {
                "timeInCloud": signal.time_in_cloud_penalty,
                "kijunFlat": signal.kijun_flat_penalty,
                "lowVolume": signal.low_volume_penalty,
                "earnings": signal.earnings_penalty,
                "gammaMismatch": signal.gamma_mismatch_penalty,
            },
        },
        "indicators": {
            "rsi": signal.rsi,
            "volumeRatio": signal.volume_ratio,
            "atr": signal.atr,
            "kijunSlope": signal.kijun_slope,
            "timeInCloud": signal.time_in_cloud,
            "chikouTangled": signal.chikou_tangled,
            "dollarAdv": signal.dollar_adv,
            "sectorEtf": signal.sector_etf,
            "sectorBias": signal.sector_bias,
            "indexDirection": signal.index_direction,
            "rsRatio": signal.rs_ratio,
            "beta": signal.beta,
            "corr": signal.corr,
        },
        "freshness": {
            "bucket": signal.freshness_bucket,
            "reasons": list(signal.freshness_reasons) if signal.freshness_reasons else [],
            "barsSinceReclaim": signal.bars_since_reclaim,
            "kijunDistanceAtr": signal.kijun_distance_atr,
            "recentTenkanTouch": signal.recent_tenkan_touch,
            "isImpulseBar": signal.is_impulse_bar,
            "triggerAlreadyRan": signal.trigger_already_ran,
            "triggerRanDistanceAtr": signal.trigger_ran_distance_atr,
            "impulseReclaim": signal.impulse_reclaim,
            "reclaimBarRangeAtr": signal.reclaim_bar_range_atr,
            "distanceToActionable": signal.distance_to_actionable,
        },
        "verdict": signal.verdict,
        "tags": list(signal.tags) if signal.tags else [],
        "notes": list(signal.notes) if signal.notes else [],
        "indexMembership": signal.index_membership,
    }
