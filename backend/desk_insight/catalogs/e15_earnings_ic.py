"""Desk Insight catalog — Engine 15 (Earnings IC Scenario Advisor).

Migrated from ``backend/engine15/card_explain.py``; kills the old
monkey-patch and adds a proper engine-specific system-prompt identity.
"""
from __future__ import annotations

ENGINE_META = {
    "id":          "e15",
    "name":        "Engine 15 v2 — Earnings Scenario Command Deck",
    "description": (
        "Single-name earnings iron-condor replay and validator. Runs "
        "THIS ticker's prior earnings events through Engine 14's replay "
        "machinery using the desk's chosen strikes and planned exit, "
        "then cross-checks against Engine 1's Wing Console MAE pool. "
        "Single-page command deck: required earnings date + timing, "
        "optional handoff from E1 Wing Console top pick, MI v2 regime "
        "probabilities, MTM timeline, outcome distribution, empirical "
        "intraday-crush factor, and an E1 convergence/divergence badge."
    ),
    "asset_class": "single-name equities — earnings cycle",
}


CATALOG = {

    "e1_summary_strip": {
        "title": "Engine 1 Summary",
        "spec": (
            "Condensed Engine 1 context for the ticker: current spot, 1σ EM "
            "percent, VRP score, IV elevation, straddle EM, strike targets, "
            "MI v2 regime probabilities, and breach-rate at 1.0x/1.5x/2.0x "
            "EM multiples.\n"
            "- spot: current close used as entry anchor.\n"
            "- 1σ EM percent: ATM-straddle expected move to expiry.\n"
            "- VRP score (-100..+100): positive = IV pricier than realized "
            "(tailwind); negative = cheap IV (headwind).\n"
            "- regimeMiV2: HMM state probabilities + vol_state (new in E15 "
            "v2; replaces the scalar regime label).\n"
            "- e1WingMAE: MAE p95 from E1's event pool (used by the E15 "
            "cross-check badge).\n"
            "E1 v2 deliberately suppresses the desk-consensus verdict "
            "from this strip — the Command Deck runs ASSUMING the desk "
            "has committed to the trade and is validating placement, "
            "not re-litigating go/no-go."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "wing_console_mini", "label": "Wing Console (E1 top picks)"},
            {"engine": "e15", "slug": "entry_state", "label": "Entry State"},
            {"engine": "e15", "slug": "e1_wing_mae_crosscheck", "label": "E1 Cross-Check Badge"},
            {"engine": "e1",  "slug": "wing_console", "label": "E1 Wing Decision Console"},
            {"engine": "e1",  "slug": "vrp_analysis", "label": "E1 VRP Analysis"},
        ],
    },

    "wing_console_mini": {
        "title": "Wing Console (E1 top picks)",
        "spec": (
            "Compact top-3 slice of Engine 1's Wing Decision Console for "
            "the same ticker + earnings event. Lets the desk see how the "
            "scenario they're replaying compares to the ranked best "
            "placements E1 scored — click any row to jump back to E1 or "
            "press 'Simulate Top Pick' to re-run the Command Deck with "
            "that placement's strikes pre-filled.\n"
            "Each row: em_mult, wing_pts, short put / call strikes, "
            "estimated credit (in dollars), composite score (0-100), "
            "breach gap probability, and theta capture percent."
        ),
        "related_cards": [
            {"engine": "e1",  "slug": "wing_console", "label": "Full E1 Wing Console"},
            {"engine": "e15", "slug": "entry_state", "label": "Entry State"},
            {"engine": "e15", "slug": "e1_wing_mae_crosscheck", "label": "Cross-Check Badge"},
        ],
    },

    "e1_wing_mae_crosscheck": {
        "title": "E1 Cross-Check Badge",
        "spec": (
            "Convergence / divergence badge that cross-validates E1's "
            "MAE p95 pool (% price move at the 95th percentile worst "
            "excursion) against E15's white-knuckle + breach bucket "
            "percent.\n"
            "Both pools are built from the same historical earnings "
            "events for this ticker, so they should tell the same "
            "story. When they agree (convergent, divergence < 0.25) "
            "the desk has high confidence in the tail estimate. When "
            "they disagree materially (divergent, divergence >= 0.5) "
            "something is off — usually a stale chain cache for some "
            "events, a bad EM override, or a placement E1 hasn't "
            "re-scored against the desk's strikes. The badge says "
            "'re-scan before acting on either' in that case."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "outcome_distribution_empirical", "label": "E15 Outcome Distribution"},
            {"engine": "e1",  "slug": "mae_distribution", "label": "E1 MAE Pool"},
            {"engine": "e1",  "slug": "wing_console", "label": "E1 Wing Console"},
        ],
    },

    "crush_reading": {
        "title": "Intraday Crush Reading",
        "spec": (
            "Per-ticker, tape-driven estimate of how much of the "
            "close-to-close earnings move has played out by the desk's "
            "planned AM exit.\n"
            "- factor: 1.0 means the whole close-to-close move is "
            "realized by morning; 0.5 means half. Computed as the "
            "median |entry-day PnL / close PnL| ratio across the "
            "analogue pool.\n"
            "- source: 'empirical' when sample >= 3 analogue events, "
            "'fixed' (0.80 default) when the pool is thin.\n"
            "- n_events: number of analogues that contributed.\n"
            "- p25/p50/p75: interquartile spread of the per-event "
            "ratios so the desk can see how consistent the crush is "
            "across history.\n"
            "Replaces the legacy hard-coded 0.80 blend that the old "
            "fidelity caveat called out as Phase 2 work."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "planned_exit_timing", "label": "Planned Exit Timing"},
            {"engine": "e15", "slug": "mtm_timeline", "label": "MTM Timeline"},
        ],
    },

    "event_analogue_row": {
        "title": "Historical Event Row",
        "spec": (
            "One row per prior earnings event in the replay pool. "
            "Columns:\n"
            "- earnDate: historical announcement date.\n"
            "- anncTod: BMO/AMC at that historical event.\n"
            "- mapped strikes: user's strikes translated into the "
            "analogue's strike space preserving EM-distance.\n"
            "- outcome: earlyTarget / fullCollect / whiteKnuckle / "
            "stopOut / breach — see planned-exit outcome card.\n"
            "- pnlPct: P&L at planned-exit boundary as % of credit.\n"
            "- MAE: worst drawdown during hold, % of credit.\n"
            "- realizedMovePct: underlying move from pre-earnings close "
            "to post-earnings session.\n"
            "- breached: short-strike taken out at planned exit."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "matched_events", "label": "Matched Events"},
            {"engine": "e15", "slug": "outcome_distribution_empirical", "label": "Outcome Distribution"},
        ],
    },

    "vrp_crush_verdict": {
        "title": "VRP / Vol Crush Verdict",
        "spec": (
            "Combines Engine 1's VRP analysis with the planned-exit "
            "fidelity note to tell the desk whether the IV crush from "
            "earnings is likely to materialize favorably during the "
            "hold.\n"
            "- tailwind: high positive VRP + confirmed anncTod → crush is "
            "likely meaningful; WR inflated in the adjusted view.\n"
            "- headwind: negative VRP (IV cheap vs realized) → crush may "
            "be shallow; adjusted WR pulls down.\n"
            "- neutral: VRP within ±20 pts; empirical distribution "
            "dominates."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "adjusted_distribution", "label": "Adjusted Distribution"},
            {"engine": "e15", "slug": "conditioning_modifiers", "label": "Conditioning Modifiers"},
            {"engine": "e1",  "slug": "vrp_analysis", "label": "VRP Analysis (E1)"},
        ],
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
            "Adjacent bucket shows adjustedOutcomeDistribution — same "
            "buckets reweighted by conditioning (VRP, anncConfidence, "
            "calendar, guidance risk)."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "adjusted_distribution", "label": "Adjusted Distribution"},
            {"engine": "e15", "slug": "mtm_timeline", "label": "MTM Timeline"},
            {"engine": "e15", "slug": "matched_events", "label": "Matched Events"},
        ],
    },

    "entry_state": {
        "title": "Entry State",
        "spec": (
            "Entry-state strip for Engine 15:\n"
            "- userSpot: close at/near request.entryDate, used to map "
            "strikes into analogue space.\n"
            "- 1σ EM%: market-implied 1σ expected move over (entry → "
            "expiry); sourced from cached chain IV when available, else "
            "E1 currentImpliedMovePct or a 30% IV fallback.\n"
            "- wingWidth: narrowest wing in points.\n"
            "- eventsUsed / eventsConsidered: analogues that priced vs "
            "the admitted pool. A gap indicates cache thinness — run the "
            "admin backfill endpoint."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "planned_exit_timing", "label": "Planned Exit Timing"},
            {"engine": "e15", "slug": "credit_richness", "label": "Credit Richness"},
            {"engine": "e15", "slug": "matched_events", "label": "Matched Events"},
        ],
    },

    "planned_exit_timing": {
        "title": "Planned Exit Timing",
        "spec": (
            "Summarizes the hard time-stop the replay obeys:\n"
            "- plannedExitDate: calendar date the desk intends to flatten.\n"
            "- hours after open: 1-4 hours typical for BMO vol crush.\n"
            "- holdBizDays: biz-day gap from entry to planned exit (≥0).\n"
            "- intradayCrushFactor: ORATS historical is EOD, so AM exit is "
            "approximated by blending the close-to-close move by this "
            "factor toward the entry-day P&L. 0.80 means ~80% of the "
            "full day's crush has played out by morning.\n"
            "- fidelityCaveat: plain-English explanation of the "
            "approximation, shown as a chip."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "entry_state", "label": "Entry State"},
            {"engine": "e15", "slug": "exit_rules_card", "label": "Exit Rules"},
            {"engine": "e15", "slug": "mtm_timeline", "label": "MTM Timeline"},
        ],
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
        "related_cards": [
            {"engine": "e15", "slug": "conditioning_modifiers", "label": "Conditioning Modifiers"},
            {"engine": "e15", "slug": "adjusted_distribution", "label": "Adjusted Distribution"},
        ],
    },

    "exit_rules_card": {
        "title": "Exit Rules (Planned Hold)",
        "spec": (
            "Recommended PT/SL inside the planned hold window. Because "
            "the time stop is hard-capped at plannedExitDate, the grid "
            "only explores profit-target and stop-loss axes (per-DTE "
            "targets and trailing stops are suppressed). deltaFromDefault "
            "shows WR / avgPnl improvement vs the user's entered PT/SL "
            "at scan time."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "planned_exit_timing", "label": "Planned Exit Timing"},
            {"engine": "e15", "slug": "expected_value", "label": "Expected Value"},
        ],
    },

    "outcome_distribution_empirical": {
        "title": "Outcome Distribution (Empirical)",
        "spec": (
            "Base outcome distribution from the historical replay pool "
            "BEFORE any earnings-conditioning modifiers are applied. Each "
            "analogue's planned-exit P&L is bucketed using the user's "
            "PT/SL into one of five mutually-exclusive outcomes summing "
            "to 100%:\n"
            "- earlyTarget: profit target hit before planned exit.\n"
            "- fullCollect: held to planned exit, finished positive.\n"
            "- whiteKnuckle: ended positive but MAE dipped into stop-loss "
            "territory.\n"
            "- stopOut: stop-loss tripped OR planned exit closed negative.\n"
            "- breach: short strike actually breached at exit (P&L ≤ "
            "-50%).\n"
            "fillModel determines NBBO (conservative, default) vs mid "
            "(aspirational)."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "adjusted_distribution", "label": "Adjusted Distribution"},
            {"engine": "e15", "slug": "matched_events", "label": "Matched Events"},
            {"engine": "e15", "slug": "mtm_timeline", "label": "MTM Timeline"},
        ],
    },

    "adjusted_distribution": {
        "title": "Adjusted Distribution (after conditioning)",
        "spec": (
            "Same five buckets as the empirical distribution but "
            "REWEIGHTED by Engine 15's conditioning modifiers — VRP / "
            "vol-crush score, anncTod confidence, calendar proximity "
            "(FOMC/CPI inside hold window), and guidance-risk shim from "
            "Engine 1.\n"
            "- netTailMultiplier > 1: tails widened (more breach + "
            "stopOut).\n"
            "- netWinRateShiftPct > 0: WR pulled UP.\n"
            "If empirical and adjusted are within ~3pp on every bucket, "
            "conditioning is neutral — treat empirical as authoritative. "
            "When they diverge, lean on adjusted."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "outcome_distribution_empirical", "label": "Empirical Distribution"},
            {"engine": "e15", "slug": "conditioning_modifiers", "label": "Conditioning Modifiers"},
            {"engine": "e15", "slug": "vrp_crush_verdict", "label": "VRP Verdict"},
        ],
    },

    "conditioning_modifiers": {
        "title": "Conditioning Modifiers",
        "spec": (
            "Per-driver breakdown of how Engine 15 tilted the empirical "
            "distribution into the adjusted distribution. Each card "
            "shows the modifier name, tail multiplier (×N tails), WR "
            "shift (pp), and a human-readable reason.\n"
            "- VRP score (E1): positive = IV pricier than realized; "
            "tail narrows, WR ticks up.\n"
            "- anncConfidence: 0 when anncTod confirmed; positive "
            "penalty when pool mixes UNK or mismatched timing.\n"
            "- calendar: macro events (FOMC/CPI) inside [entry, "
            "plannedExit] widen tails.\n"
            "- guidanceRisk: E1 eventRisk shim — high = company "
            "guidance vol risk.\n"
            "Modifiers with magnitude under ±2pp WR are dropped."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "adjusted_distribution", "label": "Adjusted Distribution"},
            {"engine": "e1",  "slug": "event_risk", "label": "Event Risk (E1)"},
            {"engine": "market-intel", "slug": "morning_brief", "label": "Macro Calendar"},
        ],
    },

    "mtm_timeline": {
        "title": "MTM Timeline (P10 / P50 / P90)",
        "spec": (
            "Cross-sectional distribution of mark-to-market P&L (as a % "
            "of credit) across replayed analogue paths, plotted at each "
            "business day from entry (D0) to planned exit. At each day "
            "we take all paths still alive, sort their MTM P&L, and plot "
            "three percentiles:\n"
            "- P50 (blue, median): the typical analogue path.\n"
            "- P10 (red, lower decile): 'if it goes wrong' envelope.\n"
            "- P90 (green, upper decile): 'if it works beautifully' "
            "envelope.\n"
            "The P90-P10 gap is dispersion at that day — should fan out "
            "through the hold. Wide D0 gap = heterogeneous pool."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "outcome_distribution_empirical", "label": "Outcome Distribution"},
            {"engine": "e15", "slug": "expected_value", "label": "Expected Value"},
        ],
    },

    "expected_value": {
        "title": "Expected Value",
        "spec": (
            "Summary stats across every replayed path's planned-exit "
            "P&L (% of credit):\n"
            "- Mean P&L: arithmetic average across analogues.\n"
            "- Median P&L: 50th-percentile path; less tail-skewed.\n"
            "- Sharpe-proxy: mean / stdev; crude quality score (>0.5 = "
            "strong, 0.0–0.3 = noisy, <0 = expected loser).\n"
            "- FullCollect 90% CI: bootstrap CI on the FullCollect %.\n"
            "If Mean << Median, a few large losers drag the average — "
            "size for the left skew, not the median's optimism."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "outcome_distribution_empirical", "label": "Outcome Distribution"},
            {"engine": "e15", "slug": "mtm_timeline", "label": "MTM Timeline"},
        ],
    },

    "matched_events": {
        "title": "Matched Events (Replay Pool)",
        "spec": (
            "One row per historical earnings event in the replay pool. "
            "This is the AUDIT TRAIL behind every percentage on the "
            "page. Columns: Earn Date, Timing (BMO/AMC), Entry/Exit/"
            "Expiry, Outcome bucket, Exit Day, P&L, MAE, Breached flag, "
            "EM% vs Realized% (EM > Realized → vol-crush tailwind played "
            "out), Analogue Credit. Compare your credit to these using "
            "the Credit Richness chip."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "outcome_distribution_empirical", "label": "Outcome Distribution"},
            {"engine": "e15", "slug": "dropped_events", "label": "Dropped Events"},
            {"engine": "e15", "slug": "credit_richness", "label": "Credit Richness"},
        ],
    },

    "dropped_events": {
        "title": "Dropped Events",
        "spec": (
            "Events the matcher CONSIDERED but excluded, with drop "
            "reason. Common reasons: chain-cache miss, season filter "
            "(when 'Same quarter only' is on), bad-print outliers, "
            "anncTod mismatch, or no usable bar at the analogue's entry/"
            "exit dates.\n"
            "Long relative to Matched Events = thin pool → run backfill "
            "or loosen season filter. Dropped > matched = yellow flag; "
            "> 2× matched = red flag."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "matched_events", "label": "Matched Events"},
            {"engine": "e15", "slug": "notes_caveats", "label": "Notes & Caveats"},
        ],
    },

    "notes_caveats": {
        "title": "Notes & Caveats",
        "spec": (
            "Free-form annotations the simulator emits when a replay had "
            "non-trivial assumptions or fallbacks: chain-cache thinness, "
            "intraday-crush approximations, missing strikes mapped to "
            "nearest, credit-richness verdict, anncTod imputed, EM "
            "source = fallback, etc.\n"
            "Treat as footnotes on confidence. If >2 notes fire, flag "
            "the trade as advisory only and verify the live fill manually."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "dropped_events", "label": "Dropped Events"},
            {"engine": "e15", "slug": "actions_panel", "label": "Actions"},
        ],
    },

    "actions_panel": {
        "title": "Actions",
        "spec": (
            "Three desk actions on the result panel:\n"
            "- Log to Journal: persists the scenario into the shared "
            "E1/E15 trade journal so /review can compare predicted vs "
            "realized P&L after close.\n"
            "- Run LLM Advisor: invokes the narrative advisor LLM which "
            "returns verdict (GO / HOLD / PASS), confidence, key points, "
            "risks, adjustments.\n"
            "- Reconcile vs Live: cross-checks entered credit against "
            "live NBBO mid/bid/ask; surfaces a credit chip (match / "
            "drift / mismatch)."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "credit_richness", "label": "Credit Richness"},
            {"engine": "e15", "slug": "notes_caveats", "label": "Notes & Caveats"},
        ],
    },

    "credit_richness": {
        "title": "Credit Richness",
        "spec": (
            "Compares the user's entered credit to historical mean / "
            "median entry credit of the matched analogue pool.\n"
            "- user_rich (Δ ≥ +15%): paid richly vs history — verify the "
            "fill is realistic before celebrating (pre-market NBBO can "
            "mislead).\n"
            "- user_cheap (Δ ≤ -15%): selling cheap — wait for the open "
            "or move strikes closer.\n"
            "- user_fair (±15%): in-line with typical analogue placement."
        ),
        "related_cards": [
            {"engine": "e15", "slug": "matched_events", "label": "Matched Events"},
            {"engine": "e15", "slug": "actions_panel", "label": "Actions"},
        ],
    },

}
