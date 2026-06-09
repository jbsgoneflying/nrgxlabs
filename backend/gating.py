"""NRGX Labs 2.0 – Gating layer for Engine 3 (Red Dog), Engine 4 (Ichimoku), and Engine 7 (Pairs).

Converts every scan result into one of three statuses:
  TRADABLE – green-lit for execution consideration
  WATCH    – conditions are marginal; watch but don't trade yet
  SUPPRESS – one or more hard failures; do not trade

All rules are config-driven, explicit, and explainable.
Each rule emits a GateReason with severity HARD (suppress) or SOFT (watch).

Resolution:
  1. ANY HARD reason → SUPPRESS
  2. ANY SOFT reason → WATCH
  3. Otherwise       → TRADABLE
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GateReason:
    code: str                   # e.g. REGIME_MISMATCH
    label: str                  # human-readable
    severity: str               # HARD | SOFT
    detail: str                 # specific threshold info
    source_value: Any = None    # actual value
    threshold_value: Any = None # required value

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GateDecision:
    ticker: str
    engine: str                 # engine3_red_dog | engine4_ichimoku
    status: str                 # TRADABLE | WATCH | SUPPRESS
    reasons: List[dict] = field(default_factory=list)
    decided_at: str = ""
    inputs: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Gating rule functions
# ---------------------------------------------------------------------------

def _check_regime(
    regime_label: str,
    allowed_labels: List[str],
    severity: str = "HARD",
) -> Optional[GateReason]:
    """Check if current regime is in the allowed set."""
    if not regime_label:
        return GateReason(
            code="REGIME_MISSING",
            label="Regime data unavailable",
            severity="SOFT",
            detail="No regime data available for gating",
            source_value=None,
            threshold_value=allowed_labels,
        )
    if regime_label not in allowed_labels:
        return GateReason(
            code="REGIME_MISMATCH",
            label="Regime mismatch",
            severity=severity,
            detail=f"Current regime '{regime_label}' not in allowed: {allowed_labels}",
            source_value=regime_label,
            threshold_value=allowed_labels,
        )
    return None


def _check_vol_state(
    vol_direction: str,
    allowed_states: List[str],
    severity: str = "SOFT",
) -> Optional[GateReason]:
    """Check if vol state is in the allowed set."""
    if not vol_direction:
        return None  # pass when missing
    if vol_direction.lower() not in [s.lower() for s in allowed_states]:
        return GateReason(
            code="VOL_STATE_MISMATCH",
            label="Vol state mismatch",
            severity=severity,
            detail=f"Current vol state '{vol_direction}' not in allowed: {allowed_states}",
            source_value=vol_direction,
            threshold_value=allowed_states,
        )
    return None


def _check_macro_proximity(
    high_events_within_days: int,
    max_days: int,
    severity: str = "HARD",
) -> Optional[GateReason]:
    """Check if high-severity macro event is too close."""
    if high_events_within_days > 0 and max_days >= 0:
        return GateReason(
            code="MACRO_EVENT_PROXIMITY",
            label="Macro event proximity",
            severity=severity,
            detail=f"{high_events_within_days} high-severity event(s) within {max_days} trading day(s)",
            source_value=high_events_within_days,
            threshold_value=max_days,
        )
    return None


def _check_index_alignment(
    setup_direction: Optional[str],
    index_state: Optional[dict],
    *,
    beta: Optional[float] = None,
    corr: Optional[float] = None,
    beta_hard: float = 1.0,
    corr_hard: float = 0.6,
) -> Optional[GateReason]:
    """Top-down index-trend filter for a continuation setup.

    A long wants its index trending up (or at least not down); a short wants
    the index trending down. The bite is beta/correlation-aware: an
    index-coupled name (high beta or high correlation) is a hostage to the
    tape, so a mismatch HARD-suppresses. A low-correlation idiosyncratic name
    can buck the index, so the same mismatch is only a SOFT watch. A neutral
    index (price inside its own cloud) is always SOFT.
    """
    if not index_state or not isinstance(index_state, dict) or not index_state.get("available"):
        return None  # no data → don't gate

    idx_dir = str(index_state.get("direction") or "neutral").lower()
    is_bearish = str(setup_direction or "").lower() in ("bearish", "bear", "short")
    want = "bearish" if is_bearish else "bullish"
    opposite = "bullish" if is_bearish else "bearish"

    coupled = (
        (beta is not None and beta >= beta_hard)
        or (corr is not None and corr >= corr_hard)
    )

    if idx_dir == opposite:
        severity = "HARD" if coupled else "SOFT"
        coupling = (
            f"beta {beta:.2f}" if beta is not None else
            (f"corr {corr:.2f}" if corr is not None else "coupling unknown")
        )
        return GateReason(
            code="INDEX_TREND_MISMATCH",
            label="Index trend mismatch",
            severity=severity,
            detail=(
                f"{want} setup vs {idx_dir} index "
                f"({index_state.get('symbol') or 'index'}); {coupling}"
            ),
            source_value={"indexDirection": idx_dir, "beta": beta, "corr": corr},
            threshold_value=want,
        )

    if idx_dir == "neutral":
        return GateReason(
            code="INDEX_TREND_NEUTRAL",
            label="Index trend neutral",
            severity="SOFT",
            detail=f"Index ({index_state.get('symbol') or 'index'}) is inside its own cloud",
            source_value="neutral",
            threshold_value=want,
        )

    return None


def _check_dealer_gamma_hostile(
    gamma_ctx: Optional[dict],
) -> Optional[GateReason]:
    """WATCH if dealer gamma is hostile (negative + high magnitude)."""
    if not gamma_ctx or not isinstance(gamma_ctx, dict):
        return None
    sign = str(gamma_ctx.get("netGammaSign") or "").lower()
    mag = str(gamma_ctx.get("magnitudeBucket") or "").lower()
    if sign == "negative" and mag in ("high", "medium"):
        return GateReason(
            code="DEALER_GAMMA_HOSTILE",
            label="Dealer gamma instability",
            severity="SOFT",
            detail=f"Dealer gamma is {sign} with {mag} magnitude",
            source_value={"sign": sign, "magnitude": mag},
            threshold_value="positive or low negative",
        )
    return None


# ---------------------------------------------------------------------------
# Resolve final status
# ---------------------------------------------------------------------------

def _resolve_status(reasons: List[GateReason]) -> str:
    """
    1. ANY HARD reason → SUPPRESS
    2. ANY SOFT reason → WATCH
    3. Otherwise       → TRADABLE
    """
    for r in reasons:
        if r.severity == "HARD":
            return "SUPPRESS"
    for r in reasons:
        if r.severity == "SOFT":
            return "WATCH"
    return "TRADABLE"


# ---------------------------------------------------------------------------
# Engine-specific gating
# ---------------------------------------------------------------------------


def gate_red_dog(
    *,
    ticker: str,
    setup_direction: Optional[str] = None,
    regime_label: str = "",
    vol_direction: str = "",
    gamma_ctx: Optional[dict] = None,
    high_events_within_days: int = 0,
    # Config overrides
    regime_allow: Optional[List[str]] = None,
    vol_state_allow: Optional[List[str]] = None,
    macro_proximity_days: int = 1,
) -> GateDecision:
    """Gate a Red Dog Reversal setup.

    Red Dog is a *mean-reversion* system, so it thrives in the SAME conditions
    the dealer-gamma overlay calls supportive — calm, long-gamma, range-bound
    tape where dips get bought and rips get sold:
      - Regime is Risk-On or Transitional (not Stressed/trending-hard)
      - Vol state is compressing / stable / falling (not expanding)
      - Dealer gamma is not strongly hostile (negative + high magnitude)

    (This deliberately matches `fetch_spx_gamma_context`'s thesis; the prior
    "wants Stressed + expanding vol" allow-list contradicted the gamma overlay
    and is the bug the 2026-06 audit flagged.)
    """
    reasons: List[GateReason] = []
    allowed_regimes = regime_allow or ["Risk-On", "Transitional"]
    allowed_vol = vol_state_allow or [
        "compressing", "stable", "falling", "NORMAL", "FALLING", "flat", "contango"
    ]

    r = _check_regime(regime_label, allowed_regimes, severity="HARD")
    if r:
        reasons.append(r)

    r = _check_vol_state(vol_direction, allowed_vol, severity="SOFT")
    if r:
        reasons.append(r)

    r = _check_macro_proximity(high_events_within_days, macro_proximity_days, severity="HARD")
    if r:
        reasons.append(r)

    r = _check_dealer_gamma_hostile(gamma_ctx)
    if r:
        reasons.append(r)

    status = _resolve_status(reasons)
    now = dt.datetime.utcnow().isoformat() + "Z"

    return GateDecision(
        ticker=ticker,
        engine="engine3_red_dog",
        status=status,
        reasons=[r.to_dict() for r in reasons],
        decided_at=now,
        inputs={
            "regime_label": regime_label,
            "vol_direction": vol_direction,
            "high_events_within_days": high_events_within_days,
        },
    )


def gate_ichimoku(
    *,
    ticker: str,
    setup_direction: Optional[str] = None,
    regime_label: str = "",
    regime_confidence: Optional[float] = None,
    vol_direction: str = "",
    gamma_ctx: Optional[dict] = None,
    high_events_within_days: int = 0,
    # Config overrides
    regime_allow: Optional[List[str]] = None,
    regime_allow_short: Optional[List[str]] = None,
    vol_state_allow: Optional[List[str]] = None,
    macro_proximity_days: int = 1,
    regime_min_confidence: float = 0.0,
    # Index-trend alignment (top-down stack)
    index_state: Optional[dict] = None,
    index_align_enable: bool = False,
    index_beta: Optional[float] = None,
    index_corr: Optional[float] = None,
    index_beta_hard: float = 1.0,
    index_corr_hard: float = 0.6,
) -> GateDecision:
    """Gate an Ichimoku Cloud Continuation setup.

    Continuation is *direction-aware* — a trend-following engine should not be
    suppressed simply because the tape is risk-off; it depends which way the
    name is trending:
      - Bullish continuation thrives in Risk-On / stable Transitional tape.
      - Bearish continuation thrives in Risk-Off / Stressed / Transitional tape
        (short trends accelerate exactly when the broad regime is stressed).
      - Either way, prefer non-expanding vol so the pullback resolves cleanly.
    """
    reasons: List[GateReason] = []
    allowed_long = regime_allow or ["Risk-On", "Transitional"]
    allowed_short = regime_allow_short or ["Risk-Off", "Stressed", "Transitional"]
    is_bearish = str(setup_direction or "").lower() in ("bearish", "bear", "short")
    allowed_regimes = allowed_short if is_bearish else allowed_long
    allowed_vol = vol_state_allow or ["compressing", "stable", "NORMAL", "FALLING", "falling", "flat"]

    # Confidence-aware regime gating: a regime mismatch only HARD-suppresses
    # when the classifier is sufficiently confident. A low-confidence (near-tie)
    # regime read shouldn't blanket-kill a whole direction, so demote the
    # mismatch to SOFT (WATCH) below the threshold.
    low_confidence_regime = (
        regime_min_confidence > 0
        and regime_confidence is not None
        and regime_confidence < regime_min_confidence
    )
    regime_severity = "SOFT" if low_confidence_regime else "HARD"

    r = _check_regime(regime_label, allowed_regimes, severity=regime_severity)
    if r:
        if low_confidence_regime and r.code == "REGIME_MISMATCH":
            r.detail += (
                f" — low-confidence regime (conf {regime_confidence:.2f}"
                f" < {regime_min_confidence:.2f}); WATCH not SUPPRESS"
            )
        reasons.append(r)

    r = _check_vol_state(vol_direction, allowed_vol, severity="SOFT")
    if r:
        reasons.append(r)

    r = _check_macro_proximity(high_events_within_days, macro_proximity_days, severity="SOFT")
    if r:
        reasons.append(r)

    if index_align_enable:
        r = _check_index_alignment(
            setup_direction,
            index_state,
            beta=index_beta,
            corr=index_corr,
            beta_hard=index_beta_hard,
            corr_hard=index_corr_hard,
        )
        if r:
            reasons.append(r)

    status = _resolve_status(reasons)
    now = dt.datetime.utcnow().isoformat() + "Z"

    return GateDecision(
        ticker=ticker,
        engine="engine4_ichimoku",
        status=status,
        reasons=[r.to_dict() for r in reasons],
        decided_at=now,
        inputs={
            "regime_label": regime_label,
            "regime_confidence": regime_confidence,
            "regime_min_confidence": regime_min_confidence,
            "vol_direction": vol_direction,
            "high_events_within_days": high_events_within_days,
            "index_align_enable": index_align_enable,
            "index_direction": (index_state or {}).get("direction") if isinstance(index_state, dict) else None,
            "index_beta": index_beta,
            "index_corr": index_corr,
        },
    )


# ---------------------------------------------------------------------------
# Engine 7: Thematic Relative Value (Pairs) gating  (INV-4)
# ---------------------------------------------------------------------------
#
# All inputs are optional with safe defaults.  No gating on fake data.
#
# - regime_label / vol_direction: SOFT if missing (warn, don't suppress)
# - macro_proximity: omitted in v1 (hardcoded 0 in platform; not reliable)


def gate_engine7_pair(
    signal: Any,
    regime_label: str = "",
    vol_direction: str = "",
    *,
    regime_allow: str = "",
    vol_state_allow: str = "",
) -> GateDecision:
    """Gate an Engine 7 pairs signal.

    INV-4: all inputs are optional.  Missing inputs produce SOFT reasons
    (WATCH) but never SUPPRESS on their own.
    """
    pair_id = ""
    if isinstance(signal, dict):
        pair_id = signal.get("pair_id", "")

    reasons: List[GateReason] = []

    # Regime check – SOFT only
    if regime_allow:
        allowed_regimes = [s.strip() for s in regime_allow.split(",") if s.strip()]
    else:
        allowed_regimes = []  # empty = all allowed

    if allowed_regimes:
        r = _check_regime(regime_label, allowed_regimes, severity="SOFT")
        if r:
            reasons.append(r)
    elif not regime_label:
        reasons.append(GateReason(
            code="REGIME_MISSING",
            label="Regime data unavailable",
            severity="SOFT",
            detail="No regime data available; pairs gating is informational only",
            source_value=None,
            threshold_value=None,
        ))

    # Vol state check – SOFT only, informational
    if vol_state_allow:
        allowed_vol = [s.strip() for s in vol_state_allow.split(",") if s.strip()]
    else:
        allowed_vol = []

    if allowed_vol:
        r = _check_vol_state(vol_direction, allowed_vol, severity="SOFT")
        if r:
            reasons.append(r)
    elif not vol_direction:
        reasons.append(GateReason(
            code="VOL_MISSING",
            label="Vol state data unavailable",
            severity="SOFT",
            detail="No vol state data available; pairs gating is informational only",
            source_value=None,
            threshold_value=None,
        ))

    # Macro proximity – omitted in v1 (INV-4)

    # Resolve
    has_hard = any(r.severity == "HARD" for r in reasons)
    has_soft = any(r.severity == "SOFT" for r in reasons)

    if has_hard:
        status = "SUPPRESS"
    elif has_soft:
        status = "WATCH"
    else:
        status = "TRADABLE"

    return GateDecision(
        ticker=pair_id,
        engine="engine7_pairs",
        status=status,
        reasons=[r.to_dict() for r in reasons],
        decided_at=dt.datetime.utcnow().isoformat() + "Z",
        inputs={
            "regime_label": regime_label,
            "vol_direction": vol_direction,
            "regime_allow": regime_allow,
            "vol_state_allow": vol_state_allow,
        },
    )


# ---------------------------------------------------------------------------
# Batch gating helpers
# ---------------------------------------------------------------------------


def gate_scan_results(
    *,
    scan_results: List[dict],
    engine: str,
    regime_label: str = "",
    regime_confidence: Optional[float] = None,
    vol_direction: str = "",
    gamma_ctx: Optional[dict] = None,
    high_events_within_days: int = 0,
    regime_allow: Optional[List[str]] = None,
    regime_allow_short: Optional[List[str]] = None,
    vol_state_allow: Optional[List[str]] = None,
    regime_min_confidence: float = 0.0,
    # Index-trend alignment (Ichimoku top-down stack)
    index_states: Optional[Dict[str, dict]] = None,
    index_align_enable: bool = False,
    index_beta_hard: float = 1.0,
    index_corr_hard: float = 0.6,
) -> List[dict]:
    """Apply gating to a list of scan results, adding gate decision to each.

    Returns the same list with 'gate' field injected into each result dict.
    `regime_allow` / `regime_allow_short` / `vol_state_allow` let the router
    pass config-driven allow-lists (so the policy is tunable without a code
    change). `regime_allow_short` is Ichimoku-only (direction-aware gating).
    `index_states` is keyed by index ("spx"/"ndx") and selected per result via
    its `indexMembership`; per-name beta/corr are read from each result's
    `indicators` block.
    """
    is_ichimoku = engine != "engine3_red_dog"
    gate_fn = gate_red_dog if engine == "engine3_red_dog" else gate_ichimoku

    for result in scan_results:
        ticker = str(result.get("ticker") or result.get("symbol") or "")
        direction = str(result.get("direction") or "")

        kwargs = dict(
            ticker=ticker,
            setup_direction=direction or None,
            regime_label=regime_label,
            vol_direction=vol_direction,
            gamma_ctx=gamma_ctx,
            high_events_within_days=high_events_within_days,
            regime_allow=regime_allow,
            vol_state_allow=vol_state_allow,
        )
        if is_ichimoku:
            kwargs["regime_allow_short"] = regime_allow_short
            kwargs["regime_confidence"] = regime_confidence
            kwargs["regime_min_confidence"] = regime_min_confidence

            # Select the index state for this name and its index coupling.
            idx_state = None
            if index_states:
                membership = str(result.get("indexMembership") or "sp500")
                key = "ndx" if membership == "nasdaq100" else "spx"
                idx_state = index_states.get(key)
            indicators = result.get("indicators") if isinstance(result.get("indicators"), dict) else {}
            kwargs["index_state"] = idx_state
            kwargs["index_align_enable"] = index_align_enable
            kwargs["index_beta"] = indicators.get("beta")
            kwargs["index_corr"] = indicators.get("corr")
            kwargs["index_beta_hard"] = index_beta_hard
            kwargs["index_corr_hard"] = index_corr_hard

        decision = gate_fn(**kwargs)
        result["gate"] = decision.to_dict()

    return scan_results


# ---------------------------------------------------------------------------
# Reconciled per-name verdict (Red Dog)
# ---------------------------------------------------------------------------
#
# The audit flagged that the pattern grade, the gamma overlay, the trend filter,
# and the gate each gave the trader a *different* answer. This collapses them
# into ONE desk verdict — TRADABLE / WATCH / STAND_DOWN — with explicit drivers,
# and the UI leads with it.

VERDICT_TRADABLE = "TRADABLE"
VERDICT_WATCH = "WATCH"
VERDICT_STAND_DOWN = "STAND_DOWN"


def reconcile_red_dog_verdict(
    signal: dict,
    *,
    gamma_ctx: Optional[dict] = None,
    regime_label: str = "",
) -> Dict[str, Any]:
    """Collapse grade + gate + gamma + trend into one verdict for a signal.

    Demotion ladder (a single failure caps the verdict):
      - Gate SUPPRESS, or grade C            → STAND_DOWN
      - Counter-trend / unconfirmed / gate
        WATCH / hostile gamma / grade B      → at most WATCH
      - Grade A or A+, none of the above     → TRADABLE
    """
    quality = signal.get("quality", {}) if isinstance(signal.get("quality"), dict) else {}
    gate = signal.get("gate", {}) if isinstance(signal.get("gate"), dict) else {}

    grade = str(quality.get("grade") or "C")
    confirmed = bool(quality.get("confirmed", True))
    trend_alignment = str(quality.get("trendAlignment") or "unknown")
    gate_status = str(gate.get("status") or "")
    score = float(quality.get("score") or 0.0)

    gamma_env = str((gamma_ctx or {}).get("environment") or "").lower()

    drivers: List[str] = []
    verdict = VERDICT_TRADABLE

    def demote(to: str, reason: str):
        nonlocal verdict
        order = {VERDICT_TRADABLE: 2, VERDICT_WATCH: 1, VERDICT_STAND_DOWN: 0}
        if order[to] < order[verdict]:
            verdict = to
        drivers.append(reason)

    # Hard stand-downs
    if gate_status == "SUPPRESS":
        demote(VERDICT_STAND_DOWN, "Gate: regime/macro suppression")
    if grade == "C":
        demote(VERDICT_STAND_DOWN, "Pattern grade C")

    # Watch-level caps
    if grade == "B":
        demote(VERDICT_WATCH, "Pattern grade B")
    if trend_alignment == "counter":
        demote(VERDICT_WATCH, "Counter-trend vs SPX")
    if not confirmed:
        demote(VERDICT_WATCH, "Unconfirmed reversal (weak tail + light volume)")
    if gate_status == "WATCH":
        demote(VERDICT_WATCH, "Gate: marginal regime/vol")
    if gamma_env == "challenging":
        demote(VERDICT_WATCH, "Dealer gamma hostile to mean reversion")

    if verdict == VERDICT_TRADABLE:
        drivers.append(f"Grade {grade}, with-trend, gate clear")

    labels = {
        VERDICT_TRADABLE: "Tradable",
        VERDICT_WATCH: "Watch",
        VERDICT_STAND_DOWN: "Stand down",
    }
    return {
        "status": verdict,
        "label": labels[verdict],
        "conviction": round(score, 1),
        "drivers": drivers[:4],
        "inputs": {
            "grade": grade,
            "trendAlignment": trend_alignment,
            "gateStatus": gate_status or "n/a",
            "gammaEnvironment": gamma_env or "n/a",
            "confirmed": confirmed,
            "regimeLabel": regime_label or "n/a",
        },
    }


def reconcile_ichimoku_verdict(
    signal: dict,
    *,
    gamma_ctx: Optional[dict] = None,
    regime_label: str = "",
) -> Dict[str, Any]:
    """Collapse grade + freshness + gate + gamma into one continuation verdict.

    Ichimoku is a *trend-continuation* engine, so the read differs from the
    mean-reversion ladder:
      - Gate SUPPRESS, grade C, or freshness 'rejected'   → STAND_DOWN
      - 'structure' (not actionable today), grade B, gate
        WATCH, gamma hostile, or earnings warning          → at most WATCH
      - Actionable A/A+, continuation-friendly gamma, gate
        clear                                              → TRADABLE
    """
    quality = signal.get("quality", {}) if isinstance(signal.get("quality"), dict) else {}
    gate = signal.get("gate", {}) if isinstance(signal.get("gate"), dict) else {}
    freshness = signal.get("freshness", {}) if isinstance(signal.get("freshness"), dict) else {}
    tags = signal.get("tags", []) if isinstance(signal.get("tags"), list) else []
    indicators = signal.get("indicators", {}) if isinstance(signal.get("indicators"), dict) else {}

    grade = str(quality.get("grade") or "C")
    score = float(quality.get("score") or 0.0)
    gate_status = str(gate.get("status") or "")
    bucket = str(freshness.get("bucket") or "actionable")

    direction = str(signal.get("direction") or "").lower()
    is_bearish = direction in ("bearish", "bear", "short")
    want_sector = "bearish" if is_bearish else "bullish"
    sector_bias = str(indicators.get("sectorBias") or "").lower()
    rs_ratio = indicators.get("rsRatio")
    index_direction = str(indicators.get("indexDirection") or "").lower()

    # Continuation likes orderly, dealer-stabilised tape (positive/long gamma);
    # negative gamma ("challenging") can whip the pullback before it resumes.
    gamma_env = str((gamma_ctx or {}).get("environment") or "").lower()

    drivers: List[str] = []
    verdict = VERDICT_TRADABLE

    def demote(to: str, reason: str):
        nonlocal verdict
        order = {VERDICT_TRADABLE: 2, VERDICT_WATCH: 1, VERDICT_STAND_DOWN: 0}
        if order[to] < order[verdict]:
            verdict = to
        drivers.append(reason)

    # Hard stand-downs
    if gate_status == "SUPPRESS":
        demote(VERDICT_STAND_DOWN, "Gate: regime/macro suppression")
    if grade == "C":
        demote(VERDICT_STAND_DOWN, "Pattern grade C")
    if bucket == "rejected":
        demote(VERDICT_STAND_DOWN, "Freshness: setup invalidated")

    # Watch-level caps
    if bucket == "structure":
        demote(VERDICT_WATCH, "Structure only — no fresh trigger today")
    if grade == "B":
        demote(VERDICT_WATCH, "Pattern grade B")
    if gate_status == "WATCH":
        demote(VERDICT_WATCH, "Gate: marginal regime/vol")
    if gamma_env == "challenging":
        demote(VERDICT_WATCH, "Dealer gamma can whip the pullback")
    if "Earnings Warning" in tags:
        demote(VERDICT_WATCH, "Earnings inside the hold window")
    # Sector trend: a name fighting its own sector is at most a watch.
    if sector_bias in ("bullish", "bearish") and sector_bias != want_sector:
        demote(VERDICT_WATCH, "Fights its sector trend")
    # Relative strength: a laggard long (or a leader you're shorting) is weak.
    if rs_ratio is not None:
        if not is_bearish and rs_ratio < 0:
            demote(VERDICT_WATCH, "Lagging its index (weak leadership)")
        elif is_bearish and rs_ratio > 0:
            demote(VERDICT_WATCH, "Outperforming its index on a short")

    if verdict == VERDICT_TRADABLE:
        drivers.append(f"Grade {grade}, fresh trigger, gate clear")

    labels = {
        VERDICT_TRADABLE: "Tradable",
        VERDICT_WATCH: "Watch",
        VERDICT_STAND_DOWN: "Stand down",
    }
    return {
        "status": verdict,
        "label": labels[verdict],
        "conviction": round(score, 1),
        "drivers": drivers[:4],
        "inputs": {
            "grade": grade,
            "freshnessBucket": bucket,
            "gateStatus": gate_status or "n/a",
            "gammaEnvironment": gamma_env or "n/a",
            "regimeLabel": regime_label or "n/a",
            "sectorBias": sector_bias or "n/a",
            "indexDirection": index_direction or "n/a",
            "rsRatio": rs_ratio,
        },
    }


def summarize_verdicts(scan_results: List[dict]) -> dict:
    """Count reconciled verdicts across scan results."""
    counts = {VERDICT_TRADABLE: 0, VERDICT_WATCH: 0, VERDICT_STAND_DOWN: 0, "total": 0}
    for r in scan_results:
        v = r.get("verdict") if isinstance(r.get("verdict"), dict) else {}
        status = str(v.get("status") or "")
        counts["total"] += 1
        if status in counts:
            counts[status] += 1
    return counts


def summarize_gates(scan_results: List[dict]) -> dict:
    """Produce a summary of gate statuses across all scan results."""
    counts = {"TRADABLE": 0, "WATCH": 0, "SUPPRESS": 0, "total": 0}
    for r in scan_results:
        gate = r.get("gate") if isinstance(r.get("gate"), dict) else {}
        status = str(gate.get("status") or "UNKNOWN")
        counts["total"] += 1
        if status in counts:
            counts[status] += 1
    return counts
