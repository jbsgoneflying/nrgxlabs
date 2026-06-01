"""Desk Brain — LLM meta-allocator routes.

Endpoints:
- ``GET  /api/desk-brain/book``    — current risk-budgeted target book (cached).
- ``POST /api/desk-brain/refresh`` — force a rebuild + LLM re-synthesis.

The heavy lifting is deterministic (``desk_brain.allocator``). The LLM is a
desk-head narrative + a bounded sleeve tilt that the allocator re-applies and
hard-clamps. We read engines' already-persisted Redis trackers + the cached
regime snapshot — no live ORATS/full scans — so the book builds in well under
a second.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

from backend.config import get_flags
from backend.desk_brain import aggregator, allocator, sleeves

LOG = logging.getLogger("desk_brain")

router = APIRouter()

_BOOK_CACHE_KEY = "desk_brain:book:latest"
_build_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Input gathering (cheap — caches/snapshots only)
# ---------------------------------------------------------------------------


def _regime_context() -> Dict[str, Any]:
    """Canonical regime label + confidence + a short-vol stress read."""
    ctx = {"label": "Transitional", "confidence": 0.0, "vol_term": "flat", "short_vol_haircut": 0.0}
    try:
        from backend.market_intel import regime_snapshot

        snap = regime_snapshot()
        ctx["label"] = snap.label or "Transitional"
        ctx["confidence"] = float(snap.confidence or 0.0)
        vol_state = snap.vol_state or {}
        ctx["vol_term"] = str(vol_state.get("term_structure", "flat") or "flat")
    except Exception as exc:  # pragma: no cover - defensive
        LOG.warning("desk_brain: regime snapshot failed: %s", exc)

    label = str(ctx["label"]).lower()
    # Overlay stress -> trim short-vol (income) sizing.
    if label == "stressed" or ctx["vol_term"] == "backwardation":
        ctx["short_vol_haircut"] = 0.50
    elif label == "risk-off":
        ctx["short_vol_haircut"] = 0.25
    return ctx


def _reddog_tracker() -> Optional[Dict[str, Any]]:
    try:
        from backend.engine3_screener import get_all_signals
        return get_all_signals()
    except Exception as exc:  # pragma: no cover - defensive
        LOG.warning("desk_brain: red dog tracker read failed: %s", exc)
        return None


def _ichimoku_tracker() -> Optional[Dict[str, Any]]:
    try:
        from backend.engine4_screener import get_all_signals
        return get_all_signals()
    except Exception as exc:  # pragma: no cover - defensive
        LOG.warning("desk_brain: ichimoku tracker read failed: %s", exc)
        return None


def _consensus(regime: Dict[str, Any]) -> Any:
    """Build the cross-engine consensus from the regime read (cheap)."""
    try:
        from backend.consensus_engine import build_consensus_from_apis

        # Derive a coarse 0..100 regime score from the label for the
        # consensus regime extractor (stressed = high, risk-on = low).
        label = str(regime.get("label", "")).lower()
        score = {"risk-on": 25.0, "transitional": 50.0, "risk-off": 65.0, "stressed": 80.0}.get(label, 50.0)
        return build_consensus_from_apis(regime_data={"label": regime.get("label"), "score": score})
    except Exception as exc:  # pragma: no cover - defensive
        LOG.warning("desk_brain: consensus build failed: %s", exc)
        return None


def _regime_income_lean(regime: Dict[str, Any]) -> list:
    """A regime-driven income lean so the volatility sleeve isn't blind.

    When the regime favours short premium (Risk-On / Transitional and not
    backwardated), surface ONE book-level income opportunity representing the
    SPX IC playbook. Honestly tagged ``source="regime_lean"`` — it is a
    posture recommendation, not a specific scanned signal.
    """
    label = str(regime.get("label", "")).lower()
    if label not in ("risk-on", "transitional") or regime.get("vol_term") == "backwardation":
        return []
    confidence = float(regime.get("confidence", 0.0) or 0.0)
    # Risk-On leans harder into premium than Transitional.
    base = 70.0 if label == "risk-on" else 55.0
    conviction = max(40.0, min(90.0, base + 20.0 * confidence))
    edge = sleeves.get_engine_edge(2)
    return [
        aggregator.Opportunity(
            engine_id=2,
            engine_name=edge.engine_name or "SPX Iron Condor",
            sleeve=sleeves.SLEEVE_VOLATILITY,
            ticker="SPX",
            direction="sell_vol",
            structure="iron_condor",
            conviction=conviction,
            verdict="TRADABLE",
            desk_status="",
            summary=f"Regime-driven income lean ({regime.get('label')}); size per SPX IC playbook",
            source="regime_lean",
        )
    ]


# ---------------------------------------------------------------------------
# Book builder
# ---------------------------------------------------------------------------


def _sanitize_for_llm(book: allocator.TargetBook, opp_summary: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only, no raw P&L — just the structure the desk-head needs."""
    return {
        "regime": {"label": book.regime_label, "confidence": round(book.regime_confidence, 3)},
        "heat": {
            "totalBudgetPct": book.total_heat_budget_pct,
            "deployedPct": book.total_deployed_pct,
            "reservePct": book.reserve_pct,
        },
        "sleeves": [s.to_dict() for s in book.sleeves],
        "positions": [
            {
                "ticker": p.ticker, "sleeve": p.sleeve, "engine": p.engine_name,
                "direction": p.direction, "riskPct": round(p.risk_pct, 3),
                "conviction": round(p.conviction, 1), "edgeScore": round(p.edge_score, 3),
            }
            for p in book.positions
        ],
        "conflicts": book.conflicts,
        "opportunitySet": opp_summary,
    }


