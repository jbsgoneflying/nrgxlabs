"""Legacy thin shim for Engine 14 per-card LLM tooltips.

The v2 **canonical catalog** lives at
:mod:`backend.desk_insight.catalogs.e14_ic_scenario` — all new slugs
(``wing_console``, ``placement_score``, ``mc_reading``,
``mae_distribution``, ``regime_mi_v2``) are defined there. This
module remains as a back-compat re-export so any stray import
elsewhere in the codebase keeps working.

Prefer the new API:

    from backend.desk_insight import (
        generate_desk_insight,
        get_catalog,
        get_engine_meta,
    )

Scheduled for removal one release cycle after all callers migrate.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.desk_insight import (
    OUTPUT_KEYS,
    OUTPUT_LABELS,
    generate_desk_insight,
    get_catalog,
    get_engine_meta,
)
from backend.desk_insight.catalogs import supported_card_types as _supported

__all__ = [
    "CARD_CATALOG",
    "OUTPUT_KEYS",
    "OUTPUT_LABELS",
    "card_title",
    "supported_card_types",
    "generate_card_explanation",
]


def _catalog() -> Dict[str, Dict[str, Any]]:
    return get_catalog("e14") or {}


# Module-level proxy so any legacy ``from ... import CARD_CATALOG`` still works.
CARD_CATALOG: Dict[str, Dict[str, Any]] = _catalog()


def card_title(card_type: str) -> str:
    entry = _catalog().get(card_type)
    return (entry or {}).get("title", card_type)


def supported_card_types() -> List[str]:
    return _supported("e14")


def generate_card_explanation(
    card_type: str,
    card_data: Any,
    scenario_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Backwards-compat wrapper. Routes through desk_insight v2."""
    return generate_desk_insight(
        engine_id="e14",
        card_type=card_type,
        card_data=card_data,
        scenario_context=scenario_context or {},
        catalog=_catalog(),
        engine_meta=get_engine_meta("e14") or {},
    )
