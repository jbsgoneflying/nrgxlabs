from __future__ import annotations

import os
from dataclasses import dataclass


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
    ENABLE_MONTE_CARLO_EARNINGS: bool = False
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

            ENABLE_MONTE_CARLO_EARNINGS=_get_bool("ENABLE_MONTE_CARLO_EARNINGS", False),
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
        )

    def cache_key(self) -> tuple:
        # Keep it stable and JSON-safe (tuples of primitives).
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
        )

    # Backwards-compatible alias used by some modules.
    def cache_fingerprint(self) -> tuple:
        return self.cache_key()


def get_flags() -> FeatureFlags:
    """
    Env-driven flags loader.

    Note: we intentionally re-read env vars each call (cheap) so unit tests that
    use monkeypatch.setenv(...) behave correctly without requiring extra reset hooks.
    """
    return FeatureFlags.from_env()
