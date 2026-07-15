"""Shadow signal lifecycle for post-promotion Equity Repricing Lab (Phase 5).

Inactive unless REPRICING_LAB_ENABLED and research promotion has occurred.
Does not place live risk while REPRICING_LAB_SHADOW_ONLY=1.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from backend.config import get_flags
from backend.repricing_lab import store

LOG = logging.getLogger("repricing_lab.signals")

# Redis-free: shadow state lives in research_candidate + a lightweight JSON artifact.
# Lifecycle: scout → setup → ticket → managing → closed|invalidated


def list_shadow_candidates(*, limit: int = 50) -> List[Dict[str, Any]]:
    flags = get_flags()
    if not flags.REPRICING_LAB_ENABLED:
        return []
    with store.connect() as conn:
        rows = store.read_rows(
            conn, "research_candidate",
            order_by="created_at DESC", limit=limit,
        )
    out = []
    for r in rows:
        out.append({
            "candidateId": r["candidate_id"],
            "archetype": r["archetype"],
            "instrumentId": r["instrument_id"],
            "side": r["side"],
            "decisionSession": r["decision_session"],
            "strategyVersion": r["strategy_version"],
            "reasonCodes": json.loads(r["reason_codes"] or "[]"),
            "vetoes": json.loads(r["vetoes"] or "[]"),
            "lifecycle": "scout",
            "shadowOnly": bool(flags.REPRICING_LAB_SHADOW_ONLY),
        })
    return out


def decay_check(*, expectancy_r: float, n: int, window: str = "6m") -> Dict[str, Any]:
    """Auto-demote to shadow-only when rolling expectancy goes negative."""
    demote = expectancy_r < 0 and n >= 30
    return {
        "window": window,
        "expectancyR": expectancy_r,
        "n": n,
        "demoteToShadowOnly": demote,
        "action": "force_shadow_only" if demote else "hold",
    }
