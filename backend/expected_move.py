"""
Expected Move computation using ATM-Forward Straddle (Gold Standard).

This module computes the risk-neutral expected absolute move to the near-dated
expiration using the ATM-forward straddle methodology. It is model-light,
robust intraday, and ties directly to no-arbitrage pricing.

Algorithm:
1. Compute T (yearfrac to expiry) and DF (discount factor e^{-rT})
2. Infer forward F via put-call parity: F(K) = K + (C_mid - P_mid) / DF
3. Interpolate C(F) and P(F) using linear interpolation
4. EAbsMove = (C(F) + P(F)) / DF
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger(__name__)

# Constants
DEFAULT_RISK_FREE_RATE = 0.05  # ~5% annualized
DAYS_PER_YEAR = 365.0
MIN_STRIKES_FOR_FORWARD = 3  # Minimum strikes needed for robust forward inference
SPREAD_QUALITY_THRESHOLD = 0.30  # Max spread/mid ratio for "good" quotes
SPOT_BAND_PCT = 0.10  # ±10% around spot for liquid strike selection


@dataclass
class StrikeQuote:
    """Represents a single strike's call/put quotes."""
    strike: float
    call_bid: Optional[float]
    call_ask: Optional[float]
    put_bid: Optional[float]
    put_ask: Optional[float]
    call_mid: Optional[float] = None
    put_mid: Optional[float] = None
    call_spread: Optional[float] = None
    put_spread: Optional[float] = None
    
    def __post_init__(self):
        # Compute mid prices
        if self.call_bid is not None and self.call_ask is not None:
            self.call_mid = (self.call_bid + self.call_ask) / 2.0
            self.call_spread = self.call_ask - self.call_bid
        if self.put_bid is not None and self.put_ask is not None:
            self.put_mid = (self.put_bid + self.put_ask) / 2.0
            self.put_spread = self.put_ask - self.put_bid
    
    def is_usable(self) -> bool:
        """Check if this strike has usable quotes for forward inference."""
        return (
            self.call_mid is not None and self.call_mid > 0 and
            self.put_mid is not None and self.put_mid > 0 and
            self.call_bid is not None and self.call_bid > 0 and
            self.put_bid is not None and self.put_bid > 0
        )
    
    def total_spread(self) -> float:
        """Sum of call + put spreads."""
        cs = self.call_spread if self.call_spread is not None else 0.0
        ps = self.put_spread if self.put_spread is not None else 0.0
        return cs + ps


def _to_float(v: Any) -> Optional[float]:
    """Safely convert a value to float."""
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _yearfrac(t0: dt.date, t_exp: dt.date) -> float:
    """Compute year fraction (ACT/365)."""
    days = (t_exp - t0).days
    return max(days, 0) / DAYS_PER_YEAR


def _discount_factor(r: float, T: float) -> float:
    """Compute discount factor DF = e^{-rT}."""
    return math.exp(-r * T)


def _weighted_median(values: List[Tuple[float, float]]) -> Optional[float]:
    """
    Compute weighted median of (value, weight) pairs.
    
    Args:
        values: List of (value, weight) tuples
    
    Returns:
        Weighted median value, or None if empty
    """
    if not values:
        return None
    
    # Filter out zero/negative weights
    filtered = [(v, w) for v, w in values if w > 0]
    if not filtered:
        return None
    
    # Sort by value
    sorted_vals = sorted(filtered, key=lambda x: x[0])
    
    total_weight = sum(w for _, w in sorted_vals)
    if total_weight <= 0:
        return None
    
    cumulative = 0.0
    half_weight = total_weight / 2.0
    
    for i, (val, weight) in enumerate(sorted_vals):
        cumulative += weight
        if cumulative >= half_weight:
            return val
    
    # Fallback to last value
    return sorted_vals[-1][0]


def _linear_interp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    """Linear interpolation: y = y0 + (x - x0) * (y1 - y0) / (x1 - x0)."""
    if abs(x1 - x0) < 1e-10:
        return (y0 + y1) / 2.0
    return y0 + (x - x0) * (y1 - y0) / (x1 - x0)


