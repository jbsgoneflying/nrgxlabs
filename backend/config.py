from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Tuple

# Canonical mapping: UI engine number -> backend module name and API prefix.
# UI numbering is the user-facing standard. Backend file/API names are frozen
# for backwards compatibility. Reference this table, not file names, when
# discussing engine numbers in logs, comments, or user-facing text.
ENGINE_REGISTRY = {
    1:  {"name": "Earnings Hold Risk",               "backend": "engine1_breach",      "api": "/api/breach"},
    2:  {"name": "SPX Iron Condor Scanner",           "backend": "engine2_spx_ic",      "api": "/api/spx-ic"},
    3:  {"name": "Global Lead-Lag Regime",            "backend": "engine5_lead_lag",    "api": "/api/engine5"},
    4:  {"name": "Mean-Reversion Scanner (Red Dog)",  "backend": "engine3_red_dog",     "api": "/api/engine3-red-dog"},
    5:  {"name": "Trend-Continuation (Ichimoku)",     "backend": "engine4_ichimoku",    "api": "/api/engine4-ichimoku"},
    6:  {"name": "Thematic Pairs Scanner",            "backend": "engine7_pairs",       "api": "/api/engine7-pairs"},
    7:  {"name": "Post-Event Extension Evaluator",    "backend": "engine8_post_event",  "api": "/api/engine8"},
    8:  {"name": "Credit Stress Drift Detection",     "backend": "engine9_credit",      "api": "/api/engine9"},
    9:  {"name": "Earnings Calendar & Compare",       "backend": "calendar",            "api": "/api/calendar"},
    10: {"name": "Multi-Ticker Compare",              "backend": "engine1_breach",      "api": "/api/breach-compare"},
    11: {"name": "Macro Events & Headline Risk",      "backend": "market_intel",        "api": "/api/news-risk"},
    12: {"name": "VIX Spike Fade",                    "backend": "engine12_vix_fade",   "api": "/api/engine12"},
    13: {"name": "Gap Regime Scanner",                "backend": "engine13_gap_regime", "api": "/api/engine13"},
    14: {"name": "IC Scenario Simulator",             "backend": "engine14_ic_scenario", "api": "/api/ic-scenario"},
}


def _get_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in ("1", "true", "t", "yes", "y", "on"):
        return True
    if s in ("0", "false", "f", "no", "n", "off"):
        return False
    return bool(default)


def _get_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None:
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _get_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return int(default)
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return int(default)


def _get_csv_list(name: str, default: List[str]) -> List[str]:
    v = os.getenv(name)
    if v is None:
        return list(default)
    parts = [p.strip() for p in str(v).split(",")]
    return [p for p in parts if p]


