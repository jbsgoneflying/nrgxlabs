"""Wing Decision Console scorer for the E14 IC Scenario Command Deck.

Parallel to :mod:`backend.engine2.wing_console`, but sourced from
E14's analogue pool + forward MC + empirical MAE rather than the
SPX scan. Where E2 scans current-week placements off a rolling
historical pool, E14 scores candidate placements for a **specific
forward scenario** (entry_date + expiry_date the desk already picked)
so the desk can see "given THIS window, where should my wings go?"
before jumping into the full analogue-replay drilldown.

Composite formula (identical shape to E2 so the UI/advisor prompts
can share wording):

    composite = 100 * (
        w_breach * (1 - breach_close_prob)    +
        w_touch  * (1 - touch_intraweek_prob) +
        w_mae    * (1 - clamp(mae_p95_vs_wing / MAX_TOLERABLE_MAE, 0, 1)) +
        w_theta  * clamp(theta_capture / TARGET_THETA, 0, 1) +
        w_credit * clamp(roc_est / TARGET_ROC, 0, 1)
    )

Weights are renormalised against the running weight total so the
desk can tune individual knobs without rescaling neighbours.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.config import FeatureFlags, get_flags
from backend.engine14.mae_proxy import MAEDistribution, mae_p95_vs_wing_ratio
from backend.engine14.mc_simulator import MCResult, run_forward_mc
from backend.engine14.scoring_context import (
    ScoringContext,
    get_scoring_context,
    store_scoring_context,
)
from backend.engine2.mc_simulator import MCPlacementResult

LOG = logging.getLogger("engine14.wing_console")


# ---------------------------------------------------------------------------
# Weights + placement dataclasses
# ---------------------------------------------------------------------------


@dataclass
class WingConsoleWeights:
    close:  float = 0.25   # aliased "breach" in plan; both names accepted
    touch:  float = 0.20
    mae:    float = 0.25
    theta:  float = 0.15
    credit: float = 0.15

    max_tolerable_mae_pct: float = 80.0
    target_theta_pct:      float = 60.0
    target_roc_pct:        float = 12.0

    def as_dict(self) -> Dict[str, float]:
        return asdict(self)

    @classmethod
    def from_flags(cls, flags: FeatureFlags) -> "WingConsoleWeights":
        # Plan knob name is E14_WING_SCORE_WEIGHT_BREACH; we support that
        # and the close-named alias so tests + env files can pick either.
        close = float(
            getattr(flags, "E14_WING_SCORE_WEIGHT_BREACH",
                    getattr(flags, "E14_WING_SCORE_WEIGHT_CLOSE", 0.25))
        )
        return cls(
            close=close,
            touch=float(getattr(flags, "E14_WING_SCORE_WEIGHT_TOUCH", 0.20)),
            mae=float(getattr(flags, "E14_WING_SCORE_WEIGHT_MAE", 0.25)),
            theta=float(getattr(flags, "E14_WING_SCORE_WEIGHT_THETA", 0.15)),
            credit=float(getattr(flags, "E14_WING_SCORE_WEIGHT_CREDIT", 0.15)),
            max_tolerable_mae_pct=float(getattr(flags, "E14_WING_MAX_TOLERABLE_MAE_PCT", 80.0)),
            target_theta_pct=float(getattr(flags, "E14_WING_TARGET_THETA_PCT", 60.0)),
            target_roc_pct=float(getattr(flags, "E14_WING_TARGET_ROC_PCT", 12.0)),
        )


DEFAULT_WEIGHTS = WingConsoleWeights()


@dataclass
class PlacementScore:
    em_mult:             float = 0.0
    wing_pts:            float = 0.0

    short_put_strike:    Optional[float] = None
    short_call_strike:   Optional[float] = None
    long_put_strike:     Optional[float] = None
    long_call_strike:    Optional[float] = None

    breach_close_prob:     float = 0.0
    touch_intraweek_prob:  float = 0.0
    outside_wings_prob:    float = 0.0
    mae_p95_vs_wing:       float = 0.0

    theta_capture_pct:   float = 0.0
    credit_est:          float = 0.0
    credit_dollars:      float = 0.0
    max_loss:            float = 0.0
    roc_est:             float = 0.0

    composite_score:     float = 0.0
    composite_breakdown: Dict[str, float] = field(default_factory=dict)

    n_analogues:         int = 0
    n_mc_sims:           int = 0
    confidence:          str = "low"
    mae_source:          str = ""
    notes:               List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WingConsolePayload:
    entry_date:       str = ""
    expiry_date:      str = ""
    as_of_date:       str = ""
    spot:             Optional[float] = None
    em_pct:           Optional[float] = None

    regime_label:     Optional[str] = None
    regime_bucket:    Optional[str] = None
    regime_mi_v2:     Optional[Dict[str, Any]] = None
    macro_bucket:     Optional[str] = None

    n_analogues:      int = 0
    placements:       List[PlacementScore] = field(default_factory=list)
    grid:             Dict[str, Any] = field(default_factory=dict)
    weights_used:     Dict[str, float] = field(default_factory=dict)
    mae:              Dict[str, Any] = field(default_factory=dict)
    mc:               Dict[str, Any] = field(default_factory=dict)
    warnings:         List[str] = field(default_factory=list)
    generated_at:     str = ""
    cache_key:        str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["placements"] = [p.to_dict() for p in self.placements]
        return d


# ---------------------------------------------------------------------------
# Small utils
# ---------------------------------------------------------------------------


def _as_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _clamp(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _parse_grid_floats(raw: Any, fallback: Sequence[float]) -> List[float]:
    if isinstance(raw, (list, tuple)):
        vals = [_as_float(x) for x in raw]
        return [v for v in vals if v is not None and v > 0] or list(fallback)
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        vals = [_as_float(p) for p in parts]
        vals = [v for v in vals if v is not None and v > 0]
        return vals or list(fallback)
    return list(fallback)


# ---------------------------------------------------------------------------
# Theta + credit helpers (shared shape with E2)
# ---------------------------------------------------------------------------


def _estimate_theta_capture_pct(
    *,
    hold_days:          int,
    dte_calendar_days:  int,
) -> float:
    """BS-ish approximation: fraction of entry credit retained at
    planned exit.

    ``theta_capture_pct = 1 - sqrt(remaining / total_dte)``

    Held to expiry => 100%. Half-dte hold => ~29%.
    """
    if dte_calendar_days <= 0:
        return 0.0
    hd = max(0, min(int(dte_calendar_days), int(hold_days)))
    remaining = max(0, int(dte_calendar_days) - hd)
    frac = float(remaining) / float(dte_calendar_days)
    return float(_clamp((1.0 - math.sqrt(frac)) * 100.0, 0.0, 100.0))


def _estimate_credit_points(
    *,
    em_multiple:       float,
    wing_pts:          float,
    implied_move_pts:  float,
) -> float:
    """Normal-IV closed-form proxy for entry credit in points.

    Credit ~ ``2 * IM * phi(em_multiple)`` with phi the standard
    normal density. Clamped at ``0.9 * wing_pts`` so it never
    exceeds theoretical max credit for the spread.
    """
    if em_multiple <= 0 or wing_pts <= 0 or implied_move_pts <= 0:
        return 0.0
    try:
        phi = math.exp(-0.5 * em_multiple * em_multiple) / math.sqrt(2.0 * math.pi)
        credit = 2.0 * implied_move_pts * phi
        return float(_clamp(credit, 0.0, wing_pts * 0.9))
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Historical breach-close fallback (when MC pool is too thin)
# ---------------------------------------------------------------------------


def _historical_breach_prob(
    events: Sequence[Dict[str, Any]],
    *,
    em_multiple: float,
    em_pct_today: float,
) -> Tuple[float, int]:
    """Fraction of historical analogues whose close-to-close move
    exceeded ``em_multiple * em_pct_today``."""
    if em_multiple <= 0 or em_pct_today <= 0 or not events:
        return 0.0, 0
    thresh = em_multiple * em_pct_today
    n = 0
    breaches = 0
    for e in events:
        m = _as_float(
            e.get("signed_move_pct") or e.get("signedMovePct")
            or e.get("returnPct")
        )
        if m is None:
            continue
        n += 1
        if abs(m) > thresh:
            breaches += 1
    if n == 0:
        return 0.0, 0
    return breaches / n, n


# ---------------------------------------------------------------------------
# Per-placement scorer
# ---------------------------------------------------------------------------


def _score_one(
    *,
    em_mult:           float,
    wing_pts:          float,
    spot:              float,
    em_pct:            float,
    hold_days:         int,
    dte_calendar_days: int,
    mae_p95_pct:       float,
    mc_placement:      Optional[MCPlacementResult],
    historical_events: Sequence[Dict[str, Any]],
    weights:           WingConsoleWeights,
) -> PlacementScore:
    short_dist_pts = (float(em_mult) * float(em_pct) / 100.0) * float(spot)
    short_put = float(spot) - short_dist_pts
    short_call = float(spot) + short_dist_pts
    long_put = short_put - float(wing_pts)
    long_call = short_call + float(wing_pts)
    max_loss = float(wing_pts)

    if mc_placement is not None:
        breach_close = float(mc_placement.breach_close_prob)
        touch_intraweek = float(mc_placement.touch_intraweek_prob)
        outside_wings = float(mc_placement.outside_wings_prob)
        n_mc_sims = 0
    else:
        hb, _n = _historical_breach_prob(
            historical_events, em_multiple=em_mult, em_pct_today=em_pct,
        )
        breach_close = hb
        touch_intraweek = hb
        outside_wings = max(0.0, hb - 0.05)
        n_mc_sims = 0

    mae_vs_wing = mae_p95_vs_wing_ratio(
        mae_p95_pct=float(mae_p95_pct),
        em_multiple=float(em_mult),
        implied_move_pct=float(em_pct),
        wing_width_pts=float(wing_pts),
        spot=float(spot),
    )

    theta_pct = _estimate_theta_capture_pct(
        hold_days=hold_days, dte_calendar_days=dte_calendar_days,
    )

    implied_move_pts = float(spot) * float(em_pct) / 100.0
    credit_pts = _estimate_credit_points(
        em_multiple=em_mult, wing_pts=wing_pts, implied_move_pts=implied_move_pts,
    )
    credit_dollars = credit_pts * 100.0
    roc = 0.0
    denom = max_loss - credit_pts
    if credit_pts > 0 and denom > 0:
        roc = (credit_pts / denom) * 100.0

    parts = {
        "breach": weights.close  * (1.0 - breach_close),
        "touch":  weights.touch  * (1.0 - touch_intraweek),
        "mae":    weights.mae    * (1.0 - _clamp(mae_vs_wing * 100.0 / max(1.0, weights.max_tolerable_mae_pct), 0.0, 1.0)),
        "theta":  weights.theta  * _clamp(theta_pct / max(1.0, weights.target_theta_pct), 0.0, 1.0),
        "credit": weights.credit * _clamp(roc / max(0.01, weights.target_roc_pct), 0.0, 1.0),
    }
    weight_total = sum(abs(w) for w in [
        weights.close, weights.touch, weights.mae, weights.theta, weights.credit,
    ]) or 1.0
    composite = 100.0 * sum(parts.values()) / weight_total

    if mc_placement is not None:
        confidence = "high"
    elif len(historical_events) >= 60:
        confidence = "med"
    else:
        confidence = "low"

    return PlacementScore(
        em_mult=round(float(em_mult), 4),
        wing_pts=round(float(wing_pts), 3),
        short_put_strike=round(short_put, 2),
        short_call_strike=round(short_call, 2),
        long_put_strike=round(long_put, 2),
        long_call_strike=round(long_call, 2),
        breach_close_prob=round(breach_close, 4),
        touch_intraweek_prob=round(touch_intraweek, 4),
        outside_wings_prob=round(outside_wings, 4),
        mae_p95_vs_wing=round(mae_vs_wing, 3),
        theta_capture_pct=round(theta_pct, 2),
        credit_est=round(credit_pts, 4),
        credit_dollars=round(credit_dollars, 2),
        max_loss=round(max_loss, 2),
        roc_est=round(roc, 2),
        composite_score=round(composite, 2),
        composite_breakdown={k: round(v, 4) for k, v in parts.items()},
        n_analogues=int(len(historical_events)),
        n_mc_sims=int(n_mc_sims),
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_placements(
    *,
    spot:              float,
    em_pct:            float,
    hold_days:         int,
    dte_calendar_days: int,
    historical_events: Sequence[Dict[str, Any]],
    mae:               Optional[MAEDistribution] = None,
    mc_result:         Optional[MCResult] = None,
    em_mults:          Optional[Sequence[float]] = None,
    wing_pts:          Optional[Sequence[float]] = None,
    weights:           Optional[WingConsoleWeights] = None,
) -> List[PlacementScore]:
    """Score the full grid and return placements ranked by composite."""
    if spot <= 0 or em_pct <= 0:
        return []

    w = weights or DEFAULT_WEIGHTS
    em_list = list(em_mults) if em_mults else [1.0, 1.25, 1.5, 2.0]
    wing_list = list(wing_pts) if wing_pts else [5.0, 10.0, 15.0]

    mc_by_pair: Dict[Tuple[float, float], MCPlacementResult] = {}
    n_mc_sims = 0
    if mc_result is not None and mc_result.placements:
        n_mc_sims = int(mc_result.n_sims)
        for pr in mc_result.placements:
            mc_by_pair[(round(float(pr.em_mult), 4), round(float(pr.wing_pts), 3))] = pr

    mae_p95 = float(mae.p95) if (mae and mae.n > 0) else 0.0

    placements: List[PlacementScore] = []
    for em in em_list:
        for wp in wing_list:
            key = (round(float(em), 4), round(float(wp), 3))
            mcp = mc_by_pair.get(key)
            ps = _score_one(
                em_mult=float(em), wing_pts=float(wp),
                spot=float(spot), em_pct=float(em_pct),
                hold_days=int(hold_days), dte_calendar_days=int(dte_calendar_days),
                mae_p95_pct=mae_p95, mc_placement=mcp,
                historical_events=historical_events, weights=w,
            )
            if mcp is not None:
                ps.n_mc_sims = int(n_mc_sims)
            placements.append(ps)

    placements.sort(
        key=lambda p: (-p.composite_score, p.breach_close_prob, p.touch_intraweek_prob)
    )
    return placements


def score_single_placement(
    *,
    context:          ScoringContext,
    em_mult:          float,
    wing_pts:         float,
    weights_override: Optional[WingConsoleWeights] = None,
) -> PlacementScore:
    """Exact-slider scoring against a cached :class:`ScoringContext`."""
    w = weights_override or DEFAULT_WEIGHTS

    # Re-run MC for just this placement against the cached analogue pool.
    mc = run_forward_mc(
        ticker="SPX",
        as_of_date=context.as_of_date,
        spot=context.spot,
        em_pct=context.em_pct,
        hold_days=context.hold_days,
        analogue_windows=context.analogue_pool,
        closes_by_date=context.closes_by_date,
        placements=[(float(em_mult), float(wing_pts))],
        n_sims=5000,
        min_pool=20,
        want_regime_bucket=context.regime_bucket,
        want_macro_bucket=context.macro_bucket,
        flags_fp=context.flags_fp,
    )
    mcp = mc.placements[0] if mc.placements else None
    mae_dict = context.mae_dist or {}
    mae_obj: Optional[MAEDistribution] = None
    if mae_dict and int(mae_dict.get("n") or 0) > 0:
        mae_obj = MAEDistribution(
            n=int(mae_dict.get("n") or 0),
            p50=float(mae_dict.get("p50") or 0.0),
            p75=float(mae_dict.get("p75") or 0.0),
            p90=float(mae_dict.get("p90") or 0.0),
            p95=float(mae_dict.get("p95") or 0.0),
            max=float(mae_dict.get("max") or 0.0),
            source=str(mae_dict.get("source") or "daily_ohlc"),
        )
    mae_p95 = float(mae_obj.p95) if (mae_obj and mae_obj.n > 0) else 0.0

    return _score_one(
        em_mult=float(em_mult), wing_pts=float(wing_pts),
        spot=float(context.spot), em_pct=float(context.em_pct),
        hold_days=int(context.hold_days),
        dte_calendar_days=int(context.hold_days),
        mae_p95_pct=mae_p95, mc_placement=mcp,
        historical_events=context.analogue_pool, weights=w,
    )


# ---------------------------------------------------------------------------
# High-level builder
# ---------------------------------------------------------------------------


def build_wing_console(
    *,
    entry_date:    str,
    expiry_date:   str,
    as_of_date:    str,
    spot:          float,
    em_pct:        float,
    hold_days:     int,
    dte_calendar_days: int,
    analogue_pool: Sequence[Dict[str, Any]],
    closes_by_date: Dict[str, float],
    ohlc_by_date:  Dict[str, Any],
    regime_label:  Optional[str] = None,
    regime_bucket: Optional[str] = None,
    regime_mi_v2:  Optional[Dict[str, Any]] = None,
    macro_bucket:  Optional[str] = None,
    weights:       Optional[WingConsoleWeights] = None,
    em_mults:      Optional[Sequence[float]] = None,
    wing_pts_grid: Optional[Sequence[float]] = None,
    flags:         Optional[FeatureFlags] = None,
) -> Tuple[WingConsolePayload, MAEDistribution, MCResult]:
    """High-level builder — feeds the router + Command Deck UI.

    Returns a ``(WingConsolePayload, MAEDistribution, MCResult)``
    tuple so the router can pass the MAE + MC readings through to
    the frontend's drilldown cards without re-serialising.
    """
    from backend.engine14.mae_proxy import compute_mae_distribution

    flags = flags or get_flags()
    w = weights or WingConsoleWeights.from_flags(flags)

    if em_mults is None:
        em_mults = _parse_grid_floats(
            getattr(flags, "E14_WING_EM_MULTS", None),
            fallback=[1.0, 1.25, 1.5, 2.0],
        )
    if wing_pts_grid is None:
        wing_pts_grid = _parse_grid_floats(
            getattr(flags, "E14_WING_PTS", None),
            fallback=[5.0, 10.0, 15.0],
        )

    warnings: List[str] = []
    if spot is None or spot <= 0:
        warnings.append("no spot price available; placements suppressed.")
    if em_pct is None or em_pct <= 0:
        warnings.append("no expected-move % available; placements suppressed.")

    # MAE pool from analogue windows.
    mae_windows = []
    for w_row in analogue_pool:
        ed = w_row.get("entry_date")
        xp = w_row.get("expiry_date")
        ec = closes_by_date.get(ed) if ed else None
        if ed and xp and ec:
            mae_windows.append({
                "entry_date": ed, "expiry_date": xp, "entry_close": ec,
            })
    mae_dist = compute_mae_distribution(
        windows=mae_windows, bars_by_date=ohlc_by_date,
    )

    # Forward MC across the full (em x wing) grid.
    placement_pairs = [(float(em), float(wp)) for em in em_mults for wp in wing_pts_grid]
    mc_result: Optional[MCResult] = None
    if (
        bool(getattr(flags, "ENABLE_E14_MC", True))
        and spot and em_pct and analogue_pool
    ):
        mc_result = run_forward_mc(
            ticker="SPX", as_of_date=as_of_date,
            spot=spot, em_pct=em_pct, hold_days=hold_days,
            analogue_windows=analogue_pool,
            closes_by_date=closes_by_date,
            placements=placement_pairs,
            n_sims=int(getattr(flags, "E14_MC_N_SIMS", 5000)),
            min_pool=int(getattr(flags, "E14_MC_MIN_POOL", 20)),
            seed=int(getattr(flags, "E14_MC_SEED", 1337)),
            condition_on_regime=bool(getattr(flags, "E14_MC_CONDITION_ON_REGIME", True)),
            condition_on_macro=bool(getattr(flags, "E14_MC_CONDITION_ON_MACRO", True)),
            want_regime_bucket=(str(regime_bucket).upper() if regime_bucket else None),
            want_macro_bucket=(str(macro_bucket).upper() if macro_bucket else None),
            gbm_fallback=bool(getattr(flags, "E14_MC_GBM_FALLBACK", True)),
            flags_fp=tuple(flags.cache_fingerprint() or ()) if hasattr(flags, "cache_fingerprint") else (),
        )

    placements: List[PlacementScore] = []
    if (spot and spot > 0) and (em_pct and em_pct > 0):
        placements = score_placements(
            spot=spot, em_pct=em_pct,
            hold_days=hold_days, dte_calendar_days=dte_calendar_days,
            historical_events=list(analogue_pool),
            mae=mae_dist, mc_result=mc_result,
            em_mults=em_mults, wing_pts=wing_pts_grid,
            weights=w,
        )

        # Publish a ScoringContext for the slider endpoint.
        store_scoring_context(ScoringContext(
            entry_date=str(entry_date)[:10],
            expiry_date=str(expiry_date)[:10],
            as_of_date=str(as_of_date)[:10],
            spot=float(spot),
            em_pct=float(em_pct),
            hold_days=int(hold_days),
            analogue_pool=list(analogue_pool),
            closes_by_date=dict(closes_by_date),
            mae_dist=(mae_dist.to_dict() if mae_dist else None),
            mc_result=(mc_result.to_dict() if mc_result else None),
            regime_bucket=(str(regime_bucket).upper() if regime_bucket else None),
            macro_bucket=(str(macro_bucket).upper() if macro_bucket else None),
            regime_mi_v2=regime_mi_v2 if isinstance(regime_mi_v2, dict) else None,
            weights=w.as_dict(),
            flags_fp=tuple(flags.cache_fingerprint() or ()) if hasattr(flags, "cache_fingerprint") else (),
        ))

    grid_sig = hashlib.sha256(
        json.dumps({"em": list(em_mults), "wp": list(wing_pts_grid)}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]

    import datetime as _dt
    payload = WingConsolePayload(
        entry_date=str(entry_date)[:10],
        expiry_date=str(expiry_date)[:10],
        as_of_date=str(as_of_date)[:10],
        spot=spot,
        em_pct=em_pct,
        regime_label=regime_label,
        regime_bucket=regime_bucket,
        regime_mi_v2=regime_mi_v2 if isinstance(regime_mi_v2, dict) else None,
        macro_bucket=macro_bucket,
        n_analogues=len(analogue_pool),
        placements=placements,
        grid={
            "em_mults":  list(em_mults),
            "wing_pts":  list(wing_pts_grid),
            "grid_sig":  grid_sig,
        },
        weights_used=w.as_dict(),
        mae=(mae_dist.to_dict() if mae_dist else {}),
        mc=(mc_result.to_dict() if mc_result else {}),
        warnings=warnings,
        generated_at=_dt.datetime.now(_dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z",
        cache_key=grid_sig,
    )
    return payload, mae_dist, mc_result or MCResult()


__all__ = [
    "DEFAULT_WEIGHTS",
    "MAEDistribution",
    "MCPlacementResult",
    "MCResult",
    "PlacementScore",
    "ScoringContext",
    "WingConsolePayload",
    "WingConsoleWeights",
    "build_wing_console",
    "score_placements",
    "score_single_placement",
    "get_scoring_context",
    "store_scoring_context",
]
