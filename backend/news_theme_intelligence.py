"""NRGX Labs Front Layer – News Theme Intelligence.

Clusters headlines from EODHD and Benzinga into named themes, scoring each
by intensity, persistence, and acceleration. Raw headlines are NEVER stored
in LLM context (spec rule).

Themes:
  - AI Displacement
  - Labor Stress
  - Credit Stress
  - Geopolitical Escalation
  - Regulation Pressure
  - Liquidity Shock

Output feeds into DailyMarketState only.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)

THEME_KEY_PREFIX = "front_layer:themes"
THEME_TTL_S = 120 * 86400  # 120 days


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ThemeReading:
    """Single theme's daily reading."""

    theme: str = ""
    key: str = ""
    intensity: float = 0.0              # 0-100
    adjusted_intensity: float = 0.0     # intensity * spx_impact_weight
    spx_impact_weight: float = 0.5
    persistence_days: int = 0
    acceleration: str = "stable"         # rising | falling | stable
    affected_sectors: List[str] = field(default_factory=list)
    keyword_hits: int = 0
    sample_keywords: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ThemeReading":
        if not isinstance(d, dict):
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class NewsThemeSnapshot:
    """Daily aggregation of all theme readings."""

    date: str = ""
    themes: List[dict] = field(default_factory=list)
    dominant_theme: str = ""
    total_headline_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "NewsThemeSnapshot":
        if not isinstance(d, dict):
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Theme definitions – keyword sets
# ---------------------------------------------------------------------------

THEME_DEFINITIONS: Dict[str, dict] = {
    "ai_displacement": {
        "label": "AI Displacement",
        "keywords": [
            "artificial intelligence", "ai replace", "ai job", "automation",
            "chatgpt", "generative ai", "machine learning", "ai layoff",
            "ai workforce", "ai disruption", "robot", "ai threat",
            "ai adoption", "large language model", "llm", "deepfake",
            "ai regulation", "openai", "anthropic", "ai chip",
        ],
        "sectors": ["Technology", "Industrials", "Financials", "Healthcare"],
        "spx_impact_weight": 0.25,
    },
    "labor_stress": {
        "label": "Labor Stress",
        "keywords": [
            "layoff", "job cut", "unemployment", "hiring freeze",
            "labor shortage", "wage pressure", "strike", "union",
            "workforce reduction", "downsizing", "furlough", "jobless",
            "labor market", "quit rate", "job opening", "initial claims",
            "nonfarm payroll", "employment report",
        ],
        "sectors": ["Consumer Discretionary", "Industrials", "Technology"],
        "spx_impact_weight": 0.50,
    },
    "credit_stress": {
        "label": "Credit Stress",
        "keywords": [
            "credit spread", "default", "bankruptcy", "debt crisis",
            "high yield", "junk bond", "credit crunch", "loan loss",
            "delinquency", "credit downgrade", "leveraged loan",
            "commercial real estate", "bank failure", "credit risk",
            "bond spread", "cds spread", "covenant breach",
            "distressed debt", "credit tighten",
        ],
        "sectors": ["Financials", "Real Estate", "Consumer Discretionary"],
        "spx_impact_weight": 0.80,
    },
    "geopolitical_escalation": {
        "label": "Geopolitical Escalation",
        "keywords": [
            "war", "conflict", "sanction", "tariff", "trade war",
            "military", "invasion", "missile", "nuclear", "nato",
            "escalation", "embargo", "geopolitical", "tension",
            "retaliation", "cyber attack", "sovereignty", "blockade",
            "territorial", "diplomatic crisis",
        ],
        "sectors": ["Energy", "Industrials", "Defense", "Materials"],
        "spx_impact_weight": 1.0,
    },
    "regulation_pressure": {
        "label": "Regulation Pressure",
        "keywords": [
            "regulation", "antitrust", "sec enforcement", "doj probe",
            "ftc", "compliance", "congressional hearing", "subpoena",
            "executive order", "regulatory crackdown", "data privacy",
            "gdpr", "fine", "penalty", "consent decree", "legislation",
            "ban", "restrict", "oversight",
        ],
        "sectors": ["Technology", "Financials", "Healthcare", "Energy"],
        "spx_impact_weight": 0.35,
    },
    "liquidity_shock": {
        "label": "Liquidity Shock",
        "keywords": [
            "liquidity crisis", "margin call", "repo rate", "treasury auction",
            "fed emergency", "cash crunch", "money market", "reverse repo",
            "balance sheet", "qt", "quantitative tightening",
            "bank run", "deposit flight", "contagion", "systemic risk",
            "flash crash", "circuit breaker", "halt", "illiquid",
        ],
        "sectors": ["Financials", "REITs", "Utilities"],
        "spx_impact_weight": 0.95,
    },
}

