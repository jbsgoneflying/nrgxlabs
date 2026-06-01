"""
Engine 3: Red Dog Reversal Trading System

A mean reversion scanner that identifies failed breakdown/breakout setups
with A+ quality scoring for the SP500 + Nasdaq100 universe.

Based on the Bottoming Tail (BT) / Topping Tail (TT) patterns from Pristine Trading,
popularized by Scott Redler as the "Red Dog Reversal."

Institutional hardening (2026-06 sprint):
- Continuous (ramped) factor scoring instead of binary cliffs, so setups rank
  within a grade instead of clustering on a single number.
- Reversal-bar rejection (wick) quality and volume confirmation are first-class
  scored factors — a real Red Dog needs the tail, not just a lower low.
- Support/Resistance confluence is computed from swing structure (no longer a
  permanently-dead hardcoded False).
- The SPX trend read is *binding*: counter-trend setups are penalized in the
  score and capped out of A+, instead of being labeled and shipped anyway.
- ATR/tick-scaled entry trigger offsets (a $0.01 trigger on a $1,600 name is
  noise).
- Deterministic outcome evaluation (`evaluate_outcome`) powers both the
  backtest harness and the live status tracker.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.technicals import DailyBar


# ---------------------------------------------------------------------------
# Grade thresholds (0-100 scale)
# ---------------------------------------------------------------------------

APLUS_THRESHOLD = 75
GRADE_A_THRESHOLD = 60
GRADE_B_THRESHOLD = 45

# ---------------------------------------------------------------------------
# Continuous scoring weights (sum = 100)
# Each factor awards partial credit on a smooth ramp between a "neutral" anchor
# (0 credit) and an "extreme" anchor (full credit), so two setups that both
# clear a threshold are still differentiated.
# ---------------------------------------------------------------------------

W_RSI = 22.0          # momentum exhaustion
W_STOCH = 12.0        # secondary oscillator confirmation
W_SMA = 18.0          # stretch from the 20-day mean (mean-reversion fuel)
W_VOL = 13.0          # participation on the reversal bar
W_CLOSE = 12.0        # close reclaimed into the favorable zone
W_WICK = 8.0          # rejection tail (the actual "Red Dog")
W_SR = 15.0           # confluence with prior swing support/resistance

# Ramp anchors (bullish framing; bearish mirrors around the midpoint)
RSI_NEUTRAL, RSI_EXTREME = 45.0, 25.0        # oversold for bullish
STOCH_NEUTRAL, STOCH_EXTREME = 35.0, 12.0
SMA_DEV_NEUTRAL, SMA_DEV_EXTREME = 3.0, 9.0  # |% from SMA20|
VOL_NEUTRAL, VOL_EXTREME = 1.0, 1.8
CLOSE_NEUTRAL, CLOSE_EXTREME = 0.50, 0.85    # close position within range
WICK_FULL_RATIO = 0.45                       # wick = 45% of range → full credit

# Trend binding: counter-trend setups get a haircut and can almost never be A+
COUNTER_TREND_MULT = 0.80
COUNTER_TREND_APLUS_CAP = float(APLUS_THRESHOLD)  # counter-trend cannot exceed A+ floor

# Confirmation floor for A+: a genuine reversal needs EITHER a real tail OR a
# volume surge. Without one, the setup is demoted regardless of oscillators.
WICK_CONFIRM_RATIO = 0.20
VOL_CONFIRM_RATIO = 1.3


@dataclass(frozen=True)
class RedDogSignal:
    """Red Dog Reversal signal with continuous A+ scoring."""
    ticker: str
    signal_date: str
    direction: str  # "bullish" or "bearish"

    # Pattern details
    low_a: float           # Prior day's low (bullish) or high (bearish)
    low_b: float           # Intraday extreme (stop level)
    close: float           # Reversal day close
    close_position: float  # 0-1, where close is within day's range

    # Entry/exit levels
    entry_trigger: float   # Buy stop above high (bullish) or sell stop below low (bearish)
    stop_loss: float       # At Low B / High B
    target_1: float        # 1R target
    target_2: float        # 2R target
    target_sma20: float    # Mean reversion target

    # Risk metrics
    risk_dollars: float    # Entry - Stop
    reward_1r: float       # Target 1 - Entry

    # Quality scoring
    score: float           # 0-100 composite score (trend-adjusted)
    grade: str             # "A+", "A", "B", "C"

    # Component scores (for transparency)
    rsi_score: float
    stochastics_score: float
    sma20_deviation_score: float
    volume_score: float
    close_position_score: float
    sr_confluence_score: float

    # Indicator values
    rsi: Optional[float]
    stochastics: Optional[float]
    sma20: Optional[float]
    sma20_deviation_pct: Optional[float]
    volume_ratio: Optional[float]
    atr14: Optional[float]

    # Metadata
    strength: str          # "strong" or "standard"
    notes: List[str]

    # --- Institutional hardening fields (defaults keep back-compat) ---
    base_score: float = 0.0                # pre-trend-adjustment score
    wick_score: float = 0.0                # rejection-tail credit
    wick_ratio: Optional[float] = None     # tail length / bar range
    sr_level: Optional[float] = None       # nearest swing S/R level
    sr_distance_atr: Optional[float] = None
    trend_alignment: str = "unknown"       # aligned | counter | neutral | unknown
    trend_penalty: float = 0.0             # points removed for counter-trend
    confirmed: bool = True                 # passed wick/volume confirmation floor
    dollar_adv: Optional[float] = None     # 20d avg dollar volume (liquidity)
    status: str = "pending"                # lifecycle (see evaluate_outcome)
    verdict: Optional[Dict[str, Any]] = None  # reconciled desk verdict


# ---------------------------------------------------------------------------
# Indicator helpers (kept stable — imported by tests)
# ---------------------------------------------------------------------------

def _compute_sma(values: List[float], period: int) -> Optional[float]:
    """Compute simple moving average."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _compute_stochastics(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
) -> Optional[float]:
    """
    Compute %K of Stochastics oscillator.
    %K = (Close - Lowest Low) / (Highest High - Lowest Low) * 100
    """
    if len(highs) < period or len(lows) < period or len(closes) < period:
        return None

    recent_highs = highs[-period:]
    recent_lows = lows[-period:]
    highest = max(recent_highs)
    lowest = min(recent_lows)
    current_close = closes[-1]

    denom = highest - lowest
    if denom <= 0:
        return 50.0  # Default to neutral if no range

    k = ((current_close - lowest) / denom) * 100.0
    return max(0.0, min(100.0, k))


