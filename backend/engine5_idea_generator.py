"""Engine 5 – Weekly Idea Generator.

Combines lead-lag signals, regime state, ORATS options surface data,
and Benzinga event filters into structured WeeklyIdea output.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.engine5_regime import GlobalRegime
from backend.engine5_translation import SectorBias, IndexBias


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TradeIdea:
    symbol: str
    structure: str                   # put_credit_spread | call_credit_spread | iron_condor | ...
    directional_lean: str            # bullish | bearish | neutral
    confidence: int                  # 0-100
    regime_context: str              # Risk-On | Risk-Off | ...
    lead_lag_source: str             # Human-readable
    iv_rank: Optional[float] = None
    expected_move: Optional[float] = None
    max_risk_estimate: Optional[str] = None
    roc_estimate_model: Optional[str] = None
    roc_assumptions: Optional[Dict[str, Any]] = None
    notes: List[str] = field(default_factory=list)
    suppressed: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        # camelCase for JSON output
        return {
            "symbol": d["symbol"],
            "structure": d["structure"],
            "directionalLean": d["directional_lean"],
            "confidence": d["confidence"],
            "regimeContext": d["regime_context"],
            "leadLagSource": d["lead_lag_source"],
            "ivRank": d["iv_rank"],
            "expectedMove": d["expected_move"],
            "maxRiskEstimate": d["max_risk_estimate"],
            "rocEstimateModel": d["roc_estimate_model"],
            "rocAssumptions": d["roc_assumptions"],
            "notes": d["notes"],
            "suppressed": d["suppressed"],
        }


@dataclass
class WeeklyIdea:
    generated_at: str
    week_label: str
    regime: Dict[str, Any]
    sector_biases: List[Dict[str, Any]]
    index_biases: List[Dict[str, Any]]
    trade_ideas: List[Dict[str, Any]]
    suppressions: List[Dict[str, Any]]
    global_signal_summary: Dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "week": self.week_label,
            "generatedAt": self.generated_at,
            "regime": self.regime,
            "globalSignalSummary": self.global_signal_summary,
            "sectorBiases": self.sector_biases,
            "indexBiases": self.index_biases,
            "tradeIdeas": self.trade_ideas,
            "suppressions": self.suppressions,
        }


# ---------------------------------------------------------------------------
# Structure selection
# ---------------------------------------------------------------------------


def _select_structure(direction: str, regime_label: str, allowed: List[str]) -> Optional[str]:
    """Select the best options structure given direction and regime."""
    if not allowed:
        return None

    if direction == "bullish":
        if "put_credit_spread" in allowed:
            return "put_credit_spread"
        if "iron_condor" in allowed:
            return "iron_condor"
    elif direction == "bearish":
        if "call_credit_spread" in allowed:
            return "call_credit_spread"
        if "iron_condor" in allowed:
            return "iron_condor"
    else:
        # Neutral -> iron condor if regime allows
        if "iron_condor" in allowed:
            return "iron_condor"
        if "put_credit_spread" in allowed:
            return "put_credit_spread"

    return allowed[0] if allowed else None


# ---------------------------------------------------------------------------
# ROC estimation
# ---------------------------------------------------------------------------


def _estimate_roc(
    structure: str,
    expected_move: Optional[float],
    iv_rank: Optional[float],
) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Produce a model ROC estimate with explicit assumptions.

    This is a MODEL ESTIMATE, not a guarantee. Assumptions are always returned.
    """
    if expected_move is None or expected_move <= 0:
        return None, None

    # Default assumptions for a weekly credit spread
    dte = 5
    width = 5.0  # $5 wide spread
    # Estimate credit as fraction of width based on IV rank
    credit_fraction = 0.12 if iv_rank is None else max(0.08, min(0.25, 0.08 + iv_rank * 0.17))
    credit_mid = round(width * credit_fraction, 2)
    max_loss = round(width - credit_mid, 2)
    roc_pct = round((credit_mid / max_loss) * 100, 1) if max_loss > 0 else 0.0

    estimate = f"{roc_pct}% on risk (model estimate)"
    assumptions = {
        "dte": dte,
        "creditMid": credit_mid,
        "width": width,
        "maxLoss": max_loss,
        "basis": "ORATS mid",
    }
    return estimate, assumptions


# ---------------------------------------------------------------------------
# Narrative builder
# ---------------------------------------------------------------------------


