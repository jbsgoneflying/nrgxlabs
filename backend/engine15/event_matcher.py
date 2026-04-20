"""Engine 15 — filter + rank historical earnings windows.

Gating policy:

1.  **AnncTod parity (hard gate).** Mixing a forward BMO trade with
    historical AMC prints is the single biggest fidelity killer for an
    earnings IC — the mechanics differ (intraday pop vs overnight gap).
    Forward BMO/AMC events only match history with the same timing.
    Forward ``UNK`` drops into a "mixed pool" and carries an explicit
    confidence penalty in the payload.

2.  **Quarter / season filter (optional).** When the user scopes to a
    specific quarter we drop off-quarter history. Rarely useful but
    occasionally the desk asks for Q4 earnings only.

3.  **EM-multiple placement coverage (optional).** If the user's short
    strikes are unusually far (|z|>2σ) and the analogue's chain only
    covers |z|<1.5σ we can't map the strikes safely — drop the
    analogue. Disabled by default to maximize sample size for single
    names.

Ranking is reverse-chronological (most recent earnings first) for the
UI, but the simulator considers all admissible events equally.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from backend.engine15.event_universe import EarningsWindow

LOG = logging.getLogger("engine15.event_matcher")


__all__ = ["MatchCriteria", "filter_events"]


@dataclass(frozen=True)
class MatchCriteria:
    user_annc_tod: str                          # "BMO" | "AMC" | "UNK"
    season_mode: str = "none"                    # "none" | "quarter" | "month"
    season_value: Optional[str] = None           # "Q2" or month int as str
    strict_annc_tod: bool = True
    # EM-multiple coverage filter — requires the replay adapter to have
    # populated ``entry_close`` and the per-window coverage dict below.
    target_em_multiple: Optional[float] = None
    em_multiple_tol: float = 0.35
    enable_em_multiple_filter: bool = False


def _annc_ok(user: str, cand: str, strict: bool) -> bool:
    u = (user or "").upper()
    c = (cand or "").upper()
    if u == "UNK" or not strict:
        return True
    return c == u


def _season_ok(criteria: MatchCriteria, w: EarningsWindow) -> bool:
    mode = (criteria.season_mode or "none").lower()
    if mode == "none" or mode == "":
        return True
    if not criteria.season_value:
        return True
    val = str(criteria.season_value).strip().upper()
    if mode == "quarter":
        return (w.quarter or "").upper() == val
    if mode == "month":
        try:
            return int(w.month or 0) == int(val)
        except Exception:
            return True
    return True


def _em_coverage_ok(
    criteria: MatchCriteria,
    w: EarningsWindow,
    coverage: Optional[Dict[str, Tuple[float, float]]],
) -> bool:
    if not criteria.enable_em_multiple_filter or criteria.target_em_multiple is None:
        return True
    if not coverage:
        # No coverage info means we can't reject confidently — let it through.
        return True
    cov = coverage.get(w.earn_date_hist)
    if not cov:
        return True
    lo, hi = cov
    z = float(criteria.target_em_multiple)
    tol = float(criteria.em_multiple_tol)
    return (lo - tol) <= z <= (hi + tol)


def filter_events(
    events: List[EarningsWindow],
    *,
    criteria: MatchCriteria,
    coverage_by_event: Optional[Dict[str, Tuple[float, float]]] = None,
) -> Tuple[List[EarningsWindow], List[Dict[str, str]]]:
    """Apply gates and return (admitted, dropped) where ``dropped`` is a
    list of ``{earnDate, reason}`` dicts suitable for the UI caveat row."""
    admitted: List[EarningsWindow] = []
    dropped: List[Dict[str, str]] = []
    for w in events:
        if not _annc_ok(criteria.user_annc_tod, w.annc_tod, criteria.strict_annc_tod):
            dropped.append({
                "earnDate": w.earn_date_hist,
                "reason": f"anncTod mismatch ({w.annc_tod} vs user {criteria.user_annc_tod})",
            })
            continue
        if not _season_ok(criteria, w):
            dropped.append({
                "earnDate": w.earn_date_hist,
                "reason": f"season mismatch (mode={criteria.season_mode}, "
                          f"value={criteria.season_value}, window={w.quarter}/{w.month})",
            })
            continue
        if not _em_coverage_ok(criteria, w, coverage_by_event):
            cov = (coverage_by_event or {}).get(w.earn_date_hist)
            dropped.append({
                "earnDate": w.earn_date_hist,
                "reason": (
                    f"strike EM-multiple outside chain coverage "
                    f"(target |z|={criteria.target_em_multiple:.2f}, "
                    f"cov={cov[0]:.2f}..{cov[1]:.2f})"
                ) if cov else "no chain coverage on entry date",
            })
            continue
        admitted.append(w)
    return admitted, dropped