def _parse_strikes_from_live_rows(rows: List[dict], spot: float) -> List[StrikeQuote]:
    """
    Parse strike quotes from ORATS live strikes response rows.
    
    ORATS live strikes fields:
    - strike: strike price
    - callBidPrice, callAskPrice: call bid/ask
    - putBidPrice, putAskPrice: put bid/ask
    """
    quotes: List[StrikeQuote] = []
    
    for row in rows:
        strike = _to_float(row.get("strike"))
        if strike is None or strike <= 0:
            continue
        
        # Skip strikes too far from spot (outside ±SPOT_BAND_PCT)
        if spot > 0:
            pct_from_spot = abs(strike - spot) / spot
            if pct_from_spot > SPOT_BAND_PCT:
                continue
        
        call_bid = _to_float(row.get("callBidPrice"))
        call_ask = _to_float(row.get("callAskPrice"))
        put_bid = _to_float(row.get("putBidPrice"))
        put_ask = _to_float(row.get("putAskPrice"))
        
        quote = StrikeQuote(
            strike=strike,
            call_bid=call_bid,
            call_ask=call_ask,
            put_bid=put_bid,
            put_ask=put_ask,
        )
        
        if quote.is_usable():
            quotes.append(quote)
    
    return sorted(quotes, key=lambda q: q.strike)


def _infer_forward_price(
    quotes: List[StrikeQuote],
    df: float,
) -> Tuple[Optional[float], int, List[str]]:
    """
    Infer forward price F via put-call parity across strikes.
    
    For each strike K: F(K) = K + (C_mid - P_mid) / DF
    
    Returns weighted median of F(K) using weight = 1 / total_spread.
    
    Args:
        quotes: List of usable strike quotes
        df: Discount factor
    
    Returns:
        (forward_price, strikes_used, warnings)
    """
    warnings: List[str] = []
    
    if not quotes:
        warnings.append("No usable strikes for forward inference.")
        return None, 0, warnings
    
    if df <= 0:
        warnings.append("Invalid discount factor.")
        return None, 0, warnings
    
    f_candidates: List[Tuple[float, float]] = []  # (F(K), weight)
    
    for q in quotes:
        if q.call_mid is None or q.put_mid is None:
            continue
        
        # F(K) = K + (C_mid - P_mid) / DF
        f_k = q.strike + (q.call_mid - q.put_mid) / df
        
        # Weight by inverse spread (tighter spreads get more weight)
        total_spread = q.total_spread()
        if total_spread > 0:
            weight = 1.0 / total_spread
        else:
            weight = 1.0
        
        # Quality filter: skip strikes with very wide spreads relative to mid
        avg_mid = (q.call_mid + q.put_mid) / 2.0
        if avg_mid > 0 and total_spread / avg_mid > SPREAD_QUALITY_THRESHOLD * 2:
            continue
        
        f_candidates.append((f_k, weight))
    
    if len(f_candidates) < MIN_STRIKES_FOR_FORWARD:
        warnings.append(f"Only {len(f_candidates)} strikes available for forward (need {MIN_STRIKES_FOR_FORWARD}).")
        if not f_candidates:
            return None, 0, warnings
    
    forward = _weighted_median(f_candidates)
    return forward, len(f_candidates), warnings


def _interpolate_atm_straddle(
    quotes: List[StrikeQuote],
    forward: float,
) -> Tuple[Optional[float], Optional[float], List[str]]:
    """
    Interpolate call and put prices at strike = forward.
    
    Finds K_L (largest strike <= F) and K_U (smallest strike >= F),
    then linearly interpolates C(F) and P(F).
    
    Returns:
        (call_at_forward, put_at_forward, warnings)
    """
    warnings: List[str] = []
    
    if not quotes:
        warnings.append("No quotes for ATM interpolation.")
        return None, None, warnings
    
    # Find bracketing strikes
    k_l: Optional[StrikeQuote] = None
    k_u: Optional[StrikeQuote] = None
    
    for q in quotes:
        if q.strike <= forward:
            if k_l is None or q.strike > k_l.strike:
                k_l = q
        if q.strike >= forward:
            if k_u is None or q.strike < k_u.strike:
                k_u = q
    
    if k_l is None or k_u is None:
        warnings.append("Cannot bracket forward price with available strikes.")
        # Try to use nearest strike
        if quotes:
            nearest = min(quotes, key=lambda q: abs(q.strike - forward))
            return nearest.call_mid, nearest.put_mid, warnings
        return None, None, warnings
    
    # Handle exact match
    if abs(k_l.strike - k_u.strike) < 0.01:
        return k_l.call_mid, k_l.put_mid, warnings
    
    # Linear interpolation
    if k_l.call_mid is None or k_u.call_mid is None:
        warnings.append("Missing call mid prices for interpolation.")
        return None, None, warnings
    
    if k_l.put_mid is None or k_u.put_mid is None:
        warnings.append("Missing put mid prices for interpolation.")
        return None, None, warnings
    
    c_f = _linear_interp(forward, k_l.strike, k_u.strike, k_l.call_mid, k_u.call_mid)
    p_f = _linear_interp(forward, k_l.strike, k_u.strike, k_l.put_mid, k_u.put_mid)
    
    return c_f, p_f, warnings