# Reverse lookup: label -> key for weight resolution
_LABEL_TO_KEY: Dict[str, str] = {
    defn["label"]: key for key, defn in THEME_DEFINITIONS.items()
}


def get_theme_impact_weight(theme_key_or_label: str) -> float:
    """Return the SPX impact weight (0-1) for a theme by key or label."""
    key = str(theme_key_or_label).strip()
    defn = THEME_DEFINITIONS.get(key)
    if defn:
        return float(defn.get("spx_impact_weight", 0.5))
    resolved = _LABEL_TO_KEY.get(key)
    if resolved:
        return float(THEME_DEFINITIONS[resolved].get("spx_impact_weight", 0.5))
    return 0.5


def compute_market_adjusted_intensity(raw_intensity: float, theme_key_or_label: str) -> float:
    """Compute market-adjusted intensity = raw_intensity * spx_impact_weight."""
    w = get_theme_impact_weight(theme_key_or_label)
    return round(raw_intensity * w, 1)


# ---------------------------------------------------------------------------
# Keyword matching engine
# ---------------------------------------------------------------------------


def _count_keyword_hits(
    headlines: List[str],
    keywords: List[str],
) -> tuple[int, List[str]]:
    """Count how many headlines match any keyword. Return count and matched keywords.

    Case-insensitive substring match.
    """
    hit_count = 0
    matched_kws: set[str] = set()

    for headline in headlines:
        hl = headline.lower()
        for kw in keywords:
            if kw.lower() in hl:
                hit_count += 1
                matched_kws.add(kw)
                break  # One hit per headline

    return hit_count, sorted(matched_kws)[:5]


def _compute_intensity(hit_count: int, total_headlines: int) -> float:
    """Compute theme intensity score (0-100).

    Based on hit ratio scaled nonlinearly to emphasize concentration.
    """
    if total_headlines <= 0 or hit_count <= 0:
        return 0.0

    ratio = hit_count / total_headlines

    # Nonlinear scaling: small ratio = low, rapid rise after ~5%
    # 1% -> ~10, 5% -> ~40, 10% -> ~65, 20%+ -> ~85+
    intensity = min(100, ratio * 500)
    # Additional boost for absolute hit count
    abs_boost = min(20, hit_count * 2)
    return round(min(100, intensity + abs_boost), 1)


def _compute_acceleration(
    today_intensity: float,
    prior_intensities: List[float],
) -> str:
    """Compute theme acceleration from rolling intensity history.

    Rising: today > avg of last 3 by more than 10 points.
    Falling: today < avg of last 3 by more than 10 points.
    Stable: otherwise.
    """
    if not prior_intensities:
        if today_intensity > 20:
            return "rising"
        return "stable"

    recent = prior_intensities[:3]  # Most recent first
    avg_recent = sum(recent) / len(recent)

    diff = today_intensity - avg_recent
    if diff > 10:
        return "rising"
    elif diff < -10:
        return "falling"
    return "stable"


def _compute_persistence(
    today_intensity: float,
    prior_intensities: List[float],
    threshold: float = 10.0,
) -> int:
    """Count consecutive days (including today) with intensity above threshold."""
    if today_intensity < threshold:
        return 0

    days = 1  # Today counts
    for pi in prior_intensities:
        if pi >= threshold:
            days += 1
        else:
            break

    return days


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------


