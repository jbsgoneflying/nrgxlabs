"""Engine 18 — pure-logic tests: buckets, quintiles, sizing tiers, grading.

No network, no Redis: everything here is the deterministic layer the desk
relies on for reproducibility (the LLM only contributes the quality score,
which is injected as a fake here).
"""
from __future__ import annotations

from backend.engine18.grade import (
    COLD_START_SCORES,
    grade_transcript,
    quintile_for,
)
from backend.engine18.models import (
    EarningsReport,
    QualityGrade,
    add_business_days,
    candidates_to_payload,
    next_business_day,
)
from backend.engine18.score import (
    COHORT_STATS,
    expected_stats,
    score_candidate,
    sizing_tier,
    surprise_bucket,
)


# ---------------------------------------------------------------------------
# Surprise buckets
# ---------------------------------------------------------------------------


def test_surprise_bucket_thresholds():
    kw = dict(min_surprise=0.05, large_surprise=0.20)
    assert surprise_bucket(0.30, **kw) == "beat_large"
    assert surprise_bucket(0.20, **kw) == "beat_large"   # boundary inclusive
    assert surprise_bucket(0.10, **kw) == "beat_small"
    assert surprise_bucket(0.05, **kw) == "beat_small"   # floor inclusive
    assert surprise_bucket(0.04, **kw) is None           # sub-threshold beat
    assert surprise_bucket(-0.30, **kw) is None          # misses NEVER trade
    assert surprise_bucket(None, **kw) is None


# ---------------------------------------------------------------------------
# Sizing matrix (the validated surprise x quality grid)
# ---------------------------------------------------------------------------


def test_sizing_matrix():
    assert sizing_tier("beat_large", "Q5") == "full"
    assert sizing_tier("beat_large", "Q4") == "full"
    assert sizing_tier("beat_large", "Q3") == "half"
    assert sizing_tier("beat_large", "Q2") == "pass"
    assert sizing_tier("beat_large", "Q1") == "pass"
    assert sizing_tier("beat_small", "Q5") == "half"
    assert sizing_tier("beat_small", "Q4") == "half"
    assert sizing_tier("beat_small", "Q3") == "pass"
    assert sizing_tier("beat_small", "Q1") == "pass"
    assert sizing_tier("", "Q5") == "pass"


def test_expected_stats_anchored_to_cohorts():
    e = expected_stats("beat_large", "Q5")
    assert e["bucketAvgNetPct"] == COHORT_STATS["beat_large"]["avgNetPct"]
    assert e["qualityAvgNetPct"] == COHORT_STATS["quality_q5"]["avgNetPct"]
    e2 = expected_stats("beat_small", "Q2")
    assert e2["bucketAvgNetPct"] == COHORT_STATS["beat_small"]["avgNetPct"]
    assert "qualityAvgNetPct" not in e2


# ---------------------------------------------------------------------------
# Quintiles vs trailing distribution
# ---------------------------------------------------------------------------


def test_quintile_cold_start_uses_seed():
    # Empty trailing -> cold-start seed distribution drives the buckets.
    assert quintile_for(0.95, []) == "Q5"
    assert quintile_for(0.05, []) == "Q1"


def test_quintile_live_distribution_dominates_when_deep():
    # 100 trailing scores clustered high: a 0.55 is now bottom-of-pack.
    trailing = [0.7 + 0.002 * i for i in range(100)]
    assert quintile_for(0.55, trailing) == "Q1"
    assert quintile_for(0.95, trailing) == "Q5"


def test_quintile_blends_seed_when_thin():
    # Below the live threshold the seed is still blended in.
    thin = [0.9] * 5
    q = quintile_for(0.5, thin)
    assert q in ("Q2", "Q3")  # mid-pack vs seed, not Q1 vs the 5 high scores


# ---------------------------------------------------------------------------
# Transcript grading (LLM injected, heuristic always logged)
# ---------------------------------------------------------------------------


