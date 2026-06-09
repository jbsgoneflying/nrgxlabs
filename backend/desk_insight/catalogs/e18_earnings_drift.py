"""Desk Insight catalog — Engine 18 (Earnings Drift / PEAD).

Long-only post-earnings-announcement-drift scanner validated by the Edge
Bake-Off (OOS 2023+ +0.65%/trade, t=2.7, n=843).
"""
from __future__ import annotations

ENGINE_META = {
    "id":          "e18",
    "name":        "Engine 18 — Earnings Drift (PEAD)",
    "description": (
        "Scans fresh earnings for large, high-quality EPS beats and surfaces "
        "long-equity drift candidates: enter at the next session open after "
        "the report, hold exactly 10 trading days. The LLM grades transcript "
        "quality; surprise buckets, quality quintiles, and sizing tiers are "
        "deterministic and anchored to validated backtest cohorts."
    ),
    "asset_class": "US large-cap equities (S&P 500 + NASDAQ 100)",
}


CATALOG = {

    "scan_summary": {
        "title": "Scan Summary",
        "spec": (
            "Headline counts for today's drift scan:\n"
            "- Candidates: in-universe reports in the lookback window with an "
            "EPS beat >= +5% that passed the $10M ADV liquidity floor.\n"
            "- Actionable: candidates sized full or half by the deterministic "
            "matrix (large beat + Q4/Q5 quality = full; large beat + Q3 or "
            "small beat + Q4/Q5 = half; everything else = pass).\n"
            "Misses are never shown — the backtest says they lose money long "
            "AND short. The scan rebuilds weekdays at 7:45 ET, after most "
            "before-open reports print and before the entry window opens."
        ),
        "related_cards": [
            {"engine": "e18", "slug": "candidate_card", "label": "Candidate Card"},
            {"engine": "e18", "slug": "quality_grade", "label": "Quality Grade"},
        ],
    },

    "candidate_card": {
        "title": "Candidate Card",
        "spec": (
            "One qualifying earnings beat. Key fields:\n"
            "- Surprise bucket: beat_small (+5..20%) or beat_large (>= +20%). "
            "Large beats carry the edge: +1.05%/trade vs +0.47% for small "
            "(validated cohorts, n=548 / n=1181).\n"
            "- Quality quintile: LLM transcript grade ranked against the "
            "trailing score distribution (Q5 = best).\n"
            "- Sizing tier: deterministic from bucket x quintile — never "
            "overridden upward by any LLM.\n"
            "- Entry/exit: next session open after the report, exit after "
            "exactly 10 trading days. The dates are computed, not advisory.\n"
            "Expected-edge stats on the card come straight from the validated "
            "bake-off cohorts, not from live extrapolation."
        ),
        "related_cards": [
            {"engine": "e18", "slug": "quality_grade", "label": "Quality Grade"},
            {"engine": "e18", "slug": "options_expression", "label": "Options Expression"},
            {"engine": "e18", "slug": "tracker", "label": "Desk Tracker"},
        ],
    },

    "quality_grade": {
        "title": "Quality Grade",
        "spec": (
            "Transcript quality score (0..1) for the earnings call, graded by "
            "the live OpenAI grader (heuristic keyword grader as fallback — "
            "both scores are logged per event for ongoing grader-vs-grader "
            "validation). The score is bucketed into quintiles vs the trailing "
            "distribution of recent grades.\n"
            "Why it matters: in the bake-off overlay, top-quintile transcripts "
            "earned +1.45%/trade @ 60% hit vs +0.18% for bottom-quintile — "
            "quality grading IS the incremental LLM edge.\n"
            "No transcript -> neutral 0.5 score; the candidate can still trade "
            "at reduced sizing if its surprise bucket allows."
        ),
        "related_cards": [
            {"engine": "e18", "slug": "candidate_card", "label": "Candidate Card"},
            {"engine": "e18", "slug": "scan_summary", "label": "Scan Summary"},
        ],
    },

    "tracker": {
        "title": "Desk Tracker",
        "spec": (
            "Tracked drift trades with entry snapshot and the 10-trading-day "
            "exit countdown. A trade at or past its planned exit date shows a "
            "red flag — the validated edge assumes disciplined exits at the "
            "10-day mark; extending holds is unbacktested behavior.\n"
            "Closed trades store outcome (exit price, return) and feed the "
            "monthly continuous-validation loop that compares live results "
            "to the backtested cohort expectations."
        ),
        "related_cards": [
            {"engine": "e18", "slug": "candidate_card", "label": "Candidate Card"},
            {"engine": "e18", "slug": "scan_summary", "label": "Scan Summary"},
        ],
    },

    "options_expression": {
        "title": "Options Expression",
        "spec": (
            "Informational ONLY — not backtested. For full-size candidates the "
            "engine reads the live ORATS chain and suggests a ~3-week call "
            "debit spread (long ~40-delta / short ~20-delta) as a defined-risk "
            "alternative to the equity position.\n"
            "The validated +0.65%/trade edge was measured on EQUITY entries at "
            "the open with 10bps/side costs. Option spreads change the payoff "
            "profile (theta, IV crush after earnings, pin risk) in ways the "
            "backtest never measured. Treat the card as a starting structure, "
            "size it as risk capital, and prefer equity when in doubt."
        ),
        "related_cards": [
            {"engine": "e18", "slug": "candidate_card", "label": "Candidate Card"},
        ],
    },

}
