"""Data structures + universe taxonomy loader for the AI Capex Reality Engine.

Two core records flow through the engine:

- ``CapexEvidence`` — one structured, source-attributed observation about AI
  capex (extracted by the LLM from a transcript / news item / web doc, or read
  straight from fundamentals). This is the audit trail.
- ``TickerVerdict`` — the deterministic roll-up per ticker: a Reality Score, a
  Consensus Gap, one of the six desk labels, and any derived trade ideas.

Nothing here calls an API or an LLM. The scorer turns ``CapexEvidence`` into
``TickerVerdict`` with pure, unit-tested math so the labels are reproducible
from the evidence table (the platform's "LLM never drives sizing" guardrail).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Enumerations (kept as plain strings so they serialise cleanly to JSON/Redis)
# ---------------------------------------------------------------------------

SOURCE_TRANSCRIPT = "transcript"
SOURCE_NEWS = "news"
SOURCE_WEB = "web"
SOURCE_FUNDAMENTAL = "fundamental"
VALID_SOURCE_TYPES = {SOURCE_TRANSCRIPT, SOURCE_NEWS, SOURCE_WEB, SOURCE_FUNDAMENTAL}

# Signal types the extractor is allowed to emit.
SIG_CAPEX_UP = "capex_up"
SIG_CAPEX_DOWN = "capex_down"
SIG_SUPPLY_CONSTRAINT = "supply_constraint"
SIG_DELAY = "delay"
SIG_DEMAND_PULL = "demand_pull"
SIG_HYPE = "hype_language"
SIG_SECOND_ORDER = "second_order_link"
VALID_SIGNAL_TYPES = {
    SIG_CAPEX_UP, SIG_CAPEX_DOWN, SIG_SUPPLY_CONSTRAINT, SIG_DELAY,
    SIG_DEMAND_PULL, SIG_HYPE, SIG_SECOND_ORDER,
}

# Signals that count as "hard, positive capex reality" when corroborated.
POSITIVE_HARD_SIGNALS = {SIG_CAPEX_UP, SIG_SUPPLY_CONSTRAINT, SIG_DEMAND_PULL}
NEGATIVE_HARD_SIGNALS = {SIG_CAPEX_DOWN}

TIMING_NEAR = "near"   # this/next quarter — already happening
TIMING_MID = "mid"     # next 2-4 quarters
TIMING_FAR = "far"     # 12m+ out / aspirational
VALID_TIMINGS = {TIMING_NEAR, TIMING_MID, TIMING_FAR}

# Desk labels (the engine's output vocabulary).
LABEL_REAL = "real_beneficiary"
LABEL_DELAYED = "delayed_beneficiary"
LABEL_OVERHYPED = "overhyped_beneficiary"
LABEL_SECOND_ORDER_WINNER = "second_order_winner"
LABEL_SECOND_ORDER_LOSER = "second_order_loser"
LABEL_CONSENSUS_NOT_UPDATED = "consensus_not_updated"
LABEL_NEUTRAL = "neutral"

LABEL_DISPLAY: Dict[str, str] = {
    LABEL_REAL: "Real beneficiary",
    LABEL_DELAYED: "Delayed beneficiary",
    LABEL_OVERHYPED: "Overhyped beneficiary",
    LABEL_SECOND_ORDER_WINNER: "Second-order winner",
    LABEL_SECOND_ORDER_LOSER: "Second-order loser",
    LABEL_CONSENSUS_NOT_UPDATED: "Consensus has not updated yet",
    LABEL_NEUTRAL: "No clear signal",
}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass
class CapexEvidence:
    """One source-attributed observation about AI capex for a ticker."""

    ticker: str
    category: str
    source_type: str          # transcript | news | web | fundamental
    signal_type: str          # see VALID_SIGNAL_TYPES
    claim: str                # short paraphrase of the evidence
    date: str = ""            # YYYY-MM-DD of the source
    source_url: str = ""      # provenance link (esp. for web-sourced docs)
    source_title: str = ""    # headline / doc title
    magnitude: float = 0.5    # 0..1 — how big/material the claim is
    timing: str = TIMING_MID  # near | mid | far
    polarity: int = 1         # +1 bullish capex, -1 bearish, 0 neutral
    confidence: float = 0.5   # 0..1 — extractor's confidence the claim is real
    corroboration_count: int = 1  # independent sources making the same claim
    evidence_id: str = ""     # stable hash, filled in __post_init__

    def __post_init__(self) -> None:
        self.ticker = str(self.ticker or "").upper().strip()
        self.category = str(self.category or "").strip()
        if self.source_type not in VALID_SOURCE_TYPES:
            self.source_type = SOURCE_NEWS
        if self.signal_type not in VALID_SIGNAL_TYPES:
            self.signal_type = SIG_DEMAND_PULL
        if self.timing not in VALID_TIMINGS:
            self.timing = TIMING_MID
        self.magnitude = _clamp(float(self.magnitude or 0.0), 0.0, 1.0)
        self.confidence = _clamp(float(self.confidence or 0.0), 0.0, 1.0)
        try:
            self.polarity = int(self.polarity)
        except (TypeError, ValueError):
            self.polarity = 1
        if self.polarity not in (-1, 0, 1):
            self.polarity = 1 if self.polarity > 0 else (-1 if self.polarity < 0 else 0)
        self.corroboration_count = max(1, int(self.corroboration_count or 1))
        if not self.evidence_id:
            self.evidence_id = self._stable_id()

    def _stable_id(self) -> str:
        basis = "|".join([
            self.ticker, self.signal_type, self.timing,
            (self.claim or "")[:120].lower().strip(),
            (self.source_url or self.source_title or "")[:160],
        ])
        return "ev_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:14]

    @property
    def is_hard_positive(self) -> bool:
        return self.polarity > 0 and self.signal_type in POSITIVE_HARD_SIGNALS

    @property
    def is_hard_negative(self) -> bool:
        return (self.polarity < 0 and self.signal_type in POSITIVE_HARD_SIGNALS) \
            or self.signal_type in NEGATIVE_HARD_SIGNALS

    @property
    def is_hype(self) -> bool:
        return self.signal_type == SIG_HYPE

    def to_dict(self) -> dict:
        return {
            "evidenceId": self.evidence_id,
            "ticker": self.ticker,
            "category": self.category,
            "sourceType": self.source_type,
            "signalType": self.signal_type,
            "claim": self.claim,
            "date": self.date,
            "sourceUrl": self.source_url,
            "sourceTitle": self.source_title,
            "magnitude": round(self.magnitude, 3),
            "timing": self.timing,
            "polarity": self.polarity,
            "confidence": round(self.confidence, 3),
            "corroborationCount": self.corroboration_count,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CapexEvidence":
        return cls(
            ticker=d.get("ticker") or d.get("Ticker") or "",
            category=d.get("category") or "",
            source_type=d.get("sourceType") or d.get("source_type") or SOURCE_NEWS,
            signal_type=d.get("signalType") or d.get("signal_type") or SIG_DEMAND_PULL,
            claim=d.get("claim") or "",
            date=str(d.get("date") or "")[:10],
            source_url=d.get("sourceUrl") or d.get("source_url") or "",
            source_title=d.get("sourceTitle") or d.get("source_title") or "",
            magnitude=d.get("magnitude", 0.5),
            timing=d.get("timing") or TIMING_MID,
            polarity=d.get("polarity", 1),
            confidence=d.get("confidence", 0.5),
            corroboration_count=d.get("corroborationCount") or d.get("corroboration_count") or 1,
            evidence_id=d.get("evidenceId") or d.get("evidence_id") or "",
        )


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass
class TickerVerdict:
    """Deterministic roll-up for one ticker."""

    ticker: str
    category: str
    label: str = LABEL_NEUTRAL
    reality_score: float = 0.0     # 0..100 — strength of corroborated capex reality
    consensus_gap: float = 0.0     # -100..100 — reality vs market positioning
    hype_ratio: float = 0.0        # 0..1 — share of evidence that is bare hype
    direction: str = "neutral"     # long | short | neutral
    conviction: float = 0.0        # 0..100 — for Desk Brain sizing
    evidence_count: int = 0
    evidence_ids: List[str] = field(default_factory=list)
    top_evidence: List[dict] = field(default_factory=list)
    trade_ideas: List[dict] = field(default_factory=list)
    market_context: Dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    @property
    def display_label(self) -> str:
        return LABEL_DISPLAY.get(self.label, self.label)

    @property
    def is_actionable(self) -> bool:
        return self.label != LABEL_NEUTRAL and self.conviction > 0

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "category": self.category,
            "label": self.label,
            "labelDisplay": self.display_label,
            "realityScore": round(self.reality_score, 1),
            "consensusGap": round(self.consensus_gap, 1),
            "hypeRatio": round(self.hype_ratio, 3),
            "direction": self.direction,
            "conviction": round(self.conviction, 1),
            "evidenceCount": self.evidence_count,
            "evidenceIds": list(self.evidence_ids),
            "topEvidence": list(self.top_evidence),
            "tradeIdeas": list(self.trade_ideas),
            "marketContext": dict(self.market_context),
            "rationale": self.rationale,
        }


# ---------------------------------------------------------------------------
# Universe taxonomy loader
# ---------------------------------------------------------------------------


def _universe_path() -> Path:
    override = os.getenv("AI_CAPEX_UNIVERSE_PATH")
    if override:
        return Path(override)
    # Lives in the package dir (NOT data/) on purpose: the prod `data/` is a
    # persisted Docker volume that shadows image contents, so a taxonomy file
    # placed there would never appear in the container. This is code/config,
    # so it ships with the package at backend/ai_capex/universe.json.
    return Path(__file__).resolve().parent / "universe.json"


@lru_cache(maxsize=1)
def load_universe() -> Dict[str, Any]:
    """Load + lightly validate the taxonomy JSON. Cached for the process."""
    path = _universe_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {"categories": {}, "second_order_edges": {}, "second_order_losers": {}, "hype_keywords": []}
    if not isinstance(data, dict):
        return {"categories": {}, "second_order_edges": {}, "second_order_losers": {}, "hype_keywords": []}
    data.setdefault("categories", {})
    data.setdefault("second_order_edges", {})
    data.setdefault("second_order_losers", {})
    data.setdefault("hype_keywords", [])
    return data


def category_of(ticker: str) -> Optional[str]:
    """Return the taxonomy category id for a ticker (first match), else None."""
    t = str(ticker or "").upper().strip()
    cats = load_universe().get("categories", {})
    for cat_id, meta in cats.items():
        for tk in (meta.get("tickers") or []):
            if str(tk).upper().strip() == t:
                return cat_id
    return None


def category_role(category: str) -> str:
    meta = load_universe().get("categories", {}).get(category, {})
    return str(meta.get("role") or "second_order")


def category_name(category: str) -> str:
    meta = load_universe().get("categories", {}).get(category, {})
    return str(meta.get("name") or category)


def all_tickers() -> List[str]:
    """Every ticker in the universe, de-duplicated, stable order."""
    seen: Dict[str, None] = {}
    for meta in load_universe().get("categories", {}).values():
        for tk in (meta.get("tickers") or []):
            seen.setdefault(str(tk).upper().strip(), None)
    return [t for t in seen if t]


def ticker_to_category_map() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for cat_id, meta in load_universe().get("categories", {}).items():
        for tk in (meta.get("tickers") or []):
            out.setdefault(str(tk).upper().strip(), cat_id)
    return out


def hype_keywords() -> List[str]:
    return [str(k).lower() for k in load_universe().get("hype_keywords", [])]


def second_order_beneficiaries(driver_category: str) -> List[Dict[str, Any]]:
    """Beneficiary categories (with weights) for a driver category."""
    edges = load_universe().get("second_order_edges", {})
    rows = edges.get(driver_category) or []
    return [r for r in rows if isinstance(r, dict) and r.get("category")]
