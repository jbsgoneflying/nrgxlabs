"""Engine 15 — per-card LLM tooltip explainer.

Lightweight wrapper around :mod:`backend.engine14.card_explain`'s LLM
plumbing, customized with an Engine-15-specific card catalog so the
narrative references earnings semantics (planned exit, VRP, anncTod)
rather than SPX-weekly ones.

The catalog keys match the UI-card identifiers on
``static/earnings-ic.html`` / ``earnings-ic.js``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.engine14 import card_explain as e14_card

LOG = logging.getLogger("engine15.card_explain")


__all__ = ["CARD_CATALOG", "supported_card_types", "generate_card_explanation"]


CARD_CATALOG: Dict[str, Dict[str, str]] = {
    "e1_summary_strip": {
        "title": "Engine 1 Summary",
        "spec": (
            "The Engine 1 summary strip rolls up the single-name earnings "
            "context for the ticker: current spot, implied move %, VRP score, "
            "desk consensus (go / no-go verdict), the next earnings date and "
            "its anncTod (BMO/AMC), plus a one-line IV-elevation stamp.\n"
            "- spot: current close used as the entry-anchor price.\n"
            "- 1σ EM%: ATM-straddle 1σ expected move to expiry.\n"
            "- VRP score (-100..+100): positive = IV pricier than realized "
            "(tailwind for premium sellers); negative = IV cheap vs realized "
            "(headwind).\n"
            "- anncTod: BMO means announcement before the Tuesday open (desk "
            "enters Monday, exits Tuesday AM); AMC means announcement after "
            "Monday close (desk enters Monday, exits Tuesday AM or PM).\n"
            "- Desk consensus: a qualitative verdict (e.g. 'Favorable', "
            "'Neutral', 'Fade') produced by Engine 1 from VRP + entry "
            "quality + regime + gap risk."
        ),
    },
    "event_analogue_row": {
        "title": "Historical Event Row",
        "spec": (
            "One row per prior earnings event used in the replay pool. "
            "Columns:\n"
            "- earnDate: the historical announcement date.\n"
            "- anncTod: BMO/AMC at the time of that historical event.\n"
            "- mapped strikes: the user's strikes translated into the "
            "analogue's strike space by preserving EM-distance.\n"
            "- outcome: earlyTarget / fullCollect / whiteKnuckle / stopOut / "
            "breach — see outcome bucket card.\n"
            "- pnlPct: P&L at the planned-exit boundary as a % of credit.\n"
            "- MAE: worst drawdown during the hold, % of credit.\n"
            "- realizedMovePct: how far the underlying moved between the "
            "pre-earnings close and the post-earnings session.\n"
            "- breached: short-strike taken out at planned exit."
        ),
    },
    "vrp_crush_verdict": {
        "title": "VRP / Vol Crush Verdict",
        "spec": (
            "Reads Engine 1's VRP analysis and combines it with the planned-"
            "exit fidelity note to tell the desk whether the IV crush from "
            "earnings is likely to materialize favorably during the hold.\n"
            "- tailwind: high positive VRP + confirmed anncTod → crush is "
            "likely meaningful; winnability is inflated in the adjusted "
            "distribution.\n"
            "- headwind: negative VRP (IV cheap vs realized) → crush may be "
            "shallow; the adjusted distribution's WR is lowered.\n"
            "- neutral: VRP within ±20pts; empirical distribution dominates."
        ),
    },
    "planned_exit_outcome": {
        "title": "Planned Exit Outcome",
        "spec": (
            "The core result block. Five buckets summing to 100%:\n"
            "- earlyTarget: PT hit before planned exit; P&L banked.\n"
            "- fullCollect: held to planned exit with positive P&L.\n"
            "- whiteKnuckle: eventually profitable but MAE reached stop "
            "territory during the hold.\n"
            "- stopOut: SL hit OR planned exit with negative P&L.\n"
            "- breach: short strike breached at exit with P&L ≤ -50%.\n"
            "Adjacent bucket shows the adjustedOutcomeDistribution — same "
            "buckets but reweighted by conditioning modifiers (VRP, "
            "anncConfidence, calendar, guidance risk). If the net "
            "conditioning effect is material, the UI highlights the "
            "adjusted bars; otherwise both views are near-identical."
        ),
    },
    "entry_state": {
        "title": "Entry State",
        "spec": (
            "The entry-state strip for Engine 15:\n"
            "- userSpot: close at or near request.entryDate, used to map "
            "strikes into analogue space.\n"
            "- 1σ EM%: market-implied 1σ expected move over (entry → "
            "expiry); sourced from a cached chain IV when available, else "
            "from E1 currentImpliedMovePct or a 30% IV fallback.\n"
            "- wingWidth: narrowest wing in points — used by sizing/risk.\n"
            "- eventsUsed / eventsConsidered: analogues that priced vs "
            "the admitted pool. A gap indicates cache thinness — run the "
            "backfill admin endpoint."
        ),
    },
    "planned_exit_timing": {
        "title": "Planned Exit Timing",
        "spec": (
            "Summarizes the hard time-stop the replay obeys:\n"
            "- plannedExitDate: calendar date the desk intends to flatten.\n"
            "- hours after open: 1-4 hours is typical for BMO vol crush.\n"
            "- holdBizDays: biz-day gap from entry to planned exit (≥0).\n"
            "- intradayCrushFactor: ORATS historical is EOD, so we "
            "approximate an AM exit by blending the close-to-close move "
            "by this factor toward the entry-day P&L. 0.80 means ~80% of "
            "the full day's crush has played out by morning.\n"
            "- fidelityCaveat: plain-English explanation of the "
            "approximation, shown in the UI as a chip."
        ),
    },
    "conditioning_summary": {
        "title": "Conditioning Summary",
        "spec": (
            "One-line verdict on whether the conditioning modifiers "
            "materially change the empirical distribution. Components:\n"
            "- vrpTilt: direction + size driven by E1 VRP score.\n"
            "- anncConfidence: 0 when timing confirmed; penalizes mixed "
            "pool for UNK or mismatched anncTod.\n"
            "- calendar: FOMC/CPI/macro proximity in [entry, plannedExit].\n"
            "- guidanceRisk: E1 eventRisk-score shim.\n"
            "netTailMultiplier and netWinRateShiftPct are the aggregate "
            "tail widening and WR shift applied to the adjusted view."
        ),
    },
    "exit_rules_card": {
        "title": "Exit Rules (Planned Hold)",
        "spec": (
            "Recommended PT/SL inside the planned hold window. Because "
            "the time stop is hard-capped at plannedExitDate, the grid "
            "only explores the profit-target and stop-loss axes (per-DTE "
            "targets and trailing stops are suppressed). deltaFromDefault "
            "shows the WR / avgPnl improvement vs the user's entered "
            "PT/SL at the time of the scan."
        ),
    },
}


def supported_card_types() -> List[str]:
    return sorted(CARD_CATALOG.keys())


def generate_card_explanation(
    *,
    card_type: str,
    card_data: Any,
    scenario_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Produce an LLM-backed tooltip for an Engine 15 card.

    We compose the Engine 14 plumbing with our local CARD_CATALOG by
    temporarily monkey-patching the module-level catalog reference. The
    E14 helper caches on (card_type, card_data, context) so repeated
    opens of the same card are free.
    """
    original = getattr(e14_card, "CARD_CATALOG", {})
    merged = {**original, **CARD_CATALOG}
    try:
        e14_card.CARD_CATALOG = merged  # type: ignore[attr-defined]
        result = e14_card.generate_card_explanation(
            card_type=card_type,
            card_data=card_data,
            scenario_context=scenario_context or {},
        )
        if isinstance(result, dict):
            result["_engine"] = 15
        return result
    finally:
        e14_card.CARD_CATALOG = original  # type: ignore[attr-defined]
