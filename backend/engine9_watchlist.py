"""
Engine 9 — Credit Stress Drift: Watchlist & Forced Seller Map

Tiered instrument universe with per-ticker scoring, fragility ranking,
and options skew integration via ORATS.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Instrument Universe
# ---------------------------------------------------------------------------

TIER_1_BDCS = ["ARCC", "OBDC", "FSK", "BXSL", "MAIN", "GBDC"]
TIER_2_ALT_MANAGERS = ["OWL", "APO", "ARES", "KKR", "BX", "BAM"]
TIER_3_CREDIT_ETFS = ["HYG", "JNK", "LQD", "BKLN"]
TIER_4_VOL_HEDGES = ["UVXY", "SPY"]

ALL_TICKERS = TIER_1_BDCS + TIER_2_ALT_MANAGERS + TIER_3_CREDIT_ETFS + TIER_4_VOL_HEDGES

TIERS: Dict[str, Dict[str, Any]] = {
    "tier1": {
        "label": "BDCs (Direct Stress)",
        "tickers": TIER_1_BDCS,
        "description": "Mark-to-market pressure hits earlier; dividend sustainability questionable",
    },
    "tier2": {
        "label": "Alt Managers (Sentiment + AUM)",
        "tickers": TIER_2_ALT_MANAGERS,
        "description": "Fee compression risk, fund outflows, reputation / gating risk",
    },
    "tier3": {
        "label": "Credit ETFs (Confirmation)",
        "tickers": TIER_3_CREDIT_ETFS,
        "description": "When these break, you're no longer early",
    },
    "tier4": {
        "label": "Vol / Tail Hedges",
        "tickers": TIER_4_VOL_HEDGES,
        "description": "Event monetization instruments",
    },
}


def get_tier_for_ticker(ticker: str) -> str:
    t = ticker.upper()
    if t in TIER_1_BDCS:
        return "tier1"
    if t in TIER_2_ALT_MANAGERS:
        return "tier2"
    if t in TIER_3_CREDIT_ETFS:
        return "tier3"
    if t in TIER_4_VOL_HEDGES:
        return "tier4"
    return "unknown"


# ---------------------------------------------------------------------------
# Per-Ticker Score
# ---------------------------------------------------------------------------

@dataclass
class TickerScore:
    ticker: str
    tier: str
    price: Optional[float] = None
    change_5d_pct: Optional[float] = None
    change_20d_pct: Optional[float] = None
    iv_rank: Optional[float] = None
    put_skew_25d: Optional[float] = None
    insider_net_30d: Optional[float] = None
    signal_score: float = 0.0
    phase_alignment: str = ""
    conviction: str = "neutral"


def compute_ticker_score(
    ticker: str,
    prices: List[float],
    iv_rank: Optional[float] = None,
    put_skew_25d: Optional[float] = None,
    insider_net_30d: Optional[float] = None,
    current_phase: int = 1,
) -> TickerScore:
    """
    Compute per-ticker conviction score for the watchlist.
    """
    tier = get_tier_for_ticker(ticker)
    price = prices[-1] if prices else None

    change_5d = None
    if len(prices) >= 6:
        change_5d = (prices[-1] / prices[-6] - 1) * 100

    change_20d = None
    if len(prices) >= 21:
        change_20d = (prices[-1] / prices[-21] - 1) * 100

    score = 0.0
    # Price decline
    if change_20d is not None and change_20d < 0:
        score += min(abs(change_20d) * 3, 30)
    if change_5d is not None and change_5d < 0:
        score += min(abs(change_5d) * 5, 20)

    # IV rank (higher = more fear)
    if iv_rank is not None:
        score += iv_rank * 0.2

    # Put skew (wider = more protection buying)
    if put_skew_25d is not None and put_skew_25d > 0:
        score += min(put_skew_25d * 5, 20)

    # Insider selling
    if insider_net_30d is not None and insider_net_30d > 0:
        score += min(insider_net_30d / 100000, 10)

    score = max(0, min(100, score))

    # Phase alignment
    alignment = "neutral"
    if current_phase <= 2 and tier in ("tier1", "tier2"):
        alignment = "early-entry"
    elif current_phase >= 2 and tier == "tier2":
        alignment = "scale-target"
    elif current_phase >= 3 and tier == "tier3":
        alignment = "confirmation"
    elif current_phase >= 3 and tier == "tier4":
        alignment = "hedge-monetize"

    conviction = "neutral"
    if score >= 70:
        conviction = "high"
    elif score >= 40:
        conviction = "medium"
    elif score >= 20:
        conviction = "low"

    return TickerScore(
        ticker=ticker,
        tier=tier,
        price=price,
        change_5d_pct=round(change_5d, 2) if change_5d is not None else None,
        change_20d_pct=round(change_20d, 2) if change_20d is not None else None,
        iv_rank=round(iv_rank, 1) if iv_rank is not None else None,
        put_skew_25d=round(put_skew_25d, 3) if put_skew_25d is not None else None,
        insider_net_30d=round(insider_net_30d, 2) if insider_net_30d is not None else None,
        signal_score=round(score, 1),
        phase_alignment=alignment,
        conviction=conviction,
    )


# ---------------------------------------------------------------------------
# Forced Seller Map
# ---------------------------------------------------------------------------

@dataclass
class ForcedSellerEntry:
    ticker: str
    tier: str
    fragility_score: float        # 0-100
    leverage: Optional[float]      # debt/equity
    liquidity_mismatch: Optional[float]
    retail_exposure: Optional[float]
    put_skew_25d: Optional[float]
    price_20d_pct: Optional[float]
    insider_net_30d: Optional[float]


def compute_forced_seller_map(
    tickers: Optional[List[str]] = None,
    ticker_data: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[ForcedSellerEntry]:
    """
    Rank Tier 1 + Tier 2 instruments by fragility.

    ticker_data expected per ticker:
      {
        "leverage": float or None,
        "liquidity_mismatch": float or None,
        "retail_exposure": float or None,  (0-100)
        "put_skew_25d": float or None,
        "price_20d_pct": float or None,
        "insider_net_30d": float or None,
      }
    """
    if tickers is None:
        tickers = TIER_1_BDCS + TIER_2_ALT_MANAGERS
    if ticker_data is None:
        ticker_data = {}

    entries: List[ForcedSellerEntry] = []

    for ticker in tickers:
        data = ticker_data.get(ticker, {})
        tier = get_tier_for_ticker(ticker)

        leverage = data.get("leverage")
        liq_mismatch = data.get("liquidity_mismatch")
        retail = data.get("retail_exposure")
        skew = data.get("put_skew_25d")
        price_chg = data.get("price_20d_pct")
        insider = data.get("insider_net_30d")

        score = 0.0

        # Leverage component (0-30)
        if leverage is not None:
            score += min(leverage * 5, 30)

        # Liquidity mismatch (0-25)
        if liq_mismatch is not None:
            score += min(liq_mismatch * 25, 25)

        # Retail exposure (0-20)
        if retail is not None:
            score += min(retail * 0.2, 20)

        # Put skew (0-15) -- wider skew = more institutional hedging
        if skew is not None and skew > 0:
            score += min(skew * 3, 15)

        # Price decline acceleration (0-10)
        if price_chg is not None and price_chg < 0:
            score += min(abs(price_chg) * 1.5, 10)

        score = max(0, min(100, score))

        entries.append(ForcedSellerEntry(
            ticker=ticker,
            tier=tier,
            fragility_score=round(score, 1),
            leverage=round(leverage, 2) if leverage is not None else None,
            liquidity_mismatch=round(liq_mismatch, 2) if liq_mismatch is not None else None,
            retail_exposure=round(retail, 1) if retail is not None else None,
            put_skew_25d=round(skew, 3) if skew is not None else None,
            price_20d_pct=round(price_chg, 2) if price_chg is not None else None,
            insider_net_30d=round(insider, 2) if insider is not None else None,
        ))

    entries.sort(key=lambda e: e.fragility_score, reverse=True)
    return entries


# ---------------------------------------------------------------------------
# Options Skew Layer (via ORATS)
# ---------------------------------------------------------------------------

def compute_put_skew_25d(
    strikes: List[Dict[str, Any]],
    spot: Optional[float] = None,
) -> Optional[float]:
    """
    Compute 25-delta put skew from ORATS live_strikes data.

    Skew = IV of 25-delta puts - ATM IV.
    Positive = puts are bid relative to ATM (institutional hedging).
    """
    if not strikes:
        return None

    if spot is None:
        for row in strikes:
            s = row.get("spotPrice") or row.get("stockPrice")
            if s:
                spot = float(s)
                break
    if not spot:
        return None

    atm_iv: Optional[float] = None
    put_25d_iv: Optional[float] = None

    best_atm_dist = float("inf")
    best_put_dist = float("inf")

    for row in strikes:
        strike = float(row.get("strike", 0))
        if strike <= 0:
            continue

        put_iv = row.get("putIv") or row.get("smvVol")
        call_iv = row.get("callIv") or row.get("smvVol")
        put_delta = abs(float(row.get("putDelta") or row.get("delta") or 0))

        moneyness_dist = abs(strike - spot)
        if moneyness_dist < best_atm_dist and (put_iv or call_iv):
            best_atm_dist = moneyness_dist
            atm_iv = float(put_iv or call_iv)

        if 0.20 <= put_delta <= 0.30:
            dist_from_25 = abs(put_delta - 0.25)
            if dist_from_25 < best_put_dist and put_iv:
                best_put_dist = dist_from_25
                put_25d_iv = float(put_iv)

    if atm_iv is not None and put_25d_iv is not None:
        return round(put_25d_iv - atm_iv, 4)
    return None


def compute_iv_rank(
    current_iv: Optional[float],
    historical_ivs: List[float],
) -> Optional[float]:
    """
    IV percentile rank over historical window (0-100).
    """
    if current_iv is None or len(historical_ivs) < 20:
        return None
    below = sum(1 for v in historical_ivs if v <= current_iv)
    return round(100.0 * below / len(historical_ivs), 1)
