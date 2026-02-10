"""Engine 5 – Translation Engine (Global -> US).

Maps global lead-lag signals and regime state into tradable US sector/index biases.
Uses the static + dynamic mapping from global_assets.json.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SectorBias:
    sector: str               # US ETF symbol (e.g. "XLI")
    name: str                 # Human-readable name
    direction: str            # bullish | bearish | neutral
    confidence: int           # 0-100
    sources: List[str]        # Human-readable source descriptions
    vol_bias: str             # expand | contract | neutral

    def to_dict(self) -> dict:
        return {
            "sector": self.sector,
            "name": self.name,
            "direction": self.direction,
            "confidence": self.confidence,
            "sources": self.sources,
            "volBias": self.vol_bias,
        }


@dataclass
class IndexBias:
    index: str                # SPY | QQQ | IWM
    direction: str            # bullish | bearish | neutral
    confidence: int           # 0-100
    vol_bias: str             # expand | contract | neutral
    note: str

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "direction": self.direction,
            "confidence": self.confidence,
            "volBias": self.vol_bias,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Sector name map
# ---------------------------------------------------------------------------

SECTOR_NAMES = {
    "XLI": "Industrials",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLB": "Materials",
    "XLU": "Utilities",
    "XLP": "Consumer Staples",
    "XLK": "Technology",
    "XLV": "Health Care",
    "XLY": "Consumer Discretionary",
    "XLRE": "Real Estate",
    "XLC": "Communication Services",
    "SMH": "Semiconductors",
    "QQQ": "Nasdaq 100",
    "SPY": "S&P 500",
    "IWM": "Russell 2000",
    "FXI": "China Large-Cap",
    "KWEB": "China Internet",
    "EFA": "EAFE",
    "EEM": "Emerging Markets",
}


# ---------------------------------------------------------------------------
# Translation logic
# ---------------------------------------------------------------------------


def translate_signals_to_us(
    signals: List[dict],
    regime: dict,
    yield_snapshot: Optional[dict] = None,
    fx_bars: Optional[Dict[str, List[dict]]] = None,
) -> tuple[List[SectorBias], List[IndexBias]]:
    """Translate global lead-lag signals into US sector and index biases.

    Args:
        signals: List of LeadLagSignal dicts.
        regime: GlobalRegime dict.
        yield_snapshot: Latest yield snapshot dict.
        fx_bars: {fx_symbol: [bar_dicts]} for FX-based regime overlays.

    Returns:
        (sector_biases, index_biases)
    """
    sector_biases: Dict[str, SectorBias] = {}
    regime_label = regime.get("label", "Transitional")
    regime_modifier = regime.get("position_size_modifier", 1.0)

    # --- Aggregate signals by US target sector ---
    sector_signals: Dict[str, List[dict]] = {}
    for sig in signals:
        target = sig.get("follower_symbol", "")
        if target not in sector_signals:
            sector_signals[target] = []
        sector_signals[target].append(sig)

    for sector, sigs in sector_signals.items():
        if not sigs:
            continue

        # Aggregate direction: majority vote weighted by signal strength
        bullish_weight = sum(s.get("signal_strength", 0) for s in sigs if s.get("direction") == "bullish")
        bearish_weight = sum(s.get("signal_strength", 0) for s in sigs if s.get("direction") == "bearish")
        total_weight = bullish_weight + bearish_weight

        if total_weight < 1:
            continue

        if bullish_weight > bearish_weight * 1.2:
            direction = "bullish"
        elif bearish_weight > bullish_weight * 1.2:
            direction = "bearish"
        else:
            direction = "neutral"

        # Confidence: average signal strength, scaled by regime modifier
        avg_strength = statistics.mean([s.get("signal_strength", 0) for s in sigs])
        confidence = int(min(100, avg_strength * regime_modifier))

        # Sources: human-readable
        sources = []
        for s in sigs:
            leader = s.get("leader_symbol", "")
            z = s.get("magnitude_z", 0)
            lag = s.get("lag_days", 1)
            corr = s.get("correlation_rolling_20d", 0)
            sources.append(f"{leader} (z={z:.1f}, lag={lag}d, corr={corr:.2f})")

        # Vol bias: based on regime
        if regime_label == "Risk-On":
            vol_bias = "contract"
        elif regime_label in ("Risk-Off", "Stressed"):
            vol_bias = "expand"
        else:
            vol_bias = "neutral"

        sector_biases[sector] = SectorBias(
            sector=sector,
            name=SECTOR_NAMES.get(sector, sector),
            direction=direction,
            confidence=confidence,
            sources=sources,
            vol_bias=vol_bias,
        )

    # --- Yield curve overlays ---
    if yield_snapshot:
        slope = yield_snapshot.get("us_2s10s_slope")
        if slope is not None:
            if slope > 0.3:
                # Steepening -> XLF bullish
                _merge_bias(sector_biases, "XLF", "bullish", 55, ["Yield curve steepening"], "contract")
            elif slope < -0.2:
                # Flattening/inversion -> XLU bullish
                _merge_bias(sector_biases, "XLU", "bullish", 50, ["Yield curve flattening"], "neutral")

    # --- FX overlays ---
    if fx_bars:
        usdjpy_bars = fx_bars.get("USDJPY.FOREX", [])
        if usdjpy_bars:
            recent = sorted(usdjpy_bars, key=lambda b: str(b.get("date", "")))
            if len(recent) >= 5:
                yen_rets = []
                for b in recent[-5:]:
                    r = b.get("return_1d_local")
                    if r is not None:
                        try:
                            yen_rets.append(float(r))
                        except (TypeError, ValueError):
                            pass
                if yen_rets:
                    yen_5d = sum(yen_rets)
                    if yen_5d < -0.01:  # JPY strengthening (USDJPY falling)
                        _merge_bias(sector_biases, "XLU", "bullish", 45, ["Yen strength (defensive)"], "neutral")
                        _merge_bias(sector_biases, "XLP", "bullish", 40, ["Yen strength (defensive)"], "neutral")

    # --- Build index biases ---
    index_biases = _build_index_biases(sector_biases, signals, regime)

    return list(sector_biases.values()), index_biases


def _merge_bias(
    biases: Dict[str, SectorBias],
    sector: str,
    direction: str,
    confidence: int,
    sources: List[str],
    vol_bias: str,
) -> None:
    """Merge a bias into existing sector biases. If already exists, take the stronger one."""
    existing = biases.get(sector)
    if existing is None:
        biases[sector] = SectorBias(
            sector=sector,
            name=SECTOR_NAMES.get(sector, sector),
            direction=direction,
            confidence=confidence,
            sources=sources,
            vol_bias=vol_bias,
        )
    else:
        # Merge sources, keep higher confidence direction
        if confidence > existing.confidence:
            existing.direction = direction
            existing.confidence = confidence
            existing.vol_bias = vol_bias
        existing.sources.extend(sources)


def _build_index_biases(
    sector_biases: Dict[str, SectorBias],
    signals: List[dict],
    regime: dict,
) -> List[IndexBias]:
    """Build broad index biases (SPY, QQQ, IWM) from sector biases."""
    index_biases: List[IndexBias] = []
    regime_label = regime.get("label", "Transitional")

    # SPY: aggregate across all sectors
    all_biases = list(sector_biases.values())
    if all_biases:
        bullish = sum(1 for b in all_biases if b.direction == "bullish")
        bearish = sum(1 for b in all_biases if b.direction == "bearish")
        total = len(all_biases)

        if bullish > bearish:
            spy_dir = "bullish"
        elif bearish > bullish:
            spy_dir = "bearish"
        else:
            spy_dir = "neutral"

        confirming = max(bullish, bearish)
        spy_conf = int(min(100, (confirming / max(total, 1)) * 80 + 20))

        vol_bias = "contract" if regime_label == "Risk-On" else ("expand" if regime_label in ("Risk-Off", "Stressed") else "neutral")

        index_biases.append(IndexBias(
            index="SPY",
            direction=spy_dir,
            confidence=spy_conf,
            vol_bias=vol_bias,
            note=f"{confirming}/{total} sectors {spy_dir}; regime={regime_label}",
        ))

    # QQQ: tech-weighted (SMH, XLK influence)
    tech_biases = [b for b in all_biases if b.sector in ("SMH", "XLK", "QQQ")]
    if tech_biases:
        tech_bull = sum(1 for b in tech_biases if b.direction == "bullish")
        tech_bear = sum(1 for b in tech_biases if b.direction == "bearish")
        qqq_dir = "bullish" if tech_bull > tech_bear else ("bearish" if tech_bear > tech_bull else "neutral")
        qqq_conf = int(statistics.mean([b.confidence for b in tech_biases]))
        index_biases.append(IndexBias(
            index="QQQ",
            direction=qqq_dir,
            confidence=qqq_conf,
            vol_bias="contract" if regime_label == "Risk-On" else "neutral",
            note=f"Tech signals: {tech_bull} bull / {tech_bear} bear",
        ))
    else:
        index_biases.append(IndexBias(
            index="QQQ",
            direction="neutral",
            confidence=30,
            vol_bias="neutral",
            note="No strong tech lead-lag signal",
        ))

    return index_biases
