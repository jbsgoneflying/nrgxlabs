from __future__ import annotations

import datetime as dt
import logging
import math
import statistics
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from cachetools import TTLCache
from zoneinfo import ZoneInfo

from backend.benzinga_client import BenzingaClient
from backend.config import FeatureFlags
from backend.orats_client import OratsClient, OratsError

LOG = logging.getLogger("spx_ic_engine")

from backend.dealer_gamma_context import compute_dealer_gamma_context
from backend.oi_clusters import compute_open_interest_clusters
from backend.technicals import DailyBar as TechDailyBar
from backend.technicals import (
    _ema_series,
    build_ta_narrative,
    build_ta_signals,
    compute_bollinger_series,
    compute_distances,
    compute_ema_levels,
    compute_ichimoku_levels,
    compute_macd_series,
    compute_rsi_series,
    compute_vwap_proxy,
    detect_candlestick_patterns,
    detect_elliott_pivot_structure,
    detect_red_dog_reversal,
    fetch_live_price_optional,
)


def _fmt_date(d: dt.date) -> str:
    return d.isoformat()


def _parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(str(s)[:10])


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        if not math.isfinite(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _iv_to_pct(v: Any) -> Optional[float]:
    """
    Normalize IV-like values to percent.
    ORATS sometimes returns vols as decimals (0.12 = 12%) or percents (12 = 12%).
    """
    x = _to_float(v)
    if x is None:
        return None
    x = abs(float(x))
    return x * 100.0 if x <= 1.0 else x


_ET = ZoneInfo("America/New_York")


def _now_et(now_dt: Optional[dt.datetime] = None) -> dt.datetime:
    """
    Return timezone-aware datetime in US/Eastern.
    - If now_dt is None: uses current wall clock in ET.
    - If now_dt is naive: assumes it is already ET.
    - If now_dt is aware: converts to ET.
    """
    if now_dt is None:
        return dt.datetime.now(tz=_ET)
    if now_dt.tzinfo is None:
        return now_dt.replace(tzinfo=_ET)
    return now_dt.astimezone(_ET)


def _after_cash_close_et(now_et: dt.datetime) -> bool:
    """
    Define when to roll weekly expiry after Friday close.
    We use 4:15pm ET to allow for settlement/prints and to keep behavior stable.
    """
    return now_et.time() >= dt.time(16, 15)


def _normalize_expiry_dates(exp_dates: List[str]) -> List[str]:
    ds = [str(d)[:10] for d in (exp_dates or []) if d]
    # unique + sorted
    return sorted(list(dict.fromkeys(ds)))


def _pick_nearest_expiry_date(exp_dates: List[str], *, today: dt.date) -> Optional[str]:
    """
    Nearest-expiry selector (daily/0DTE behavior):
    - Prefer 0DTE if present
    - Else nearest upcoming
    - Else last known
    """
    ds = _normalize_expiry_dates(exp_dates)
    if not ds:
        return None
    td = _fmt_date(today)
    if td in ds:
        return td
    for d0 in ds:
        try:
            if _parse_date(d0) > today:
                return d0
        except Exception:
            continue
    return ds[-1]


def _pick_weekly_close_expiry_date(
    exp_dates: List[str],
    *,
    today: dt.date,
    now_dt: Optional[dt.datetime] = None,
) -> Optional[str]:
    """
    Weekly trade-management selector:
    - If today is Friday and BEFORE 4:15pm ET: pick today's Friday expiry (if listed)
    - Otherwise: pick the next Friday
    - Holiday week fallback: if no Friday, pick the next Thursday
    - Otherwise fallback: nearest upcoming expiry
    """
    ds = _normalize_expiry_dates(exp_dates)
    if not ds:
        return None

    now_et = _now_et(now_dt)
    after_close = _after_cash_close_et(now_et)

    # On Friday pre-close, use same-day Friday if present.
    td = _fmt_date(today)
    if today.weekday() == 4 and (not after_close) and td in ds:
        return td

    # Start date: if we're after close, search from tomorrow; else from today.
    start = today + dt.timedelta(days=1) if after_close else today
    future: List[Tuple[str, dt.date]] = []
    for d0 in ds:
        try:
            dd = _parse_date(d0)
        except Exception:
            continue
        if dd >= start:
            future.append((d0, dd))

    # Prefer next Friday
    for d0, dd in future:
        if dd.weekday() == 4:
            return d0
    # Holiday fallback: next Thursday
    for d0, dd in future:
        if dd.weekday() == 3:
            return d0
    # Fallback: nearest upcoming
    if future:
        return future[0][0]
    # else last known
    return ds[-1]


def _pick_expiry_window(
    exp_dates: List[str],
    *,
    view: str,
    today: dt.date,
    now_dt: Optional[dt.datetime] = None,
    limit: int = 12,
) -> List[str]:
    """
    Pick a small forward window of expiries for visualization (heatmap).

    - view="weekly": prefer Friday expiries (then Thursday holiday fallbacks), starting from
      today (or tomorrow if after cash close).
    - view="nearest": prefer nearest expiries (including 0DTE if present).

    Returns a sorted, unique list of ISO dates (YYYY-MM-DD).
    """
    ds = _normalize_expiry_dates(exp_dates)
    if not ds:
        return []
    lim = max(1, int(limit))

    v = str(view or "weekly").strip().lower()
    now_et = _now_et(now_dt)
    after_close = _after_cash_close_et(now_et)
    start = today + dt.timedelta(days=1) if after_close else today

    # Partition into (>=start) and all
    future: List[Tuple[str, dt.date]] = []
    for d0 in ds:
        try:
            dd = _parse_date(d0)
        except Exception:
            continue
        if dd >= start:
            future.append((d0, dd))
    future.sort(key=lambda x: x[1])

    # If nothing upcoming, return last known few.
    if not future:
        return ds[-lim:]

    if v.startswith("week"):
        fr = [d0 for (d0, dd) in future if dd.weekday() == 4]
        th = [d0 for (d0, dd) in future if dd.weekday() == 3]
        other = [d0 for (d0, dd) in future if dd.weekday() not in (3, 4)]
        out = fr + th + other
        return out[:lim]

    # nearest/0DTE style: just take upcoming in order
    return [d0 for (d0, _) in future[:lim]]


def _pick_spot_from_live_rows(rows: List[dict]) -> Optional[float]:
    # Prefer spotPrice if present, else stockPrice
    for key in ("spotPrice", "spot_price", "spot"):
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            v = _to_float(r.get(key))
            if v and v > 0:
                return float(v)
    for key in ("stockPrice", "stock_price", "underlyingPrice"):
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            v = _to_float(r.get(key))
            if v and v > 0:
                return float(v)
    return None


def compute_spx_net_gex_heatmap(
    strikes_rows: List[dict],
    *,
    expiries: List[str],
    spot: float,
    band_pct: float = 0.05,
    contract_multiplier: int = 100,
    strike_cap: int = 180,
) -> Dict[str, Any]:
    """
    Build an expiry × strike matrix of net dollar gamma exposure (best-effort proxy).

      net$GEX(strike,exp) ≈ gamma * (callOI - putOI) * spot^2 * contract_multiplier

    Notes:
    - Uses ORATS live strikes payload for gamma + OI.
    - If OI is missing, falls back to volume (noisy); otherwise gamma-only (very noisy).
    - Filters to strikes within ±band_pct of spot to keep the matrix compact.
    """
    rows = [r for r in (strikes_rows or []) if isinstance(r, dict)]
    out_exp = [str(e)[:10] for e in (expiries or []) if e]

    s0 = float(spot)
    lo = s0 * (1.0 - float(band_pct))
    hi = s0 * (1.0 + float(band_pct))

    has_call_oi = any(_to_float(r.get("callOpenInterest")) is not None for r in rows)
    has_put_oi = any(_to_float(r.get("putOpenInterest")) is not None for r in rows)
    has_call_vol = any(_to_float(r.get("callVolume")) is not None for r in rows)
    has_put_vol = any(_to_float(r.get("putVolume")) is not None for r in rows)

    warnings: List[str] = []
    if has_call_oi or has_put_oi:
        weighting_mode = "oi"
        if not (has_call_oi and has_put_oi):
            warnings.append("Open interest missing on one side; using 0 for missing side.")
    elif has_call_vol or has_put_vol:
        weighting_mode = "volume"
        warnings.append("Open interest unavailable; using volume-weighted net $GEX proxy (noisy).")
    else:
        weighting_mode = "gamma_only"
        warnings.append("Open interest and volume unavailable; using gamma-only net $GEX proxy (very noisy).")

    # Group by expiry (only for requested expiries)
    rows_by_exp: Dict[str, List[dict]] = {e: [] for e in out_exp}
    for r in rows:
        ex = str(r.get("expirDate") or r.get("expiry") or r.get("expDate") or r.get("exp_date") or "")[:10]
        if ex in rows_by_exp:
            rows_by_exp[ex].append(r)

    # Collect strikes (union across expiries) inside the band
    strike_set = set()
    for ex in out_exp:
        for r in rows_by_exp.get(ex) or []:
            k = _to_float(r.get("strike"))
            if k is None:
                continue
            kk = float(k)
            if lo <= kk <= hi:
                strike_set.add(kk)
    strikes = sorted(strike_set)

    # Cap strikes (keep stable sampling across the band)
    cap = max(40, int(strike_cap))
    if len(strikes) > cap:
        stride = int(math.ceil(len(strikes) / float(cap)))
        strikes = strikes[:: max(1, stride)]

    # Build expiry×strike matrix (null for missing)
    mat: List[List[Optional[float]]] = []
    for ex in out_exp:
        by_strike: Dict[float, float] = {}
        for r in rows_by_exp.get(ex) or []:
            k = _to_float(r.get("strike"))
            gamma = _to_float(r.get("gamma"))
            if k is None or gamma is None:
                continue
            kk = float(k)
            if kk not in strike_set:
                # Outside band or filtered out; skip
                continue

            if weighting_mode == "oi":
                c = _to_float(r.get("callOpenInterest")) or 0.0
                p = _to_float(r.get("putOpenInterest")) or 0.0
            elif weighting_mode == "volume":
                c = _to_float(r.get("callVolume")) or 0.0
                p = _to_float(r.get("putVolume")) or 0.0
            else:
                c = 1.0
                p = 1.0
            c = max(0.0, float(c))
            p = max(0.0, float(p))

            # Dollar-gamma-ish scaling (street-style proxy)
            val = float(gamma) * (c - p) * (s0**2) * float(contract_multiplier)
            if math.isfinite(val):
                by_strike[kk] = by_strike.get(kk, 0.0) + float(val)

        row_vals: List[Optional[float]] = []
        for kk in strikes:
            v = by_strike.get(float(kk))
            row_vals.append(None if v is None else round(float(v), 6))
        mat.append(row_vals)

    return {
        "enabled": True,
        "spot": round(float(s0), 6),
        "bandPct": float(band_pct),
        "weightingMode": weighting_mode,
        "contractMultiplier": int(contract_multiplier),
        "expiries": out_exp,
        "strikes": [round(float(k), 6) for k in strikes],
        "netDollarGex": mat,
        "warnings": warnings,
        "notes": [
            "Net $GEX is a best-effort proxy computed from ORATS strike gamma and OI; not a full dealer positioning model.",
            "Missing strikes are returned as null (not zero).",
        ],
    }


def _finite(v: Any) -> Optional[float]:
    x = _to_float(v)
    return None if x is None else float(x)


def _row_slope_first_diff(vals: List[Optional[float]]) -> List[Optional[float]]:
    """
    First difference across strikes: slope[i] = vals[i] - vals[i-1].
    Keeps the same length; slope[0] = None.

    IMPORTANT: this is computed on RAW Net $GEX (not normalized).
    """
    out: List[Optional[float]] = [None] * len(vals)
    if not vals:
        return out
    prev = vals[0]
    for i in range(1, len(vals)):
        cur = vals[i]
        if cur is None or prev is None:
            out[i] = None
        else:
            out[i] = float(cur) - float(prev)
        prev = cur
    return out


def _rolling_mean(vals: List[Optional[float]], *, window: int) -> List[Optional[float]]:
    """
    Centered rolling mean ignoring None; returns None where insufficient data.
    """
    w = int(window)
    if w <= 1:
        return list(vals)
    n = len(vals)
    out: List[Optional[float]] = [None] * n
    half = w // 2
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        xs = [float(x) for x in vals[lo:hi] if x is not None and math.isfinite(float(x))]
        if len(xs) >= max(2, min(w, hi - lo) // 2):
            out[i] = float(sum(xs) / float(len(xs)))
        else:
            out[i] = None
    return out


def _apply_slope(vals: List[Optional[float]], *, window: int) -> List[Optional[float]]:
    """
    Slope = first difference across strikes, then rolling-mean smoothing.
    Computed on RAW Net $GEX; normalization is applied only at render time.
    """
    return _rolling_mean(_row_slope_first_diff(vals), window=int(window))


def _parse_expiry_safe(s: str) -> Optional[dt.date]:
    try:
        return _parse_date(str(s)[:10])
    except Exception:
        return None


def _select_expiries_by_dte(
    exp_dates: List[str],
    *,
    today: dt.date,
    dte_max: int,
    cap: int,
) -> List[Tuple[str, int]]:
    """
    Return (expiry_iso, dte_days) sorted by expiry date, filtered to 0..dte_max.
    """
    out: List[Tuple[str, int]] = []
    for e in _normalize_expiry_dates(exp_dates):
        ed = _parse_expiry_safe(e)
        if ed is None:
            continue
        dte = int((ed - today).days)
        if dte < 0 or dte > int(dte_max):
            continue
        out.append((str(e)[:10], dte))
    out.sort(key=lambda x: x[0])
    if cap and len(out) > int(cap):
        out = out[: int(cap)]
    return out


def _bucket_for_dte(dte: int) -> Optional[str]:
    dd = int(dte)
    if 0 <= dd <= 5:
        return "0_5"
    if 6 <= dd <= 10:
        return "6_10"
    if 20 <= dd <= 40:
        return "20_40"
    return None


def _bucket_label(k: str) -> str:
    return {"0_5": "0–5 DTE", "6_10": "6–10 DTE", "20_40": "20–40 DTE"}.get(k, k)


def _exp_decay_weight(*, dte: int, half_life_dte: float) -> float:
    hl = float(half_life_dte)
    if hl <= 1e-9:
        return 1.0
    lam = math.log(2.0) / hl
    return math.exp(-lam * float(max(0, int(dte))))


def _weighted_sum_rows(
    *,
    rows: List[List[Optional[float]]],
    weights: List[float],
) -> List[Optional[float]]:
    """
    Weighted sum across expiry rows, skipping None cells.
    """
    if not rows:
        return []
    m = len(rows[0])
    out: List[Optional[float]] = [None] * m
    for j in range(m):
        num = 0.0
        den = 0.0
        for i in range(len(rows)):
            w = float(weights[i])
            if w <= 0:
                continue
            v = rows[i][j] if j < len(rows[i]) else None
            if v is None:
                continue
            fv = float(v)
            if not math.isfinite(fv):
                continue
            num += w * fv
            den += w
        out[j] = (num / den) if den > 1e-12 else None
    return out


def _find_accel_boundary_from_spot(
    *,
    strikes: List[float],
    vals: List[Optional[float]],
    spot: float,
    side: str,
    adjacent_n: int,
) -> Optional[float]:
    """
    Find the first boundary from spot outward where sign flips from positive->negative
    and remains negative for >= adjacent_n strikes beyond the boundary.

    side: "down" (scan decreasing strikes) or "up" (scan increasing strikes)
    Returns an approximate boundary strike (midpoint between the two strikes around the flip).
    """
    if not strikes or not vals or len(strikes) != len(vals):
        return None
    s0 = float(spot)
    n = max(1, int(adjacent_n))

    # Find nearest strike index to spot with a finite value
    best_i = None
    best_d = None
    for i, k in enumerate(strikes):
        v = vals[i]
        if v is None:
            continue
        if not math.isfinite(float(v)):
            continue
        d = abs(float(k) - s0)
        if best_i is None or best_d is None or d < best_d:
            best_i = i
            best_d = d
    if best_i is None:
        return None

    def _sign(v: float) -> int:
        return 1 if v >= 0 else -1

    v0 = vals[best_i]
    if v0 is None:
        return None
    base_sign = _sign(float(v0))

    if side == "down":
        # boundary below spot: looking for + to - as we move down
        for i in range(best_i - 1, -1, -1):
            vi = vals[i]
            vup = vals[i + 1]
            if vi is None or vup is None:
                continue
            if not (math.isfinite(float(vi)) and math.isfinite(float(vup))):
                continue
            if _sign(float(vup)) == 1 and _sign(float(vi)) == -1:
                # persistence: i down to i-(n-1) are negative
                ok = True
                for j in range(i, max(-1, i - n), -1):
                    vj = vals[j]
                    if vj is None or (not math.isfinite(float(vj))) or _sign(float(vj)) != -1:
                        ok = False
                        break
                if ok:
                    return 0.5 * (float(strikes[i]) + float(strikes[i + 1]))
        return None

    if side == "up":
        # boundary above spot: looking for + to - as we move up
        for i in range(best_i, len(strikes) - 1):
            vi = vals[i]
            vdn = vals[i + 1]
            if vi is None or vdn is None:
                continue
            if not (math.isfinite(float(vi)) and math.isfinite(float(vdn))):
                continue
            if _sign(float(vi)) == 1 and _sign(float(vdn)) == -1:
                # persistence: i+1 up to i+n are negative
                ok = True
                for j in range(i + 1, min(len(strikes), i + 1 + n)):
                    vj = vals[j]
                    if vj is None or (not math.isfinite(float(vj))) or _sign(float(vj)) != -1:
                        ok = False
                        break
                if ok:
                    return 0.5 * (float(strikes[i]) + float(strikes[i + 1]))
        return None

    return None


def _classify_stability(
    *,
    strikes: List[float],
    vals0_5: List[Optional[float]],
    spot: float,
    em_pts: Optional[float],
    downside_em: Optional[float],
    upside_em: Optional[float],
    fragile_band_em: float = 0.75,
    asym_diff_em: float = 0.5,
) -> Dict[str, Any]:
    """
    Stability label priority (explicit):
    1) Fragile if negative gamma exists within ±0.75 EM of spot
    2) Asymmetric if |distance_up_EM - distance_down_EM| > 0.5 EM
    3) Stable otherwise
    """
    reasons: List[str] = []
    s0 = float(spot)

    # 1) Fragile
    if em_pts is not None and float(em_pts) > 1e-9:
        band = float(fragile_band_em) * float(em_pts)
        lo = s0 - band
        hi = s0 + band
        neg_found = False
        for k, v in zip(strikes, vals0_5):
            if v is None:
                continue
            if float(k) < lo or float(k) > hi:
                continue
            if float(v) < 0:
                neg_found = True
                break
        if neg_found:
            reasons.append(f"Fragile: negative gamma detected within ±{fragile_band_em:.2f} EM of spot")
            return {"label": "Fragile", "reasons": reasons}

    # 2) Asymmetric
    if downside_em is None or upside_em is None:
        reasons.append("Asymmetric: one-sided boundary missing (insufficient data for both sides)")
        return {"label": "Asymmetric", "reasons": reasons}
    if abs(float(upside_em) - float(downside_em)) > float(asym_diff_em):
        reasons.append(f"Asymmetric: upside vs downside boundary distance differs by > {asym_diff_em:.2f} EM")
        return {"label": "Asymmetric", "reasons": reasons}

    # 3) Stable
    reasons.append("Stable: no negative gamma near spot and boundaries are balanced")
    return {"label": "Stable", "reasons": reasons}



def _pick_live_expiry(expirations_rows: List[dict], *, today: dt.date) -> Optional[str]:
    # Backwards-compatible wrapper: preserve old semantics (nearest / 0DTE).
    exp_dates: List[str] = []
    for r in expirations_rows or []:
        if not isinstance(r, dict):
            continue
        d0 = str(r.get("expirDate") or r.get("expiry") or r.get("expDate") or r.get("exp_date") or "")[:10]
        if d0 and len(d0) >= 10:
            exp_dates.append(d0)
    return _pick_nearest_expiry_date(exp_dates, today=today)


def _infer_live_expiries_from_strikes(rows: List[dict]) -> List[str]:
    exp_dates: List[str] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        d0 = str(r.get("expirDate") or r.get("expiry") or r.get("expDate") or r.get("exp_date") or "")[:10]
        if d0 and len(d0) >= 10:
            exp_dates.append(d0)
    return sorted(list(dict.fromkeys(exp_dates)))


def _select_expiry_from_dates(exp_dates: List[str], *, today: dt.date) -> Optional[str]:
    # Backwards-compatible wrapper: preserve old semantics (nearest / 0DTE).
    return _pick_nearest_expiry_date(exp_dates, today=today)


def _filter_chain_by_expiry(rows: List[dict], *, expiry: str) -> List[dict]:
    ex = str(expiry)[:10]
    out: List[dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        d0 = str(r.get("expirDate") or r.get("expiry") or r.get("expDate") or r.get("exp_date") or "")[:10]
        if d0 == ex:
            out.append(r)
    return out


def _live_chain_with_fallback(
    client: OratsClient,
    *,
    tickers: List[str],
    expiry: str,
    fields: str,
) -> Tuple[Optional[str], List[dict], List[str]]:
    warnings: List[str] = []
    for t in tickers:
        try:
            # IMPORTANT (weeklies): /live/strikes/monthly can omit weeklies.
            # Use full /live/strikes and filter locally by expiry.
            resp = client.live_strikes(ticker=t, fields=fields)
            rows = [r for r in (resp.rows or []) if isinstance(r, dict)]
            ex = str(expiry)[:10]
            filt = [r for r in rows if str(r.get("expirDate") or r.get("expiry") or r.get("expDate") or r.get("exp_date") or "")[:10] == ex]
            if filt:
                return t, filt, warnings
            warnings.append(f"Live chain empty for {t} expiry={ex} (filtered from live_strikes)")
        except Exception as e:
            warnings.append(f"Live chain error for {t}: {type(e).__name__}")
    return None, [], warnings


def _compute_gamma_flip_strike(
    chain_rows: List[dict],
    *,
    spot: float,
    band_pct: float,
    weighting_mode: str,
    contract_multiplier: int = 100,
) -> Optional[float]:
    """
    Best-effort "gamma flip" proxy: find a strike where net gamma exposure changes sign.

    We compute per-strike netGex ≈ gamma * (callWeight - putWeight) * multiplier
    within a spot band and look for a sign change across adjacent strikes. If found,
    return the flip closest to spot (linear interpolation).
    """
    try:
        s0 = float(spot)
    except Exception:
        return None
    if not math.isfinite(s0) or s0 <= 0:
        return None

    lo = s0 * (1.0 - float(band_pct))
    hi = s0 * (1.0 + float(band_pct))

    pts: List[Tuple[float, float]] = []
    for r in chain_rows or []:
        if not isinstance(r, dict):
            continue
        strike = _to_float(r.get("strike"))
        gamma = _to_float(r.get("gamma"))
        if strike is None or gamma is None:
            continue
        k = float(strike)
        if not (lo <= k <= hi):
            continue
        if weighting_mode == "oi":
            c = _to_float(r.get("callOpenInterest")) or 0.0
            p = _to_float(r.get("putOpenInterest")) or 0.0
        elif weighting_mode == "volume":
            c = _to_float(r.get("callVolume")) or 0.0
            p = _to_float(r.get("putVolume")) or 0.0
        else:
            c = 1.0
            p = 1.0
        c = max(0.0, float(c))
        p = max(0.0, float(p))
        net = float(gamma) * (c - p) * float(contract_multiplier)
        if math.isfinite(net):
            pts.append((k, float(net)))

    if len(pts) < 2:
        return None
    pts.sort(key=lambda x: x[0])

    best = None
    best_dist = None
    prev_k, prev_v = pts[0]
    prev_sign = 0 if prev_v == 0 else (1 if prev_v > 0 else -1)
    for k, v in pts[1:]:
        sign = 0 if v == 0 else (1 if v > 0 else -1)
        if sign == 0:
            flip = float(k)
        elif prev_sign == 0:
            flip = float(prev_k)
        elif sign != prev_sign:
            # Linear interpolation between (prev_k, prev_v) and (k, v)
            denom = (v - prev_v)
            if denom == 0:
                flip = float(k)
            else:
                t = (0.0 - prev_v) / denom
                t = max(0.0, min(1.0, float(t)))
                flip = float(prev_k) + t * (float(k) - float(prev_k))
        else:
            flip = None

        if flip is not None and math.isfinite(flip):
            dist = abs(float(flip) - s0)
            if best is None or best_dist is None or dist < best_dist:
                best = float(flip)
                best_dist = float(dist)

        prev_k, prev_v, prev_sign = float(k), float(v), sign

    return best


def compute_live_levels(
    client: OratsClient,
    *,
    underlying: str,
    symbols: Optional[Tuple[str, ...]] = None,
    view: str = "weekly",
    now_dt: Optional[dt.datetime] = None,
    band_pct: float = 0.05,
    top_n: int = 5,
    cluster_steps: int = 2,
    include_heatmap: bool = True,
    heatmap_expiries: int = 30,
    heatmap_band_pct: Optional[float] = None,
    heatmap_mode: str = "net",  # net|slope (display hint; payload contains both)
    heatmap_view: str = "composite",  # composite|raw (display hint; payload contains both)
    slope_window: int = 5,
    flip_adjacent_n: int = 5,
) -> Dict[str, Any]:
    """
    Compute LIVE dealer-gamma and OI wall/cluster levels (informational).

    view:
      - "weekly": prefer weekly Friday expiry
      - "nearest": prefer nearest expiry / 0DTE
    """
    now_et = _now_et(now_dt)
    today = now_et.date()

    under = str(underlying or "").strip().upper()
    if not under:
        return {
            "enabled": False,
            "view": "weekly" if str(view).lower().startswith("week") else "nearest",
            "symbolUsed": None,
            "expiry": None,
            "spot": None,
            "bandPct": float(band_pct),
            "weightingMode": None,
            "gammaFlipStrike": None,
            "dealerGamma": None,
            "oiClusters": None,
            "warnings": ["Missing underlying."],
            "notes": ["Live context unavailable (missing underlying)."],
        }

    # Symbol selection:
    # - SPX keeps SPXW->SPX->SPY fallbacks for robustness.
    # - Equities use only the single-name symbol (no proxies).
    symbols0 = tuple(
        str(s).strip().upper()
        for s in (symbols or (("SPXW", "SPX", "SPY") if under == "SPX" else (under,)))
        if str(s).strip()
    )
    if not symbols0:
        symbols0 = (under,)
    fields0 = "ticker,tradeDate,expirDate,expiry,expDate,exp_date,strike,spotPrice,stockPrice,gamma,callOpenInterest,putOpenInterest,callVolume,putVolume,callMidIv,putMidIv"

    exp_warn: List[str] = []
    strikes_cache_by_symbol: Dict[str, List[dict]] = {}
    exp_dates_by_symbol: Dict[str, List[str]] = {}

    for sym in symbols0:
        exp_rows: List[dict] = []
        exp_dates: List[str] = []
        try:
            exp_rows = client.live_expirations(ticker=sym).rows or []
        except Exception as e:
            exp_warn.append(f"Live expirations error for {sym}: {type(e).__name__}: {e}")
            exp_rows = []

        if exp_rows:
            for r in exp_rows:
                if not isinstance(r, dict):
                    continue
                d0 = str(r.get("expirDate") or r.get("expiry") or r.get("expDate") or r.get("exp_date") or "")[:10]
                if d0 and len(d0) >= 10:
                    exp_dates.append(d0)
        else:
            # Fallback: infer expiries from full strikes payload (cached short-TTL in ORATS client).
            try:
                all_rows = client.live_strikes(ticker=sym, fields=fields0).rows or []
                all_rows = [r for r in all_rows if isinstance(r, dict)]
                strikes_cache_by_symbol[sym] = all_rows
                exp_dates = _infer_live_expiries_from_strikes(all_rows)
            except Exception as e:
                exp_warn.append(f"Live strikes fallback error for {sym}: {type(e).__name__}: {e}")
                exp_dates = []

        exp_dates_by_symbol[sym] = exp_dates

    def _pick_expiry_for_symbol(sym: str) -> Optional[str]:
        ds = exp_dates_by_symbol.get(sym) or []
        if str(view).lower().startswith("week"):
            return _pick_weekly_close_expiry_date(ds, today=today, now_dt=now_dt)
        return _pick_nearest_expiry_date(ds, today=today)

    used_symbol = None
    used_expiry = None
    used_exp_dates: List[str] = []
    chain_rows: List[dict] = []
    chain_warn: List[str] = []

    for sym in symbols0:
        ex = _pick_expiry_for_symbol(sym)
        if not ex:
            continue
        used_chain_sym, rows, warn = _live_chain_with_fallback(client, tickers=[sym], expiry=ex, fields=fields0)
        if (not rows) and sym in strikes_cache_by_symbol:
            rows = _filter_chain_by_expiry(strikes_cache_by_symbol.get(sym) or [], expiry=ex)
            if rows:
                warn = [*warn, "Live strikes-by-expiry empty; used full strikes filtered by expiry."]
        if rows:
            used_symbol = used_chain_sym or sym
            used_expiry = ex
            used_exp_dates = exp_dates_by_symbol.get(sym) or []
            chain_rows = rows
            chain_warn = warn
            break

    if not chain_rows:
        return {
            "enabled": False,
            "view": "weekly" if str(view).lower().startswith("week") else "nearest",
            "symbolUsed": None,
            "expiry": None,
            "spot": None,
            "bandPct": float(band_pct),
            "weightingMode": None,
            "gammaFlipStrike": None,
            "dealerGamma": None,
            "oiClusters": None,
            "warnings": exp_warn,
            "notes": ["Live context unavailable (no usable chain rows)."],
        }

    dg = compute_dealer_gamma_context(chain_rows, expiry=used_expiry, contract_multiplier=100, band_pct=float(band_pct), top_n=int(top_n))
    oi = compute_open_interest_clusters(chain_rows, expiry=used_expiry, band_pct=float(band_pct), top_n=int(top_n), cluster_steps=int(cluster_steps))
    spot = dg.get("spot")
    wmode = str(dg.get("weightingMode") or "oi")
    flip = None
    if spot is not None:
        flip = _compute_gamma_flip_strike(chain_rows, spot=float(spot), band_pct=float(band_pct), weighting_mode=wmode, contract_multiplier=100)

    gex_heatmap = None
    if bool(include_heatmap) and used_symbol:
        try:
            band_h = float(heatmap_band_pct) if (heatmap_band_pct is not None) else float(band_pct)

            # Prefer a full strikes cache if we already have it.
            all_rows = strikes_cache_by_symbol.get(used_symbol)
            if all_rows is None:
                all_rows = [r for r in (client.live_strikes(ticker=used_symbol, fields=fields0).rows or []) if isinstance(r, dict)]

            # Prefer spot from the full rows in case the selected-expiry rows were sparse.
            s0 = _pick_spot_from_live_rows(all_rows) or (float(spot) if spot is not None else None)
            if s0 is not None:
                # --- Pick expiries for RAW grid (need to cover out to 40 DTE for composite buckets) ---
                exp_dtes = _select_expiries_by_dte(used_exp_dates, today=today, dte_max=45, cap=int(heatmap_expiries))
                exp_window = [e for (e, _) in exp_dtes]
                dte_by_exp = {e: int(dte) for (e, dte) in exp_dtes}

                raw = compute_spx_net_gex_heatmap(
                    all_rows,
                    expiries=exp_window,
                    spot=float(s0),
                    band_pct=float(band_h),
                    contract_multiplier=100,
                    strike_cap=180,
                )

                strikes = [float(x) for x in (raw.get("strikes") or [])]
                raw_rows = raw.get("netDollarGex") if isinstance(raw.get("netDollarGex"), list) else []
                raw_rows = [r if isinstance(r, list) else [] for r in raw_rows]

                # --- Slope mode (computed on RAW Net $GEX; normalization applies only at render time) ---
                slope_rows = [_apply_slope([_finite(x) for x in row], window=int(slope_window)) for row in raw_rows]

                # --- Daily IV proxy for normalization + EM (prefer ORATS cores iv30) ---
                iv_notes: List[str] = []
                iv_used_pct = None
                iv_trade = prior_trading_day(client, ticker=under, date=today) or today
                try:
                    fields_iv = "ticker,tradeDate,iv30,iv30d,iv30Day,iv"
                    core_rows = fetch_hist_cores_range(client, ticker=under, start=iv_trade - dt.timedelta(days=30), end=iv_trade, fields=fields_iv)
                    core_rows = [r for r in core_rows if isinstance(r, dict)]
                    core_rows.sort(key=lambda r: str(r.get("tradeDate") or "")[:10])
                    best_iv = None
                    for r in reversed(core_rows):
                        for k in ("iv30", "iv30d", "iv30Day", "iv"):
                            best_iv = _iv_to_pct(r.get(k))
                            if best_iv is not None:
                                break
                        if best_iv is not None:
                            break
                    iv_used_pct = best_iv
                except Exception:
                    iv_used_pct = None

                if iv_used_pct is None:
                    # Fallback: monies implied vol50 around ~30DTE (still daily, but slower / entitlement-dependent)
                    try:
                        iv_used_pct = fetch_atm_iv_pct(client, ticker=under, trade_date=iv_trade, dte_target=30)
                        if iv_used_pct is not None:
                            iv_notes.append("ATM IV proxy fell back to monies-implied vol50 (30DTE).")
                    except Exception:
                        iv_used_pct = None

                iv_dec = (float(iv_used_pct) / 100.0) if (iv_used_pct is not None and float(iv_used_pct) > 0) else None
                scale_denom = (float(s0) * float(iv_dec)) if (iv_dec is not None) else None
                if scale_denom is None:
                    iv_notes.append("ATM IV proxy unavailable; normalization disabled (raw color scaling).")

                # --- Composite rows (expiry buckets weighted by exponential DTE decay) ---
                base_weights = {"0_5": 1.0, "6_10": 0.6, "20_40": 0.25}
                half_life = 2.0
                expiries_used: List[dict] = []
                for e in exp_window:
                    dte = dte_by_exp.get(e)
                    b = _bucket_for_dte(int(dte)) if dte is not None else None
                    if b is None:
                        continue
                    w = float(base_weights.get(b, 0.0)) * _exp_decay_weight(dte=int(dte), half_life_dte=float(half_life))
                    expiries_used.append({"expiry": e, "dte": int(dte), "bucket": b, "weight": round(float(w), 8)})

                buckets_out: List[dict] = []
                bucket_keys = ["0_5", "6_10", "20_40"]
                for b in bucket_keys:
                    idxs: List[int] = []
                    ws: List[float] = []
                    dtes_for_bucket: List[int] = []
                    for i, e in enumerate(exp_window):
                        dte = dte_by_exp.get(e)
                        if dte is None:
                            continue
                        if _bucket_for_dte(int(dte)) != b:
                            continue
                        w = float(base_weights.get(b, 0.0)) * _exp_decay_weight(dte=int(dte), half_life_dte=float(half_life))
                        if w <= 0:
                            continue
                        idxs.append(i)
                        ws.append(float(w))
                        dtes_for_bucket.append(int(dte))

                    rows_sel = [raw_rows[i] for i in idxs]
                    net_row = _weighted_sum_rows(rows=rows_sel, weights=ws) if rows_sel else [None] * len(strikes)
                    slope_row = _apply_slope([_finite(x) for x in net_row], window=int(slope_window)) if net_row else [None] * len(strikes)

                    # effectiveDTE = Σ(w*dte)/Σ(w)
                    den = sum(ws) if ws else 0.0
                    eff_dte = (sum(float(w) * float(d) for (w, d) in zip(ws, dtes_for_bucket)) / den) if den > 1e-12 else None
                    em_pts = None
                    if iv_dec is not None and eff_dte is not None and float(eff_dte) > 0:
                        em_pts = float(s0) * float(iv_dec) * math.sqrt(float(eff_dte) / 252.0)

                    buckets_out.append(
                        {
                            "key": b,
                            "label": _bucket_label(b),
                            "effectiveDte": None if eff_dte is None else round(float(eff_dte), 4),
                            "expectedMovePts": None if em_pts is None else round(float(em_pts), 4),
                            "rowsUsed": len(rows_sel),
                            "netDollarGex": [None if x is None else round(float(x), 6) for x in net_row],
                            "slopeNetDollarGex": [None if x is None else round(float(x), 6) for x in slope_row],
                        }
                    )

                # --- Boundaries computed ONLY from 0–5 composite row ---
                b0 = next((x for x in buckets_out if x.get("key") == "0_5"), None) or {}
                vals0 = [(_finite(x) if x is not None else None) for x in (b0.get("netDollarGex") or [])]
                em0 = _finite(b0.get("expectedMovePts"))
                down_strike = _find_accel_boundary_from_spot(strikes=strikes, vals=vals0, spot=float(s0), side="down", adjacent_n=int(flip_adjacent_n))
                up_strike = _find_accel_boundary_from_spot(strikes=strikes, vals=vals0, spot=float(s0), side="up", adjacent_n=int(flip_adjacent_n))

                down_pts = (float(s0) - float(down_strike)) if (down_strike is not None) else None
                up_pts = (float(up_strike) - float(s0)) if (up_strike is not None) else None
                down_em = (float(down_pts) / float(em0)) if (down_pts is not None and em0 is not None and em0 > 1e-9) else None
                up_em = (float(up_pts) / float(em0)) if (up_pts is not None and em0 is not None and em0 > 1e-9) else None

                stability = _classify_stability(
                    strikes=strikes,
                    vals0_5=vals0,
                    spot=float(s0),
                    em_pts=em0,
                    downside_em=down_em,
                    upside_em=up_em,
                    fragile_band_em=0.75,
                    asym_diff_em=0.5,
                )

                gex_heatmap = {
                    "enabled": True,
                    "display": {"view": str(heatmap_view or "composite"), "mode": str(heatmap_mode or "net")},
                    "spot": round(float(s0), 6),
                    "bandPct": float(band_h),
                    "weightingMode": raw.get("weightingMode"),
                    "contractMultiplier": int(raw.get("contractMultiplier") or 100),
                    "atmIvUsedPct": None if iv_used_pct is None else round(float(iv_used_pct), 4),
                    "scaleDenom": None if scale_denom is None else round(float(scale_denom), 8),
                    "raw": {
                        "expiries": raw.get("expiries") or [],
                        "dteByExpiry": dte_by_exp,
                        "strikes": raw.get("strikes") or [],
                        "netDollarGex": raw.get("netDollarGex") or [],
                        "slopeNetDollarGex": [[None if x is None else round(float(x), 6) for x in row] for row in slope_rows],
                    },
                    "composite": {
                        "halfLifeDte": float(half_life),
                        "baseWeights": base_weights,
                        "expiriesUsed": expiries_used,
                        "strikes": raw.get("strikes") or [],
                        "buckets": buckets_out,
                    },
                    "boundaries": {
                        "flipAdjacentN": int(flip_adjacent_n),
                        "downsideAccelerationBoundaryStrike": None if down_strike is None else round(float(down_strike), 2),
                        "upsideAccelerationBoundaryStrike": None if up_strike is None else round(float(up_strike), 2),
                    },
                    "metrics": {
                        "downsideDistancePts": None if down_pts is None else round(float(down_pts), 2),
                        "upsideDistancePts": None if up_pts is None else round(float(up_pts), 2),
                        "downsideDistanceEm": None if down_em is None else round(float(down_em), 4),
                        "upsideDistanceEm": None if up_em is None else round(float(up_em), 4),
                    },
                    "stability": stability,
                    "warnings": [*list(raw.get("warnings") or []), *iv_notes],
                    "notes": [
                        "Slope is computed on raw Net $GEX; normalization applies only at render time.",
                        *list(raw.get("notes") or []),
                    ],
                }
        except Exception as e:
            # Keep endpoint resilient: heatmap is additive UI. Don't fail the entire levels payload.
            LOG.exception("Failed to compute SPX net $GEX heatmap")
            gex_heatmap = {
                "enabled": False,
                "error": f"{type(e).__name__}",
                "notes": ["Heatmap unavailable (best-effort)."],
            }

    notes_out = [
        "Live, informational only. Does not change backtest/odds.",
        "Best-effort: depends on entitlement, session timing, and chain coverage.",
    ]
    if under == "SPX":
        notes_out.append("SPXW/SPX may differ from SPY proxy intraday depending on entitlement and session.")

    return {
        "enabled": True,
        "view": "weekly" if str(view).lower().startswith("week") else "nearest",
        "symbolUsed": used_symbol,
        "expiry": str(used_expiry)[:10] if used_expiry else None,
        "spot": dg.get("spot"),
        "bandPct": float(band_pct),
        "weightingMode": wmode,
        "gammaFlipStrike": None if flip is None else round(float(flip), 2),
        "dealerGamma": dg,
        "oiClusters": oi,
        "gexHeatmap": gex_heatmap,
        "warnings": [*exp_warn, *list(chain_warn or []), *list(dg.get("warnings") or []), *list(oi.get("warnings") or [])],
        "notes": notes_out,
    }


def compute_spx_live_levels(
    client: OratsClient,
    *,
    view: str = "weekly",
    now_dt: Optional[dt.datetime] = None,
    band_pct: float = 0.05,
    top_n: int = 5,
    cluster_steps: int = 2,
    include_heatmap: bool = True,
    heatmap_expiries: int = 30,
    heatmap_band_pct: Optional[float] = None,
    heatmap_mode: str = "net",  # net|slope (display hint; payload contains both)
    heatmap_view: str = "composite",  # composite|raw (display hint; payload contains both)
    slope_window: int = 5,
    flip_adjacent_n: int = 5,
) -> Dict[str, Any]:
    """
    Back-compat wrapper for older callers.
    """
    return compute_live_levels(
        client,
        underlying="SPX",
        symbols=("SPXW", "SPX"),
        view=view,
        now_dt=now_dt,
        band_pct=band_pct,
        top_n=top_n,
        cluster_steps=cluster_steps,
        include_heatmap=include_heatmap,
        heatmap_expiries=heatmap_expiries,
        heatmap_band_pct=heatmap_band_pct,
        heatmap_mode=heatmap_mode,
        heatmap_view=heatmap_view,
        slope_window=slope_window,
        flip_adjacent_n=flip_adjacent_n,
    )


def _row_dte_days(row: dict, *, trade_date: dt.date) -> Optional[float]:
    """
    Prefer ORATS-provided dte; otherwise compute from expirDate - trade_date.
    """
    dte = _to_float(row.get("dte"))
    if dte is not None:
        return float(dte)
    exp = row.get("expirDate") or row.get("expiryDate") or row.get("exp_date") or row.get("expDate")
    if not exp:
        return None
    try:
        ed = _parse_date(str(exp))
        return float((ed - trade_date).days)
    except Exception:
        return None


def _quarter_key(d: dt.date) -> str:
    q = ((d.month - 1) // 3) + 1
    return f"Q{q}"


def _pct_ret(a: float, b: float) -> float:
    return (b / a - 1.0) * 100.0


# ---- Daily bars (OHLC) ----
@dataclass(frozen=True)
class DailyOHLC:
    trade_date: str
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[float] = None
    vwap: Optional[float] = None


_ohlc_cache = TTLCache(maxsize=250_000, ttl=24 * 60 * 60)
_ohlc_lock = threading.Lock()


def _first_row(rows: Any) -> Optional[dict]:
    if not rows or not isinstance(rows, list):
        return None
    for r in rows:
        if isinstance(r, dict):
            return r
    return None


def fetch_daily_ohlc(client: OratsClient, *, ticker: str, date: dt.date) -> Optional[DailyOHLC]:
    """
    Best-effort OHLC fetch for a trade date.
    Uses ORATS /hist/dailies; field names can vary by entitlement/plan.
    """
    key = ("ohlc", ticker, _fmt_date(date))
    cached = _cache_get(_ohlc_cache, _ohlc_lock, key)
    if cached is not None:
        return cached

    try:
        fields = "ticker,tradeDate,open,opPx,hiPx,loPx,clsPx,close,high,low,volume,vol,vwap"
        resp = client.hist_dailies(ticker=ticker, trade_date=_fmt_date(date), fields=fields)
        row = _first_row(resp.rows)
    except Exception:
        row = None

    if not row:
        _cache_set(_ohlc_cache, _ohlc_lock, key, None)
        return None

    td = str(row.get("tradeDate") or _fmt_date(date))[:10]
    o = _to_float(row.get("open") or row.get("opPx") or row.get("op_px"))
    h = _to_float(row.get("hiPx") or row.get("high") or row.get("hi") or row.get("hPx"))
    l = _to_float(row.get("loPx") or row.get("low") or row.get("lo") or row.get("lPx"))
    c = _to_float(row.get("clsPx") or row.get("close") or row.get("cls_px"))
    vol = _to_float(row.get("volume") or row.get("vol") or row.get("totalVolume"))
    vwap = _to_float(row.get("vwap"))
    out = DailyOHLC(trade_date=td, open=o, high=h, low=l, close=c, volume=vol, vwap=vwap)
    _cache_set(_ohlc_cache, _ohlc_lock, key, out)
    return out


def fetch_close_px(client: OratsClient, *, ticker: str, date: dt.date) -> Optional[float]:
    """
    Fetch close price for ticker on a given trade date (EOD).
    We only need clsPx; tolerate missing stockPrice in some ORATS responses.
    """
    bar = fetch_daily_ohlc(client, ticker=ticker, date=date)
    return None if bar is None else bar.close


def fetch_open_px(client: OratsClient, *, ticker: str, date: dt.date) -> Optional[float]:
    bar = fetch_daily_ohlc(client, ticker=ticker, date=date)
    return None if bar is None else bar.open


def fetch_high_low(client: OratsClient, *, ticker: str, date: dt.date) -> Tuple[Optional[float], Optional[float]]:
    bar = fetch_daily_ohlc(client, ticker=ticker, date=date)
    if bar is None:
        return None, None
    return bar.high, bar.low


# ---- Regime helpers (SPX-focused, risk-only) ----
def clamp(lo: float, hi: float, x: float) -> float:
    return max(lo, min(hi, float(x)))


def percentile_rank(x: float, xs: List[float]) -> Optional[float]:
    vals = [v for v in xs if v is not None and isinstance(v, (int, float)) and math.isfinite(v)]
    if not vals:
        return None
    c = sum(1 for v in vals if v <= x)
    return c / len(vals)


def _log_returns(closes: List[float]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(closes)):
        a = closes[i - 1]
        b = closes[i]
        if a and a > 0 and b and b > 0:
            out.append(math.log(b / a))
    return out


def _rv_annualized(logrets: List[float], window: int = 20) -> Optional[float]:
    if len(logrets) < window or window < 2:
        return None
    w = logrets[-window:]
    if len(w) < 2:
        return None
    return statistics.stdev(w) * math.sqrt(252.0)


def _rolling_rv20(logrets: List[float], lookback: int = 252, window: int = 20) -> List[float]:
    out: List[float] = []
    start = max(window, len(logrets) - lookback)
    for i in range(start, len(logrets) + 1):
        w = logrets[i - window : i]
        if len(w) < window:
            continue
        if len(w) >= 2:
            out.append(statistics.stdev(w) * math.sqrt(252.0))
    return out


def _rolling_abs_ret_5d(closes: List[float], lookback: int = 252, window: int = 5) -> List[float]:
    out: List[float] = []
    if len(closes) < window + 1:
        return out
    start = max(window, len(closes) - lookback)
    for i in range(start, len(closes)):
        a = closes[i - window]
        b = closes[i]
        if a and a > 0 and b and b > 0:
            out.append(abs(b / a - 1.0))
    return out


def _label_from_tail_multiplier(tm: float) -> str:
    if tm < 0.9:
        return "Calm"
    if tm < 1.3:
        return "Normal"
    if tm < 1.6:
        return "Elevated"
    return "Stress"


def _trade_gate(label: str) -> str:
    if label == "Stress":
        return "NO_TRADE"
    if label == "Elevated":
        return "CAUTION"
    return "OK"

def _zscore(x: float, xs: List[float]) -> Optional[float]:
    vals = [float(v) for v in xs if v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))]
    if len(vals) < 30:
        return None
    mu = statistics.mean(vals)
    sd = statistics.stdev(vals) if len(vals) >= 2 else 0.0
    if sd <= 1e-9:
        return None
    return (float(x) - mu) / sd


def _ema(xs: List[float], span: int) -> List[float]:
    if not xs:
        return []
    a = 2.0 / (float(span) + 1.0)
    out = [float(xs[0])]
    for i in range(1, len(xs)):
        out.append(a * float(xs[i]) + (1.0 - a) * out[-1])
    return out


def _true_range(prev_close: float, high: float, low: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def _atr20(bars: List[DailyOHLC]) -> Optional[float]:
    vals: List[float] = []
    # Need close for prev day; require 21 bars minimum for 20 TRs
    if len(bars) < 21:
        return None
    for i in range(1, len(bars)):
        b0 = bars[i - 1]
        b1 = bars[i]
        if b0.close is None or b1.high is None or b1.low is None:
            continue
        vals.append(_true_range(float(b0.close), float(b1.high), float(b1.low)))
    if len(vals) < 20:
        return None
    return statistics.mean(vals[-20:])


def _parkinson_vol(bars: List[DailyOHLC], window: int = 20) -> Optional[float]:
    """
    Parkinson volatility estimator (uses high/low only), annualized.
    """
    if len(bars) < window:
        return None
    vals = []
    for b in bars[-window:]:
        if b.high is None or b.low is None or b.high <= 0 or b.low <= 0:
            return None
        vals.append(math.log(float(b.high) / float(b.low)) ** 2)
    if not vals:
        return None
    sigma2 = (1.0 / (4.0 * math.log(2.0))) * (sum(vals) / len(vals))
    # daily to annual
    return math.sqrt(max(0.0, sigma2) * 252.0)


def _yang_zhang_vol(bars: List[DailyOHLC], window: int = 20) -> Optional[float]:
    """
    Yang-Zhang volatility estimator (uses open/high/low/close), annualized.
    """
    if len(bars) < window + 1:
        return None
    use = bars[-(window + 1) :]
    ro = []
    rc = []
    rs = []
    for i in range(1, len(use)):
        b0 = use[i - 1]
        b1 = use[i]
        if b0.close is None or b1.open is None or b1.close is None or b1.high is None or b1.low is None:
            return None
        c0 = float(b0.close)
        o1 = float(b1.open)
        c1 = float(b1.close)
        h1 = float(b1.high)
        l1 = float(b1.low)
        if c0 <= 0 or o1 <= 0 or c1 <= 0 or h1 <= 0 or l1 <= 0:
            return None
        ro.append(math.log(o1 / c0))
        rc.append(math.log(c1 / o1))
        rs.append(math.log(h1 / o1) * math.log(h1 / c1) + math.log(l1 / o1) * math.log(l1 / c1))
    if len(ro) < 2:
        return None
    k = 0.34 / (1.34 + (window + 1.0) / (window - 1.0))
    sigma_o2 = statistics.variance(ro) if len(ro) >= 2 else 0.0
    sigma_c2 = statistics.variance(rc) if len(rc) >= 2 else 0.0
    sigma_rs = sum(rs) / len(rs)
    yz = sigma_o2 + k * sigma_c2 + (1.0 - k) * sigma_rs
    return math.sqrt(max(0.0, yz) * 252.0)


def _parse_float_list(s: str) -> List[float]:
    out: List[float] = []
    for part in str(s or "").split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.append(float(p))
        except Exception:
            continue
    return out


def _parse_int_list(s: str) -> List[int]:
    out: List[int] = []
    for part in str(s or "").split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.append(int(float(p)))
        except Exception:
            continue
    return out


def _is_summer(d: dt.date) -> bool:
    return d.month in (6, 7, 8)


def _is_opex_week(d: dt.date) -> bool:
    """
    OpEx week: week containing the 3rd Friday of the month.
    """
    # Find third Friday of the month
    first = dt.date(d.year, d.month, 1)
    # move to first Friday
    ff = first
    while ff.weekday() != 4:
        ff += dt.timedelta(days=1)
    third_friday = ff + dt.timedelta(days=14)
    # define "week" as Mon..Fri containing that Friday
    mon = third_friday - dt.timedelta(days=4)
    fri = third_friday
    return mon <= d <= fri


def _regime_bucket(score100: float, flags: FeatureFlags) -> str:
    s = float(score100)
    if s <= float(flags.ENGINE2_REGIME_LOW_MAX):
        return "LOW"
    if s <= float(flags.ENGINE2_REGIME_MODERATE_MAX):
        return "MODERATE"
    if s <= float(flags.ENGINE2_REGIME_ELEVATED_MAX):
        return "ELEVATED"
    return "NO_TRADE"


def _risk01_from_z_abs(z: Optional[float], *, z0: float = 0.0, z1: float = 2.0) -> float:
    if z is None:
        return 0.5
    x = abs(float(z))
    return clamp(0.0, 1.0, (x - z0) / max(1e-9, (z1 - z0)))


def _risk01_from_ratio(x: Optional[float], *, lo: float, hi: float) -> float:
    if x is None:
        return 0.5
    return clamp(0.0, 1.0, (float(x) - lo) / max(1e-9, (hi - lo)))


def _pctile_or_default(x: Optional[float], xs: List[float], default: float = 0.5) -> float:
    if x is None:
        return float(default)
    p = percentile_rank(float(x), xs)
    return float(default) if p is None else float(p)


def _macro_classify_name(name: str) -> Optional[str]:
    n = str(name or "").lower()
    if not n:
        return None
    if "cpi" in n or "consumer price" in n:
        return "CPI"
    if "fomc" in n or "fed rate" in n or "interest rate decision" in n:
        return "FOMC"
    if "nonfarm" in n or "nfp" in n or "payroll" in n:
        return "NFP"
    if "refunding" in n or "treasury" in n and "auction" in n:
        return "REFUNDING"
    return None


def fetch_trading_closes(
    client: OratsClient,
    *,
    ticker: str,
    end: dt.date,
    n: int = 320,
    max_calendar_scan: int = 520,
) -> List[Tuple[str, float]]:
    """
    Build a trailing close series by walking back calendar days and keeping dates where
    ORATS provides a close. This avoids needing a range endpoint.
    """
    series: List[Tuple[str, float]] = []
    d = end
    scanned = 0
    while len(series) < n and scanned < max_calendar_scan:
        px = fetch_close_px(client, ticker=ticker, date=d)
        if px is not None and px > 0:
            series.append((_fmt_date(d), float(px)))
        d = d - dt.timedelta(days=1)
        scanned += 1
    series.reverse()
    return series


_atm_iv_cache = TTLCache(maxsize=50_000, ttl=24 * 60 * 60)
_atm_iv_lock = threading.Lock()

_macro_cache = TTLCache(maxsize=5_000, ttl=6 * 60 * 60)
_macro_lock = threading.Lock()

_iv_curve_cache = TTLCache(maxsize=50_000, ttl=24 * 60 * 60)
_iv_curve_lock = threading.Lock()


def _cache_get(cache: TTLCache, lock: threading.Lock, key: tuple) -> Any:
    with lock:
        return cache.get(key)


def _cache_set(cache: TTLCache, lock: threading.Lock, key: tuple, val: Any) -> None:
    with lock:
        cache[key] = val


def find_trading_day(client: OratsClient, *, ticker: str, start: dt.date, step: int, max_steps: int = 10) -> Optional[dt.date]:
    """Walk calendar days until we find a date with a close price."""
    d = start
    for _ in range(max_steps):
        if fetch_close_px(client, ticker=ticker, date=d) is not None:
            return d
        d = d + dt.timedelta(days=step)
    return None


def next_trading_day(client: OratsClient, *, ticker: str, date: dt.date) -> Optional[dt.date]:
    return find_trading_day(client, ticker=ticker, start=date, step=+1, max_steps=10)


def prior_trading_day(client: OratsClient, *, ticker: str, date: dt.date) -> Optional[dt.date]:
    return find_trading_day(client, ticker=ticker, start=date, step=-1, max_steps=10)


def fetch_atm_iv_pct(
    client: OratsClient,
    *,
    ticker: str,
    trade_date: dt.date,
    dte_target: int,
) -> Optional[float]:
    """
    Approximate ATM IV from ORATS monies implied surface: use vol50 (call-delta 50) for nearest expiry to dte_target.
    Returns IV as a percent (e.g., 15.2).
    """
    key = ("atm_iv50", ticker, _fmt_date(trade_date), int(dte_target))
    cached = _cache_get(_atm_iv_cache, _atm_iv_lock, key)
    if cached is not None:
        return cached

    lo = max(1, int(dte_target) - 2)
    hi = int(dte_target) + 7
    iv = None
    try:
        fields = "ticker,tradeDate,expirDate,dte,stockPrice,vol50"
        resp = client.hist_monies_implied(ticker=ticker, trade_date=_fmt_date(trade_date), fields=fields, dte=f"{lo},{hi}")
        rows = resp.rows or []
        best = None
        best_dist = None
        for r in rows:
            if not isinstance(r, dict):
                continue
            dte = _row_dte_days(r, trade_date=trade_date)
            v = _iv_to_pct(r.get("vol50"))
            if dte is None or v is None:
                continue
            dist = abs(float(dte) - float(dte_target))
            if best is None or (best_dist is not None and dist < best_dist):
                best = r
                best_dist = dist
        if best is not None:
            iv = _iv_to_pct(best.get("vol50"))
    except Exception:
        iv = None

    _cache_set(_atm_iv_cache, _atm_iv_lock, key, iv)
    return iv


def fetch_iv_curve(
    client: OratsClient,
    *,
    ticker: str,
    trade_date: dt.date,
    dte_targets: List[int],
) -> Dict[int, Optional[float]]:
    """
    Fetch vol50 for several DTE targets (best-effort) in one ORATS call.
    Returns mapping dte_target -> vol50 (percent).
    """
    key = ("iv_curve", ticker, _fmt_date(trade_date), tuple(int(x) for x in dte_targets))
    cached = _cache_get(_iv_curve_cache, _iv_curve_lock, key)
    if cached is not None:
        return cached

    out: Dict[int, Optional[float]] = {int(x): None for x in dte_targets}
    if not dte_targets:
        _cache_set(_iv_curve_cache, _iv_curve_lock, key, out)
        return out

    lo = max(1, min(int(x) for x in dte_targets) - 2)
    hi = max(int(x) for x in dte_targets) + 7
    try:
        fields = "ticker,tradeDate,expirDate,dte,vol50"
        resp = client.hist_monies_implied(ticker=ticker, trade_date=_fmt_date(trade_date), fields=fields, dte=f"{lo},{hi}")
        rows = resp.rows or []
    except Exception:
        rows = []

    for target in dte_targets:
        best = None
        best_dist = None
        for r in rows:
            if not isinstance(r, dict):
                continue
            dte = _row_dte_days(r, trade_date=trade_date)
            v = _iv_to_pct(r.get("vol50"))
            if dte is None or v is None:
                continue
            dist = abs(float(dte) - float(target))
            if best is None or best_dist is None or dist < best_dist:
                best = r
                best_dist = dist
        out[int(target)] = None if best is None else _iv_to_pct(best.get("vol50"))

    _cache_set(_iv_curve_cache, _iv_curve_lock, key, out)
    return out


def fetch_iv_pack(
    client: OratsClient,
    *,
    ticker: str,
    trade_date: dt.date,
    dte_targets: List[int],
) -> Dict[int, Optional[float]]:
    """
    Fetch vol50 once and pick best matches for multiple DTE targets.
    This is a single-call version of fetch_atm_iv_pct + fetch_iv_curve.
    Returns mapping dte_target -> vol50 (percent).
    """
    key = ("iv_pack", ticker, _fmt_date(trade_date), tuple(int(x) for x in dte_targets))
    cached = _cache_get(_iv_curve_cache, _iv_curve_lock, key)
    if cached is not None:
        return cached

    out: Dict[int, Optional[float]] = {int(x): None for x in dte_targets}
    if not dte_targets:
        _cache_set(_iv_curve_cache, _iv_curve_lock, key, out)
        return out

    lo = max(1, min(int(x) for x in dte_targets) - 2)
    hi = max(int(x) for x in dte_targets) + 7
    try:
        fields = "ticker,tradeDate,expirDate,dte,vol50"
        resp = client.hist_monies_implied(ticker=ticker, trade_date=_fmt_date(trade_date), fields=fields, dte=f"{lo},{hi}")
        rows = resp.rows or []
    except Exception:
        rows = []

    for target in dte_targets:
        best = None
        best_dist = None
        for r in rows:
            if not isinstance(r, dict):
                continue
            dte = _row_dte_days(r, trade_date=trade_date)
            v = _iv_to_pct(r.get("vol50"))
            if dte is None or v is None:
                continue
            dist = abs(float(dte) - float(target))
            if best is None or best_dist is None or dist < best_dist:
                best = r
                best_dist = dist
        out[int(target)] = None if best is None else _iv_to_pct(best.get("vol50"))

    _cache_set(_iv_curve_cache, _iv_curve_lock, key, out)
    return out


def _iv_pack_from_rows(*, rows: List[dict], trade_date: dt.date, dte_targets: List[int]) -> Dict[int, Optional[float]]:
    """
    Pick best vol50 matches for multiple DTE targets from a pre-fetched rows list
    (e.g., from a tradeDate range query).
    Returns mapping dte_target -> vol50 (percent).
    """
    out: Dict[int, Optional[float]] = {int(x): None for x in dte_targets}
    if not rows or not dte_targets:
        return out

    for target in dte_targets:
        best = None
        best_dist = None
        for r in rows:
            if not isinstance(r, dict):
                continue
            dte = _row_dte_days(r, trade_date=trade_date)
            v = _iv_to_pct(r.get("vol50"))
            if dte is None or v is None:
                continue
            dist = abs(float(dte) - float(target))
            if best is None or best_dist is None or dist < best_dist:
                best = r
                best_dist = dist
        out[int(target)] = None if best is None else _iv_to_pct(best.get("vol50"))

    return out


def fetch_monies_implied_range(
    client: OratsClient,
    *,
    ticker: str,
    start: dt.date,
    end: dt.date,
    dte_lo: int,
    dte_hi: int,
) -> List[dict]:
    """
    Best-effort bulk fetch for ORATS /hist/monies/implied using tradeDate ranges if supported.
    If the endpoint doesn't support ranges for your entitlement, this returns [] and callers
    should fall back to per-date fetch_iv_pack().
    """
    if end < start:
        return []
    td = f"{_fmt_date(start)},{_fmt_date(end)}"
    try:
        fields = "ticker,tradeDate,expirDate,dte,vol50"
        resp = client.hist_monies_implied(ticker=ticker, trade_date=td, fields=fields, dte=f"{int(dte_lo)},{int(dte_hi)}")
        return [r for r in (resp.rows or []) if isinstance(r, dict)]
    except Exception:
        return []


def fetch_hist_cores_range(
    client: OratsClient,
    *,
    ticker: str,
    start: dt.date,
    end: dt.date,
    fields: str,
) -> List[dict]:
    """
    Range fetch for ORATS /hist/cores using fromDate/toDate (fast).
    This endpoint supports range mode (see backend/regime_overlay.py for similar usage).
    """
    if end < start:
        return []
    get_fn = getattr(client, "get", None)
    if not callable(get_fn):
        return []
    resp = get_fn(
        "/hist/cores",
        {"ticker": ticker, "fromDate": _fmt_date(start), "toDate": _fmt_date(end), "fields": fields},
    )
    return [r for r in (resp.rows or []) if isinstance(r, dict)]


def iv_to_em1sigma_pct(*, iv_pct: float, dte_calendar_days: int) -> float:
    """
    Convert annualized IV (%) to a 1-sigma expected move (%) over T calendar days.
    """
    t = max(1, int(dte_calendar_days)) / 365.0
    return float(iv_pct) * math.sqrt(t)


@dataclass(frozen=True)
class WeeklyWindow:
    entry_date: dt.date
    expiry_date: dt.date
    dte_sessions: int
    dte_calendar_days: int


def count_trading_sessions(client: OratsClient, *, ticker: str, start: dt.date, end: dt.date) -> int:
    """Count trading sessions between start and end inclusive (best-effort)."""
    if end < start:
        return 0
    n = 0
    d = start
    # windows are short (<= ~2 weeks), so day-by-day scan is fine with cached closes
    while d <= end and n < 30:
        if fetch_close_px(client, ticker=ticker, date=d) is not None:
            n += 1
        d += dt.timedelta(days=1)
    return n


def build_weekly_windows(
    client: OratsClient,
    *,
    ticker: str,
    start: dt.date,
    end: dt.date,
    entry_dow: int,  # 0=Mon
    max_weeks: int = 260,
) -> List[WeeklyWindow]:
    """
    Build (entry_date, expiry_date) weekly windows for IC backtests.
    entry_dow controls which day we enter (Mon/Tue/Wed).
    Expiry is the Friday of the same calendar week.
    """
    # Find the first Monday on/after start, then iterate weeks.
    d = start
    while d.weekday() != 0:
        d += dt.timedelta(days=1)

    out: List[WeeklyWindow] = []
    while d <= end and len(out) < max_weeks:
        entry_anchor = d + dt.timedelta(days=int(entry_dow))
        expiry_anchor = d + dt.timedelta(days=4)  # Friday
        entry_td = next_trading_day(client, ticker=ticker, date=entry_anchor)
        exp_td = next_trading_day(client, ticker=ticker, date=expiry_anchor)
        if entry_td and exp_td and entry_td < exp_td:
            dte = (exp_td - entry_td).days
            dte_sessions = count_trading_sessions(client, ticker=ticker, start=entry_td, end=exp_td)
            out.append(WeeklyWindow(entry_date=entry_td, expiry_date=exp_td, dte_sessions=int(dte_sessions), dte_calendar_days=int(dte)))
        d += dt.timedelta(days=7)
    return out


def build_weekly_windows_from_trade_dates(
    *,
    trade_dates: List[str],
    start: dt.date,
    end: dt.date,
    entry_dow: int,  # 0=Mon
    max_weeks: int = 260,
) -> List[WeeklyWindow]:
    """
    Build (entry_date, expiry_date) weekly windows without per-day ORATS calls.

    This uses the already-fetched OHLC trade_dates (EOD) to:
    - find the next available trading day on/after entry anchor
    - find the next available trading day on/after Friday anchor
    - count trading sessions via index range (fast)
    """
    if not trade_dates:
        return []

    # Ensure sorted YYYY-MM-DD strings.
    dates_sorted = sorted([str(d)[:10] for d in trade_dates if d])
    date_set = set(dates_sorted)
    idx = {d: i for i, d in enumerate(dates_sorted)}

    def _next_td(d: dt.date, *, max_steps: int = 10) -> Optional[dt.date]:
        x = d
        for _ in range(max_steps):
            k = _fmt_date(x)
            if k in date_set:
                return x
            x += dt.timedelta(days=1)
        return None

    # Find the first Monday on/after start.
    d = start
    while d.weekday() != 0:
        d += dt.timedelta(days=1)

    out: List[WeeklyWindow] = []
    while d <= end and len(out) < max_weeks:
        entry_anchor = d + dt.timedelta(days=int(entry_dow))
        expiry_anchor = d + dt.timedelta(days=4)  # Friday
        entry_td = _next_td(entry_anchor, max_steps=10)
        exp_td = _next_td(expiry_anchor, max_steps=10)
        if entry_td and exp_td and entry_td < exp_td:
            ek = _fmt_date(entry_td)
            fk = _fmt_date(exp_td)
            i0 = idx.get(ek)
            i1 = idx.get(fk)
            dte_sessions = (int(i1) - int(i0) + 1) if (i0 is not None and i1 is not None and i1 >= i0) else 0
            dte_calendar = int((exp_td - entry_td).days)
            if dte_sessions > 0:
                out.append(
                    WeeklyWindow(
                        entry_date=entry_td,
                        expiry_date=exp_td,
                        dte_sessions=int(dte_sessions),
                        dte_calendar_days=int(dte_calendar),
                    )
                )
        d += dt.timedelta(days=7)

    return out


def fetch_trading_bars(
    client: OratsClient,
    *,
    ticker: str,
    end: dt.date,
    n: int = 900,
    max_calendar_scan: int = 1400,
) -> List[DailyOHLC]:
    """
    Pull up to `n` most recent trading-day OHLC bars up to `end` (inclusive), walking back calendar days.
    Deterministic (EOD) and cached per-date.
    """
    out: List[DailyOHLC] = []
    d = end
    scanned = 0
    while len(out) < n and scanned < max_calendar_scan:
        b = fetch_daily_ohlc(client, ticker=ticker, date=d)
        if b and b.close is not None and b.close > 0:
            out.append(b)
        d = d - dt.timedelta(days=1)
        scanned += 1
    out.reverse()
    return out


def fetch_dailies_ohlc_range(
    client: OratsClient,
    *,
    ticker: str,
    start: dt.date,
    end: dt.date,
) -> List[DailyOHLC]:
    """
    Fast path: ORATS /hist/dailies supports tradeDate ranges of the form:
      tradeDate=YYYY-MM-DD,YYYY-MM-DD
    This returns many rows in one request (massively faster than per-day probing).
    """
    if end < start:
        return []
    try:
        td = f"{_fmt_date(start)},{_fmt_date(end)}"
        fields = "ticker,tradeDate,open,opPx,hiPx,loPx,clsPx,close,high,low,volume,vol,vwap"
        resp = client.hist_dailies(ticker=ticker, trade_date=td, fields=fields)
        rows = resp.rows or []
    except Exception:
        rows = []
    out: List[DailyOHLC] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        td0 = str(r.get("tradeDate") or "")[:10]
        if not td0:
            continue
        o = _to_float(r.get("open") or r.get("opPx") or r.get("op_px"))
        h = _to_float(r.get("hiPx") or r.get("high") or r.get("hi") or r.get("hPx"))
        l = _to_float(r.get("loPx") or r.get("low") or r.get("lo") or r.get("lPx"))
        c = _to_float(r.get("clsPx") or r.get("close") or r.get("cls_px"))
        if c is None or c <= 0:
            continue
        vol = _to_float(r.get("volume") or r.get("vol") or r.get("totalVolume"))
        vwap = _to_float(r.get("vwap"))
        out.append(DailyOHLC(trade_date=td0, open=o, high=h, low=l, close=c, volume=vol, vwap=vwap))
    out.sort(key=lambda b: b.trade_date)
    return out


def fetch_close_map_range(
    client: OratsClient,
    *,
    ticker: str,
    start: dt.date,
    end: dt.date,
) -> Dict[str, float]:
    """Convenience: date->close using range pull."""
    out: Dict[str, float] = {}
    for b in fetch_dailies_ohlc_range(client, ticker=ticker, start=start, end=end):
        if b.close is None:
            continue
        out[b.trade_date] = float(b.close)
    return out


def compute_regime_score_for_date(
    client: OratsClient,
    *,
    ticker: str,
    as_of: dt.date,
    bars: List[DailyOHLC],
    flags: FeatureFlags,
    iv_weekly_sample: Dict[str, Dict[str, float]] | None = None,
    sector_dispersion_cache: Dict[str, float] | None = None,
    macro_multiplier: float = 1.0,
    macro_flags: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Compute a 0..100 regime risk score with component breakdown.
    Uses daily OHLC (no intraday), ORATS implied surface (weekly samples), and Benzinga macro overlay handled elsewhere.
    """
    asof = _fmt_date(as_of)
    # index up to as_of
    idx = None
    for i in range(len(bars) - 1, -1, -1):
        if bars[i].trade_date <= asof:
            idx = i
            break
    if idx is None or idx < 60:
        return {
            "asOfDate": asof,
            "score100": 50.0,
            "bucket": _regime_bucket(50.0, flags),
            "label": "Insufficient history",
            "components": {},
            "inputs": {},
            "notes": ["Insufficient history to compute full regime."],
        }

    use = bars[: idx + 1]
    closes = [float(b.close) for b in use if b.close is not None]
    dates = [b.trade_date for b in use if b.close is not None]
    if len(closes) < 60:
        return {
            "asOfDate": asof,
            "score100": 50.0,
            "bucket": _regime_bucket(50.0, flags),
            "label": "Insufficient history",
            "components": {},
            "inputs": {},
            "notes": ["Insufficient history to compute full regime."],
        }

    # ---- Trend block ----
    # 5d return z-score vs 1y distribution
    ret5 = None
    ret5_hist = []
    if len(closes) >= 6:
        ret5 = (closes[-1] / closes[-6] - 1.0) * 100.0
        # build trailing 1y ret5 distribution
        start = max(5, len(closes) - 252)
        for i in range(start, len(closes)):
            a = closes[i - 5]
            b = closes[i]
            if a > 0 and b > 0:
                ret5_hist.append((b / a - 1.0) * 100.0)
    z5 = _zscore(float(ret5 or 0.0), ret5_hist) if ret5 is not None else None

    # EMA slope / ATR + distance from 20DMA / ATR
    ema20 = _ema(closes, 20)
    sma20 = statistics.mean(closes[-20:]) if len(closes) >= 20 else closes[-1]
    atr20 = _atr20(use[-21:]) if len(use) >= 21 else None
    ema_slope_norm = None
    if len(ema20) >= 6 and atr20 and atr20 > 0:
        ema_slope_norm = abs(ema20[-1] - ema20[-6]) / float(atr20)
    dist20_norm = None
    if atr20 and atr20 > 0:
        dist20_norm = abs(closes[-1] - float(sma20)) / float(atr20)

    trend_risk = clamp(
        0.0,
        1.0,
        0.45 * _risk01_from_z_abs(z5, z1=2.0)
        + 0.30 * _risk01_from_ratio(ema_slope_norm, lo=0.0, hi=2.0)
        + 0.25 * _risk01_from_ratio(dist20_norm, lo=0.0, hi=2.0),
    )

    # ---- Volatility block ----
    logrets = _log_returns(closes)
    rv20 = _rv_annualized(logrets, window=20)
    rv5 = _rv_annualized(logrets, window=5) if len(logrets) >= 5 else None
    rv_ratio = (float(rv5) / float(rv20)) if (rv5 is not None and rv20 is not None and rv20 > 1e-9) else None
    rv_hist = _rolling_rv20(logrets, lookback=252, window=20)
    rv20_pct = _pctile_or_default(rv20, rv_hist, default=0.5) if rv20 is not None else 0.5

    # Implied proxy: use weekly-sampled iv7/iv30 if provided
    iv30 = None
    iv7 = None
    iv30_pct = None
    term_slope = None
    vv = None
    if iv_weekly_sample and asof in iv_weekly_sample:
        iv7 = iv_weekly_sample[asof].get("iv7")
        iv30 = iv_weekly_sample[asof].get("iv30")
        term_slope = None if (iv7 is None or iv30 is None) else float(iv7) - float(iv30)
        # vol-of-vol proxy using prior sample
        prev_dates = sorted([d for d in iv_weekly_sample.keys() if d < asof])
        if prev_dates and iv7 is not None:
            p = iv_weekly_sample[prev_dates[-1]].get("iv7")
            if p is not None and float(iv7) > 0:
                vv = abs(float(iv7) - float(p)) / float(iv7)
        # percentile vs weekly history
        iv30_hist = [v.get("iv30") for d, v in iv_weekly_sample.items() if d <= asof and v.get("iv30") is not None]
        iv30_pct = percentile_rank(float(iv30), [float(x) for x in iv30_hist if x is not None]) if (iv30 is not None and iv30_hist) else None

    iv_risk = _pctile_or_default(iv30, [float(x.get("iv30")) for x in (iv_weekly_sample or {}).values() if x.get("iv30") is not None], default=0.5) if iv30 is not None else 0.5
    term_risk = clamp(0.0, 1.0, (float(term_slope) + 2.0) / 6.0) if term_slope is not None else 0.5
    vv_risk = clamp(0.0, 1.0, (float(vv) - 0.02) / 0.10) if vv is not None else 0.5
    vol_risk = clamp(0.0, 1.0, 0.45 * rv20_pct + 0.25 * _risk01_from_ratio(rv_ratio, lo=0.8, hi=1.6) + 0.20 * iv_risk + 0.10 * max(term_risk, vv_risk))

    # ---- Stress block ----
    # EM(1d) from iv7 if available else rv20
    em1d = None
    if iv7 is not None:
        em1d = float(iv7) * math.sqrt(1.0 / 365.0)
    elif rv20 is not None:
        # rv20 is annualized stdev; use it as a rough implied proxy
        em1d = float(rv20) * 100.0 * math.sqrt(1.0 / 252.0)
    # last daily return, range, gap
    last = use[-1]
    prev = use[-2] if len(use) >= 2 else None
    daily_abs_ret = abs((closes[-1] / closes[-2] - 1.0) * 100.0) if len(closes) >= 2 else None
    rng = None
    gap = None
    if last.high is not None and last.low is not None and last.close is not None and last.close > 0:
        rng = (float(last.high) - float(last.low)) / float(last.close) * 100.0
    if last.open is not None and prev and prev.close is not None and prev.close > 0:
        gap = abs(float(last.open) - float(prev.close)) / float(prev.close) * 100.0

    shock = None if (daily_abs_ret is None or em1d is None or em1d <= 1e-9) else float(daily_abs_ret) / float(em1d)
    rng_em = None if (rng is None or em1d is None or em1d <= 1e-9) else float(rng) / float(em1d)
    gap_em = None if (gap is None or em1d is None or em1d <= 1e-9) else float(gap) / float(em1d)

    stress_risk = clamp(
        0.0,
        1.0,
        0.45 * _risk01_from_ratio(shock, lo=0.5, hi=2.0)
        + 0.35 * _risk01_from_ratio(rng_em, lo=0.8, hi=2.5)
        + 0.20 * _risk01_from_ratio(gap_em, lo=0.3, hi=1.5),
    )

    # ---- Dispersion block ----
    disp = None
    if sector_dispersion_cache and asof in sector_dispersion_cache:
        disp = sector_dispersion_cache[asof]
    disp_risk = clamp(0.0, 1.0, (float(disp) - 0.005) / 0.02) if disp is not None else 0.5

    # ---- Event overlay (macro proximity + event flags) ----
    mm = float(macro_multiplier or 1.0)
    event_risk = clamp(0.0, 1.0, (mm - 1.0) / max(1e-9, float(flags.ENGINE2_MACRO_MULTIPLIER_CAP) - 1.0))
    # If key flags exist, nudge upward (bounded).
    if macro_flags and isinstance(macro_flags, dict):
        bump = 0.0
        for k in ("CPI", "FOMC", "NFP"):
            if macro_flags.get(k) is True:
                bump += 0.10
        if macro_flags.get("OPEX") is True:
            bump += 0.05
        event_risk = clamp(0.0, 1.0, event_risk + bump)

    score01 = (
        0.30 * vol_risk
        + 0.25 * stress_risk
        + 0.20 * trend_risk
        + 0.15 * event_risk
        + 0.10 * disp_risk
    )
    score100 = round(clamp(0.0, 100.0, 100.0 * score01), 2)
    bucket = _regime_bucket(score100, flags)

    return {
        "asOfDate": asof,
        "score100": score100,
        "bucket": bucket,
        "label": bucket.title().replace("_", " "),
        "components": {
            "trend": round(trend_risk, 3),
            "volatility": round(vol_risk, 3),
            "stress": round(stress_risk, 3),
            "event": round(event_risk, 3),
            "dispersion": round(disp_risk, 3),
        },
        "inputs": {
            "ret5Pct": None if ret5 is None else round(float(ret5), 3),
            "ret5Z": None if z5 is None else round(float(z5), 2),
            "emaSlopeNorm": None if ema_slope_norm is None else round(float(ema_slope_norm), 3),
            "dist20Norm": None if dist20_norm is None else round(float(dist20_norm), 3),
            "rv20": None if rv20 is None else round(float(rv20), 3),
            "rv5": None if rv5 is None else round(float(rv5), 3),
            "rv5OverRv20": None if rv_ratio is None else round(float(rv_ratio), 3),
            "rv20Percentile": round(float(rv20_pct), 3),
            "iv7": None if iv7 is None else round(float(iv7), 2),
            "iv30": None if iv30 is None else round(float(iv30), 2),
            "iv30PercentileApprox": None if iv30_pct is None else round(float(iv30_pct), 3),
            "termSlopeIv7MinusIv30": None if term_slope is None else round(float(term_slope), 3),
            "volOfVolAbsD7OverIv7": None if vv is None else round(float(vv), 4),
            "em1dPct": None if em1d is None else round(float(em1d), 3),
            "dailyAbsRetPct": None if daily_abs_ret is None else round(float(daily_abs_ret), 3),
            "rangePct": None if rng is None else round(float(rng), 3),
            "gapPct": None if gap is None else round(float(gap), 3),
            "shockOverEm1d": None if shock is None else round(float(shock), 3),
            "rangeOverEm1d": None if rng_em is None else round(float(rng_em), 3),
            "gapOverEm1d": None if gap_em is None else round(float(gap_em), 3),
            "sectorDispersion": None if disp is None else round(float(disp), 6),
            "parkinsonVol20": None if (_parkinson_vol(use[-20:]) is None) else round(float(_parkinson_vol(use[-20:])), 3),
            "yangZhangVol20": None if (_yang_zhang_vol(use[-21:]) is None) else round(float(_yang_zhang_vol(use[-21:])), 3),
        },
        "notes": [
            "IV inputs are optional; when unavailable, regime falls back to realized-vol + OHLC stress proxies.",
        ],
    }


def compute_sector_dispersion_series(
    client: OratsClient,
    *,
    dates: List[str],
    sector_tickers: List[str],
) -> Dict[str, float]:
    """
    Dispersion proxy: cross-sectional stdev of 1-day returns across sector ETFs.
    Returns mapping tradeDate -> dispersion value (unitless, e.g., 0.01 = 1%).
    """
    # NOTE: The old implementation did per-day per-ticker calls, which is too slow.
    # We now range-fetch each sector once and compute dispersion on intersected dates.
    out: Dict[str, float] = {}
    if len(dates) < 2 or not sector_tickers:
        return out
    try:
        start = _parse_date(dates[0])
        end = _parse_date(dates[-1])
    except Exception:
        return out

    closes_by_ticker: Dict[str, Dict[str, float]] = {}
    for t in sector_tickers:
        closes_by_ticker[t] = fetch_close_map_range(client, ticker=t, start=start, end=end)

    for i in range(1, len(dates)):
        d0 = dates[i - 1]
        d1 = dates[i]
        rets: List[float] = []
        for t in sector_tickers:
            m = closes_by_ticker.get(t) or {}
            a = m.get(d0)
            b = m.get(d1)
            if a is None or b is None or a <= 0:
                continue
            rets.append((float(b) / float(a)) - 1.0)
        if len(rets) >= max(4, len(sector_tickers) // 2):
            out[d1] = float(statistics.pstdev(rets))
    return out

def _macro_context(
    bz: BenzingaClient,
    *,
    start: dt.date,
    end: dt.date,
    as_of: dt.date,
    flags: FeatureFlags,
    economics_rows: Optional[List[dict]] = None,
) -> Dict[str, Any]:
    key = ("macro_v2", _fmt_date(start), _fmt_date(end), _fmt_date(as_of), float(flags.ENGINE2_MACRO_LAMBDA))
    cached = _cache_get(_macro_cache, _macro_lock, key)
    if cached is not None:
        return cached

    out: Dict[str, Any] = {
        "window": {"start": _fmt_date(start), "end": _fmt_date(end)},
        "highImpactUS": {"count": 0, "top": []},
        "flags": {"CPI": False, "FOMC": False, "NFP": False, "OPEX": False, "REFUNDING": False},
        "multiplier": 1.0,
        "components": {"CPI": 0.0, "FOMC": 0.0, "NFP": 0.0, "OPEX": 0.0, "REFUNDING": 0.0, "OTHER": 0.0},
        "sources": [],
        "notes": [],
    }
    try:
        if economics_rows is None:
            resp = bz.calendar_economics(date_from=_fmt_date(start), date_to=_fmt_date(end), pagesize=1000, page=0)
            out["sources"].append("benzinga:/calendar/economics")
            rows = resp.rows or []
        else:
            rows = list(economics_rows or [])
        hi = []
        scored = []
        for r in rows:
            try:
                imp = int(float(r.get("importance") or 0))
            except Exception:
                imp = 0
            ctry = str(r.get("country") or "").upper()
            if ctry and ctry not in ("US", "UNITED STATES", "USA"):
                continue
            if imp >= 3:
                name = str(r.get("event_name") or "").strip()
                date = str(r.get("date") or "")[:10]
                hi.append((imp, date, name))
                k = _macro_classify_name(name)
                if k:
                    out["flags"][k] = True
                # proximity decay relative to as_of (entry date)
                try:
                    d = _parse_date(date)
                    days = abs((d - as_of).days)
                except Exception:
                    days = None
                if days is not None:
                    decay = math.exp(-float(flags.ENGINE2_MACRO_LAMBDA) * float(days))
                    base = 0.0
                    if k == "CPI":
                        base = float(flags.ENGINE2_MACRO_BASE_CPI)
                    elif k == "FOMC":
                        base = float(flags.ENGINE2_MACRO_BASE_FOMC)
                    elif k == "NFP":
                        base = float(flags.ENGINE2_MACRO_BASE_NFP)
                    elif k == "REFUNDING":
                        base = float(flags.ENGINE2_MACRO_BASE_REFUNDING)
                    else:
                        base = 0.25
                    scored.append((k or "OTHER", base * decay, date, name))
        # Sort by importance desc then date asc
        hi.sort(key=lambda x: (-x[0], x[1]))
        out["highImpactUS"]["count"] = int(len(hi))
        out["highImpactUS"]["top"] = [f"{d} {n}".strip() for (_, d, n) in hi[:6] if (d or n)]

        # OpEx proximity flag (calendar rule)
        out["flags"]["OPEX"] = bool(_is_opex_week(end))
        if out["flags"]["OPEX"]:
            out["components"]["OPEX"] = float(flags.ENGINE2_MACRO_BASE_OPEX)

        # Sum weighted components
        for k, w, _, _ in scored:
            if k in out["components"]:
                out["components"][k] += float(w)
            else:
                out["components"]["OTHER"] += float(w)

        total_risk = sum(float(v) for v in out["components"].values() if v is not None)
        mult = 1.0 + float(total_risk)
        out["multiplier"] = clamp(1.0, float(flags.ENGINE2_MACRO_MULTIPLIER_CAP), mult)
    except Exception as e:
        out["notes"].append(f"macro unavailable: {type(e).__name__}: {e}")

    _cache_set(_macro_cache, _macro_lock, key, out)
    return out


def _prefetch_benzinga_economics(
    bz: BenzingaClient,
    *,
    start: dt.date,
    end: dt.date,
    pagesize: int = 1000,
    max_pages: int = 8,
    importance: int | None = 3,
    country: str | None = "US",
) -> List[dict]:
    """
    Fetch Benzinga economics calendar once for a broad date range (paged), so we can
    compute per-week macro context without N network round-trips.
    """
    rows_all: List[dict] = []
    for page in range(int(max_pages)):
        resp = bz.calendar_economics(
            date_from=_fmt_date(start),
            date_to=_fmt_date(end),
            pagesize=int(pagesize),
            page=int(page),
            importance=(int(importance) if importance is not None else None),
            country=(str(country) if country else None),
        )
        batch = resp.rows or []
        rows_all.extend([r for r in batch if isinstance(r, dict)])
        if len(batch) < int(pagesize):
            break
    return rows_all


def backtest_weekly_ic_risk(
    client: OratsClient,
    *,
    ticker: str,
    years: int,
    entry_dow: int,
    widths: List[float],
    today: Optional[dt.date] = None,
) -> Dict[str, Any]:
    """
    Risk-only weekly IC backtest.
    - Breach defined at expiry close beyond short strike distance.
    - Short strike distance set in EM multiples: width * EM1sigma% (derived from ATM IV).
    """
    now = today or dt.date.today()
    start = now - dt.timedelta(days=int(years) * 365)
    end = now

    windows = build_weekly_windows(client, ticker=ticker, start=start, end=end, entry_dow=entry_dow, max_weeks=260 * max(1, int(years)))

    rows_out: List[Dict[str, Any]] = []
    per_width: Dict[float, Dict[str, Any]] = {float(w): {"w": float(w), "n": 0, "breachEither": 0, "breachPut": 0, "breachCall": 0, "avgAbsRetPct": 0.0} for w in widths}
    per_quarter: Dict[str, Dict[str, Any]] = {q: {float(w): {"n": 0, "breachEither": 0} for w in widths} for q in ("Q1", "Q2", "Q3", "Q4")}

    used = 0
    for win in windows:
        entry_bar = fetch_daily_ohlc(client, ticker=ticker, date=win.entry_date)
        exp_bar = fetch_daily_ohlc(client, ticker=ticker, date=win.expiry_date)
        entry_px = None if entry_bar is None else entry_bar.close
        exp_px = None if exp_bar is None else exp_bar.close
        if entry_px is None or exp_px is None or entry_px <= 0:
            continue
        iv = fetch_atm_iv_pct(client, ticker=ticker, trade_date=win.entry_date, dte_target=max(1, win.dte_calendar_days))
        if iv is None or iv <= 0:
            continue

        ret = _pct_ret(entry_px, exp_px)
        abs_ret = abs(ret)
        em1 = iv_to_em1sigma_pct(iv_pct=float(iv), dte_calendar_days=max(1, win.dte_calendar_days))
        qk = _quarter_key(win.entry_date)
        used += 1

        # MAE/MFE using daily highs/lows in window (close-to-extrema relative to entry close).
        # Touch is intentionally not modeled; this is a risk label.
        down_mae_pct: Optional[float] = 0.0
        up_mae_pct: Optional[float] = 0.0
        d = win.entry_date
        while d <= win.expiry_date and (win.expiry_date - win.entry_date).days <= 14:
            b = fetch_daily_ohlc(client, ticker=ticker, date=d)
            if b and b.high is not None and b.low is not None and entry_px and entry_px > 0:
                up = (float(b.high) / float(entry_px) - 1.0) * 100.0
                dn = (1.0 - float(b.low) / float(entry_px)) * 100.0
                up_mae_pct = max(float(up_mae_pct or 0.0), float(up))
                down_mae_pct = max(float(down_mae_pct or 0.0), float(dn))
            d += dt.timedelta(days=1)
        mae_abs_pct = max(float(up_mae_pct or 0.0), float(down_mae_pct or 0.0))

        row = {
            "entryDate": _fmt_date(win.entry_date),
            "expiryDate": _fmt_date(win.expiry_date),
            "dte": int(win.dte_sessions),
            "dteCalendar": int(win.dte_calendar_days),
            "entryPx": round(float(entry_px), 2),
            "expiryPx": round(float(exp_px), 2),
            "retPct": round(float(ret), 3),
            "absRetPct": round(float(abs_ret), 3),
            "maeDownPct": None if down_mae_pct is None else round(float(down_mae_pct), 3),
            "maeUpPct": None if up_mae_pct is None else round(float(up_mae_pct), 3),
            "maeAbsPct": round(float(mae_abs_pct), 3),
            "ivAtmPct": round(float(iv), 2),
            "em1sigmaPct": round(float(em1), 3),
            "quarter": qk,
            "byWidth": {},
        }

        for w in widths:
            dist = float(w) * float(em1)
            breach_put = ret < -dist
            breach_call = ret > dist
            breach = bool(breach_put or breach_call)
            row["byWidth"][str(w)] = {"distPct": round(dist, 3), "breach": breach, "breachSide": ("PUT" if breach_put else "CALL" if breach_call else None)}

            acc = per_width[float(w)]
            acc["n"] += 1
            acc["breachEither"] += 1 if breach else 0
            acc["breachPut"] += 1 if breach_put else 0
            acc["breachCall"] += 1 if breach_call else 0
            acc["avgAbsRetPct"] += float(abs_ret)

            qacc = per_quarter[qk][float(w)]
            qacc["n"] += 1
            qacc["breachEither"] += 1 if breach else 0

        rows_out.append(row)

    # finalize
    by_width = []
    for w, acc in per_width.items():
        n = int(acc["n"])
        if n > 0:
            avg_abs = float(acc["avgAbsRetPct"]) / n
            out = dict(acc)
            out["avgAbsRetPct"] = round(avg_abs, 3)
            out["breachEitherPct"] = round(acc["breachEither"] / n * 100.0, 2)
            out["breachPutPct"] = round(acc["breachPut"] / n * 100.0, 2)
            out["breachCallPct"] = round(acc["breachCall"] / n * 100.0, 2)
            by_width.append(out)
        else:
            by_width.append({**acc, "breachEitherPct": None, "breachPutPct": None, "breachCallPct": None})
    by_width.sort(key=lambda x: x["w"])

    by_q = {}
    for qk, wmap in per_quarter.items():
        by_q[qk] = {}
        for w, acc in wmap.items():
            n = int(acc["n"])
            by_q[qk][str(w)] = {"n": n, "breachEitherPct": (round(acc["breachEither"] / n * 100.0, 2) if n else None)}

    # Provide most recent windows first for UI
    rows_out.sort(key=lambda r: r["entryDate"], reverse=True)

    return {
        "rowsUsed": int(used),
        "rows": rows_out[:260],  # cap for payload size
        "byWidth": by_width,
        "byQuarter": by_q,
        "notes": [],
    }


def recommend_width(
    *,
    by_width: List[Dict[str, Any]],
    risk_target_breach_pct: float,
) -> Dict[str, Any]:
    """Pick the smallest width that meets breachEitherPct <= target (if possible)."""
    tgt = float(risk_target_breach_pct)
    eligible = [r for r in by_width if r.get("breachEitherPct") is not None and float(r["breachEitherPct"]) <= tgt]
    choice = eligible[0] if eligible else (by_width[-1] if by_width else None)
    if not choice:
        return {"width": None, "notes": ["No backtest rows available."]}
    return {
        "width": float(choice["w"]),
        "breachEitherPct": choice.get("breachEitherPct"),
        "notes": (["Meets risk target."] if eligible else ["No width met target; using widest candidate."]),
    }


def beta_binomial_mean(*, k: int, n: int, alpha: float = 1.0, beta: float = 1.0) -> Optional[float]:
    if n <= 0:
        return None
    return (float(k) + float(alpha)) / (float(n) + float(alpha) + float(beta))


def pctile(xs: List[float], p: float) -> Optional[float]:
    vals = sorted([float(x) for x in xs if x is not None and math.isfinite(float(x))])
    if not vals:
        return None
    if p <= 0:
        return vals[0]
    if p >= 100:
        return vals[-1]
    k = (len(vals) - 1) * (float(p) / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return vals[int(k)]
    d0 = vals[int(f)] * (c - k)
    d1 = vals[int(c)] * (k - f)
    return d0 + d1


def compute_engine2_spx_ic(
    *,
    client: OratsClient,
    benzinga_client: Optional[BenzingaClient],
    flags: FeatureFlags,
    underlying_preference: str = "SPX",  # SPX|SPY|QQQ
    entry_day: str = "mon",
    years: int = 3,
    widths: Optional[List[float]] = None,
    risk_target_breach_pct: float = 25.0,
    seasonality_mode: str = "none",  # none|quarter|month|summer|opex
    today: Optional[dt.date] = None,
) -> Dict[str, Any]:
    """
    Main Engine 2 payload generator.
    Uses SPY as the default proxy for SPX if SPX is not available in ORATS dailies.
    """
    t0 = time.perf_counter()
    telemetry: Dict[str, Any] = {"timingsMs": {}, "counts": {}, "notes": []}

    def mark(name: str) -> None:
        telemetry["timingsMs"][name] = int(round((time.perf_counter() - t0) * 1000.0))

    def add_count(name: str, delta: int = 1) -> None:
        telemetry["counts"][name] = int(telemetry["counts"].get(name, 0)) + int(delta)

    # Desk-locked config (Engine 2): simplify to the weekly IC workflow you trade.
    # - 2y lookback (~104 weekly observations per entry weekday)
    # - widths fixed to 1.0/1.5/2.0 × EM (short distance)
    # - wings fixed to 5pt (risk-defined)
    yrs = 2
    widths_use = [1.0, 1.5, 2.0]
    em_mults = list(widths_use)
    wing_pts = [5]
    ed = str(entry_day or "mon").strip().lower()
    entry_dow = 0 if ed.startswith("mon") else 1 if ed.startswith("tue") else 2 if ed.startswith("wed") else 0
    now = today or dt.date.today()
    # Use an explicit, timezone-aware "now" for live expiry roll logic.
    # (Weekly expiry rolls after 4:15pm ET on Fridays.)
    now_dt_utc = dt.datetime.now(dt.timezone.utc)
    season_mode = str(seasonality_mode or "none").strip().lower()
    LOG.info("Engine2 compute start (desk-locked): entry_day=%s years=%s widths=%s wingPts=%s seasonality=%s", ed, yrs, widths_use, wing_pts, season_mode)

    def _season_bucket(d: dt.date) -> str:
        if season_mode == "quarter":
            return _quarter_key(d)
        if season_mode == "month":
            return f"M{int(d.month):02d}"
        if season_mode == "summer":
            return "SUMMER" if _is_summer(d) else "NON_SUMMER"
        if season_mode == "opex":
            return "OPEX" if _is_opex_week(d) else "NON_OPEX"
        return "ALL"

    # Ticker selection:
    # - If preference=SPX: prefer SPX, fallback to SPY proxy if SPX dailies unavailable.
    # - If preference=SPY: prefer SPY, fallback to SPX proxy if SPY dailies unavailable (explicitly noted).
    proxy_notes: List[str] = []
    pref = str(underlying_preference or "SPX").strip().upper()
    if pref not in ("SPX", "SPY", "QQQ"):
        pref = "SPX"
        proxy_notes.append("Invalid underlying preference; defaulted to SPX.")

    # Underlying selection policy:
    # - Prefer the requested underlying.
    # - For SPX<->SPY only: allow a proxy fallback if the preferred ticker is unavailable in ORATS dailies.
    # - For QQQ: do not proxy.
    underlying = pref
    is_proxy = False

    # Use range probe (fast + consistent) to detect availability.
    probe_rows = fetch_dailies_ohlc_range(client, ticker=underlying, start=now - dt.timedelta(days=7), end=now)
    telemetry["counts"]["orats.probe_rows"] = len(probe_rows or [])
    if not probe_rows and pref in ("SPX", "SPY"):
        alt = "SPY" if pref == "SPX" else "SPX"
        probe_rows_alt = fetch_dailies_ohlc_range(client, ticker=alt, start=now - dt.timedelta(days=7), end=now)
        if probe_rows_alt:
            underlying = alt
            is_proxy = True
            proxy_notes.append(f"{pref} unavailable in ORATS dailies; using {alt} as a proxy for this run.")
            probe_rows = probe_rows_alt
            telemetry["counts"]["orats.probe_rows"] = len(probe_rows or [])
    if not probe_rows:
        raise OratsError(f"{underlying} unavailable in ORATS dailies (no rows returned for probe window).")

    # Build OHLC history once (range pull; fast).
    start_hist = now - dt.timedelta(days=int(yrs) * 365 + 120)
    bars = fetch_dailies_ohlc_range(client, ticker=underlying, start=start_hist, end=now)
    mark("orats.dailies_range")
    if not bars:
        # Fail safe: old slow path (should rarely happen)
        bars = fetch_trading_bars(client, ticker=underlying, end=now, n=1100, max_calendar_scan=1600)
        mark("orats.dailies_fallback_slow")
    trade_dates = [b.trade_date for b in bars]
    bar_by_date: Dict[str, DailyOHLC] = {b.trade_date: b for b in bars if b and b.trade_date}
    idx_by_date: Dict[str, int] = {b.trade_date: i for i, b in enumerate(bars) if b and b.trade_date}
    closes = [float(b.close) for b in bars if b.close is not None]
    logrets_all = _log_returns(closes)
    telemetry["counts"]["orats.dailies_rows"] = len(bars)
    telemetry["counts"]["trade_dates"] = len(trade_dates)

    # Build weekly windows for backtest (fast: derived from already-fetched trade_dates).
    windows = build_weekly_windows_from_trade_dates(
        trade_dates=trade_dates,
        start=(now - dt.timedelta(days=yrs * 365)),
        end=now,
        entry_dow=entry_dow,
        max_weeks=260 * yrs,
    )
    telemetry["counts"]["windows"] = len(windows)
    mark("build.windows")

    # IV samples are optional; in rate-limited environments we avoid per-week surface loads.
    iv_weekly_sample: Dict[str, Dict[str, float]] = {}
    # Per-week macro context (if Benzinga available)
    macro_by_entry: Dict[str, Dict[str, Any]] = {}

    # Batch fetch Benzinga economics once for the whole backtest span (avoid N network calls).
    econ_by_date: Dict[str, List[dict]] = {}
    if benzinga_client is not None:
        try:
            if windows:
                # IMPORTANT: ORATS EOD can lag during market hours, so the last backtest window may end
                # before the upcoming "next week" macro window. Ensure the prefetch also covers forward
                # dates from 'now' so the current macro panel is populated.
                econ_start = min(windows[0].entry_date - dt.timedelta(days=7), now - dt.timedelta(days=30))
                econ_end = max(windows[-1].expiry_date + dt.timedelta(days=7), now + dt.timedelta(days=21))
            else:
                econ_start = now - dt.timedelta(days=30)
                econ_end = now + dt.timedelta(days=21)
            # Fetch only the slice we actually use for the macro overlay: US + high-impact items.
            # This avoids huge pagination ranges that can omit recent dates depending on API ordering.
            econ_rows_all = _prefetch_benzinga_economics(
                benzinga_client,
                start=econ_start,
                end=econ_end,
                pagesize=1000,
                max_pages=8,
                importance=3,
                country="US",
            )
            telemetry["counts"]["benzinga.econ_rows"] = len(econ_rows_all)
            for r in econ_rows_all:
                d0 = str(r.get("date") or "")[:10]
                if not d0:
                    continue
                econ_by_date.setdefault(d0, []).append(r)
        except Exception:
            econ_by_date = {}
            telemetry["notes"].append("Benzinga economics prefetch failed (non-fatal).")
    mark("benzinga.economics_prefetch")

    # Batch fetch ORATS IV series via /hist/cores (fast, supports fromDate/toDate).
    # This avoids 100+ slow /hist/monies/implied calls when range mode isn't supported there.
    iv7_by_date: Dict[str, float] = {}
    iv30_by_date: Dict[str, float] = {}
    try:
        from_core = (now - dt.timedelta(days=int(yrs) * 365 + 120))
        to_core = now
        fields = "ticker,tradeDate,iv7,iv7d,iv7Day,iv30,iv30d,iv30Day,iv"
        core_rows = fetch_hist_cores_range(client, ticker=underlying, start=from_core, end=to_core, fields=fields)
        telemetry["counts"]["orats.cores_rows"] = len(core_rows)
        for r in core_rows:
            d0 = str(r.get("tradeDate") or "")[:10]
            if not d0:
                continue
            iv7 = None
            for k in ("iv7", "iv7d", "iv7Day"):
                iv7 = _iv_to_pct(r.get(k))
                if iv7 is not None:
                    break
            iv30 = None
            for k in ("iv30", "iv30d", "iv30Day", "iv"):
                iv30 = _iv_to_pct(r.get(k))
                if iv30 is not None:
                    break
            if iv7 is not None:
                iv7_by_date[d0] = float(iv7)
            if iv30 is not None:
                iv30_by_date[d0] = float(iv30)
    except Exception:
        telemetry["notes"].append("ORATS cores IV range fetch failed; IV inputs will be reduced (fallback to realized vol).")
        iv7_by_date = {}
        iv30_by_date = {}
    mark("orats.cores_iv_range")

    # Precompute sector dispersion (EOD) across trade_dates.
    sector_tickers = ["XLF", "XLK", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU"]
    sector_disp = compute_sector_dispersion_series(client, dates=trade_dates, sector_tickers=sector_tickers)
    telemetry["counts"]["orats.sector_tickers"] = len(sector_tickers)
    telemetry["counts"]["sector_dispersion_dates"] = len(sector_disp)
    mark("orats.sector_dispersion")

    # Collect week records and grid aggregations.
    week_rows: List[Dict[str, Any]] = []
    # Key: (entryDay, regimeBucket, macroBucket, emMult, wingPts)
    agg: Dict[Tuple[str, str, str, str, float, int], Dict[str, Any]] = {}

    def _macro_bucket(m: Dict[str, Any]) -> str:
        try:
            mult = float(m.get("multiplier") or 1.0)
        except Exception:
            mult = 1.0
        flags0 = m.get("flags") if isinstance(m.get("flags"), dict) else {}
        hi = any(bool(flags0.get(k)) for k in ("CPI", "FOMC", "NFP"))
        return "MACRO" if (mult >= 1.25 or hi) else "NORMAL"

    for win in windows:
        entry = win.entry_date
        expiry = win.expiry_date
        ek = _fmt_date(entry)
        fk = _fmt_date(expiry)
        entry_bar = bar_by_date.get(ek)
        exp_bar = bar_by_date.get(fk)
        if not entry_bar or not exp_bar or entry_bar.close is None or exp_bar.close is None or entry_bar.close <= 0:
            continue

        entry_px = float(entry_bar.close)
        exp_px = float(exp_bar.close)
        ret_pct = _pct_ret(entry_px, exp_px)

        # Weekly EM(1σ) using ORATS cores IV series (fast). Prefer iv7 for weekly horizons.
        dte_h = max(1, int(win.dte_calendar_days))
        iv7 = iv7_by_date.get(ek)
        iv30 = iv30_by_date.get(ek)
        iv_h = iv7 if iv7 is not None else iv30
        if iv_h is None or float(iv_h) <= 0:
            # Last resort: realized-vol proxy (keeps engine alive on missing IV rows)
            i0 = idx_by_date.get(ek)
            vol_ann = None
            if i0 is not None and i0 >= 3:
                lr = logrets_all[:i0]
                w = min(20, len(lr))
                if w >= 2:
                    try:
                        vol_ann = statistics.stdev(lr[-w:]) * math.sqrt(252.0)
                    except Exception:
                        vol_ann = None
            if vol_ann is None:
                vol_ann = _parkinson_vol(bars[: (i0 + 1)] if i0 is not None else bars)
            if vol_ann is None or float(vol_ann) <= 0:
                continue
            em1sigma_pct = float(vol_ann) * 100.0 * math.sqrt(max(1, int(win.dte_sessions)) / 252.0)
            em_source = "RV20"
        else:
            em1sigma_pct = iv_to_em1sigma_pct(iv_pct=float(iv_h), dte_calendar_days=max(1, int(win.dte_calendar_days)))
            em_source = "IV"
            # Cache implied samples for regime scoring (term slope / vv).
            iv_weekly_sample[ek] = {
                "iv7": float(iv7) if iv7 is not None else float(iv_h),
                "iv30": float(iv30) if iv30 is not None else float(iv_h),
            }

        # Macro context for the week (Mon..Fri) anchored to entry
        macro = None
        if benzinga_client is not None:
            # Week window is entry-week Monday -> Friday
            mon = entry - dt.timedelta(days=entry.weekday())
            fri = mon + dt.timedelta(days=4)
            # Use pre-fetched economics rows to avoid repeated network calls.
            econ_rows_week: List[dict] = []
            d0 = mon
            while d0 <= fri:
                econ_rows_week.extend(econ_by_date.get(_fmt_date(d0), []))
                d0 += dt.timedelta(days=1)
            macro = _macro_context(benzinga_client, start=mon, end=fri, as_of=entry, flags=flags, economics_rows=econ_rows_week)
        if macro is None:
            macro = {"multiplier": 1.0, "flags": {"OPEX": bool(_is_opex_week(expiry))}, "highImpactUS": {"count": 0, "top": []}, "notes": ["Benzinga unavailable or disabled."]}
        macro_by_entry[_fmt_date(entry)] = macro

        # Regime at entry (0..100)
        r = compute_regime_score_for_date(
            client,
            ticker=underlying,
            as_of=entry,
            bars=bars,
            flags=flags,
            iv_weekly_sample=(iv_weekly_sample if iv_weekly_sample else None),
            sector_dispersion_cache=sector_disp,
            macro_multiplier=float(macro.get("multiplier") or 1.0),
            macro_flags=(macro.get("flags") if isinstance(macro.get("flags"), dict) else None),
        )
        bucket = str(r.get("bucket") or "MODERATE")
        mb = _macro_bucket(macro)

        # MAE/MFE (absolute, points)
        mae_abs_pct = 0.0
        up_mae_pct = 0.0
        down_mae_pct = 0.0
        # Use the already-fetched bars (no per-day ORATS calls).
        i0 = idx_by_date.get(ek)
        i1 = idx_by_date.get(fk)
        if i0 is not None and i1 is not None and i1 >= i0:
            for b in bars[i0 : i1 + 1]:
                if b.high is not None and b.low is not None:
                    up_mae_pct = max(up_mae_pct, (float(b.high) / entry_px - 1.0) * 100.0)
                    down_mae_pct = max(down_mae_pct, (1.0 - float(b.low) / entry_px) * 100.0)
        mae_abs_pct = max(up_mae_pct, down_mae_pct)
        mae_abs_pts = mae_abs_pct / 100.0 * entry_px
        mae_abs_em = mae_abs_pct / float(em1sigma_pct) if em1sigma_pct > 1e-9 else None

        # Seasonality labels
        season = {
            "quarter": _quarter_key(entry),
            "month": int(entry.month),
            "isSummer": bool(_is_summer(entry)),
            "isOpexWeek": bool(_is_opex_week(expiry)),
        }
        season_bucket = _season_bucket(entry)

        week_rows.append(
            {
                "entryDate": _fmt_date(entry),
                "expiryDate": _fmt_date(expiry),
                "dte": int(win.dte_sessions),
                "entryPx": round(entry_px, 2),
                "expiryPx": round(exp_px, 2),
                "retPct": round(float(ret_pct), 3),
                "em1sigmaPct": round(float(em1sigma_pct), 3),
                "emSource": em_source,
                "macroMultiplier": round(float(macro.get("multiplier") or 1.0), 3),
                "regimeScore100": float(r.get("score100") or 50.0),
                "regimeBucket": bucket,
                "macroBucket": mb,
                "seasonBucket": season_bucket,
                "maeAbsPts": round(float(mae_abs_pts), 2),
                "maeAbsEm": None if mae_abs_em is None else round(float(mae_abs_em), 3),
                "seasonality": season,
            }
        )

        # Aggregate grid over EM multiples and wing widths
        diff_pts = abs(exp_px - entry_px)
        for em in em_mults:
            if em <= 0:
                continue
            short_dist_pts = (float(em) * float(em1sigma_pct) / 100.0) * entry_px
            breach = diff_pts > short_dist_pts
            for wp in wing_pts:
                if int(wp) <= 0:
                    continue
                long_dist_pts = short_dist_pts + float(wp)
                outside = diff_pts > long_dist_pts
                k = (ed, bucket, mb, season_bucket, float(em), int(wp))
                cell = agg.get(k)
                if cell is None:
                    cell = {"n": 0, "breach": 0, "outside": 0, "maePts": [], "lossPts": []}
                    agg[k] = cell
                cell["n"] += 1
                cell["breach"] += 1 if breach else 0
                cell["outside"] += 1 if outside else 0
                cell["maePts"].append(float(mae_abs_pts))
                # Worst-case expiry loss proxy (no credit): intrinsic loss beyond short strikes, capped by wing width.
                loss_pts = max(0.0, float(diff_pts) - float(short_dist_pts))
                loss_pts = min(float(wp), loss_pts)
                cell["lossPts"].append(float(loss_pts))

    # Current macro context (for recommendation)
    # Rolling window (requested): today .. today+7 (ET), not limited to Mon..Fri.
    macro_now = None
    if benzinga_client is not None:
        d0 = now
        exp0 = now + dt.timedelta(days=7)
        econ_rows_now: List[dict] = []
        d1 = d0
        while d1 <= exp0:
            econ_rows_now.extend(econ_by_date.get(_fmt_date(d1), []))
            d1 += dt.timedelta(days=1)
        macro_now = _macro_context(benzinga_client, start=d0, end=exp0, as_of=now, flags=flags, economics_rows=econ_rows_now)
    if macro_now is None:
        macro_now = {"multiplier": 1.0, "flags": {"OPEX": bool(_is_opex_week(now + dt.timedelta(days=7)))}, "highImpactUS": {"count": 0, "top": []}, "notes": ["Benzinga unavailable or disabled."]}
    macro_bucket_now = _macro_bucket(macro_now)
    regime_now = compute_regime_score_for_date(
        client,
        ticker=underlying,
        as_of=now,
        bars=bars,
        flags=flags,
        iv_weekly_sample=(iv_weekly_sample if iv_weekly_sample else None),
        sector_dispersion_cache=sector_disp,
        macro_multiplier=float(macro_now.get("multiplier") or 1.0),
        macro_flags=(macro_now.get("flags") if isinstance(macro_now.get("flags"), dict) else None),
    )
    regime_bucket_now = str(regime_now.get("bucket") or "MODERATE")
    season_bucket_now = _season_bucket(now)

    # --- Live options context (current-only, informational) ---
    live_context: Dict[str, Any] = {
        "enabled": False,
        # Backwards-compatible "primary" view fields (we set these to weeklyFriday if available).
        "symbolUsed": None,
        "expiry": None,
        "spot": None,
        "bandPct": 0.05,
        "atmIvPct": None,
        "greeksAgg": None,
        "dealerGamma": None,
        "oiClusters": None,
        # New: dual live views
        "weeklyFriday": None,
        "nearestDaily": None,
        "warnings": [],
        "notes": ["Live context unavailable."],
    }
    try:
        # Only attempt if live methods exist (keeps unit tests/mock clients safe).
        if callable(getattr(client, "live_strikes_by_expiry", None)) and callable(getattr(client, "live_strikes", None)):
            # Build expiries list once per symbol.
            # Do NOT hard depend on /live/expirations since some entitlements return empty expirations;
            # infer expiries from full strikes as fallback.
            exp_warn: List[str] = []
            strikes_cache_by_symbol: Dict[str, List[dict]] = {}
            exp_dates_by_symbol: Dict[str, List[str]] = {}

            # Respect user's Engine2 underlying selection (no cross-ticker proxy):
            # - SPX: allow SPXW -> SPX (same family), but never SPY
            # - SPY: SPY only
            # - QQQ: QQQ only
            symbols = ("SPXW", "SPX") if pref == "SPX" else (pref,)
            fields0 = "ticker,tradeDate,expirDate,expiry,expDate,exp_date,strike,spotPrice,stockPrice,gamma,theta,vega,callOpenInterest,putOpenInterest,callVolume,putVolume,callMidIv,putMidIv"

            for sym in symbols:
                exp_dates: List[str] = []
                exp_rows: List[dict] = []
                try:
                    if callable(getattr(client, "live_expirations", None)):
                        exp_rows = client.live_expirations(ticker=sym).rows or []
                except Exception as e:
                    exp_warn.append(f"Live expirations error for {sym}: {type(e).__name__}: {e}")
                    exp_rows = []

                if exp_rows:
                    for r in exp_rows:
                        if not isinstance(r, dict):
                            continue
                        d0 = str(r.get("expirDate") or r.get("expiry") or r.get("expDate") or r.get("exp_date") or "")[:10]
                        if d0 and len(d0) >= 10:
                            exp_dates.append(d0)
                else:
                    # Fallback: infer expiries from full strikes payload (cached short-TTL).
                    try:
                        all_rows = client.live_strikes(ticker=sym, fields=fields0).rows or []
                        all_rows = [r for r in all_rows if isinstance(r, dict)]
                        strikes_cache_by_symbol[sym] = all_rows
                        exp_dates = _infer_live_expiries_from_strikes(all_rows)
                    except Exception as e:
                        exp_warn.append(f"Live strikes fallback error for {sym}: {type(e).__name__}: {e}")
                        exp_dates = []

                exp_dates_by_symbol[sym] = exp_dates

            def _pick_symbol_and_expiry(*, mode: str) -> Tuple[Optional[str], Optional[str]]:
                for sym in symbols:
                    ds = exp_dates_by_symbol.get(sym) or []
                    if mode == "weekly":
                        ex = _pick_weekly_close_expiry_date(ds, today=now, now_dt=now_dt_utc)
                    else:
                        ex = _pick_nearest_expiry_date(ds, today=now)
                    if ex:
                        return sym, ex
                return None, None

            weekly_sym, weekly_expiry = _pick_symbol_and_expiry(mode="weekly")
            daily_sym, daily_expiry = _pick_symbol_and_expiry(mode="daily")

            def _build_view(*, symbol: Optional[str], expiry: Optional[str], label: str) -> Dict[str, Any]:
                base = {
                    "enabled": False,
                    "label": label,
                    "symbolUsed": symbol,
                    "expiry": str(expiry)[:10] if expiry else None,
                    "spot": None,
                    "bandPct": 0.05,
                    "atmIvPct": None,
                    "greeksAgg": None,
                    "dealerGamma": None,
                    "oiClusters": None,
                    "warnings": [],
                    "notes": [],
                }
                if not symbol or not expiry:
                    base["notes"] = ["No suitable expiry found for this view."]
                    return base

                fields = ",".join(
                    [
                        "ticker",
                        "tradeDate",
                        "expirDate",
                        "strike",
                        "spotPrice",
                        "stockPrice",
                        "gamma",
                        "theta",
                        "vega",
                        "callOpenInterest",
                        "putOpenInterest",
                        "callVolume",
                        "putVolume",
                        "callMidIv",
                        "putMidIv",
                    ]
                )
                used_chain_sym, chain_rows, chain_warn = _live_chain_with_fallback(
                    client,
                    tickers=[symbol],
                    expiry=expiry,
                    fields=fields,
                )
                # If strikes-by-expiry is empty, fall back to filtering full strikes payload (if we have it).
                if (not chain_rows) and symbol in strikes_cache_by_symbol:
                    chain_rows = _filter_chain_by_expiry(strikes_cache_by_symbol.get(symbol) or [], expiry=expiry)
                    if chain_rows:
                        chain_warn.append("Live strikes-by-expiry empty; used full strikes filtered by expiry.")

                if not chain_rows:
                    base["warnings"] = chain_warn
                    base["notes"] = ["Live strikes returned no usable chain rows for the selected expiry (check entitlement, symbol, or expiry selection)."]
                    return base

                dg = compute_dealer_gamma_context(chain_rows, expiry=expiry, contract_multiplier=100, band_pct=0.05, top_n=5)
                oi = compute_open_interest_clusters(chain_rows, expiry=expiry, band_pct=0.05, top_n=5, cluster_steps=2)

                # Simple greek aggregates near spot band (same band as dealer gamma)
                spot = dg.get("spot")
                lo = float(spot) * (1.0 - 0.05) if spot else None
                hi = float(spot) * (1.0 + 0.05) if spot else None
                w_mode = str(dg.get("weightingMode") or "oi")
                g_sum = 0.0
                t_sum = 0.0
                v_sum = 0.0
                iv_atm = None
                if spot and lo and hi:
                    best_dist = None
                    for r in chain_rows:
                        strike = _to_float(r.get("strike"))
                        if strike is None or not (lo <= float(strike) <= hi):
                            continue
                        gamma = _to_float(r.get("gamma")) or 0.0
                        theta = _to_float(r.get("theta")) or 0.0
                        vega = _to_float(r.get("vega")) or 0.0
                        if w_mode == "oi":
                            w = (_to_float(r.get("callOpenInterest")) or 0.0) + (_to_float(r.get("putOpenInterest")) or 0.0)
                        elif w_mode == "volume":
                            w = (_to_float(r.get("callVolume")) or 0.0) + (_to_float(r.get("putVolume")) or 0.0)
                        else:
                            w = 1.0
                        w = max(0.0, float(w))
                        g_sum += float(gamma) * w * 100.0
                        t_sum += float(theta) * w * 100.0
                        v_sum += float(vega) * w * 100.0

                        dist = abs(float(strike) - float(spot))
                        if best_dist is None or dist < best_dist:
                            best_dist = dist
                            # Prefer call mid iv, fallback to put mid iv
                            iv = _iv_to_pct(r.get("callMidIv")) or _iv_to_pct(r.get("putMidIv"))
                            iv_atm = iv

                base.update(
                    {
                        "enabled": True,
                        "symbolUsed": used_chain_sym or symbol,
                        "expiry": str(expiry)[:10],
                        "spot": dg.get("spot"),
                        "atmIvPct": None if iv_atm is None else round(float(iv_atm), 2),
                        "greeksAgg": {
                            "gamma": round(float(g_sum), 3),
                            "theta": round(float(t_sum), 3),
                            "vega": round(float(v_sum), 3),
                            "weightingMode": w_mode,
                        },
                        "dealerGamma": dg,
                        "oiClusters": oi,
                        "warnings": chain_warn,
                        "notes": [
                            "Live, informational only. Dealer gamma context does not change breach odds or any historical stats.",
                            "spotPrice is preferred; stockPrice may be parity-derived intraday.",
                        ],
                    }
                )
                return base

            weekly_view = _build_view(symbol=weekly_sym, expiry=weekly_expiry, label="weeklyFriday")
            daily_view = _build_view(symbol=daily_sym, expiry=daily_expiry, label="nearestDaily")

            # Back-compat: expose a primary view at top-level (weekly preferred).
            primary_view = weekly_view if weekly_view.get("enabled") else daily_view
            any_enabled = bool(weekly_view.get("enabled") or daily_view.get("enabled"))
            live_context = {
                "enabled": any_enabled,
                "symbolUsed": primary_view.get("symbolUsed"),
                "expiry": primary_view.get("expiry"),
                "spot": primary_view.get("spot"),
                "bandPct": 0.05,
                "atmIvPct": primary_view.get("atmIvPct"),
                "greeksAgg": primary_view.get("greeksAgg"),
                "dealerGamma": primary_view.get("dealerGamma"),
                "oiClusters": primary_view.get("oiClusters"),
                "weeklyFriday": weekly_view,
                "nearestDaily": daily_view,
                "warnings": [*exp_warn, *(primary_view.get("warnings") or [])],
                "notes": [
                    "Live, informational only. Backtest/odds use ORATS EOD and are not affected by these live panels.",
                    "Weekly view targets the Friday weekly expiry (rolls after 4:15pm ET on Fridays).",
                    "Nearest view targets 0DTE/nearest expiry (intraday microstructure).",
                ],
            }
            if not any_enabled:
                live_context["enabled"] = False
                live_context["notes"] = [
                    "Live context unavailable (no usable chain rows for weekly or nearest expiry).",
                ]
                live_context["warnings"] = exp_warn
        else:
            live_context["notes"] = ["Live endpoints not configured on this ORATS client (missing live_* methods)."]
    except Exception:
        # Never fail Engine 2 on live context
        live_context = {
            "enabled": False,
            "symbolUsed": None,
            "expiry": None,
            "spot": None,
            "bandPct": 0.05,
            "atmIvPct": None,
            "greeksAgg": None,
            "dealerGamma": None,
            "oiClusters": None,
            "weeklyFriday": None,
            "nearestDaily": None,
            "warnings": [],
            "notes": ["Live context unavailable (unexpected error)."],
        }

    # "Like now" conditional odds: filter historical weeks to the current buckets (regime/macro/season).
    # This is the core desk question: "in conditions like now, how often do 1.0/1.5/2.0× EM breach?"
    like_rows = [r for r in week_rows if str(r.get("regimeBucket")) == regime_bucket_now and str(r.get("macroBucket")) == macro_bucket_now and str(r.get("seasonBucket")) == season_bucket_now]
    per_w: Dict[float, Dict[str, Any]] = {float(w): {"w": float(w), "n": 0, "breachEither": 0, "breachPut": 0, "breachCall": 0, "avgAbsRetPct": 0.0} for w in widths_use}
    for r in like_rows:
        try:
            ret = float(r.get("retPct"))
            em1 = float(r.get("em1sigmaPct"))
        except Exception:
            continue
        abs_ret = abs(ret)
        for w in widths_use:
            dist = float(w) * float(em1)
            breach_put = ret < -dist
            breach_call = ret > dist
            breach = bool(breach_put or breach_call)
            acc = per_w[float(w)]
            acc["n"] += 1
            acc["breachEither"] += 1 if breach else 0
            acc["breachPut"] += 1 if breach_put else 0
            acc["breachCall"] += 1 if breach_call else 0
            acc["avgAbsRetPct"] += float(abs_ret)

    odds_like_now: List[Dict[str, Any]] = []
    for w, acc in per_w.items():
        n = int(acc["n"])
        if n > 0:
            avg_abs = float(acc["avgAbsRetPct"]) / n
            out = dict(acc)
            out["avgAbsRetPct"] = round(avg_abs, 3)
            out["breachEitherPct"] = round(acc["breachEither"] / n * 100.0, 2)
            out["breachPutPct"] = round(acc["breachPut"] / n * 100.0, 2)
            out["breachCallPct"] = round(acc["breachCall"] / n * 100.0, 2)
            odds_like_now.append(out)
        else:
            odds_like_now.append({**acc, "breachEitherPct": None, "breachPutPct": None, "breachCallPct": None})
    odds_like_now.sort(key=lambda x: x["w"])

    # Build aggregated cells output
    cells_out: List[Dict[str, Any]] = []
    for (entry_day_k, reg_k, macro_k, season_k, em_k, wp_k), v in agg.items():
        n = int(v["n"])
        k_b = int(v["breach"])
        k_o = int(v["outside"])
        mae_list = list(v["maePts"] or [])
        loss_list = list(v["lossPts"] or [])
        pb = beta_binomial_mean(k=k_b, n=n, alpha=1.0, beta=1.0)
        po = beta_binomial_mean(k=k_o, n=n, alpha=1.0, beta=1.0)
        mae95 = pctile(mae_list, 95.0)
        loss95 = pctile(loss_list, 95.0)
        cells_out.append(
            {
                "entryDay": entry_day_k,
                "regimeBucket": reg_k,
                "macroBucket": macro_k,
                "seasonBucket": season_k,
                "emMult": float(em_k),
                "wingWidthPts": int(wp_k),
                "n": n,
                "pBreachPct": None if pb is None else round(100.0 * float(pb), 3),
                "pOutsideWingsPct": None if po is None else round(100.0 * float(po), 3),
                "mae95Pts": None if mae95 is None else round(float(mae95), 3),
                "mae95xWing": None if (mae95 is None or wp_k <= 0) else round(float(mae95) / float(wp_k), 3),
                "loss95Pts": None if loss95 is None else round(float(loss95), 3),
                "loss95xWing": None if (loss95 is None or wp_k <= 0) else round(float(loss95) / float(wp_k), 3),
            }
        )

    # Recommendation search for current buckets, prefer emMult=1.0
    policy = {
        # Let caller-supplied risk_target_breach_pct override the default breach cap.
        "maxBreachPct": float(risk_target_breach_pct) if risk_target_breach_pct is not None else float(flags.ENGINE2_POLICY_MAX_BREACH_PCT),
        "maxOutsideWingsPct": float(flags.ENGINE2_POLICY_MAX_OUTSIDE_WINGS_PCT),
        "maxMae95xWing": float(flags.ENGINE2_POLICY_MAX_MAE95_X_WING),
    }
    # Candidate selection: exact bucket first, then graceful fallbacks (so UI isn't empty).
    def _select_candidates(*, macro_bucket: Optional[str], season_bucket: Optional[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for c in cells_out:
            if c.get("entryDay") != ed:
                continue
            if c.get("regimeBucket") != regime_bucket_now:
                continue
            if macro_bucket is not None and c.get("macroBucket") != macro_bucket:
                continue
            if season_bucket is not None and c.get("seasonBucket") != season_bucket:
                continue
            out.append(c)
        return out

    match_used = {
        "entryDay": ed,
        "regimeBucket": regime_bucket_now,
        "macroBucket": macro_bucket_now,
        "seasonBucket": season_bucket_now,
        "fallbackUsed": False,
        "fallbackReason": None,
    }
    candidates = _select_candidates(macro_bucket=macro_bucket_now, season_bucket=season_bucket_now)
    if not candidates:
        # 1) If seasonality is enabled, relax season bucket (keep macro).
        if season_mode != "none":
            c2 = _select_candidates(macro_bucket=macro_bucket_now, season_bucket=None)
            if c2:
                candidates = c2
                match_used.update({"fallbackUsed": True, "fallbackReason": "season_bucket_relaxed"})
        # 2) If macro bucket is MACRO, relax to NORMAL (keep season if possible).
        if (not candidates) and macro_bucket_now == "MACRO":
            c3 = _select_candidates(macro_bucket="NORMAL", season_bucket=(season_bucket_now if season_mode != "none" else None))
            if c3:
                candidates = c3
                match_used.update({"fallbackUsed": True, "fallbackReason": "macro_bucket_relaxed_to_normal", "macroBucket": "NORMAL"})
        # 3) If still empty, relax both macro + season.
        if not candidates:
            c4 = _select_candidates(macro_bucket=None, season_bucket=None)
            if c4:
                candidates = c4
                match_used.update({"fallbackUsed": True, "fallbackReason": "macro_and_season_relaxed"})
    # Prefer EM=1.0 then minimal wing
    def _meets(c: Dict[str, Any]) -> bool:
        if c.get("pBreachPct") is None or c.get("pOutsideWingsPct") is None or c.get("mae95xWing") is None:
            return False
        return (
            float(c["pBreachPct"]) <= policy["maxBreachPct"]
            and float(c["pOutsideWingsPct"]) <= policy["maxOutsideWingsPct"]
            and float(c["mae95xWing"]) <= policy["maxMae95xWing"]
        )

    pick = None
    # pass 1: EM 1.0
    em_pref = 1.0
    same_em = [c for c in candidates if abs(float(c["emMult"]) - em_pref) < 1e-9]
    for c in sorted(same_em, key=lambda x: int(x["wingWidthPts"])):
        if _meets(c):
            pick = c
            break
    # pass 2: any config, choose min wing then min EM
    if pick is None:
        ok = [c for c in candidates if _meets(c)]
        ok.sort(key=lambda x: (int(x["wingWidthPts"]), float(x["emMult"])))
        pick = ok[0] if ok else None

    # If still none, provide best-effort (lowest breach/outside/mae) so UI has a suggestion.
    best_effort = None
    if pick is None and candidates:
        scored = []
        for c in candidates:
            pb = float(c.get("pBreachPct") or 9999.0)
            po = float(c.get("pOutsideWingsPct") or 9999.0)
            m = float(c.get("mae95xWing") or 9999.0)
            scored.append((pb, po, m, int(c.get("wingWidthPts") or 9999), float(c.get("emMult") or 9999.0), c))
        scored.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]))
        best_effort = scored[0][-1] if scored else None
    rec = {
        "entryDay": ed,
        "regimeBucket": regime_bucket_now,
        "macroBucket": macro_bucket_now,
        "seasonBucket": season_bucket_now,
        "seasonalityMode": season_mode,
        "matchUsed": match_used,
        "policy": policy,
        "recommended": None,
        "bestEffort": None,
        "notes": [],
    }
    if pick is not None:
        rec["recommended"] = {"emMult": pick["emMult"], "wingWidthPts": pick["wingWidthPts"], "n": pick["n"], "pBreachPct": pick["pBreachPct"], "pOutsideWingsPct": pick["pOutsideWingsPct"], "mae95Pts": pick["mae95Pts"], "mae95xWing": pick["mae95xWing"]}
        rec["notes"].append("Meets policy constraints in the matched bucket.")
    else:
        rec["notes"].append("No configuration met constraints for the matched bucket.")
        if best_effort is not None:
            rec["bestEffort"] = {
                "emMult": best_effort["emMult"],
                "wingWidthPts": best_effort["wingWidthPts"],
                "n": best_effort["n"],
                "pBreachPct": best_effort["pBreachPct"],
                "pOutsideWingsPct": best_effort["pOutsideWingsPct"],
                "mae95Pts": best_effort["mae95Pts"],
                "mae95xWing": best_effort["mae95xWing"],
            }
            rec["notes"].append("Showing best-effort (lowest breach/outside/MAE) for transparency.")
        rec["notes"].append("Consider widening wings, reducing size, or relaxing constraints (risk-only engine does not price credit).")

    # Empirical macro vs non-macro effects (risk-only), using a fixed baseline geometry for comparison:
    # EM=1.0 and wing=15pts (if available), otherwise closest.
    baseline_em = 1.0
    baseline_wing = 15
    if wing_pts:
        baseline_wing = min(wing_pts, key=lambda x: abs(int(x) - 15))
    # Choose the closest EM in the configured grid
    if em_mults:
        baseline_em = min(em_mults, key=lambda x: abs(float(x) - 1.0))
    baseline_cells = [c for c in cells_out if c["entryDay"] == ed and abs(float(c["emMult"]) - float(baseline_em)) < 1e-9 and int(c["wingWidthPts"]) == int(baseline_wing)]

    def _split_macro(cells: List[Dict[str, Any]]) -> Dict[str, Any]:
        mac = [x for x in cells if x.get("macroBucket") == "MACRO"]
        nor = [x for x in cells if x.get("macroBucket") == "NORMAL"]
        def _avg(key: str, xs: List[Dict[str, Any]]) -> Optional[float]:
            vals = [float(r[key]) for r in xs if r.get(key) is not None]
            if not vals:
                return None
            return sum(vals) / len(vals)
        return {
            "macro": {"nCells": len(mac), "avgPBreachPct": _avg("pBreachPct", mac), "avgMae95xWing": _avg("mae95xWing", mac)},
            "normal": {"nCells": len(nor), "avgPBreachPct": _avg("pBreachPct", nor), "avgMae95xWing": _avg("mae95xWing", nor)},
        }

    macro_effects = {
        "baseline": {"emMult": float(baseline_em), "wingWidthPts": int(baseline_wing)},
        "overall": _split_macro(baseline_cells),
        "byRegimeBucket": {},
        "notes": ["Macro effect uses smoothed grid probabilities for baseline geometry (risk-only)."],
    }
    for rb in ("LOW", "MODERATE", "ELEVATED", "NO_TRADE"):
        macro_effects["byRegimeBucket"][rb] = _split_macro([c for c in baseline_cells if c.get("regimeBucket") == rb])

    # Backtest summary (fast): derive the "byWidth" table from the already-computed week_rows.
    # This avoids calling backtest_weekly_ic_risk(), which performs many per-day ORATS requests.
    per_width: Dict[float, Dict[str, Any]] = {float(w): {"w": float(w), "n": 0, "breachEither": 0, "breachPut": 0, "breachCall": 0, "avgAbsRetPct": 0.0} for w in widths_use}
    per_quarter: Dict[str, Dict[float, Dict[str, Any]]] = {q: {float(w): {"n": 0, "breachEither": 0} for w in widths_use} for q in ("Q1", "Q2", "Q3", "Q4")}
    for r in week_rows:
        try:
            ret = float(r.get("retPct"))
            em1 = float(r.get("em1sigmaPct"))
            entry_dt = _parse_date(str(r.get("entryDate") or ""))
        except Exception:
            continue
        abs_ret = abs(ret)
        qk = _quarter_key(entry_dt)
        for w in widths_use:
            dist = float(w) * float(em1)
            breach_put = ret < -dist
            breach_call = ret > dist
            breach = bool(breach_put or breach_call)
            acc = per_width[float(w)]
            acc["n"] += 1
            acc["breachEither"] += 1 if breach else 0
            acc["breachPut"] += 1 if breach_put else 0
            acc["breachCall"] += 1 if breach_call else 0
            acc["avgAbsRetPct"] += float(abs_ret)
            qacc = per_quarter[qk][float(w)]
            qacc["n"] += 1
            qacc["breachEither"] += 1 if breach else 0

    by_width: List[Dict[str, Any]] = []
    for w, acc in per_width.items():
        n = int(acc["n"])
        if n > 0:
            avg_abs = float(acc["avgAbsRetPct"]) / n
            out = dict(acc)
            out["avgAbsRetPct"] = round(avg_abs, 3)
            out["breachEitherPct"] = round(acc["breachEither"] / n * 100.0, 2)
            out["breachPutPct"] = round(acc["breachPut"] / n * 100.0, 2)
            out["breachCallPct"] = round(acc["breachCall"] / n * 100.0, 2)
            by_width.append(out)
        else:
            by_width.append({**acc, "breachEitherPct": None, "breachPutPct": None, "breachCallPct": None})
    by_width.sort(key=lambda x: x["w"])

    by_q: Dict[str, Any] = {}
    for qk, wmap in per_quarter.items():
        by_q[qk] = {}
        for w, acc in wmap.items():
            n = int(acc["n"])
            by_q[qk][str(w)] = {"n": n, "breachEitherPct": (round(acc["breachEither"] / n * 100.0, 2) if n else None)}

    bt = {"rowsUsed": int(len(week_rows)), "rows": [], "byWidth": by_width, "byQuarter": by_q, "notes": ["Derived from Engine 2 weekly rows (fast path)."]}
    rec_simple = recommend_width(by_width=by_width, risk_target_breach_pct=float(risk_target_breach_pct))

    # --- Technicals (daily indicators + live overlay; additive, does not affect backtest) ---
    tech_bars: List[TechDailyBar] = []
    for b in bars:
        # only keep fully ordered series, tolerate missing volume/vwap
        if not b or not b.trade_date:
            continue
        tech_bars.append(
            TechDailyBar(
                trade_date=str(b.trade_date)[:10],
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                vwap=b.vwap,
            )
        )
    closes_tech = [float(b.close) for b in tech_bars if b.close is not None and float(b.close) > 0]
    ema = compute_ema_levels(closes_tech, spans=[8, 21, 50, 100, 200]) if closes_tech else {}
    ok_ohlc = bool(tech_bars) and (tech_bars[-1].high is not None) and (tech_bars[-1].low is not None) and (tech_bars[-1].close is not None)

    # Close-based indicators (daily)
    rsi: Dict[str, Any] = {"enabled": False, "period": 14, "value": None, "slope1d": None, "state": None, "notes": []}
    macd: Dict[str, Any] = {
        "enabled": False,
        "fast": 12,
        "slow": 26,
        "signal": 9,
        "macd": None,
        "signalLine": None,
        "hist": None,
        "cross": None,
        "histTrend": None,
        "notes": [],
    }
    boll: Dict[str, Any] = {
        "enabled": False,
        "period": 20,
        "stdev": 2.0,
        "mid": None,
        "upper": None,
        "lower": None,
        "bandwidthPct": None,
        "percentB": None,
        "state": None,
        "squeeze": None,
        "notes": [],
    }
    ema_slopes: Dict[str, Optional[float]] = {}
    try:
        for span in (21, 50, 200):
            if len(closes_tech) >= int(span) + 6:
                ser = _ema_series(closes_tech, int(span))
                if len(ser) >= 6:
                    ema_slopes[f"ema{int(span)}_slope5"] = float(ser[-1]) - float(ser[-6])
            else:
                ema_slopes[f"ema{int(span)}_slope5"] = None
    except Exception:
        ema_slopes = {}

    if len(closes_tech) >= 16:
        rsi_series = compute_rsi_series(closes_tech, period=14)
        rv = rsi_series[-1]
        rp = rsi_series[-2] if len(rsi_series) >= 2 else None
        if rv is not None and math.isfinite(float(rv)):
            slope = None
            if rp is not None and math.isfinite(float(rp)):
                slope = float(rv) - float(rp)
            state = "overbought" if float(rv) >= 70.0 else "oversold" if float(rv) <= 30.0 else "neutral"
            rsi = {
                "enabled": True,
                "period": 14,
                "value": float(rv),
                "slope1d": None if slope is None else float(slope),
                "state": state,
                "notes": ["RSI computed on daily closes (Wilder smoothing)."],
            }

    if len(closes_tech) >= 40:
        m = compute_macd_series(closes_tech, fast=12, slow=26, signal=9)
        macd_series = m.get("macd") or []
        sig_series = m.get("signal") or []
        hist_series = m.get("hist") or []
        mv = macd_series[-1] if macd_series else None
        sv = sig_series[-1] if sig_series else None
        hv = hist_series[-1] if hist_series else None
        cross = None
        hist_trend = None
        if len(macd_series) >= 2 and len(sig_series) >= 2:
            mp = macd_series[-2]
            sp = sig_series[-2]
            if all(x is not None for x in (mp, sp, mv, sv)):
                prev = float(mp) - float(sp)
                cur = float(mv) - float(sv)
                if prev <= 0 and cur > 0:
                    cross = "bullish"
                elif prev >= 0 and cur < 0:
                    cross = "bearish"
        if len(hist_series) >= 2 and hist_series[-2] is not None and hv is not None:
            try:
                hist_trend = "increasing" if float(hv) > float(hist_series[-2]) else "decreasing" if float(hv) < float(hist_series[-2]) else "flat"
            except Exception:
                hist_trend = None
        if mv is not None and sv is not None:
            macd = {
                "enabled": True,
                "fast": 12,
                "slow": 26,
                "signal": 9,
                "macd": float(mv) if mv is not None else None,
                "signalLine": float(sv) if sv is not None else None,
                "hist": float(hv) if hv is not None else None,
                "cross": cross,
                "histTrend": hist_trend,
                "notes": ["MACD computed on daily closes (12/26 EMA, 9 EMA signal)."],
            }

    if len(closes_tech) >= 40:
        bb = compute_bollinger_series(closes_tech, period=20, stdev=2.0)
        mid_s = bb.get("mid") or []
        up_s = bb.get("upper") or []
        lo_s = bb.get("lower") or []
        bw_s = bb.get("bandwidthPct") or []
        pb_s = bb.get("percentB") or []
        mid_v = mid_s[-1] if mid_s else None
        up_v = up_s[-1] if up_s else None
        lo_v = lo_s[-1] if lo_s else None
        bw_v = bw_s[-1] if bw_s else None
        pb_v = pb_s[-1] if pb_s else None
        state = None
        if up_v is not None and lo_v is not None:
            c0 = float(closes_tech[-1])
            if c0 > float(up_v):
                state = "above_upper"
            elif c0 < float(lo_v):
                state = "below_lower"
            else:
                state = "inside"
        squeeze = None
        bw_vals = [float(x) for x in bw_s[-120:] if x is not None and math.isfinite(float(x))]
        if bw_v is not None and bw_vals:
            # simple percentile: bottom 20% => squeeze
            pr = None
            try:
                c = sum(1 for v in bw_vals if v <= float(bw_v))
                pr = c / float(len(bw_vals))
            except Exception:
                pr = None
            if pr is not None:
                squeeze = bool(float(pr) <= 0.20)
        if mid_v is not None and up_v is not None and lo_v is not None:
            boll = {
                "enabled": True,
                "period": 20,
                "stdev": 2.0,
                "mid": float(mid_v),
                "upper": float(up_v),
                "lower": float(lo_v),
                "bandwidthPct": None if bw_v is None else float(bw_v),
                "percentB": None if pb_v is None else float(pb_v),
                "state": state,
                "squeeze": squeeze,
                "notes": ["Bollinger Bands computed on daily closes (20 SMA, 2σ)."],
            }

    # OHLC-based
    ich = compute_ichimoku_levels(tech_bars) if ok_ohlc else {"enabled": False, "notes": ["Insufficient OHLC for Ichimoku."]}
    vwap_proxy = compute_vwap_proxy(tech_bars, window=20) if tech_bars else {"enabled": False}
    candles = detect_candlestick_patterns(tech_bars) if ok_ohlc else {"enabled": False, "patterns": [], "notes": ["Insufficient OHLC for candle patterns."]}
    red_dog = detect_red_dog_reversal(tech_bars) if ok_ohlc else {"enabled": False, "bullish": False, "bearish": False, "notes": ["Insufficient OHLC for Red Dog."]}
    elliott = detect_elliott_pivot_structure(tech_bars, threshold_pct=0.04) if closes_tech else {"enabled": False, "structure": "unclear", "notes": ["Insufficient closes for pivots."]}
    live_px = None
    # Prefer liveContext spot if available, else try live summaries for the underlying/proxy
    try:
        live_px = _to_float((live_context.get("spot") if isinstance(live_context, dict) else None))
    except Exception:
        live_px = None
    if live_px is None:
        live_px = fetch_live_price_optional(client, ticker=str(underlying).upper())
    level_map: Dict[str, Optional[float]] = {}
    level_map.update(ema)
    if isinstance(boll, dict) and boll.get("enabled"):
        try:
            if boll.get("mid") is not None:
                level_map["bbMid"] = float(boll["mid"])
            if boll.get("upper") is not None:
                level_map["bbUpper"] = float(boll["upper"])
            if boll.get("lower") is not None:
                level_map["bbLower"] = float(boll["lower"])
        except Exception:
            pass
    if isinstance(vwap_proxy, dict) and vwap_proxy.get("enabled") and vwap_proxy.get("value") is not None:
        try:
            level_map["vwapProxy"] = float(vwap_proxy["value"])
        except Exception:
            pass
    if isinstance(ich, dict) and ich.get("enabled"):
        if isinstance(ich.get("tenkan"), (int, float)):
            level_map["tenkan"] = float(ich["tenkan"])
        if isinstance(ich.get("kijun"), (int, float)):
            level_map["kijun"] = float(ich["kijun"])
        cn = ich.get("cloudNow") if isinstance(ich.get("cloudNow"), dict) else None
        if cn and isinstance(cn.get("cloudTop"), (int, float)) and isinstance(cn.get("cloudBottom"), (int, float)):
            level_map["cloudTopNow"] = float(cn["cloudTop"])
            level_map["cloudBottomNow"] = float(cn["cloudBottom"])
    distances = compute_distances(live_price=live_px, levels=level_map)
    last_bar = tech_bars[-1] if tech_bars else None
    last_close = None if (last_bar is None or last_bar.close is None) else float(last_bar.close)
    px_for_narr = float(live_px) if (live_px is not None and float(live_px) > 0) else (float(last_close) if last_close is not None else (float(closes_tech[-1]) if closes_tech else 0.0))

    signals = build_ta_signals(
        price=float(px_for_narr),
        ema_levels=ema,
        ema_slopes=ema_slopes,
        rsi=rsi,
        macd=macd,
        boll=boll,
        ich=ich,
        candles=candles,
        red_dog=red_dog,
        elliott=elliott,
        distances=distances,
    )
    narrative = build_ta_narrative(
        ticker=str(underlying).upper(),
        price=float(px_for_narr),
        last_close=float(last_close) if last_close is not None else float(px_for_narr),
        ema_levels=ema,
        ema_slopes=ema_slopes,
        rsi=rsi,
        macd=macd,
        boll=boll,
        ich=ich,
        candles=candles,
        red_dog=red_dog,
        elliott=elliott,
        signals=signals,
    )
    technicals = {
        "enabled": bool(bool(tech_bars)),
        "ticker": str(underlying).upper(),
        "asOfDate": _fmt_date(now),
        "barDateUsed": None if last_bar is None else str(last_bar.trade_date)[:10],
        "lastDailyClose": None if (last_bar is None or last_bar.close is None) else round(float(last_bar.close), 4),
        "livePrice": None if live_px is None else round(float(live_px), 4),
        "ema": {k: (None if v is None else round(float(v), 4)) for k, v in (ema or {}).items()},
        "rsi": {
            **(rsi if isinstance(rsi, dict) else {"enabled": False}),
            "value": (None if not isinstance(rsi, dict) or rsi.get("value") is None else round(float(rsi["value"]), 4)),
            "slope1d": (None if not isinstance(rsi, dict) or rsi.get("slope1d") is None else round(float(rsi["slope1d"]), 4)),
        },
        "macd": (
            {"enabled": False}
            if not isinstance(macd, dict)
            else {
                **macd,
                "macd": (None if macd.get("macd") is None else round(float(macd["macd"]), 6)),
                "signalLine": (None if macd.get("signalLine") is None else round(float(macd["signalLine"]), 6)),
                "hist": (None if macd.get("hist") is None else round(float(macd["hist"]), 6)),
            }
        ),
        "bollinger": (
            {"enabled": False}
            if not isinstance(boll, dict)
            else {
                **boll,
                "mid": (None if boll.get("mid") is None else round(float(boll["mid"]), 4)),
                "upper": (None if boll.get("upper") is None else round(float(boll["upper"]), 4)),
                "lower": (None if boll.get("lower") is None else round(float(boll["lower"]), 4)),
                "bandwidthPct": (None if boll.get("bandwidthPct") is None else round(float(boll["bandwidthPct"]), 4)),
                "percentB": (None if boll.get("percentB") is None else round(float(boll["percentB"]), 4)),
            }
        ),
        "candles": candles,
        "redDog": red_dog,
        "elliott": elliott,
        "ichimoku": ich,
        "vwapProxy": ({"enabled": False} if not isinstance(vwap_proxy, dict) else {**vwap_proxy, "value": (None if vwap_proxy.get("value") is None else round(float(vwap_proxy["value"]), 4))}),
        "distances": distances,
        "signals": signals,
        "narrative": narrative,
        "notes": [
            "Indicators computed on daily bars (EOD).",
            "Live overlay uses ORATS Live spot/stockPrice when available (may reflect afterhours/last known).",
        ],
    }

    # --- Actionable VWAP level (surface a single level for each Engine2 run) ---
    vwap_level: Dict[str, Any] = {"enabled": False, "notes": []}
    try:
        vp = technicals.get("vwapProxy") if isinstance(technicals, dict) else None
        if isinstance(vp, dict) and bool(vp.get("enabled")) and vp.get("value") is not None:
            require_orats = bool(getattr(flags, "ENGINE2_REQUIRE_ORATS_DAILY_VWAP", False))
            if require_orats and str(vp.get("mode") or "") != "orats_daily_vwap":
                vwap_level = {
                    "enabled": False,
                    "notes": [
                        "Pinned to ORATS daily VWAP (ENGINE2_REQUIRE_ORATS_DAILY_VWAP=1).",
                        "ORATS daily VWAP not available for this run; no proxy fallback used.",
                    ],
                }
            else:
                vwap_val = float(vp.get("value"))
                if math.isfinite(vwap_val) and vwap_val > 0:
                    vwap_level = {
                        "enabled": True,
                        "value": round(vwap_val, 4),
                        "mode": str(vp.get("mode") or ""),
                        "window": (None if vp.get("window") is None else int(vp.get("window"))),
                        "barDateUsed": technicals.get("barDateUsed"),
                        "livePrice": technicals.get("livePrice"),
                        "distance": None,
                        "notes": (vp.get("notes") if isinstance(vp.get("notes"), list) else []),
                    }
                    dist = technicals.get("distances") if isinstance(technicals, dict) else None
                    lv = (dist.get("levels") if isinstance(dist, dict) else None) or {}
                    vwap_dist = lv.get("vwapProxy") if isinstance(lv, dict) else None
                    if isinstance(vwap_dist, dict):
                        dp = vwap_dist.get("diffPts")
                        dpc = vwap_dist.get("diffPct")
                        side = None
                        try:
                            dp0 = float(dp)
                            if math.isfinite(dp0):
                                side = "above" if dp0 > 0 else "below" if dp0 < 0 else "at"
                        except Exception:
                            side = None
                        vwap_level["distance"] = {
                            "diffPts": dp,
                            "diffPct": dpc,
                            "side": side,
                        }
    except Exception:
        vwap_level = {"enabled": False, "notes": ["VWAP level unavailable."]}

    telemetry["counts"]["backtest.rowsUsed"] = int(len(week_rows))
    mark("compute.total")
    LOG.info(
        "Engine2 compute done in %.2fs: trade_dates=%s windows=%s week_rows=%s cores_rows=%s",
        (time.perf_counter() - t0),
        int(telemetry["counts"].get("trade_dates", 0)),
        int(telemetry["counts"].get("windows", 0)),
        int(len(week_rows)),
        int(telemetry["counts"].get("orats.cores_rows", 0)),
    )

    return {
        "enabled": bool(flags.ENABLE_ENGINE2_SPX_IC),
        "asOfDate": _fmt_date(now),
        "params": {
            "entryDay": ed,
            "years": yrs,
            "widths": [float(x) for x in widths_use],
            "emMults": [float(x) for x in em_mults],
            "wingWidthPts": [int(x) for x in wing_pts],
            "seasonalityMode": season_mode,
            "deskLocked": True,
        },
        "underlying": {"symbol": underlying, "isProxy": bool(is_proxy), "notes": proxy_notes},
        "current": {"regime": regime_now, "macro": macro_now, "vwap": vwap_level},
        "liveContext": live_context,
        "oddsLikeNow": {
            "regimeBucket": regime_bucket_now,
            "macroBucket": macro_bucket_now,
            "seasonBucket": season_bucket_now,
            "weeksUsed": int(len(like_rows)),
            "byWidth": odds_like_now,
            "notes": ["Conditioned on current buckets (regime/macro/season). Risk-only: breach is expiry-close outside ±(width×EM)."],
        },
        "backtest": bt,
        "technicals": technicals,
        "telemetry": telemetry,
        "notes": proxy_notes,
    }


