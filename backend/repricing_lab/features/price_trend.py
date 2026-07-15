"""Price / trend features (returns, MA distance, ATR)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


def _closes(bars: Sequence[Dict[str, Any]]) -> List[float]:
    out = []
    for b in bars:
        px = b.get("adjusted_close")
        if px is None:
            px = b.get("close")
        if px is not None:
            out.append(float(px))
    return out


def _ret(closes: Sequence[float], lookback: int) -> Optional[float]:
    if len(closes) <= lookback:
        return None
    a, b = closes[-(lookback + 1)], closes[-1]
    if a == 0:
        return None
    return (b / a) - 1.0


def sma(closes: Sequence[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def atr_wilder(bars: Sequence[Dict[str, Any]], period: int = 14) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    trs = []
    prev_c = None
    for b in bars:
        h = b.get("high")
        l = b.get("low")
        c = b.get("adjusted_close") if b.get("adjusted_close") is not None else b.get("close")
        if h is None or l is None or c is None:
            return None
        h, l, c = float(h), float(l), float(c)
        if prev_c is None:
            trs.append(h - l)
        else:
            trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
        prev_c = c
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def compute_price_trend(bars: Sequence[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    closes = _closes(bars)
    last = closes[-1] if closes else None
    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200)
    atr14 = atr_wilder(bars, 14)
    hi_252 = max(closes[-252:]) if len(closes) >= 20 else (max(closes) if closes else None)
    lo_252 = min(closes[-252:]) if len(closes) >= 20 else (min(closes) if closes else None)
    return {
        "ret_1m": _ret(closes, 21),
        "ret_3m": _ret(closes, 63),
        "ret_6m": _ret(closes, 126),
        "ret_12m": _ret(closes, 252),
        "dist_sma20": ((last / sma20) - 1.0) if last and sma20 else None,
        "dist_sma50": ((last / sma50) - 1.0) if last and sma50 else None,
        "dist_sma200": ((last / sma200) - 1.0) if last and sma200 else None,
        "dist_52w_high": ((last / hi_252) - 1.0) if last and hi_252 else None,
        "dist_52w_low": ((last / lo_252) - 1.0) if last and lo_252 else None,
        "atr14": atr14,
        "atr_pct": (atr14 / last) if atr14 and last else None,
        "last_price": last,
    }