def score_themes(
    *,
    headlines: List[str],
    prior_snapshots: Optional[List[dict]] = None,
    date_str: Optional[str] = None,
) -> NewsThemeSnapshot:
    """Score all themes from today's headlines against prior history.

    Args:
        headlines: List of headline strings from EODHD/Benzinga.
                   Raw text used only for keyword matching, then discarded.
        prior_snapshots: Rolling previous NewsThemeSnapshot dicts (newest first).
        date_str: Override date (defaults to today).

    Returns:
        NewsThemeSnapshot with per-theme readings.
    """
    date_str = date_str or dt.date.today().isoformat()
    total = len(headlines)
    prior_snapshots = prior_snapshots or []

    # Build prior intensity lookup per theme
    prior_intensities: Dict[str, List[float]] = {}
    for snap in prior_snapshots:
        for theme_d in snap.get("themes", []):
            theme_key = theme_d.get("theme", "")
            if theme_key:
                if theme_key not in prior_intensities:
                    prior_intensities[theme_key] = []
                prior_intensities[theme_key].append(
                    float(theme_d.get("intensity", 0))
                )

    readings: List[ThemeReading] = []
    for theme_key, defn in THEME_DEFINITIONS.items():
        hits, matched_kws = _count_keyword_hits(headlines, defn["keywords"])
        intensity = _compute_intensity(hits, total)
        prior = prior_intensities.get(defn["label"], [])
        acceleration = _compute_acceleration(intensity, prior)
        persistence = _compute_persistence(intensity, prior)

        weight = float(defn.get("spx_impact_weight", 0.5))
        readings.append(ThemeReading(
            theme=defn["label"],
            key=theme_key,
            intensity=intensity,
            adjusted_intensity=round(intensity * weight, 1),
            spx_impact_weight=weight,
            persistence_days=persistence,
            acceleration=acceleration,
            affected_sectors=defn["sectors"],
            keyword_hits=hits,
            sample_keywords=matched_kws,
        ))

    # Sort by intensity descending
    readings.sort(key=lambda r: r.intensity, reverse=True)

    dominant = readings[0].theme if readings and readings[0].intensity > 0 else ""

    return NewsThemeSnapshot(
        date=date_str,
        themes=[r.to_dict() for r in readings],
        dominant_theme=dominant,
        total_headline_count=total,
    )


# ---------------------------------------------------------------------------
# Headline extraction helpers
# ---------------------------------------------------------------------------


def extract_headlines_from_eodhd(news_response: List[dict]) -> List[str]:
    """Extract headline strings from EODHD news API response.

    Discards everything except the title text.
    """
    headlines: List[str] = []
    for item in news_response:
        title = item.get("title") or ""
        if isinstance(title, str) and title.strip():
            headlines.append(title.strip())
    return headlines


def extract_headlines_from_benzinga(news_response: List[dict]) -> List[str]:
    """Extract headline strings from Benzinga news API response.

    Discards everything except the title/headline text.
    """
    headlines: List[str] = []
    for item in news_response:
        title = item.get("title") or item.get("headline") or ""
        if isinstance(title, str) and title.strip():
            headlines.append(title.strip())
    return headlines


# ---------------------------------------------------------------------------
# Redis persistence
# ---------------------------------------------------------------------------


def persist_theme_snapshot(
    snapshot: NewsThemeSnapshot,
    store: Any,
    ttl_s: int = THEME_TTL_S,
) -> bool:
    """Persist a theme snapshot to Redis."""
    if store is None:
        return False
    key = f"{THEME_KEY_PREFIX}:{snapshot.date}"
    return store.set_json(key, snapshot.to_dict(), ttl_s=ttl_s)


def load_theme_history(
    store: Any,
    n_days: int = 14,
    end_date: Optional[str] = None,
) -> List[dict]:
    """Load rolling theme snapshot history from Redis.

    Tries the last n_days dates and returns whatever is available.
    """
    if store is None:
        return []

    end = dt.date.fromisoformat(end_date) if end_date else dt.date.today()
    results: List[dict] = []

    for i in range(1, n_days + 1):
        d = end - dt.timedelta(days=i)
        key = f"{THEME_KEY_PREFIX}:{d.isoformat()}"
        data = store.get_json(key)
        if data is not None:
            results.append(data)

    return results
