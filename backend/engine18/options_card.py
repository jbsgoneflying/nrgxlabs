"""Engine 18 — informational options expression (NOT backtested).

For full-size candidates the desk may prefer defined-risk convexity over
equity. This helper reads the live ORATS chain and suggests a ~3-week call
spread (long ~40Δ / short ~20Δ). The validated edge is the EQUITY drift —
this card is explicitly informational and labeled as such in the payload.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)

_FIELDS = "ticker,expirDate,strike,dte,callBidPrice,callAskPrice,callValue,delta,callDelta"

_TARGET_DTE = 21          # ~3 weeks — covers the 10-trading-day hold
_DTE_MIN, _DTE_MAX = 12, 45
_LONG_DELTA, _SHORT_DELTA = 0.40, 0.20

DISCLAIMER = "Informational — options expression not backtested. The validated edge is the equity drift."


def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _mid(bid, ask, fallback=None) -> Optional[float]:
    b, a = _f(bid), _f(ask)
    if b is not None and a is not None and a >= b >= 0:
        return (a + b) / 2.0
    return _f(fallback)


def _call_delta(row: Dict[str, Any]) -> Optional[float]:
    d = _f(row.get("callDelta"))
    if d is None:
        d = _f(row.get("delta"))
    return d


def suggest_call_spread(ticker: str, *, client=None) -> Optional[Dict[str, Any]]:
    """Suggest a long ~40Δ / short ~20Δ call spread on the ~3-week expiry.

    Returns None when the chain is unavailable or no sane spread exists.
    """
    if client is None:
        try:
            from backend.deps import get_client_optional

            client = get_client_optional()
        except Exception:
            client = None
    if client is None:
        return None

    try:
        resp = client.live_strikes(ticker=str(ticker).upper(), fields=_FIELDS)
        rows: List[Dict[str, Any]] = list(resp.rows or [])
    except Exception as exc:
        LOG.debug("engine18 options: chain fetch failed for %s: %s", ticker, exc)
        return None
    if not rows:
        return None

    # Pick the expiry with DTE closest to the target inside the allowed band.
    by_expiry: Dict[str, List[Dict[str, Any]]] = {}
    expiry_dte: Dict[str, float] = {}
    for r in rows:
        exp = str(r.get("expirDate") or "")[:10]
        dte = _f(r.get("dte"))
        if not exp or dte is None or not (_DTE_MIN <= dte <= _DTE_MAX):
            continue
        by_expiry.setdefault(exp, []).append(r)
        expiry_dte[exp] = dte
    if not by_expiry:
        return None
    expiry = min(expiry_dte, key=lambda e: abs(expiry_dte[e] - _TARGET_DTE))
    chain = by_expiry[expiry]

    def closest(target: float) -> Optional[Dict[str, Any]]:
        best, best_err = None, 1e9
        for r in chain:
            d = _call_delta(r)
            if d is None or d <= 0.02 or d >= 0.98:
                continue
            err = abs(d - target)
            if err < best_err:
                best, best_err = r, err
        return best

    long_row = closest(_LONG_DELTA)
    short_row = closest(_SHORT_DELTA)
    if long_row is None or short_row is None:
        return None
    long_strike = _f(long_row.get("strike"))
    short_strike = _f(short_row.get("strike"))
    if long_strike is None or short_strike is None or short_strike <= long_strike:
        return None

    long_mid = _mid(long_row.get("callBidPrice"), long_row.get("callAskPrice"), long_row.get("callValue"))
    short_mid = _mid(short_row.get("callBidPrice"), short_row.get("callAskPrice"), short_row.get("callValue"))
    if long_mid is None or short_mid is None:
        return None
    debit = round(long_mid - short_mid, 2)
    width = round(short_strike - long_strike, 2)
    if debit <= 0 or width <= 0:
        return None

    return {
        "ticker": str(ticker).upper(),
        "structure": "call debit spread",
        "expiry": expiry,
        "dte": expiry_dte[expiry],
        "longStrike": long_strike,
        "longDelta": _call_delta(long_row),
        "longMid": round(long_mid, 2),
        "shortStrike": short_strike,
        "shortDelta": _call_delta(short_row),
        "shortMid": round(short_mid, 2),
        "debit": debit,
        "width": width,
        "maxValue": width,
        "rewardRisk": round((width - debit) / debit, 2) if debit > 0 else None,
        "disclaimer": DISCLAIMER,
    }
