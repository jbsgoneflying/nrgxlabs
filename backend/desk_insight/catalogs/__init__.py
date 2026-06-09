"""Raven Desk Insight v2 — per-engine catalog registry.

Each catalog module exports:

- ``ENGINE_META``: ``{id, name, description, asset_class}``
- ``CATALOG``: ``{slug: {"title", "spec", "related_cards": [...]}}``

The registry below is the single source of truth the router consults when
validating engine + card_type arguments.

Engine IDs follow the **UI numbering** convention ("e1" .. "e15") — the
same numbering the desk uses in conversation. Some IDs map to backend
modules with different legacy numbers (see ``backend/config.py::ENGINE_REGISTRY``).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.desk_insight.catalogs import (
    calendar,
    compare,
    e1_breach,
    e2_spx_ic,
    e3_red_dog,
    e4_ichimoku,
    e5_lead_lag,
    e7_pairs,
    e8_post_event,
    e9_credit_stress,
    e11_news_risk,
    e12_vix_fade,
    e13_gap_regime,
    e14_ic_scenario,
    e15_earnings_ic,
    e18_earnings_drift,
    market_intel,
)

_REGISTRY: Dict[str, Any] = {
    "market-intel":  market_intel,
    "e1":            e1_breach,
    "e2":            e2_spx_ic,
    "e3":            e3_red_dog,
    "e4":            e4_ichimoku,
    "e5":            e5_lead_lag,
    "e7":            e7_pairs,
    "e8":            e8_post_event,
    "e9":            e9_credit_stress,
    "e11":           e11_news_risk,
    "e12":           e12_vix_fade,
    "e13":           e13_gap_regime,
    "e14":           e14_ic_scenario,
    "e15":           e15_earnings_ic,
    "e18":           e18_earnings_drift,
    "calendar":      calendar,
    "compare":       compare,
}

# Alternate IDs that should route to the canonical ones. Kept here so shims
# from legacy endpoints can be satisfied without duplicating catalogs.
_ALIASES: Dict[str, str] = {
    "mi":                      "market-intel",
    "market_intel":            "market-intel",
    "market-intelligence":     "market-intel",
    "ic-scenario":             "e14",
    "earnings-ic":             "e15",
    "red-dog":                 "e3",
    "ichimoku":                "e4",
    "lead-lag":                "e5",
    "pairs":                   "e7",
    "post-event":              "e8",
    "credit-stress":           "e9",
    "news-risk":               "e11",
    "vix-fade":                "e12",
    "gap-regime":              "e13",
    "earnings-drift":          "e18",
    "engine18":                "e18",
    "earnings-calendar":       "calendar",
}


def _canonicalize(engine_id: str) -> str:
    s = (engine_id or "").strip().lower()
    return _ALIASES.get(s, s)


def supported_engines() -> List[str]:
    """Canonical engine IDs only (no aliases)."""
    return sorted(_REGISTRY.keys())


def get_engine_meta(engine_id: str) -> Optional[Dict[str, Any]]:
    mod = _REGISTRY.get(_canonicalize(engine_id))
    if mod is None:
        return None
    return dict(getattr(mod, "ENGINE_META", {}))


def get_catalog(engine_id: str) -> Optional[Dict[str, Dict[str, Any]]]:
    mod = _REGISTRY.get(_canonicalize(engine_id))
    if mod is None:
        return None
    return dict(getattr(mod, "CATALOG", {}))


def supported_card_types(engine_id: str) -> List[str]:
    cat = get_catalog(engine_id) or {}
    return sorted(cat.keys())


def resolve_related_label(engine_id: str, slug: str) -> Optional[str]:
    """Look up the human label for a (engine, slug) pair — used by the
    frontend cross-link resolver to render chip titles even when the chip
    points at a card the LLM response didn't fully label."""
    cat = get_catalog(engine_id) or {}
    entry = cat.get(slug)
    if not isinstance(entry, dict):
        return None
    return str(entry.get("title") or slug)


def union_titles() -> Dict[str, Dict[str, str]]:
    """Return ``{engine_id: {slug: title}}`` across every registered engine.

    Used by the shared catalog GET for the frontend cross-link resolver.
    """
    out: Dict[str, Dict[str, str]] = {}
    for eid, mod in _REGISTRY.items():
        cat = dict(getattr(mod, "CATALOG", {}))
        out[eid] = {s: str(v.get("title") or s) for s, v in cat.items()}
    return out


__all__ = [
    "supported_engines",
    "supported_card_types",
    "get_engine_meta",
    "get_catalog",
    "resolve_related_label",
    "union_titles",
]
