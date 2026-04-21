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
from typing import Any, Dict, List, Optional, Tuple

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


# Keywords that signal elevated pre-earnings guidance risk. Scanned against
# Benzinga news headlines in the (earnings_date - 10d .. earnings_date) window.
# Ranked roughly by severity: high-severity hits are rare and trigger the
# largest bump; medium-severity hits accumulate additively.
_HIGH_SEVERITY_KEYWORDS = (
    "sec investigation", "subpoena", "fraud", "accounting irregular",
    "restate", "restatement", "class action", "material weakness",
    "going concern", "bankruptcy", "chapter 11",
    "ceo resign", "ceo steps down", "cfo resign", "cfo steps down",
    "ceo replaced", "cfo replaced", "auditor dismiss",
)
_MEDIUM_SEVERITY_KEYWORDS = (
    "downgrade", "cut price target", "guidance cut", "lowers guidance",
    "warns", "warning", "preannounce", "pre-announce", "profit warning",
    "revenue warning", "miss", "shortfall",
    "delay", "postpone", "pushed back",
    "regulatory probe", "antitrust", "doj inquiry", "ftc inquiry",
    "labor dispute", "strike", "recall", "safety concern",
    "short seller", "short report", "short-seller",
)
_LOW_SEVERITY_KEYWORDS = (
    "activist", "activist investor", "board shake", "ceo transition",
    "restructuring", "layoff", "layoffs",
    "analyst skeptical", "bear thesis",
)


