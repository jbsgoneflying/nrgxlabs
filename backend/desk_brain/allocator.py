"""Allocator — the deterministic core of the Desk Brain.

Pure functions, no I/O: given a normalised opportunity set, the regime, the
per-engine edge, and a risk config, produce a single ranked **target book**
with suggested size (in risk-% and dollars) plus a total-heat meter.

Design contract (so the LLM layer can never blow up the book):
- The math here is fully deterministic and unit-tested.
- The only LLM input is ``sleeve_tilt`` (multipliers per sleeve). It is
  **clamped server-side** to +/- ``tilt_max_pct`` before it can touch sizing.
- Hard caps — total heat, per-sleeve heat, max concurrent positions
  (total + per-sleeve) — are enforced last and cannot be exceeded.

Pipeline:
1. Regime sets base sleeve weights (volatility / directional / overlay).
2. Clamped LLM tilt nudges the weights; renormalise.
3. Deployable heat = total_heat x (volatility + directional weight).
4. Rank candidates within each deployable sleeve by edge x conviction.
5. Enforce per-sleeve + total concurrency caps (global score order).
6. Size proportional to score, clamped to per-trade risk; cross-sleeve
   ticker collisions get a correlation haircut.
7. Emit the target book + sleeve allocation + heat meter + conflicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.desk_brain import sleeves
from backend.desk_brain.aggregator import Opportunity


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskConfig:
    """Risk-budget knobs (mirrors the DESK_BRAIN_* flags in config.py)."""
    account_size: float = 25_000.0
    total_heat_pct: float = 6.0
    per_trade_risk_pct: float = 1.0
    max_concurrent_total: int = 12
    max_concurrent_per_sleeve: int = 5
    tilt_max_pct: float = 20.0

    @classmethod
    def from_flags(cls, flags: Any) -> "RiskConfig":
        return cls(
            account_size=float(getattr(flags, "DESK_BRAIN_ACCOUNT_SIZE", 25_000.0)),
            total_heat_pct=float(getattr(flags, "DESK_BRAIN_TOTAL_HEAT_PCT", 6.0)),
            per_trade_risk_pct=float(getattr(flags, "DESK_BRAIN_PER_TRADE_RISK_PCT", 1.0)),
            max_concurrent_total=int(getattr(flags, "DESK_BRAIN_MAX_CONCURRENT_TOTAL", 12)),
            max_concurrent_per_sleeve=int(getattr(flags, "DESK_BRAIN_MAX_CONCURRENT_PER_SLEEVE", 5)),
            tilt_max_pct=float(getattr(flags, "DESK_BRAIN_TILT_MAX_PCT", 20.0)),
        )


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass
class TargetPosition:
    rank: int
    engine_id: int
    engine_name: str
    sleeve: str
    ticker: str
    direction: str
    structure: str
    conviction: float
    edge_score: float
    score: float            # edge x conviction (0..1), pre-haircut
    risk_pct: float         # account risk allocated to this position
    risk_dollars: float
    desk_status: str
    verdict: str
    haircut: float = 0.0    # fraction trimmed for correlation/conflict
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "engineId": self.engine_id,
            "engineName": self.engine_name,
            "sleeve": self.sleeve,
            "ticker": self.ticker,
            "direction": self.direction,
            "structure": self.structure,
            "conviction": round(self.conviction, 1),
            "edgeScore": round(self.edge_score, 3),
            "score": round(self.score, 4),
            "riskPct": round(self.risk_pct, 3),
            "riskDollars": round(self.risk_dollars, 2),
            "deskStatus": self.desk_status,
            "verdict": self.verdict,
            "haircut": round(self.haircut, 3),
            "notes": list(self.notes),
        }


@dataclass
class SleeveAllocation:
    sleeve: str
    name: str
    deployable: bool
    base_weight: float
    tilted_weight: float
    heat_budget_pct: float    # share of total heat assigned to this sleeve
    deployed_pct: float       # heat actually sized into positions
    position_count: int

    def to_dict(self) -> dict:
        return {
            "sleeve": self.sleeve,
            "name": self.name,
            "deployable": self.deployable,
            "baseWeight": round(self.base_weight, 3),
            "tiltedWeight": round(self.tilted_weight, 3),
            "heatBudgetPct": round(self.heat_budget_pct, 3),
            "deployedPct": round(self.deployed_pct, 3),
            "positionCount": self.position_count,
        }


@dataclass
class TargetBook:
    as_of: str
    regime_label: str
    regime_confidence: float
    positions: List[TargetPosition] = field(default_factory=list)
    sleeves: List[SleeveAllocation] = field(default_factory=list)
    total_heat_budget_pct: float = 0.0
    total_deployed_pct: float = 0.0
    reserve_pct: float = 0.0
    conflicts: List[str] = field(default_factory=list)
    tilt_applied: Dict[str, float] = field(default_factory=dict)
    caps: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "asOf": self.as_of,
            "regimeLabel": self.regime_label,
            "regimeConfidence": round(self.regime_confidence, 3),
            "positions": [p.to_dict() for p in self.positions],
            "sleeves": [s.to_dict() for s in self.sleeves],
            "totalHeatBudgetPct": round(self.total_heat_budget_pct, 3),
            "totalDeployedPct": round(self.total_deployed_pct, 3),
            "reservePct": round(self.reserve_pct, 3),
            "conflicts": list(self.conflicts),
            "tiltApplied": {k: round(v, 3) for k, v in self.tilt_applied.items()},
            "caps": dict(self.caps),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def clamp_tilt(
    tilt: Optional[Dict[str, float]],
    *,
    tilt_max_pct: float,
) -> Dict[str, float]:
    """Clamp every LLM sleeve-tilt multiplier into [1-max, 1+max].

    Unknown sleeves are dropped; missing sleeves default to 1.0 (no tilt).
    This is the hard guardrail between the LLM and position sizing.
    """
    lo = 1.0 - (tilt_max_pct / 100.0)
    hi = 1.0 + (tilt_max_pct / 100.0)
    out: Dict[str, float] = {}
    for sid in sleeves.SLEEVES:
        mult = 1.0
        if tilt and sid in tilt:
            try:
                mult = float(tilt[sid])
            except (TypeError, ValueError):
                mult = 1.0
        out[sid] = round(_clamp(mult, lo, hi), 4)
    return out


def _tilted_weights(
    base: Dict[str, float],
    tilt: Dict[str, float],
) -> Dict[str, float]:
    """Apply clamped tilt multipliers and renormalise to sum 1."""
    raw = {sid: max(0.0, base.get(sid, 0.0) * tilt.get(sid, 1.0)) for sid in sleeves.SLEEVES}
    total = sum(raw.values()) or 1.0
    return {sid: raw[sid] / total for sid in raw}


def _sort_key(opp: Opportunity, score: float) -> tuple:
    """Deterministic ranking: score desc, then live-first, then ticker."""
    return (-round(score, 6), 0 if opp.is_live else 1, opp.ticker, opp.engine_id)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def allocate(
    opportunities: List[Opportunity],
    *,
    regime_label: str = "Transitional",
    regime_confidence: float = 0.0,
    config: Optional[RiskConfig] = None,
    edges: Optional[Dict[int, Any]] = None,
    sleeve_tilt: Optional[Dict[str, float]] = None,
    short_vol_haircut: float = 0.0,
    as_of: str = "",
) -> TargetBook:
    """Produce the deterministic target book.

    Args:
        opportunities: normalised candidate set (see aggregator).
        regime_label: canonical regime ("Risk-On"/"Transitional"/...).
        regime_confidence: 0..1 model confidence (telemetry only).
        config: risk knobs; defaults to ``RiskConfig()``.
        edges: optional engine_id -> EngineEdge override (else prior-only).
        sleeve_tilt: optional LLM multipliers; clamped here, never trusted raw.
        short_vol_haircut: extra 0..1 trim on the volatility sleeve when
            overlays flag stress (e.g. credit phase >= 3).
        as_of: timestamp echoed into the book.
    """
    cfg = config or RiskConfig()
    if edges is None:
        edges = sleeves.all_engine_edges()

    tilt = clamp_tilt(sleeve_tilt, tilt_max_pct=cfg.tilt_max_pct)
    base_weights = sleeves.regime_sleeve_weights(regime_label)
    tilted = _tilted_weights(base_weights, tilt)

    # Sleeve heat budgets (% of account). Overlay weight stays as reserve.
    sleeve_budget_pct: Dict[str, float] = {
        sid: cfg.total_heat_pct * tilted[sid] for sid in sleeves.SLEEVES
    }
    deployable_sleeves = [sid for sid, s in sleeves.SLEEVES.items() if s.deployable]

    # 1) Keep only actionable opportunities in deployable sleeves; score them.
    scored: Dict[str, List[tuple]] = {sid: [] for sid in deployable_sleeves}
    for opp in opportunities:
        if opp.sleeve not in scored:
            continue
        if not opp.is_actionable:
            continue
        edge = edges.get(opp.engine_id)
        edge_score = float(getattr(edge, "edge_score", 0.0)) if edge else 0.0
        score = edge_score * (_clamp(opp.conviction, 0.0, 100.0) / 100.0)
        if score <= 0:
            continue
        scored[opp.sleeve].append((opp, score, edge_score))

    # 2) Per-sleeve concurrency cap (rank within sleeve, truncate).
    selected: List[tuple] = []
    for sid in deployable_sleeves:
        ranked = sorted(scored[sid], key=lambda t: _sort_key(t[0], t[1]))
        capped = ranked[: cfg.max_concurrent_per_sleeve]
        selected.extend(capped)

    # 3) Total concurrency cap (global score order across sleeves).
    selected.sort(key=lambda t: _sort_key(t[0], t[1]))
    dropped_for_total = max(0, len(selected) - cfg.max_concurrent_total)
    selected = selected[: cfg.max_concurrent_total]

    # 4) Correlation/conflict handling — same ticker across sleeves/engines.
    conflicts: List[str] = []
    ticker_counts: Dict[str, int] = {}
    for opp, _s, _e in selected:
        ticker_counts[opp.ticker] = ticker_counts.get(opp.ticker, 0) + 1
    collided = {t for t, n in ticker_counts.items() if n > 1}
    for t in sorted(collided):
        conflicts.append(f"{t}: {ticker_counts[t]} engines flag the same name — size haircut applied")

    # 5) Size within each sleeve proportional to score, clamped per-trade.
    positions: List[TargetPosition] = []
    deployed_by_sleeve: Dict[str, float] = {sid: 0.0 for sid in deployable_sleeves}
    by_sleeve_selected: Dict[str, List[tuple]] = {sid: [] for sid in deployable_sleeves}
    for tup in selected:
        by_sleeve_selected[tup[0].sleeve].append(tup)

    for sid in deployable_sleeves:
        rows = by_sleeve_selected[sid]
        if not rows:
            continue
        budget = sleeve_budget_pct[sid]
        score_sum = sum(s for _o, s, _e in rows) or 1.0
        for opp, score, edge_score in rows:
            raw_pct = budget * (score / score_sum)
            risk_pct = _clamp(raw_pct, 0.0, cfg.per_trade_risk_pct)
            notes: List[str] = []
            haircut = 0.0
            if opp.ticker in collided:
                haircut = max(haircut, 0.25)
                notes.append("correlation haircut (duplicate name)")
            if sid == sleeves.SLEEVE_VOLATILITY and short_vol_haircut > 0:
                haircut = max(haircut, _clamp(short_vol_haircut, 0.0, 1.0))
                notes.append("short-vol haircut (overlay stress)")
            risk_pct = risk_pct * (1.0 - haircut)
            risk_dollars = cfg.account_size * (risk_pct / 100.0)
            deployed_by_sleeve[sid] += risk_pct
            positions.append(
                TargetPosition(
                    rank=0,
                    engine_id=opp.engine_id,
                    engine_name=opp.engine_name,
                    sleeve=sid,
                    ticker=opp.ticker,
                    direction=opp.direction,
                    structure=opp.structure,
                    conviction=opp.conviction,
                    edge_score=edge_score,
                    score=score,
                    risk_pct=risk_pct,
                    risk_dollars=risk_dollars,
                    desk_status=opp.desk_status,
                    verdict=opp.verdict,
                    haircut=haircut,
                    notes=notes,
                )
            )

    # Global rank by final risk then score.
    positions.sort(key=lambda p: (-round(p.risk_pct, 6), -round(p.score, 6), p.ticker))
    for i, p in enumerate(positions, start=1):
        p.rank = i

    # 6) Sleeve allocation rows + totals.
    sleeve_rows: List[SleeveAllocation] = []
    for sid, sdef in sleeves.SLEEVES.items():
        sleeve_rows.append(
            SleeveAllocation(
                sleeve=sid,
                name=sdef.name,
                deployable=sdef.deployable,
                base_weight=base_weights.get(sid, 0.0),
                tilted_weight=tilted.get(sid, 0.0),
                heat_budget_pct=sleeve_budget_pct.get(sid, 0.0),
                deployed_pct=deployed_by_sleeve.get(sid, 0.0),
                position_count=sum(1 for p in positions if p.sleeve == sid),
            )
        )

    total_deployed = round(sum(deployed_by_sleeve.values()), 4)
    reserve_pct = round(max(0.0, cfg.total_heat_pct - total_deployed), 4)

    notes: List[str] = []
    if dropped_for_total:
        notes.append(f"{dropped_for_total} lower-ranked candidate(s) dropped at the {cfg.max_concurrent_total}-position cap")
    if not positions:
        notes.append("No actionable opportunities cleared the gate — book is flat (full reserve)")

    return TargetBook(
        as_of=as_of,
        regime_label=regime_label,
        regime_confidence=regime_confidence,
        positions=positions,
        sleeves=sleeve_rows,
        total_heat_budget_pct=round(cfg.total_heat_pct, 4),
        total_deployed_pct=total_deployed,
        reserve_pct=reserve_pct,
        conflicts=conflicts,
        tilt_applied=tilt,
        caps={
            "maxConcurrentTotal": cfg.max_concurrent_total,
            "maxConcurrentPerSleeve": cfg.max_concurrent_per_sleeve,
            "perTradeRiskPct": cfg.per_trade_risk_pct,
            "droppedForTotalCap": dropped_for_total,
        },
        notes=notes,
    )
