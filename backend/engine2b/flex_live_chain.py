"""Live-chain probe for the Flex-Expiry engine.

Pulls the actual SPXW (or SPX / SPY proxy) chain for the *requested
expiry*, snaps to the recommended short-strike distances at each EM
multiple, and returns a clean per-row card the desk can compare against
the broker before entry:

    {
      "emMult": 2.5,
      "wingWidthPts": 5,
      "shortPut": 5430, "longPut": 5425,
      "shortCall": 5680, "longCall": 5685,
      "shortPutBid": 0.55, "shortPutAsk": 0.65, "shortPutMid": 0.60,
      "shortCallBid": 0.50, "shortCallAsk": 0.62, "shortCallMid": 0.56,
      "longPutMid": 0.20, "longCallMid": 0.18,
      "netMidCredit": 0.78,           # short calls + short puts - long calls - long puts
      "maxLossPerContract": 4.22,     # wingWidth - netMidCredit  (in points)
      "rocPct": 18.5,                 # credit / maxLoss
      "popFromMid": 0.66,             # 1 - (shortCallDelta + shortPutDelta_abs)/2 if deltas available
      "shortPutDistancePct": 2.1,
      "shortCallDistancePct": 2.0,
      "notes": [...],
    }

The math is intentionally deterministic — we never re-derive an EM
here. We use the EM that ``flex_em.py`` already computed (so the
strikes line up with the EM card on the dashboard) and snap to the
nearest live strike.

If the chain is unavailable the function returns ``enabled=False`` so
the engine + UI degrade gracefully.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.orats_client import OratsClient
from backend.spx_ic.live_levels import (
    _filter_chain_by_expiry,
    _infer_live_expiries_from_strikes,
)
from backend.spx_ic.utils import _to_float

LOG = logging.getLogger("engine2b.flex_live_chain")


_DEFAULT_FIELDS = (
    "ticker,tradeDate,expirDate,strike,spotPrice,stockPrice,"
    "callBidPrice,callAskPrice,callMidIv,callDelta,callValue,"
    "putBidPrice,putAskPrice,putMidIv,putDelta,putValue,"
    "callOpenInterest,putOpenInterest"
)


def _resolve_symbols(ticker: str, symbols: Optional[Tuple[str, ...]]) -> Tuple[str, ...]:
    if symbols:
        return tuple(s for s in symbols if s)
    t = str(ticker).strip().upper()
    if t == "SPX":
        return ("SPXW", "SPX", "SPY")
    if t == "QQQ":
        return ("QQQ",)
    return (t,)


def _strike_step_for(symbol: str, spot: float) -> int:
    """Heuristic strike step. SPXW/SPX list $5 strikes for weeklies; SPY
    is $1. QQQ is mostly $1. Used purely to "snap to nearest" — if the
    actual chain offers a finer grid we still find it via the nearest
    available strike search.
    """
    s = str(symbol).upper()
    if s in ("SPXW", "SPX"):
        return 5
    return 1


def _nearest_strike(strikes: Sequence[float], target: float) -> Optional[float]:
    if not strikes:
        return None
    return min(strikes, key=lambda k: abs(float(k) - float(target)))


def _row_by_strike(rows: Sequence[Dict[str, Any]], strike: float) -> Optional[Dict[str, Any]]:
    eps = 1e-3
    for r in rows:
        try:
            k = float(r.get("strike"))
        except Exception:
            continue
        if abs(k - float(strike)) < eps:
            return r
    return None


def _mid(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None:
        return None
    try:
        b = float(bid)
        a = float(ask)
    except Exception:
        return None
    if b < 0 or a < 0:
        return None
    if a <= 0 and b <= 0:
        return None
    return round((b + a) / 2.0, 4)


def _pull_live_chain(
    client: OratsClient,
    *,
    symbols: Tuple[str, ...],
    expiry_str: str,
) -> Tuple[Optional[str], List[Dict[str, Any]], Optional[float], List[str]]:
    """Return ``(symbol_used, chain_rows, spot, warnings)`` for the first
    symbol in ``symbols`` that has a live chain for ``expiry_str``.
    """
    warnings: List[str] = []
    for sym in symbols:
        rows: List[Dict[str, Any]] = []
        try:
            if callable(getattr(client, "live_strikes_by_expiry", None)):
                resp = client.live_strikes_by_expiry(ticker=sym, expiry=expiry_str, fields=_DEFAULT_FIELDS)
                rows = [r for r in (getattr(resp, "rows", None) or []) if isinstance(r, dict)]
        except Exception as e:
            warnings.append(f"{sym}: live_strikes_by_expiry failed ({type(e).__name__}).")
            rows = []

        if not rows:
            try:
                if callable(getattr(client, "live_strikes", None)):
                    resp = client.live_strikes(ticker=sym, fields=_DEFAULT_FIELDS)
                    all_rows = [r for r in (getattr(resp, "rows", None) or []) if isinstance(r, dict)]
                    rows = _filter_chain_by_expiry(all_rows, expiry=expiry_str)
                    if not rows and all_rows:
                        inferred = _infer_live_expiries_from_strikes(all_rows)
                        warnings.append(
                            f"{sym}: full chain {len(all_rows)} rows but no rows for {expiry_str}. "
                            f"Listed: {inferred[:8]}{'...' if len(inferred) > 8 else ''}"
                        )
            except Exception as e:
                warnings.append(f"{sym}: live_strikes fallback failed ({type(e).__name__}).")

        if not rows:
            continue

        spot: Optional[float] = None
        for r in rows:
            s = _to_float(r.get("spotPrice")) or _to_float(r.get("stockPrice"))
            if s and s > 0:
                spot = s
                break
        if spot is None or spot <= 0:
            warnings.append(f"{sym}: no spot in chain rows for {expiry_str}.")
            continue

        return sym, rows, float(spot), warnings

    return None, [], None, warnings


def compute_flex_live_chain_targets(
    client: OratsClient,
    *,
    ticker: str,
    today: dt.date,
    expiry: dt.date,
    em_pct: float,
    em_mults: Sequence[float],
    wing_pts: Sequence[int],
    symbols: Optional[Tuple[str, ...]] = None,
) -> Dict[str, Any]:
    """Build the live-chain target rows for the desk to verify on the broker.

    Args:
        client: ORATS client (same one Engine 2 uses).
        ticker: SPX | SPY | QQQ — fallback chain order matches Engine 2.
        today: As-of date.
        expiry: Target expiry date.
        em_pct: 1σ EM in percent (already computed by ``flex_em``).
        em_mults: List of EM multiples to probe (e.g. [1.0, 1.5, 2.0, 2.5]).
        wing_pts: Wing widths in points (e.g. [5, 10]).
        symbols: Optional explicit symbol order; defaults to SPXW→SPX→SPY for SPX.
    """
    expiry_str = expiry.isoformat()
    result: Dict[str, Any] = {
        "enabled": False,
        "ticker": ticker.upper(),
        "expiry": expiry_str,
        "asOfDate": today.isoformat(),
        "symbolUsed": None,
        "spotPrice": None,
        "emPct": float(em_pct) if em_pct is not None else None,
        "targets": [],
        "warnings": [],
        "notes": [
            f"Live SPXW chain probe for expiry {expiry_str}.",
            "Mid prices are bid/ask midpoints — broker fills depend on liquidity.",
        ],
    }

    if em_pct is None or em_pct <= 0:
        result["warnings"].append("EM unavailable or <= 0; cannot project strikes.")
        return result

    syms = _resolve_symbols(ticker, symbols)
    sym_used, chain_rows, spot, warnings = _pull_live_chain(client, symbols=syms, expiry_str=expiry_str)
    result["warnings"].extend(warnings)
    if not sym_used or not chain_rows or not spot:
        result["warnings"].append("No usable live chain for requested expiry; live-chain targets disabled.")
        return result

    result["enabled"] = True
    result["symbolUsed"] = sym_used
    result["spotPrice"] = round(float(spot), 2)

    # Unique sorted strike list.
    strikes: List[float] = []
    seen: set = set()
    for r in chain_rows:
        k = _to_float(r.get("strike"))
        if k is None or k <= 0:
            continue
        if k in seen:
            continue
        seen.add(k)
        strikes.append(float(k))
    strikes.sort()

    if not strikes:
        result["warnings"].append("Chain has no strikes.")
        return result

    em_dollars = float(spot) * float(em_pct) / 100.0
    step = _strike_step_for(sym_used, spot)

    for emm in em_mults:
        try:
            emv = float(emm)
        except Exception:
            continue
        if emv <= 0:
            continue
        short_put_target = spot - emv * em_dollars
        short_call_target = spot + emv * em_dollars
        sp_strike = _nearest_strike(strikes, short_put_target)
        sc_strike = _nearest_strike(strikes, short_call_target)
        if sp_strike is None or sc_strike is None:
            continue

        sp_row = _row_by_strike(chain_rows, sp_strike)
        sc_row = _row_by_strike(chain_rows, sc_strike)
        if not sp_row or not sc_row:
            continue
        sp_bid = _to_float(sp_row.get("putBidPrice"))
        sp_ask = _to_float(sp_row.get("putAskPrice"))
        sp_mid = _mid(sp_bid, sp_ask)
        sc_bid = _to_float(sc_row.get("callBidPrice"))
        sc_ask = _to_float(sc_row.get("callAskPrice"))
        sc_mid = _mid(sc_bid, sc_ask)

        sp_delta = _to_float(sp_row.get("putDelta"))
        sc_delta = _to_float(sc_row.get("callDelta"))

        for wp in wing_pts:
            try:
                wpv = int(wp)
            except Exception:
                continue
            if wpv <= 0:
                continue
            lp_strike = _nearest_strike(strikes, sp_strike - wpv)
            lc_strike = _nearest_strike(strikes, sc_strike + wpv)
            if lp_strike is None or lc_strike is None:
                continue
            lp_row = _row_by_strike(chain_rows, lp_strike)
            lc_row = _row_by_strike(chain_rows, lc_strike)
            lp_mid = _mid(_to_float(lp_row.get("putBidPrice")) if lp_row else None,
                         _to_float(lp_row.get("putAskPrice")) if lp_row else None)
            lc_mid = _mid(_to_float(lc_row.get("callBidPrice")) if lc_row else None,
                         _to_float(lc_row.get("callAskPrice")) if lc_row else None)

            if sp_mid is None or sc_mid is None or lp_mid is None or lc_mid is None:
                # Partial mid — still publish strikes so desk sees the
                # shape, but mark credit unavailable.
                net_mid = None
            else:
                # IC = sell shorts, buy longs. Credit positive.
                net_mid = round(float(sp_mid) + float(sc_mid) - float(lp_mid) - float(lc_mid), 4)

            actual_wp_put = abs(float(sp_strike) - float(lp_strike))
            actual_wp_call = abs(float(lc_strike) - float(sc_strike))
            # IC max loss per contract = max(put_wing, call_wing) - credit (in points).
            wing_used = max(actual_wp_put, actual_wp_call)
            max_loss = None
            roc = None
            be_put = None
            be_call = None
            if net_mid is not None and wing_used > 0:
                max_loss = round(float(wing_used) - float(net_mid), 4)
                if max_loss > 0:
                    roc = round(100.0 * float(net_mid) / float(max_loss), 2)
                be_put = round(float(sp_strike) - float(net_mid), 2)
                be_call = round(float(sc_strike) + float(net_mid), 2)

            # Probability of profit estimate from short deltas (puts are
            # negative — take absolute value). Naive but standard. Use
            # the higher of (1 - shortCallDelta - |shortPutDelta|) and
            # NaN when deltas unavailable.
            pop_from_mid = None
            try:
                if sc_delta is not None and sp_delta is not None:
                    pop_from_mid = round(max(0.0, min(1.0, 1.0 - (float(sc_delta) + abs(float(sp_delta))))), 4)
            except Exception:
                pop_from_mid = None

            short_put_dist_pct = round(100.0 * (float(spot) - float(sp_strike)) / float(spot), 3)
            short_call_dist_pct = round(100.0 * (float(sc_strike) - float(spot)) / float(spot), 3)

            result["targets"].append({
                "emMult": float(emm),
                "wingWidthPts": int(wpv),
                "shortPut": int(sp_strike) if float(sp_strike).is_integer() else float(sp_strike),
                "longPut": int(lp_strike) if float(lp_strike).is_integer() else float(lp_strike),
                "shortCall": int(sc_strike) if float(sc_strike).is_integer() else float(sc_strike),
                "longCall": int(lc_strike) if float(lc_strike).is_integer() else float(lc_strike),
                "shortPutBid": sp_bid,
                "shortPutAsk": sp_ask,
                "shortPutMid": sp_mid,
                "shortCallBid": sc_bid,
                "shortCallAsk": sc_ask,
                "shortCallMid": sc_mid,
                "longPutMid": lp_mid,
                "longCallMid": lc_mid,
                "shortPutDelta": sp_delta,
                "shortCallDelta": sc_delta,
                "netMidCredit": net_mid,
                "maxLossPerContract": max_loss,
                "rocPct": roc,
                "popFromMid": pop_from_mid,
                "putBreakeven": be_put,
                "callBreakeven": be_call,
                "shortPutDistancePct": short_put_dist_pct,
                "shortCallDistancePct": short_call_dist_pct,
                "strikeStep": int(step),
            })

    if not result["targets"]:
        result["warnings"].append("No live targets resolved from chain (no strikes matched the EM/wing grid).")
        result["enabled"] = False

    return result


__all__ = ["compute_flex_live_chain_targets"]
