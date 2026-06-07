"""POST /api/v2/committee/deliberate — five-agent committee deliberation.

Assembles a Foundation Brain context block from the live regime index,
analogue index, and conformal calibrators, then runs a 5-agent Claude
mesh (Researcher → Quant → Devil's Advocate → Risk Officer →
Synthesizer) over the resulting setup.

Each deliberation is persisted to a Redis stream
(``v2:committee:deliberations``) so the desk can replay any decision
and we can wire a counterfactual logger that compares "what the v1
advisor said" vs. "what the v2 committee decided" in a single view.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

try:
    import redis as redis_pkg  # type: ignore
except Exception:  # pragma: no cover - tests inject a fake client
    redis_pkg = None  # type: ignore

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..agents.committee import (
    AgentRole,
    ClaudeClient,
    CommitteeRunner,
    SetupPayload,
    get_default_client,
)
from ..foundation.analogues import extract_features as analogue_extract
from ..foundation.analogues_store import load_index as load_analogue_index
from ..foundation.conformal_store import (
    list_calibrators as list_conformal_calibrators,
    load_calibrator,
)
from ..foundation.regime import REGIME_LABELS, extract_market_state
from ..foundation.regime_store import load_index as load_regime_index

LOG = logging.getLogger("v2.committee_api")
router = APIRouter()


COMMITTEE_STREAM = "v2:committee:deliberations"
COMMITTEE_STREAM_MAXLEN = 500


# ── Optional injection point for tests ────────────────────


_client_factory: callable | None = None


def set_claude_client_factory(factory):
    """Tests inject a fake client by calling this with a callable that
    returns the desired ClaudeClient. Production leaves it None and the
    runner falls through to the real Anthropic SDK."""
    global _client_factory
    _client_factory = factory


def _resolve_client() -> ClaudeClient:
    if _client_factory is not None:
        return _client_factory()
    return get_default_client()


# ── Redis helper (scoped — many other modules already do this) ──


def _redis_client():
    if redis_pkg is None:
        raise RuntimeError("redis package not available")
    url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    return redis_pkg.Redis.from_url(url, decode_responses=True)


# ── Request models ────────────────────────────────────────


class CommitteeSetupModel(BaseModel):
    engine: str = Field(..., min_length=1, max_length=8)
    ticker: str = Field(..., min_length=1, max_length=12)
    structure: str = Field(..., min_length=1, max_length=64)
    short_strikes: list[float] = Field(default_factory=list)
    long_strikes: list[float] = Field(default_factory=list)
    dte: int = Field(0, ge=0, le=365)
    expected_move: Optional[float] = None
    iv_rank: Optional[float] = None
    notes: str = ""


class DeliberatePayload(BaseModel):
    setup: CommitteeSetupModel
    market_state: Optional[dict[str, Any]] = None  # raw DMS doc OR feature dict
    extra_context: Optional[dict[str, Any]] = None


class DryRunPayload(BaseModel):
    setup: CommitteeSetupModel
    market_state: Optional[dict[str, Any]] = None


# ── Context assembly ──────────────────────────────────────


def _assemble_context(setup: SetupPayload, market_state: dict[str, Any] | None) -> dict[str, Any]:
    """Pull regime / analogue / conformal context from the Foundation Brain
    so every agent reads from the same fact set."""
    context: dict[str, Any] = {}

    # Regime: top-K nearest historical days + current label distribution.
    try:
        idx = load_regime_index()
    except Exception as exc:
        LOG.warning("committee: regime index unreadable: %s", exc)
        idx = None
    if idx is not None and idx.n_indexed:
        feats = (
            extract_market_state(market_state)
            if market_state and any(k in market_state for k in ("regime", "vol_state"))
            else market_state or {}
        )
        # Filter out unknown feature keys.
        feats = {k: v for k, v in (feats or {}).items() if k in idx.feature_names}
        if feats:
            context["regime"] = {
                "n_indexed": idx.n_indexed,
                "encoding": idx.encode(feats),
                "label_distribution": idx.label_distribution(),
                "labels": REGIME_LABELS,
            }
        else:
            context["regime"] = {
                "n_indexed": idx.n_indexed,
                "label_distribution": idx.label_distribution(),
                "note": "no market_state supplied — only label distribution is available",
            }
    else:
        context["regime"] = {"n_indexed": 0, "note": "regime index not built"}

    # Analogues: top-K similar historical setups for the engine.
    try:
        a_idx = load_analogue_index(engine=setup.engine)
    except Exception as exc:
        LOG.warning("committee: analogue index unreadable: %s", exc)
        a_idx = None
    if a_idx is not None and a_idx.n_indexed:
        try:
            features = analogue_extract(setup.to_dict(), engine=setup.engine)
            nbrs = a_idx.search(features, k=5, ticker_exclude=setup.ticker)
            outcomes = a_idx.outcome_summary(nbrs)
        except Exception as exc:
            LOG.warning("committee: analogue search failed: %s", exc)
            nbrs, outcomes = [], {}
        context["analogues"] = {
            "n_indexed": a_idx.n_indexed,
            "engine": setup.engine,
            "neighbors": nbrs,
            "outcome_summary": outcomes,
        }
    else:
        context["analogues"] = {"n_indexed": 0, "note": f"no analogue index for engine {setup.engine}"}

    # Conformal: latest calibrator coverage for the engine's breach metric.
    try:
        cals = list_conformal_calibrators()
    except Exception as exc:
        LOG.warning("committee: conformal list unreadable: %s", exc)
        cals = []
    breach_cal = None
    for c in cals or []:
        if c.get("engine") == setup.engine and c.get("metric") == "breach":
            breach_cal = c
            break
    if breach_cal:
        try:
            cal = load_calibrator(engine=setup.engine, metric="breach")
            interval = (
                cal.interval(prediction=0.5, alpha=0.10)
                if cal and cal.state.n >= cal.MIN_WARMUP_N else None
            )
            context["conformal_breach"] = {
                "engine": setup.engine,
                "n_observations": breach_cal.get("n_calibration", 0),
                "empirical_coverage": breach_cal.get("empirical_coverage"),
                "target_coverage": 0.90,
                "sample_interval_at_p_eq_0.5": (
                    {"lower": interval.lower, "upper": interval.upper}
                    if interval else None
                ),
            }
        except Exception as exc:
            LOG.warning("committee: conformal load failed: %s", exc)

    return context


# ── Persistence ────────────────────────────────────────────


def _persist(decision: dict[str, Any]) -> None:
    try:
        client = _redis_client()
        client.xadd(
            COMMITTEE_STREAM,
            {"json": json.dumps(decision, default=str)},
            maxlen=COMMITTEE_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as exc:  # pragma: no cover - persistence is best-effort
        LOG.warning("committee: persistence failed: %s", exc)


# ── Endpoints ─────────────────────────────────────────────


@router.post("/api/v2/committee/dry-run")
def dry_run(payload: DryRunPayload) -> dict:
    """Assemble the Foundation Brain context for a setup but skip the LLM
    calls. Used by the desk to inspect what the agents would see before
    burning tokens.
    """
    setup = SetupPayload(**payload.setup.model_dump())
    context = _assemble_context(setup, payload.market_state)
    return {
        "ok": True,
        "setup": setup.to_dict(),
        "context": context,
    }


@router.post("/api/v2/committee/deliberate")
def deliberate(payload: DeliberatePayload) -> dict:
    setup = SetupPayload(**payload.setup.model_dump())
    context = _assemble_context(setup, payload.market_state)
    if payload.extra_context:
        context["extra"] = payload.extra_context

    try:
        client = _resolve_client()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"committee unavailable: {exc}",
        ) from exc

    runner = CommitteeRunner(client=client)
    decision = runner.deliberate(setup=setup, context=context)
    out = decision.to_dict()
    _persist(out)
    return {"ok": True, **out}


@router.get("/api/v2/committee/recent")
def recent(n: int = 10) -> dict:
    n = max(1, min(int(n), 100))
    try:
        client = _redis_client()
        entries = client.xrevrange(COMMITTEE_STREAM, count=n)
    except Exception as exc:
        return {"status": "redis_unavailable", "error": str(exc), "entries": []}
    out = []
    for entry_id, fields in entries or []:
        raw = fields.get("json") if isinstance(fields, dict) else None
        if not raw:
            continue
        try:
            doc = json.loads(raw)
        except Exception:
            continue
        out.append({
            "id": entry_id,
            "decision": (doc.get("synthesis") or {}).get("parsed", {}).get("decision"),
            "headline": (doc.get("synthesis") or {}).get("parsed", {}).get("headline"),
            "ticker": (doc.get("setup") or {}).get("ticker"),
            "engine": (doc.get("setup") or {}).get("engine"),
            "elapsed_ms": doc.get("elapsed_ms"),
        })
    return {"status": "ok", "n": len(out), "entries": out}


@router.get("/api/v2/committee/roles")
def roles() -> dict:
    """Return the role lineup so the dashboard can render the committee
    panel without hard-coding role names."""
    return {
        "roles": [
            {"id": role.value, "order": i}
            for i, role in enumerate(list(AgentRole))
        ],
    }
