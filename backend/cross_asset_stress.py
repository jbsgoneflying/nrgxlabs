"""NRGX Labs Front Layer – Cross-Asset Stress Module.

Structured ingestion for FX, Commodities, Crypto, and Volatility markets.
Each asset produces:
  - Direction (up / down / flat)
  - Stress score (0-100)
  - Change vs prior day
  - Confirmation or divergence vs equities

Outputs feed into DailyMarketState only.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AssetStressReading:
    """Per-asset stress reading."""

    symbol: str = ""
    name: str = ""
    asset_class: str = ""              # fx | commodity | crypto | volatility
    direction: str = "flat"            # up | down | flat
    stress_score: float = 50.0         # 0-100 (higher = more stress)
    change_vs_prior: float = 0.0       # percent change
    equity_relationship: str = "neutral"  # confirming | diverging | neutral

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AssetStressReading":
        if not isinstance(d, dict):
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class CrossAssetStressSnapshot:
    """Multi-asset stress snapshot."""

    timestamp: str = ""
    readings: List[dict] = field(default_factory=list)
    composite_score: float = 50.0
    composite_label: str = "Neutral"   # Risk-On | Neutral | Risk-Off | Stressed

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CrossAssetStressSnapshot":
        if not isinstance(d, dict):
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Asset universe – tickers to fetch from EODHD
# ---------------------------------------------------------------------------

CROSS_ASSET_UNIVERSE = {
    # FX
    "DXY": {
        "symbol": "UUP.US",
        "name": "US Dollar Index (UUP)",
        "asset_class": "fx",
        "stress_direction": "positive",  # DXY up = stress for risk assets
    },
    "USDJPY": {
        "symbol": "USDJPY.FOREX",
        "name": "USD/JPY",
        "asset_class": "fx",
        "stress_direction": "negative",  # USDJPY down (JPY strong) = stress
    },
    "USDCHF": {
        "symbol": "USDCHF.FOREX",
        "name": "USD/CHF",
        "asset_class": "fx",
        "stress_direction": "negative",  # USDCHF down (CHF strong) = stress
    },
    "EMFX": {
        "symbol": "EEM.US",
        "name": "EM FX Proxy (EEM)",
        "asset_class": "fx",
        "stress_direction": "negative",  # EEM down = EM stress
    },
    # Commodities
    "OIL": {
        "symbol": "USO.US",
        "name": "Crude Oil (USO)",
        "asset_class": "commodity",
        "stress_direction": "variable",  # context-dependent
    },
    "COPPER": {
        "symbol": "CPER.US",
        "name": "Copper (CPER)",
        "asset_class": "commodity",
        "stress_direction": "negative",  # copper down = demand stress
    },
    "GOLD": {
        "symbol": "GLD.US",
        "name": "Gold (GLD)",
        "asset_class": "commodity",
        "stress_direction": "positive",  # gold up = safety bid = stress
    },
    "SILVER": {
        "symbol": "SLV.US",
        "name": "Silver (SLV)",
        "asset_class": "commodity",
        "stress_direction": "variable",
    },
    # Crypto
    "BTC": {
        "symbol": "BTC-USD.CC",
        "name": "Bitcoin",
        "asset_class": "crypto",
        "stress_direction": "negative",  # BTC down = risk-off
    },
    "ETH": {
        "symbol": "ETH-USD.CC",
        "name": "Ethereum",
        "asset_class": "crypto",
        "stress_direction": "negative",  # ETH down = risk-off
    },
    # Volatility
    "VIX": {
        "symbol": "VIX.INDX",
        "name": "VIX Spot",
        "asset_class": "volatility",
        "stress_direction": "positive",  # VIX up = stress
    },
}


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------


def _clamp(lo: float, hi: float, x: float) -> float:
    return max(lo, min(hi, x))


def _safe_pct_change(current: float, prior: float) -> float:
    """Compute percent change safely."""
    if prior == 0 or not math.isfinite(prior) or not math.isfinite(current):
        return 0.0
    return round((current - prior) / abs(prior) * 100, 4)


def _direction_from_change(pct_change: float, threshold: float = 0.15) -> str:
    """Classify direction from percent change."""
    if pct_change > threshold:
        return "up"
    elif pct_change < -threshold:
        return "down"
    return "flat"


# ---------------------------------------------------------------------------
# Stress scoring
# ---------------------------------------------------------------------------


def compute_asset_stress(
    *,
    symbol_key: str,
    current_close: float,
    prior_close: float,
    equity_return_1d: float = 0.0,
    history_closes: Optional[List[float]] = None,
) -> AssetStressReading:
    """Compute stress reading for a single cross-asset instrument.

    Args:
        symbol_key: Key into CROSS_ASSET_UNIVERSE (e.g. "DXY", "GOLD").
        current_close: Latest close price.
        prior_close: Previous day close price.
        equity_return_1d: S&P 500 1-day return for divergence check.
        history_closes: Rolling history for percentile ranking.
    """
    meta = CROSS_ASSET_UNIVERSE.get(symbol_key, {})
    if not meta:
        return AssetStressReading(symbol=symbol_key, name=symbol_key)

    pct_change = _safe_pct_change(current_close, prior_close)
    direction = _direction_from_change(pct_change)
    stress_dir = meta.get("stress_direction", "variable")

    # --- Stress score logic ---
    # Base: magnitude of move
    abs_change = abs(pct_change)
    magnitude_score = _clamp(0, 100, abs_change * 20)  # 5% move -> 100

    # Directional adjustment: does the move align with the stress direction?
    if stress_dir == "positive":
        # Asset going up = stress (e.g. DXY, GOLD, VIX)
        stress_score = 50 + (pct_change * 15) if pct_change > 0 else 50 - (abs(pct_change) * 10)
    elif stress_dir == "negative":
        # Asset going down = stress (e.g. copper, BTC, EM)
        stress_score = 50 + (abs(pct_change) * 15) if pct_change < 0 else 50 - (pct_change * 10)
    else:
        # Variable: use magnitude only
        stress_score = 50 + magnitude_score * 0.3

    # Percentile ranking boost if history available
    if history_closes and len(history_closes) >= 10:
        recent_changes = []
        for i in range(1, len(history_closes)):
            c = _safe_pct_change(history_closes[i], history_closes[i - 1])
            recent_changes.append(abs(c))
        if recent_changes:
            rank = sum(1 for x in recent_changes if abs(pct_change) >= x) / len(recent_changes)
            # Blend: 60% directional, 40% percentile
            stress_score = stress_score * 0.6 + (rank * 100) * 0.4

    stress_score = round(_clamp(0, 100, stress_score), 1)

    # --- Equity relationship ---
    equity_relationship = _compute_equity_relationship(
        pct_change, equity_return_1d, stress_dir,
    )

    return AssetStressReading(
        symbol=meta.get("symbol", ""),
        name=meta.get("name", symbol_key),
        asset_class=meta.get("asset_class", ""),
        direction=direction,
        stress_score=stress_score,
        change_vs_prior=round(pct_change, 4),
        equity_relationship=equity_relationship,
    )


def _compute_equity_relationship(
    asset_change: float,
    equity_return: float,
    stress_dir: str,
) -> str:
    """Determine if asset is confirming or diverging vs equities.

    Confirming: asset stress aligns with equity weakness (or calm aligns with equity strength).
    Diverging: asset signals stress but equities are up (or vice versa).
    """
    threshold = 0.15  # minimum move to judge

    if abs(asset_change) < threshold and abs(equity_return) < threshold:
        return "neutral"

    if stress_dir == "positive":
        # Asset up = stress. If equities also down -> confirming.
        asset_signals_stress = asset_change > threshold
    elif stress_dir == "negative":
        # Asset down = stress. If equities also down -> confirming.
        asset_signals_stress = asset_change < -threshold
    else:
        return "neutral"

    equity_weak = equity_return < -threshold

    if asset_signals_stress and equity_weak:
        return "confirming"
    elif asset_signals_stress and not equity_weak:
        return "diverging"
    elif not asset_signals_stress and equity_weak:
        return "diverging"
    else:
        return "confirming"


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------


def build_cross_asset_snapshot(
    *,
    readings: List[AssetStressReading],
    timestamp: str = "",
) -> CrossAssetStressSnapshot:
    """Aggregate individual asset readings into a snapshot.

    Composite weights:
      - FX: 30%
      - Commodities: 25%
      - Crypto: 15%
      - Volatility: 30%
    """
    CLASS_WEIGHTS = {
        "fx": 0.30,
        "commodity": 0.25,
        "crypto": 0.15,
        "volatility": 0.30,
    }

    class_scores: Dict[str, List[float]] = {}
    for r in readings:
        cls = r.asset_class
        if cls not in class_scores:
            class_scores[cls] = []
        class_scores[cls].append(r.stress_score)

    # Weighted composite
    weighted_sum = 0.0
    weight_total = 0.0
    for cls, scores in class_scores.items():
        if not scores:
            continue
        avg = sum(scores) / len(scores)
        w = CLASS_WEIGHTS.get(cls, 0.1)
        weighted_sum += avg * w
        weight_total += w

    composite = round(weighted_sum / weight_total, 1) if weight_total > 0 else 50.0
    composite = _clamp(0, 100, composite)

    # Label
    if composite >= 70:
        label = "Stressed"
    elif composite >= 55:
        label = "Risk-Off"
    elif composite <= 35:
        label = "Risk-On"
    else:
        label = "Neutral"

    return CrossAssetStressSnapshot(
        timestamp=timestamp,
        readings=[r.to_dict() for r in readings],
        composite_score=composite,
        composite_label=label,
    )


# ---------------------------------------------------------------------------
# Crypto ratio helper
# ---------------------------------------------------------------------------


def compute_btc_eth_ratio(btc_close: float, eth_close: float) -> Optional[float]:
    """Compute BTC/ETH price ratio. Returns None if invalid."""
    if eth_close <= 0 or not math.isfinite(btc_close) or not math.isfinite(eth_close):
        return None
    return round(btc_close / eth_close, 4)
