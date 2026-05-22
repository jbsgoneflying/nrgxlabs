"""Decision-grade analytics for the Engine 2b Flex-Expiry payload.

Three responsibilities, kept out of ``engine.py`` so the grid loop stays
readable:

1. **Open-gap risk** — for analogue windows where the expiry session is
   the *only* held session (Fri close → Tue open after a 3-day weekend),
   the closing-only breach stats undersell real risk: a Tuesday gap-open
   can blow through the short strike before the desk can roll. This
   module computes the Fri-close → expiry-open move and reports it
   alongside the standard close-to-close stats.

2. **Segmented subsamples** — the historical odds collapse into three
   honest cohorts the desk can compare side-by-side:

   - **all** — every shape match (same entry weekday, sessions, calendar
     days), no regime / macro / holiday filter.
   - **regimeMacro** — restricted to the *current* regime + macro bucket.
     This is what the legacy ``oddsLikeNow`` returns and tends to be
     tiny (n=3-5) for unusual shapes.
   - **holidayClass** — only windows that span an NYSE holiday weekday
     (i.e. share the "1 session, 4 cal day" shape with the live trade).
   - **exactHoliday** — only windows that span the *same* holiday
     family (e.g. Memorial Day for a Memorial Day weekend trade).

3. **Breach-by-width** — for each subsample, compute breach %, full-loss
   %, and the distribution of close-vs-open MAE so the LLM/desk can see
   how the edge holds up as the cohort tightens.

All inputs come from ``week_rows`` (already-resolved historical analogue
rows) plus the ``widths`` list. No ORATS calls happen here.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, Iterable, List, Optional, Sequence


# ---------------------------------------------------------------------------
# Open-gap math
# ---------------------------------------------------------------------------

def compute_open_gap_metrics(week_rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize Fri-close → expiry-open gap stats across a row set.

    Each ``week_row`` is expected to carry ``openGapPct`` and
    ``openGapXEm`` (signed: positive = gap up vs entry close). Rows
    without an open price are skipped — the engine simply doesn't have
    that bar yet for the bleeding edge of history.
    """
    abs_pct: List[float] = []
    abs_em: List[float] = []
    signed_pct: List[float] = []
    n = 0
    n_up = 0
    n_down = 0
    n_breach_at_em15 = 0
    n_breach_at_em20 = 0
    n_breach_at_em25 = 0
    for r in week_rows or []:
        g = r.get("openGapPct")
        if g is None:
            continue
        try:
            g_pct = float(g)
        except Exception:
            continue
        n += 1
        signed_pct.append(g_pct)
        abs_pct.append(abs(g_pct))
        ge = r.get("openGapXEm")
        if ge is not None:
            try:
                abs_em.append(abs(float(ge)))
            except Exception:
                pass
        if g_pct > 0:
            n_up += 1
        elif g_pct < 0:
            n_down += 1
        if ge is not None:
            try:
                ae = abs(float(ge))
                if ae >= 1.5:
                    n_breach_at_em15 += 1
                if ae >= 2.0:
                    n_breach_at_em20 += 1
                if ae >= 2.5:
                    n_breach_at_em25 += 1
            except Exception:
                pass

    def _pct(vals: List[float], q: float) -> Optional[float]:
        if not vals:
            return None
        s = sorted(vals)
        if len(s) == 1:
            return round(s[0], 3)
        # nearest-rank percentile, safer for small n than statistics.quantiles
        idx = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
        return round(s[idx], 3)

    return {
        "n": n,
        "nUp": n_up,
        "nDown": n_down,
        "absPct": {
            "p50": _pct(abs_pct, 0.50),
            "p75": _pct(abs_pct, 0.75),
            "p90": _pct(abs_pct, 0.90),
            "p95": _pct(abs_pct, 0.95),
            "max": _pct(abs_pct, 1.00),
            "mean": round(sum(abs_pct) / len(abs_pct), 3) if abs_pct else None,
        },
        "absXEm": {
            "p50": _pct(abs_em, 0.50),
            "p75": _pct(abs_em, 0.75),
            "p90": _pct(abs_em, 0.90),
            "p95": _pct(abs_em, 0.95),
            "max": _pct(abs_em, 1.00),
            "mean": round(sum(abs_em) / len(abs_em), 3) if abs_em else None,
        },
        "gapBreachAtEM": {
            "1.5x": _safe_pct(n_breach_at_em15, n),
            "2.0x": _safe_pct(n_breach_at_em20, n),
            "2.5x": _safe_pct(n_breach_at_em25, n),
        },
        "skewBucket": _skew_bucket(signed_pct),
    }