def test_grade_transcript_llm_primary():
    g = grade_transcript(
        "we raise guidance and expect record growth",
        model="test-model",
        trailing=[],
        llm_fn=lambda text: (0.9, "Strong guidance raise."),
    )
    assert g.source == "llm"
    assert g.score == 0.9
    assert g.rationale == "Strong guidance raise."
    assert g.transcript_found is True
    assert 0.0 <= g.heuristic_score <= 1.0  # heuristic always computed


def test_grade_transcript_falls_back_to_heuristic():
    g = grade_transcript(
        "we raise guidance and expect strong record growth momentum",
        model="test-model",
        trailing=[],
        llm_fn=lambda text: None,  # LLM unavailable
    )
    assert g.source == "heuristic"
    assert g.score == g.heuristic_score
    assert g.score > 0.5  # bullish keywords dominate


def test_grade_transcript_no_text_is_neutral():
    g = grade_transcript("", model="test-model", trailing=[], llm_fn=lambda t: (1.0, "x"))
    assert g.source == "none"
    assert g.score == 0.5
    assert g.transcript_found is False


# ---------------------------------------------------------------------------
# Candidate scoring + dates
# ---------------------------------------------------------------------------


def _report(surprise: float, date: str = "2026-06-05") -> EarningsReport:
    return EarningsReport(
        ticker="ABC", report_date=date, timing="amc",
        actual_eps=1.3, estimate_eps=1.0, surprise_pct=surprise,
    )


def test_score_candidate_full_path():
    grade = QualityGrade(score=0.9, source="llm", quintile="Q5", transcript_found=True)
    c = score_candidate(
        _report(0.30), grade,
        min_surprise=0.05, large_surprise=0.20, hold_days=10,
        adv_usd=50e6, last_close=100.0,
    )
    assert c is not None
    assert c.bucket == "beat_large"
    assert c.sizing == "full"
    # Friday 2026-06-05 report -> Monday 2026-06-08 entry, +10 trading days exit.
    assert c.entry_date == "2026-06-08"
    assert c.exit_date == "2026-06-22"
    assert c.expected["bucketAvgNetPct"] == 1.05


def test_score_candidate_rejects_miss_and_small():
    grade = QualityGrade(quintile="Q5")
    assert score_candidate(_report(-0.20), grade, min_surprise=0.05, large_surprise=0.20, hold_days=10) is None
    assert score_candidate(_report(0.02), grade, min_surprise=0.05, large_surprise=0.20, hold_days=10) is None


def test_business_day_helpers():
    assert next_business_day("2026-06-05") == "2026-06-08"  # Fri -> Mon
    assert next_business_day("2026-06-08") == "2026-06-09"  # Mon -> Tue
    assert add_business_days("2026-06-08", 10) == "2026-06-22"


def test_payload_sorting_and_summary():
    g5 = QualityGrade(score=0.9, quintile="Q5")
    g1 = QualityGrade(score=0.1, quintile="Q1")
    full = score_candidate(_report(0.30), g5, min_surprise=0.05, large_surprise=0.20, hold_days=10)
    halfer = score_candidate(
        EarningsReport(ticker="DEF", report_date="2026-06-05", surprise_pct=0.10,
                       actual_eps=1.1, estimate_eps=1.0),
        g5, min_surprise=0.05, large_surprise=0.20, hold_days=10,
    )
    passer = score_candidate(
        EarningsReport(ticker="GHI", report_date="2026-06-05", surprise_pct=0.50,
                       actual_eps=1.5, estimate_eps=1.0),
        g1, min_surprise=0.05, large_surprise=0.20, hold_days=10,
    )
    payload = candidates_to_payload([passer, halfer, full])
    assert payload["summary"] == {"candidates": 3, "actionable": 2, "fullSize": 1, "halfSize": 1}
    # full first, then half, then pass.
    assert [c["sizing"] for c in payload["candidates"]] == ["full", "half", "pass"]


def test_cold_start_seed_is_sane():
    assert len(COLD_START_SCORES) >= 20
    assert all(0.0 <= s <= 1.0 for s in COLD_START_SCORES)
    assert COLD_START_SCORES == sorted(COLD_START_SCORES)
