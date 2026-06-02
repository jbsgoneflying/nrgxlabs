"""Timeframe / catalyst estimation for the AI Capex Reality Engine.

Puts a *defensible* timeframe on each actionable verdict — a probabilistic
**horizon window anchored to a datable catalyst**, NOT a precise day-count
prediction (which markets don't support and a desk shouldn't trust).

Two inputs, both already in hand:

- The verdict's **evidence timing mix** (near/mid/far shares) — the structural
  horizon: capex that's happening *now* vs aspirational.
- **ORATS cores** — the next earnings date (``nextErn``), which is the cleanest
  datable catalyst (most capex repricing lands on the print), plus implied vol
  so we can compare the option-implied move over that window to the thesis move.

Everything is deterministic and defensive: missing ORATS data degrades to a
structural horizon (no catalyst / implied-move), never an exception. Like the
rest of the engine, no LLM output drives any of this.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Dict, Optional

from backend.ai_capex import models
from backend.ai_capex.models import TickerVerdict

LOG = logging.getLogger("ai_capex.horizon")

# Labels whose edge resolves at a discrete catalyst (the next print): the gap
# closes when consensus updates, or the overhyped name disappoints on guidance.
_CATALYST_LABELS = {models.LABEL_CONSENSUS_NOT_UPDATED, models.LABEL_OVERHYPED}

# Term-IV cores fields (for the structural implied move when there's no event).
_IV_FIELDS = "ticker,iv30d,iv60d,iv90d,ivRank"


def _f(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_orats_timing(ticker: str, orats_client: Any) -> Dict[str, Any]:
    """Pull the next-earnings catalyst + implied moves from ORATS.

    The catalyst date comes via the platform's shared resolver, which tries
    ORATS ``/cores`` ``nextErn`` and then falls back to scanning
    ``/hist/earnings`` for the next future date — ``nextErn`` alone is often
    subscription-gated/stale, so the fallback is what actually lands the date.
    That resolver also returns ``impErnMv`` (the implied *earnings* move), which
    is the right event-specific number for the catalyst comparison; term IV
    (``iv30d`` …) is kept only for the no-event structural fallback.

    Returns ``{}`` when ORATS is unavailable or the name isn't covered.
    """
    if orats_client is None:
        return {}
    out: Dict[str, Any] = {}

    try:
        from backend.engine8_e1_bridge import resolve_next_earnings
        ern = resolve_next_earnings(orats_client, str(ticker).upper())
        if ern:
            if ern.get("earnings_date"):
                out["nextErn"] = str(ern["earnings_date"])[:10]
            if ern.get("days_to_earnings") is not None:
                out["daysToNextErn"] = int(ern["days_to_earnings"])
            if ern.get("timing"):
                out["nextErnTod"] = ern["timing"]
            if ern.get("expected_move_pct") is not None:
                out["impErnMvPct"] = float(ern["expected_move_pct"])
    except Exception as exc:  # pragma: no cover - defensive
        LOG.debug("orats earnings resolve failed for %s: %s", ticker, exc)

    try:
        resp = orats_client.cores(ticker=str(ticker).upper(), fields=_IV_FIELDS)
        rows = getattr(resp, "rows", None) or []
        if rows and isinstance(rows[0], dict):
            r = rows[0]
            for k in ("iv30d", "iv60d", "iv90d", "ivRank"):
                val = _f(r.get(k))
                if val is not None:
                    out[k] = val
    except Exception as exc:  # pragma: no cover - defensive
        LOG.debug("orats iv fetch failed for %s: %s", ticker, exc)

    return out


def _iv_decimal(orats: Dict[str, Any], days: Optional[int]) -> Optional[float]:
    """Annualised IV (decimal, e.g. 0.45) at roughly the relevant tenor."""
    if not orats:
        return None
    if days is not None and days > 75 and orats.get("iv90d") is not None:
        raw = orats["iv90d"]
    elif days is not None and days > 45 and orats.get("iv60d") is not None:
        raw = orats["iv60d"]
    else:
        raw = orats.get("iv30d")
    raw = _f(raw)
    if raw is None or raw <= 0:
        return None
    return raw / 100.0 if raw > 3.0 else raw  # accept either decimal or percent


def _implied_move_pct(orats: Dict[str, Any], days: Optional[int]) -> Optional[float]:
    """Option-implied move over ``days`` from term IV: iv * sqrt(t/365)."""
    iv = _iv_decimal(orats, days)
    if iv is None:
        return None
    t = max(5, int(days)) if days else 21
    return round(iv * math.sqrt(t / 365.0) * 100.0, 1)


def _thesis_move_pct(verdict: TickerVerdict) -> Optional[float]:
    """Heuristic target move = the mispricing the gap implies, floored/capped.

    The consensus gap is how far reality leads (or trails) market positioning;
    the trade is that gap closing. Scaled conservatively — a *target*, not a
    forecast."""
    gap = abs(_f(verdict.consensus_gap) or 0.0)
    if gap < 1.0:
        return None
    return round(min(25.0, max(3.0, gap * 0.22)), 1)


def _parse_date(s: str) -> Optional[dt.date]:
    try:
        return dt.datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _structural_band(timing_mix: Dict[str, float]) -> str:
    near = float(timing_mix.get("near", 0.0))
    far = float(timing_mix.get("far", 0.0))
    if near >= 0.5:
        return "~2–8 weeks"
    if far >= 0.5:
        return "multi-quarter (structural)"
    return "~1–2 quarters"


def _catalyst_band(days: int) -> str:
    if days <= 10:
        return f"into next earnings (~{days}d)"
    if days <= 21:
        return "~0–3 weeks (into earnings)"
    if days <= 45:
        return "~3–6 weeks"
    if days <= 90:
        return "~1–3 months"
    return "~1 quarter"


def _assess(thesis: Optional[float], implied: Optional[float]) -> Optional[str]:
    if thesis is None or implied is None or implied <= 0:
        return None
    if thesis >= implied * 1.25:
        return "underpriced"
    if thesis <= implied * 0.8:
        return "rich"
    return "fair"


def derive_horizon(
    verdict: TickerVerdict, orats: Optional[Dict[str, Any]] = None, *, today: Optional[dt.date] = None,
) -> Dict[str, Any]:
    """Build the horizon block for one verdict (empty dict if not actionable)."""
    if not verdict.is_actionable or verdict.direction == "neutral":
        # "Delayed" is honestly date-uncertain — say so rather than fake a date.
        if verdict.label == models.LABEL_DELAYED:
            return {
                "band": "~2–4 quarters (date-uncertain)",
                "basis": "uncertain",
                "note": "Right thesis, wrong quarter — wait for a capex/interconnect milestone before sizing.",
            }
        return {}

    orats = orats or {}
    today = today or dt.date.today()
    timing_mix = verdict.timing_mix or {}
    catalyst_driven = verdict.label in _CATALYST_LABELS

    out: Dict[str, Any] = {
        "nearShare": round(float(timing_mix.get("near", 0.0)), 2),
    }

    # Catalyst anchor (next earnings) from ORATS.
    days: Optional[int] = None
    ern = _parse_date(str(orats.get("nextErn") or ""))
    if ern is not None:
        days = (ern - today).days
        if days < 0:
            days = None  # stale snapshot; ignore
    if days is not None:
        out["catalystDate"] = ern.isoformat()
        out["daysToCatalyst"] = days
        tod = orats.get("nextErnTod")
        out["catalyst"] = f"Next earnings ~{ern.isoformat()}" + (f" ({tod})" if tod else "")

    # Band: catalyst-driven labels anchor to the print when we have a date;
    # otherwise fall back to the structural (evidence-timing) horizon.
    if catalyst_driven and days is not None and days <= 120:
        out["band"] = _catalyst_band(days)
        out["basis"] = "catalyst"
    elif catalyst_driven and days is not None:
        out["band"] = "~1–2 quarters"
        out["basis"] = "catalyst"
    else:
        out["band"] = _structural_band(timing_mix)
        out["basis"] = "structural"

    # Implied move vs thesis target, so the desk sees whether the move is
    # underpriced. For catalyst-driven names prefer the ORATS *implied earnings
    # move* (the single-event jump) — the right comparison; otherwise fall back
    # to a term-IV move over the band's nominal horizon.
    implied: Optional[float] = None
    if catalyst_driven and orats.get("impErnMvPct") is not None:
        implied = round(float(orats["impErnMvPct"]), 1)
    if implied is None:
        nominal_days = days if days is not None else (35 if out["basis"] != "structural" else 63)
        implied = _implied_move_pct(orats, nominal_days)
    thesis = _thesis_move_pct(verdict)
    if implied is not None:
        out["impliedMovePct"] = implied
    if thesis is not None:
        out["thesisMovePct"] = thesis
    assessment = _assess(thesis, implied)
    if assessment:
        out["assessment"] = assessment

    out["note"] = _note(verdict, out, catalyst_driven)
    return out


def _note(verdict: TickerVerdict, h: Dict[str, Any], catalyst_driven: bool) -> str:
    bits = []
    if catalyst_driven and h.get("daysToCatalyst") is not None:
        bits.append(f"Edge typically resolves on the next print ({h['daysToCatalyst']}d).")
    elif verdict.label == models.LABEL_REAL:
        bits.append("Already partly priced — expect a multi-quarter drift, not a single event.")
    elif verdict.label in (models.LABEL_SECOND_ORDER_WINNER, models.LABEL_SECOND_ORDER_LOSER):
        bits.append("Second-order — lags the driver category by a quarter or so.")
    imp, th, a = h.get("impliedMovePct"), h.get("thesisMovePct"), h.get("assessment")
    if imp is not None and th is not None and a:
        verb = {"underpriced": "underprices", "rich": "already prices", "fair": "fairly prices"}.get(a, "prices")
        bits.append(f"Options imply ±{imp}% over the window; thesis targets ~{th}% — market {verb} the move.")
    return " ".join(bits)
