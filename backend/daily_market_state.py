"""Raven-Tech Front Layer – DailyMarketState.

Single canonical object written once per day that synthesizes all existing
Raven Tech engines into a deterministic market snapshot.

Run time:
  - Daily at 03:55 EST
  - Weekly snapshot Sunday at 18:00 EST

Storage:
  - Redis: front_layer:dms:{YYYY-MM-DD}, TTL 120 days
  - Rolling index: front_layer:dms:index

This object is the ONLY thing the LLM layer is allowed to read.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)

DMS_TTL_S = 120 * 86400  # 120 days
DMS_KEY_PREFIX = "front_layer:dms"
DMS_INDEX_KEY = "front_layer:dms:index"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RegimeState:
    state: str = "Transitional"          # Risk-On | Transitional | Risk-Off | Stressed
    score: float = 50.0                  # 0-100 composite stress
    drivers: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RegimeState":
        if not isinstance(d, dict):
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class VolState:
    level: float = 0.0
    term_structure: str = "flat"         # contango | flat | backwardation
    skew: str = "neutral"                # low | neutral | elevated

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "VolState":
        if not isinstance(d, dict):
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class EngineGates:
    earnings: str = "allowed"            # allowed | selective | suppressed
    red_dog: str = "allowed"             # allowed | watch | suppressed
    ichimoku: str = "allowed"            # allowed | selective | suppressed
    index_income: str = "allowed"        # allowed | reduced | suppressed
    post_event_ext: str = "allowed"      # allowed | selective | suppressed

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EngineGates":
        if not isinstance(d, dict):
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class EarningsCandidate:
    ticker: str = ""
    score: float = 0.0
    dealer_gamma: str = "neutral"        # supportive | neutral | hostile
    expected_move_ratio: float = 0.0
    regime_fit: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EarningsCandidate":
        if not isinstance(d, dict):
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class NewsRiskState:
    today: str = "low"                   # low | medium | high
    week_ahead: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "NewsRiskState":
        if not isinstance(d, dict):
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class DailyMarketState:
    """Core Front Layer object – the only input to the LLM pipeline."""

    date: str = ""
    generated_at: str = ""
    regime: dict = field(default_factory=dict)
    vol_state: dict = field(default_factory=dict)
    engine_gates: dict = field(default_factory=dict)
    earnings_candidates: List[dict] = field(default_factory=list)
    index_state: Dict[str, dict] = field(default_factory=dict)
    news_risk: dict = field(default_factory=dict)
    cross_asset_stress: dict = field(default_factory=dict)
    news_themes: List[dict] = field(default_factory=list)
    sequencer_summary: dict = field(default_factory=dict)
    asymmetry_signals: List[dict] = field(default_factory=list)
    post_event_extensions: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DailyMarketState":
        if not isinstance(d, dict):
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Engine gate derivation
# ---------------------------------------------------------------------------


def _derive_engine_gates(
    regime_label: str,
    vol_direction: str,
) -> EngineGates:
    """Derive engine gate statuses from regime and vol state.

    Rules:
      - Stressed regime suppresses everything.
      - Risk-Off suppresses earnings/index income, allows red_dog.
      - Transitional is selective.
      - Risk-On allows everything.
    """
    if regime_label == "Stressed":
        return EngineGates(
            earnings="suppressed",
            red_dog="allowed",      # Red Dog thrives in stress
            ichimoku="suppressed",
            index_income="suppressed",
            post_event_ext="suppressed",
        )
    elif regime_label == "Risk-Off":
        red_dog = "allowed"
        ichimoku = "suppressed"
        earnings = "selective"
        index_income = "reduced"
        post_event_ext = "suppressed"
    elif regime_label == "Transitional":
        red_dog = "allowed"
        ichimoku = "selective"
        earnings = "selective"
        index_income = "allowed"
        post_event_ext = "selective"
    else:  # Risk-On
        red_dog = "watch"  # Red Dog is contrarian, less useful in risk-on
        ichimoku = "allowed"
        earnings = "allowed"
        index_income = "allowed"
        post_event_ext = "allowed"

    return EngineGates(
        earnings=earnings,
        red_dog=red_dog,
        ichimoku=ichimoku,
        index_income=index_income,
        post_event_ext=post_event_ext,
    )


def _derive_vol_state(
    vol_direction: str,
    iv_stress: float,
    vix_level: Optional[float] = None,
) -> VolState:
    """Derive VolState from Engine 5 vol lead-lag and IV stress."""
    # Level: use VIX if available, else map from iv_stress
    level = vix_level if vix_level is not None else round(iv_stress * 0.5, 1)

    # Term structure from vol direction
    direction_lower = (vol_direction or "").lower()
    if direction_lower in ("rising", "confirmed_stress", "expanding"):
        term_structure = "backwardation"
    elif direction_lower in ("falling", "compressing"):
        term_structure = "contango"
    else:
        term_structure = "flat"

    # Skew from IV stress level
    if iv_stress >= 65:
        skew = "elevated"
    elif iv_stress <= 30:
        skew = "low"
    else:
        skew = "neutral"

    return VolState(level=level, term_structure=term_structure, skew=skew)


def _derive_news_risk(
    event_count_5d: int,
    high_severity_count: int,
    upcoming_events: Optional[List[str]] = None,
) -> NewsRiskState:
    """Derive NewsRiskState from macro event data."""
    if high_severity_count >= 3 or event_count_5d >= 8:
        today = "high"
    elif high_severity_count >= 1 or event_count_5d >= 4:
        today = "medium"
    else:
        today = "low"

    return NewsRiskState(
        today=today,
        week_ahead=upcoming_events or [],
    )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_daily_market_state(
    *,
    date_str: Optional[str] = None,
    regime: Optional[dict] = None,
    vol_direction: str = "",
    iv_stress: float = 50.0,
    vix_level: Optional[float] = None,
    earnings_candidates: Optional[List[dict]] = None,
    index_state: Optional[Dict[str, dict]] = None,
    event_count_5d: int = 0,
    high_severity_count: int = 0,
    upcoming_events: Optional[List[str]] = None,
    cross_asset_stress: Optional[dict] = None,
    news_themes: Optional[List[dict]] = None,
    sequencer_summary: Optional[dict] = None,
    asymmetry_signals: Optional[List[dict]] = None,
    post_event_extensions: Optional[List[dict]] = None,
) -> DailyMarketState:
    """Build a DailyMarketState from engine outputs.

    All inputs are read-only snapshots from existing engines.
    No engine logic is modified.
    """
    now = dt.datetime.now(dt.timezone.utc)
    date_str = date_str or now.strftime("%Y-%m-%d")

    # --- Regime ---
    regime = regime or {}
    regime_label = str(regime.get("label", "Transitional"))
    regime_score = float(regime.get("score", 50.0))
    regime_components = regime.get("components", {})
    # Derive top 2 drivers
    sorted_comps = sorted(regime_components.items(), key=lambda kv: kv[1], reverse=True)
    drivers = [k for k, _ in sorted_comps[:3]] if sorted_comps else []

    regime_state = RegimeState(
        state=regime_label,
        score=regime_score,
        drivers=drivers,
    )

    # --- Vol State ---
    vol_state = _derive_vol_state(vol_direction, iv_stress, vix_level)

    # --- Engine Gates ---
    engine_gates = _derive_engine_gates(regime_label, vol_direction)

    # --- News Risk ---
    news_risk = _derive_news_risk(event_count_5d, high_severity_count, upcoming_events)

    return DailyMarketState(
        date=date_str,
        generated_at=now.isoformat() + "Z",
        regime=regime_state.to_dict(),
        vol_state=vol_state.to_dict(),
        engine_gates=engine_gates.to_dict(),
        earnings_candidates=earnings_candidates or [],
        index_state=index_state or {},
        news_risk=news_risk.to_dict(),
        cross_asset_stress=cross_asset_stress or {},
        news_themes=news_themes or [],
        sequencer_summary=sequencer_summary or {},
        asymmetry_signals=asymmetry_signals or [],
        post_event_extensions=post_event_extensions or [],
    )


# ---------------------------------------------------------------------------
# Redis persistence
# ---------------------------------------------------------------------------


def persist_dms(dms: DailyMarketState, store: Any, ttl_s: int = DMS_TTL_S) -> bool:
    """Persist a DailyMarketState snapshot to Redis.

    Key: front_layer:dms:{date}
    Also updates the rolling index.
    """
    if store is None:
        LOG.warning("No Redis store available; DMS not persisted")
        return False

    key = f"{DMS_KEY_PREFIX}:{dms.date}"
    payload = dms.to_dict()

    ok = store.set_json(key, payload, ttl_s=ttl_s)
    if not ok:
        LOG.error("Failed to persist DMS for %s", dms.date)
        return False

    # Update rolling index
    index = store.get_json(DMS_INDEX_KEY) or []
    if not isinstance(index, list):
        index = []
    if dms.date not in index:
        index.insert(0, dms.date)
    # Keep max 150 entries (120 days + buffer)
    index = index[:150]
    store.set_json(DMS_INDEX_KEY, index, ttl_s=ttl_s)

    LOG.info("Persisted DMS for %s", dms.date)
    return True


def load_dms(date_str: str, store: Any) -> Optional[DailyMarketState]:
    """Load a single DailyMarketState from Redis."""
    if store is None:
        return None
    key = f"{DMS_KEY_PREFIX}:{date_str}"
    data = store.get_json(key)
    if data is None:
        return None
    return DailyMarketState.from_dict(data)


def load_dms_history(store: Any, n: int = 7) -> List[DailyMarketState]:
    """Load the last N DailyMarketState snapshots from Redis.

    Returns list sorted newest-first.
    """
    if store is None:
        return []

    index = store.get_json(DMS_INDEX_KEY) or []
    if not isinstance(index, list):
        return []

    results: List[DailyMarketState] = []
    for date_str in index[:n]:
        dms = load_dms(date_str, store)
        if dms is not None:
            results.append(dms)

    return results


# ---------------------------------------------------------------------------
# Diff utility
# ---------------------------------------------------------------------------


def compute_dms_diff(today: DailyMarketState, yesterday: DailyMarketState) -> dict:
    """Compute structured differences between two DailyMarketState snapshots.

    Returns a dict with changed fields and their old/new values.
    """
    changes: Dict[str, dict] = {}
    today_d = today.to_dict()
    yesterday_d = yesterday.to_dict()

    # Top-level scalar-like fields to diff
    diff_fields = ["regime", "vol_state", "engine_gates", "news_risk"]

    for field_name in diff_fields:
        t_val = today_d.get(field_name, {})
        y_val = yesterday_d.get(field_name, {})

        if not isinstance(t_val, dict) or not isinstance(y_val, dict):
            if t_val != y_val:
                changes[field_name] = {"old": y_val, "new": t_val}
            continue

        field_changes: Dict[str, dict] = {}
        all_keys = set(list(t_val.keys()) + list(y_val.keys()))
        for k in all_keys:
            tv = t_val.get(k)
            yv = y_val.get(k)
            if tv != yv:
                field_changes[k] = {"old": yv, "new": tv}
        if field_changes:
            changes[field_name] = field_changes

    # Count changes in list fields
    for list_field in ["earnings_candidates", "news_themes", "asymmetry_signals", "post_event_extensions"]:
        t_list = today_d.get(list_field, [])
        y_list = yesterday_d.get(list_field, [])
        if len(t_list) != len(y_list):
            changes[list_field] = {
                "count_change": {"old": len(y_list), "new": len(t_list)},
            }

    return {
        "from_date": yesterday.date,
        "to_date": today.date,
        "changes": changes,
        "has_changes": len(changes) > 0,
    }
