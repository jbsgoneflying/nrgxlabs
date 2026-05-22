"""Weekend Stress Gauge — pre-trade detection of priced-in weekend risk.

The flex cohort math assumes "this weekend looks like the last 8 holiday
weekends in the sample." That assumption fails the moment the options
market starts pricing an asymmetric catalyst (Iran headlines, sovereign
event, surprise central-bank action over a 4-day weekend). This module
catches that *before* you enter by reading two orthogonal options-market
signals:

1. **Term-structure inversion.** ATM IV of the near expiry (the trade's
   expiry, which spans the holiday) vs ATM IV of the next Friday after
   it. Annualized IV uses calendar-day convention, so for a normal
   holiday weekend the longer-dated IV runs 1-3 vol pts above the
   shorter — pure time-value. When that flips (shorter > longer, or
   shorter ≈ longer), the market is overweighting the near window:
   weekend-gap premium being bid.

2. **25Δ put skew on the near chain.** Steeper-than-normal put skew
   (25Δ put IV well above ATM IV) means downside-fear hedging demand —
   exactly what an Iran-escalation scenario would produce.

The composite ``level`` is the *worst of* the two reads, so a single
signal can flip the gauge even if the other is benign. Returned as a
clean ``weekendStress`` block surfaced both in the engine payload and
to the flex advisor, so the LLM can cite it directly in ``riskContext``.

Reads are conditional on ``target_shape["spansHoliday"] is True`` — for
non-holiday flex trades the gauge is N/A and not computed.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional, Tuple

from backend.orats_client import OratsClient
from backend.spx_ic.live_levels import _filter_chain_by_expiry
from backend.spx_ic.utils import _to_float

LOG = logging.getLogger("engine2b.weekend_stress")


_CHAIN_FIELDS = (
    "ticker,tradeDate,expirDate,strike,spotPrice,stockPrice,"
    "callBidPrice,callAskPrice,callMidIv,callDelta,"
    "putBidPrice,putAskPrice,putMidIv,putDelta"
)


# ---------------------------------------------------------------------------
# Classification thresholds
# ---------------------------------------------------------------------------
# Term-spread = (near ATM IV) - (next-Friday ATM IV), in vol percentage
# points. The historical baseline for non-holiday weeks is ~ -2pts to
# -1pts (longer-dated trades higher). For holiday weekends a slight
# inversion (-0.5 to +1pt) is normal due to calendar-day annualization.
# We start flagging at +1pt (near is bid above next-Fri) and escalate
# to SEVERE at +3pts.

_TERM_THRESHOLDS = (
    (3.0, "SEVERE"),
    (1.0, "ELEVATED"),
    (-1.0, "MODERATE"),
)

# 25Δ put skew = (put-25Δ IV) - (ATM IV) on the near chain, in vol
# percentage points. SPX 5-7 DTE skew typically runs +2pt to +4pt.
# Anything north of +5pt is hedging demand; +8pt is panic-grade.

_SKEW_THRESHOLDS = (
    (8.0, "SEVERE"),
    (5.0, "ELEVATED"),
    (3.0, "MODERATE"),
)

_LEVEL_RANK = {"NORMAL": 0, "MODERATE": 1, "ELEVATED": 2, "SEVERE": 3, "UNKNOWN": -1}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_iv_pct(iv: Optional[float]) -> Optional[float]:
    """Return IV in vol percentage points (e.g. 12.5 not 0.125).

    ORATS serves IVs as decimal fractions, but we defensively handle the
    edge case where a value lands > 1.5 (already in percent).
    """
    if iv is None:
        return None
    try:
        v = float(iv)
    except Exception:
        return None
    if v <= 0:
        return None
    return round(v * 100.0, 4) if v < 1.5 else round(v, 4)


def _classify(value: Optional[float], thresholds: Tuple[Tuple[float, str], ...]) -> str:
    """Map a value to a level using ordered ``(threshold, label)`` pairs.

    Returns ``"NORMAL"`` when value is below every threshold (and is not
    None) and ``"UNKNOWN"`` when value is missing.
    """
    if value is None:
        return "UNKNOWN"
    for cutoff, label in thresholds:
        if value >= cutoff:
            return label
    return "NORMAL"


def _composite_level(*levels: str) -> str:
    """Worst-of categorical aggregation across the input levels."""
    ranks = [_LEVEL_RANK.get(l, -1) for l in levels]
    ranks = [r for r in ranks if r >= 0]
    if not ranks:
        return "UNKNOWN"
    worst = max(ranks)
    return {0: "NORMAL", 1: "MODERATE", 2: "ELEVATED", 3: "SEVERE"}[worst]


def next_friday_after(d: dt.date) -> dt.date:
    """Return the first Friday strictly after ``d``.

    For a same-day Friday the helper returns the *following* Friday so
    the comparison expiry is always strictly later than the trade's
    expiry.
    """
    days = (4 - d.weekday() + 7) % 7
    if days == 0:
        days = 7
    return d + dt.timedelta(days=days)


def _pull_chain(
    client: OratsClient,
    *,
    ticker: str,
    expiry_str: str,
) -> Tuple[List[Dict[str, Any]], Optional[float], Optional[str]]:
    """Pull a live chain for ``expiry_str``. Falls back through SPXW → SPX → SPY.

    Returns ``(rows, spot, symbol_used)``.
    """
    t = str(ticker).strip().upper()
    if t == "SPX":
        syms = ("SPXW", "SPX", "SPY")
    elif t == "QQQ":
        syms = ("QQQ",)
    else:
        syms = (t,)
    for sym in syms:
        rows: List[Dict[str, Any]] = []
        try:
            if callable(getattr(client, "live_strikes_by_expiry", None)):
                resp = client.live_strikes_by_expiry(ticker=sym, expiry=expiry_str, fields=_CHAIN_FIELDS)
                rows = [r for r in (getattr(resp, "rows", None) or []) if isinstance(r, dict)]
        except Exception:
            rows = []
        if not rows:
            try:
                resp = client.live_strikes(ticker=sym, fields=_CHAIN_FIELDS)
                all_rows = [r for r in (getattr(resp, "rows", None) or []) if isinstance(r, dict)]
                rows = _filter_chain_by_expiry(all_rows, expiry=expiry_str)
            except Exception:
                rows = []
        if not rows:
            continue
        spot: Optional[float] = None
        for r in rows:
            s = _to_float(r.get("spotPrice")) or _to_float(r.get("stockPrice"))
            if s and s > 0:
                spot = float(s)
                break
        if spot is None:
            continue
        return rows, spot, sym
    return [], None, None


def _atm_iv(rows: List[Dict[str, Any]], spot: float) -> Optional[float]:
    """Average put/call mid IV at the strike closest to spot, normalized to vol pts."""
    best: Optional[Dict[str, Any]] = None
    best_dist = float("inf")
    for r in rows:
        k = _to_float(r.get("strike"))
        if k is None or k <= 0:
            continue
        d = abs(float(k) - float(spot))
        if d < best_dist:
            best_dist = d
            best = r
    if best is None:
        return None
    civ = _normalize_iv_pct(_to_float(best.get("callMidIv")))
    piv = _normalize_iv_pct(_to_float(best.get("putMidIv")))
    vals = [x for x in (civ, piv) if x is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _put_skew_25d_pts(rows: List[Dict[str, Any]], *, atm_iv_pct: float) -> Optional[float]:
    """Return (25Δ put IV - ATM IV) in vol pts.

    ``atm_iv_pct`` is the spot-anchored ATM IV from :func:`_atm_iv`. We
    measure skew against that anchor so the metric is comparable to the
    term-spread number.
    """
    best: Optional[Dict[str, Any]] = None
    best_dist = float("inf")
    for r in rows:
        pd = _to_float(r.get("putDelta"))
        if pd is None:
            continue
        d = abs(abs(float(pd)) - 0.25)
        if d < best_dist:
            best_dist = d
            best = r
    if best is None:
        return None
    iv25 = _normalize_iv_pct(_to_float(best.get("putMidIv")))
    if iv25 is None:
        return None
    return round(float(iv25) - float(atm_iv_pct), 4)


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------

def compute_weekend_stress(
    client: OratsClient,
    *,
    ticker: str,
    today: dt.date,
    near_expiry: dt.date,
    comparison_expiry: Optional[dt.date] = None,
) -> Dict[str, Any]:
    """Compute the weekend stress gauge for a holiday-spanning trade.

    Args:
        client: ORATS client.
        ticker: SPX | SPY | QQQ — SPXW used as the primary SPX symbol.
        today: As-of date for the read.
        near_expiry: The trade's expiry (e.g. Tue 5/26 for a Memorial
            Day weekend Fri→Tue trade).
        comparison_expiry: Defaults to the next Friday strictly after
            ``near_expiry`` (5/29 for a 5/26 expiry, 6/5 for a 5/29
            expiry). The Friday is the natural "post-weekend baseline"
            for SPXW pricing.
    """
    cmp_expiry = comparison_expiry or next_friday_after(near_expiry)

    result: Dict[str, Any] = {
        "enabled": False,
        "ticker": ticker.upper(),
        "asOfDate": today.isoformat(),
        "nearExpiry": near_expiry.isoformat(),
        "comparisonExpiry": cmp_expiry.isoformat(),
        "level": "UNKNOWN",
        "termLevel": "UNKNOWN",
        "skewLevel": "UNKNOWN",
        "warnings": [],
        "notes": [],
    }

    try:
        near_rows, near_spot, near_sym = _pull_chain(client, ticker=ticker, expiry_str=near_expiry.isoformat())
        cmp_rows, cmp_spot, cmp_sym = _pull_chain(client, ticker=ticker, expiry_str=cmp_expiry.isoformat())
    except Exception as e:
        result["warnings"].append(f"Chain fetch failed: {type(e).__name__}: {e}")
        return result

    if not near_rows or not cmp_rows or not near_spot:
        result["warnings"].append("Chain unavailable for one or both expiries; weekend stress disabled.")
        return result

    near_atm_pct = _atm_iv(near_rows, float(near_spot))
    cmp_atm_pct = _atm_iv(cmp_rows, float(cmp_spot or near_spot))
    if near_atm_pct is None or cmp_atm_pct is None:
        result["warnings"].append("ATM IV unavailable on one or both chains.")
        return result

    term_spread = round(float(near_atm_pct) - float(cmp_atm_pct), 2)
    term_level = _classify(term_spread, _TERM_THRESHOLDS)

    put_skew_pts = _put_skew_25d_pts(near_rows, atm_iv_pct=float(near_atm_pct))
    skew_level = _classify(put_skew_pts, _SKEW_THRESHOLDS)

    composite = _composite_level(term_level, skew_level)

    notes: List[str] = []
    if term_level == "SEVERE":
        notes.append(
            f"Near-expiry IV is {term_spread:+.2f}pts above next-Friday IV — severe weekend premium priced; market is bidding the gap window."
        )
    elif term_level == "ELEVATED":
        notes.append(
            f"Near-expiry IV is {term_spread:+.2f}pts vs next-Friday — elevated weekend fear (curve inversion outside normal range)."
        )
    elif term_level == "MODERATE":
        notes.append(
            f"Term spread is {term_spread:+.2f}pts — modest weekend bid, within tolerance for a holiday close."
        )
    else:
        notes.append(
            f"Term curve {term_spread:+.2f}pts — normal shape. No exceptional weekend premium being priced."
        )

    if put_skew_pts is not None:
        if skew_level == "SEVERE":
            notes.append(
                f"25Δ put skew is {put_skew_pts:+.2f}pts above ATM — severe downside hedging demand (Iran-class fear signature)."
            )
        elif skew_level == "ELEVATED":
            notes.append(
                f"25Δ put skew {put_skew_pts:+.2f}pts — elevated downside fear; protection is being bought."
            )
        elif skew_level == "MODERATE":
            notes.append(
                f"25Δ put skew {put_skew_pts:+.2f}pts — typical SPX skew range."
            )
        else:
            notes.append(
                f"25Δ put skew {put_skew_pts:+.2f}pts — flat / benign."
            )

    if composite == "SEVERE":
        notes.append("RECOMMENDATION: SKIP this trade. The options market is screaming weekend risk; cohort math does not apply.")
    elif composite == "ELEVATED":
        notes.append("RECOMMENDATION: Halve size and add tail hedges (long single OTM call + put at ±3% strikes) to cap fat-tail loss. Or wait for stress to ease.")
    elif composite == "MODERATE":
        notes.append("RECOMMENDATION: Normal sizing OK; consider tail hedge if you carry directional view on the headline calendar.")
    elif composite == "NORMAL":
        notes.append("RECOMMENDATION: Cohort math holds. No exceptional weekend signal from the options market.")

    result.update({
        "enabled": True,
        "spot": round(float(near_spot), 2),
        "symbolUsed": near_sym,
        "nearAtmIvPct": near_atm_pct,
        "comparisonAtmIvPct": cmp_atm_pct,
        "termSpreadPts": term_spread,
        "termLevel": term_level,
        "put25dSkewPts": put_skew_pts,
        "skewLevel": skew_level,
        "level": composite,
        "notes": notes,
    })
    return result


__all__ = [
    "compute_weekend_stress",
    "next_friday_after",
]
