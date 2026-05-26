"""Desk Insight catalog — Market Intelligence (home / front layer).

Market Intelligence is the desk's first-thing-in-the-morning surface:
cross-asset regime read, overnight-to-open diff, macro calendar, pattern
recognition, and per-asset drill-downs. These tooltips teach a new desk
hire what each tile is actually telling them and how it should shape
today's plan.
"""
from __future__ import annotations

ENGINE_META = {
    "id":          "market-intel",
    "name":        "Market Intelligence (v2)",
    "description": (
        "The desk's cross-asset morning surface, powered by a 3-state "
        "Gaussian HMM (Risk-On / Transitional / Stressed) over an 8-factor "
        "z-scored universe. Returns smooth posterior probabilities, a "
        "confidence chip, transition-risk to the next state, per-factor "
        "log-likelihood contributions, and bootstrap confidence bands. "
        "Single source of truth consumed by E3/E4/E7 gating, E14 "
        "conditioning, and NRGX Chat."
    ),
    "asset_class": "cross-asset (SPX RV, VIX term, HYG/LQD credit, DXY, WTI/GLD, BTC, dealer gamma, sector breadth)",
}


CATALOG = {

    "regime_card": {
        "title": "Regime State",
        "spec": (
            "Today's market regime, served as a smooth posterior "
            "probability vector from a 3-state Gaussian Hidden Markov "
            "Model fit on a 5-year rolling window of 8 standardized "
            "factors. The card shows three probability bars (Risk-On / "
            "Transitional / Stressed), a confidence chip (= max prob), "
            "a transition-risk chip (= P(state more-stressed in next "
            "session) computed via the HMM transition matrix), and an "
            "anomaly-score chip (Mahalanobis-style distance from "
            "calibration window).\n"
            "The 8 input factors are rv_spx_20d, vix_term_slope "
            "(VX1-VX2), credit_hyg_lqd, dxy_drift, commodity_stress "
            "(WTI+GLD), btc_decoupling, dealer_gamma (sign + z), and "
            "breadth_proxy (sector ETF dispersion). Each factor is "
            "z-scored against its trailing 252-day distribution before "
            "being fed to the HMM.\n"
            "The argmax label gates which engines fire (Red Dog wants "
            "Transitional/Stressed, Ichimoku wants Risk-On/Transitional), "
            "but the desk should read the FULL probability vector when "
            "conviction matters - a P(stressed) of 0.55 is a very "
            "different trade than 0.85 even though both label as "
            "'Stressed'."
        ),
        "related_cards": [
            {"engine": "market-intel", "slug": "factor_stack", "label": "Factor Stack"},
            {"engine": "market-intel", "slug": "morning_brief", "label": "Morning Brief"},
            {"engine": "market-intel", "slug": "diff_card", "label": "Overnight Diff"},
            {"engine": "market-intel", "slug": "cross_asset_matrix", "label": "Cross-Asset Stress"},
            {"engine": "e5",           "slug": "global_regime_score", "label": "Engine 5 Regime (legacy)"},
        ],
    },

    "factor_stack": {
        "title": "Factor Stack (HMM Inputs)",
        "spec": (
            "The 8 z-scored factors that the regime HMM consumes to "
            "compute today's posterior probability vector. Every row "
            "shows the factor's name, a bidirectional z-bar (left = "
            "negative z / risk-off; right = positive z / stress), the "
            "raw z value, an OK/STALE/MISSING data-quality chip, and "
            "the factor's log-likelihood contribution to the winning "
            "regime state.\n"
            "Factors:\n"
            "- rv_spx_20d: 20-day annualized realized vol on SPY.\n"
            "- vix_term_slope: VIX - VIX3M (positive = backwardation = stress).\n"
            "- credit_hyg_lqd: HYG/LQD ratio z (sign-flipped so higher z = stress).\n"
            "- dxy_drift: 20-day cumulative return on UUP, z-scored.\n"
            "- commodity_stress: |WTI 20d return| + GLD 20d return, z-scored.\n"
            "- btc_decoupling: |20d corr(BTC, SPY) - long-run mean|.\n"
            "- dealer_gamma: sign + magnitude z of dealer net gamma.\n"
            "- breadth_proxy: cross-sector ETF return dispersion z.\n"
            "When 3+ factors go MISSING the regime service falls back "
            "to a legacy linear composite. The data-quality banner at "
            "the top of MI surfaces this state."
        ),
        "related_cards": [
            {"engine": "market-intel", "slug": "regime_card", "label": "Regime State"},
            {"engine": "market-intel", "slug": "diff_card", "label": "Day-over-Day Diff"},
            {"engine": "market-intel", "slug": "cross_asset_matrix", "label": "Cross-Asset Stress"},
            {"engine": "e9",           "slug": "credit_stress_score", "label": "E9 Credit Stress (drill-down)"},
        ],
    },

    "morning_brief": {
        "title": "Morning Brief",
        "spec": (
            "A three-line LLM narrative summarizing the current market "
            "state, the weekly bias, and the top risks. Produced by "
            "backend.llm_client.generate_desk_brief from the same inputs "
            "the Regime card sees (regime, vol state, sequencer events, "
            "macro calendar).\n"
            "- market_state: one sentence describing where we are.\n"
            "- weekly_bias: what is likely to work this week.\n"
            "- top_risks: what could break the plan.\n"
            "Each line is capped at ~30 words; never mentions specific "
            "prices, trade ideas, or tickers — it's a framing tool, not "
            "a signal."
        ),
        "related_cards": [
            {"engine": "market-intel", "slug": "regime_card", "label": "Regime State"},
            {"engine": "market-intel", "slug": "patterns_card", "label": "Patterns"},
            {"engine": "e11",          "slug": "macro_calendar", "label": "Macro Calendar (E11)"},
        ],
    },

    "patterns_card": {
        "title": "Pattern Detection",
        "spec": (
            "Rolling cross-asset pattern scanner — looks for recognized "
            "setups over the last 14 sessions. Pattern family examples:\n"
            "- vol_compression_at_support: SPX near prior support with "
            "RV20 bottoming. Historically precedes a grind-up.\n"
            "- credit_decoupling: HYG/LQD cracking while SPX holds — "
            "regime-transition warning.\n"
            "- dealer_gamma_flip: SPX crossing the zero-gamma line — "
            "volatility regime shift.\n"
            "- breadth_thrust: ≥90% of Russell 3000 up on the session — "
            "bullish thrust bias for days/weeks out.\n"
            "Each pattern shows confidence (0-100) and the lookback "
            "matches that seeded it."
        ),
        "related_cards": [
            {"engine": "market-intel", "slug": "regime_card", "label": "Regime State"},
            {"engine": "market-intel", "slug": "asset_tile", "label": "Asset Tiles"},
            {"engine": "e5",           "slug": "vol_leadlag", "label": "Vol Lead-Lag"},
        ],
    },

    "diff_card": {
        "title": "Day-over-Day Intelligence (v2)",
        "spec": (
            "Real day-over-day diff panel computed by "
            "backend.market_intel.diff. Shows what ACTUALLY changed "
            "overnight using the v2 factor and HMM state vectors:\n"
            "- Headline summary: a single sentence rolling up the "
            "largest factor move + regime flip delta + gate changes.\n"
            "- Top factor moves: factors ranked by |Delta z(today vs "
            "yesterday)| from the 8-factor HMM input vector.\n"
            "- Engine gate changes: which gates (E3/E4/E7) opened or "
            "closed overnight as a side effect of the regime flip.\n"
            "- Regime threshold proximity: today's P(stressed) and how "
            "far it sits from the 0.5 flip line; flagged when the "
            "0.5 line was crossed overnight.\n"
            "- Regime flip delta: P(stressed today) - P(stressed "
            "yesterday); flagged 'material' at >= 15pp.\n"
            "If nothing meaningful changed the headline reads 'Quiet "
            "tape' - use that to calibrate urgency for the open."
        ),
        "related_cards": [
            {"engine": "market-intel", "slug": "regime_card", "label": "Regime State"},
            {"engine": "market-intel", "slug": "morning_brief", "label": "Morning Brief"},
            {"engine": "market-intel", "slug": "cross_asset_matrix", "label": "Cross-Asset Matrix"},
        ],
    },

    "asset_tile": {
        "title": "Asset Tile",
        "spec": (
            "Per-asset drill-down card (SPX, VIX, TLT, HYG, DXY, crude, "
            "gold, BTC, etc.). Each tile shows:\n"
            "- spot + 1d / 5d / 20d % change.\n"
            "- IV rank (for optioned assets) and RV20 percentile.\n"
            "- Stress flags: elevated vol, cross-asset correlation "
            "anomaly, macro-event proximity.\n"
            "- asset-insight body: LLM narrative grounded in the tile's "
            "numbers (what the move means for the rest of the desk).\n"
            "Scoped to the single asset — use the Cross-Asset Matrix card "
            "for inter-asset context."
        ),
        "related_cards": [
            {"engine": "market-intel", "slug": "cross_asset_matrix", "label": "Cross-Asset Matrix"},
            {"engine": "market-intel", "slug": "regime_card", "label": "Regime State"},
            {"engine": "e5",           "slug": "vol_leadlag", "label": "Vol Lead-Lag"},
        ],
    },

    "cross_asset_matrix": {
        "title": "Cross-Asset Matrix",
        "spec": (
            "Heatmap of rolling correlations between the tracked asset "
            "universe (typically 20-day Pearson). Cells color from red "
            "(strong negative) through white (zero) to green (strong "
            "positive). Brackets flag anomaly cells where today's corr "
            "diverges from the 60-day mean by > 1 sigma.\n"
            "This is the at-a-glance map of 'what's moving together' — "
            "when a normally-green cell goes white, something has "
            "decoupled, and that's usually the most important information "
            "in the whole morning pack."
        ),
        "related_cards": [
            {"engine": "market-intel", "slug": "regime_card", "label": "Regime State"},
            {"engine": "market-intel", "slug": "asset_tile", "label": "Asset Tile"},
            {"engine": "e5",           "slug": "global_regime_score", "label": "Global Lead/Lag Regime"},
        ],
    },

}
