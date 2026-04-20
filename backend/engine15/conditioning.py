"""Engine 15 — earnings-specialized conditioning modifiers.

Swaps Engine 14's macro-oriented modifier chain (credit stress, dealer
gamma, gap regime) for a single-name earnings equivalent:

* ``calendar`` — macro events in the ``[entry, plannedExit]`` window.
  Reused verbatim from :func:`backend.engine14.conditioning.compute_calendar_modifier`
  because FOMC/CPI still matters for single-name ICs (rate-day vol
  spikes hit everyone).
* ``vrpTilt`` — leans on E1's ``vrpAnalysis.vrpScore``. A high positive
  VRP (IV > realized) is a tailwind for IC premium sellers; a negative
  VRP is a headwind. We map score∈[-100,+100] → (tailMultiplier,
  winRateShiftPct) via a gentle linear transform capped at ±5pp WR.
* ``anncConfidence`` — encodes uncertainty about the earnings timing.
  A mismatched or ``UNK`` anncTod widens tails (+5-10%) and cuts WR by
  1-2pp because the replay pool is degraded.
* ``guidanceRisk`` — optional; reads Benzinga event risk signals when
  available. Currently a conservative shim (`severity="low"`, no tail
  multiplier) that leaves a hook for future wiring.

``apply_modifiers_to_distribution`` is re-exported from Engine 14 so
the simulator has a single call path.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from backend.engine14.conditioning import (
    Modifier,
    apply_modifiers_to_distribution,
    compute_calendar_modifier,
)

LOG = logging.getLogger("engine15.conditioning")


__all__ = [
    "apply_modifiers_to_distribution",
    "compute_earnings_conditioning",
    "vrp_tilt_modifier",
    "annc_confidence_modifier",
    "guidance_risk_modifier",
]


def vrp_tilt_modifier(engine1: Dict[str, Any]) -> Modifier:
    """VRP tailwind/headwind for earnings IC premium sellers."""
    if not engine1:
        return Modifier(
            name="vrpTilt", status="unavailable",
            note="Engine 1 payload missing; VRP tilt skipped.",
        )
    vrp = engine1.get("vrpAnalysis") or {}
    score = vrp.get("vrpScore")
    if score is None:
        return Modifier(
            name="vrpTilt", status="unavailable",
            note="E1 vrpAnalysis.vrpScore missing.",
        )
    try:
        s = float(score)
    except (TypeError, ValueError):
        return Modifier(
            name="vrpTilt", status="unavailable",
            note=f"Invalid VRP score: {score!r}.",
        )

    # Normalize to [-1, +1]; expect E1 reports 0..100 or -100..100.
    s_norm = max(-1.0, min(1.0, float(s) / 100.0))
    # Linear mapping — gentle by design:
    #   * score=+100 (IV≫RV)  → tail ×0.92, WR +4pp  (tailwind)
    #   * score=   0          → neutral
    #   * score=-100 (IV≪RV)  → tail ×1.10, WR -4pp  (headwind)
    tail = float(1.0 + (-0.08 * s_norm))
    wr = float(4.0 * s_norm)
    severity = (
        "elevated" if abs(s_norm) >= 0.5 else "moderate" if abs(s_norm) >= 0.2 else "low"
    )
    direction = "tailwind" if s_norm > 0 else "headwind" if s_norm < 0 else "neutral"
    return Modifier(
        name="vrpTilt",
        status="ok",
        severity=severity,
        tail_multiplier=float(max(0.80, min(1.25, tail))),
        win_rate_shift_pct=float(max(-5.0, min(5.0, wr))),
        note=(
            f"VRP score {s:+.0f}/100 → {direction}; "
            f"tail ×{tail:.2f}, WR {wr:+.1f}pp."
        ),
        details={"vrpScore": s, "ivElevation": vrp.get("ivElevation")},
    )


def annc_confidence_modifier(
    *,
    user_annc_tod: str,
    engine1: Dict[str, Any],
) -> Modifier:
    """Penalize the distribution when the earnings timing is uncertain."""
    utod = (user_annc_tod or "").upper()
    next_event = (engine1.get("nextEvent") or {}) if engine1 else {}
    e1_tod = str(next_event.get("anncTod") or next_event.get("timing") or "").upper()

    if utod == "UNK":
        return Modifier(
            name="anncConfidence",
            status="ok",
            severity="moderate",
            tail_multiplier=1.08,
            win_rate_shift_pct=-2.0,
            note="User timing=UNK: mixed BMO/AMC pool introduces ~2pp WR drag and 8% tail widening.",
            details={"userAnncTod": utod, "e1AnncTod": e1_tod},
        )
    if e1_tod and e1_tod != "UNK" and e1_tod != utod:
        return Modifier(
            name="anncConfidence",
            status="ok",
            severity="elevated",
            tail_multiplier=1.15,
            win_rate_shift_pct=-3.0,
            note=(
                f"User timing ({utod}) does not match Engine 1 ({e1_tod}). "
                "Replay fidelity reduced."
            ),
            details={"userAnncTod": utod, "e1AnncTod": e1_tod, "mismatch": True},
        )
    return Modifier(
        name="anncConfidence",
        status="ok",
        severity="none",
        tail_multiplier=1.0,
        win_rate_shift_pct=0.0,
        note=f"anncTod confirmed ({utod}); replay fidelity preserved.",
        details={"userAnncTod": utod, "e1AnncTod": e1_tod or utod},
    )


def guidance_risk_modifier(engine1: Dict[str, Any]) -> Modifier:
    """Encode desk-level guidance / pre-release leak risk.

    Currently a conservative shim: if E1 exposes an ``eventRisk`` block
    with a score, we pass it through; otherwise return ``unavailable``.
    This keeps a hook for future wiring without introducing fake
    tailwinds/headwinds.
    """
    if not engine1:
        return Modifier(
            name="guidanceRisk", status="unavailable",
            note="Engine 1 payload missing; guidance risk skipped.",
        )
    er = engine1.get("eventRisk") or {}
    if not er:
        return Modifier(
            name="guidanceRisk", status="unavailable",
            note="E1 eventRisk block absent.",
        )
    score = er.get("score") or er.get("riskScore")
    try:
        s = float(score) if score is not None else None
    except (TypeError, ValueError):
        s = None
    if s is None:
        return Modifier(
            name="guidanceRisk", status="ok", severity="low",
            tail_multiplier=1.0, win_rate_shift_pct=0.0,
            note="E1 eventRisk present but no numeric score; neutral treatment.",
            details=er,
        )
    s_norm = max(-1.0, min(1.0, s / 100.0))
    tail = float(1.0 + 0.08 * max(0.0, s_norm))  # only penalize, never reward
    wr = float(-2.0 * max(0.0, s_norm))
    severity = "elevated" if s_norm >= 0.5 else "moderate" if s_norm >= 0.2 else "low"
    return Modifier(
        name="guidanceRisk",
        status="ok",
        severity=severity,
        tail_multiplier=float(min(1.20, tail)),
        win_rate_shift_pct=float(max(-4.0, wr)),
        note=f"E1 eventRisk score {s:.0f} → tail ×{tail:.2f}, WR {wr:+.1f}pp.",
        details=er,
    )


def _combine_modifiers(modifiers: Dict[str, Modifier]) -> Dict[str, Any]:
    """Fold per-modifier contributions into net tail + WR adjustments.

    Tail multipliers multiply; WR shifts add. This mirrors the E14
    ``compute_conditioning`` aggregation.
    """
    net_tail = 1.0
    net_wr = 0.0
    notes: List[str] = []
    out: Dict[str, Any] = {}
    for key, m in modifiers.items():
        out[key] = m.to_dict()
        if m.status == "ok":
            net_tail *= float(m.tail_multiplier)
            net_wr += float(m.win_rate_shift_pct)
        if m.note:
            notes.append(f"{m.name}: {m.note}")
    out["netTailMultiplier"] = round(float(net_tail), 3)
    out["netWinRateShiftPct"] = round(float(net_wr), 2)
    out["notes"] = notes
    return out


def compute_earnings_conditioning(
    *,
    request: Any,
    engine1: Dict[str, Any],
    orats_client: Any = None,
    benzinga_client: Any = None,
    store: Any = None,
) -> Dict[str, Any]:
    """Build the earnings-specialized modifier bundle.

    ``request`` is the :class:`backend.engine15.simulator.EarningsIcRequest`
    (we only read a handful of fields off it, so we duck-type).
    """
    mods: Dict[str, Modifier] = {
        "calendar": compute_calendar_modifier(
            entry_date=str(getattr(request, "entry_date", "")),
            expiry_date=str(getattr(request, "planned_exit_date", "")),
            benzinga_client=benzinga_client,
        ),
        "vrpTilt": vrp_tilt_modifier(engine1),
        "anncConfidence": annc_confidence_modifier(
            user_annc_tod=str(getattr(request, "earnings_timing", "")),
            engine1=engine1,
        ),
        "guidanceRisk": guidance_risk_modifier(engine1),
    }
    return _combine_modifiers(mods)
