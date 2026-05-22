"""Tests for backend.engine2b.flex_analytics.

The analytics module is what turns the raw historical analogue rows
into the decision-grade cohort breakdown the desk reads before sizing.
We pin:

- Open-gap percentile math + skew bucketing.
- The four subsamples (all / regimeMacro / holidayClass / exactHoliday).
- Breach-by-width using both close-only and open-or-close definitions.
- The "primary cohort" ladder (smallest cohort with n >= 5 wins).
"""
from __future__ import annotations

from backend.engine2b.flex_analytics import (
    breach_by_width,
    build_flex_analytics,
    compute_open_gap_metrics,
    filter_exact_holiday,
    filter_holiday_class,
)


def _row(
    *,
    entry: str,
    expiry: str,
    ret_pct: float,
    em1: float,
    regime: str = "MODERATE",
    macro: str = "NORMAL",
    spans_holiday: bool = False,
    holiday_label: str | None = None,
    gap_pct: float | None = None,
    mae_em: float | None = None,
) -> dict:
    return {
        "entryDate": entry,
        "expiryDate": expiry,
        "retPct": float(ret_pct),
        "em1sigmaPct": float(em1),
        "regimeBucket": regime,
        "macroBucket": macro,
        "spansHoliday": bool(spans_holiday),
        "holidayLabel": holiday_label,
        "openGapPct": gap_pct,
        "openGapXEm": (None if gap_pct is None else float(gap_pct) / float(em1)),
        "maeAbsEm": mae_em,
    }


def test_open_gap_percentiles_and_skew():
    rows = [
        _row(entry="2025-05-23", expiry="2025-05-27", ret_pct=0.4, em1=1.0, gap_pct=0.6, mae_em=1.2),
        _row(entry="2024-05-24", expiry="2024-05-28", ret_pct=-0.2, em1=1.1, gap_pct=-0.3, mae_em=0.8),
        _row(entry="2023-05-26", expiry="2023-05-30", ret_pct=0.5, em1=0.9, gap_pct=0.9, mae_em=1.0),
    ]
    m = compute_open_gap_metrics(rows)
    assert m["n"] == 3
    assert m["nUp"] == 2 and m["nDown"] == 1
    # max abs gap is 0.9
    assert m["absPct"]["max"] == 0.9
    # absXEm should also be present
    assert m["absXEm"]["max"] is not None and m["absXEm"]["max"] > 0
    # 2/3 are positive — skewBucket should call out an "up" tilt.
    assert m["skewBucket"] == "up"


def test_open_gap_handles_missing_gaps_gracefully():
    rows = [
        _row(entry="2025-05-23", expiry="2025-05-27", ret_pct=0.4, em1=1.0, gap_pct=None),
        _row(entry="2024-05-24", expiry="2024-05-28", ret_pct=-0.2, em1=1.1, gap_pct=0.2),
    ]
    m = compute_open_gap_metrics(rows)
    assert m["n"] == 1  # only one row had an openGapPct


def test_filter_holiday_class_and_exact_holiday():
    rows = [
        _row(entry="2025-05-23", expiry="2025-05-27", ret_pct=0.1, em1=1.0,
             spans_holiday=True, holiday_label="Memorial Day"),
        _row(entry="2025-01-17", expiry="2025-01-21", ret_pct=0.2, em1=1.0,
             spans_holiday=True, holiday_label="MLK Day"),
        _row(entry="2025-02-21", expiry="2025-02-25", ret_pct=0.0, em1=1.0,
             spans_holiday=False, holiday_label=None),
    ]
    hc = filter_holiday_class(rows)
    assert len(hc) == 2
    eh = filter_exact_holiday(rows, holiday_label="Memorial Day")
    assert len(eh) == 1
    assert eh[0]["holidayLabel"] == "Memorial Day"


def test_breach_by_width_close_only_vs_open_or_close():
    """Construct two rows where the open gap breaches but the close does not.
    For a 1σ EM of 1.0%, a +2.5% gap breaches 2.0× wing but the close at
    -0.3% does not. We expect closeBreachPct=0%, openOrCloseBreachPct=100%
    at the 2.0× width.
    """
    rows = [
        _row(entry="2025-05-23", expiry="2025-05-27", ret_pct=-0.3, em1=1.0, gap_pct=2.5),
    ]
    bw = breach_by_width(rows, widths=[1.0, 2.0])
    one = next(r for r in bw if r["w"] == 2.0)
    assert one["closeBreachPct"] == 0.0
    assert one["openOrCloseBreachPct"] == 100.0


def test_build_flex_analytics_picks_primary_with_n_ge_5_when_available():
    rows = []
    # 6 holiday-spanning rows in MODERATE/NORMAL bucket, Memorial Day.
    for d in ("2024-05-24", "2024-08-30", "2023-05-26", "2025-05-23", "2024-02-16", "2025-01-17"):
        label = "Memorial Day" if "05" in d else ("Labor Day" if "08" in d else ("MLK Day" if "01" in d else "Presidents' Day"))
        rows.append(_row(entry=d, expiry=d, ret_pct=0.1, em1=1.0, spans_holiday=True, holiday_label=label, gap_pct=0.2))
    # Plus 8 non-holiday rows for the 'all' cohort.
    for i in range(8):
        rows.append(_row(entry=f"2024-04-{10+i:02d}", expiry=f"2024-04-{14+i:02d}", ret_pct=0.0, em1=1.0))

    out = build_flex_analytics(
        week_rows=rows,
        widths=[1.0, 2.0],
        regime_bucket_now="MODERATE",
        macro_bucket_now="NORMAL",
        target_holiday_label="Memorial Day",
        target_spans_holiday=True,
    )
    assert "all" in out["subsamples"]
    assert "holidayClass" in out["subsamples"]
    assert "exactHoliday" in out["subsamples"]
    # exactHoliday should have n=3 (Memorial Day rows). holidayClass=6. regimeMacro=14.
    assert out["subsamples"]["exactHoliday"]["n"] == 3
    assert out["subsamples"]["holidayClass"]["n"] == 6
    # primary ladder: exactHoliday has n=3 (<5), so we move to holidayClass (n=6, >=5).
    assert out["primary"] == "holidayClass"


def test_build_flex_analytics_flags_thin_sample():
    rows = [
        _row(entry="2024-05-24", expiry="2024-05-28", ret_pct=0.1, em1=1.0,
             spans_holiday=True, holiday_label="Memorial Day", gap_pct=0.2),
    ]
    out = build_flex_analytics(
        week_rows=rows,
        widths=[1.0],
        regime_bucket_now="MODERATE",
        macro_bucket_now="NORMAL",
        target_holiday_label="Memorial Day",
        target_spans_holiday=True,
    )
    # Nothing has n>=5 so the analytics block should call this out.
    assert any("thin" in (n or "").lower() for n in out["notes"])


def test_build_flex_analytics_no_holiday_for_non_holiday_trade():
    rows = [
        _row(entry="2025-03-17", expiry="2025-03-21", ret_pct=0.1, em1=1.0,
             spans_holiday=False, gap_pct=0.2),
    ]
    out = build_flex_analytics(
        week_rows=rows,
        widths=[1.0],
        regime_bucket_now="MODERATE",
        macro_bucket_now="NORMAL",
        target_holiday_label=None,
        target_spans_holiday=False,
    )
    assert "holidayClass" not in out["subsamples"]
    assert "exactHoliday" not in out["subsamples"]
