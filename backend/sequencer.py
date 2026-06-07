"""NRGX Labs 2.0 – Weekly Signal Sequencer.

Tracks when key signals flip throughout the week and matches the
resulting sequence to named historical patterns.

Event types:
  REGIME_FLIP, DEALER_GAMMA_SHIFT, VOL_LEADLAG_FLIP,
  EARNINGS_DISPERSION_SPIKE,
  RED_DOG_BREADTH_CHANGE, ICHIMOKU_BREADTH_CHANGE

Patterns (Phase 1 – simple template matching):
  pin_and_grind          – stable, compressing, positive gamma
  break_and_trend        – regime flip mid-week, vol expands
  chop_and_mean_revert   – regime oscillates, neutral FP
  vol_expansion_accel    – vol rises, gamma turns negative
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SequencerEvent:
    id: str = ""
    timestamp: str = ""
    date: str = ""
    event_type: str = ""
    from_state: str = ""
    to_state: str = ""
    source_engine: str = ""
    summary: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)
    week_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SequencerEvent":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class WeeklySequence:
    week_id: str = ""
    events: List[dict] = field(default_factory=list)
    pattern_match: Optional[str] = None
    pattern_confidence: float = 0.0
    primary_risk: str = ""
    favored_play_types: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------

EVENT_TYPES = [
    "REGIME_FLIP",
    "DEALER_GAMMA_SHIFT",
    "VOL_LEADLAG_FLIP",
    "EARNINGS_DISPERSION_SPIKE",
    "RED_DOG_BREADTH_CHANGE",
    "ICHIMOKU_BREADTH_CHANGE",
]


# ---------------------------------------------------------------------------
# Week ID helpers
# ---------------------------------------------------------------------------


def current_week_id(d: Optional[dt.date] = None) -> str:
    """Return ISO week ID like '2026-W07'."""
    day = d or dt.date.today()
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def week_trading_days(d: Optional[dt.date] = None) -> List[str]:
    """Return Mon-Fri dates for the ISO week containing d."""
    day = d or dt.date.today()
    # Monday of the week
    monday = day - dt.timedelta(days=day.weekday())
    return [(monday + dt.timedelta(days=i)).isoformat() for i in range(5)]


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


def emit_event(
    *,
    event_type: str,
    from_state: str,
    to_state: str,
    source_engine: str,
    summary: str = "",
    metrics: Optional[dict] = None,
    date: Optional[str] = None,
    week_id: Optional[str] = None,
) -> SequencerEvent:
    """Create a new SequencerEvent."""
    now = dt.datetime.utcnow()
    d = date or now.strftime("%Y-%m-%d")
    wid = week_id or current_week_id(dt.date.fromisoformat(d))

    if not summary:
        summary = f"{source_engine}: {event_type} from {from_state} to {to_state}"

    return SequencerEvent(
        id=str(uuid.uuid4()),
        timestamp=now.isoformat() + "Z",
        date=d,
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        source_engine=source_engine,
        summary=summary,
        metrics=metrics or {},
        week_id=wid,
    )


def detect_state_changes(
    *,
    previous: Dict[str, str],
    current: Dict[str, str],
    date: Optional[str] = None,
) -> List[SequencerEvent]:
    """Compare previous vs current state dicts and emit events for any changes.

    Keys should be like:
      {
        "regime": "Risk-On",
        "dealer_gamma": "positive",
        "vol_leadlag": "NORMAL",
      }
    """
    events = []
    type_map = {
        "regime": ("REGIME_FLIP", "engine5"),
        "dealer_gamma": ("DEALER_GAMMA_SHIFT", "dealer_gamma"),
        "vol_leadlag": ("VOL_LEADLAG_FLIP", "engine5"),
        "earnings_dispersion": ("EARNINGS_DISPERSION_SPIKE", "engine1"),
        "red_dog_breadth": ("RED_DOG_BREADTH_CHANGE", "engine3"),
        "ichimoku_breadth": ("ICHIMOKU_BREADTH_CHANGE", "engine4"),
    }

    for key, (event_type, source) in type_map.items():
        prev = str(previous.get(key, "")).strip()
        curr = str(current.get(key, "")).strip()
        if prev and curr and prev != curr:
            events.append(emit_event(
                event_type=event_type,
                from_state=prev,
                to_state=curr,
                source_engine=source,
                date=date,
            ))

    return events


# ---------------------------------------------------------------------------
# Pattern templates (Phase 1 – simple matching)
# ---------------------------------------------------------------------------


PATTERN_TEMPLATES = {
    "pin_and_grind": {
        "label": "Pin and Grind",
        "description": "Regime stable, vol compressing, dealer gamma positive. No major flips.",
        "expected_events": [],  # zero or few events = match
        "max_events": 1,
        "required_states": {
            "vol_leadlag": ["NORMAL", "CONFIRMED_STRESS"],
        },
        "favored_play_types": ["premium_selling", "iron_condor", "credit_spread"],
        "primary_risk": "Low vol regime break if macro catalyst hits",
    },
    "break_and_trend": {
        "label": "Break and Trend",
        "description": "Regime flips to Risk-On or Risk-Off mid-week. Vol expands.",
        "expected_events": ["REGIME_FLIP"],
        "min_events": 1,
        "favored_play_types": ["directional_spread", "calendar", "defined_risk"],
        "primary_risk": "False breakout if regime flip reverses",
    },
    "chop_and_mean_revert": {
        "label": "Chop and Mean Revert",
        "description": "Regime oscillates with multiple small flips. Vol stable.",
        "expected_events": ["REGIME_FLIP"],
        "min_events": 2,
        "favored_play_types": ["mean_reversion", "iron_condor", "butterfly"],
        "primary_risk": "Trend emerging from what looks like chop",
    },
    "vol_expansion_accel": {
        "label": "Vol Expansion Acceleration",
        "description": "Vol lead-lag flips to rising. Dealer gamma turns negative.",
        "expected_events": ["VOL_LEADLAG_FLIP", "DEALER_GAMMA_SHIFT"],
        "min_events": 2,
        "favored_play_types": ["defined_risk", "debit_spread", "hedge"],
        "primary_risk": "Acceleration continues beyond expected magnitude",
    },
}


def match_pattern(events: List[SequencerEvent]) -> tuple:
    """Match a list of events to the best-fitting pattern.

    Returns (pattern_key, confidence, pattern_template).
    """
    event_types = [e.event_type for e in events]
    n_events = len(events)

    best_key = None
    best_confidence = 0.0

    for key, tmpl in PATTERN_TEMPLATES.items():
        expected = tmpl.get("expected_events") or []
        min_events = tmpl.get("min_events", 0)
        max_events = tmpl.get("max_events", 999)

        if key == "pin_and_grind":
            if n_events <= max_events:
                confidence = 0.70 if n_events == 0 else 0.45
            else:
                confidence = max(0.0, 0.25 - (n_events - max_events) * 0.1)
        else:
            if not expected:
                continue
            matched = sum(1 for et in expected if et in event_types)
            if matched == 0:
                continue
            match_ratio = matched / len(expected)

            if n_events >= min_events:
                event_bonus = 1.0
            else:
                event_bonus = 0.75

            confidence = match_ratio * event_bonus * 0.85

        if confidence > best_confidence:
            best_confidence = confidence
            best_key = key

    if best_key is None:
        return None, 0.0, None

    return best_key, round(best_confidence, 2), PATTERN_TEMPLATES[best_key]


def build_weekly_sequence(
    week_id: str,
    events: List[SequencerEvent],
) -> WeeklySequence:
    """Build a WeeklySequence from events with pattern matching."""
    sorted_events = sorted(events, key=lambda e: e.timestamp)
    pattern_key, confidence, tmpl = match_pattern(sorted_events)

    return WeeklySequence(
        week_id=week_id,
        events=[e.to_dict() for e in sorted_events],
        pattern_match=pattern_key,
        pattern_confidence=confidence,
        primary_risk=tmpl.get("primary_risk", "") if tmpl else "",
        favored_play_types=tmpl.get("favored_play_types", []) if tmpl else [],
    )
