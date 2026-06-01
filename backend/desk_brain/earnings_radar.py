"""Earnings Radar — which mega-cap names report in the next N days.

Design principle (mirrors the rest of Desk Brain): **facts are deterministic,
the LLM only adds judgment.**

- The *facts* — which $100B+ companies report, the exact report date, and
  BMO/AMC timing — come from EODHD's mega-cap earnings calendar
  (``eodhd_earnings_calendar.get_earnings_calendar``). The LLM never invents or
  recites a date; that is the failure mode the desk's old calendar suffered.
- The *judgment* — which of those reporters are most worth running E1/E15 on
  (materiality, sector clustering, what to prep) — is an optional LLM layer on
  top of a deterministic base score (market-cap rank x report proximity).

Output feeds Desk Brain's volatility/income sleeve so the book auto-flags the
names to run earnings-IC signals on, instead of the trader hand-entering dates.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("desk_brain.earnings_radar")

_MEGA_CAP_FLOOR = 100_000_000_000.0  # $100B (matches EODHD universe screen)
_MEGA_CAP_CEIL = 3_000_000_000_000.0  # $3T anchors the top of the cap score


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _cap_component(market_cap: float) -> float:
    """0..1 on a log scale: $100B -> 0, ~$3T -> 1."""
    if market_cap <= _MEGA_CAP_FLOOR:
        return 0.0
    span = math.log10(_MEGA_CAP_CEIL / _MEGA_CAP_FLOOR)
    return _clamp(math.log10(market_cap / _MEGA_CAP_FLOOR) / span, 0.0, 1.0)


def _proximity_component(days_to_report: int, window_days: int) -> float:
    """Sooner = more actionable. Today -> ~1.0, far end of window -> ~0."""
    if window_days <= 0:
        return 1.0
    return _clamp(1.0 - (max(0, days_to_report) / float(window_days)), 0.0, 1.0)


def _base_materiality(market_cap: float, days_to_report: int, window_days: int) -> float:
    """Deterministic 0..100 score: 60% cap weight, 40% proximity weight."""
    score = 0.60 * _cap_component(market_cap) + 0.40 * _proximity_component(days_to_report, window_days)
    return round(100.0 * score, 1)


def _fetch_reporters(start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
    """Factual mega-cap reporters for [start, end] from EODHD. Never raises."""
    try:
        from backend.eodhd_earnings_calendar import get_earnings_calendar

        days = get_earnings_calendar(start, end)
    except Exception as exc:
        LOG.warning("earnings_radar: EODHD calendar fetch failed: %s", exc)
        return []

    out: List[Dict[str, Any]] = []
    for report_date in sorted(days.keys()):
        for entry in days[report_date]:
            try:
                rd = dt.date.fromisoformat(str(entry.get("report_date"))[:10])
                days_to = (rd - start).days
            except (ValueError, TypeError):
                days_to = 0
            out.append({
                "ticker": entry.get("ticker"),
                "name": entry.get("name"),
                "reportDate": report_date,
                "timing": entry.get("timing_label") or "TBD",
                "marketCap": float(entry.get("market_cap") or 0.0),
                "sector": entry.get("sector") or "",
                "daysToReport": days_to,
            })
    return out


def get_earnings_radar(
    *,
    days_ahead: int = 7,
    with_llm: bool = True,
    as_of: Optional[dt.date] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the next-``days_ahead`` mega-cap earnings radar.

    Each reporter carries factual date/timing/market-cap plus a 0..100
    ``materiality`` score. With ``with_llm`` the score blends a deterministic
    base with an LLM materiality read and a desk narrative; without it the
    base score stands alone.
    """
    start = as_of or dt.date.today()
    end = start + dt.timedelta(days=max(1, days_ahead))

    reporters = _fetch_reporters(start, end)
    for r in reporters:
        r["baseMateriality"] = _base_materiality(r["marketCap"], r["daysToReport"], days_ahead)
        r["materiality"] = r["baseMateriality"]
        r["llmMateriality"] = None

    narrative: Dict[str, Any] = {}
    if with_llm and reporters:
        narrative = _llm_layer(reporters, regime=None, model=model)
        scores = narrative.get("materiality") or {}
        if isinstance(scores, dict):
            for r in reporters:
                llm_score = scores.get(r["ticker"])
                if isinstance(llm_score, (int, float)):
                    r["llmMateriality"] = round(float(llm_score), 1)
                    # Blend: deterministic base anchors, LLM judgment adjusts.
                    r["materiality"] = round(0.5 * r["baseMateriality"] + 0.5 * float(llm_score), 1)

    reporters.sort(key=lambda r: (-r["materiality"], r["reportDate"], r["ticker"]))

    return {
        "asOf": start.isoformat(),
        "windowDays": days_ahead,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": len(reporters),
        "reporters": reporters,
        "narrative": {k: v for k, v in narrative.items() if k != "materiality"},
        "llmSource": narrative.get("_source", "none") if narrative else "none",
    }


def _llm_layer(
    reporters: List[Dict[str, Any]],
    *,
    regime: Optional[str],
    model: Optional[str],
) -> Dict[str, Any]:
    """Call the earnings-radar LLM card. Never raises."""
    try:
        from backend.front_layer_llm import generate_earnings_radar_synthesis

        context = {
            "regime": regime,
            "reporters": [
                {
                    "ticker": r["ticker"], "name": r["name"], "reportDate": r["reportDate"],
                    "timing": r["timing"], "marketCapB": round(r["marketCap"] / 1e9, 1),
                    "sector": r["sector"], "daysToReport": r["daysToReport"],
                }
                for r in reporters
            ],
        }
        return generate_earnings_radar_synthesis(context, model=model)
    except Exception as exc:
        LOG.warning("earnings_radar: LLM layer failed: %s", exc)
        return {"_source": "fallback", "_fallback_reason": str(exc)}
