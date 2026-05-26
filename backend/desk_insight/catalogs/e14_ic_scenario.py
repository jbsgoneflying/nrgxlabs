"""Desk Insight catalog — Engine 14 (IC Scenario Simulator).

Migrated from the legacy ``backend/engine14/card_explain.py`` with added
``quant_mechanics`` and ``related_cards`` cross-links.
"""
from __future__ import annotations

ENGINE_META = {
    "id":          "e14",
    "name":        "Engine 14 v2 — IC Scenario Command Deck",
    "description": (
        "SPX iron-condor Command Deck. Ranks candidate "
        "(EM-multiple × wing-width) placements via a deterministic "
        "composite score (historical breach + intraweek touch + "
        "empirical MAE + theta capture + credit ROC), overlays MI v2 "
        "regime probabilities, layers a forward Monte Carlo on the "
        "analogue pool so the desk sees predictive + realised "
        "distributions side-by-side, and exposes a native LLM advisor "
        "button (separate from the E2 advisor that /reconcile still "
        "delegates to). Scenario simulator stays as the drill-down "
        "for the selected placement: analogue replay, MTM percentile "
        "timeline, exit-rule optimisation, sizing, greeks attribution, "
        "and conditioning adjustments."
    ),
    "asset_class": "SPX weekly iron condors",
}


