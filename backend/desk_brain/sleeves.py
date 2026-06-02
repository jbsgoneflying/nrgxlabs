"""Sleeves + per-engine edge config for the Desk Brain meta-allocator.

A *sleeve* groups engines by their fundamental return driver so the
allocator can budget risk across uncorrelated bets rather than across
individual signals. Three sleeves:

- ``volatility`` (income): short-premium / vol-harvesting engines.
- ``directional`` (growth): trend / mean-reversion / relative-value engines.
- ``overlay`` (risk): regime + stress engines that *modulate* sizing rather
  than trade on their own. The overlay sleeve weight is treated as a
  cash/hedge reserve by the allocator — heat that is intentionally NOT
  deployed into trades.

Engine numbers here are the **UI** numbers (see ``ENGINE_REGISTRY`` in
``config.py``), which is the desk-facing standard.

Edge priors below are seeded from the desk's own walk-forward + portfolio
pressure tests (notably the E4/E5 study: Ichimoku workable, Red Dog thin).
When live paper-trade performance exists in ``backtest_engine`` it blends
in on top of the prior so the allocator tracks realised edge over time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Sleeve definitions
# ---------------------------------------------------------------------------

SLEEVE_VOLATILITY = "volatility"
SLEEVE_DIRECTIONAL = "directional"
SLEEVE_THEMATIC = "thematic"
SLEEVE_OVERLAY = "overlay"


@dataclass(frozen=True)
class SleeveDef:
    sleeve_id: str
    name: str
    kind: str          # "income" | "growth" | "risk"
    deployable: bool   # False => weight is reserved as cash/hedge, not traded
    base_weight: float # neutral-regime share of the heat budget (0..1)
    blurb: str


SLEEVES: Dict[str, SleeveDef] = {
    SLEEVE_VOLATILITY: SleeveDef(
        sleeve_id=SLEEVE_VOLATILITY,
        name="Volatility / Premium",
        kind="income",
        deployable=True,
        base_weight=0.35,
        blurb="Short-premium income: earnings IC, SPX IC, VIX fade.",
    ),
    SLEEVE_DIRECTIONAL: SleeveDef(
        sleeve_id=SLEEVE_DIRECTIONAL,
        name="Directional / Growth",
        kind="growth",
        deployable=True,
        base_weight=0.35,
        blurb="Trend, mean-reversion, post-event and relative-value bets.",
    ),
    SLEEVE_THEMATIC: SleeveDef(
        sleeve_id=SLEEVE_THEMATIC,
        name="Thematic / Structural",
        kind="growth",
        deployable=True,
        base_weight=0.10,
        blurb="Slower-moving structural theses (AI-capex reality); sized small until proven.",
    ),
    SLEEVE_OVERLAY: SleeveDef(
        sleeve_id=SLEEVE_OVERLAY,
        name="Risk Overlay / Reserve",
        kind="risk",
        deployable=False,
        base_weight=0.20,
        blurb="Regime + credit-stress overlays; held as cash/hedge reserve.",
    ),
}

# UI engine number -> sleeve id.
ENGINE_SLEEVE_MAP: Dict[int, str] = {
    1:  SLEEVE_VOLATILITY,    # Earnings Hold Risk / IC
    2:  SLEEVE_VOLATILITY,    # SPX Iron Condor
    12: SLEEVE_VOLATILITY,    # VIX Spike Fade
    15: SLEEVE_VOLATILITY,    # Earnings IC Scenario
    4:  SLEEVE_DIRECTIONAL,   # Mean-Reversion (Red Dog)
    5:  SLEEVE_DIRECTIONAL,   # Trend-Continuation (Ichimoku)
    6:  SLEEVE_DIRECTIONAL,   # Thematic Pairs
    7:  SLEEVE_DIRECTIONAL,   # Post-Event Extension
    17: SLEEVE_THEMATIC,      # AI Capex Reality Engine
    3:  SLEEVE_OVERLAY,       # Global Lead-Lag Regime
    8:  SLEEVE_OVERLAY,       # Credit Stress Drift
    11: SLEEVE_OVERLAY,       # Macro / Headline Risk
}


def sleeve_for_engine(engine_id: int) -> str:
    """Return the sleeve id for a UI engine number (defaults to directional)."""
    return ENGINE_SLEEVE_MAP.get(int(engine_id), SLEEVE_DIRECTIONAL)


# ---------------------------------------------------------------------------
# Per-engine measured edge
# ---------------------------------------------------------------------------


@dataclass
class EngineEdge:
    """Measured edge for one engine, normalised for the allocator.

    ``edge_score`` (0..1) is the single knob the allocator uses to weight
    candidates within a sleeve. It blends expectancy (in R) and a Sharpe
    proxy so a high-hit-rate income engine and a high-payoff directional
    engine are comparable.
    """
    engine_id: int
    engine_name: str
    sleeve: str
    expectancy_r: float        # average return in units of risk (R)
    win_rate: float            # 0..1
    sharpe: float              # annualised Sharpe proxy
    sample: int                # number of trades behind the estimate
    source: str                # "prior" | "blended:live"
    edge_score: float = 0.0    # 0..1 composite, filled by normaliser

    def to_dict(self) -> dict:
        return {
            "engineId": self.engine_id,
            "engineName": self.engine_name,
            "sleeve": self.sleeve,
            "expectancyR": round(self.expectancy_r, 3),
            "winRate": round(self.win_rate, 3),
            "sharpe": round(self.sharpe, 2),
            "sample": self.sample,
            "source": self.source,
            "edgeScore": round(self.edge_score, 3),
        }


# Baseline priors keyed by UI engine number. expectancy_r / win_rate / sharpe
# are conservative reads from the desk's walk-forward + portfolio tests.
_EDGE_PRIORS: Dict[int, Dict[str, Any]] = {
    1:  {"name": "Earnings Hold Risk (IC)", "expectancy_r": 0.16, "win_rate": 0.70, "sharpe": 1.00, "sample": 120},
    2:  {"name": "SPX Iron Condor",         "expectancy_r": 0.13, "win_rate": 0.75, "sharpe": 1.15, "sample": 200},
    12: {"name": "VIX Spike Fade",          "expectancy_r": 0.22, "win_rate": 0.64, "sharpe": 0.95, "sample": 60},
    15: {"name": "Earnings IC Scenario",    "expectancy_r": 0.15, "win_rate": 0.69, "sharpe": 0.95, "sample": 90},
    4:  {"name": "Mean-Reversion (Red Dog)","expectancy_r": 0.05, "win_rate": 0.42, "sharpe": 0.25, "sample": 150},
    5:  {"name": "Trend (Ichimoku)",        "expectancy_r": 0.26, "win_rate": 0.46, "sharpe": 0.85, "sample": 140},
    6:  {"name": "Thematic Pairs",          "expectancy_r": 0.14, "win_rate": 0.55, "sharpe": 0.60, "sample": 80},
    7:  {"name": "Post-Event Extension",    "expectancy_r": 0.11, "win_rate": 0.52, "sharpe": 0.50, "sample": 70},
    # Thin prior: unproven, so the allocator sizes it tiny until paper history
    # builds (sample shrinkage drives edge_score toward the floor).
    17: {"name": "AI Capex Reality Engine", "expectancy_r": 0.08, "win_rate": 0.50, "sharpe": 0.40, "sample": 15},
}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _compute_edge_score(expectancy_r: float, sharpe: float, sample: int) -> float:
    """Map (expectancy, sharpe, sample) into a single 0..1 edge score.

    - Expectancy in R is the primary driver (0R -> 0, ~0.4R -> ~1.0).
    - Sharpe adds a consistency premium.
    - Thin samples are shrunk toward the floor so unproven edges size small.
    """
    exp_component = _clamp(expectancy_r / 0.40, 0.0, 1.0)
    sharpe_component = _clamp(sharpe / 1.50, 0.0, 1.0)
    raw = 0.65 * exp_component + 0.35 * sharpe_component
    # Sample shrinkage: full credit at >=100 trades, linear below.
    confidence = _clamp(sample / 100.0, 0.20, 1.0)
    return round(_clamp(raw * confidence, 0.0, 1.0), 4)


def _live_perf(engine_id: int, store: Any) -> Optional[Dict[str, float]]:
    """Pull live paper-trade performance for an engine, if any exists.

    Returns ``{"expectancy_r", "win_rate", "sharpe", "sample"}`` or None.
    Defensive — never raises; returns None on any failure or thin sample.
    """
    if store is None:
        return None
    try:
        from backend.backtest_engine import compute_performance, get_paper_trades

        trades = get_paper_trades(store=store, engine_id=int(engine_id))
        perf = compute_performance(trades, int(engine_id), "")
        if perf.closed_trades < 10:
            return None
        win_rate = _clamp(perf.win_rate / 100.0, 0.0, 1.0)
        # Approximate expectancy in R from win-rate and avg win/loss magnitude.
        avg_win = max(0.0, float(perf.max_win)) or 1.0
        avg_loss = abs(float(perf.max_loss)) or 1.0
        rr = _clamp(avg_win / avg_loss, 0.1, 5.0)
        expectancy_r = win_rate * rr - (1.0 - win_rate)
        sharpe = float(perf.sharpe_estimate) if perf.sharpe_estimate is not None else 0.0
        return {
            "expectancy_r": expectancy_r,
            "win_rate": win_rate,
            "sharpe": _clamp(sharpe, -2.0, 4.0),
            "sample": int(perf.closed_trades),
        }
    except Exception:
        return None


def get_engine_edge(engine_id: int, *, store: Any = None) -> EngineEdge:
    """Return the measured edge for an engine (prior, blended with live)."""
    engine_id = int(engine_id)
    prior = _EDGE_PRIORS.get(engine_id)
    sleeve = sleeve_for_engine(engine_id)
    if prior is None:
        # Unknown / overlay engine — no standalone tradable edge.
        return EngineEdge(
            engine_id=engine_id, engine_name="", sleeve=sleeve,
            expectancy_r=0.0, win_rate=0.0, sharpe=0.0, sample=0,
            source="prior", edge_score=0.0,
        )

    exp_r = float(prior["expectancy_r"])
    win = float(prior["win_rate"])
    sharpe = float(prior["sharpe"])
    sample = int(prior["sample"])
    source = "prior"

    live = _live_perf(engine_id, store)
    if live is not None:
        # Bayesian-ish blend: weight live by its sample share.
        live_n = live["sample"]
        w_live = _clamp(live_n / (live_n + 60.0), 0.0, 0.75)
        exp_r = (1 - w_live) * exp_r + w_live * live["expectancy_r"]
        win = (1 - w_live) * win + w_live * live["win_rate"]
        sharpe = (1 - w_live) * sharpe + w_live * live["sharpe"]
        sample = sample + live_n
        source = "blended:live"

    edge = EngineEdge(
        engine_id=engine_id,
        engine_name=str(prior["name"]),
        sleeve=sleeve,
        expectancy_r=exp_r,
        win_rate=win,
        sharpe=sharpe,
        sample=sample,
        source=source,
    )
    edge.edge_score = _compute_edge_score(exp_r, sharpe, sample)
    return edge


def all_engine_edges(*, store: Any = None) -> Dict[int, EngineEdge]:
    """Return edges for every engine that has a tradable prior."""
    return {eid: get_engine_edge(eid, store=store) for eid in _EDGE_PRIORS}


# ---------------------------------------------------------------------------
# Regime -> sleeve weight tilt
# ---------------------------------------------------------------------------

# Sleeve weights by canonical regime label. Each row sums to ~1.0. The
# overlay weight is the intended cash/hedge reserve.
_REGIME_WEIGHTS: Dict[str, Dict[str, float]] = {
    "risk-on":       {SLEEVE_VOLATILITY: 0.40, SLEEVE_DIRECTIONAL: 0.40, SLEEVE_THEMATIC: 0.12, SLEEVE_OVERLAY: 0.08},
    "transitional":  {SLEEVE_VOLATILITY: 0.38, SLEEVE_DIRECTIONAL: 0.32, SLEEVE_THEMATIC: 0.08, SLEEVE_OVERLAY: 0.22},
    "risk-off":      {SLEEVE_VOLATILITY: 0.28, SLEEVE_DIRECTIONAL: 0.28, SLEEVE_THEMATIC: 0.06, SLEEVE_OVERLAY: 0.38},
    "stressed":      {SLEEVE_VOLATILITY: 0.15, SLEEVE_DIRECTIONAL: 0.22, SLEEVE_THEMATIC: 0.03, SLEEVE_OVERLAY: 0.60},
}


def regime_sleeve_weights(regime_label: Optional[str]) -> Dict[str, float]:
    """Return base sleeve weights for a regime label (defaults to neutral)."""
    key = str(regime_label or "").strip().lower()
    weights = _REGIME_WEIGHTS.get(key)
    if weights is None:
        weights = {sid: s.base_weight for sid, s in SLEEVES.items()}
    return dict(weights)


def sleeve_list() -> List[dict]:
    """Serialisable sleeve catalogue for the API / UI."""
    return [
        {
            "id": s.sleeve_id,
            "name": s.name,
            "kind": s.kind,
            "deployable": s.deployable,
            "baseWeight": s.base_weight,
            "blurb": s.blurb,
        }
        for s in SLEEVES.values()
    ]