def _build_book(*, force_refresh: bool, with_llm: bool = True) -> Dict[str, Any]:
    """Assemble inputs -> deterministic book -> LLM synthesis (clamped)."""
    flags = get_flags()
    cfg = allocator.RiskConfig.from_flags(flags)

    from backend.redis_store import get_store_optional
    store = get_store_optional()

    if not force_refresh and store is not None:
        cached = store.get_json(_BOOK_CACHE_KEY)
        if cached:
            cached["cached"] = True
            return cached

    regime = _regime_context()
    reddog = _reddog_tracker()
    ichimoku = _ichimoku_tracker()
    consensus = _consensus(regime)

    opportunities = aggregator.build_opportunity_set(
        reddog_tracker=reddog,
        ichimoku_tracker=ichimoku,
        consensus=consensus,
        extra=_regime_income_lean(regime),
    )
    opp_summary = aggregator.summarize_opportunities(opportunities)

    # Edges blend live paper-trade performance when present.
    edges = sleeves.all_engine_edges(store=store)

    as_of = dt.datetime.utcnow().isoformat() + "Z"

    # First pass: deterministic book with neutral tilt.
    book = allocator.allocate(
        opportunities,
        regime_label=regime["label"],
        regime_confidence=regime["confidence"],
        config=cfg,
        edges=edges,
        short_vol_haircut=float(regime.get("short_vol_haircut", 0.0)),
        as_of=as_of,
    )

    # LLM desk-head synthesis (bounded tilt) -> re-allocate with the tilt.
    synthesis: Dict[str, Any] = {}
    if with_llm and getattr(flags, "ENABLE_FRONT_LAYER_LLM", True):
        try:
            from backend.front_layer_llm import generate_desk_brain_synthesis

            synthesis = generate_desk_brain_synthesis(
                _sanitize_for_llm(book, opp_summary),
                model=getattr(flags, "DESK_BRAIN_MODEL", "gpt-5.5"),
            )
            tilt = synthesis.get("sleeve_tilt") if isinstance(synthesis, dict) else None
            if tilt:
                book = allocator.allocate(
                    opportunities,
                    regime_label=regime["label"],
                    regime_confidence=regime["confidence"],
                    config=cfg,
                    edges=edges,
                    sleeve_tilt=tilt,
                    short_vol_haircut=float(regime.get("short_vol_haircut", 0.0)),
                    as_of=as_of,
                )
        except Exception as exc:  # pragma: no cover - defensive
            LOG.warning("desk_brain: LLM synthesis failed: %s", exc)
            synthesis = {"_source": "fallback", "_fallback_reason": str(exc)}

    payload: Dict[str, Any] = {
        "asOf": as_of,
        "book": book.to_dict(),
        "edges": [e.to_dict() for e in edges.values()],
        "sleeveCatalogue": sleeves.sleeve_list(),
        "opportunities": [o.to_dict() for o in opportunities],
        "opportunitySummary": opp_summary,
        "llm": synthesis,
        "cached": False,
    }

    if store is not None:
        try:
            store.set_json(_BOOK_CACHE_KEY, payload, ttl_s=int(getattr(flags, "DESK_BRAIN_CACHE_TTL_S", 900)))
        except Exception:
            pass

    return payload


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _require_enabled() -> None:
    if not getattr(get_flags(), "ENABLE_DESK_BRAIN", False):
        raise HTTPException(status_code=404, detail="Desk Brain is disabled")


@router.get("/api/desk-brain/book")
def get_book(with_llm: bool = Query(True, description="Include LLM desk-head synthesis")):
    """Return the current risk-budgeted target book (served from cache)."""
    _require_enabled()
    try:
        return _build_book(force_refresh=False, with_llm=with_llm)
    except HTTPException:
        raise
    except Exception as exc:
        LOG.exception("desk_brain: book build failed")
        raise HTTPException(status_code=500, detail=f"Desk Brain book build failed: {exc}")


@router.post("/api/desk-brain/refresh")
def refresh_book(with_llm: bool = Query(True, description="Include LLM desk-head synthesis")):
    """Force a fresh rebuild + LLM re-synthesis of the target book."""
    _require_enabled()
    # Serialise rebuilds so concurrent refreshes don't stampede the LLM.
    with _build_lock:
        try:
            return _build_book(force_refresh=True, with_llm=with_llm)
        except HTTPException:
            raise
        except Exception as exc:
            LOG.exception("desk_brain: book refresh failed")
            raise HTTPException(status_code=500, detail=f"Desk Brain refresh failed: {exc}")


@router.post("/api/desk-brain/paper/record")
def record_paper():
    """Log the current target book into the paper-trade framework."""
    _require_enabled()
    try:
        from backend.desk_brain import paper
        from backend.redis_store import get_store_optional

        store = get_store_optional()
        payload = _build_book(force_refresh=False, with_llm=False)
        result = paper.record_target_book(payload, store=store)
        return {"asOf": payload.get("asOf"), **result}
    except HTTPException:
        raise
    except Exception as exc:
        LOG.exception("desk_brain: paper record failed")
        raise HTTPException(status_code=500, detail=f"Desk Brain paper record failed: {exc}")


@router.get("/api/desk-brain/paper/performance")
def paper_performance():
    """Blended (edge-weighted) vs equal-weight baseline paper performance."""
    _require_enabled()
    try:
        from backend.desk_brain import paper
        from backend.redis_store import get_store_optional

        store = get_store_optional()
        return paper.blended_vs_baseline(store=store)
    except HTTPException:
        raise
    except Exception as exc:
        LOG.exception("desk_brain: paper performance failed")
        raise HTTPException(status_code=500, detail=f"Desk Brain paper performance failed: {exc}")