def _compute_atr(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
) -> Optional[float]:
    """Compute Average True Range."""
    if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1:
        return None

    tr_values: List[float] = []
    for i in range(len(closes) - period, len(closes)):
        if i < 1:
            continue
        h = highs[i]
        l = lows[i]
        pc = closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_values.append(tr)

    if not tr_values:
        return None
    return sum(tr_values) / len(tr_values)


def _compute_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Compute RSI using Wilder smoothing."""
    if len(closes) < period + 1:
        return None

    gains: List[float] = []
    losses: List[float] = []

    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))

    if len(gains) < period:
        return None

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss <= 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def _ramp(x: float, lo: float, hi: float) -> float:
    """Linear ramp returning 0 at `lo` and 1 at `hi`, clamped to [0, 1].

    Supports descending ramps (lo > hi), e.g. RSI where a *lower* value is
    more extreme.
    """
    if x is None:
        return 0.0
    if hi == lo:
        return 1.0 if ((x >= hi) if hi >= lo else (x <= hi)) else 0.0
    t = (x - lo) / (hi - lo)
    return max(0.0, min(1.0, t))


# ---------------------------------------------------------------------------
# Swing-structure support/resistance confluence
# ---------------------------------------------------------------------------

def _swing_levels(values: List[float], *, kind: str, left: int = 2, right: int = 2) -> List[float]:
    """Return pivot highs or lows from a price series.

    A bar is a swing low if it is the strict minimum of the [i-left, i+right]
    window (mirror for highs). The most recent bar is intentionally excluded by
    the caller so confluence is measured against *prior* structure.
    """
    out: List[float] = []
    n = len(values)
    for i in range(left, n - right):
        window = values[i - left:i + right + 1]
        v = values[i]
        if kind == "low" and v == min(window) and window.count(v) == 1:
            out.append(v)
        elif kind == "high" and v == max(window) and window.count(v) == 1:
            out.append(v)
    return out


def compute_sr_confluence(
    bars: List[DailyBar],
    *,
    direction: str,
    pivot_price: float,
    atr: Optional[float],
    lookback: int = 40,
) -> Dict[str, Any]:
    """Score how close the reversal extreme sits to prior swing structure.

    Failed breakdowns that occur right at a prior support shelf (or failed
    breakouts at prior resistance) are far higher quality than ones in open
    air. Returns a 0-1 confluence score plus the nearest level and its
    distance in ATR units.
    """
    result = {"confluence": 0.0, "level": None, "distanceAtr": None}
    if not bars or len(bars) < 6 or not atr or atr <= 0:
        return result

    # Exclude the most recent (reversal) bar from the structure search.
    hist = bars[:-1][-lookback:]
    if direction == "bullish":
        lows = [float(b.low) for b in hist if b.low is not None]
        levels = _swing_levels(lows, kind="low")
    else:
        highs = [float(b.high) for b in hist if b.high is not None]
        levels = _swing_levels(highs, kind="high")

    if not levels:
        return result

    nearest = min(levels, key=lambda lvl: abs(lvl - pivot_price))
    distance_atr = abs(pivot_price - nearest) / atr
    # Full credit at the level, zero credit at >= 1.2 ATR away.
    confluence = _ramp(distance_atr, 1.2, 0.0)

    result["confluence"] = round(confluence, 4)
    result["level"] = round(nearest, 4)
    result["distanceAtr"] = round(distance_atr, 3)
    return result


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------

def detect_red_dog_enhanced(
    bars: List[DailyBar],
    *,
    ticker: str = "",
) -> Dict[str, Any]:
    """
    Enhanced Red Dog detection with full indicator calculation.

    Returns a dict with:
    - enabled: bool
    - bullish / bearish: bool
    - pattern: dict with detailed signal info (incl. wick rejection metrics)
    - indicators: dict with RSI, Stochastics, SMA20, ATR, volume, dollar ADV
    """
    result: Dict[str, Any] = {
        "enabled": False,
        "bullish": False,
        "bearish": False,
        "pattern": None,
        "indicators": {},
        "notes": [],
    }

    if not bars or len(bars) < 21:
        result["notes"].append("Insufficient bars for Red Dog detection (need 21+).")
        return result

    closes = [float(b.close) for b in bars if b.close is not None and b.close > 0]
    highs = [float(b.high) for b in bars if b.high is not None]
    lows = [float(b.low) for b in bars if b.low is not None]
    volumes = [float(b.volume) for b in bars if b.volume is not None and b.volume > 0]

    if len(closes) < 21 or len(highs) < 2 or len(lows) < 2:
        result["notes"].append("Insufficient OHLC data.")
        return result

    result["enabled"] = True

    b1 = bars[-1]  # Today (reversal day)
    b0 = bars[-2]  # Yesterday

    if any(v is None for v in (b1.high, b1.low, b1.close, b1.open, b0.high, b0.low)):
        result["notes"].append("Missing OHLC on recent bars.")
        return result

    h1, l1, c1, o1 = float(b1.high), float(b1.low), float(b1.close), float(b1.open)
    h0, l0 = float(b0.high), float(b0.low)

    rsi = _compute_rsi(closes, period=14)
    stochastics = _compute_stochastics(highs, lows, closes, period=14)
    sma20 = _compute_sma(closes, period=20)
    atr14 = _compute_atr(highs, lows, closes, period=14)

    volume_ratio: Optional[float] = None
    dollar_adv: Optional[float] = None
    if len(volumes) >= 20:
        avg_vol = sum(volumes[-20:]) / 20
        if avg_vol > 0:
            dollar_adv = avg_vol * c1
            if b1.volume is not None and b1.volume > 0:
                volume_ratio = float(b1.volume) / avg_vol

    sma20_deviation_pct: Optional[float] = None
    if sma20 is not None and sma20 > 0:
        sma20_deviation_pct = ((c1 - sma20) / sma20) * 100.0

    result["indicators"] = {
        "rsi": round(rsi, 2) if rsi is not None else None,
        "stochastics": round(stochastics, 2) if stochastics is not None else None,
        "sma20": round(sma20, 4) if sma20 is not None else None,
        "sma20DeviationPct": round(sma20_deviation_pct, 2) if sma20_deviation_pct is not None else None,
        "volumeRatio": round(volume_ratio, 2) if volume_ratio is not None else None,
        "atr14": round(atr14, 4) if atr14 is not None else None,
        "dollarAdv": round(dollar_adv, 0) if dollar_adv is not None else None,
    }

    day_range = max(1e-9, h1 - l1)
    close_position = (c1 - l1) / day_range  # 0 = at low, 1 = at high

    # Wick / rejection metrics on the reversal bar.
    upper_wick = h1 - max(o1, c1)
    lower_wick = min(o1, c1) - l1

    # Bullish Red Dog: today's low < prior low AND today's close > prior low
    bullish = (l1 < l0) and (c1 > l0)
    # Bearish Red Dog: today's high > prior high AND today's close < prior high
    bearish = (h1 > h0) and (c1 < h0)

    result["bullish"] = bool(bullish)
    result["bearish"] = bool(bearish)

    if not bullish and not bearish:
        return result

    direction = "bullish" if bullish else "bearish"

    if bullish:
        wick_ratio = max(0.0, lower_wick) / day_range
        low_a = l0
        low_b = l1
        offset = _entry_offset(atr14)
        entry_trigger = h1 + offset
        stop_loss = l1 - max(0.10, (atr14 or 0) * 0.25)
        risk = entry_trigger - stop_loss
        target_1 = entry_trigger + risk
        target_2 = entry_trigger + (2 * risk)
        target_sma20 = sma20 if sma20 is not None else entry_trigger + risk
    else:  # bearish
        wick_ratio = max(0.0, upper_wick) / day_range
        low_a = h0
        low_b = h1
        offset = _entry_offset(atr14)
        entry_trigger = l1 - offset
        stop_loss = h1 + max(0.10, (atr14 or 0) * 0.25)
        risk = stop_loss - entry_trigger
        target_1 = entry_trigger - risk
        target_2 = entry_trigger - (2 * risk)
        target_sma20 = sma20 if sma20 is not None else entry_trigger - risk

    result["pattern"] = {
        "direction": direction,
        "signalDate": str(b1.trade_date)[:10],
        "lowA": round(low_a, 4),
        "lowB": round(low_b, 4),
        "close": round(c1, 4),
        "closePosition": round(close_position, 4),
        "wickRatio": round(wick_ratio, 4),
        "entryTrigger": round(entry_trigger, 4),
        "stopLoss": round(stop_loss, 4),
        "riskDollars": round(risk, 4),
        "target1": round(target_1, 4),
        "target2": round(target_2, 4),
        "targetSma20": round(target_sma20, 4),
    }

    return result


def _entry_offset(atr: Optional[float]) -> float:
    """Tick/ATR-scaled trigger offset.

    A flat $0.01 trigger fills on noise for high-priced names. Scale the offset
    to ~5% of ATR with a 5-cent floor, rounded to the cent.
    """
    base = max(0.05, round((atr or 0.0) * 0.05, 2))
    return base


# ---------------------------------------------------------------------------
# Continuous scoring
# ---------------------------------------------------------------------------

def score_red_dog_setup(
    *,
    direction: str,
    rsi: Optional[float],
    stochastics: Optional[float],
    sma20_deviation_pct: Optional[float],
    volume_ratio: Optional[float],
    close_position: float,
    wick_quality: float = 0.0,
    sr_confluence: float = 0.0,
    trend_alignment: str = "unknown",
) -> Tuple[float, Dict[str, float], str, Dict[str, Any]]:
    """
    Score a Red Dog setup from 0-100 using continuous ramps.

    Returns:
        (total_score, component_scores, grade, scoring_meta)
    """
    is_bullish = direction == "bullish"

    # RSI — momentum exhaustion
    if rsi is None:
        rsi_credit = 0.0
    elif is_bullish:
        rsi_credit = _ramp(rsi, RSI_NEUTRAL, RSI_EXTREME)
    else:
        rsi_credit = _ramp(rsi, 100.0 - RSI_NEUTRAL, 100.0 - RSI_EXTREME)

    # Stochastics — secondary oscillator
    if stochastics is None:
        stoch_credit = 0.0
    elif is_bullish:
        stoch_credit = _ramp(stochastics, STOCH_NEUTRAL, STOCH_EXTREME)
    else:
        stoch_credit = _ramp(stochastics, 100.0 - STOCH_NEUTRAL, 100.0 - STOCH_EXTREME)

    # SMA20 deviation — stretch from mean (must be correct sign)
    sma_credit = 0.0
    if sma20_deviation_pct is not None:
        if is_bullish and sma20_deviation_pct < 0:
            sma_credit = _ramp(abs(sma20_deviation_pct), SMA_DEV_NEUTRAL, SMA_DEV_EXTREME)
        elif (not is_bullish) and sma20_deviation_pct > 0:
            sma_credit = _ramp(sma20_deviation_pct, SMA_DEV_NEUTRAL, SMA_DEV_EXTREME)

    # Volume — participation on the reversal bar
    vol_credit = _ramp(volume_ratio, VOL_NEUTRAL, VOL_EXTREME) if volume_ratio is not None else 0.0

    # Reversal close — reclaimed into favorable zone
    if is_bullish:
        close_credit = _ramp(close_position, CLOSE_NEUTRAL, CLOSE_EXTREME)
    else:
        close_credit = _ramp(close_position, 1.0 - CLOSE_NEUTRAL, 1.0 - CLOSE_EXTREME)

    wick_credit = max(0.0, min(1.0, wick_quality))
    sr_credit = max(0.0, min(1.0, sr_confluence))

    components: Dict[str, float] = {
        "rsi": round(rsi_credit * W_RSI, 2),
        "stochastics": round(stoch_credit * W_STOCH, 2),
        "sma20Deviation": round(sma_credit * W_SMA, 2),
        "volume": round(vol_credit * W_VOL, 2),
        "closePosition": round(close_credit * W_CLOSE, 2),
        "wick": round(wick_credit * W_WICK, 2),
        "srConfluence": round(sr_credit * W_SR, 2),
    }

    base_score = round(sum(components.values()), 2)

    # --- Trend binding: counter-trend setups are penalized and capped ---
    penalty = 0.0
    total = base_score
    if trend_alignment == "counter":
        total = base_score * COUNTER_TREND_MULT
        penalty = round(base_score - total, 2)
        if total > COUNTER_TREND_APLUS_CAP - 1e-9:
            total = COUNTER_TREND_APLUS_CAP - 1.0  # keep counter-trend below A+ floor
            penalty = round(base_score - total, 2)
    total = round(max(0.0, min(100.0, total)), 2)

    if total >= APLUS_THRESHOLD:
        grade = "A+"
    elif total >= GRADE_A_THRESHOLD:
        grade = "A"
    elif total >= GRADE_B_THRESHOLD:
        grade = "B"
    else:
        grade = "C"

    meta = {
        "baseScore": base_score,
        "trendAlignment": trend_alignment,
        "trendPenalty": penalty,
    }
    return total, components, grade, meta


# ---------------------------------------------------------------------------
# Signal construction
# ---------------------------------------------------------------------------

def _trend_alignment_for(direction: str, trend_direction: Optional[str]) -> str:
    """Map setup direction + SPX trend direction → alignment label."""
    td = (trend_direction or "").lower()
    if td not in ("bullish", "bearish"):
        return "unknown"
    return "aligned" if direction == td else "counter"


def build_red_dog_signal(
    *,
    ticker: str,
    detection: Dict[str, Any],
    bars: Optional[List[DailyBar]] = None,
    trend_direction: Optional[str] = None,
    near_support_resistance: Optional[bool] = None,  # legacy override (tests)
) -> Optional[RedDogSignal]:
    """
    Build a complete RedDogSignal from detection results.

    `bars` enables S/R confluence + wick context; `trend_direction` makes the
    SPX trend read binding on the score.
    """
    if not detection.get("enabled"):
        return None
    if not detection.get("bullish") and not detection.get("bearish"):
        return None

    pattern = detection.get("pattern")
    indicators = detection.get("indicators") or {}
    if not pattern:
        return None

    direction = pattern.get("direction", "")
    atr14 = indicators.get("atr14")
    close_pos = pattern.get("closePosition", 0.5)
    wick_ratio = pattern.get("wickRatio", 0.0)
    wick_quality = max(0.0, min(1.0, (wick_ratio or 0.0) / WICK_FULL_RATIO))

    # Support/Resistance confluence from swing structure (real now).
    sr_conf = 0.0
    sr_level: Optional[float] = None
    sr_dist: Optional[float] = None
    pivot = pattern.get("lowB")
    if near_support_resistance is not None:
        # Legacy boolean override path (kept for explicit callers/tests).
        sr_conf = 1.0 if near_support_resistance else 0.0
    elif bars and pivot is not None:
        sr = compute_sr_confluence(bars, direction=direction, pivot_price=pivot, atr=atr14)
        sr_conf = sr["confluence"]
        sr_level = sr["level"]
        sr_dist = sr["distanceAtr"]

    trend_alignment = _trend_alignment_for(direction, trend_direction)

    total_score, component_scores, grade, meta = score_red_dog_setup(
        direction=direction,
        rsi=indicators.get("rsi"),
        stochastics=indicators.get("stochastics"),
        sma20_deviation_pct=indicators.get("sma20DeviationPct"),
        volume_ratio=indicators.get("volumeRatio"),
        close_position=close_pos,
        wick_quality=wick_quality,
        sr_confluence=sr_conf,
        trend_alignment=trend_alignment,
    )

    # Confirmation floor: a real reversal needs a tail OR a volume surge.
    vol_ratio = indicators.get("volumeRatio")
    confirmed = (wick_ratio or 0.0) >= WICK_CONFIRM_RATIO or (
        vol_ratio is not None and vol_ratio >= VOL_CONFIRM_RATIO
    )
    if not confirmed and grade == "A+":
        grade = "A"  # demote unconfirmed setups out of A+

    if direction == "bullish":
        strength = "strong" if close_pos >= 0.70 else "standard"
    else:
        strength = "strong" if close_pos <= 0.30 else "standard"

    notes: List[str] = []
    if grade == "A+":
        notes.append("A+ setup: multiple confirmation factors aligned.")
    if trend_alignment == "counter":
        notes.append(f"Counter-trend vs SPX — score penalized {meta['trendPenalty']:.0f} pts.")
    if not confirmed:
        notes.append("Weak rejection tail and light volume — confirmation pending.")
    rsi_val = indicators.get("rsi")
    if rsi_val is not None:
        if rsi_val <= 30:
            notes.append(f"RSI oversold at {rsi_val:.1f}")
        elif rsi_val >= 70:
            notes.append(f"RSI overbought at {rsi_val:.1f}")
    if vol_ratio is not None and vol_ratio >= 1.5:
        notes.append(f"Volume surge: {vol_ratio:.1f}x average")
    if sr_conf >= 0.5 and sr_level is not None:
        notes.append(f"S/R confluence near {sr_level:.2f} ({sr_dist:.2f} ATR)")

    return RedDogSignal(
        ticker=ticker,
        signal_date=pattern.get("signalDate", ""),
        direction=direction,
        low_a=pattern.get("lowA", 0),
        low_b=pattern.get("lowB", 0),
        close=pattern.get("close", 0),
        close_position=close_pos,
        entry_trigger=pattern.get("entryTrigger", 0),
        stop_loss=pattern.get("stopLoss", 0),
        target_1=pattern.get("target1", 0),
        target_2=pattern.get("target2", 0),
        target_sma20=pattern.get("targetSma20", 0),
        risk_dollars=pattern.get("riskDollars", 0),
        reward_1r=abs(pattern.get("target1", 0) - pattern.get("entryTrigger", 0)),
        score=total_score,
        grade=grade,
        rsi_score=component_scores.get("rsi", 0.0),
        stochastics_score=component_scores.get("stochastics", 0.0),
        sma20_deviation_score=component_scores.get("sma20Deviation", 0.0),
        volume_score=component_scores.get("volume", 0.0),
        close_position_score=component_scores.get("closePosition", 0.0),
        sr_confluence_score=component_scores.get("srConfluence", 0.0),
        rsi=indicators.get("rsi"),
        stochastics=indicators.get("stochastics"),
        sma20=indicators.get("sma20"),
        sma20_deviation_pct=indicators.get("sma20DeviationPct"),
        volume_ratio=vol_ratio,
        atr14=atr14,
        strength=strength,
        notes=notes,
        base_score=meta["baseScore"],
        wick_score=component_scores.get("wick", 0.0),
        wick_ratio=wick_ratio,
        sr_level=sr_level,
        sr_distance_atr=sr_dist,
        trend_alignment=trend_alignment,
        trend_penalty=meta["trendPenalty"],
        confirmed=confirmed,
        dollar_adv=indicators.get("dollarAdv"),
    )


def signal_to_dict(signal: RedDogSignal) -> Dict[str, Any]:
    """Convert RedDogSignal to API-friendly dict."""
    return {
        "ticker": signal.ticker,
        "signalDate": signal.signal_date,
        "direction": signal.direction,
        "status": signal.status,
        "pattern": {
            "lowA": signal.low_a,
            "lowB": signal.low_b,
            "close": signal.close,
            "closePosition": signal.close_position,
            "wickRatio": signal.wick_ratio,
        },
        "levels": {
            "entryTrigger": signal.entry_trigger,
            "stopLoss": signal.stop_loss,
            "target1": signal.target_1,
            "target2": signal.target_2,
            "targetSma20": signal.target_sma20,
            "riskDollars": signal.risk_dollars,
            "reward1R": signal.reward_1r,
        },
        "quality": {
            "score": signal.score,
            "baseScore": signal.base_score,
            "grade": signal.grade,
            "strength": signal.strength,
            "confirmed": signal.confirmed,
            "trendAlignment": signal.trend_alignment,
            "trendPenalty": signal.trend_penalty,
            "components": {
                "rsi": signal.rsi_score,
                "stochastics": signal.stochastics_score,
                "sma20Deviation": signal.sma20_deviation_score,
                "volume": signal.volume_score,
                "closePosition": signal.close_position_score,
                "wick": signal.wick_score,
                "srConfluence": signal.sr_confluence_score,
            },
        },
        "indicators": {
            "rsi": signal.rsi,
            "stochastics": signal.stochastics,
            "sma20": signal.sma20,
            "sma20DeviationPct": signal.sma20_deviation_pct,
            "volumeRatio": signal.volume_ratio,
            "atr14": signal.atr14,
            "dollarAdv": signal.dollar_adv,
        },
        "srConfluence": {
            "level": signal.sr_level,
            "distanceAtr": signal.sr_distance_atr,
        },
        "verdict": signal.verdict,
        "notes": signal.notes,
    }


# ---------------------------------------------------------------------------
# Deterministic outcome evaluation (powers backtest + live status tracking)
# ---------------------------------------------------------------------------

# Lifecycle vocabulary
STATUS_PENDING = "pending"          # awaiting entry trigger
STATUS_TRIGGERED = "triggered"      # entered, still open
STATUS_TARGET = "target_hit"        # reached 1R+ target
STATUS_STOPPED = "stopped"          # hit protective stop
STATUS_EXPIRED = "expired"          # never triggered within window
STATUS_INVALIDATED = "invalidated"  # structural invalidation before trigger


def evaluate_outcome(
    *,
    direction: str,
    entry_trigger: float,
    stop_loss: float,
    target_1: float,
    forward_bars: List[DailyBar],
    trigger_window: int = 3,
    max_hold: int = 10,
) -> Dict[str, Any]:
    """Walk forward bar-by-bar and resolve a setup's outcome.

    Used identically by the backtest harness (historical bars) and the live
    status tracker (bars since the signal date). Pure and side-effect free.

    Returns a dict with: status, rMultiple, barsHeld, mae (R), mfe (R),
    triggered (bool), exitPrice.
    """
    is_bull = direction == "bullish"
    risk = abs(entry_trigger - stop_loss)
    out: Dict[str, Any] = {
        "status": STATUS_PENDING,
        "triggered": False,
        "rMultiple": 0.0,
        "barsHeld": 0,
        "mae": 0.0,
        "mfe": 0.0,
        "exitPrice": None,
    }
    if risk <= 0 or not forward_bars:
        return out

    triggered = False
    entry_idx = None
    # 1) Find the trigger within the trigger window.
    for i, b in enumerate(forward_bars[:trigger_window]):
        if b.high is None or b.low is None:
            continue
        hi, lo = float(b.high), float(b.low)
        if is_bull and hi >= entry_trigger:
            triggered = True
            entry_idx = i
            break
        if (not is_bull) and lo <= entry_trigger:
            triggered = True
            entry_idx = i
            break

    if not triggered:
        out["status"] = STATUS_EXPIRED
        return out

    out["triggered"] = True
    mae_r = 0.0
    mfe_r = 0.0

    # 2) Manage the open trade from the trigger bar forward.
    for j in range(entry_idx, min(len(forward_bars), entry_idx + max_hold)):
        b = forward_bars[j]
        if b.high is None or b.low is None:
            continue
        hi, lo = float(b.high), float(b.low)
        bars_held = j - entry_idx + 1

        if is_bull:
            fav = (hi - entry_trigger) / risk
            adv = (entry_trigger - lo) / risk
        else:
            fav = (entry_trigger - lo) / risk
            adv = (hi - entry_trigger) / risk
        mfe_r = max(mfe_r, fav)
        mae_r = max(mae_r, adv)

        stop_hit = (lo <= stop_loss) if is_bull else (hi >= stop_loss)
        target_hit = (hi >= target_1) if is_bull else (lo <= target_1)

        # Conservative tie-break: assume stop fills first on a same-bar conflict.
        if stop_hit:
            out.update({
                "status": STATUS_STOPPED, "rMultiple": -1.0, "barsHeld": bars_held,
                "exitPrice": round(stop_loss, 4),
            })
            out["mae"] = round(mae_r, 3)
            out["mfe"] = round(mfe_r, 3)
            return out
        if target_hit:
            out.update({
                "status": STATUS_TARGET, "rMultiple": 1.0, "barsHeld": bars_held,
                "exitPrice": round(target_1, 4),
            })
            out["mae"] = round(mae_r, 3)
            out["mfe"] = round(mfe_r, 3)
            return out

    # 3) Time stop: mark-to-close at the last managed bar.
    last = forward_bars[min(len(forward_bars), entry_idx + max_hold) - 1]
    last_close = float(last.close) if last.close is not None else entry_trigger
    r_mult = ((last_close - entry_trigger) if is_bull else (entry_trigger - last_close)) / risk
    out.update({
        "status": STATUS_TRIGGERED,
        "rMultiple": round(r_mult, 3),
        "barsHeld": min(len(forward_bars), max_hold),
        "exitPrice": round(last_close, 4),
        "mae": round(mae_r, 3),
        "mfe": round(mfe_r, 3),
    })
    return out
