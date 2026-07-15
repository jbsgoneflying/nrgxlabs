"""Data-quality gate for the Equity Repricing Lab.

Research runs refuse to start when ``has_critical_failures`` is true.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.config import get_flags
from backend.repricing_lab import store

LOG = logging.getLogger("repricing_lab.qa")


@dataclass
class QAFinding:
    domain: str
    severity: str  # critical | warning | info
    code: str
    message: str
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class QAReport:
    as_of: str
    findings: List[QAFinding] = field(default_factory=list)
    coverage: Dict[str, Any] = field(default_factory=dict)

    def has_critical_failures(self) -> bool:
        return any(f.severity == "critical" for f in self.findings)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "as_of": self.as_of,
            "hasCriticalFailures": self.has_critical_failures(),
            "coverage": self.coverage,
            "findings": [asdict(f) for f in self.findings],
        }


def run_qa(conn, *, as_of: Optional[str] = None) -> QAReport:
    as_of = as_of or store.utcnow_iso()
    report = QAReport(as_of=as_of)

    n_inst = conn.execute("SELECT COUNT(*) FROM instrument_master").fetchone()[0]
    n_bars = conn.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
    n_ca = conn.execute("SELECT COUNT(*) FROM corporate_action").fetchone()[0]
    n_univ = conn.execute("SELECT COUNT(*) FROM universe_snapshot").fetchone()[0]
    n_earn = conn.execute("SELECT COUNT(*) FROM earnings_event").fetchone()[0]
    bar_span = conn.execute(
        "SELECT MIN(session_date), MAX(session_date), COUNT(DISTINCT instrument_id) FROM daily_bar"
    ).fetchone()
    report.coverage = {
        "instruments": int(n_inst or 0),
        "dailyBars": int(n_bars or 0),
        "corporateActions": int(n_ca or 0),
        "universeRows": int(n_univ or 0),
        "earningsEvents": int(n_earn or 0),
        "barMinDate": bar_span[0],
        "barMaxDate": bar_span[1],
        "barInstruments": int(bar_span[2] or 0),
    }

    if n_inst == 0:
        report.findings.append(QAFinding(
            "instruments", "critical", "no_instruments",
            "instrument_master is empty — run backfill instruments first",
        ))
    if n_bars == 0:
        report.findings.append(QAFinding(
            "bars", "critical", "no_bars",
            "daily_bar is empty — historical backfill required before research",
        ))
    else:
        # Duplicate (instrument, session) should be impossible under PK, but check adj anomalies.
        null_adj = conn.execute(
            "SELECT COUNT(*) FROM daily_bar WHERE adjusted_close IS NULL AND close IS NOT NULL"
        ).fetchone()[0]
        if null_adj:
            report.findings.append(QAFinding(
                "bars", "warning", "null_adjusted_close",
                f"{null_adj} bars have close but null adjusted_close",
                {"count": int(null_adj)},
            ))
        # Gap detection sample: instruments with < 50% of expected weekday span (cheap heuristic).
        thin = conn.execute(
            """
            SELECT instrument_id, COUNT(*) AS n
            FROM daily_bar
            GROUP BY instrument_id
            HAVING n < 20
            LIMIT 50
            """
        ).fetchall()
        if thin:
            report.findings.append(QAFinding(
                "bars", "warning", "thin_history",
                f"{len(thin)} instruments have <20 bars",
                {"sample": [r[0] for r in thin[:10]]},
            ))

    # Split ratio sanity
    bad_splits = conn.execute(
        """
        SELECT COUNT(*) FROM corporate_action
        WHERE action_type='split' AND (ratio_or_amount IS NULL OR ratio_or_amount <= 0)
        """
    ).fetchone()[0]
    if bad_splits:
        report.findings.append(QAFinding(
            "corporate_actions", "critical", "invalid_split_ratio",
            f"{bad_splits} split rows have null/non-positive ratio",
            {"count": int(bad_splits)},
        ))

    if n_univ == 0 and n_bars > 0:
        report.findings.append(QAFinding(
            "universe", "warning", "no_universe_snapshot",
            "bars exist but universe_snapshot is empty — run universe builder",
        ))

    # estimate_is_pit coverage once earnings exist
    if n_earn > 0:
        pit = conn.execute(
            "SELECT SUM(estimate_is_pit), COUNT(*) FROM earnings_event"
        ).fetchone()
        pit_n, total = int(pit[0] or 0), int(pit[1] or 0)
        if total and pit_n / total < 0.5:
            report.findings.append(QAFinding(
                "earnings", "warning", "estimate_not_pit",
                f"Only {pit_n}/{total} earnings rows marked estimate_is_pit=1 — "
                "surprise features must be gated or haircut",
                {"pit": pit_n, "total": total},
            ))

    return report


def write_qa_report(report: QAReport, *, path: Optional[str] = None) -> str:
    flags = get_flags()
    runs = Path(str(getattr(flags, "REPRICING_LAB_RUNS_DIR", "data/lab_runs")))
    if not runs.is_absolute():
        root = Path(__file__).resolve().parent.parent.parent
        runs = (root / runs).resolve()
    runs.mkdir(parents=True, exist_ok=True)
    if path is None:
        path = str(runs / f"qa-{report.as_of[:10]}.json")
    with open(path, "w") as fh:
        json.dump(report.to_dict(), fh, indent=2, sort_keys=True)
    LOG.info("qa report written %s (critical=%s)", path, report.has_critical_failures())
    return path


def assert_qa_clean_for_research(conn, *, as_of: Optional[str] = None) -> QAReport:
    report = run_qa(conn, as_of=as_of)
    if report.has_critical_failures():
        codes = [f.code for f in report.findings if f.severity == "critical"]
        raise RuntimeError(f"repricing_lab QA critical failures: {codes}")
    return report