@dataclass(frozen=True)
class FeatureFlags:
    """
    Feature flags are env-driven and must be included in cache keys for determinism.

    Safe defaults:
    - Additive telemetry can default ON without breaking existing fields.
    - Any logic affecting decisioning/stats must default OFF.
    """

    # Guardrails / telemetry
    ADD_EVENT_SHIFT_TELEMETRY: bool = True
    STRICT_REALIZED_WINDOW: bool = False

    # Quant estimators
    USE_BETA_POSTERIOR_FOR_DECISIONING: bool = False
    USE_BETA_CI_FOR_CONFIDENCE: bool = False
    BETA_PRIOR_ALPHA: float = 1.0
    BETA_PRIOR_BETA: float = 1.0

    # Definitions
    ADD_K_CONSISTENT_OVERSHOOT: bool = False

    # Trade builder
    TRADEBUILDER_ENFORCE_OTM: bool = False

    # --- Benzinga (default OFF; additive only) ---
    ENABLE_BENZINGA: bool = False
    BENZINGA_ENABLE_EVENT_RISK: bool = False
    BENZINGA_EVENT_RISK_AFFECTS_REGIME: bool = False
    BENZINGA_EVENT_RISK_AFFECTS_MC: bool = False

    # Event-risk tuning (bounded, explainable knobs)
    BENZINGA_EVENT_RISK_HIGH_THRESHOLD: float = 0.66
    BENZINGA_EVENT_RISK_CAUTION_THRESHOLD: float = 0.50
    BENZINGA_EVENT_RISK_REGIME_TAIL_BUMP_MAX_PCT: float = 20.0  # max +% bump to tailMultiplier
    BENZINGA_EVENT_RISK_MC_WING_BUMP_MAX_PCT: float = 15.0  # max +% bump to wing distances in MC (risk-only)

    # --- Monte Carlo (default OFF; additive only) ---
    ENABLE_MONTE_CARLO_EARNINGS: bool = True   # v2: MC always on; flag is now a kill-switch only
    MC_ENABLE_CONDITION_ON_QUARTER: bool = False
    MC_ENABLE_CONDITION_ON_REGIME: bool = False
    MC_ENABLE_CONDITION_ON_TRADE_GATE: bool = False
    MC_ENABLE_RECENCY_WEIGHTING: bool = False
    MC_ENABLE_WING_OPTIMIZATION: bool = False
    MC_ENABLE_TAS_STABILITY: bool = False

    MC_N_SIMS: int = 5000
    MC_BOOTSTRAP_N: int = 500
    MC_GLOBAL_SEED: int = 1337
    MC_MIN_POOL: int = 12
    MC_MIN_IMPLIED_MOVE_PCT: float = 0.5
    MC_RECENCY_HALFLIFE_EVENTS: int = 8

    MC_OPT_MAX_MULT_DELTA: float = 0.50
    MC_OPT_STEP: float = 0.05
    MC_MAX_BREACH_EITHER_PCT: float = 25.0
    MC_MAX_CVAR95_TOTAL: float = 0.0  # 0 => disabled (no hard CVaR budget)
    MC_DEFAULT_WING_WIDTH_DOLLARS: float = 5.0  # used only when strikes are unavailable

    # --- Engine 2: SPX weekly IC (default OFF; separate page/endpoint) ---
    ENABLE_ENGINE2_SPX_IC: bool = False

    # --- Engine 2b: Flex-Expiry SPX IC scenario engine ---
    # Sibling of Engine 2 that accepts any (entry_date, expiry) pair so
    # the desk can score non-Friday trades (e.g. Fri -> Tue across a
    # holiday weekend). Default OFF so the public surface doesn't change
    # until the desk explicitly opts in.
    ENABLE_E2B_FLEX_EXPIRY: bool = False

    # --- Engine 3: Red Dog Reversal Scanner (default ON - UI controlled) ---
    ENABLE_ENGINE3_RED_DOG: bool = True
    ENGINE3_CACHE_TTL_BARS: int = 6 * 3600       # 6 hours for daily bars
    ENGINE3_CACHE_TTL_SCAN: int = 30 * 60        # 30 minutes for full scan
    ENGINE3_MAX_WORKERS: int = 10                # Parallel workers for scanning
    ENGINE3_MIN_SCORE_DEFAULT: int = 50          # Default minimum score filter
    ENGINE3_APLUS_THRESHOLD: int = 75            # A+ grade threshold

    # --- Engine 4: Ichimoku Cloud Continuation Scanner (default ON - UI controlled) ---
    ENABLE_ENGINE4_ICHIMOKU: bool = True
    ENGINE4_CACHE_TTL_BARS: int = 6 * 3600       # 6 hours for daily bars
    ENGINE4_CACHE_TTL_SCAN: int = 30 * 60        # 30 minutes for full scan
    ENGINE4_MAX_WORKERS: int = 10                # Parallel workers for scanning
    ENGINE4_MIN_SCORE_DEFAULT: int = 50          # Default minimum score filter
    ENGINE4_APLUS_THRESHOLD: int = 75            # A+ grade threshold
    ENGINE4_MIN_DOLLAR_ADV: float = 20_000_000.0  # 20d avg $ volume liquidity floor
    ENGINE4_STRUCTURE_MAX: int = 8               # Cap the "Approaching" structure list

    # Engine 2 policy knobs (risk-only; env-driven; safe defaults)
    # NOTE (v2): these three flags are retained for backwards compatibility
    # with env deployments but are NOT consumed by the v2 engine:
    #   - ENGINE2_ENTRY_DAYS is documented but the `entry_day` query param
    #     is the source of truth.
    #   - ENGINE2_EM_MULTS is superseded by E2_WING_EM_MULTS (v2 Wing
    #     Console grid). The scan still uses the hard desk-lock
    #     [1.0, 1.5, 2.0] unless the query ?widths= is passed.
    #   - ENGINE2_LOOKBACK_YEARS_DEFAULT is superseded by the query
    #     ?years= (defaults to 2 in v2).
    ENGINE2_ENTRY_DAYS: str = "mon,tue,wed"
    ENGINE2_EM_MULTS: str = "0.7,0.8,0.9,1.0,1.1,1.2"
    ENGINE2_WING_WIDTH_PTS: str = "5,10,15,20,25"
    # If enabled, Engine 2 will only surface VWAP when ORATS provides a true daily VWAP field.
    # Otherwise VWAP is marked unavailable (no proxy fallback).
    ENGINE2_REQUIRE_ORATS_DAILY_VWAP: bool = False

    # v2: the router `weeks_limit` query param is the live pagination knob;
    # ENGINE2_MAX_WEEKS_RETURN remains as a compile-time ceiling (no active use).
    ENGINE2_MAX_WEEKS_RETURN: int = 120
    ENGINE2_LOOKBACK_YEARS_DEFAULT: int = 3

    ENGINE2_POLICY_MAX_BREACH_PCT: float = 25.0
    ENGINE2_POLICY_MAX_OUTSIDE_WINGS_PCT: float = 10.0
    ENGINE2_POLICY_MAX_MAE95_X_WING: float = 1.0

    # Engine 2 AI Trade Advisor
    ENGINE2_MULTI_WING: bool = True
    ENGINE2_ADVISOR_ENABLED: bool = True
    ENGINE2_ADVISOR_MODEL: str = "gpt-5.5"
    ENGINE2_ADVISOR_MAX_CALLS_PER_MINUTE: int = 4
    ENGINE2_TRADE_TTL_S: int = 180 * 86400   # 180 days (survive full earnings cycle)
    ENGINE2_TRADE_MAX_INDEX: int = 200

    # Regime thresholds (score is 0..100)
    ENGINE2_REGIME_LOW_MAX: float = 25.0
    ENGINE2_REGIME_MODERATE_MAX: float = 45.0
    ENGINE2_REGIME_ELEVATED_MAX: float = 65.0

    # Macro proximity model
    ENGINE2_MACRO_LAMBDA: float = 0.35  # exp(-lambda * days_to_event)
    ENGINE2_MACRO_MULTIPLIER_CAP: float = 2.5
    ENGINE2_MACRO_BASE_CPI: float = 1.0
    ENGINE2_MACRO_BASE_FOMC: float = 1.2
    ENGINE2_MACRO_BASE_NFP: float = 0.7
    ENGINE2_MACRO_BASE_OPEX: float = 0.4
    ENGINE2_MACRO_BASE_REFUNDING: float = 0.5
    ENGINE2_MACRO_BASE_FOMC_MINUTES: float = 0.30
    ENGINE2_MACRO_BASE_GDP: float = 0.40
    ENGINE2_MACRO_BASE_PCE: float = 0.35
    ENGINE2_MACRO_BASE_PPI: float = 0.25
    ENGINE2_MACRO_BASE_PMI_ISM: float = 0.20
    ENGINE2_MACRO_BASE_RETAIL_SALES: float = 0.20
    ENGINE2_MACRO_BASE_JOBLESS_CLAIMS: float = 0.08
    ENGINE2_MACRO_BASE_TREASURY_AUCTION: float = 0.05
    ENGINE2_MACRO_BASE_OTHER: float = 0.05

    # --- Engine 2 v2: SPX IC Command Deck ---
    # Kill-switch. When False the new Wing Console endpoints 404 and the
    # frontend reverts to the legacy scan UX. Default ON.
    ENABLE_E2_V2: bool = True

    # Legacy flag parity: E2_EMIT_DESK_CONSENSUS controls whether the
    # public /api/spx-ic response body carries the legacy TRADE /
    # LEAN_PASS / PASS verdict strings. Advisor endpoint re-computes
    # internally either way.
    E2_EMIT_DESK_CONSENSUS: bool = False

    # Wing Console grid (comma-separated floats).
    E2_WING_EM_MULTS: str = "1.0,1.25,1.5,2.0"
    E2_WING_PTS:      str = "5,10,15"

    # Composite-score weights (renormalised at scoring time so the
    # desk can tune one knob without rescaling neighbours).
    E2_WING_SCORE_WEIGHT_CLOSE:  float = 0.25
    E2_WING_SCORE_WEIGHT_TOUCH:  float = 0.20
    E2_WING_SCORE_WEIGHT_MAE:    float = 0.25
    E2_WING_SCORE_WEIGHT_THETA:  float = 0.15
    E2_WING_SCORE_WEIGHT_CREDIT: float = 0.15

    # Score-normalisation targets.
    E2_WING_MAX_TOLERABLE_MAE_PCT: float = 80.0
    E2_WING_TARGET_THETA_PCT:      float = 60.0
    E2_WING_TARGET_ROC_PCT:        float = 12.0

    # Monte Carlo (always on in v2; flag acts as a kill-switch).
    ENABLE_E2_MC: bool = True
    E2_MC_N_SIMS: int = 5000
    E2_MC_MIN_POOL: int = 20
    E2_MC_SEED: int = 1337
    E2_MC_CONDITION_ON_REGIME: bool = True
    E2_MC_CONDITION_ON_MACRO: bool = True
    E2_MC_GBM_FALLBACK: bool = True

    # --- Engine 1: Earnings IC Advisor (vol crush premium harvesting) ---
    E1_ADVISOR_ENABLED: bool = True
    E1_ADVISOR_MODEL: str = "gpt-5.5"
    E1_ADVISOR_MAX_CALLS_PER_MINUTE: int = 4
    E1_EM_MULTS: str = "1.0,1.5,2.0"
    E1_WING_WIDTH_PTS: str = "2.5,5,7.5,10"
    E1_TRADE_TTL_S: int = 180 * 86400   # 180 days (survive full earnings cycle)
    E1_TRADE_MAX_INDEX: int = 200

    # --- Engine 10: Multi-Ticker Portfolio Advisor ---
    E10_ADVISOR_ENABLED: bool = True
    E10_ADVISOR_MODEL: str = "gpt-5.5"
    E10_ADVISOR_MAX_CALLS_PER_MINUTE: int = 3

    # --- Engine 1: GO / NO-GO decisioning (strict; additive UI) ---
    GO_IVP_MIN: float = 0.80
    GO_IV_SAMPLE_MIN: int = 20
    GO_IV30_FLOOR: float = 0.30  # 0.30 == 30% (see backend/go_no_go.py)
    GO_IV_Z_ENABLED: bool = True
    GO_IV30_Z_MIN: float = 0.75

    GO_MIN_EARNINGS_N: int = 6
    GO_EM_RICHNESS_MULT: float = 1.05

    GO_TAIL_SAMPLE_MIN: int = 8
    GO_TAIL_P90_MULT: float = 0.80

    GO_CORR20_HIGH: float = 0.70
    GO_BETA20_HIGH: float = 1.20

    GO_AVG_DOLLAR_VOL20D_MIN: float = 200_000_000.0
    GO_AVG_DOLLAR_VOL20D_BLOCK: float = 20_000_000.0
    # Delta band for liquidity check — covers the IC short-strike zone
    # 0.10 to 0.25 spans aggressive (10d) to standard (25d) wing placement
    GO_OPT_DELTA_BAND_LO: float = 0.10
    GO_OPT_DELTA_BAND_HI: float = 0.25
    GO_OPT_SPREAD_MAX: float = 0.15
    GO_OPT_SPREAD_BLOCK: float = 0.30
    GO_OPT_SPREAD_MAX_P90: float = 0.25
    GO_OPT_SPREAD_P90_BLOCK: float = 0.50
    GO_OPT_MIN_MID: float = 0.20
    GO_OPT_OI_MIN: float = 500.0
    GO_OPT_VOL_MIN: float = 50.0
    GO_BAND_QUOTE_COVERAGE_MIN: float = 0.70
    GO_BAND_OI_SUM_MIN: float = 2000.0
    GO_BAND_OI_SUM_BLOCK: float = 200.0
    GO_BAND_VOL_SUM_MIN: float = 200.0
    GO_BAND_VOL_SUM_BLOCK: float = 20.0

    GO_RV5_JUMP_MAX: float = 1.15
    GO_RV20_JUMP_MAX: float = 1.10
    GO_RV5_ACCEL_TIGHTEN_TRIGGER: float = 1.05
    GO_FLIP_CUTOFF_BASE: float = 2.0
    GO_FLIP_CUTOFF_TIGHT: float = 2.5

    GO_FORCED_FLOW_WINDOW_TRADING_DAYS: int = 4
    GO_FORCED_FLOW_IMPORTANCE_HIGH_MIN: int = 4
    GO_FORCED_FLOW_IMPORTANCE_MED_MIN: int = 3
    GO_FORCED_FLOW_MANUAL_RANGES: Tuple[str, ...] = ()

    # --- Engine 5: Global Lead-Lag Engine (default OFF) ---
    ENABLE_ENGINE5_LEAD_LAG: bool = True
    ENGINE5_CACHE_TTL_LATEST: int = 48 * 3600        # 48h Redis TTL for latest snapshots
    ENGINE5_CACHE_TTL_HISTORY: int = 180 * 86400      # 180d Redis TTL for durable bar history
    ENGINE5_CORR_WINDOW: int = 20                     # Rolling correlation window (trading days)
    ENGINE5_CORR_THRESHOLD: float = 0.40              # Min |correlation| for signal
    ENGINE5_Z_SIGNIFICANT: float = 1.50               # Magnitude z-score threshold
    ENGINE5_REGIME_STRESSED_THRESHOLD: float = 75.0   # Higher score = more stress
    ENGINE5_REGIME_RISK_OFF_THRESHOLD: float = 55.0
    ENGINE5_REGIME_TRANSITIONAL_THRESHOLD: float = 30.0
    ENGINE5_MAX_LAG_DAYS: int = 5
    ENGINE5_LOOKBACK_DAYS: int = 60                   # History window for correlations
    ENGINE5_FRESHNESS_RETRY_COUNT: int = 3            # Data freshness guard retries
    ENGINE5_FRESHNESS_RETRY_INTERVAL_S: int = 900     # 15 min between retries

    # --- Engine 5: Immutable snapshots ---
    ENGINE5_SNAPSHOT_TTL_S: int = 14 * 86400             # 14 days
    ENGINE5_SNAPSHOT_INDEX_TTL_S: int = 30 * 86400        # 30 days
    ENGINE5_SNAPSHOT_MAX_INDEX: int = 50                   # max snapshots in index
    ENGINE5_SNAPSHOT_BEST_MAX_AGE_DAYS: int = 14           # search window for view=best

    # --- Engine 5: Vol Lead-Lag sub-module ---
    ENGINE5_VOL_LEADLAG_ENABLED: bool = True
    ENGINE5_GLOBAL_VOL_RISING_THRESHOLD: float = 0.75   # GlobalVolScore > this = rising
    ENGINE5_GLOBAL_VOL_FALLING_THRESHOLD: float = -0.75  # GlobalVolScore < this = falling
    ENGINE5_GLOBAL_VOL_NOISE_FLOOR: float = 0.40         # |score| < this = NORMAL
    ENGINE5_US_IV_LOW_THRESHOLD: float = 30.0             # IV rank < this = LOW
    ENGINE5_US_IV_HIGH_THRESHOLD: float = 60.0            # IV rank > this = HIGH
    ENGINE5_VOL_ZSCORE_WINDOW: int = 60                   # Rolling z-score window (trading days)

    # --- Engine 7: Thematic Relative Value / Pairs Engine ---
    ENABLE_ENGINE7_PAIRS: bool = True
    ENGINE7_CACHE_TTL_BARS: int = 6 * 3600          # 6h for daily bars
    ENGINE7_CACHE_TTL_SCAN: int = 30 * 60            # 30min for full scan
    ENGINE7_MAX_WORKERS: int = 8                     # Parallel workers
    ENGINE7_MIN_SCORE_DEFAULT: int = 50              # Minimum confidence threshold
    ENGINE7_APLUS_THRESHOLD: int = 75                # A+ grade cutoff
    ENGINE7_Z_SCORE_WINDOW: int = 40                 # Default rolling window (20-60)
    ENGINE7_Z_ENTRY_THRESHOLD: float = 1.5           # Min |z| for mean-reversion
    ENGINE7_Z_MOMENTUM_THRESHOLD: float = 1.0        # Min |z| for momentum mode
    ENGINE7_MAX_CONCURRENT_PAIRS: int = 5            # Max positions
    ENGINE7_THEME_REQUIRED: bool = True              # Enforce theme validation (INV-2)
    ENGINE7_ENABLE_ORATS_VOL: bool = True            # ORATS IV overlay for pairs scoring (INV-5)
    ENGINE7_ENABLE_LLM_ANNOTATION: bool = False      # Optional LLM theme annotation (INV-1)
    ENGINE7_ENABLE_LLM_THEME_SCORING: bool = True   # LLM-enhanced theme scoring (demote false positives, catch missed themes)
    ENGINE7_OVERLAP_CORR_THRESHOLD: float = 0.70     # Ratio-return correlation cap (INV-3)
    ENGINE7_OVERLAP_CORR_WINDOW: int = 20            # Rolling correlation window (trading days)

    # --- Engine 7: Gating (INV-4: all inputs optional with safe defaults) ---
    GATE_PAIRS_REGIME_ALLOW: str = ""                # Empty = all regimes allowed
    GATE_PAIRS_VOL_STATE_ALLOW: str = ""             # Empty = all states allowed

    # --- Engine 8: Post-Event Trade Extension ---
    ENABLE_ENGINE8_POST_EVENT: bool = True

    # --- Engine 9: Credit Stress Drift ---
    ENABLE_ENGINE9_CREDIT_STRESS: bool = True
    ENGINE9_CACHE_TTL_SCAN: int = 5 * 60            # 5 min in-memory scan cache
    ENGINE9_MAX_WORKERS: int = 8                     # Parallel price fetch workers
    ENGINE8_CACHE_TTL_EVAL: int = 30 * 60            # 30 min in-memory cache for evaluations
    ENGINE8_SNAPSHOT_TTL_S: int = 30 * 86400          # 30 days Redis TTL for persisted snapshots
    ENGINE8_LLM_RESULT_TTL_S: int = 90 * 86400        # 90 days Redis TTL for persisted LLM results
    ENGINE8_MAX_WORKERS: int = 5                       # Parallel evaluation workers
    ENGINE8_CONFIDENCE_THRESHOLD: int = 65             # Min score for any trade
    ENGINE8_CONTINUE_THRESHOLD: int = 65               # Min score for CONTINUE
    ENGINE8_FADE_THRESHOLD: int = 70                   # Min score for FADE (higher bar)
    ENGINE8_MIN_HISTORICAL_SAMPLE: int = 8              # Min similar events; below this, try relaxed matching
    ENGINE8_EM_RATIO_OVER: float = 1.20                # move_vs_em above this = "over"
    ENGINE8_EM_RATIO_EXTREME: float = 1.50             # move_vs_em above this = "extreme"
    ENGINE8_ATR_ELEVATED: float = 1.50                 # ATR multiple threshold
    ENGINE8_ATR_EXTREME: float = 2.50                  # ATR multiple extreme threshold
    ENGINE8_MAX_RISK_UNITS: float = 1.5                # Max risk allocation per trade
    ENGINE8_MIN_RISK_UNITS: float = 0.5                # Min risk allocation per trade
    ENGINE8_MAX_HOLDING_DAYS: int = 5                  # Max holding period
    ENGINE8_CONTINUATION_PROB_MIN: float = 0.55        # Min probability for CONTINUE
    ENGINE8_REVERSION_PROB_MIN: float = 0.55           # Min probability for FADE
    ENGINE8_ENABLE_LLM_CLASSIFY: bool = True           # Use LLM for event classification
    ENGINE8_LLM_MODEL_VERSION: str = "gpt-5.5"
    ENGINE8_LOOKBACK_EVENTS: int = 40                  # Historical events to consider
    ENGINE8_MAX_CONTROLLED_LOSS_PCT: float = 50.0      # Max % of entry credit loss for "controlled_loss"

    # --- Engine 12: VIX Spike Fade / Vol Dislocation Engine ---
    ENABLE_ENGINE12_VIX_FADE: bool = True
    ENGINE12_CACHE_TTL_SCAN: int = 15 * 60              # 15 min in-memory scan cache
    ENGINE12_MC_N_SIMS: int = 10000                     # Monte Carlo simulations
    ENGINE12_MC_SEED: int = 42                          # Deterministic seed
    ENGINE12_OU_CALIBRATION_LOOKBACK_DAYS: int = 1260   # 5 years of VIX history
    ENGINE12_MAX_WORKERS: int = 4                       # Parallel data fetch workers
    ENGINE12_SECONDARY_SPIKE_THRESHOLD: float = 0.25    # 25% prob -> structure gating
    ENGINE12_CONTAINED_THRESHOLD: float = 0.60          # 60% prob -> aggressive short vol
    ENGINE12_DEALER_GAMMA_ENABLED: bool = True          # Toggle dealer gamma integration
    ENGINE12_STRESS_WEIGHT_OIL: float = 0.30
    ENGINE12_STRESS_WEIGHT_GOLD: float = 0.20
    ENGINE12_STRESS_WEIGHT_HYG: float = 0.20
    ENGINE12_STRESS_WEIGHT_DXY: float = 0.15
    ENGINE12_STRESS_WEIGHT_TLT_VOL: float = 0.15
    ENGINE12_GAMMA_AMP_LOW: float = 0.10                # Dealer gamma amplification factors
    ENGINE12_GAMMA_AMP_MED: float = 0.20
    ENGINE12_GAMMA_AMP_HIGH: float = 0.30

    # --- Engine 13: Gap Regime Scanner ---
    ENABLE_ENGINE13_GAP_REGIME: bool = True
    ENGINE13_CACHE_TTL_SCAN: int = 10 * 60              # 10 min scan cache
    ENGINE13_ADVISOR_MODEL: str = "gpt-5.5"
    ENGINE13_GAP_THRESHOLD_PCT: float = 1.5             # Min gap % for analogues
    ENGINE13_FRAGILITY_ENABLED: bool = True
    ENGINE13_FRAGILITY_W_OPTIONS: float = 0.30
    ENGINE13_FRAGILITY_W_CROSS_ASSET: float = 0.25
    ENGINE13_FRAGILITY_W_HISTORICAL: float = 0.20
    ENGINE13_FRAGILITY_W_HEADLINE: float = 0.15
    ENGINE13_FRAGILITY_W_PRICE_ACTION: float = 0.10

    # --- Engine 14: IC Scenario Simulator (path-dependent replay) ---
    # Default OFF: requires an ORATS historical chain cache to be backfilled
    # via scripts/engine14_backfill_chains.py before it can produce payloads.
    ENABLE_ENGINE14_IC_SCENARIO: bool = False
    ENGINE14_CACHE_TTL_SCAN: int = 10 * 60           # 10 min in-memory request cache
    ENGINE14_LOOKBACK_YEARS: int = 2                  # depth of chain backfill
    ENGINE14_MIN_ANALOGUES: int = 20                  # min historical pool to return a payload
    ENGINE14_MAX_ANALOGUES: int = 250                 # cap returned analogue detail
    ENGINE14_REGIME_BUCKET_TOL: float = 12.0          # score points; +/- vs entry regime to admit
    ENGINE14_DEFAULT_PROFIT_TARGET_PCT: float = 50.0  # exit at 50% of credit
    ENGINE14_DEFAULT_STOP_LOSS_PCT: float = 200.0     # stop at -200% of credit
    ENGINE14_SQLITE_PATH: str = "data/engine14_chains.db"
    ENGINE14_STRIKE_SNAP_MAX_PTS: float = 5.0         # max SPX points we will snap a strike
    # NOTE (v2): retained for BC with env deployments; NOT consumed by v2.
    # The canonical fill-model knob is ENGINE14_FILL_MODEL below.
    ENGINE14_BID_ASK_FILL_MODE: str = "mid"           # legacy alias; use ENGINE14_FILL_MODEL
    ENGINE14_ENABLE_CONDITIONING: bool = True         # Phase 2 modifiers (calendar/gamma/stress/gap)
    ENGINE14_ADMIN_TOKEN: str = ""                    # header token for /api/ic-scenario/backfill
    ENGINE14_BACKFILL_MAX_YEARS: float = 3.0          # safety cap on admin-triggered backfills

    # Phase A — fill realism & placement filter
    ENGINE14_FILL_MODEL: str = "nbbo"                 # mid|nbbo|mid_penalty
    ENGINE14_FILL_PENALTY_PCT: float = 15.0           # % of half-spread in mid_penalty mode
    ENGINE14_EMIT_LEGACY_MID_DIST: bool = True        # emit outcomeDistributionMid side-by-side
    # NOTE (v2): retained for BC; NOT consumed by v2. Entry-fill validation
    # lives in /api/ic-scenario/pre-check now.
    ENGINE14_ENTRY_FILL_CHECK: bool = False           # (legacy) no-op in v2
    ENGINE14_EM_MULTIPLE_TOL: float = 0.25            # sigma tolerance on short-strike EM match
    ENGINE14_ENABLE_EM_MULTIPLE_FILTER: bool = True   # Phase A3 placement filter on/off
    ENGINE14_MAE_PROXY_ENABLED: bool = True           # inflate MAE using underlying OHLC range

    # Phase B — empirical modifier coefficients (learned from cache)
    ENGINE14_MODIFIER_COEFFICIENTS_PATH: str = "data/engine14_modifier_coefficients.json"

    # Phase C — multi-factor regime match
    ENGINE14_REGIME_FEATURES_PATH: str = "data/engine14_regime_features.db"
    ENGINE14_ENABLE_KNN_REGIME: bool = False          # gated until features backfilled
    ENGINE14_KNN_TOP_N: int = 80                      # size of KNN analogue pool

    # Phase D — bootstrap CIs
    ENGINE14_BOOTSTRAP_B: int = 500
    ENGINE14_BOOTSTRAP_SEED: int = 14
    ENGINE14_THIN_SAMPLE_N: int = 20                  # below this -> UI warning

    # Phase E — decision utility
    ENGINE14_PER_DTE_EXITS: bool = True
    ENGINE14_ENABLE_SIZING: bool = True
    ENGINE14_ENABLE_GREEKS_ATTRIBUTION: bool = True

    # --- Engine 14 v2 — IC Scenario Command Deck ---
    # Kill-switch. When False the new /wing-console + /wing-console/
    # score-placement + /advisor routes 404 and the frontend reverts to
    # the legacy form-first UX.
    ENABLE_E14_V2: bool = True

    # Parity with E1 / E15 / E2 — strips legacy verdict fields
    # (reconcile.overall + echoed engine2 consensus) from the public
    # /api/ic-scenario response. Advisor endpoint re-computes internally.
    E14_EMIT_DESK_CONSENSUS: bool = False

    # Wing Console grid (comma-separated floats).
    E14_WING_EM_MULTS: str = "1.0,1.25,1.5,2.0"
    E14_WING_PTS:      str = "5,10,15"

    # Composite-score weights (renormalised at scoring time). Plan uses
    # "breach" as the name of the close-breach weight; E2 uses "close".
    # Both names are accepted by WingConsoleWeights.from_flags.
    E14_WING_SCORE_WEIGHT_BREACH: float = 0.25
    E14_WING_SCORE_WEIGHT_TOUCH:  float = 0.20
    E14_WING_SCORE_WEIGHT_MAE:    float = 0.25
    E14_WING_SCORE_WEIGHT_THETA:  float = 0.15
    E14_WING_SCORE_WEIGHT_CREDIT: float = 0.15

    # Score-normalisation targets.
    E14_WING_MAX_TOLERABLE_MAE_PCT: float = 80.0
    E14_WING_TARGET_THETA_PCT:      float = 60.0
    E14_WING_TARGET_ROC_PCT:        float = 12.0

    # Forward Monte Carlo (always on; flag is a kill-switch).
    ENABLE_E14_MC: bool = True
    E14_MC_N_SIMS: int = 5000
    E14_MC_MIN_POOL: int = 20
    E14_MC_SEED: int = 1337
    E14_MC_CONDITION_ON_REGIME: bool = True
    E14_MC_CONDITION_ON_MACRO: bool = True
    E14_MC_GBM_FALLBACK: bool = True

    # NYSE holiday calendar for chain_replay + simulator business-day
    # math. When off, engine14 keeps its legacy session counting.
    ENGINE14_HOLIDAY_CALENDAR: bool = True

    # New E14-native advisor (separate from E2's advisor which /reconcile
    # still calls for BC). Rate-limited per process.
    E14_ADVISOR_ENABLED: bool = True
    E14_ADVISOR_MODEL: str = "gpt-5.5"
    E14_ADVISOR_MAX_CALLS_PER_MINUTE: int = 4

    # --- Engine 15: Earnings IC Scenario Simulator ---
    # Blends Engine 1's single-name earnings data with Engine 14's replay
    # machinery. Analogue pool = this ticker's prior earnings events; exit
    # is a planned date + time-of-day (not expiry). Reuses the existing
    # engine14 chain cache schema (ticker-keyed) — no new DB file.
    # Default ON so the desk-review build can hit /earnings-ic out of the box;
    # flip back to False if you need to silence the engine without redeploying.
    ENABLE_ENGINE15_EARNINGS_IC: bool = True
    ENGINE15_CACHE_TTL_SCAN: int = 10 * 60            # request-level TTL
    ENGINE15_MIN_EVENTS: int = 8                       # min event pool to return a payload
    ENGINE15_MAX_EVENTS: int = 20
    ENGINE15_EVENT_BACKFILL_DAYS_BEFORE: int = 1       # biz days before earnDate to cache
    ENGINE15_EVENT_BACKFILL_DAYS_AFTER: int = 2        # biz days after earnDate to cache
    ENGINE15_FILL_MODEL: str = "nbbo"                 # mid|nbbo|mid_penalty
    ENGINE15_FILL_PENALTY_PCT: float = 15.0
    ENGINE15_STRIKE_SNAP_MAX_PTS: float = 2.5          # single names use tighter snap than SPX
    ENGINE15_DEFAULT_PROFIT_TARGET_PCT: float = 50.0
    ENGINE15_DEFAULT_STOP_LOSS_PCT: float = 150.0
    ENGINE15_INTRADAY_CRUSH_FACTOR: float = 0.80       # EOD-close -> ~10:30AM exit approximation
    ENGINE15_ENABLE_CONDITIONING: bool = True
    ENGINE15_ADMIN_TOKEN: str = ""
    ENGINE15_BACKFILL_DELAY_MS: int = 200              # throttle between ORATS calls during backfill
    ENGINE15_MAX_EXPIRY_OFFSET_DAYS: int = 4           # tolerance when matching analogue expiries
    ENGINE15_EM_MULTIPLE_TOL: float = 0.35             # sigma tol on short-strike placement
    ENGINE15_ENABLE_EM_MULTIPLE_FILTER: bool = False
    ENGINE15_ADVISOR_ENABLED: bool = True
    ENGINE15_ADVISOR_MODEL: str = "gpt-5.5"

    # --- Engine 15 v2 — Earnings Scenario Command Deck ---
    ENABLE_E15_V2: bool = True                         # kill-switch
    ENGINE15_HOLIDAY_CALENDAR: bool = True             # use NYSE trading calendar for biz-day math
    # "empirical" | "fixed" — empirical uses per-ticker historical open→close
    # PnL collapse ratio; fixed falls back to ENGINE15_INTRADAY_CRUSH_FACTOR.
    ENGINE15_INTRADAY_CRUSH_MODE: str = "empirical"
    ENGINE15_GUIDANCE_RISK_ENABLED: bool = True        # Benzinga press/news risk modifier
    E15_EMIT_DESK_CONSENSUS: bool = False              # parity with E1_EMIT_DESK_CONSENSUS
    GUIDANCE_RISK_MAX_BUMP_PCT: float = 15.0           # cap for guidance-risk breach-rate bump

    # --- Gating (Engine 3 & 4) ---
    ENABLE_GATING: bool = True
    # Red Dog is mean-reversion: favored in calm / long-gamma / range tape,
    # consistent with the dealer-gamma overlay (not Stressed + expanding vol).
    GATE_RD_REGIME_ALLOW: str = "Risk-On,Transitional"
    GATE_RD_VOL_STATE_ALLOW: str = "compressing,stable,falling,NORMAL,FALLING,flat,contango"
    GATE_RD_MACRO_PROXIMITY_DAYS: int = 1
    # 20-day average dollar volume floor for the Red Dog universe (liquidity).
    ENGINE3_MIN_DOLLAR_ADV: float = 20_000_000.0
    GATE_ICH_REGIME_ALLOW: str = "Risk-On,Transitional"
    GATE_ICH_VOL_STATE_ALLOW: str = "compressing,stable,NORMAL,FALLING,falling,flat"
    GATE_ICH_MACRO_PROXIMITY_DAYS: int = 1

    # --- LLM Integration ---
    ENABLE_LLM_NARRATIVE: bool = True
    LLM_NARRATIVE_CACHE_TTL_S: int = 1800         # 30 minutes
    LLM_MAX_CALLS_PER_MINUTE: int = 2

    # --- Raven-Tech Front Layer (Market Intelligence) ---
    ENABLE_FRONT_LAYER: bool = True
    ENABLE_FRONT_LAYER_LLM: bool = True
    FRONT_LAYER_DMS_TTL_S: int = 120 * 86400      # 120 days
    FRONT_LAYER_LLM_CACHE_TTL_S: int = 3600       # 1 hour
    FRONT_LAYER_CROSS_ASSET_SYMBOLS: str = ""      # comma-separated override; empty = use defaults
    FRONT_LAYER_THEME_LOOKBACK_DAYS: int = 14
    FRONT_LAYER_DMS_HISTORY_DAYS: int = 7          # rolling context for LLM
    FRONT_LAYER_LLM_MAX_CALLS_PER_MINUTE: int = 4

    # --- Raven Chat (Senior Quant Advisor) ---
    ENABLE_RAVEN_CHAT: bool = True
    RAVEN_CHAT_MODEL: str = "gpt-5.5"
    RAVEN_CHAT_MAX_TURNS: int = 30
    RAVEN_CHAT_RATE_LIMIT: int = 10               # per minute
    RAVEN_CHAT_MAX_CONTEXT_CHARS: int = 40000

    # --- Raven Desk Insight v2 (unified LLM tooltip pipeline) ---
    ENABLE_DESK_INSIGHT_V2: bool = True
    DESK_INSIGHT_MODEL: str = "gpt-5.5"
    DESK_INSIGHT_RATE_LIMIT_PER_MIN: int = 60     # shared across all engines

    # --- Market Intelligence v2 (HMM regime + canonical service) ---
    ENABLE_MI_V2: bool = True                      # kill-switch
    MI_HMM_STATE_COUNT: int = 3
    MI_HMM_LOOKBACK_DAYS: int = 1260              # 5y calibration window
    MI_FACTOR_STALE_DAYS: int = 1
    MI_INSUFFICIENT_DATA_MISSING_FLOOR: int = 3
    MI_BOOTSTRAP_N: int = 500
    MI_MODEL_STORAGE_PATH: str = "data/market_intel_regime_model.json"
    MI_REDIS_MODEL_KEY: str = "market_intel:model:v1"
    MI_REDIS_MODEL_TTL_S: int = 7 * 86400          # 7 days

    # --- Engine 1 v2 — Wing Decision Console ---
    ENABLE_E1_V2: bool = True                          # kill-switch (default on)
    ENABLE_E1_OPTIONS_LIQUIDITY_GATE: bool = False     # OFF: options chain data unreliable for the desk's universe
    E1_REQUIRE_EVENT_DATE: bool = True                 # 400 on /api/breach when event_date missing
    # v2: drop legacy TRADE / LEAN_PASS / PASS / FADE desk-consensus verdict
    # from /api/breach + /api/breach-compare response bodies. The advisor
    # endpoint still re-computes the consensus internally for the LLM prompt.
    E1_EMIT_DESK_CONSENSUS: bool = False
    E1_WING_EM_MULTS: str = "1.0,1.25,1.5,1.75,2.0"
    E1_WING_PTS: str = "5,7.5,10"
    # Composite-score weights
    E1_WING_SCORE_WEIGHT_GAP:    float = 0.30
    E1_WING_SCORE_WEIGHT_CTC:    float = 0.20
    E1_WING_SCORE_WEIGHT_MAE:    float = 0.25
    E1_WING_SCORE_WEIGHT_THETA:  float = 0.15
    E1_WING_SCORE_WEIGHT_CREDIT: float = 0.10
    # Score-normalization targets (desk-tunable posture)
    E1_WING_MAX_TOLERABLE_MAE_PCT: float = 60.0    # MAE % of wing above which "white-knuckle"
    E1_WING_TARGET_THETA_PCT:      float = 50.0
    E1_WING_TARGET_CREDIT_MULT:    float = 1.5
    E1_MAE_HOLD_DAYS: int = 2

    # Legal/reg binary (hybrid): deny/allow lists + keywords (comma-separated)
    LEGAL_REG_TICKER_DENYLIST: Tuple[str, ...] = ()
    LEGAL_REG_TICKER_ALLOWLIST: Tuple[str, ...] = ()
    LEGAL_REG_KEYWORDS: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "FeatureFlags":
        return cls(
            ADD_EVENT_SHIFT_TELEMETRY=_get_bool("ADD_EVENT_SHIFT_TELEMETRY", True),
            STRICT_REALIZED_WINDOW=_get_bool("STRICT_REALIZED_WINDOW", False),
            USE_BETA_POSTERIOR_FOR_DECISIONING=_get_bool("USE_BETA_POSTERIOR_FOR_DECISIONING", False),
            USE_BETA_CI_FOR_CONFIDENCE=_get_bool("USE_BETA_CI_FOR_CONFIDENCE", False),
            BETA_PRIOR_ALPHA=_get_float("BETA_PRIOR_ALPHA", 1.0),
            BETA_PRIOR_BETA=_get_float("BETA_PRIOR_BETA", 1.0),
            ADD_K_CONSISTENT_OVERSHOOT=_get_bool("ADD_K_CONSISTENT_OVERSHOOT", False),
            TRADEBUILDER_ENFORCE_OTM=_get_bool("TRADEBUILDER_ENFORCE_OTM", False),

            ENABLE_BENZINGA=_get_bool("ENABLE_BENZINGA", False),
            BENZINGA_ENABLE_EVENT_RISK=_get_bool("BENZINGA_ENABLE_EVENT_RISK", False),
            BENZINGA_EVENT_RISK_AFFECTS_REGIME=_get_bool("BENZINGA_EVENT_RISK_AFFECTS_REGIME", False),
            BENZINGA_EVENT_RISK_AFFECTS_MC=_get_bool("BENZINGA_EVENT_RISK_AFFECTS_MC", False),
            BENZINGA_EVENT_RISK_HIGH_THRESHOLD=_get_float("BENZINGA_EVENT_RISK_HIGH_THRESHOLD", 0.66),
            BENZINGA_EVENT_RISK_CAUTION_THRESHOLD=_get_float("BENZINGA_EVENT_RISK_CAUTION_THRESHOLD", 0.50),
            BENZINGA_EVENT_RISK_REGIME_TAIL_BUMP_MAX_PCT=_get_float("BENZINGA_EVENT_RISK_REGIME_TAIL_BUMP_MAX_PCT", 20.0),
            BENZINGA_EVENT_RISK_MC_WING_BUMP_MAX_PCT=_get_float("BENZINGA_EVENT_RISK_MC_WING_BUMP_MAX_PCT", 15.0),

            ENABLE_MONTE_CARLO_EARNINGS=_get_bool("ENABLE_MONTE_CARLO_EARNINGS", True),
            MC_ENABLE_CONDITION_ON_QUARTER=_get_bool("MC_ENABLE_CONDITION_ON_QUARTER", False),
            MC_ENABLE_CONDITION_ON_REGIME=_get_bool("MC_ENABLE_CONDITION_ON_REGIME", False),
            MC_ENABLE_CONDITION_ON_TRADE_GATE=_get_bool("MC_ENABLE_CONDITION_ON_TRADE_GATE", False),
            MC_ENABLE_RECENCY_WEIGHTING=_get_bool("MC_ENABLE_RECENCY_WEIGHTING", False),
            MC_ENABLE_WING_OPTIMIZATION=_get_bool("MC_ENABLE_WING_OPTIMIZATION", False),
            MC_ENABLE_TAS_STABILITY=_get_bool("MC_ENABLE_TAS_STABILITY", False),

            MC_N_SIMS=_get_int("MC_N_SIMS", 5000),
            MC_BOOTSTRAP_N=_get_int("MC_BOOTSTRAP_N", 500),
            MC_GLOBAL_SEED=_get_int("MC_GLOBAL_SEED", 1337),
            MC_MIN_POOL=_get_int("MC_MIN_POOL", 12),
            MC_MIN_IMPLIED_MOVE_PCT=_get_float("MC_MIN_IMPLIED_MOVE_PCT", 0.5),
            MC_RECENCY_HALFLIFE_EVENTS=_get_int("MC_RECENCY_HALFLIFE_EVENTS", 8),

            MC_OPT_MAX_MULT_DELTA=_get_float("MC_OPT_MAX_MULT_DELTA", 0.50),
            MC_OPT_STEP=_get_float("MC_OPT_STEP", 0.05),
            MC_MAX_BREACH_EITHER_PCT=_get_float("MC_MAX_BREACH_EITHER_PCT", 25.0),
            MC_MAX_CVAR95_TOTAL=_get_float("MC_MAX_CVAR95_TOTAL", 0.0),
            MC_DEFAULT_WING_WIDTH_DOLLARS=_get_float("MC_DEFAULT_WING_WIDTH_DOLLARS", 5.0),

            ENABLE_ENGINE2_SPX_IC=_get_bool("ENABLE_ENGINE2_SPX_IC", False),
            ENABLE_E2B_FLEX_EXPIRY=_get_bool("ENABLE_E2B_FLEX_EXPIRY", False),

            ENABLE_ENGINE3_RED_DOG=_get_bool("ENABLE_ENGINE3_RED_DOG", True),
            ENGINE3_CACHE_TTL_BARS=_get_int("ENGINE3_CACHE_TTL_BARS", 6 * 3600),
            ENGINE3_CACHE_TTL_SCAN=_get_int("ENGINE3_CACHE_TTL_SCAN", 30 * 60),
            ENGINE3_MAX_WORKERS=_get_int("ENGINE3_MAX_WORKERS", 10),
            ENGINE3_MIN_SCORE_DEFAULT=_get_int("ENGINE3_MIN_SCORE_DEFAULT", 50),
            ENGINE3_APLUS_THRESHOLD=_get_int("ENGINE3_APLUS_THRESHOLD", 75),

            ENABLE_ENGINE4_ICHIMOKU=_get_bool("ENABLE_ENGINE4_ICHIMOKU", True),
            ENGINE4_CACHE_TTL_BARS=_get_int("ENGINE4_CACHE_TTL_BARS", 6 * 3600),
            ENGINE4_CACHE_TTL_SCAN=_get_int("ENGINE4_CACHE_TTL_SCAN", 30 * 60),
            ENGINE4_MAX_WORKERS=_get_int("ENGINE4_MAX_WORKERS", 10),
            ENGINE4_MIN_SCORE_DEFAULT=_get_int("ENGINE4_MIN_SCORE_DEFAULT", 50),
            ENGINE4_APLUS_THRESHOLD=_get_int("ENGINE4_APLUS_THRESHOLD", 75),
            ENGINE4_MIN_DOLLAR_ADV=_get_float("ENGINE4_MIN_DOLLAR_ADV", 20_000_000.0),
            ENGINE4_STRUCTURE_MAX=_get_int("ENGINE4_STRUCTURE_MAX", 8),

            ENGINE2_ENTRY_DAYS=os.getenv("ENGINE2_ENTRY_DAYS", "mon,tue,wed"),
            ENGINE2_EM_MULTS=os.getenv("ENGINE2_EM_MULTS", "0.7,0.8,0.9,1.0,1.1,1.2"),
            ENGINE2_WING_WIDTH_PTS=os.getenv("ENGINE2_WING_WIDTH_PTS", "5,10,15,20,25"),
            ENGINE2_REQUIRE_ORATS_DAILY_VWAP=_get_bool("ENGINE2_REQUIRE_ORATS_DAILY_VWAP", False),
            ENGINE2_MAX_WEEKS_RETURN=_get_int("ENGINE2_MAX_WEEKS_RETURN", 120),
            ENGINE2_LOOKBACK_YEARS_DEFAULT=_get_int("ENGINE2_LOOKBACK_YEARS_DEFAULT", 3),
            ENGINE2_POLICY_MAX_BREACH_PCT=_get_float("ENGINE2_POLICY_MAX_BREACH_PCT", 25.0),
            ENGINE2_POLICY_MAX_OUTSIDE_WINGS_PCT=_get_float("ENGINE2_POLICY_MAX_OUTSIDE_WINGS_PCT", 10.0),
            ENGINE2_POLICY_MAX_MAE95_X_WING=_get_float("ENGINE2_POLICY_MAX_MAE95_X_WING", 1.0),
            ENGINE2_MULTI_WING=_get_bool("ENGINE2_MULTI_WING", True),

            # Engine 2 v2 Command Deck
            ENABLE_E2_V2=_get_bool("ENABLE_E2_V2", True),
            E2_EMIT_DESK_CONSENSUS=_get_bool("E2_EMIT_DESK_CONSENSUS", False),
            E2_WING_EM_MULTS=os.getenv("E2_WING_EM_MULTS", "1.0,1.25,1.5,2.0"),
            E2_WING_PTS=os.getenv("E2_WING_PTS", "5,10,15"),
            E2_WING_SCORE_WEIGHT_CLOSE=_get_float("E2_WING_SCORE_WEIGHT_CLOSE", 0.25),
            E2_WING_SCORE_WEIGHT_TOUCH=_get_float("E2_WING_SCORE_WEIGHT_TOUCH", 0.20),
            E2_WING_SCORE_WEIGHT_MAE=_get_float("E2_WING_SCORE_WEIGHT_MAE", 0.25),
            E2_WING_SCORE_WEIGHT_THETA=_get_float("E2_WING_SCORE_WEIGHT_THETA", 0.15),
            E2_WING_SCORE_WEIGHT_CREDIT=_get_float("E2_WING_SCORE_WEIGHT_CREDIT", 0.15),
            E2_WING_MAX_TOLERABLE_MAE_PCT=_get_float("E2_WING_MAX_TOLERABLE_MAE_PCT", 80.0),
            E2_WING_TARGET_THETA_PCT=_get_float("E2_WING_TARGET_THETA_PCT", 60.0),
            E2_WING_TARGET_ROC_PCT=_get_float("E2_WING_TARGET_ROC_PCT", 12.0),
            ENABLE_E2_MC=_get_bool("ENABLE_E2_MC", True),
            E2_MC_N_SIMS=_get_int("E2_MC_N_SIMS", 5000),
            E2_MC_MIN_POOL=_get_int("E2_MC_MIN_POOL", 20),
            E2_MC_SEED=_get_int("E2_MC_SEED", 1337),
            E2_MC_CONDITION_ON_REGIME=_get_bool("E2_MC_CONDITION_ON_REGIME", True),
            E2_MC_CONDITION_ON_MACRO=_get_bool("E2_MC_CONDITION_ON_MACRO", True),
            E2_MC_GBM_FALLBACK=_get_bool("E2_MC_GBM_FALLBACK", True),
            ENGINE2_ADVISOR_ENABLED=_get_bool("ENGINE2_ADVISOR_ENABLED", True),
            ENGINE2_ADVISOR_MODEL=os.getenv("ENGINE2_ADVISOR_MODEL", "gpt-5.5"),
            ENGINE2_ADVISOR_MAX_CALLS_PER_MINUTE=_get_int("ENGINE2_ADVISOR_MAX_CALLS_PER_MINUTE", 4),
            ENGINE2_TRADE_TTL_S=_get_int("ENGINE2_TRADE_TTL_S", 180 * 86400),
            ENGINE2_TRADE_MAX_INDEX=_get_int("ENGINE2_TRADE_MAX_INDEX", 200),
            ENGINE2_REGIME_LOW_MAX=_get_float("ENGINE2_REGIME_LOW_MAX", 25.0),
            ENGINE2_REGIME_MODERATE_MAX=_get_float("ENGINE2_REGIME_MODERATE_MAX", 45.0),
            ENGINE2_REGIME_ELEVATED_MAX=_get_float("ENGINE2_REGIME_ELEVATED_MAX", 65.0),
            ENGINE2_MACRO_LAMBDA=_get_float("ENGINE2_MACRO_LAMBDA", 0.35),
            ENGINE2_MACRO_MULTIPLIER_CAP=_get_float("ENGINE2_MACRO_MULTIPLIER_CAP", 2.5),
            ENGINE2_MACRO_BASE_CPI=_get_float("ENGINE2_MACRO_BASE_CPI", 1.0),
            ENGINE2_MACRO_BASE_FOMC=_get_float("ENGINE2_MACRO_BASE_FOMC", 1.2),
            ENGINE2_MACRO_BASE_NFP=_get_float("ENGINE2_MACRO_BASE_NFP", 0.7),
            ENGINE2_MACRO_BASE_OPEX=_get_float("ENGINE2_MACRO_BASE_OPEX", 0.4),
            ENGINE2_MACRO_BASE_REFUNDING=_get_float("ENGINE2_MACRO_BASE_REFUNDING", 0.5),
            ENGINE2_MACRO_BASE_FOMC_MINUTES=_get_float("ENGINE2_MACRO_BASE_FOMC_MINUTES", 0.30),
            ENGINE2_MACRO_BASE_GDP=_get_float("ENGINE2_MACRO_BASE_GDP", 0.40),
            ENGINE2_MACRO_BASE_PCE=_get_float("ENGINE2_MACRO_BASE_PCE", 0.35),
            ENGINE2_MACRO_BASE_PPI=_get_float("ENGINE2_MACRO_BASE_PPI", 0.25),
            ENGINE2_MACRO_BASE_PMI_ISM=_get_float("ENGINE2_MACRO_BASE_PMI_ISM", 0.20),
            ENGINE2_MACRO_BASE_RETAIL_SALES=_get_float("ENGINE2_MACRO_BASE_RETAIL_SALES", 0.20),
            ENGINE2_MACRO_BASE_JOBLESS_CLAIMS=_get_float("ENGINE2_MACRO_BASE_JOBLESS_CLAIMS", 0.08),
            ENGINE2_MACRO_BASE_TREASURY_AUCTION=_get_float("ENGINE2_MACRO_BASE_TREASURY_AUCTION", 0.05),
            ENGINE2_MACRO_BASE_OTHER=_get_float("ENGINE2_MACRO_BASE_OTHER", 0.05),

            E10_ADVISOR_ENABLED=_get_bool("E10_ADVISOR_ENABLED", True),
            E10_ADVISOR_MODEL=os.getenv("E10_ADVISOR_MODEL", "gpt-5.5"),
            E10_ADVISOR_MAX_CALLS_PER_MINUTE=_get_int("E10_ADVISOR_MAX_CALLS_PER_MINUTE", 3),

            E1_ADVISOR_ENABLED=_get_bool("E1_ADVISOR_ENABLED", True),
            E1_ADVISOR_MODEL=os.getenv("E1_ADVISOR_MODEL", "gpt-5.5"),
            E1_ADVISOR_MAX_CALLS_PER_MINUTE=_get_int("E1_ADVISOR_MAX_CALLS_PER_MINUTE", 4),
            E1_EM_MULTS=os.getenv("E1_EM_MULTS", "1.0,1.5,2.0"),
            E1_WING_WIDTH_PTS=os.getenv("E1_WING_WIDTH_PTS", "2.5,5,7.5,10"),
            E1_TRADE_TTL_S=_get_int("E1_TRADE_TTL_S", 180 * 86400),
            E1_TRADE_MAX_INDEX=_get_int("E1_TRADE_MAX_INDEX", 200),

            GO_IVP_MIN=_get_float("GO_IVP_MIN", 0.80),
            GO_IV_SAMPLE_MIN=_get_int("GO_IV_SAMPLE_MIN", 20),
            GO_IV30_FLOOR=_get_float("GO_IV30_FLOOR", 0.30),
            GO_IV_Z_ENABLED=_get_bool("GO_IV_Z_ENABLED", True),
            GO_IV30_Z_MIN=_get_float("GO_IV30_Z_MIN", 0.75),

            GO_MIN_EARNINGS_N=_get_int("GO_MIN_EARNINGS_N", 6),
            GO_EM_RICHNESS_MULT=_get_float("GO_EM_RICHNESS_MULT", 1.05),

            GO_TAIL_SAMPLE_MIN=_get_int("GO_TAIL_SAMPLE_MIN", 8),
            GO_TAIL_P90_MULT=_get_float("GO_TAIL_P90_MULT", 0.80),

            GO_CORR20_HIGH=_get_float("GO_CORR20_HIGH", 0.70),
            GO_BETA20_HIGH=_get_float("GO_BETA20_HIGH", 1.20),

            GO_AVG_DOLLAR_VOL20D_MIN=_get_float("GO_AVG_DOLLAR_VOL20D_MIN", 200_000_000.0),
            GO_AVG_DOLLAR_VOL20D_BLOCK=_get_float("GO_AVG_DOLLAR_VOL20D_BLOCK", 20_000_000.0),
            GO_OPT_DELTA_BAND_LO=_get_float("GO_OPT_DELTA_BAND_LO", 0.10),
            GO_OPT_DELTA_BAND_HI=_get_float("GO_OPT_DELTA_BAND_HI", 0.25),
            GO_OPT_SPREAD_MAX=_get_float("GO_OPT_SPREAD_MAX", 0.15),
            GO_OPT_SPREAD_BLOCK=_get_float("GO_OPT_SPREAD_BLOCK", 0.30),
            GO_OPT_SPREAD_MAX_P90=_get_float("GO_OPT_SPREAD_MAX_P90", 0.25),
            GO_OPT_SPREAD_P90_BLOCK=_get_float("GO_OPT_SPREAD_P90_BLOCK", 0.50),
            GO_OPT_MIN_MID=_get_float("GO_OPT_MIN_MID", 0.20),
            GO_OPT_OI_MIN=_get_float("GO_OPT_OI_MIN", 500.0),
            GO_OPT_VOL_MIN=_get_float("GO_OPT_VOL_MIN", 50.0),
            GO_BAND_QUOTE_COVERAGE_MIN=_get_float("GO_BAND_QUOTE_COVERAGE_MIN", 0.70),
            GO_BAND_OI_SUM_MIN=_get_float("GO_BAND_OI_SUM_MIN", 2000.0),
            GO_BAND_OI_SUM_BLOCK=_get_float("GO_BAND_OI_SUM_BLOCK", 200.0),
            GO_BAND_VOL_SUM_MIN=_get_float("GO_BAND_VOL_SUM_MIN", 200.0),
            GO_BAND_VOL_SUM_BLOCK=_get_float("GO_BAND_VOL_SUM_BLOCK", 20.0),

            GO_RV5_JUMP_MAX=_get_float("GO_RV5_JUMP_MAX", 1.15),
            GO_RV20_JUMP_MAX=_get_float("GO_RV20_JUMP_MAX", 1.10),
            GO_RV5_ACCEL_TIGHTEN_TRIGGER=_get_float("GO_RV5_ACCEL_TIGHTEN_TRIGGER", 1.05),
            GO_FLIP_CUTOFF_BASE=_get_float("GO_FLIP_CUTOFF_BASE", 2.0),
            GO_FLIP_CUTOFF_TIGHT=_get_float("GO_FLIP_CUTOFF_TIGHT", 2.5),

            GO_FORCED_FLOW_WINDOW_TRADING_DAYS=_get_int("GO_FORCED_FLOW_WINDOW_TRADING_DAYS", 4),
            GO_FORCED_FLOW_IMPORTANCE_HIGH_MIN=_get_int("GO_FORCED_FLOW_IMPORTANCE_HIGH_MIN", 4),
            GO_FORCED_FLOW_IMPORTANCE_MED_MIN=_get_int("GO_FORCED_FLOW_IMPORTANCE_MED_MIN", 3),
            GO_FORCED_FLOW_MANUAL_RANGES=tuple(_get_csv_list("GO_FORCED_FLOW_MANUAL_RANGES", [])),

            ENABLE_ENGINE5_LEAD_LAG=_get_bool("ENABLE_ENGINE5_LEAD_LAG", False),
            ENGINE5_CACHE_TTL_LATEST=_get_int("ENGINE5_CACHE_TTL_LATEST", 48 * 3600),
            ENGINE5_CACHE_TTL_HISTORY=_get_int("ENGINE5_CACHE_TTL_HISTORY", 180 * 86400),
            ENGINE5_CORR_WINDOW=_get_int("ENGINE5_CORR_WINDOW", 20),
            ENGINE5_CORR_THRESHOLD=_get_float("ENGINE5_CORR_THRESHOLD", 0.40),
            ENGINE5_Z_SIGNIFICANT=_get_float("ENGINE5_Z_SIGNIFICANT", 1.50),
            ENGINE5_REGIME_STRESSED_THRESHOLD=_get_float("ENGINE5_REGIME_STRESSED_THRESHOLD", 75.0),
            ENGINE5_REGIME_RISK_OFF_THRESHOLD=_get_float("ENGINE5_REGIME_RISK_OFF_THRESHOLD", 55.0),
            ENGINE5_REGIME_TRANSITIONAL_THRESHOLD=_get_float("ENGINE5_REGIME_TRANSITIONAL_THRESHOLD", 30.0),
            ENGINE5_MAX_LAG_DAYS=_get_int("ENGINE5_MAX_LAG_DAYS", 5),
            ENGINE5_LOOKBACK_DAYS=_get_int("ENGINE5_LOOKBACK_DAYS", 60),
            ENGINE5_FRESHNESS_RETRY_COUNT=_get_int("ENGINE5_FRESHNESS_RETRY_COUNT", 3),
            ENGINE5_FRESHNESS_RETRY_INTERVAL_S=_get_int("ENGINE5_FRESHNESS_RETRY_INTERVAL_S", 900),

            ENGINE5_SNAPSHOT_TTL_S=_get_int("ENGINE5_SNAPSHOT_TTL_S", 14 * 86400),
            ENGINE5_SNAPSHOT_INDEX_TTL_S=_get_int("ENGINE5_SNAPSHOT_INDEX_TTL_S", 30 * 86400),
            ENGINE5_SNAPSHOT_MAX_INDEX=_get_int("ENGINE5_SNAPSHOT_MAX_INDEX", 50),
            ENGINE5_SNAPSHOT_BEST_MAX_AGE_DAYS=_get_int("ENGINE5_SNAPSHOT_BEST_MAX_AGE_DAYS", 14),

            ENGINE5_VOL_LEADLAG_ENABLED=_get_bool("ENGINE5_VOL_LEADLAG_ENABLED", True),
            ENGINE5_GLOBAL_VOL_RISING_THRESHOLD=_get_float("ENGINE5_GLOBAL_VOL_RISING_THRESHOLD", 0.75),
            ENGINE5_GLOBAL_VOL_FALLING_THRESHOLD=_get_float("ENGINE5_GLOBAL_VOL_FALLING_THRESHOLD", -0.75),
            ENGINE5_GLOBAL_VOL_NOISE_FLOOR=_get_float("ENGINE5_GLOBAL_VOL_NOISE_FLOOR", 0.40),
            ENGINE5_US_IV_LOW_THRESHOLD=_get_float("ENGINE5_US_IV_LOW_THRESHOLD", 30.0),
            ENGINE5_US_IV_HIGH_THRESHOLD=_get_float("ENGINE5_US_IV_HIGH_THRESHOLD", 60.0),
            ENGINE5_VOL_ZSCORE_WINDOW=_get_int("ENGINE5_VOL_ZSCORE_WINDOW", 60),

            # --- Engine 7 ---
            ENABLE_ENGINE7_PAIRS=_get_bool("ENABLE_ENGINE7_PAIRS", True),
            ENGINE7_CACHE_TTL_BARS=_get_int("ENGINE7_CACHE_TTL_BARS", 6 * 3600),
            ENGINE7_CACHE_TTL_SCAN=_get_int("ENGINE7_CACHE_TTL_SCAN", 30 * 60),
            ENGINE7_MAX_WORKERS=_get_int("ENGINE7_MAX_WORKERS", 8),
            ENGINE7_MIN_SCORE_DEFAULT=_get_int("ENGINE7_MIN_SCORE_DEFAULT", 50),
            ENGINE7_APLUS_THRESHOLD=_get_int("ENGINE7_APLUS_THRESHOLD", 75),
            ENGINE7_Z_SCORE_WINDOW=_get_int("ENGINE7_Z_SCORE_WINDOW", 40),
            ENGINE7_Z_ENTRY_THRESHOLD=_get_float("ENGINE7_Z_ENTRY_THRESHOLD", 1.5),
            ENGINE7_Z_MOMENTUM_THRESHOLD=_get_float("ENGINE7_Z_MOMENTUM_THRESHOLD", 1.0),
            ENGINE7_MAX_CONCURRENT_PAIRS=_get_int("ENGINE7_MAX_CONCURRENT_PAIRS", 5),
            ENGINE7_THEME_REQUIRED=_get_bool("ENGINE7_THEME_REQUIRED", True),
            ENGINE7_ENABLE_ORATS_VOL=_get_bool("ENGINE7_ENABLE_ORATS_VOL", True),
            ENGINE7_ENABLE_LLM_ANNOTATION=_get_bool("ENGINE7_ENABLE_LLM_ANNOTATION", False),
            ENGINE7_ENABLE_LLM_THEME_SCORING=_get_bool("ENGINE7_ENABLE_LLM_THEME_SCORING", True),
            ENGINE7_OVERLAP_CORR_THRESHOLD=_get_float("ENGINE7_OVERLAP_CORR_THRESHOLD", 0.70),
            ENGINE7_OVERLAP_CORR_WINDOW=_get_int("ENGINE7_OVERLAP_CORR_WINDOW", 20),
            GATE_PAIRS_REGIME_ALLOW=os.getenv("GATE_PAIRS_REGIME_ALLOW", ""),
            GATE_PAIRS_VOL_STATE_ALLOW=os.getenv("GATE_PAIRS_VOL_STATE_ALLOW", ""),

            # --- Engine 12 ---
            ENABLE_ENGINE12_VIX_FADE=_get_bool("ENABLE_ENGINE12_VIX_FADE", True),
            ENGINE12_CACHE_TTL_SCAN=_get_int("ENGINE12_CACHE_TTL_SCAN", 15 * 60),
            ENGINE12_MC_N_SIMS=_get_int("ENGINE12_MC_N_SIMS", 10000),
            ENGINE12_MC_SEED=_get_int("ENGINE12_MC_SEED", 42),
            ENGINE12_OU_CALIBRATION_LOOKBACK_DAYS=_get_int("ENGINE12_OU_CALIBRATION_LOOKBACK_DAYS", 1260),
            ENGINE12_MAX_WORKERS=_get_int("ENGINE12_MAX_WORKERS", 4),
            ENGINE12_SECONDARY_SPIKE_THRESHOLD=_get_float("ENGINE12_SECONDARY_SPIKE_THRESHOLD", 0.25),
            ENGINE12_CONTAINED_THRESHOLD=_get_float("ENGINE12_CONTAINED_THRESHOLD", 0.60),
            ENGINE12_DEALER_GAMMA_ENABLED=_get_bool("ENGINE12_DEALER_GAMMA_ENABLED", True),
            ENGINE12_STRESS_WEIGHT_OIL=_get_float("ENGINE12_STRESS_WEIGHT_OIL", 0.30),
            ENGINE12_STRESS_WEIGHT_GOLD=_get_float("ENGINE12_STRESS_WEIGHT_GOLD", 0.20),
            ENGINE12_STRESS_WEIGHT_HYG=_get_float("ENGINE12_STRESS_WEIGHT_HYG", 0.20),
            ENGINE12_STRESS_WEIGHT_DXY=_get_float("ENGINE12_STRESS_WEIGHT_DXY", 0.15),
            ENGINE12_STRESS_WEIGHT_TLT_VOL=_get_float("ENGINE12_STRESS_WEIGHT_TLT_VOL", 0.15),
            ENGINE12_GAMMA_AMP_LOW=_get_float("ENGINE12_GAMMA_AMP_LOW", 0.10),
            ENGINE12_GAMMA_AMP_MED=_get_float("ENGINE12_GAMMA_AMP_MED", 0.20),
            ENGINE12_GAMMA_AMP_HIGH=_get_float("ENGINE12_GAMMA_AMP_HIGH", 0.30),

            # --- Engine 13 ---
            ENABLE_ENGINE13_GAP_REGIME=_get_bool("ENABLE_ENGINE13_GAP_REGIME", True),
            ENGINE13_CACHE_TTL_SCAN=_get_int("ENGINE13_CACHE_TTL_SCAN", 10 * 60),
            ENGINE13_ADVISOR_MODEL=os.getenv("ENGINE13_ADVISOR_MODEL", "gpt-5.5"),
            ENGINE13_GAP_THRESHOLD_PCT=_get_float("ENGINE13_GAP_THRESHOLD_PCT", 1.5),
            ENGINE13_FRAGILITY_ENABLED=_get_bool("ENGINE13_FRAGILITY_ENABLED", True),
            ENGINE13_FRAGILITY_W_OPTIONS=_get_float("ENGINE13_FRAGILITY_W_OPTIONS", 0.30),
            ENGINE13_FRAGILITY_W_CROSS_ASSET=_get_float("ENGINE13_FRAGILITY_W_CROSS_ASSET", 0.25),
            ENGINE13_FRAGILITY_W_HISTORICAL=_get_float("ENGINE13_FRAGILITY_W_HISTORICAL", 0.20),
            ENGINE13_FRAGILITY_W_HEADLINE=_get_float("ENGINE13_FRAGILITY_W_HEADLINE", 0.15),
            ENGINE13_FRAGILITY_W_PRICE_ACTION=_get_float("ENGINE13_FRAGILITY_W_PRICE_ACTION", 0.10),

            # --- Engine 14 ---
            ENABLE_ENGINE14_IC_SCENARIO=_get_bool("ENABLE_ENGINE14_IC_SCENARIO", False),
            ENGINE14_CACHE_TTL_SCAN=_get_int("ENGINE14_CACHE_TTL_SCAN", 10 * 60),
            ENGINE14_LOOKBACK_YEARS=_get_int("ENGINE14_LOOKBACK_YEARS", 2),
            ENGINE14_MIN_ANALOGUES=_get_int("ENGINE14_MIN_ANALOGUES", 20),
            ENGINE14_MAX_ANALOGUES=_get_int("ENGINE14_MAX_ANALOGUES", 250),
            ENGINE14_REGIME_BUCKET_TOL=_get_float("ENGINE14_REGIME_BUCKET_TOL", 12.0),
            ENGINE14_DEFAULT_PROFIT_TARGET_PCT=_get_float("ENGINE14_DEFAULT_PROFIT_TARGET_PCT", 50.0),
            ENGINE14_DEFAULT_STOP_LOSS_PCT=_get_float("ENGINE14_DEFAULT_STOP_LOSS_PCT", 200.0),
            ENGINE14_SQLITE_PATH=os.getenv("ENGINE14_SQLITE_PATH", "data/engine14_chains.db"),
            ENGINE14_STRIKE_SNAP_MAX_PTS=_get_float("ENGINE14_STRIKE_SNAP_MAX_PTS", 5.0),
            ENGINE14_BID_ASK_FILL_MODE=os.getenv("ENGINE14_BID_ASK_FILL_MODE", "mid"),
            ENGINE14_ENABLE_CONDITIONING=_get_bool("ENGINE14_ENABLE_CONDITIONING", True),
            ENGINE14_ADMIN_TOKEN=os.getenv("ENGINE14_ADMIN_TOKEN", ""),
            ENGINE14_BACKFILL_MAX_YEARS=_get_float("ENGINE14_BACKFILL_MAX_YEARS", 3.0),
            ENGINE14_FILL_MODEL=os.getenv("ENGINE14_FILL_MODEL", "nbbo"),
            ENGINE14_FILL_PENALTY_PCT=_get_float("ENGINE14_FILL_PENALTY_PCT", 15.0),
            ENGINE14_EMIT_LEGACY_MID_DIST=_get_bool("ENGINE14_EMIT_LEGACY_MID_DIST", True),
            ENGINE14_ENTRY_FILL_CHECK=_get_bool("ENGINE14_ENTRY_FILL_CHECK", False),
            ENGINE14_EM_MULTIPLE_TOL=_get_float("ENGINE14_EM_MULTIPLE_TOL", 0.25),
            ENGINE14_ENABLE_EM_MULTIPLE_FILTER=_get_bool("ENGINE14_ENABLE_EM_MULTIPLE_FILTER", True),
            ENGINE14_MAE_PROXY_ENABLED=_get_bool("ENGINE14_MAE_PROXY_ENABLED", True),
            ENGINE14_MODIFIER_COEFFICIENTS_PATH=os.getenv(
                "ENGINE14_MODIFIER_COEFFICIENTS_PATH",
                "data/engine14_modifier_coefficients.json",
            ),
            ENGINE14_REGIME_FEATURES_PATH=os.getenv(
                "ENGINE14_REGIME_FEATURES_PATH",
                "data/engine14_regime_features.db",
            ),
            ENGINE14_ENABLE_KNN_REGIME=_get_bool("ENGINE14_ENABLE_KNN_REGIME", False),
            ENGINE14_KNN_TOP_N=_get_int("ENGINE14_KNN_TOP_N", 80),
            ENGINE14_BOOTSTRAP_B=_get_int("ENGINE14_BOOTSTRAP_B", 500),
            ENGINE14_BOOTSTRAP_SEED=_get_int("ENGINE14_BOOTSTRAP_SEED", 14),
            ENGINE14_THIN_SAMPLE_N=_get_int("ENGINE14_THIN_SAMPLE_N", 20),

            # Engine 14 v2 Command Deck
            ENABLE_E14_V2=_get_bool("ENABLE_E14_V2", True),
            E14_EMIT_DESK_CONSENSUS=_get_bool("E14_EMIT_DESK_CONSENSUS", False),
            E14_WING_EM_MULTS=os.getenv("E14_WING_EM_MULTS", "1.0,1.25,1.5,2.0"),
            E14_WING_PTS=os.getenv("E14_WING_PTS", "5,10,15"),
            E14_WING_SCORE_WEIGHT_BREACH=_get_float("E14_WING_SCORE_WEIGHT_BREACH", 0.25),
            E14_WING_SCORE_WEIGHT_TOUCH=_get_float("E14_WING_SCORE_WEIGHT_TOUCH", 0.20),
            E14_WING_SCORE_WEIGHT_MAE=_get_float("E14_WING_SCORE_WEIGHT_MAE", 0.25),
            E14_WING_SCORE_WEIGHT_THETA=_get_float("E14_WING_SCORE_WEIGHT_THETA", 0.15),
            E14_WING_SCORE_WEIGHT_CREDIT=_get_float("E14_WING_SCORE_WEIGHT_CREDIT", 0.15),
            E14_WING_MAX_TOLERABLE_MAE_PCT=_get_float("E14_WING_MAX_TOLERABLE_MAE_PCT", 80.0),
            E14_WING_TARGET_THETA_PCT=_get_float("E14_WING_TARGET_THETA_PCT", 60.0),
            E14_WING_TARGET_ROC_PCT=_get_float("E14_WING_TARGET_ROC_PCT", 12.0),
            ENABLE_E14_MC=_get_bool("ENABLE_E14_MC", True),
            E14_MC_N_SIMS=_get_int("E14_MC_N_SIMS", 5000),
            E14_MC_MIN_POOL=_get_int("E14_MC_MIN_POOL", 20),
            E14_MC_SEED=_get_int("E14_MC_SEED", 1337),
            E14_MC_CONDITION_ON_REGIME=_get_bool("E14_MC_CONDITION_ON_REGIME", True),
            E14_MC_CONDITION_ON_MACRO=_get_bool("E14_MC_CONDITION_ON_MACRO", True),
            E14_MC_GBM_FALLBACK=_get_bool("E14_MC_GBM_FALLBACK", True),
            ENGINE14_HOLIDAY_CALENDAR=_get_bool("ENGINE14_HOLIDAY_CALENDAR", True),
            E14_ADVISOR_ENABLED=_get_bool("E14_ADVISOR_ENABLED", True),
            E14_ADVISOR_MODEL=os.getenv("E14_ADVISOR_MODEL", "gpt-5.5"),
            E14_ADVISOR_MAX_CALLS_PER_MINUTE=_get_int("E14_ADVISOR_MAX_CALLS_PER_MINUTE", 4),
            ENGINE14_PER_DTE_EXITS=_get_bool("ENGINE14_PER_DTE_EXITS", True),
            ENGINE14_ENABLE_SIZING=_get_bool("ENGINE14_ENABLE_SIZING", True),
            ENGINE14_ENABLE_GREEKS_ATTRIBUTION=_get_bool("ENGINE14_ENABLE_GREEKS_ATTRIBUTION", True),

            # --- Engine 15 ---
            ENABLE_ENGINE15_EARNINGS_IC=_get_bool("ENABLE_ENGINE15_EARNINGS_IC", True),
            ENGINE15_CACHE_TTL_SCAN=_get_int("ENGINE15_CACHE_TTL_SCAN", 10 * 60),
            ENGINE15_MIN_EVENTS=_get_int("ENGINE15_MIN_EVENTS", 8),
            ENGINE15_MAX_EVENTS=_get_int("ENGINE15_MAX_EVENTS", 20),
            ENGINE15_EVENT_BACKFILL_DAYS_BEFORE=_get_int("ENGINE15_EVENT_BACKFILL_DAYS_BEFORE", 1),
            ENGINE15_EVENT_BACKFILL_DAYS_AFTER=_get_int("ENGINE15_EVENT_BACKFILL_DAYS_AFTER", 2),
            ENGINE15_FILL_MODEL=os.getenv("ENGINE15_FILL_MODEL", "nbbo"),
            ENGINE15_FILL_PENALTY_PCT=_get_float("ENGINE15_FILL_PENALTY_PCT", 15.0),
            ENGINE15_STRIKE_SNAP_MAX_PTS=_get_float("ENGINE15_STRIKE_SNAP_MAX_PTS", 2.5),
            ENGINE15_DEFAULT_PROFIT_TARGET_PCT=_get_float("ENGINE15_DEFAULT_PROFIT_TARGET_PCT", 50.0),
            ENGINE15_DEFAULT_STOP_LOSS_PCT=_get_float("ENGINE15_DEFAULT_STOP_LOSS_PCT", 150.0),
            ENGINE15_INTRADAY_CRUSH_FACTOR=_get_float("ENGINE15_INTRADAY_CRUSH_FACTOR", 0.80),
            ENGINE15_ENABLE_CONDITIONING=_get_bool("ENGINE15_ENABLE_CONDITIONING", True),
            ENGINE15_ADMIN_TOKEN=os.getenv("ENGINE15_ADMIN_TOKEN", ""),
            ENGINE15_BACKFILL_DELAY_MS=_get_int("ENGINE15_BACKFILL_DELAY_MS", 200),
            ENGINE15_MAX_EXPIRY_OFFSET_DAYS=_get_int("ENGINE15_MAX_EXPIRY_OFFSET_DAYS", 4),
            ENGINE15_EM_MULTIPLE_TOL=_get_float("ENGINE15_EM_MULTIPLE_TOL", 0.35),
            ENGINE15_ENABLE_EM_MULTIPLE_FILTER=_get_bool("ENGINE15_ENABLE_EM_MULTIPLE_FILTER", False),
            ENGINE15_ADVISOR_ENABLED=_get_bool("ENGINE15_ADVISOR_ENABLED", True),

            # Engine 15 v2
            ENABLE_E15_V2=_get_bool("ENABLE_E15_V2", True),
            ENGINE15_HOLIDAY_CALENDAR=_get_bool("ENGINE15_HOLIDAY_CALENDAR", True),
            ENGINE15_INTRADAY_CRUSH_MODE=os.getenv("ENGINE15_INTRADAY_CRUSH_MODE", "empirical"),
            ENGINE15_GUIDANCE_RISK_ENABLED=_get_bool("ENGINE15_GUIDANCE_RISK_ENABLED", True),
            E15_EMIT_DESK_CONSENSUS=_get_bool("E15_EMIT_DESK_CONSENSUS", False),
            GUIDANCE_RISK_MAX_BUMP_PCT=_get_float("GUIDANCE_RISK_MAX_BUMP_PCT", 15.0),
            ENGINE15_ADVISOR_MODEL=os.getenv("ENGINE15_ADVISOR_MODEL", os.getenv("E1_ADVISOR_MODEL", "gpt-5.5")),

            # --- Engine 8 ---
            ENABLE_ENGINE8_POST_EVENT=_get_bool("ENABLE_ENGINE8_POST_EVENT", True),

            # --- Engine 9 ---
            ENABLE_ENGINE9_CREDIT_STRESS=_get_bool("ENABLE_ENGINE9_CREDIT_STRESS", True),
            ENGINE9_CACHE_TTL_SCAN=_get_int("ENGINE9_CACHE_TTL_SCAN", 5 * 60),
            ENGINE9_MAX_WORKERS=_get_int("ENGINE9_MAX_WORKERS", 8),
            ENGINE8_CACHE_TTL_EVAL=_get_int("ENGINE8_CACHE_TTL_EVAL", 30 * 60),
            ENGINE8_SNAPSHOT_TTL_S=_get_int("ENGINE8_SNAPSHOT_TTL_S", 30 * 86400),
            ENGINE8_LLM_RESULT_TTL_S=_get_int("ENGINE8_LLM_RESULT_TTL_S", 90 * 86400),
            ENGINE8_MAX_WORKERS=_get_int("ENGINE8_MAX_WORKERS", 5),
            ENGINE8_CONFIDENCE_THRESHOLD=_get_int("ENGINE8_CONFIDENCE_THRESHOLD", 65),
            ENGINE8_CONTINUE_THRESHOLD=_get_int("ENGINE8_CONTINUE_THRESHOLD", 65),
            ENGINE8_FADE_THRESHOLD=_get_int("ENGINE8_FADE_THRESHOLD", 70),
            ENGINE8_MIN_HISTORICAL_SAMPLE=_get_int("ENGINE8_MIN_HISTORICAL_SAMPLE", 8),
            ENGINE8_EM_RATIO_OVER=_get_float("ENGINE8_EM_RATIO_OVER", 1.20),
            ENGINE8_EM_RATIO_EXTREME=_get_float("ENGINE8_EM_RATIO_EXTREME", 1.50),
            ENGINE8_ATR_ELEVATED=_get_float("ENGINE8_ATR_ELEVATED", 1.50),
            ENGINE8_ATR_EXTREME=_get_float("ENGINE8_ATR_EXTREME", 2.50),
            ENGINE8_MAX_RISK_UNITS=_get_float("ENGINE8_MAX_RISK_UNITS", 1.5),
            ENGINE8_MIN_RISK_UNITS=_get_float("ENGINE8_MIN_RISK_UNITS", 0.5),
            ENGINE8_MAX_HOLDING_DAYS=_get_int("ENGINE8_MAX_HOLDING_DAYS", 5),
            ENGINE8_CONTINUATION_PROB_MIN=_get_float("ENGINE8_CONTINUATION_PROB_MIN", 0.55),
            ENGINE8_REVERSION_PROB_MIN=_get_float("ENGINE8_REVERSION_PROB_MIN", 0.55),
            ENGINE8_ENABLE_LLM_CLASSIFY=_get_bool("ENGINE8_ENABLE_LLM_CLASSIFY", True),
            ENGINE8_LLM_MODEL_VERSION=os.getenv("ENGINE8_LLM_MODEL_VERSION", "gpt-5.5"),
            ENGINE8_LOOKBACK_EVENTS=_get_int("ENGINE8_LOOKBACK_EVENTS", 40),
            ENGINE8_MAX_CONTROLLED_LOSS_PCT=_get_float("ENGINE8_MAX_CONTROLLED_LOSS_PCT", 50.0),

            # --- Gating (Engine 3 & 4) ---
            ENABLE_GATING=_get_bool("ENABLE_GATING", True),
            GATE_RD_REGIME_ALLOW=os.getenv("GATE_RD_REGIME_ALLOW", "Transitional,Stressed"),
            GATE_RD_VOL_STATE_ALLOW=os.getenv("GATE_RD_VOL_STATE_ALLOW", "expanding,unstable,RISING,rising"),
            GATE_RD_MACRO_PROXIMITY_DAYS=_get_int("GATE_RD_MACRO_PROXIMITY_DAYS", 1),
            GATE_ICH_REGIME_ALLOW=os.getenv("GATE_ICH_REGIME_ALLOW", "Risk-On,Transitional"),
            GATE_ICH_VOL_STATE_ALLOW=os.getenv("GATE_ICH_VOL_STATE_ALLOW", "compressing,stable,NORMAL,FALLING,falling,flat"),
            GATE_ICH_MACRO_PROXIMITY_DAYS=_get_int("GATE_ICH_MACRO_PROXIMITY_DAYS", 1),

            ENABLE_LLM_NARRATIVE=_get_bool("ENABLE_LLM_NARRATIVE", False),
            LLM_NARRATIVE_CACHE_TTL_S=_get_int("LLM_NARRATIVE_CACHE_TTL_S", 1800),
            LLM_MAX_CALLS_PER_MINUTE=_get_int("LLM_MAX_CALLS_PER_MINUTE", 2),

            ENABLE_FRONT_LAYER=_get_bool("ENABLE_FRONT_LAYER", True),
            ENABLE_FRONT_LAYER_LLM=_get_bool("ENABLE_FRONT_LAYER_LLM", True),
            FRONT_LAYER_DMS_TTL_S=_get_int("FRONT_LAYER_DMS_TTL_S", 120 * 86400),
            FRONT_LAYER_LLM_CACHE_TTL_S=_get_int("FRONT_LAYER_LLM_CACHE_TTL_S", 3600),
            FRONT_LAYER_CROSS_ASSET_SYMBOLS=os.getenv("FRONT_LAYER_CROSS_ASSET_SYMBOLS", ""),
            FRONT_LAYER_THEME_LOOKBACK_DAYS=_get_int("FRONT_LAYER_THEME_LOOKBACK_DAYS", 14),
            FRONT_LAYER_DMS_HISTORY_DAYS=_get_int("FRONT_LAYER_DMS_HISTORY_DAYS", 7),
            FRONT_LAYER_LLM_MAX_CALLS_PER_MINUTE=_get_int("FRONT_LAYER_LLM_MAX_CALLS_PER_MINUTE", 4),

            ENABLE_RAVEN_CHAT=_get_bool("ENABLE_RAVEN_CHAT", True),
            RAVEN_CHAT_MODEL=os.getenv("RAVEN_CHAT_MODEL", "gpt-5.5"),
            RAVEN_CHAT_MAX_TURNS=_get_int("RAVEN_CHAT_MAX_TURNS", 30),
            RAVEN_CHAT_RATE_LIMIT=_get_int("RAVEN_CHAT_RATE_LIMIT", 10),
            RAVEN_CHAT_MAX_CONTEXT_CHARS=_get_int("RAVEN_CHAT_MAX_CONTEXT_CHARS", 40000),

            ENABLE_DESK_INSIGHT_V2=_get_bool("ENABLE_DESK_INSIGHT_V2", True),
            DESK_INSIGHT_MODEL=os.getenv("DESK_INSIGHT_MODEL", "gpt-5.5"),
            DESK_INSIGHT_RATE_LIMIT_PER_MIN=_get_int("DESK_INSIGHT_RATE_LIMIT_PER_MIN", 60),

            ENABLE_MI_V2=_get_bool("ENABLE_MI_V2", True),

            # Engine 1 v2 — Wing Decision Console
            ENABLE_E1_V2=_get_bool("ENABLE_E1_V2", True),
            ENABLE_E1_OPTIONS_LIQUIDITY_GATE=_get_bool("ENABLE_E1_OPTIONS_LIQUIDITY_GATE", False),
            E1_REQUIRE_EVENT_DATE=_get_bool("E1_REQUIRE_EVENT_DATE", True),
            E1_WING_EM_MULTS=os.getenv("E1_WING_EM_MULTS", "1.0,1.25,1.5,1.75,2.0"),
            E1_WING_PTS=os.getenv("E1_WING_PTS", "5,7.5,10"),
            E1_WING_SCORE_WEIGHT_GAP=_get_float("E1_WING_SCORE_WEIGHT_GAP", 0.30),
            E1_WING_SCORE_WEIGHT_CTC=_get_float("E1_WING_SCORE_WEIGHT_CTC", 0.20),
            E1_WING_SCORE_WEIGHT_MAE=_get_float("E1_WING_SCORE_WEIGHT_MAE", 0.25),
            E1_WING_SCORE_WEIGHT_THETA=_get_float("E1_WING_SCORE_WEIGHT_THETA", 0.15),
            E1_WING_SCORE_WEIGHT_CREDIT=_get_float("E1_WING_SCORE_WEIGHT_CREDIT", 0.10),
            E1_WING_MAX_TOLERABLE_MAE_PCT=_get_float("E1_WING_MAX_TOLERABLE_MAE_PCT", 60.0),
            E1_WING_TARGET_THETA_PCT=_get_float("E1_WING_TARGET_THETA_PCT", 50.0),
            E1_WING_TARGET_CREDIT_MULT=_get_float("E1_WING_TARGET_CREDIT_MULT", 1.5),
            E1_MAE_HOLD_DAYS=_get_int("E1_MAE_HOLD_DAYS", 2),
            E1_EMIT_DESK_CONSENSUS=_get_bool("E1_EMIT_DESK_CONSENSUS", False),
            MI_HMM_STATE_COUNT=_get_int("MI_HMM_STATE_COUNT", 3),
            MI_HMM_LOOKBACK_DAYS=_get_int("MI_HMM_LOOKBACK_DAYS", 1260),
            MI_FACTOR_STALE_DAYS=_get_int("MI_FACTOR_STALE_DAYS", 1),
            MI_INSUFFICIENT_DATA_MISSING_FLOOR=_get_int("MI_INSUFFICIENT_DATA_MISSING_FLOOR", 3),
            MI_BOOTSTRAP_N=_get_int("MI_BOOTSTRAP_N", 500),
            MI_MODEL_STORAGE_PATH=os.getenv("MI_MODEL_STORAGE_PATH", "data/market_intel_regime_model.json"),
            MI_REDIS_MODEL_KEY=os.getenv("MI_REDIS_MODEL_KEY", "market_intel:model:v1"),
            MI_REDIS_MODEL_TTL_S=_get_int("MI_REDIS_MODEL_TTL_S", 7 * 86400),

            LEGAL_REG_TICKER_DENYLIST=tuple(_get_csv_list("LEGAL_REG_TICKER_DENYLIST", [])),
            LEGAL_REG_TICKER_ALLOWLIST=tuple(_get_csv_list("LEGAL_REG_TICKER_ALLOWLIST", [])),
            LEGAL_REG_KEYWORDS=tuple(_get_csv_list("LEGAL_REG_KEYWORDS", [])),
        )

    def cache_key(self) -> tuple:
        """
        Engine 1 cache fingerprint.

        IMPORTANT: Do NOT include Engine 2 knobs here. They do not affect the earnings-breach model and
        should not change Engine 1 caching or MC seeds.
        """
        return (
            ("ADD_EVENT_SHIFT_TELEMETRY", bool(self.ADD_EVENT_SHIFT_TELEMETRY)),
            ("STRICT_REALIZED_WINDOW", bool(self.STRICT_REALIZED_WINDOW)),
            ("USE_BETA_POSTERIOR_FOR_DECISIONING", bool(self.USE_BETA_POSTERIOR_FOR_DECISIONING)),
            ("USE_BETA_CI_FOR_CONFIDENCE", bool(self.USE_BETA_CI_FOR_CONFIDENCE)),
            ("BETA_PRIOR_ALPHA", float(self.BETA_PRIOR_ALPHA)),
            ("BETA_PRIOR_BETA", float(self.BETA_PRIOR_BETA)),
            ("ADD_K_CONSISTENT_OVERSHOOT", bool(self.ADD_K_CONSISTENT_OVERSHOOT)),
            ("TRADEBUILDER_ENFORCE_OTM", bool(self.TRADEBUILDER_ENFORCE_OTM)),

            ("ENABLE_BENZINGA", bool(self.ENABLE_BENZINGA)),
            ("BENZINGA_ENABLE_EVENT_RISK", bool(self.BENZINGA_ENABLE_EVENT_RISK)),
            ("BENZINGA_EVENT_RISK_AFFECTS_REGIME", bool(self.BENZINGA_EVENT_RISK_AFFECTS_REGIME)),
            ("BENZINGA_EVENT_RISK_AFFECTS_MC", bool(self.BENZINGA_EVENT_RISK_AFFECTS_MC)),
            ("BENZINGA_EVENT_RISK_HIGH_THRESHOLD", float(self.BENZINGA_EVENT_RISK_HIGH_THRESHOLD)),
            ("BENZINGA_EVENT_RISK_CAUTION_THRESHOLD", float(self.BENZINGA_EVENT_RISK_CAUTION_THRESHOLD)),
            ("BENZINGA_EVENT_RISK_REGIME_TAIL_BUMP_MAX_PCT", float(self.BENZINGA_EVENT_RISK_REGIME_TAIL_BUMP_MAX_PCT)),
            ("BENZINGA_EVENT_RISK_MC_WING_BUMP_MAX_PCT", float(self.BENZINGA_EVENT_RISK_MC_WING_BUMP_MAX_PCT)),

            ("ENABLE_MONTE_CARLO_EARNINGS", bool(self.ENABLE_MONTE_CARLO_EARNINGS)),
            ("MC_ENABLE_CONDITION_ON_QUARTER", bool(self.MC_ENABLE_CONDITION_ON_QUARTER)),
            ("MC_ENABLE_CONDITION_ON_REGIME", bool(self.MC_ENABLE_CONDITION_ON_REGIME)),
            ("MC_ENABLE_CONDITION_ON_TRADE_GATE", bool(self.MC_ENABLE_CONDITION_ON_TRADE_GATE)),
            ("MC_ENABLE_RECENCY_WEIGHTING", bool(self.MC_ENABLE_RECENCY_WEIGHTING)),
            ("MC_ENABLE_WING_OPTIMIZATION", bool(self.MC_ENABLE_WING_OPTIMIZATION)),
            ("MC_ENABLE_TAS_STABILITY", bool(self.MC_ENABLE_TAS_STABILITY)),

            ("MC_N_SIMS", int(self.MC_N_SIMS)),
            ("MC_BOOTSTRAP_N", int(self.MC_BOOTSTRAP_N)),
            ("MC_GLOBAL_SEED", int(self.MC_GLOBAL_SEED)),
            ("MC_MIN_POOL", int(self.MC_MIN_POOL)),
            ("MC_MIN_IMPLIED_MOVE_PCT", float(self.MC_MIN_IMPLIED_MOVE_PCT)),
            ("MC_RECENCY_HALFLIFE_EVENTS", int(self.MC_RECENCY_HALFLIFE_EVENTS)),

            ("MC_OPT_MAX_MULT_DELTA", float(self.MC_OPT_MAX_MULT_DELTA)),
            ("MC_OPT_STEP", float(self.MC_OPT_STEP)),
            ("MC_MAX_BREACH_EITHER_PCT", float(self.MC_MAX_BREACH_EITHER_PCT)),
            ("MC_MAX_CVAR95_TOTAL", float(self.MC_MAX_CVAR95_TOTAL)),
            ("MC_DEFAULT_WING_WIDTH_DOLLARS", float(self.MC_DEFAULT_WING_WIDTH_DOLLARS)),

            # --- Engine 1 GO/NO-GO knobs (affects additive decision payload; must be deterministic) ---
            ("GO_IVP_MIN", float(self.GO_IVP_MIN)),
            ("GO_IV_SAMPLE_MIN", int(self.GO_IV_SAMPLE_MIN)),
            ("GO_IV30_FLOOR", float(self.GO_IV30_FLOOR)),
            ("GO_IV_Z_ENABLED", bool(self.GO_IV_Z_ENABLED)),
            ("GO_IV30_Z_MIN", float(self.GO_IV30_Z_MIN)),
            ("GO_MIN_EARNINGS_N", int(self.GO_MIN_EARNINGS_N)),
            ("GO_EM_RICHNESS_MULT", float(self.GO_EM_RICHNESS_MULT)),
            ("GO_TAIL_SAMPLE_MIN", int(self.GO_TAIL_SAMPLE_MIN)),
            ("GO_TAIL_P90_MULT", float(self.GO_TAIL_P90_MULT)),
            ("GO_CORR20_HIGH", float(self.GO_CORR20_HIGH)),
            ("GO_BETA20_HIGH", float(self.GO_BETA20_HIGH)),
            ("GO_AVG_DOLLAR_VOL20D_MIN", float(self.GO_AVG_DOLLAR_VOL20D_MIN)),
            ("GO_AVG_DOLLAR_VOL20D_BLOCK", float(self.GO_AVG_DOLLAR_VOL20D_BLOCK)),
            ("GO_OPT_DELTA_BAND_LO", float(self.GO_OPT_DELTA_BAND_LO)),
            ("GO_OPT_DELTA_BAND_HI", float(self.GO_OPT_DELTA_BAND_HI)),
            ("GO_OPT_SPREAD_MAX", float(self.GO_OPT_SPREAD_MAX)),
            ("GO_OPT_SPREAD_BLOCK", float(self.GO_OPT_SPREAD_BLOCK)),
            ("GO_OPT_SPREAD_MAX_P90", float(self.GO_OPT_SPREAD_MAX_P90)),
            ("GO_OPT_SPREAD_P90_BLOCK", float(self.GO_OPT_SPREAD_P90_BLOCK)),
            ("GO_OPT_MIN_MID", float(self.GO_OPT_MIN_MID)),
            ("GO_OPT_OI_MIN", float(self.GO_OPT_OI_MIN)),
            ("GO_OPT_VOL_MIN", float(self.GO_OPT_VOL_MIN)),
            ("GO_BAND_QUOTE_COVERAGE_MIN", float(self.GO_BAND_QUOTE_COVERAGE_MIN)),
            ("GO_BAND_OI_SUM_MIN", float(self.GO_BAND_OI_SUM_MIN)),
            ("GO_BAND_OI_SUM_BLOCK", float(self.GO_BAND_OI_SUM_BLOCK)),
            ("GO_BAND_VOL_SUM_MIN", float(self.GO_BAND_VOL_SUM_MIN)),
            ("GO_BAND_VOL_SUM_BLOCK", float(self.GO_BAND_VOL_SUM_BLOCK)),
            ("GO_RV5_JUMP_MAX", float(self.GO_RV5_JUMP_MAX)),
            ("GO_RV20_JUMP_MAX", float(self.GO_RV20_JUMP_MAX)),
            ("GO_RV5_ACCEL_TIGHTEN_TRIGGER", float(self.GO_RV5_ACCEL_TIGHTEN_TRIGGER)),
            ("GO_FLIP_CUTOFF_BASE", float(self.GO_FLIP_CUTOFF_BASE)),
            ("GO_FLIP_CUTOFF_TIGHT", float(self.GO_FLIP_CUTOFF_TIGHT)),
            ("GO_FORCED_FLOW_WINDOW_TRADING_DAYS", int(self.GO_FORCED_FLOW_WINDOW_TRADING_DAYS)),
            ("GO_FORCED_FLOW_IMPORTANCE_HIGH_MIN", int(self.GO_FORCED_FLOW_IMPORTANCE_HIGH_MIN)),
            ("GO_FORCED_FLOW_IMPORTANCE_MED_MIN", int(self.GO_FORCED_FLOW_IMPORTANCE_MED_MIN)),
            ("GO_FORCED_FLOW_MANUAL_RANGES", tuple(self.GO_FORCED_FLOW_MANUAL_RANGES)),
            ("LEGAL_REG_TICKER_DENYLIST", tuple(self.LEGAL_REG_TICKER_DENYLIST)),
            ("LEGAL_REG_TICKER_ALLOWLIST", tuple(self.LEGAL_REG_TICKER_ALLOWLIST)),
            ("LEGAL_REG_KEYWORDS", tuple(self.LEGAL_REG_KEYWORDS)),
        )

    # Backwards-compatible alias used by some modules.
    def cache_fingerprint(self) -> tuple:
        return self.cache_key()

    def cache_key_engine2(self) -> tuple:
        """Engine 2 cache fingerprint (SPX IC engine)."""
        return (
            ("ENABLE_BENZINGA", bool(self.ENABLE_BENZINGA)),
            ("BENZINGA_ENABLE_EVENT_RISK", bool(self.BENZINGA_ENABLE_EVENT_RISK)),
            ("ENABLE_ENGINE2_SPX_IC", bool(self.ENABLE_ENGINE2_SPX_IC)),
            ("ENABLE_E2B_FLEX_EXPIRY", bool(self.ENABLE_E2B_FLEX_EXPIRY)),
            ("ENGINE2_ENTRY_DAYS", str(self.ENGINE2_ENTRY_DAYS)),
            ("ENGINE2_EM_MULTS", str(self.ENGINE2_EM_MULTS)),
            ("ENGINE2_WING_WIDTH_PTS", str(self.ENGINE2_WING_WIDTH_PTS)),
            ("ENGINE2_REQUIRE_ORATS_DAILY_VWAP", bool(self.ENGINE2_REQUIRE_ORATS_DAILY_VWAP)),
            ("ENGINE2_MAX_WEEKS_RETURN", int(self.ENGINE2_MAX_WEEKS_RETURN)),
            ("ENGINE2_LOOKBACK_YEARS_DEFAULT", int(self.ENGINE2_LOOKBACK_YEARS_DEFAULT)),
            ("ENGINE2_POLICY_MAX_BREACH_PCT", float(self.ENGINE2_POLICY_MAX_BREACH_PCT)),
            ("ENGINE2_POLICY_MAX_OUTSIDE_WINGS_PCT", float(self.ENGINE2_POLICY_MAX_OUTSIDE_WINGS_PCT)),
            ("ENGINE2_POLICY_MAX_MAE95_X_WING", float(self.ENGINE2_POLICY_MAX_MAE95_X_WING)),
            ("ENGINE2_REGIME_LOW_MAX", float(self.ENGINE2_REGIME_LOW_MAX)),
            ("ENGINE2_REGIME_MODERATE_MAX", float(self.ENGINE2_REGIME_MODERATE_MAX)),
            ("ENGINE2_REGIME_ELEVATED_MAX", float(self.ENGINE2_REGIME_ELEVATED_MAX)),
            ("ENGINE2_MACRO_LAMBDA", float(self.ENGINE2_MACRO_LAMBDA)),
            ("ENGINE2_MACRO_MULTIPLIER_CAP", float(self.ENGINE2_MACRO_MULTIPLIER_CAP)),
            ("ENGINE2_MACRO_BASE_CPI", float(self.ENGINE2_MACRO_BASE_CPI)),
            ("ENGINE2_MACRO_BASE_FOMC", float(self.ENGINE2_MACRO_BASE_FOMC)),
            ("ENGINE2_MACRO_BASE_NFP", float(self.ENGINE2_MACRO_BASE_NFP)),
            ("ENGINE2_MACRO_BASE_OPEX", float(self.ENGINE2_MACRO_BASE_OPEX)),
            ("ENGINE2_MACRO_BASE_REFUNDING", float(self.ENGINE2_MACRO_BASE_REFUNDING)),
            ("ENGINE2_MACRO_BASE_FOMC_MINUTES", float(self.ENGINE2_MACRO_BASE_FOMC_MINUTES)),
            ("ENGINE2_MACRO_BASE_GDP", float(self.ENGINE2_MACRO_BASE_GDP)),
            ("ENGINE2_MACRO_BASE_PCE", float(self.ENGINE2_MACRO_BASE_PCE)),
            ("ENGINE2_MACRO_BASE_PPI", float(self.ENGINE2_MACRO_BASE_PPI)),
            ("ENGINE2_MACRO_BASE_PMI_ISM", float(self.ENGINE2_MACRO_BASE_PMI_ISM)),
            ("ENGINE2_MACRO_BASE_RETAIL_SALES", float(self.ENGINE2_MACRO_BASE_RETAIL_SALES)),
            ("ENGINE2_MACRO_BASE_JOBLESS_CLAIMS", float(self.ENGINE2_MACRO_BASE_JOBLESS_CLAIMS)),
            ("ENGINE2_MACRO_BASE_TREASURY_AUCTION", float(self.ENGINE2_MACRO_BASE_TREASURY_AUCTION)),
            ("ENGINE2_MACRO_BASE_OTHER", float(self.ENGINE2_MACRO_BASE_OTHER)),
            ("ENGINE2_MULTI_WING", bool(self.ENGINE2_MULTI_WING)),
        )

    def cache_key_engine3(self) -> tuple:
        """Engine 3 cache fingerprint (Red Dog Reversal scanner)."""
        return (
            ("ENABLE_ENGINE3_RED_DOG", bool(self.ENABLE_ENGINE3_RED_DOG)),
            ("ENGINE3_CACHE_TTL_BARS", int(self.ENGINE3_CACHE_TTL_BARS)),
            ("ENGINE3_CACHE_TTL_SCAN", int(self.ENGINE3_CACHE_TTL_SCAN)),
            ("ENGINE3_MAX_WORKERS", int(self.ENGINE3_MAX_WORKERS)),
            ("ENGINE3_MIN_SCORE_DEFAULT", int(self.ENGINE3_MIN_SCORE_DEFAULT)),
            ("ENGINE3_APLUS_THRESHOLD", int(self.ENGINE3_APLUS_THRESHOLD)),
        )

    def cache_key_engine4(self) -> tuple:
        """Engine 4 cache fingerprint (Ichimoku Cloud Continuation scanner)."""
        return (
            ("ENABLE_ENGINE4_ICHIMOKU", bool(self.ENABLE_ENGINE4_ICHIMOKU)),
            ("ENGINE4_CACHE_TTL_BARS", int(self.ENGINE4_CACHE_TTL_BARS)),
            ("ENGINE4_CACHE_TTL_SCAN", int(self.ENGINE4_CACHE_TTL_SCAN)),
            ("ENGINE4_MAX_WORKERS", int(self.ENGINE4_MAX_WORKERS)),
            ("ENGINE4_MIN_DOLLAR_ADV", float(self.ENGINE4_MIN_DOLLAR_ADV)),
            ("ENGINE4_STRUCTURE_MAX", int(self.ENGINE4_STRUCTURE_MAX)),
            ("ENGINE4_MIN_SCORE_DEFAULT", int(self.ENGINE4_MIN_SCORE_DEFAULT)),
            ("ENGINE4_APLUS_THRESHOLD", int(self.ENGINE4_APLUS_THRESHOLD)),
        )

    def cache_key_engine5(self) -> tuple:
        """Engine 5 cache fingerprint (Global Lead-Lag Engine)."""
        return (
            ("ENABLE_ENGINE5_LEAD_LAG", bool(self.ENABLE_ENGINE5_LEAD_LAG)),
            ("ENGINE5_CACHE_TTL_LATEST", int(self.ENGINE5_CACHE_TTL_LATEST)),
            ("ENGINE5_CACHE_TTL_HISTORY", int(self.ENGINE5_CACHE_TTL_HISTORY)),
            ("ENGINE5_CORR_WINDOW", int(self.ENGINE5_CORR_WINDOW)),
            ("ENGINE5_CORR_THRESHOLD", float(self.ENGINE5_CORR_THRESHOLD)),
            ("ENGINE5_Z_SIGNIFICANT", float(self.ENGINE5_Z_SIGNIFICANT)),
            ("ENGINE5_REGIME_STRESSED_THRESHOLD", float(self.ENGINE5_REGIME_STRESSED_THRESHOLD)),
            ("ENGINE5_REGIME_RISK_OFF_THRESHOLD", float(self.ENGINE5_REGIME_RISK_OFF_THRESHOLD)),
            ("ENGINE5_REGIME_TRANSITIONAL_THRESHOLD", float(self.ENGINE5_REGIME_TRANSITIONAL_THRESHOLD)),
            ("ENGINE5_MAX_LAG_DAYS", int(self.ENGINE5_MAX_LAG_DAYS)),
            ("ENGINE5_LOOKBACK_DAYS", int(self.ENGINE5_LOOKBACK_DAYS)),
            ("ENGINE5_SNAPSHOT_TTL_S", int(self.ENGINE5_SNAPSHOT_TTL_S)),
            ("ENGINE5_SNAPSHOT_MAX_INDEX", int(self.ENGINE5_SNAPSHOT_MAX_INDEX)),
            ("ENGINE5_VOL_LEADLAG_ENABLED", bool(self.ENGINE5_VOL_LEADLAG_ENABLED)),
            ("ENGINE5_GLOBAL_VOL_RISING_THRESHOLD", float(self.ENGINE5_GLOBAL_VOL_RISING_THRESHOLD)),
            ("ENGINE5_GLOBAL_VOL_FALLING_THRESHOLD", float(self.ENGINE5_GLOBAL_VOL_FALLING_THRESHOLD)),
            ("ENGINE5_GLOBAL_VOL_NOISE_FLOOR", float(self.ENGINE5_GLOBAL_VOL_NOISE_FLOOR)),
            ("ENGINE5_US_IV_LOW_THRESHOLD", float(self.ENGINE5_US_IV_LOW_THRESHOLD)),
            ("ENGINE5_US_IV_HIGH_THRESHOLD", float(self.ENGINE5_US_IV_HIGH_THRESHOLD)),
            ("ENGINE5_VOL_ZSCORE_WINDOW", int(self.ENGINE5_VOL_ZSCORE_WINDOW)),
        )

    def cache_key_engine7(self) -> tuple:
        """Engine 7 cache fingerprint (Thematic Relative Value / Pairs).

        Excludes ENGINE7_ENABLE_LLM_ANNOTATION since LLM is annotation-only
        and never affects deterministic scoring (INV-1).
        """
        return (
            ("ENABLE_ENGINE7_PAIRS", bool(self.ENABLE_ENGINE7_PAIRS)),
            ("ENGINE7_CACHE_TTL_BARS", int(self.ENGINE7_CACHE_TTL_BARS)),
            ("ENGINE7_CACHE_TTL_SCAN", int(self.ENGINE7_CACHE_TTL_SCAN)),
            ("ENGINE7_MAX_WORKERS", int(self.ENGINE7_MAX_WORKERS)),
            ("ENGINE7_MIN_SCORE_DEFAULT", int(self.ENGINE7_MIN_SCORE_DEFAULT)),
            ("ENGINE7_APLUS_THRESHOLD", int(self.ENGINE7_APLUS_THRESHOLD)),
            ("ENGINE7_Z_SCORE_WINDOW", int(self.ENGINE7_Z_SCORE_WINDOW)),
            ("ENGINE7_Z_ENTRY_THRESHOLD", float(self.ENGINE7_Z_ENTRY_THRESHOLD)),
            ("ENGINE7_Z_MOMENTUM_THRESHOLD", float(self.ENGINE7_Z_MOMENTUM_THRESHOLD)),
            ("ENGINE7_MAX_CONCURRENT_PAIRS", int(self.ENGINE7_MAX_CONCURRENT_PAIRS)),
            ("ENGINE7_THEME_REQUIRED", bool(self.ENGINE7_THEME_REQUIRED)),
            ("ENGINE7_ENABLE_ORATS_VOL", bool(self.ENGINE7_ENABLE_ORATS_VOL)),
            ("ENGINE7_OVERLAP_CORR_THRESHOLD", float(self.ENGINE7_OVERLAP_CORR_THRESHOLD)),
            ("ENGINE7_OVERLAP_CORR_WINDOW", int(self.ENGINE7_OVERLAP_CORR_WINDOW)),
            ("GATE_PAIRS_REGIME_ALLOW", str(self.GATE_PAIRS_REGIME_ALLOW)),
            ("GATE_PAIRS_VOL_STATE_ALLOW", str(self.GATE_PAIRS_VOL_STATE_ALLOW)),
        )

    def cache_key_engine8(self) -> tuple:
        """Engine 8 cache fingerprint (Post-Event Trade Extension)."""
        return (
            ("ENABLE_ENGINE8_POST_EVENT", bool(self.ENABLE_ENGINE8_POST_EVENT)),
            ("ENGINE8_CACHE_TTL_EVAL", int(self.ENGINE8_CACHE_TTL_EVAL)),
            ("ENGINE8_CONFIDENCE_THRESHOLD", int(self.ENGINE8_CONFIDENCE_THRESHOLD)),
            ("ENGINE8_CONTINUE_THRESHOLD", int(self.ENGINE8_CONTINUE_THRESHOLD)),
            ("ENGINE8_FADE_THRESHOLD", int(self.ENGINE8_FADE_THRESHOLD)),
            ("ENGINE8_MIN_HISTORICAL_SAMPLE", int(self.ENGINE8_MIN_HISTORICAL_SAMPLE)),
            ("ENGINE8_EM_RATIO_OVER", float(self.ENGINE8_EM_RATIO_OVER)),
            ("ENGINE8_EM_RATIO_EXTREME", float(self.ENGINE8_EM_RATIO_EXTREME)),
            ("ENGINE8_ATR_ELEVATED", float(self.ENGINE8_ATR_ELEVATED)),
            ("ENGINE8_ATR_EXTREME", float(self.ENGINE8_ATR_EXTREME)),
            ("ENGINE8_MAX_RISK_UNITS", float(self.ENGINE8_MAX_RISK_UNITS)),
            ("ENGINE8_MIN_RISK_UNITS", float(self.ENGINE8_MIN_RISK_UNITS)),
            ("ENGINE8_MAX_HOLDING_DAYS", int(self.ENGINE8_MAX_HOLDING_DAYS)),
            ("ENGINE8_CONTINUATION_PROB_MIN", float(self.ENGINE8_CONTINUATION_PROB_MIN)),
            ("ENGINE8_REVERSION_PROB_MIN", float(self.ENGINE8_REVERSION_PROB_MIN)),
            ("ENGINE8_ENABLE_LLM_CLASSIFY", bool(self.ENGINE8_ENABLE_LLM_CLASSIFY)),
            ("ENGINE8_LLM_MODEL_VERSION", str(self.ENGINE8_LLM_MODEL_VERSION)),
            ("ENGINE8_LOOKBACK_EVENTS", int(self.ENGINE8_LOOKBACK_EVENTS)),
            ("ENGINE8_MAX_CONTROLLED_LOSS_PCT", float(self.ENGINE8_MAX_CONTROLLED_LOSS_PCT)),
        )

    def cache_key_engine14(self) -> tuple:
        """Engine 14 cache fingerprint (IC Scenario Simulator)."""
        return (
            ("ENABLE_ENGINE14_IC_SCENARIO", bool(self.ENABLE_ENGINE14_IC_SCENARIO)),
            ("ENGINE14_LOOKBACK_YEARS", int(self.ENGINE14_LOOKBACK_YEARS)),
            ("ENGINE14_MIN_ANALOGUES", int(self.ENGINE14_MIN_ANALOGUES)),
            ("ENGINE14_MAX_ANALOGUES", int(self.ENGINE14_MAX_ANALOGUES)),
            ("ENGINE14_REGIME_BUCKET_TOL", float(self.ENGINE14_REGIME_BUCKET_TOL)),
            ("ENGINE14_DEFAULT_PROFIT_TARGET_PCT", float(self.ENGINE14_DEFAULT_PROFIT_TARGET_PCT)),
            ("ENGINE14_DEFAULT_STOP_LOSS_PCT", float(self.ENGINE14_DEFAULT_STOP_LOSS_PCT)),
            ("ENGINE14_STRIKE_SNAP_MAX_PTS", float(self.ENGINE14_STRIKE_SNAP_MAX_PTS)),
            ("ENGINE14_BID_ASK_FILL_MODE", str(self.ENGINE14_BID_ASK_FILL_MODE)),
            ("ENGINE14_ENABLE_CONDITIONING", bool(self.ENGINE14_ENABLE_CONDITIONING)),
            ("ENGINE14_FILL_MODEL", str(self.ENGINE14_FILL_MODEL)),
            ("ENGINE14_FILL_PENALTY_PCT", float(self.ENGINE14_FILL_PENALTY_PCT)),
            ("ENGINE14_ENABLE_EM_MULTIPLE_FILTER", bool(self.ENGINE14_ENABLE_EM_MULTIPLE_FILTER)),
            ("ENGINE14_EM_MULTIPLE_TOL", float(self.ENGINE14_EM_MULTIPLE_TOL)),
            ("ENGINE14_MAE_PROXY_ENABLED", bool(self.ENGINE14_MAE_PROXY_ENABLED)),
            ("ENGINE14_ENABLE_KNN_REGIME", bool(self.ENGINE14_ENABLE_KNN_REGIME)),
            ("ENGINE14_KNN_TOP_N", int(self.ENGINE14_KNN_TOP_N)),
            ("ENGINE14_BOOTSTRAP_B", int(self.ENGINE14_BOOTSTRAP_B)),
            ("ENGINE14_BOOTSTRAP_SEED", int(self.ENGINE14_BOOTSTRAP_SEED)),
            ("ENGINE14_PER_DTE_EXITS", bool(self.ENGINE14_PER_DTE_EXITS)),
        )

    def cache_key_engine15(self) -> tuple:
        """Engine 15 cache fingerprint (Earnings IC Scenario Simulator)."""
        return (
            ("ENABLE_ENGINE15_EARNINGS_IC", bool(self.ENABLE_ENGINE15_EARNINGS_IC)),
            ("ENGINE15_MIN_EVENTS", int(self.ENGINE15_MIN_EVENTS)),
            ("ENGINE15_MAX_EVENTS", int(self.ENGINE15_MAX_EVENTS)),
            ("ENGINE15_FILL_MODEL", str(self.ENGINE15_FILL_MODEL)),
            ("ENGINE15_FILL_PENALTY_PCT", float(self.ENGINE15_FILL_PENALTY_PCT)),
            ("ENGINE15_STRIKE_SNAP_MAX_PTS", float(self.ENGINE15_STRIKE_SNAP_MAX_PTS)),
            ("ENGINE15_DEFAULT_PROFIT_TARGET_PCT", float(self.ENGINE15_DEFAULT_PROFIT_TARGET_PCT)),
            ("ENGINE15_DEFAULT_STOP_LOSS_PCT", float(self.ENGINE15_DEFAULT_STOP_LOSS_PCT)),
            ("ENGINE15_INTRADAY_CRUSH_FACTOR", float(self.ENGINE15_INTRADAY_CRUSH_FACTOR)),
            ("ENGINE15_ENABLE_CONDITIONING", bool(self.ENGINE15_ENABLE_CONDITIONING)),
            ("ENGINE15_MAX_EXPIRY_OFFSET_DAYS", int(self.ENGINE15_MAX_EXPIRY_OFFSET_DAYS)),
            ("ENGINE15_EM_MULTIPLE_TOL", float(self.ENGINE15_EM_MULTIPLE_TOL)),
            ("ENGINE15_ENABLE_EM_MULTIPLE_FILTER", bool(self.ENGINE15_ENABLE_EM_MULTIPLE_FILTER)),
            ("ENGINE15_EVENT_BACKFILL_DAYS_BEFORE", int(self.ENGINE15_EVENT_BACKFILL_DAYS_BEFORE)),
            ("ENGINE15_EVENT_BACKFILL_DAYS_AFTER", int(self.ENGINE15_EVENT_BACKFILL_DAYS_AFTER)),
        )

    def cache_key_engine12(self) -> tuple:
        """Engine 12 cache fingerprint (VIX Spike Fade / Vol Dislocation)."""
        return (
            ("ENABLE_ENGINE12_VIX_FADE", bool(self.ENABLE_ENGINE12_VIX_FADE)),
            ("ENGINE12_MC_N_SIMS", int(self.ENGINE12_MC_N_SIMS)),
            ("ENGINE12_MC_SEED", int(self.ENGINE12_MC_SEED)),
            ("ENGINE12_OU_CALIBRATION_LOOKBACK_DAYS", int(self.ENGINE12_OU_CALIBRATION_LOOKBACK_DAYS)),
            ("ENGINE12_SECONDARY_SPIKE_THRESHOLD", float(self.ENGINE12_SECONDARY_SPIKE_THRESHOLD)),
            ("ENGINE12_CONTAINED_THRESHOLD", float(self.ENGINE12_CONTAINED_THRESHOLD)),
            ("ENGINE12_DEALER_GAMMA_ENABLED", bool(self.ENGINE12_DEALER_GAMMA_ENABLED)),
            ("ENGINE12_STRESS_WEIGHT_OIL", float(self.ENGINE12_STRESS_WEIGHT_OIL)),
            ("ENGINE12_STRESS_WEIGHT_GOLD", float(self.ENGINE12_STRESS_WEIGHT_GOLD)),
            ("ENGINE12_STRESS_WEIGHT_HYG", float(self.ENGINE12_STRESS_WEIGHT_HYG)),
            ("ENGINE12_STRESS_WEIGHT_DXY", float(self.ENGINE12_STRESS_WEIGHT_DXY)),
            ("ENGINE12_STRESS_WEIGHT_TLT_VOL", float(self.ENGINE12_STRESS_WEIGHT_TLT_VOL)),
            ("ENGINE12_GAMMA_AMP_LOW", float(self.ENGINE12_GAMMA_AMP_LOW)),
            ("ENGINE12_GAMMA_AMP_MED", float(self.ENGINE12_GAMMA_AMP_MED)),
            ("ENGINE12_GAMMA_AMP_HIGH", float(self.ENGINE12_GAMMA_AMP_HIGH)),
        )


def get_flags() -> FeatureFlags:
    """
    Env-driven flags loader.

    Note: we intentionally re-read env vars each call (cheap) so unit tests that
    use monkeypatch.setenv(...) behave correctly without requiring extra reset hooks.
    """
    return FeatureFlags.from_env()