def compute_expected_move_from_chain(
    rows: List[dict],
    *,
    spot: float,
    expiry: dt.date,
    as_of: dt.date,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> Dict[str, Any]:
    """
    Compute expected move from option chain data.
    
    This is the core algorithm implementing the ATM-forward straddle method.
    
    Args:
        rows: ORATS live/hist strikes rows with bid/ask data
        spot: Current spot price
        expiry: Expiration date
        as_of: Current date (for T calculation)
        risk_free_rate: Annualized risk-free rate
    
    Returns:
        Dict with expected move data or error info
    """
    warnings: List[str] = []
    
    result: Dict[str, Any] = {
        "asOfDate": as_of.isoformat(),
        "expiry": expiry.isoformat(),
        "dte": (expiry - as_of).days,
        "source": "chain",
        "spotPrice": round(spot, 2) if spot else None,
        "forwardPrice": None,
        "straddlePV": None,
        "expectedMoveDollars": None,
        "expectedMovePct": None,
        "discountFactor": None,
        "riskFreeRate": risk_free_rate,
        "strikesUsedForForward": 0,
        "warnings": [],
    }
    
    # Step 0: Compute T and DF
    T = _yearfrac(as_of, expiry)
    if T <= 0:
        result["warnings"] = ["Expiry is in the past or today."]
        return result
    
    df = _discount_factor(risk_free_rate, T)
    result["discountFactor"] = round(df, 6)
    
    # Parse strike quotes
    quotes = _parse_strikes_from_live_rows(rows, spot)
    if not quotes:
        result["warnings"] = ["No usable strike quotes found in chain."]
        return result
    
    # Step 1: Infer forward price
    forward, strikes_used, fwd_warnings = _infer_forward_price(quotes, df)
    warnings.extend(fwd_warnings)
    result["strikesUsedForForward"] = strikes_used
    
    if forward is None:
        result["warnings"] = warnings
        return result
    
    result["forwardPrice"] = round(forward, 2)
    
    # Step 2: Interpolate ATM straddle
    c_f, p_f, interp_warnings = _interpolate_atm_straddle(quotes, forward)
    warnings.extend(interp_warnings)
    
    if c_f is None or p_f is None:
        result["warnings"] = warnings
        return result
    
    # Step 3: Compute expected absolute move
    straddle_pv = c_f + p_f
    e_abs_move = straddle_pv / df
    
    result["straddlePV"] = round(straddle_pv, 4)
    result["expectedMoveDollars"] = round(e_abs_move, 2)
    
    # Express as percentage of spot
    if spot and spot > 0:
        result["expectedMovePct"] = round((e_abs_move / spot) * 100, 2)
    
    result["warnings"] = warnings
    return result


def compute_expected_move(
    client,  # OratsClient
    *,
    ticker: str,
    expiry: Optional[str] = None,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    as_of_date: Optional[dt.date] = None,
) -> Dict[str, Any]:
    """
    Compute expected move for a ticker using ORATS live/EOD data.
    
    Attempts to use live strikes data first, falls back to EOD if unavailable.
    
    Args:
        client: OratsClient instance
        ticker: Stock ticker symbol
        expiry: Target expiry date (YYYY-MM-DD), or None for nearest weekly
        risk_free_rate: Annualized risk-free rate
        as_of_date: Override current date (for testing)
    
    Returns:
        Dict with expected move data
    """
    t = str(ticker).strip().upper()
    today = as_of_date or dt.date.today()
    warnings: List[str] = []
    
    base_result: Dict[str, Any] = {
        "ticker": t,
        "asOfDate": today.isoformat(),
        "expiry": None,
        "dte": None,
        "source": None,
        "spotPrice": None,
        "forwardPrice": None,
        "straddlePV": None,
        "expectedMoveDollars": None,
        "expectedMovePct": None,
        "discountFactor": None,
        "riskFreeRate": risk_free_rate,
        "strikesUsedForForward": 0,
        "warnings": [],
    }
    
    # Determine target expiry
    exp_date: Optional[dt.date] = None
    if expiry:
        try:
            exp_date = dt.date.fromisoformat(str(expiry)[:10])
        except ValueError:
            warnings.append(f"Invalid expiry format: {expiry}")
    
    # If no expiry specified, find nearest Friday (standard weekly)
    if exp_date is None:
        # Find next Friday
        days_until_friday = (4 - today.weekday()) % 7
        if days_until_friday == 0:
            days_until_friday = 7  # If today is Friday, use next Friday
        exp_date = today + dt.timedelta(days=days_until_friday)
    
    base_result["expiry"] = exp_date.isoformat()
    base_result["dte"] = (exp_date - today).days
    
    # Try live data first
    spot: Optional[float] = None
    rows: List[dict] = []
    source = "unknown"
    
    # Attempt 1: Live strikes
    try:
        if callable(getattr(client, "live_strikes", None)):
            fields = (
                "ticker,tradeDate,expirDate,strike,spotPrice,stockPrice,"
                "callBidPrice,callAskPrice,putBidPrice,putAskPrice,"
                "callOpenInterest,putOpenInterest"
            )
            resp = client.live_strikes(ticker=t, fields=fields)
            all_rows = [r for r in (resp.rows or []) if isinstance(r, dict)]
            
            # Filter to target expiry
            exp_str = exp_date.isoformat()
            rows = [
                r for r in all_rows
                if str(r.get("expirDate") or r.get("expiry") or "")[:10] == exp_str
            ]
            
            # Get spot from rows
            if all_rows:
                for r in all_rows:
                    s = _to_float(r.get("spotPrice")) or _to_float(r.get("stockPrice"))
                    if s and s > 0:
                        spot = s
                        break
            
            if rows:
                source = "live"
                LOG.debug(f"[{t}] Got {len(rows)} live strikes for expiry {exp_str}")
    except Exception as e:
        warnings.append(f"Live strikes unavailable: {type(e).__name__}")
        LOG.debug(f"[{t}] Live strikes failed: {e}")
    
    # Attempt 2: Fall back to hist_strikes if live is empty
    if not rows:
        try:
            fields = (
                "ticker,tradeDate,expirDate,strike,stockPrice,"
                "callBidPrice,callAskPrice,putBidPrice,putAskPrice"
            )
            # Use yesterday's trade date for EOD data
            trade_date = (today - dt.timedelta(days=1)).isoformat()
            for step in range(0, 5):  # Try up to 5 days back
                td = (today - dt.timedelta(days=1 + step)).isoformat()
                resp = client.hist_strikes(
                    ticker=t,
                    trade_date=td,
                    fields=fields,
                    dte=f"{max(1, base_result['dte'] - 2)},{base_result['dte'] + 7}",
                )
                all_rows = [r for r in (resp.rows or []) if isinstance(r, dict)]
                
                # Filter to target expiry (or nearest)
                exp_str = exp_date.isoformat()
                rows = [
                    r for r in all_rows
                    if str(r.get("expirDate") or r.get("expiry") or "")[:10] == exp_str
                ]
                
                if not rows and all_rows:
                    # Use nearest expiry if exact match not found
                    available_expiries = set(
                        str(r.get("expirDate") or r.get("expiry") or "")[:10]
                        for r in all_rows
                    )
                    available_expiries.discard("")
                    if available_expiries:
                        nearest = min(available_expiries, key=lambda e: abs((dt.date.fromisoformat(e) - exp_date).days))
                        rows = [
                            r for r in all_rows
                            if str(r.get("expirDate") or r.get("expiry") or "")[:10] == nearest
                        ]
                        base_result["expiry"] = nearest
                        base_result["dte"] = (dt.date.fromisoformat(nearest) - today).days
                        warnings.append(f"Using nearest expiry {nearest} (requested {exp_str})")
                
                if rows:
                    # Get spot from rows
                    for r in all_rows:
                        s = _to_float(r.get("stockPrice"))
                        if s and s > 0:
                            spot = s
                            break
                    source = "eod"
                    base_result["asOfDate"] = td
                    LOG.debug(f"[{t}] Got {len(rows)} hist strikes for expiry from {td}")
                    break
        except Exception as e:
            warnings.append(f"Hist strikes unavailable: {type(e).__name__}")
            LOG.debug(f"[{t}] Hist strikes failed: {e}")
    
    # Attempt 3: Fall back to impErnMv from cores
    if not rows or spot is None:
        try:
            # Try live summaries for spot
            if callable(getattr(client, "live_summaries", None)):
                resp = client.live_summaries(ticker=t)
                if resp.rows:
                    row = resp.rows[0]
                    s = _to_float(row.get("spotPrice")) or _to_float(row.get("stockPrice"))
                    if s and s > 0:
                        spot = s
        except Exception:
            pass
        
        # Try cores for impErnMv as final fallback
        if not rows:
            try:
                for step in range(0, 5):
                    td = (today - dt.timedelta(days=step)).isoformat()
                    resp = client.hist_cores(
                        ticker=t,
                        trade_date=td,
                        fields="ticker,tradeDate,stockPrice,impErnMv",
                    )
                    if resp.rows:
                        row = resp.rows[0]
                        imp_ern_mv = _to_float(row.get("impErnMv"))
                        sp = _to_float(row.get("stockPrice"))
                        
                        if imp_ern_mv is not None and sp and sp > 0:
                            # impErnMv is already the expected move percentage
                            base_result["source"] = "impErnMv"
                            base_result["spotPrice"] = round(sp, 2)
                            base_result["expectedMovePct"] = round(imp_ern_mv * 100, 2) if imp_ern_mv < 1 else round(imp_ern_mv, 2)
                            base_result["expectedMoveDollars"] = round(sp * (base_result["expectedMovePct"] / 100), 2)
                            base_result["asOfDate"] = td
                            base_result["warnings"] = warnings + ["Using impErnMv fallback (no live/hist chain)."]
                            return base_result
                        break
            except Exception as e:
                warnings.append(f"Cores fallback failed: {type(e).__name__}")
    
    if not rows:
        base_result["warnings"] = warnings + ["No option chain data available."]
        return base_result
    
    if spot is None or spot <= 0:
        base_result["warnings"] = warnings + ["Could not determine spot price."]
        return base_result
    
    # Compute expected move from chain
    result = compute_expected_move_from_chain(
        rows,
        spot=spot,
        expiry=dt.date.fromisoformat(base_result["expiry"]),
        as_of=today,
        risk_free_rate=risk_free_rate,
    )
    
    # Merge with base result
    result["ticker"] = t
    result["source"] = source
    result["warnings"] = warnings + (result.get("warnings") or [])
    
    return result


def compute_strike_targets(
    expected_move_pct: float,
    spot_price: float,
) -> Dict[str, Any]:
    """
    Compute strike targets based on expected move.
    
    White Box: EM% × spot × 2
    Blue Box: White × 1.5
    Red Box: White × 2
    
    Args:
        expected_move_pct: Expected move as percentage (e.g., 2.5 for 2.5%)
        spot_price: Current spot price
    
    Returns:
        Dict with strike target distances in points
    """
    # Convert percentage to decimal
    em_decimal = expected_move_pct / 100.0
    
    # White box: EM converted to points × 2
    white_pts = spot_price * em_decimal * 2
    
    # Blue box: White × 1.5
    blue_pts = white_pts * 1.5
    
    # Red box: White × 2
    red_pts = white_pts * 2
    
    return {
        "whitePts": round(white_pts, 2),
        "bluePts": round(blue_pts, 2),
        "redPts": round(red_pts, 2),
        "whiteMultiple": 1.0,
        "blueMultiple": 1.5,
        "redMultiple": 2.0,
        "basedOnEmPct": expected_move_pct,
        "basedOnSpot": spot_price,
    }


# =============================================================================
# EARNINGS HOLD RISK MODULE
# =============================================================================
#
# This module implements the Earnings Hold Risk Extension as specified in:
# engine_1_earnings_hold_risk_master_plan.md
#
# Purpose: Quantify close-based breach probabilities for earnings day and
# the following day, including conditional flat open logic and post-event
# drift risk. This is informational risk analytics only - no trade gating.
#
# Time Anchors:
#   PC = Prior Close: close of trading day before earnings
#   EO = Earnings Day Open: market open on earnings day
#   EC = Earnings Day Close: close of earnings day session
#   NC = Next Day Close: close of trading day following earnings
#
# =============================================================================


@dataclass
class HoldRiskEvent:
    """
    Represents a single historical earnings event with all price anchors
    required for hold risk calculations.
    
    All prices are in dollars. EM is the implied expected move percentage.
    """
    earn_date: str  # YYYY-MM-DD
    timing: str  # AMC, BMO, or UNK
    prior_close: Optional[float]  # PC
    earnings_day_open: Optional[float]  # EO
    earnings_day_close: Optional[float]  # EC
    next_day_close: Optional[float]  # NC
    expected_move_pct: Optional[float]  # EM as percentage (e.g., 5.0 for 5%)
    
    def is_valid_for_unconditional(self) -> bool:
        """Check if this event has all required fields for unconditional breach metrics."""
        return (
            self.prior_close is not None and self.prior_close > 0 and
            self.earnings_day_close is not None and
            self.expected_move_pct is not None and self.expected_move_pct > 0
        )
    
    def is_valid_for_conditional(self) -> bool:
        """Check if this event has all required fields for conditional (flat open) metrics."""
        return (
            self.is_valid_for_unconditional() and
            self.earnings_day_open is not None
        )
    
    def is_valid_for_next_day(self) -> bool:
        """Check if this event has next day close for extended metrics."""
        return (
            self.is_valid_for_unconditional() and
            self.next_day_close is not None
        )
    
    def is_valid_for_drift(self) -> bool:
        """Check if this event has all fields for drift metrics."""
        return (
            self.earnings_day_open is not None and
            self.earnings_day_close is not None and
            self.next_day_close is not None and
            self.expected_move_pct is not None and self.expected_move_pct > 0
        )


@dataclass
class BreachRateResult:
    """
    Result container for breach rate calculations.
    
    Always exposes sample_size alongside the rate per master plan requirements.
    Rates are expressed as decimals (0.0 to 1.0), not percentages.
    """
    rate: Optional[float]  # Breach rate as decimal (None if insufficient data)
    sample_size: int  # Number of events used in calculation
    breach_count: int  # Number of breaches observed
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict with rate as decimal."""
        return {
            "rate": round(self.rate, 6) if self.rate is not None else None,
            "sample_size": self.sample_size,
            "breach_count": self.breach_count,
        }


# K-multiples used for all breach calculations per master plan
HOLD_RISK_K_VALUES: Tuple[float, ...] = (1.0, 1.5, 2.0)

# Default flat open gate per master plan: abs(EO - PC) <= 0.25 * EM
DEFAULT_FLAT_OPEN_GATE: float = 0.25


def _compute_breach(
    baseline: float,
    target: float,
    em_pct: float,
    k: float,
) -> bool:
    """
    Determine if a breach occurred.
    
    Breach condition: abs(target - baseline) >= k * EM
    
    Args:
        baseline: Reference price (e.g., prior close)
        target: Target price (e.g., earnings day close)
        em_pct: Expected move as percentage (e.g., 5.0 for 5%)
        k: Multiple of EM for breach threshold
    
    Returns:
        True if breach occurred, False otherwise
    """
    if baseline <= 0 or em_pct <= 0:
        return False
    
    # Convert EM percentage to price threshold
    em_decimal = em_pct / 100.0
    threshold = baseline * em_decimal * k
    
    # Compute absolute move
    abs_move = abs(target - baseline)
    
    return abs_move >= threshold


def _is_flat_open(
    prior_close: float,
    earnings_open: float,
    em_pct: float,
    gate: float = DEFAULT_FLAT_OPEN_GATE,
) -> bool:
    """
    Check if the earnings day open qualifies as "flat" per the gate condition.
    
    Flat open condition: abs(EO - PC) <= gate * EM
    
    Args:
        prior_close: PC price
        earnings_open: EO price
        em_pct: Expected move as percentage
        gate: Multiple of EM for flat threshold (default 0.25)
    
    Returns:
        True if open is flat, False otherwise
    """
    if prior_close <= 0 or em_pct <= 0:
        return False
    
    em_decimal = em_pct / 100.0
    threshold = prior_close * em_decimal * gate
    abs_gap = abs(earnings_open - prior_close)
    
    return abs_gap <= threshold


def compute_breach_rate(
    events: List[HoldRiskEvent],
    baseline_field: str,
    target_field: str,
    k: float,
) -> BreachRateResult:
    """
    Compute breach rate for a set of events at a given k-multiple.
    
    This is a reusable, deterministic helper that returns both breach rate
    and sample size per master plan requirements.
    
    Args:
        events: List of HoldRiskEvent objects
        baseline_field: Field name for baseline price ('prior_close', 'earnings_day_open', 'earnings_day_close')
        target_field: Field name for target price ('earnings_day_close', 'next_day_close')
        k: Multiple of EM for breach threshold
    
    Returns:
        BreachRateResult with rate, sample_size, and breach_count
    """
    valid_events = []
    
    for ev in events:
        baseline = getattr(ev, baseline_field, None)
        target = getattr(ev, target_field, None)
        em = ev.expected_move_pct
        
        if baseline is not None and baseline > 0 and target is not None and em is not None and em > 0:
            valid_events.append((baseline, target, em))
    
    if not valid_events:
        return BreachRateResult(rate=None, sample_size=0, breach_count=0)
    
    breach_count = sum(
        1 for baseline, target, em in valid_events
        if _compute_breach(baseline, target, em, k)
    )
    
    sample_size = len(valid_events)
    rate = breach_count / sample_size if sample_size > 0 else None
    
    return BreachRateResult(rate=rate, sample_size=sample_size, breach_count=breach_count)


def filter_flat_open_events(
    events: List[HoldRiskEvent],
    gate: float = DEFAULT_FLAT_OPEN_GATE,
) -> List[HoldRiskEvent]:
    """
    Filter events to only those that pass the flat open gate.
    
    Flat open gate: abs(EO - PC) <= gate * EM
    
    Args:
        events: List of HoldRiskEvent objects
        gate: Multiple of EM for flat threshold (default 0.25)
    
    Returns:
        Filtered list of events where the open was flat
    """
    flat_events = []
    
    for ev in events:
        if not ev.is_valid_for_conditional():
            continue
        
        if _is_flat_open(
            prior_close=ev.prior_close,
            earnings_open=ev.earnings_day_open,
            em_pct=ev.expected_move_pct,
            gate=gate,
        ):
            flat_events.append(ev)
    
    return flat_events


def compute_unconditional_breach_rates(
    events: List[HoldRiskEvent],
    k_values: Tuple[float, ...] = HOLD_RISK_K_VALUES,
) -> Dict[str, Dict[str, BreachRateResult]]:
    """
    Compute unconditional close breach rates.
    
    Baseline: Prior Close (PC)
    
    Metrics:
    - earnings_close: abs(EC - PC) >= k * EM
    - next_day_close: abs(NC - PC) >= k * EM
    
    Args:
        events: List of HoldRiskEvent objects
        k_values: Tuple of k-multiples to compute
    
    Returns:
        Dict with 'earnings_close' and 'next_day_close' keys,
        each containing a dict of k -> BreachRateResult
    """
    results: Dict[str, Dict[str, BreachRateResult]] = {
        "earnings_close": {},
        "next_day_close": {},
    }
    
    # Filter to events valid for unconditional metrics
    valid_ec = [ev for ev in events if ev.is_valid_for_unconditional()]
    valid_nc = [ev for ev in events if ev.is_valid_for_next_day()]
    
    for k in k_values:
        k_str = str(k)
        
        # Earnings Day Close vs Prior Close
        results["earnings_close"][k_str] = compute_breach_rate(
            valid_ec, "prior_close", "earnings_day_close", k
        )
        
        # Next Day Close vs Prior Close
        results["next_day_close"][k_str] = compute_breach_rate(
            valid_nc, "prior_close", "next_day_close", k
        )
    
    return results


def compute_conditional_breach_rates(
    events: List[HoldRiskEvent],
    gate: float = DEFAULT_FLAT_OPEN_GATE,
    k_values: Tuple[float, ...] = HOLD_RISK_K_VALUES,
) -> Dict[str, Dict[str, BreachRateResult]]:
    """
    Compute conditional breach rates for events with flat opens.
    
    This answers the trading question: "If earnings gap is flat,
    what's the probability of breach by close?"
    
    Filter: abs(EO - PC) <= gate * EM
    Baseline: Prior Close (PC)
    
    Args:
        events: List of HoldRiskEvent objects
        gate: Flat open gate (default 0.25)
        k_values: Tuple of k-multiples to compute
    
    Returns:
        Dict with 'earnings_close' and 'next_day_close' keys,
        each containing a dict of k -> BreachRateResult
    """
    flat_events = filter_flat_open_events(events, gate)
    
    results: Dict[str, Dict[str, BreachRateResult]] = {
        "earnings_close": {},
        "next_day_close": {},
    }
    
    # Filter flat events for each metric
    valid_ec = [ev for ev in flat_events if ev.is_valid_for_unconditional()]
    valid_nc = [ev for ev in flat_events if ev.is_valid_for_next_day()]
    
    for k in k_values:
        k_str = str(k)
        
        # Earnings Day Close vs Prior Close (conditional on flat open)
        results["earnings_close"][k_str] = compute_breach_rate(
            valid_ec, "prior_close", "earnings_day_close", k
        )
        
        # Next Day Close vs Prior Close (conditional on flat open)
        results["next_day_close"][k_str] = compute_breach_rate(
            valid_nc, "prior_close", "next_day_close", k
        )
    
    return results


def compute_drift_rates(
    events: List[HoldRiskEvent],
    k_values: Tuple[float, ...] = HOLD_RISK_K_VALUES,
) -> Dict[str, Dict[str, BreachRateResult]]:
    """
    Compute post-event drift rates.
    
    These metrics rebase risk once the earnings gap is known
    and implied volatility has collapsed.
    
    Metrics:
    - earnings_intraday: abs(EC - EO) >= k * EM (baseline: EO)
    - next_day: abs(NC - EC) >= k * EM (baseline: EC)
    
    Args:
        events: List of HoldRiskEvent objects
        k_values: Tuple of k-multiples to compute
    
    Returns:
        Dict with 'earnings_intraday' and 'next_day' keys,
        each containing a dict of k -> BreachRateResult
    """
    results: Dict[str, Dict[str, BreachRateResult]] = {
        "earnings_intraday": {},
        "next_day": {},
    }
    
    # Filter to events valid for drift metrics
    valid_drift = [ev for ev in events if ev.is_valid_for_drift()]
    
    for k in k_values:
        k_str = str(k)
        
        # Earnings Intraday Drift: EC - EO
        results["earnings_intraday"][k_str] = compute_breach_rate(
            valid_drift, "earnings_day_open", "earnings_day_close", k
        )
        
        # Next Day Drift: NC - EC
        results["next_day"][k_str] = compute_breach_rate(
            valid_drift, "earnings_day_close", "next_day_close", k
        )
    
    return results


def _rates_to_schema(
    rates_dict: Dict[str, BreachRateResult],
) -> Dict[str, Optional[float]]:
    """
    Convert BreachRateResult dict to master plan schema format.
    
    Schema format: {"1.0": rate, "1.5": rate, "2.0": rate}
    Rates are expressed as decimals (0.0 to 1.0).
    """
    return {
        k: (round(v.rate, 6) if v.rate is not None else None)
        for k, v in rates_dict.items()
    }


def compute_earnings_hold_risk(
    events: List[HoldRiskEvent],
    flat_open_gate: float = DEFAULT_FLAT_OPEN_GATE,
    k_values: Tuple[float, ...] = HOLD_RISK_K_VALUES,
    em_source: str = "ORATS_EARNINGS_IMPLIED",
    lookback_label: str = "36_events",
) -> Dict[str, Any]:
    """
    Compute the full earnings hold risk payload per master plan schema.
    
    This is the main entry point for Engine1 integration. It computes all
    metric groups and packages them into the specified output schema.
    
    Args:
        events: List of HoldRiskEvent objects with all price anchors
        flat_open_gate: Multiple of EM for flat open condition (default 0.25)
        k_values: Tuple of k-multiples to compute (default 1.0, 1.5, 2.0)
        em_source: Label for the EM source used
        lookback_label: Label for the lookback window (e.g., "36_events")
    
    Returns:
        Dict matching the master plan earnings_hold_risk schema:
        {
            "em_source": str,
            "flat_open_gate": float,
            "lookback": str,
            "sample_size": {"unconditional": N1, "flat_open": N2},
            "unconditional": {...},
            "conditional_flat_open": {...},
            "drift": {...}
        }
    """
    # Compute all metric groups
    unconditional = compute_unconditional_breach_rates(events, k_values)
    conditional = compute_conditional_breach_rates(events, flat_open_gate, k_values)
    drift = compute_drift_rates(events, k_values)
    
    # Determine sample sizes
    # Unconditional sample size = events valid for EC breach
    valid_unconditional = [ev for ev in events if ev.is_valid_for_unconditional()]
    unconditional_sample_size = len(valid_unconditional)
    
    # Flat open sample size = events that passed the flat open gate
    flat_events = filter_flat_open_events(events, flat_open_gate)
    flat_open_sample_size = len(flat_events)
    
    # Build the output schema per master plan
    result: Dict[str, Any] = {
        "em_source": em_source,
        "flat_open_gate": flat_open_gate,
        "lookback": lookback_label,
        "sample_size": {
            "unconditional": unconditional_sample_size,
            "flat_open": flat_open_sample_size,
        },
        "unconditional": {
            "earnings_close": _rates_to_schema(unconditional["earnings_close"]),
            "next_day_close": _rates_to_schema(unconditional["next_day_close"]),
        },
        "conditional_flat_open": {
            "earnings_close": _rates_to_schema(conditional["earnings_close"]),
            "next_day_close": _rates_to_schema(conditional["next_day_close"]),
        },
        "drift": {
            "earnings_intraday": _rates_to_schema(drift["earnings_intraday"]),
            "next_day": _rates_to_schema(drift["next_day"]),
        },
    }
    
    return result
