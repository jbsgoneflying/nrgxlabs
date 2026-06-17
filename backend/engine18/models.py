"""Engine 18 dataclasses."""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EarningsReport:
    """A fresh earnings report pulled from the calendar (pre-grading)."""

    ticker: str
    report_date: str            # YYYY-MM-DD
    timing: str = ""            # "bmo" | "amc" | "" (unknown)
    actual_eps: Optional[float] = None
    estimate_eps: Optional[float] = None
    surprise_pct: Optional[float] = None  # (actual - est) / |est|

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EarningsReport":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


@dataclass
class QualityGrade:
    """Transcript quality assessment for one report."""

    score: float = 0.5              # 0..1 (1 = strongly bullish forward quality)
    source: str = "none"            # "llm" | "heuristic" | "none" (no transcript)
    heuristic_score: float = 0.5    # always computed — comparative validation log
    quintile: str = "Q3"            # Q1 (worst) .. Q5 (best) vs trailing distribution
    rationale: str = ""             # short LLM rationale for the card
    transcript_found: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "QualityGrade":
        return cls(**{k: d.get(k, cls.__dataclass_fields__[k].default) for k in cls.__dataclass_fields__})


@dataclass
class DriftCandidate:
    """A scored long-drift candidate (the unit the desk acts on)."""

    ticker: str
    report: EarningsReport = field(default_factory=lambda: EarningsReport("", ""))
    grade: QualityGrade = field(default_factory=QualityGrade)
    bucket: str = ""                # "beat_small" | "beat_large"
    sizing: str = "pass"            # "full" | "half" | "pass"
    entry_date: str = ""            # next session open after report
    exit_date: str = ""             # entry + hold_days trading days
    hold_days: int = 10
    adv_usd: Optional[float] = None
    last_close: Optional[float] = None
    expected: Dict[str, Any] = field(default_factory=dict)  # validated cohort stats
    regime_context: Optional[str] = None                    # display only, never a gate
    origin: str = "scan"            # "scan" (auto) | "manual" (on-demand profile)
    entry_status: str = ""          # "" | "on_time" | "late" (validated entry passed)
    days_late: int = 0              # business days past the validated entry open
    eps_source: str = "eodhd"       # "eodhd" | "fmp" | "manual" (agent override)

    # PEAD is long-only — the bake-off showed misses lose money and shorting
    # them also loses, so direction is fixed and surfaced for the desk plan.
    direction: str = "long"

    def decision(self) -> str:
        """Deterministic GO / NO_GO / CAUTION verdict the desk acts on.

        Mirrors the validated sizing matrix: only full/half-size tiers are
        capital-committable (GO). A pass tier is NO_GO. A qualifying signal
        whose validated entry open has already passed is CAUTION — the
        mid-drift entry was never backtested, so the desk must review.
        """
        if self.sizing not in ("full", "half"):
            return "NO_GO"
        if self.entry_status == "late":
            return "CAUTION"
        return "GO"

    def confidence(self) -> str:
        """Plain-language confidence tier derived from the sizing matrix."""
        return {"full": "High", "half": "Moderate"}.get(self.sizing, "Low")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["decision"] = self.decision()
        d["confidence"] = self.confidence()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DriftCandidate":
        rep = EarningsReport.from_dict(d.get("report") or {})
        gr = QualityGrade.from_dict(d.get("grade") or {})
        out = cls(ticker=str(d.get("ticker") or ""), report=rep, grade=gr)
        for k in ("bucket", "sizing", "entry_date", "exit_date", "hold_days",
                  "adv_usd", "last_close", "expected", "regime_context",
                  "origin", "entry_status", "days_late", "eps_source", "direction"):
            if k in d:
                setattr(out, k, d[k])
        return out


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_business_day(date: str) -> str:
    d = dt.date.fromisoformat(date[:10]) + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d.isoformat()


def add_business_days(date: str, n: int) -> str:
    d = dt.date.fromisoformat(date[:10])
    added = 0
    while added < n:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d.isoformat()


def candidates_to_payload(
    candidates: List[DriftCandidate],
    *,
    as_of: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the scan payload served by the API."""
    actionable = [c for c in candidates if c.sizing in ("full", "half")]
    return {
        "engine": 18,
        "name": "Earnings Drift (PEAD)",
        "asOf": as_of or utcnow_iso(),
        "summary": {
            "candidates": len(candidates),
            "actionable": len(actionable),
            "fullSize": sum(1 for c in candidates if c.sizing == "full"),
            "halfSize": sum(1 for c in candidates if c.sizing == "half"),
        },
        "candidates": [c.to_dict() for c in sorted(
            candidates,
            key=lambda c: ({"full": 0, "half": 1, "pass": 2}.get(c.sizing, 3),
                           -(c.report.surprise_pct or 0.0)),
        )],
        "meta": meta or {},
        "validation": None,  # filled from e18:validation:latest at serve time
    }