def _scan_headlines_for_guidance_risk(headlines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Classify a list of Benzinga news rows into a guidance-risk score.

    Each hit contributes to a score in [0, 100]. Score is capped at 100
    even when multiple keywords fire on the same headline.
    """
    if not headlines:
        return {"score": 0.0, "hits": [], "n_scanned": 0}

    hits: List[Dict[str, Any]] = []
    score = 0.0
    for row in headlines:
        title = str((row or {}).get("title") or "").lower()
        teaser = str((row or {}).get("teaser") or "").lower()
        text = f"{title} {teaser}"
        if not text.strip():
            continue
        matched: List[Tuple[str, int]] = []
        for kw in _HIGH_SEVERITY_KEYWORDS:
            if kw in text:
                matched.append((kw, 35))
        for kw in _MEDIUM_SEVERITY_KEYWORDS:
            if kw in text:
                matched.append((kw, 15))
        for kw in _LOW_SEVERITY_KEYWORDS:
            if kw in text:
                matched.append((kw, 5))
        if matched:
            kw_total = sum(w for _, w in matched)
            hits.append({
                "title": str((row or {}).get("title") or "")[:140],
                "publishedAt": str((row or {}).get("created") or (row or {}).get("date") or ""),
                "keywords": [k for k, _ in matched][:3],
                "weight": int(kw_total),
            })
            score += float(kw_total)

    return {
        "score": round(float(min(100.0, score)), 2),
        "hits": hits[:8],                     # keep top 8 for tooltip
        "n_scanned": int(len(headlines)),
    }


def guidance_risk_modifier(
    engine1: Dict[str, Any],
    *,
    benzinga_client: Any = None,
    ticker: Optional[str] = None,
    earnings_date: Optional[str] = None,
    max_bump_pct: float = 15.0,
) -> Modifier:
    """Encode desk-level guidance / pre-release leak risk.

    v2 wiring:

    1. Scan Benzinga news headlines in the (earnings_date - 10d,
       earnings_date) window for a curated keyword set covering SEC
       investigations, guidance cuts, analyst downgrades, executive
       departures, and activist / short-seller chatter.
    2. Combine with Engine 1's ``eventRisk`` score when present (the
       legacy signal).
    3. Scale the tail multiplier and win-rate shift proportionally to
       the combined score, capped by ``max_bump_pct``
       (``GUIDANCE_RISK_MAX_BUMP_PCT``).

    When Benzinga is unavailable the modifier still reads the E1
    eventRisk block, preserving the previous behaviour. When both are
    absent, status is ``unavailable``.
    """
    headline_scan: Dict[str, Any] = {}
    if benzinga_client is not None and ticker and earnings_date:
        try:
            ed = dt.date.fromisoformat(str(earnings_date)[:10])
            d_from = (ed - dt.timedelta(days=10)).isoformat()
            d_to = ed.isoformat()
            resp = benzinga_client.news(
                tickers=str(ticker).upper(),
                date_from=d_from,
                date_to=d_to,
                display_output="headline",
                page_size=30,
            )
            rows = getattr(resp, "rows", None) or []
            headline_scan = _scan_headlines_for_guidance_risk(rows)
        except Exception as err:
            LOG.debug("guidance_risk: benzinga scan failed: %s", err)
            headline_scan = {"score": 0.0, "hits": [], "error": f"{type(err).__name__}"}

    if not engine1 and not headline_scan:
        return Modifier(
            name="guidanceRisk", status="unavailable",
            note="No E1 payload or Benzinga headlines available.",
        )

    er = (engine1 or {}).get("eventRisk") or {}
    e1_score: Optional[float] = None
    raw = er.get("score") or er.get("riskScore")
    try:
        if raw is not None:
            e1_score = float(raw)
    except (TypeError, ValueError):
        e1_score = None

    headline_score = float(headline_scan.get("score") or 0.0)
    # Combine: max of (E1 score, headline score). Keeps simple semantics
    # and avoids double-counting the same underlying risk.
    combined = max(float(e1_score or 0.0), headline_score)

    if combined <= 0.0:
        return Modifier(
            name="guidanceRisk", status="ok", severity="low",
            tail_multiplier=1.0, win_rate_shift_pct=0.0,
            note=(
                "No elevated guidance-risk signals detected"
                f"{' (Benzinga scanned ' + str(headline_scan.get('n_scanned') or 0) + ' headlines)' if headline_scan else ''}."
            ),
            details={
                "eventRisk": er, "headlineScan": headline_scan,
                "combinedScore": 0.0,
            },
        )

    s_norm = max(0.0, min(1.0, combined / 100.0))
    # Cap the tail bump at max_bump_pct expressed as a percent.
    max_bump = max(0.0, float(max_bump_pct)) / 100.0
    tail = float(1.0 + max_bump * s_norm)
    # Win-rate shift is half the tail-multiplier impact, capped at -5pp.
    wr = float(-5.0 * s_norm)
    severity = "elevated" if s_norm >= 0.5 else "moderate" if s_norm >= 0.2 else "low"

    drivers: List[str] = []
    if e1_score is not None:
        drivers.append(f"E1 eventRisk={e1_score:.0f}")
    if headline_score > 0:
        drivers.append(
            f"Benzinga headlines={headline_score:.0f} "
            f"({len(headline_scan.get('hits') or [])} hits)"
        )

    return Modifier(
        name="guidanceRisk",
        status="ok",
        severity=severity,
        tail_multiplier=float(min(1.0 + max_bump, tail)),
        win_rate_shift_pct=float(max(-5.0, wr)),
        note=(
            f"Combined guidance-risk score {combined:.0f}/100 "
            f"(sources: {', '.join(drivers) or 'none'}) → "
            f"tail ×{tail:.2f}, WR {wr:+.1f}pp."
        ),
        details={
            "eventRisk":     er,
            "headlineScan":  headline_scan,
            "combinedScore": round(float(combined), 2),
            "maxBumpPct":    float(max_bump_pct),
        },
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
    # v2: read flags so the guidance-risk wire can reach Benzinga + cap
    # the max bump at GUIDANCE_RISK_MAX_BUMP_PCT.
    try:
        from backend.config import get_flags
        _f = get_flags()
        _guidance_enabled = bool(getattr(_f, "ENGINE15_GUIDANCE_RISK_ENABLED", True))
        _max_bump = float(getattr(_f, "GUIDANCE_RISK_MAX_BUMP_PCT", 15.0))
    except Exception:
        _guidance_enabled = True
        _max_bump = 15.0

    guidance_bz = benzinga_client if _guidance_enabled else None
    ticker = str(getattr(request, "ticker", "") or "").upper() or None
    earnings_date = str(getattr(request, "earnings_date", "") or "") or None

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
        "guidanceRisk": guidance_risk_modifier(
            engine1,
            benzinga_client=guidance_bz,
            ticker=ticker,
            earnings_date=earnings_date,
            max_bump_pct=_max_bump,
        ),
    }
    return _combine_modifiers(mods)