CATALOG = {

    "wing_console": {
        "title": "Wing Decision Console",
        "spec": (
            "Primary card on the /ic-scenario page. Ranks 12 candidate "
            "placements (4 EM-multiples × 3 wing-widths by default) by "
            "a deterministic composite score (0-100). Five inputs:\n"
            "- breach_close_prob: MC P(close at expiry outside shorts), "
            "bootstrapped from the analogue pool under today's regime.\n"
            "- touch_intraweek_prob: MC P(spot touched short strike "
            "midweek). A weekly IC's real failure mode.\n"
            "- mae_p95 vs wing: empirical 95th-pct intraweek max "
            "adverse excursion from the analogue pool, as a fraction "
            "of wing width.\n"
            "- theta_capture: expected % of entry credit retained by "
            "the planned exit (BS approximation).\n"
            "- credit_est / ROC: normal-IV closed-form proxy for entry "
            "credit and return-on-capital.\n"
            "Weights default to breach 25% / touch 20% / mae 25% / "
            "theta 15% / credit 15% and are desk-tunable via "
            "E14_WING_SCORE_WEIGHT_* env knobs. The desk click a "
            "placement row to handoff into the Scenario drilldown "
            "(existing analogue replay + MTM + exit optimiser + "
            "sizing)."
        ),
        "related_cards": [
            {"engine": "e14",          "slug": "placement_score",    "label": "Placement Scorecard"},
            {"engine": "e14",          "slug": "mc_reading",         "label": "MC Reading"},
            {"engine": "e14",          "slug": "mae_distribution",   "label": "MAE Pool"},
            {"engine": "e14",          "slug": "regime_mi_v2",       "label": "MI v2 Regime"},
            {"engine": "market-intel", "slug": "regime_card",        "label": "Market Intel Regime"},
            {"engine": "e14",          "slug": "outcome_distribution","label": "Outcome Distribution"},
        ],
    },

    "placement_score": {
        "title": "Placement Scorecard",
        "spec": (
            "Row-level drill-down of one candidate placement from the "
            "Wing Console grid. Each row shows:\n"
            "- em_mult × wing_pts + absolute short/long strikes derived "
            "from today's spot + 1σ EM.\n"
            "- Three risk terms: breach_close_prob (MC), "
            "touch_intraweek_prob (MC path), mae_p95_vs_wing.\n"
            "- Three reward terms: theta_capture_pct, credit_dollars, "
            "roc_est.\n"
            "- Confidence chip: high when MC pool is deep + regime/"
            "macro conditioned; low when bootstrap fell back to "
            "unconditioned or historical-only.\n"
            "- Composite breakdown so the desk can see which term "
            "drove the score (close-safety vs MAE-safety vs credit-"
            "richness).\n"
            "Hand-tune via the EM and wing sliders under the table — "
            "exact scores come from /api/ic-scenario/wing-console/"
            "score-placement (re-uses the cached ScoringContext, so "
            "slider moves are sub-200ms)."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "wing_console",    "label": "Wing Decision Console"},
            {"engine": "e14", "slug": "mc_reading",      "label": "MC Reading"},
            {"engine": "e14", "slug": "mae_distribution", "label": "MAE Pool"},
        ],
    },

    "mc_reading": {
        "title": "Monte Carlo Reading",
        "spec": (
            "Forward Monte Carlo on top of the analogue pool. For each "
            "(em_mult, wing_pts) the simulator bootstraps weekly paths "
            "from the conditioned analogue pool and aggregates:\n"
            "- breach_close_prob: P(close at expiry outside shorts).\n"
            "- touch_intraweek_prob: P(spot touched a short strike).\n"
            "- outside_wings_prob: P(close outside the long strikes).\n"
            "- mae_p50 / p75 / p90 / p95: intraweek excursion tail.\n"
            "Conditioning hierarchy (same pattern as E2 v2):\n"
            "1. (regime_bucket + macro_bucket) when pool >= min_pool.\n"
            "2. regime_bucket only (falls back automatically).\n"
            "3. Unconditioned pool with a conditioning_degraded note.\n"
            "Deterministic seed from (entry_date, expiry_date, "
            "strikes_fp, n_sims, flags_fp), so cache hits are "
            "reproducible. Layered SEPARATELY from the analogue-only "
            "outcomeDistribution which stays on the page as the "
            "realised (empirical) reading. MC = predictive; "
            "analogues = realised; no double counting."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "wing_console",        "label": "Wing Decision Console"},
            {"engine": "e14", "slug": "outcome_distribution", "label": "Outcome Distribution (realised)"},
            {"engine": "e14", "slug": "placement_score",     "label": "Placement Scorecard"},
        ],
    },

    "mae_distribution": {
        "title": "MAE Pool (intraweek)",
        "spec": (
            "Historical distribution of intraweek max adverse "
            "excursion (worst |spot - entry_close| across the hold "
            "window) across the analogue pool. For each past weekly "
            "window:\n"
            "``mae_pct = max(|high - entry|, |entry - low|) / entry * 100``\n"
            "aggregated to p50/p75/p90/p95 + max. The Wing Console "
            "composite uses p95 in the penalty term: placements whose "
            "historical p95 MAE punched past the shorts and into wing "
            "territory get a saturating penalty.\n"
            "Source tags:\n"
            "- 'daily_ohlc' (best): every hold day had high + low "
            "from ORATS.\n"
            "- 'open_close_fallback': weeks lacked intraday extremes, "
            "so p95 under-estimates true intraweek MAE.\n"
            "- 'mixed': partial coverage across the pool."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "wing_console", "label": "Wing Decision Console"},
            {"engine": "e14", "slug": "mc_reading",   "label": "MC Reading"},
        ],
    },

    "regime_mi_v2": {
        "title": "MI v2 Regime (HMM)",
        "spec": (
            "Market Intelligence v2 regime snapshot — the single "
            "regime source now shared across E1 / E2 / E15 / E14 "
            "Command Decks. Replaces the E14-specific DMS-only regime "
            "reader for forward-dated scenarios (historical entry "
            "dates still benefit from the stored DMS as-of-day "
            "snapshot).\n"
            "- probabilities: 3-state HMM (Risk-On / Transitional / "
            "Stressed) with posterior probabilities summing to 1.\n"
            "- label: most-likely state.\n"
            "- vol_state: aligned to the HMM state so the tracker + "
            "sizing consensus keep the same volatility language.\n"
            "- source: 'v2_hmm' when calibrated; 'default_model' "
            "when MI v2 hasn't been fit yet.\n"
            "E14's own regime_match card (next) describes how the "
            "analogue pool is filtered off this label + the KNN "
            "feature-store distance."
        ),
        "related_cards": [
            {"engine": "market-intel", "slug": "regime_card", "label": "Market Intel Regime"},
            {"engine": "e14",          "slug": "regime_match", "label": "Analogue Regime Match"},
            {"engine": "e14",          "slug": "wing_console", "label": "Wing Decision Console"},
        ],
    },

    "entry_state": {
        "title": "Entry State",
        "spec": (
            "The Entry State strip summarizes the replay context at the "
            "trade's entry moment. Fields:\n"
            "- Analogues Used / Considered: how many historical IC replays "
            "survived the matcher filter. More = tighter estimate.\n"
            "- Regime Bucket: a label (low/mid/high RV20 percentile) "
            "classifying today's realized-vol regime; analogues are drawn "
            "from the same bucket.\n"
            "- Spot (Entry): SPX cash price used as the reference price. "
            "If the requested entry date has no printed bar, the card shows "
            "the most recent close with an amber 'market closed' stamp.\n"
            "- 1σ EM %: market-implied 1-standard-deviation expected move "
            "to expiry (from the ATM straddle).\n"
            "- Short PUT/CALL Dist: distance from spot to each short strike, "
            "in % and in EM multiples. Rule of thumb: <1.00× EM = inside "
            "the cone (red/amber); ≥1.00× = outside (blue/green).\n"
            "- Wing Width: smaller of put-wing or call-wing in points — "
            "the max-loss geometry.\n"
            "- Mean / Median P&L + Sharpe-proxy: structural quality across "
            "the analogue pool under the active exit rules."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "regime_match", "label": "Regime Match Quality"},
            {"engine": "e14", "slug": "outcome_distribution", "label": "Outcome Distribution"},
            {"engine": "e14", "slug": "position_sizing", "label": "Position Sizing"},
        ],
    },

    "regime_match": {
        "title": "Analogue Regime Match",
        "spec": (
            "v2 hierarchy:\n"
            "1. **MI v2 HMM primary** — regime label comes from the "
            "shared market_intel.regime_snapshot() (same source E1 / "
            "E2 / E15 Command Decks consume). The label maps into "
            "E14's LOW / MODERATE / ELEVATED bucket so the analogue "
            "matcher filter stays apples-to-apples with pre-v2 pools.\n"
            "2. **DMS secondary** — if MI v2 isn't calibrated, falls "
            "back to the DailyMarketState regime (multi-factor trend + "
            "volatility + stress + event + dispersion).\n"
            "3. **KNN secondary** — when "
            "ENGINE14_ENABLE_KNN_REGIME=1, a multi-factor nearest-"
            "neighbor match over the feature store (RV20, term "
            "structure, skew, dealer gamma, etc.) weighted L2 "
            "distance. Distances are raw; lower = closer.\n"
            "4. **EM-proxy last** — 1σ EM bucket only when nothing "
            "else is available.\n"
            "Distance / imputation stats remain valuable signal when "
            "KNN is on: wide spread = pool isn't cohesive; high "
            "imputation = brittle match."
        ),
        "related_cards": [
            {"engine": "e14",          "slug": "regime_mi_v2",        "label": "MI v2 Regime (HMM)"},
            {"engine": "e14",          "slug": "entry_state",         "label": "Entry State"},
            {"engine": "e14",          "slug": "matched_analogues",   "label": "Matched Analogues"},
            {"engine": "e14",          "slug": "conditioning_notes",  "label": "Conditioning Notes"},
            {"engine": "market-intel", "slug": "regime_card",         "label": "Market Intel Regime"},
        ],
    },

    "outcome_distribution": {
        "title": "Outcome Distribution (NBBO)",
        "spec": (
            "Primary empirical outcome mix across all matched analogue "
            "replays under the active fill model. Five mutually-exclusive "
            "buckets summing to 100%:\n"
            "- Early Target: hit profit target (typically 50% of credit) "
            "early and closed for a clean win; MAE never approached stop.\n"
            "- Full Collect: rolled to expiry ended positive without "
            "hitting stop — a calm win.\n"
            "- White Knuckle: ended positive BUT intraday/EOD MAE reached "
            "stop territory during the hold. Functionally a win, stressful.\n"
            "- Stop Out: triggered loss stop OR rolled to expiry finished "
            "below zero without hitting stop rule — both are realized losses.\n"
            "- Breach: underlying closed beyond a short strike at expiry → "
            "assignment / max-loss if held.\n"
            "Per-bucket: pct, n, avg P&L, avg days, and (when shown) a 90% "
            "bootstrap CI — wider bands = thinner sample."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "outcome_adjusted", "label": "Adjusted Distribution"},
            {"engine": "e14", "slug": "outcome_mid", "label": "Legacy Mid-Fill"},
            {"engine": "e14", "slug": "matched_analogues", "label": "Matched Analogues"},
            {"engine": "e14", "slug": "mtm_timeline", "label": "MTM Timeline"},
        ],
    },

    "outcome_mid": {
        "title": "Legacy Mid-Fill Distribution",
        "spec": (
            "Same five-outcome mix as the primary distribution but computed "
            "under a pure mid-price fill model (no NBBO, no slippage). "
            "Shown only as a calibration reference — expect mid-only to "
            "overstate win rate vs NBBO because it doesn't pay the bid/ask "
            "spread to exit. Use the delta between mid and NBBO to see how "
            "much of the edge is spread-sensitive."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "outcome_distribution", "label": "Outcome Distribution (NBBO)"},
            {"engine": "e14", "slug": "conditioning_notes", "label": "Conditioning Notes"},
        ],
    },

    "outcome_adjusted": {
        "title": "Adjusted Distribution (Phase 2 conditioning)",
        "spec": (
            "Outcome distribution after applying the Conditioning Modifiers "
            "(macro calendar density, dealer-gamma regime, cross-asset "
            "stress, gap regime from Engine 13). Tail probabilities are "
            "multiplied by the net tail-multiplier and win-rate is shifted "
            "by the net win-rate shift. This is the distribution to trust "
            "when today's regime diverges from the raw analogue pool's "
            "regime."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "outcome_distribution", "label": "Empirical Distribution"},
            {"engine": "e14", "slug": "modifiers", "label": "Conditioning Modifiers"},
            {"engine": "e13", "slug": "fragility_score", "label": "Gap Fragility"},
        ],
    },

    "modifiers": {
        "title": "Conditioning Modifiers",
        "spec": (
            "Per-factor adjustments applied to the raw empirical "
            "distribution to get the Adjusted Distribution. Each card "
            "shows a severity label (none / low / moderate / elevated / "
            "extreme), a tail multiplier (scales breach+stop tails), a "
            "WR shift (percentage-point add-on to full-collect + "
            "early-target), and a reason.\n"
            "- Macro Calendar: high-impact events in the holding window "
            "(FOMC, CPI, NFP) — denser calendars fatten tails.\n"
            "- Dealer Gamma: SPX dealer net gamma. Positive = dealers damp "
            "moves (IC friendly). Negative = amplifies (IC hostile).\n"
            "- Cross-Asset Stress: HYG/LQD spreads, DXY, crude, gold, BTC "
            "composite — elevated stress raises breach tails.\n"
            "- Gap Regime (E13): current overnight-gap environment.\n"
            "- Net Adjustment: composite tail-mult × WR shift applied."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "outcome_adjusted", "label": "Adjusted Distribution"},
            {"engine": "e13", "slug": "gap_regime_card", "label": "Gap Regime"},
            {"engine": "e9",  "slug": "credit_stress_score", "label": "Credit Stress Drift"},
        ],
    },

    "mtm_timeline": {
        "title": "MTM Timeline (P10 / P50 / P90)",
        "spec": (
            "Mark-to-market P&L path through the life of the trade, as a "
            "% of credit received, at each day-to-expiry step.\n"
            "- P50 (median): the typical path — what you'd MTM on a normal "
            "analogue.\n"
            "- P10 / P90: the 10th and 90th percentile paths — the bad-tail "
            "and good-tail envelopes.\n"
            "A steep P10 dip early = analogues commonly got punched before "
            "recovering (path risk even if outcome was positive). A flat "
            "P50 that drifts up is the classic theta-decay glide. Wide "
            "P10-P90 fan means high path uncertainty; narrow fan = tight "
            "analogue cluster."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "outcome_distribution", "label": "Outcome Distribution"},
            {"engine": "e14", "slug": "greeks_attribution", "label": "Greeks Attribution"},
            {"engine": "e14", "slug": "position_sizing", "label": "Position Sizing"},
        ],
    },

    "position_sizing": {
        "title": "Position Sizing",
        "spec": (
            "Four sizing recommendations as a fraction of equity to risk:\n"
            "- Consensus (min of three): the floor — the most conservative "
            "of the three methods. Defer to this unless you have a reason.\n"
            "- Kelly (½-Kelly): half-Kelly using empirical win probability "
            "and payoff ratio from the replay. Clamped to guard outliers.\n"
            "- Fixed-Fractional: standard risk-per-trade against the "
            "worst-case loss seen in the analogue pool.\n"
            "- Empirical Max-DD: sizing that would have capped historical "
            "drawdown to the target percentage given this structure's "
            "observed drawdown path."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "outcome_distribution", "label": "Outcome Distribution"},
            {"engine": "e14", "slug": "mtm_timeline", "label": "MTM Timeline"},
            {"engine": "e14", "slug": "exit_optimization", "label": "Exit Rules"},
        ],
    },

    "greeks_attribution": {
        "title": "P&L Attribution (Greeks)",
        "spec": (
            "Average decomposition of per-analogue P&L across delta, "
            "gamma, theta, vega, and residual, using an entry-Taylor "
            "approximation (greeks × realized factor moves). Two numbers "
            "per greek:\n"
            "- Pct value: contribution to P&L in % of credit (signed).\n"
            "- Share of |P&L|: the greek's share of the total absolute-value "
            "bar.\n"
            "Residual absorbs unmodeled IV-path, second-order cross greeks, "
            "and fill slippage — a large residual is itself a signal that "
            "the Taylor proxy is missing something."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "mtm_timeline", "label": "MTM Timeline"},
            {"engine": "e14", "slug": "outcome_distribution", "label": "Outcome Distribution"},
        ],
    },

    "exit_optimization": {
        "title": "Exit-Rule Optimization",
        "spec": (
            "A grid search over profit-target and stop-loss levels across "
            "matched analogues, picking the PT/SL pair that maximizes "
            "average P&L subject to a minimum win-rate floor.\n"
            "- Recommended PT / SL: the best grid cell.\n"
            "- Δ Win Rate / Δ Avg P&L: change vs the defaults you "
            "submitted (green = improvement).\n"
            "If the recommendation matches your defaults, your rules are "
            "already near-optimal — don't chase small edges."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "exit_sensitivity", "label": "Exit-Rule Sensitivity"},
            {"engine": "e14", "slug": "outcome_distribution", "label": "Outcome Distribution"},
        ],
    },

    "exit_sensitivity": {
        "title": "Exit-Rule Sensitivity",
        "spec": (
            "Interactive sliders that scrub across the exit-rule grid to "
            "see win-rate + avg-P&L for any PT/SL combo without re-running "
            "the replay. Flat metrics across a wide region = sturdy rule; "
            "cliff = fragile. If only a narrow PT/SL band wins, the edge "
            "depends on the stop being exactly right and won't generalize."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "exit_optimization", "label": "Exit Optimization"},
        ],
    },

    "conditioning_notes": {
        "title": "Conditioning Notes",
        "spec": (
            "Plain-English bullets emitted when unusual conditions were "
            "detected during the replay: thin sample, feature-store outage, "
            "unusual calendar density, sparse chain cache, analogue-pool "
            "skew, etc. Treat these as sanity checks before leaning on the "
            "distribution — when two or more notes fire, down-weight the "
            "signal and verify manually."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "regime_match", "label": "Regime Match Quality"},
            {"engine": "e14", "slug": "outcome_distribution", "label": "Outcome Distribution"},
        ],
    },

    "matched_analogues": {
        "title": "Matched Analogues",
        "spec": (
            "Row-by-row view of the individual historical IC replays that "
            "informed the distribution. Each row shows the historical "
            "entry and expiry dates, the outcome bucket, the day the "
            "replay exited, realized P&L (% of credit), max adverse "
            "excursion (% of credit), the mapped strikes, and whether a "
            "short strike was breached at expiry. Use this to sanity-check "
            "the distribution against specific dates and to spot unusual "
            "rows that might deserve exclusion."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "outcome_distribution", "label": "Outcome Distribution"},
            {"engine": "e14", "slug": "regime_match", "label": "Regime Match Quality"},
        ],
    },

    "post_trade_review": {
        "title": "Post-Trade Review",
        "spec": (
            "After a live trade is journaled and later closed, this panel "
            "compares the actual realized P&L and outcome vs the predicted "
            "mean / median / outcome-probability from the simulation at "
            "entry. The verdict banner summarizes whether the sim was "
            "within ±15pp of reality, and in which direction divergence "
            "went — a fast feedback loop for model calibration."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "outcome_distribution", "label": "Outcome Distribution"},
            {"engine": "e14", "slug": "actions", "label": "Actions"},
        ],
    },

    "actions": {
        "title": "Actions",
        "spec": (
            "Operational hand-offs after a run:\n"
            "- Save to Trade Log: persists scenario + entry context to "
            "the shared journal so Post-Trade Review can score it "
            "later. Includes the reconcile snapshot, regime tag, and "
            "modifier state at entry so the post-trade loop is "
            "apples-to-apples against the actual close.\n"
            "- Copy Chat Summary: builds a text summary and copies it "
            "to the clipboard so you can paste into NRGX Chat for a "
            "human-in-the-loop discussion with the senior-quant advisor."
        ),
        "related_cards": [
            {"engine": "e14", "slug": "post_trade_review", "label": "Post-Trade Review"},
        ],
    },

}