def _build_narrative(
    signals: List[dict],
    bars: List[dict],
    regime: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a human-readable global signal summary."""
    active_leaders = set()
    confirming_leaders = set()
    for sig in signals:
        leader = sig.get("leader_symbol", "")
        active_leaders.add(leader)
        if sig.get("confirmation_count", 0) > 0:
            confirming_leaders.add(leader)

    # Dominant theme: most common direction
    directions = [s.get("direction") for s in signals if s.get("direction")]
    if directions:
        bullish_count = sum(1 for d in directions if d == "bullish")
        bearish_count = sum(1 for d in directions if d == "bearish")
        if bullish_count > bearish_count:
            theme = "Global cyclical strength"
        elif bearish_count > bullish_count:
            theme = "Global risk-off pressure"
        else:
            theme = "Mixed global signals"
    else:
        theme = "Insufficient data for theme"

    # Build narrative text from bars
    parts: List[str] = []
    for b in bars:
        sym = b.get("symbol", "")
        ret = b.get("return_1d_local")
        z = b.get("z_score_20d")
        if ret is not None:
            pct = f"{ret * 100:+.1f}%"
            z_str = f" (z={z:.1f})" if z is not None else ""
            parts.append(f"{sym} {pct}{z_str}")

    narrative = "; ".join(parts[:8])  # Limit to 8 most notable
    if not narrative:
        narrative = "Global data collected; see signals for details."

    return {
        "narrative": narrative,
        "leadersActive": len(active_leaders),
        "leadersConfirming": len(confirming_leaders),
        "dominantTheme": theme,
    }


# ---------------------------------------------------------------------------
# Suppression logic
# ---------------------------------------------------------------------------


def _check_suppressions(
    sector_biases: List[SectorBias],
    earnings_symbols: List[str],
    macro_event_flags: List[str],
    regime: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Check for suppressions: earnings, macro events, regime stress."""
    suppressions: List[Dict[str, Any]] = []

    # Earnings-based suppressions
    for bias in sector_biases:
        if bias.sector in earnings_symbols:
            suppressions.append({
                "symbol": bias.sector,
                "reason": f"Earnings window active for {bias.sector}; suppress until post-report",
                "source": "benzinga_earnings_filter",
            })

    # Macro event suppressions
    for flag in macro_event_flags:
        suppressions.append({
            "symbol": "ALL",
            "reason": flag,
            "source": "benzinga_macro_filter",
        })

    # Regime suppressions
    if regime.get("label") == "Stressed":
        suppressions.append({
            "symbol": "ALL",
            "reason": "Regime is Stressed; all ideas suppressed",
            "source": "engine5_regime",
        })

    return suppressions


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def generate_weekly_ideas(
    *,
    date: str,
    signals: List[dict],
    regime: GlobalRegime,
    sector_biases: List[SectorBias],
    index_biases: List[IndexBias],
    bars: List[dict],
    orats_data: Optional[Dict[str, dict]] = None,
    earnings_symbols: Optional[List[str]] = None,
    macro_event_flags: Optional[List[str]] = None,
) -> WeeklyIdea:
    """Generate the weekly idea output.

    Args:
        date: Date string YYYY-MM-DD.
        signals: List of LeadLagSignal dicts.
        regime: GlobalRegime instance.
        sector_biases: List of SectorBias from translation engine.
        index_biases: List of IndexBias from translation engine.
        bars: Today's GlobalAssetBar dicts.
        orats_data: {symbol: {"iv_rank": float, "expected_move": float, ...}}
        earnings_symbols: Symbols with earnings in the week window.
        macro_event_flags: Macro event warnings.
    """
    orats = orats_data or {}
    earnings = earnings_symbols or []
    macro_flags = macro_event_flags or []
    regime_dict = regime.to_dict()

    # Week label
    try:
        d = dt.date.fromisoformat(date)
        week_label = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
    except Exception:
        week_label = date

    # Suppressions
    suppressions = _check_suppressions(sector_biases, earnings, macro_flags, regime_dict)
    suppressed_symbols = {s["symbol"] for s in suppressions}

    # Generate trade ideas from sector biases
    trade_ideas: List[Dict[str, Any]] = []
    for bias in sector_biases:
        if bias.confidence < 30:
            continue  # Too weak

        # Select structure
        structure = _select_structure(
            bias.direction,
            regime.label,
            regime.allowed_structures,
        )
        if structure is None:
            continue

        # ORATS data for this symbol
        sym_orats = orats.get(bias.sector, {})
        iv_rank = sym_orats.get("iv_rank")
        expected_move = sym_orats.get("expected_move")

        # ROC estimate
        roc_est, roc_assumptions = _estimate_roc(structure, expected_move, iv_rank)

        # Notes
        notes: List[str] = []
        if regime.label == "Risk-On":
            notes.append("Regime allows full position sizing")
        elif regime.label == "Transitional":
            notes.append("Transitional regime; reduced position sizing (0.75x)")
        elif regime.label == "Risk-Off":
            notes.append("Risk-Off regime; reduced position sizing (0.50x)")

        if bias.sector not in earnings:
            notes.append("No earnings in window")
        else:
            notes.append(f"CAUTION: {bias.sector} has earnings this week")

        # Suppression check
        is_suppressed = (
            bias.sector in suppressed_symbols
            or "ALL" in suppressed_symbols
        )

        idea = TradeIdea(
            symbol=bias.sector,
            structure=structure,
            directional_lean=bias.direction,
            confidence=bias.confidence,
            regime_context=regime.label,
            lead_lag_source="; ".join(bias.sources[:3]),
            iv_rank=round(iv_rank, 2) if iv_rank is not None else None,
            expected_move=round(expected_move, 2) if expected_move is not None else None,
            max_risk_estimate=f"${roc_assumptions['maxLoss'] * 100:.0f} per spread" if roc_assumptions else None,
            roc_estimate_model=roc_est,
            roc_assumptions=roc_assumptions,
            notes=notes,
            suppressed=is_suppressed,
        )
        trade_ideas.append(idea.to_dict())

    # Narrative
    narrative = _build_narrative(signals, bars, regime_dict)

    return WeeklyIdea(
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        week_label=week_label,
        regime=regime_dict,
        sector_biases=[b.to_dict() for b in sector_biases],
        index_biases=[b.to_dict() for b in index_biases],
        trade_ideas=trade_ideas,
        suppressions=suppressions,
        global_signal_summary=narrative,
    )
