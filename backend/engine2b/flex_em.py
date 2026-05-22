"""Flexible-expiry live Expected Move.

Mirrors :func:`backend.spx_ic.live_levels.compute_expected_move_weekly`
but accepts an arbitrary expiry date instead of "next Friday only". The
straddle math (forward via put-call parity, ATM-forward straddle, PV
discounting) is delegated to
:func:`backend.expected_move.compute_expected_move_from_chain`, which is
already DTE-agnostic.

Symbol order matches the Friday engine for SPX (``SPXW → SPX → SPY``) so
the desk gets the same data source for both flows.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional, Tuple

from backend.expected_move import compute_expected_move_from_chain
from backend.orats_client import OratsClient
from backend.spx_ic.live_levels import (
    _filter_chain_by_expiry,
    _infer_live_expiries_from_strikes,
)
from backend.spx_ic.utils import _parse_date, _to_float
from backend.technicals import fetch_live_price_context_optional

LOG = logging.getLogger("engine2b.flex_em")


def _resolve_symbols(ticker: str, symbols: Optional[Tuple[str, ...]]) -> Tuple[str, ...]:
    if symbols:
        return tuple(s for s in symbols if s)
    t = str(ticker).strip().upper()
    if t == "SPX":
        return ("SPXW", "SPX", "SPY")
    if t == "QQQ":
        return ("QQQ",)
    return (t,)


def compute_expected_move_flex(
    client: OratsClient,
    *,
    ticker: str,
    today: dt.date,
    expiry: dt.date,
    symbols: Optional[Tuple[str, ...]] = None,
) -> Dict[str, Any]:
    """Compute Expected Move for an arbitrary live expiry.

    Returns the same dict shape as ``compute_expected_move_weekly`` so
    the downstream renderers can light up without translation. ``expiry``
    drives the chain fetch directly — no Friday-only filter — and the
    returned ``dte`` reflects the requested expiry date.
    """
    t = str(ticker).strip().upper()
    expiry_str = expiry.isoformat()
    dte_computed = (expiry - today).days
    warnings: List[str] = []

    result: Dict[str, Any] = {
        "ticker": t,
        "asOfDate": today.isoformat(),
        "expiry": expiry_str,
        "dte": int(dte_computed) if dte_computed > 0 else 0,
        "source": None,
        "spotPrice": None,
        "forwardPrice": None,
        "straddlePV": None,
        "expectedMoveDollars": None,
        "expectedMovePct": None,
        "discountFactor": None,
        "strikesUsedForForward": 0,
        "smartSpotPrice": None,
        "smartSpotSource": None,
        "smartSpotMode": None,
        "smartSpotMarketOpen": None,
        "symbolUsed": None,
        "warnings": [],
        "notes": [
            f"Flex-expiry EM (target {expiry_str}); not constrained to Friday weeklies.",
        ],
    }

    if dte_computed <= 0:
        result["warnings"] = ["Requested expiry is on or before as-of date; no EM computable."]
        return result

    syms = _resolve_symbols(t, symbols)
    fields = (
        "ticker,tradeDate,expirDate,strike,spotPrice,stockPrice,"
        "callBidPrice,callAskPrice,putBidPrice,putAskPrice,"
        "callOpenInterest,putOpenInterest"
    )

    chain_rows: List[dict] = []
    used_symbol: Optional[str] = None
    spot: Optional[float] = None

    for sym in syms:
        # First check this symbol actually lists the requested expiry.
        # If not, we still attempt strikes-by-expiry (some ORATS plans
        # serve strikes for unlisted expiries) but warn loudly.
        exp_dates: List[str] = []
        try:
            if callable(getattr(client, "live_expirations", None)):
                resp = client.live_expirations(ticker=sym)
                for r in (resp.rows or []):
                    if isinstance(r, dict):
                        d0 = str(r.get("expirDate") or r.get("expiry") or "")[:10]
                        if d0 and len(d0) >= 10:
                            exp_dates.append(d0)
        except Exception as e:
            warnings.append(f"{sym}: expirations lookup failed ({type(e).__name__}).")

        if exp_dates and expiry_str not in {str(d)[:10] for d in exp_dates}:
            warnings.append(f"{sym}: expiry {expiry_str} not in live expirations list (will try direct fetch).")

        rows: List[dict] = []
        try:
            if callable(getattr(client, "live_strikes_by_expiry", None)):
                resp = client.live_strikes_by_expiry(ticker=sym, expiry=expiry_str, fields=fields)
                rows = [r for r in (resp.rows or []) if isinstance(r, dict)]
        except Exception as e:
            warnings.append(f"{sym}: live_strikes_by_expiry failed ({type(e).__name__}).")
            rows = []

        if not rows:
            # Fallback: full-chain pull filtered to the requested expiry.
            try:
                if callable(getattr(client, "live_strikes", None)):
                    all_rows = client.live_strikes(ticker=sym, fields=fields).rows or []
                    all_rows = [r for r in all_rows if isinstance(r, dict)]
                    rows = _filter_chain_by_expiry(all_rows, expiry=expiry_str)
                    if rows:
                        warnings.append(f"{sym}: used full live_strikes filtered by expiry.")
                    elif all_rows:
                        # Record what was actually listed so the operator can debug.
                        inferred = _infer_live_expiries_from_strikes(all_rows)
                        warnings.append(
                            f"{sym}: full chain had {len(all_rows)} rows but no rows for {expiry_str}. "
                            f"Listed expiries: {inferred[:10]}{'...' if len(inferred) > 10 else ''}"
                        )
            except Exception as e:
                warnings.append(f"{sym}: live_strikes fallback failed ({type(e).__name__}).")

        if not rows:
            continue

        for r in rows:
            s = _to_float(r.get("spotPrice")) or _to_float(r.get("stockPrice"))
            if s and s > 0:
                spot = s
                break

        if spot is None:
            warnings.append(f"{sym}: could not determine spot price from chain rows.")
            continue

        used_symbol = sym
        chain_rows = rows
        break

    if not chain_rows or spot is None:
        result["warnings"] = warnings + ["No usable chain found for requested expiry."]
        return result

    smart_spot_ctx = fetch_live_price_context_optional(client, ticker=t)
    smart_spot = _to_float(smart_spot_ctx.get("price")) if isinstance(smart_spot_ctx, dict) else None
    if smart_spot is None or smart_spot <= 0:
        smart_spot = spot
    if isinstance(smart_spot_ctx, dict):
        result["smartSpotPrice"] = round(float(smart_spot), 2) if smart_spot is not None else None
        result["smartSpotSource"] = smart_spot_ctx.get("source")
        result["smartSpotMode"] = smart_spot_ctx.get("mode")
        result["smartSpotMarketOpen"] = smart_spot_ctx.get("marketOpen")

    em_result = compute_expected_move_from_chain(
        chain_rows,
        spot=float(smart_spot) if (smart_spot is not None and smart_spot > 0) else spot,
        expiry=expiry,
        as_of=today,
        risk_free_rate=0.05,
    )

    result["source"] = "live"
    result["spotPrice"] = round(float(spot), 2)
    result["forwardPrice"] = em_result.get("forwardPrice")
    result["straddlePV"] = em_result.get("straddlePV")
    result["expectedMoveDollars"] = em_result.get("expectedMoveDollars")
    result["expectedMovePct"] = em_result.get("expectedMovePct")
    result["discountFactor"] = em_result.get("discountFactor")
    result["strikesUsedForForward"] = em_result.get("strikesUsedForForward", 0)
    result["symbolUsed"] = used_symbol
    result["warnings"] = warnings + list(em_result.get("warnings") or [])

    return result


__all__ = ["compute_expected_move_flex"]