def _skew_bucket(signed: Sequence[float]) -> str:
    """Bucket the signed-gap distribution into ``up`` / ``balanced`` / ``down``.

    Used by the UI / advisor to call out asymmetric tail risk that
    breach % alone hides.
    """
    if not signed:
        return "unknown"
    pos = sum(1 for x in signed if x > 0)
    neg = sum(1 for x in signed if x < 0)
    if pos == 0 and neg == 0:
        return "balanced"
    total = max(1, pos + neg)
    if pos / total >= 0.65:
        return "up"
    if neg / total >= 0.65:
        return "down"
    return "balanced"


def _safe_pct(num: int, den: int) -> Optional[float]:
    if not den:
        return None
    return round(100.0 * float(num) / float(den), 2)


# ---------------------------------------------------------------------------
# Subsample filters
# ---------------------------------------------------------------------------

def filter_regime_macro(
    rows: Iterable[Dict[str, Any]],
    *,
    regime_bucket: str,
    macro_bucket: str,
) -> List[Dict[str, Any]]:
    return [
        r for r in rows
        if str(r.get("regimeBucket")) == regime_bucket
        and str(r.get("macroBucket")) == macro_bucket
    ]


def filter_holiday_class(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in rows if bool(r.get("spansHoliday"))]


def filter_exact_holiday(
    rows: Iterable[Dict[str, Any]],
    *,
    holiday_label: Optional[str],
) -> List[Dict[str, Any]]:
    if not holiday_label:
        return []
    return [
        r for r in rows
        if str(r.get("holidayLabel") or "").lower() == str(holiday_label).lower()
    ]


# ---------------------------------------------------------------------------
# Breach by width — close-only and close-or-open
# ---------------------------------------------------------------------------

def breach_by_width(
    rows: Sequence[Dict[str, Any]],
    *,
    widths: Sequence[float],
) -> List[Dict[str, Any]]:
    """Compute breach % per EM-multiple for a row set.

    Returns one row per width with:
      - ``n``: sample size
      - ``closeBreachPct``: expiry close outside ±(w × EM)
      - ``openOrCloseBreachPct``: expiry close OR expiry open outside
        ±(w × EM). For 1-session windows the open is the actual
        first opportunity to react.
      - ``maxIntraHoldEm``: p95 of the intra-window MAE expressed in EM
        units (from the engine's ``maeAbsEm`` field).
      - ``meanGapXEm``: average absolute open-gap measured in EM units.
    """
    out: List[Dict[str, Any]] = []
    for w in widths:
        try:
            wf = float(w)
        except Exception:
            continue
        n = 0
        nb_close = 0
        nb_either = 0
        mae_em_vals: List[float] = []
        gap_em_vals: List[float] = []
        for r in rows:
            try:
                ret = float(r.get("retPct"))
                em1 = float(r.get("em1sigmaPct"))
            except Exception:
                continue
            if em1 <= 0:
                continue
            n += 1
            dist = wf * em1
            close_breach = abs(ret) > dist
            if close_breach:
                nb_close += 1
            # Open-or-close breach uses the expiry-open gap when available.
            gap_pct = r.get("openGapPct")
            open_breach = False
            if gap_pct is not None:
                try:
                    open_breach = abs(float(gap_pct)) > dist
                except Exception:
                    open_breach = False
            if close_breach or open_breach:
                nb_either += 1
            mae_em = r.get("maeAbsEm")
            if mae_em is not None:
                try:
                    mae_em_vals.append(float(mae_em))
                except Exception:
                    pass
            gap_em = r.get("openGapXEm")
            if gap_em is not None:
                try:
                    gap_em_vals.append(abs(float(gap_em)))
                except Exception:
                    pass

        out.append({
            "w": wf,
            "n": n,
            "closeBreachPct": _safe_pct(nb_close, n),
            "openOrCloseBreachPct": _safe_pct(nb_either, n),
            "maeP95XEm": _quantile(mae_em_vals, 0.95),
            "meanGapXEm": round(sum(gap_em_vals) / len(gap_em_vals), 3) if gap_em_vals else None,
        })
    return out


