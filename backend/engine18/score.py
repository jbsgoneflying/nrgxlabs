"""Engine 18 deterministic scoring — buckets, sizing tiers, expected edge.

Pure functions, no I/O. Every number in ``COHORT_STATS`` comes straight from
the validated Edge Bake-Off live run (backend/research/reports/pead.json +
overlay_pead.json, run 2026-06-09, S&P 500 universe, 2018-2026, 10bps/side):

    full sample        n=2125  +0.43%/trade  hit 53.4%  t=2.75
    OOS 2023+          n= 843  +0.65%/trade  hit 52.3%  t=2.70
    beat_large         n= 548  +1.05%/trade  hit 56.2%  t=2.97
    beat_small         n=1181  +0.47%/trade  hit 53.9%  t=2.55
    misses (n=396)     -0.57%/trade -> long-only, never shorted
    quality Q5         n=  63  +1.45%/trade  hit 60.3%  (overlay, large beats)
    quality Q1         n=  64  +0.18%/trade  hit 54.7%

Sizing matrix (per the validated quality overlay):
    beat_large + Q4/Q5            -> full
    beat_large + Q3               -> half
    beat_small + Q4/Q5            -> half
    everything else               -> pass
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from backend.engine18.models import (
    DriftCandidate,
    EarningsReport,
    QualityGrade,
    add_business_days,
    next_business_day,
)

# Validated cohort stats (percent per trade, fraction hit rate).
COHORT_STATS: Dict[str, Dict[str, Any]] = {
    "beat_large": {"avgNetPct": 1.05, "hitRate": 0.562, "n": 548, "tStat": 2.97},
    "beat_small": {"avgNetPct": 0.47, "hitRate": 0.539, "n": 1181, "tStat": 2.55},
    "oos_2023_plus": {"avgNetPct": 0.65, "hitRate": 0.523, "n": 843, "tStat": 2.70},
    "quality_q5": {"avgNetPct": 1.45, "hitRate": 0.603, "n": 63},
    "quality_q4": {"avgNetPct": 1.20, "hitRate": 0.587, "n": 63},
}

_HIGH_QUINTILES = ("Q4", "Q5")


def surprise_bucket(
    surprise_pct: Optional[float],
    *,
    min_surprise: float,
    large_surprise: float,
) -> Optional[str]:
    """Return ``beat_small`` / ``beat_large`` for qualifying beats, else None.

    Misses and sub-threshold beats are not candidates: the bake-off showed
    misses LOSE money (avg -0.57%/trade) and shorting them also loses.
    """
    if surprise_pct is None:
        return None
    s = float(surprise_pct)
    if s < float(min_surprise):
        return None
    return "beat_large" if s >= float(large_surprise) else "beat_small"


def sizing_tier(bucket: str, quintile: str) -> str:
    """Deterministic sizing from the validated surprise x quality matrix."""
    if bucket == "beat_large":
        if quintile in _HIGH_QUINTILES:
            return "full"
        if quintile == "Q3":
            return "half"
        return "pass"
    if bucket == "beat_small":
        if quintile in _HIGH_QUINTILES:
            return "half"
        return "pass"
    return "pass"


def expected_stats(bucket: str, quintile: str) -> Dict[str, Any]:
    """Expected-edge display stats for a candidate, straight from the cohorts."""
    base = COHORT_STATS.get(bucket) or {}
    out: Dict[str, Any] = {
        "bucket": bucket,
        "bucketAvgNetPct": base.get("avgNetPct"),
        "bucketHitRate": base.get("hitRate"),
        "bucketN": base.get("n"),
        "bucketTStat": base.get("tStat"),
        "source": "edge-bakeoff 2026-06-09, OOS 2023+ +0.65%/trade t=2.7 n=843",
    }
    if quintile == "Q5":
        out["qualityAvgNetPct"] = COHORT_STATS["quality_q5"]["avgNetPct"]
        out["qualityHitRate"] = COHORT_STATS["quality_q5"]["hitRate"]
    elif quintile == "Q4":
        out["qualityAvgNetPct"] = COHORT_STATS["quality_q4"]["avgNetPct"]
        out["qualityHitRate"] = COHORT_STATS["quality_q4"]["hitRate"]
    return out


def score_candidate(
    report: EarningsReport,
    grade: QualityGrade,
    *,
    min_surprise: float,
    large_surprise: float,
    hold_days: int,
    adv_usd: Optional[float] = None,
    last_close: Optional[float] = None,
    regime_context: Optional[str] = None,
) -> Optional[DriftCandidate]:
    """Build a scored candidate, or None if the report doesn't qualify."""
    bucket = surprise_bucket(
        report.surprise_pct, min_surprise=min_surprise, large_surprise=large_surprise
    )
    if bucket is None:
        return None
    entry = next_business_day(report.report_date)
    return DriftCandidate(
        ticker=report.ticker,
        report=report,
        grade=grade,
        bucket=bucket,
        sizing=sizing_tier(bucket, grade.quintile),
        entry_date=entry,
        exit_date=add_business_days(entry, int(hold_days)),
        hold_days=int(hold_days),
        adv_usd=adv_usd,
        last_close=last_close,
        expected=expected_stats(bucket, grade.quintile),
        regime_context=regime_context,
    )