def _quantile(vals: List[float], q: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return round(s[0], 3)
    idx = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return round(s[idx], 3)


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------

def build_flex_analytics(
    *,
    week_rows: Sequence[Dict[str, Any]],
    widths: Sequence[float],
    regime_bucket_now: str,
    macro_bucket_now: str,
    target_holiday_label: Optional[str],
    target_spans_holiday: bool,
) -> Dict[str, Any]:
    """Return a structured analytics block for the flex payload.

    Layout:
      {
        "subsamples": {
          "all":           { n, breach: [...], openGap: {...} },
          "regimeMacro":   { n, ... },
          "holidayClass":  { n, ... },          # only when target spans a holiday
          "exactHoliday":  { n, label, ... },   # only when target spans a holiday
        },
        "primary": "<bucket name>",   # which subsample the desk should weight
        "notes": [...],
      }

    The ``primary`` selection follows a small ladder: prefer the
    smallest cohort that still has ``>=5`` rows; otherwise fall back to
    the next-wider one. This matches how the desk reads "n=3" — small-n
    cohorts are advisory only; the broader pool keeps the breach % from
    looking heroic on a coin-flip sample.
    """
    rows = list(week_rows or [])
    notes: List[str] = []

    all_rows = rows
    rm_rows = filter_regime_macro(rows, regime_bucket=regime_bucket_now, macro_bucket=macro_bucket_now)
    hc_rows = filter_holiday_class(rows) if target_spans_holiday else []
    eh_rows = filter_exact_holiday(rows, holiday_label=target_holiday_label) if target_spans_holiday else []

    def _bundle(label: str, sub_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "n": len(sub_rows),
            "label": label,
            "breach": breach_by_width(sub_rows, widths=widths),
            "openGap": compute_open_gap_metrics(sub_rows),
        }

    subsamples: Dict[str, Any] = {
        "all": _bundle("All shape matches", all_rows),
        "regimeMacro": _bundle(
            f"Current regime ({regime_bucket_now}) + macro ({macro_bucket_now})",
            rm_rows,
        ),
    }
    if target_spans_holiday:
        subsamples["holidayClass"] = _bundle(
            "Any NYSE holiday spans this shape",
            hc_rows,
        )
        eh_label = target_holiday_label or "Holiday family"
        eh_bundle = _bundle(f"Exact holiday: {eh_label}", eh_rows)
        eh_bundle["holidayLabel"] = target_holiday_label
        subsamples["exactHoliday"] = eh_bundle

    # Decide a "primary" cohort the UI/advisor should anchor on. Tightest
    # cohort that still has n>=5 wins; otherwise broaden until we have one.
    primary = "all"
    ladder = ["exactHoliday", "holidayClass", "regimeMacro", "all"]
    chosen = None
    for key in ladder:
        if key not in subsamples:
            continue
        b = subsamples[key]
        if int(b.get("n") or 0) >= 5:
            chosen = key
            break
    if chosen is None:
        # Nothing has >=5 — pick the largest sample so the user sees a
        # meaningful breach %, with a "thin sample" note attached.
        biggest = max(
            (k for k in ladder if k in subsamples),
            key=lambda k: int(subsamples[k].get("n") or 0),
            default="all",
        )
        chosen = biggest
        notes.append("Thin historical sample (no cohort >= 5). Treat breach % as directional, not statistical.")
    primary = chosen

    if target_spans_holiday and not eh_rows:
        notes.append(
            f"No prior {target_holiday_label or 'exact-holiday'} analogues in lookback; "
            "exactHoliday cohort is empty — use holidayClass or regimeMacro."
        )

    return {
        "subsamples": subsamples,
        "primary": primary,
        "notes": notes,
    }


__all__ = [
    "build_flex_analytics",
    "breach_by_width",
    "compute_open_gap_metrics",
    "filter_exact_holiday",
    "filter_holiday_class",
    "filter_regime_macro",
]
