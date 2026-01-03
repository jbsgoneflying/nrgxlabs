from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from backend.benzinga_client import BenzingaClient
from backend.config import get_flags
from backend.market_calendar import market_structure_events_by_date, opex_events_by_date
from backend.macro_events import macro_events_by_date
from backend.spx_ic_engine import compute_live_levels, fetch_dailies_ohlc_range, fetch_hist_cores_range, fetch_trading_bars


State = str  # PASS|FAIL|MISSING


def _now_et_date() -> dt.date:
    if ZoneInfo is None:
        return dt.date.today()
    try:
        return dt.datetime.now(tz=ZoneInfo("America/New_York")).date()
    except Exception:
        return dt.date.today()


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        if not math.isfinite(x):
            return None
        return x
    except Exception:
        return None


def _to_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(float(v))
    except Exception:
        return None


def _pct_rank(x: float, xs: List[float]) -> Optional[float]:
    vals = [float(v) for v in (xs or []) if v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))]
    if not vals:
        return None
    c = sum(1 for v in vals if v <= float(x))
    return c / len(vals)


def _zscore(x: float, xs: List[float]) -> Optional[float]:
    vals = [float(v) for v in (xs or []) if v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))]
    if len(vals) < 2:
        return None
    try:
        mu = statistics.mean(vals)
        sd = statistics.stdev(vals)
    except Exception:
        return None
    if not (math.isfinite(mu) and math.isfinite(sd)) or sd <= 1e-12:
        return None
    return (float(x) - float(mu)) / float(sd)


def _median(xs: List[float]) -> Optional[float]:
    vals = [float(v) for v in (xs or []) if v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))]
    if not vals:
        return None
    try:
        return float(statistics.median(vals))
    except Exception:
        return None


def _percentile(xs: List[float], p: float) -> Optional[float]:
    vals = [float(v) for v in (xs or []) if v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))]
    if not vals:
        return None
    q = max(0.0, min(1.0, float(p)))
    vals.sort()
    if len(vals) == 1:
        return float(vals[0])
    idx = q * (len(vals) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(vals[lo])
    w = idx - lo
    return float(vals[lo]) * (1.0 - w) + float(vals[hi]) * w


def _mk_check(
    *,
    id: str,
    label: str,
    state: State,
    code: Optional[str],
    value: Dict[str, Any],
    threshold: Dict[str, Any],
    explain: str,
) -> Dict[str, Any]:
    st = str(state).upper()
    if st not in ("PASS", "FAIL", "MISSING"):
        st = "MISSING"
    return {
        "id": str(id),
        "label": str(label),
        "state": st,
        "code": (str(code) if code else None),
        "value": value or {},
        "threshold": threshold or {},
        "explain": str(explain or ""),
    }


def _bucket_from_ratio(r: Optional[float]) -> Optional[str]:
    if r is None or not math.isfinite(float(r)):
        return None
    x = abs(float(r))
    if x < 0.20:
        return "low"
    if x < 0.50:
        return "medium"
    return "high"


def _is_trading_day(d: dt.date) -> bool:
    if d.weekday() >= 5:
        return False
    evs = (market_structure_events_by_date(start=d, end=d) or {}).get(d.isoformat()) or []
    return not any(str(e.get("kind") or "").upper() == "HOLIDAY" for e in evs if isinstance(e, dict))


def _next_trading_days(start: dt.date, n: int) -> List[dt.date]:
    out: List[dt.date] = []
    d = start
    while len(out) < int(n) and len(out) < 20:
        if _is_trading_day(d):
            out.append(d)
        d = d + dt.timedelta(days=1)
    return out


def _fetch_iv_series_30d(client, *, ticker: str, asof: dt.date) -> Tuple[Optional[str], List[Tuple[str, float]]]:
    """
    Return (field_used, [(tradeDate, ivPct), ...]) for a ~30-trading-day window ending at asof.
    """
    t = str(ticker).strip().upper()
    if not t:
        return None, []
    start = asof - dt.timedelta(days=60)  # buffer for ~30 trading days
    fields = "ticker,tradeDate,iv30,iv30d,iv30Day,iv"
    rows = fetch_hist_cores_range(client, ticker=t, start=start, end=asof, fields=fields) or []
    rows = [r for r in rows if isinstance(r, dict)]
    rows.sort(key=lambda r: str(r.get("tradeDate") or "")[:10])

    candidates = ["iv30", "iv30d", "iv30Day", "iv"]
    series_by: Dict[str, List[Tuple[str, float]]] = {k: [] for k in candidates}
    for r in rows:
        d0 = str(r.get("tradeDate") or "")[:10]
        if not d0:
            continue
        for k in candidates:
            v = _to_float(r.get(k))
            if v is None:
                continue
            iv_pct = float(v) * 100.0 if float(v) <= 1.0 else float(v)
            if iv_pct <= 0:
                continue
            series_by[k].append((d0, float(iv_pct)))

    best = None
    best_n = -1
    for k in candidates:
        npts = len(series_by.get(k) or [])
        if npts > best_n:
            best = k
            best_n = npts
    if len(series_by.get("iv30") or []) >= max(10, best_n):
        best = "iv30"

    series = series_by.get(best or "") or []
    if len(series) > 40:
        series = series[-40:]
    return best, series


def _fetch_underlying_liquidity(client, *, ticker: str) -> Dict[str, Any]:
    t = str(ticker).strip().upper()
    if not t:
        return {"enabled": False, "notes": ["Missing ticker."]}
    notes: List[str] = []
    source: Optional[str] = None
    # Include a few extra aggregate option fields when available (some ORATS plans expose these instead of stock volume).
    fields = "ticker,stockPrice,spotPrice,price,last,avgDollarVol20,avgDolVol20,avgDVol20,avgDollarVol20d,avgDollarVolume20,avgVolume20,avgVol20,avgVolume,avgVol,marketCap,cVolu,pVolu"
    try:
        rows = client.cores(ticker=t, fields=fields).rows or []
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
    except Exception as e:
        row = {}
        notes.append(f"cores unavailable: {type(e).__name__}: {e}")

    # Field aliasing + sniffing (ORATS field names can vary across plans/feeds).
    def _sniff(keys: List[str], *, must: List[str], avoid: List[str]) -> Optional[str]:
        # deterministic: scan sorted keys
        for k in sorted(keys):
            kk = k.lower()
            if any(a in kk for a in avoid):
                continue
            if all(m in kk for m in must):
                return k
        return None

    keys = list(row.keys()) if isinstance(row, dict) else []

    px = _to_float(row.get("spotPrice")) or _to_float(row.get("stockPrice")) or _to_float(row.get("price")) or _to_float(row.get("last"))
    if px is None and keys:
        kpx = _sniff(keys, must=["price"], avoid=["avg", "vol", "dollar"])
        if kpx:
            px = _to_float(row.get(kpx))
            if px is not None:
                notes.append(f"Price inferred from /cores field '{kpx}'.")

    avg_dvol = _to_float(row.get("avgDollarVol20") or row.get("avgDolVol20") or row.get("avgDVol20") or row.get("avgDollarVol20d") or row.get("avgDollarVolume20"))
    if avg_dvol is not None:
        source = "cores"

    if avg_dvol is None and keys:
        kd = _sniff(keys, must=["dollar", "vol", "20"], avoid=[])
        if kd:
            avg_dvol = _to_float(row.get(kd))
            if avg_dvol is not None:
                source = f"cores:{kd}"
                notes.append(f"avgDollarVol20d inferred from /cores field '{kd}'.")

    avg_vol = _to_float(row.get("avgVolume20") or row.get("avgVol20") or row.get("avgVolume") or row.get("avgVol"))
    if avg_vol is None and keys:
        kv = _sniff(keys, must=["avg", "vol", "20"], avoid=["dollar", "iv"])
        if kv:
            avg_vol = _to_float(row.get(kv))
            if avg_vol is not None:
                notes.append(f"avgVolume20 inferred from /cores field '{kv}'.")
    if avg_dvol is None and px is not None and avg_vol is not None and px > 0 and avg_vol > 0:
        avg_dvol = float(px) * float(avg_vol)
        source = source or "cores_price_x_avgVolume20"
    mcap = _to_float(row.get("marketCap"))
    cvolu = _to_float(row.get("cVolu") or row.get("cvolu"))
    pvolu = _to_float(row.get("pVolu") or row.get("pvolu"))

    # If still missing, record why cores wasn't enough (even if /cores call succeeded).
    if avg_dvol is None:
        if not row:
            notes.append("No /cores snapshot row returned.")
        else:
            if keys:
                # Keep short but informative.
                notes.append("cores keys: " + ", ".join(sorted(keys)[:12]) + ("…" if len(keys) > 12 else ""))
            has_dvol_field = any(row.get(k) is not None for k in ("avgDollarVol20", "avgDolVol20", "avgDVol20"))
            if not has_dvol_field:
                notes.append("Missing /cores avgDollarVol20* fields.")
            if avg_vol is None:
                notes.append("Missing /cores avgVolume20/avgVol20 fields.")
            if px is None:
                notes.append("Missing /cores spot/stock price fields.")

    # Fallback: derive avg $ volume from last ~20 trading days of dailies.
    # (This keeps GO/NO-GO resilient when /cores doesn't include volume fields.)
    if avg_dvol is None:
        try:
            end = _now_et_date()
            start = end - dt.timedelta(days=45)  # buffer for ~20 trading days
            bars = fetch_dailies_ohlc_range(client, ticker=t, start=start, end=end) or []
            pairs = [(b.close, b.volume) for b in bars if getattr(b, "close", None) and getattr(b, "volume", None)]
            pairs = [(float(c), float(v)) for (c, v) in pairs if c and v and math.isfinite(float(c)) and math.isfinite(float(v)) and float(c) > 0 and float(v) > 0]
            if len(pairs) >= 10:
                tail = pairs[-20:]
                avg_dvol = sum(c * v for (c, v) in tail) / float(len(tail))
                notes.append("avgDollarVol20d derived from /hist/dailies (close*volume).")
                source = "dailies_close_x_volume"
            else:
                notes.append(f"/hist/dailies insufficient volume rows (bars={len(bars)}, usable_close_x_vol={len(pairs)}).")
        except Exception as e:
            notes.append(f"/hist/dailies range failed: {type(e).__name__}: {e}")

    # Fallback #2: if range pulls are empty/unreliable for this symbol, probe day-by-day (cached) to get last ~20 bars.
    if avg_dvol is None:
        try:
            end = _now_et_date()
            bars = fetch_trading_bars(client, ticker=t, end=end, n=60, max_calendar_scan=140) or []
            pairs = [(b.close, b.volume) for b in bars if getattr(b, "close", None) and getattr(b, "volume", None)]
            pairs = [(float(c), float(v)) for (c, v) in pairs if c and v and math.isfinite(float(c)) and math.isfinite(float(v)) and float(c) > 0 and float(v) > 0]
            if len(pairs) >= 10:
                tail = pairs[-20:]
                avg_dvol = sum(c * v for (c, v) in tail) / float(len(tail))
                notes.append("avgDollarVol20d derived via per-day /hist/dailies probe (close*volume).")
                source = "dailies_probe_close_x_volume"
            else:
                notes.append(f"Per-day dailies probe insufficient volume rows (bars={len(bars)}, usable_close_x_vol={len(pairs)}).")
        except Exception as e:
            notes.append(f"/hist/dailies probe failed: {type(e).__name__}: {e}")

    enabled = bool(row) or (avg_dvol is not None) or (px is not None)
    return {
        "enabled": enabled,
        "price": px,
        "avgDollarVol20d": avg_dvol,
        "marketCap": mcap,
        "cVolu": cvolu,
        "pVolu": pvolu,
        "notes": notes,
        "source": source,
    }


def _band_liquidity_agg(
    *,
    rows: List[dict],
    side: str,
    underlying: Optional[float],
    delta_lo: float,
    delta_hi: float,
    min_mid: float,
) -> Dict[str, Any]:
    """
    Strike-less liquidity proxy: aggregate across an expiry within the target delta band.
    """
    s = str(side).lower()
    if s not in ("put", "call"):
        return {"side": s, "nBand": 0}

    def _strike(r: dict) -> Optional[float]:
        return _to_float(r.get("strike"))

    def _delta(r: dict) -> Optional[float]:
        if s == "call":
            v = _to_float(r.get("callDelta"))
            if v is None:
                d0 = _to_float(r.get("delta"))
                v = d0 if d0 is not None and d0 > 0 else None
            return v
        v = _to_float(r.get("putDelta"))
        if v is None:
            d0 = _to_float(r.get("delta"))
            v = d0 if d0 is not None and d0 < 0 else None
        return v

    bid_key = "callBidPrice" if s == "call" else "putBidPrice"
    ask_key = "callAskPrice" if s == "call" else "putAskPrice"
    oi_key = "callOpenInterest" if s == "call" else "putOpenInterest"
    vol_key = "callVolume" if s == "call" else "putVolume"

    spreads: List[float] = []
    mids: List[float] = []
    n_band = 0
    n_good = 0
    oi_sum = 0.0
    vol_sum = 0.0

    for r in rows:
        if not isinstance(r, dict):
            continue
        k = _strike(r)
        if k is None:
            continue
        if underlying is not None and math.isfinite(float(underlying)) and float(underlying) > 0:
            if s == "call" and float(k) <= float(underlying):
                continue
            if s == "put" and float(k) >= float(underlying):
                continue

        d = _delta(r)
        if d is None:
            continue
        ad = abs(float(d))
        if ad < float(delta_lo) - 1e-12 or ad > float(delta_hi) + 1e-12:
            continue

        n_band += 1

        oi = _to_float(r.get(oi_key))
        if oi is not None and math.isfinite(float(oi)) and float(oi) > 0:
            oi_sum += float(oi)
        vol = _to_float(r.get(vol_key))
        if vol is not None and math.isfinite(float(vol)) and float(vol) > 0:
            vol_sum += float(vol)

        bid = _to_float(r.get(bid_key))
        ask = _to_float(r.get(ask_key))
        if bid is None or ask is None:
            continue
        mid = 0.5 * (float(bid) + float(ask))
        if not (math.isfinite(mid) and mid > 0):
            continue
        if float(mid) < float(min_mid):
            continue
        spr = (float(ask) - float(bid)) / float(mid) if mid > 0 else None
        if spr is None or not math.isfinite(float(spr)) or float(spr) < 0:
            continue
        n_good += 1
        mids.append(float(mid))
        spreads.append(float(spr))

    cov = (float(n_good) / float(n_band)) if n_band > 0 else None
    med_mid = _median(mids)
    med_spr = _median(spreads)
    p90_spr = _percentile(spreads, 0.90) if spreads else None
    return {
        "side": s,
        "nBand": int(n_band),
        "nQuoted": int(n_good),
        "coverage": None if cov is None else round(float(cov), 4),
        "medianMid": None if med_mid is None else round(float(med_mid), 4),
        "medianSpread": None if med_spr is None else round(float(med_spr), 4),
        "p90Spread": None if p90_spr is None else round(float(p90_spr), 4),
        "sumOI": round(float(oi_sum), 2),
        "sumVol": round(float(vol_sum), 2),
    }


@dataclass(frozen=True)
class _OptLegQuote:
    side: str  # put|call
    strike: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    mid: Optional[float]
    spread_ratio: Optional[float]
    oi: Optional[float]
    vol: Optional[float]
    delta: Optional[float]


def _pick_opt_leg_row(*, rows: List[dict], side: str, underlying: Optional[float], target_abs_delta: float) -> Optional[dict]:
    s = str(side).lower()
    if s not in ("put", "call"):
        return None

    def _strike(r: dict) -> Optional[float]:
        return _to_float(r.get("strike"))

    def _delta(r: dict) -> Optional[float]:
        if s == "call":
            v = _to_float(r.get("callDelta"))
            if v is None:
                d0 = _to_float(r.get("delta"))
                v = d0 if d0 is not None and d0 > 0 else None
            return v
        v = _to_float(r.get("putDelta"))
        if v is None:
            d0 = _to_float(r.get("delta"))
            v = d0 if d0 is not None and d0 < 0 else None
        return v

    use = [r for r in rows if isinstance(r, dict)]
    if underlying is not None and underlying > 0:
        eps = 1e-9
        if s == "call":
            otm = [r for r in use if _strike(r) is not None and _strike(r) > float(underlying) + eps]
        else:
            otm = [r for r in use if _strike(r) is not None and _strike(r) < float(underlying) - eps]
        if otm:
            use = otm

    best = None
    best_dist = None
    for r in use:
        d = _delta(r)
        if d is None:
            continue
        dist = abs(abs(float(d)) - float(target_abs_delta))
        if best is None or best_dist is None or dist < best_dist:
            best = r
            best_dist = dist
    return best


def _extract_leg_metrics(row: dict, *, side: str) -> _OptLegQuote:
    s = str(side).lower()
    strike = _to_float(row.get("strike"))
    if s == "call":
        bid = _to_float(row.get("callBidPrice"))
        ask = _to_float(row.get("callAskPrice"))
        oi = _to_float(row.get("callOpenInterest"))
        vol = _to_float(row.get("callVolume"))
        delta = _to_float(row.get("callDelta")) or (_to_float(row.get("delta")) if (_to_float(row.get("delta")) or 0) > 0 else None)
    else:
        bid = _to_float(row.get("putBidPrice"))
        ask = _to_float(row.get("putAskPrice"))
        oi = _to_float(row.get("putOpenInterest"))
        vol = _to_float(row.get("putVolume"))
        delta = _to_float(row.get("putDelta")) or (_to_float(row.get("delta")) if (_to_float(row.get("delta")) or 0) < 0 else None)

    mid = None
    spread_ratio = None
    if bid is not None and ask is not None:
        mid = 0.5 * (float(bid) + float(ask))
        if mid is not None and mid > 1e-9:
            spread_ratio = (float(ask) - float(bid)) / float(mid)

    return _OptLegQuote(side=s, strike=strike, bid=bid, ask=ask, mid=mid, spread_ratio=spread_ratio, oi=oi, vol=vol, delta=delta)


def _rv_annualized_from_closes(closes: List[float], window: int) -> Optional[float]:
    if len(closes) < window + 1 or window < 2:
        return None
    rets: List[float] = []
    for i in range(len(closes) - window, len(closes)):
        a = float(closes[i - 1])
        b = float(closes[i])
        if a > 0 and b > 0:
            rets.append(math.log(b / a))
    if len(rets) < 2:
        return None
    try:
        s = statistics.stdev(rets)
    except Exception:
        return None
    if not math.isfinite(s):
        return None
    return float(s) * math.sqrt(252.0)


def _corr_beta_from_close_maps(
    *,
    a_by_date: Dict[str, float],
    b_by_date: Dict[str, float],
    lookback_days: int,
) -> Tuple[Optional[float], Optional[float], int]:
    """
    Compute correlation and beta between two close series using aligned daily log returns.
    Returns (corr, beta, n_returns).
    """
    dates = sorted(set(a_by_date.keys()).intersection(set(b_by_date.keys())))
    if len(dates) < lookback_days + 1:
        return None, None, 0
    dates = dates[-(lookback_days + 1) :]
    ra: List[float] = []
    rb: List[float] = []
    for i in range(1, len(dates)):
        d0 = dates[i - 1]
        d1 = dates[i]
        a0 = float(a_by_date.get(d0) or 0.0)
        a1 = float(a_by_date.get(d1) or 0.0)
        b0 = float(b_by_date.get(d0) or 0.0)
        b1 = float(b_by_date.get(d1) or 0.0)
        if a0 <= 0 or a1 <= 0 or b0 <= 0 or b1 <= 0:
            continue
        ra.append(math.log(a1 / a0))
        rb.append(math.log(b1 / b0))
    n = min(len(ra), len(rb))
    if n < max(10, lookback_days // 2):
        return None, None, n
    ra = ra[-n:]
    rb = rb[-n:]
    try:
        ma = statistics.mean(ra)
        mb = statistics.mean(rb)
        va = statistics.pvariance(ra)
        vb = statistics.pvariance(rb)
    except Exception:
        return None, None, n
    if not (math.isfinite(va) and math.isfinite(vb)) or vb <= 1e-18:
        return None, None, n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / max(1, n)
    beta = cov / vb
    corr = None if va <= 1e-18 else cov / (math.sqrt(va) * math.sqrt(vb))
    if corr is not None and not math.isfinite(corr):
        corr = None
    if beta is not None and not math.isfinite(beta):
        beta = None
    return corr, beta, n


def compute_go_no_go(
    client,
    *,
    ticker: str,
    payload: Dict[str, Any],
    benzinga_client: Optional[BenzingaClient] = None,
) -> Dict[str, Any]:
    """
    Compute strict GO/NO-GO with explainable PASS/FAIL/MISSING checks.
    Missing data yields NO_GO but is distinct from FAIL.
    """
    f = get_flags()
    t = str(ticker or "").strip().upper()
    checks: List[dict] = []
    warnings: List[dict] = []
    notes: List[str] = []

    # Thresholds (config-driven with safe defaults)
    IVP_MIN = float(getattr(f, "GO_IVP_MIN", 0.80))
    IV_SAMPLE_MIN = int(getattr(f, "GO_IV_SAMPLE_MIN", 20))
    IV30_FLOOR = float(getattr(f, "GO_IV30_FLOOR", 0.30))
    # IV values in this module are expressed in percent units (e.g., 30.0 = 30%).
    # Accept user-friendly config like 0.30 to mean 30%.
    if IV30_FLOOR <= 1.5:
        IV30_FLOOR = IV30_FLOOR * 100.0
    IV_Z_ENABLED = bool(getattr(f, "GO_IV_Z_ENABLED", True))
    IV30_Z_MIN = float(getattr(f, "GO_IV30_Z_MIN", 0.75))

    MIN_EARNINGS_N = int(getattr(f, "GO_MIN_EARNINGS_N", 6))
    EM_RICHNESS_MULT = float(getattr(f, "GO_EM_RICHNESS_MULT", 1.05))

    TAIL_SAMPLE_MIN = int(getattr(f, "GO_TAIL_SAMPLE_MIN", 8))
    TAIL_P90_MULT = float(getattr(f, "GO_TAIL_P90_MULT", 0.80))

    CORR20_HIGH = float(getattr(f, "GO_CORR20_HIGH", 0.70))
    BETA20_HIGH = float(getattr(f, "GO_BETA20_HIGH", 1.20))

    AVG_DVOL_MIN = float(getattr(f, "GO_AVG_DOLLAR_VOL20D_MIN", 200_000_000.0))
    DELTA_LO = float(getattr(f, "GO_OPT_DELTA_BAND_LO", 0.15))
    DELTA_HI = float(getattr(f, "GO_OPT_DELTA_BAND_HI", 0.20))
    SPREAD_MAX = float(getattr(f, "GO_OPT_SPREAD_MAX", 0.15))
    SPREAD_MAX_P90 = float(getattr(f, "GO_OPT_SPREAD_MAX_P90", 0.25))
    MIN_MID = float(getattr(f, "GO_OPT_MIN_MID", 0.20))
    OI_MIN = float(getattr(f, "GO_OPT_OI_MIN", 500.0))
    VOL_MIN = float(getattr(f, "GO_OPT_VOL_MIN", 50.0))
    BAND_QUOTE_COVERAGE_MIN = float(getattr(f, "GO_BAND_QUOTE_COVERAGE_MIN", 0.70))
    BAND_OI_SUM_MIN = float(getattr(f, "GO_BAND_OI_SUM_MIN", 2000.0))
    BAND_VOL_SUM_MIN = float(getattr(f, "GO_BAND_VOL_SUM_MIN", 200.0))

    RV5_JUMP_MAX = float(getattr(f, "GO_RV5_JUMP_MAX", 1.15))
    RV20_JUMP_MAX = float(getattr(f, "GO_RV20_JUMP_MAX", 1.10))
    RV5_ACCEL_TIGHTEN = float(getattr(f, "GO_RV5_ACCEL_TIGHTEN_TRIGGER", 1.05))
    FLIP_CUTOFF_BASE = float(getattr(f, "GO_FLIP_CUTOFF_BASE", 2.0))
    FLIP_CUTOFF_TIGHT = float(getattr(f, "GO_FLIP_CUTOFF_TIGHT", 2.5))

    FLOW_WINDOW_TD = int(getattr(f, "GO_FORCED_FLOW_WINDOW_TRADING_DAYS", 4))
    FLOW_IMPORTANCE_HIGH_MIN = int(getattr(f, "GO_FORCED_FLOW_IMPORTANCE_HIGH_MIN", 4))
    FLOW_IMPORTANCE_MED_MIN = int(getattr(f, "GO_FORCED_FLOW_IMPORTANCE_MED_MIN", 3))
    FLOW_MANUAL_RANGES = list(getattr(f, "GO_FORCED_FLOW_MANUAL_RANGES", []) or [])

    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    cur_asof = str(current.get("asOfDate") or "")[:10]
    try:
        asof_date = dt.date.fromisoformat(cur_asof) if cur_asof else _now_et_date()
    except Exception:
        asof_date = _now_et_date()

    expected_move_pct = _to_float(current.get("impliedMovePct"))
    expected_move_src: Optional[str] = "current.impliedMovePct" if expected_move_pct is not None else None
    if expected_move_pct is None:
        ne = payload.get("nextEvent") if isinstance(payload.get("nextEvent"), dict) else {}
        for k in ("impliedMovePctPlanned", "impliedMovePct", "expectedMovePct"):
            cand = _to_float(ne.get(k))
            if cand is not None:
                expected_move_pct = cand
                expected_move_src = f"nextEvent.{k}"
                break

    # ===== A2) EM richness =====
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    realized_vals = [_to_float(e.get("realizedMovePct")) for e in events if isinstance(e, dict)]
    realized_vals = [float(x) for x in realized_vals if x is not None]
    realized_median = _median(realized_vals)
    n_used = len(realized_vals)

    if n_used < MIN_EARNINGS_N:
        checks.append(
            _mk_check(
                id="SN_EM_RICHNESS",
                label="Expected move rich vs realized median",
                state="MISSING",
                code="SN_EM_SAMPLE_TOO_SMALL",
                value={"expectedMovePct": expected_move_pct, "expectedMoveSource": expected_move_src, "realizedMedianPct": realized_median, "nUsed": n_used},
                threshold={"minEarningsN": MIN_EARNINGS_N, "emRichnessMult": EM_RICHNESS_MULT},
                explain=f"Insufficient usable earnings events (n={n_used} < {MIN_EARNINGS_N}).",
            )
        )
    elif expected_move_pct is None or realized_median is None or realized_median <= 0:
        checks.append(
            _mk_check(
                id="SN_EM_RICHNESS",
                label="Expected move rich vs realized median",
                state="MISSING",
                code="SN_EM_DATA_MISSING",
                value={"expectedMovePct": expected_move_pct, "expectedMoveSource": expected_move_src, "realizedMedianPct": realized_median, "nUsed": n_used},
                threshold={"minEarningsN": MIN_EARNINGS_N, "emRichnessMult": EM_RICHNESS_MULT},
                explain="Missing expected move or realized median.",
            )
        )
    else:
        ratio = float(expected_move_pct) / float(realized_median) if realized_median > 0 else None
        need = float(realized_median) * float(EM_RICHNESS_MULT)
        passed = float(expected_move_pct) >= float(need)
        checks.append(
            _mk_check(
                id="SN_EM_RICHNESS",
                label="Expected move rich vs realized median",
                state="PASS" if passed else "FAIL",
                code=None if passed else "SN_EM_NOT_RICH_ENOUGH",
                value={"expectedMovePct": expected_move_pct, "expectedMoveSource": expected_move_src, "realizedMedianPct": realized_median, "nUsed": n_used, "ratio": None if ratio is None else round(float(ratio), 4)},
                threshold={"minEarningsN": MIN_EARNINGS_N, "emRichnessMult": EM_RICHNESS_MULT, "requiredExpectedMovePct": round(float(need), 4)},
                explain=(f"Expected/median ratio={ratio:.3f}× vs min {EM_RICHNESS_MULT:.2f}×." if ratio is not None else ("Pass" if passed else "Fail")),
            )
        )

    # ===== A2b) Tail gap proxy: P90 realized =====
    p90 = _percentile(realized_vals, 0.90) if realized_vals else None
    if n_used < int(TAIL_SAMPLE_MIN):
        checks.append(
            _mk_check(
                id="SN_TAIL_P90_RICHNESS",
                label="Expected move vs P90 realized (tail proxy)",
                state="MISSING",
                code="SN_TAIL_SAMPLE_TOO_SMALL",
                value={"expectedMovePct": expected_move_pct, "expectedMoveSource": expected_move_src, "p90RealizedPct": p90, "nUsed": n_used},
                threshold={"minEarningsN": int(TAIL_SAMPLE_MIN), "tailMult": float(TAIL_P90_MULT)},
                explain=f"Insufficient usable earnings events for tail estimate (n={n_used} < {int(TAIL_SAMPLE_MIN)}).",
            )
        )
    elif expected_move_pct is None or p90 is None or float(p90) <= 0:
        checks.append(
            _mk_check(
                id="SN_TAIL_P90_RICHNESS",
                label="Expected move vs P90 realized (tail proxy)",
                state="MISSING",
                code="SN_TAIL_DATA_MISSING",
                value={"expectedMovePct": expected_move_pct, "expectedMoveSource": expected_move_src, "p90RealizedPct": p90, "nUsed": n_used},
                threshold={"minEarningsN": int(TAIL_SAMPLE_MIN), "tailMult": float(TAIL_P90_MULT)},
                explain="Missing expected move or P90 realized.",
            )
        )
    else:
        req = float(TAIL_P90_MULT) * float(p90)
        ratio90 = float(expected_move_pct) / float(p90) if float(p90) > 0 else None
        passed90 = float(expected_move_pct) >= float(req)
        checks.append(
            _mk_check(
                id="SN_TAIL_P90_RICHNESS",
                label="Expected move vs P90 realized (tail proxy)",
                state="PASS" if passed90 else "FAIL",
                code=None if passed90 else "SN_TAIL_P90_TOO_LARGE",
                value={
                    "expectedMovePct": expected_move_pct,
                    "expectedMoveSource": expected_move_src,
                    "p90RealizedPct": round(float(p90), 4),
                    "nUsed": n_used,
                    "ratio": None if ratio90 is None else round(float(ratio90), 4),
                },
                threshold={"tailMult": float(TAIL_P90_MULT), "requiredExpectedMovePct": round(float(req), 4)},
                explain=f"Expected/P90 ratio={ratio90:.3f}× vs min {float(TAIL_P90_MULT):.2f}×." if ratio90 is not None else ("Pass" if passed90 else "Fail"),
            )
        )

    # ===== A1) IV elevated =====
    field_used, iv_series = _fetch_iv_series_30d(client, ticker=t, asof=asof_date)
    iv_vals = [float(v) for _, v in iv_series]
    current_iv = iv_vals[-1] if iv_vals else None
    iv_sample_n = len(iv_vals)
    iv_pctl = _pct_rank(float(current_iv), iv_vals) if current_iv is not None else None
    iv_z = _zscore(float(current_iv), iv_vals) if current_iv is not None else None

    if iv_sample_n < IV_SAMPLE_MIN:
        checks.append(
            _mk_check(
                id="SN_IV_ELEVATED",
                label="IV30 elevated vs own 30d history",
                state="MISSING",
                code="SN_IV_SAMPLE_INSUFFICIENT",
                value={"fieldUsed": field_used, "currentIv30Pct": current_iv, "sampleN": iv_sample_n, "percentile01": iv_pctl, "z": iv_z},
                threshold={"ivpMin": IVP_MIN, "sampleMin": IV_SAMPLE_MIN, "ivFloorPct": IV30_FLOOR, "zEnabled": IV_Z_ENABLED, "zMin": IV30_Z_MIN},
                explain=f"Insufficient IV history (n={iv_sample_n} < {IV_SAMPLE_MIN}).",
            )
        )
    elif current_iv is None or iv_pctl is None:
        checks.append(
            _mk_check(
                id="SN_IV_ELEVATED",
                label="IV30 elevated vs own 30d history",
                state="MISSING",
                code="SN_IV_DATA_MISSING",
                value={"fieldUsed": field_used, "currentIv30Pct": current_iv, "sampleN": iv_sample_n, "percentile01": iv_pctl, "z": iv_z},
                threshold={"ivpMin": IVP_MIN, "sampleMin": IV_SAMPLE_MIN, "ivFloorPct": IV30_FLOOR, "zEnabled": IV_Z_ENABLED, "zMin": IV30_Z_MIN},
                explain="Missing IV series/percentile.",
            )
        )
    elif float(current_iv) < float(IV30_FLOOR):
        checks.append(
            _mk_check(
                id="SN_IV_ELEVATED",
                label="IV30 elevated vs own 30d history",
                state="FAIL",
                code="SN_IV_TOO_LOW_ABSOLUTE",
                value={"fieldUsed": field_used, "currentIv30Pct": current_iv, "sampleN": iv_sample_n, "percentile01": round(float(iv_pctl), 4), "z": None if iv_z is None else round(float(iv_z), 4)},
                threshold={"ivpMin": IVP_MIN, "sampleMin": IV_SAMPLE_MIN, "ivFloorPct": IV30_FLOOR, "zEnabled": IV_Z_ENABLED, "zMin": IV30_Z_MIN},
                explain=f"IV30 {float(current_iv):.2f}% below absolute floor {IV30_FLOOR:.2f}.",
            )
        )
    elif float(iv_pctl) < float(IVP_MIN):
        checks.append(
            _mk_check(
                id="SN_IV_ELEVATED",
                label="IV30 elevated vs own 30d history",
                state="FAIL",
                code="SN_IVP_TOO_LOW",
                value={"fieldUsed": field_used, "currentIv30Pct": current_iv, "sampleN": iv_sample_n, "percentile01": round(float(iv_pctl), 4), "z": None if iv_z is None else round(float(iv_z), 4)},
                threshold={"ivpMin": IVP_MIN, "sampleMin": IV_SAMPLE_MIN, "ivFloorPct": IV30_FLOOR, "zEnabled": IV_Z_ENABLED, "zMin": IV30_Z_MIN},
                explain=f"IV percentile {float(iv_pctl):.2f} below cutoff {IVP_MIN:.2f}.",
            )
        )
    else:
        if IV_Z_ENABLED:
            if iv_z is None:
                checks.append(
                    _mk_check(
                        id="SN_IV_ELEVATED",
                        label="IV30 elevated vs own 30d history",
                        state="MISSING",
                        code="SN_IV_Z_UNAVAILABLE",
                        value={"fieldUsed": field_used, "currentIv30Pct": current_iv, "sampleN": iv_sample_n, "percentile01": round(float(iv_pctl), 4), "z": None},
                        threshold={"ivpMin": IVP_MIN, "sampleMin": IV_SAMPLE_MIN, "ivFloorPct": IV30_FLOOR, "zEnabled": IV_Z_ENABLED, "zMin": IV30_Z_MIN},
                        explain="Z-score unavailable (insufficient variance/history).",
                    )
                )
            elif float(iv_z) < float(IV30_Z_MIN):
                checks.append(
                    _mk_check(
                        id="SN_IV_ELEVATED",
                        label="IV30 elevated vs own 30d history",
                        state="FAIL",
                        code="SN_IV_NOT_ELEVATED_Z",
                        value={"fieldUsed": field_used, "currentIv30Pct": current_iv, "sampleN": iv_sample_n, "percentile01": round(float(iv_pctl), 4), "z": round(float(iv_z), 4)},
                        threshold={"ivpMin": IVP_MIN, "sampleMin": IV_SAMPLE_MIN, "ivFloorPct": IV30_FLOOR, "zEnabled": IV_Z_ENABLED, "zMin": IV30_Z_MIN},
                        explain=f"IV z-score {float(iv_z):.2f} below cutoff {IV30_Z_MIN:.2f}.",
                    )
                )
            else:
                checks.append(
                    _mk_check(
                        id="SN_IV_ELEVATED",
                        label="IV30 elevated vs own 30d history",
                        state="PASS",
                        code=None,
                        value={"fieldUsed": field_used, "currentIv30Pct": current_iv, "sampleN": iv_sample_n, "percentile01": round(float(iv_pctl), 4), "z": round(float(iv_z), 4)},
                        threshold={"ivpMin": IVP_MIN, "sampleMin": IV_SAMPLE_MIN, "ivFloorPct": IV30_FLOOR, "zEnabled": IV_Z_ENABLED, "zMin": IV30_Z_MIN},
                        explain="IV elevated (percentile + floor + z-score).",
                    )
                )
        else:
            checks.append(
                _mk_check(
                    id="SN_IV_ELEVATED",
                    label="IV30 elevated vs own 30d history",
                    state="PASS",
                    code=None,
                    value={"fieldUsed": field_used, "currentIv30Pct": current_iv, "sampleN": iv_sample_n, "percentile01": round(float(iv_pctl), 4), "z": None if iv_z is None else round(float(iv_z), 4)},
                    threshold={"ivpMin": IVP_MIN, "sampleMin": IV_SAMPLE_MIN, "ivFloorPct": IV30_FLOOR, "zEnabled": False, "zMin": IV30_Z_MIN},
                    explain="IV elevated (percentile + floor).",
                )
            )

    # ===== A3) Legal/reg binary (hybrid) =====
    deny = {str(x).strip().upper() for x in (getattr(f, "LEGAL_REG_TICKER_DENYLIST", []) or []) if str(x).strip()}
    allow = {str(x).strip().upper() for x in (getattr(f, "LEGAL_REG_TICKER_ALLOWLIST", []) or []) if str(x).strip()}
    kw = [str(x).strip().lower() for x in (getattr(f, "LEGAL_REG_KEYWORDS", []) or []) if str(x).strip()]
    if not kw:
        kw = ["sec", "doj", "ftc", "lawsuit", "probe", "investigation", "antitrust", "injunction", "ban", "regulator", "settlement"]

    if t in deny:
        checks.append(
            _mk_check(
                id="SN_LEGAL_REG",
                label="No known legal / regulatory binary",
                state="FAIL",
                code="SN_LEGAL_REG_MANUAL_FLAG",
                value={"ticker": t, "source": "manual_denylist"},
                threshold={"keywordScanEnabled": benzinga_client is not None},
                explain="Ticker is manually flagged for legal/regulatory binary risk.",
            )
        )
    elif benzinga_client is None:
        if t in allow:
            checks.append(
                _mk_check(
                    id="SN_LEGAL_REG",
                    label="No known legal / regulatory binary",
                    state="PASS",
                    code=None,
                    value={"ticker": t, "source": "manual_allowlist"},
                    threshold={"keywordScanEnabled": False},
                    explain="Ticker is allowlisted (Benzinga disabled).",
                )
            )
        else:
            checks.append(
                _mk_check(
                    id="SN_LEGAL_REG",
                    label="No known legal / regulatory binary",
                    state="MISSING",
                    code="SN_LEGAL_REG_DATA_MISSING",
                    value={"ticker": t, "source": "benzinga_disabled"},
                    threshold={"keywordScanEnabled": False},
                    explain="Benzinga disabled and ticker not allowlisted.",
                )
            )
    else:
        hits: List[str] = []
        headlines: List[str] = []
        now = _now_et_date()
        # news
        try:
            resp = benzinga_client.news(
                tickers=t,
                date_from=str(now)[:10],
                date_to=str(now + dt.timedelta(days=7))[:10],
                page_size=50,
                display_output="headline",
            )
            for r in (resp.rows or []):
                if isinstance(r, dict):
                    h = str(r.get("title") or r.get("headline") or "").strip()
                    if h:
                        headlines.append(h)
        except Exception:
            pass
        # WIIM
        try:
            resp = benzinga_client.news(
                tickers=t,
                date_from=str(now)[:10],
                date_to=str(now + dt.timedelta(days=7))[:10],
                channels="WIIM",
                page_size=50,
                display_output="headline",
            )
            for r in (resp.rows or []):
                if isinstance(r, dict):
                    h = str(r.get("title") or r.get("headline") or "").strip()
                    if h:
                        headlines.append(h)
        except Exception:
            pass

        for h in headlines:
            s = h.lower()
            if any(k in s for k in kw):
                hits.append(h)
            if len(hits) >= 5:
                break

        if hits:
            checks.append(
                _mk_check(
                    id="SN_LEGAL_REG",
                    label="No known legal / regulatory binary",
                    state="FAIL",
                    code="SN_LEGAL_REG_HEADLINE_HIT",
                    value={"ticker": t, "hits": hits[:5], "keywords": kw[:12]},
                    threshold={"keywordScanEnabled": True, "windowDays": 7},
                    explain=f"Found {len(hits)} legal/reg keyword headline hit(s).",
                )
            )
        else:
            checks.append(
                _mk_check(
                    id="SN_LEGAL_REG",
                    label="No known legal / regulatory binary",
                    state="PASS",
                    code=None,
                    value={"ticker": t, "hits": [], "keywords": kw[:12]},
                    threshold={"keywordScanEnabled": True, "windowDays": 7},
                    explain="No legal/reg headline hits detected.",
                )
            )

    # ===== A4) Liquidity (underlying + options) =====
    liq = _fetch_underlying_liquidity(client, ticker=t)
    avg_dvol = _to_float(liq.get("avgDollarVol20d"))
    underlying_ok = avg_dvol is not None and float(avg_dvol) >= float(AVG_DVOL_MIN)
    liq_notes = list(liq.get("notes") or []) if isinstance(liq, dict) else []

    # Options liquidity (strike-less): aggregate within delta band for chosen expiry
    trade_date = str(current.get("asOfDate") or "")[:10] or None
    dte_target = 2
    tb_inputs = payload.get("tradeBuilderInputs") if isinstance(payload.get("tradeBuilderInputs"), dict) else {}
    if isinstance(tb_inputs, dict):
        dte_in = _to_int(tb_inputs.get("dte_target"))
        if dte_in is not None and 1 <= int(dte_in) <= 60:
            dte_target = int(dte_in)
    opt_expiry = None
    band_put: Optional[Dict[str, Any]] = None
    band_call: Optional[Dict[str, Any]] = None
    under_px: Optional[float] = None

    opt_state: State = "PASS"
    opt_code: Optional[str] = None
    opt_explain = ""

    if trade_date:
        try:
            fields_m = "ticker,tradeDate,expirDate,dte,stockPrice,vol50,atmiv"
            # Include 0DTE on Fridays so "Friday looks at Friday" works.
            lo = max(0, int(dte_target) - 2)
            hi = int(dte_target) + 10
            mrows = client.hist_monies_implied(ticker=t, trade_date=trade_date, fields=fields_m, dte=f"{lo},{hi}").rows or []
            mrows = [r for r in mrows if isinstance(r, dict)]
            # Prefer nearest expiry to *today* (ET), not a fixed DTE.
            today_et = _now_et_date()
            best = None
            best_gap = None
            for r in mrows:
                ed = str(r.get("expirDate") or "")[:10]
                if not ed:
                    continue
                try:
                    ed_dt = dt.date.fromisoformat(ed)
                except Exception:
                    continue
                gap = (ed_dt - today_et).days
                if gap < 0:
                    continue
                if best is None or best_gap is None or gap < best_gap:
                    best = r
                    best_gap = gap
            # Fallback: if nothing is >= today, pick closest to dte_target (legacy behavior).
            if best is None:
                best_dist = None
                for r in mrows:
                    dte_v = _to_float(r.get("dte"))
                    if dte_v is None:
                        continue
                    dist = abs(float(dte_v) - float(dte_target))
                    if best is None or best_dist is None or dist < best_dist:
                        best = r
                        best_dist = dist
            if best and best.get("expirDate"):
                opt_expiry = str(best.get("expirDate"))[:10]
        except Exception:
            opt_expiry = None

        if not opt_expiry:
            opt_state, opt_code, opt_explain = "MISSING", "SN_OPT_QUOTES_MISSING", "Unable to determine expiration for liquidity check."
        else:
            fields_s = ",".join(
                [
                    "ticker",
                    "tradeDate",
                    "expirDate",
                    "dte",
                    "strike",
                    "stockPrice",
                    "callBidPrice",
                    "callAskPrice",
                    "putBidPrice",
                    "putAskPrice",
                    "callDelta",
                    "putDelta",
                    "delta",
                    "callOpenInterest",
                    "putOpenInterest",
                    "callVolume",
                    "putVolume",
                ]
            )
            try:
                rows = client.hist_strikes(ticker=t, trade_date=trade_date, fields=fields_s, dte=f"{max(0,dte_target-2)},{dte_target+10}").rows or []
                rows = [r for r in rows if isinstance(r, dict) and str(r.get("expirDate") or "")[:10] == opt_expiry]
            except Exception:
                rows = []

            if not rows:
                opt_state, opt_code, opt_explain = "MISSING", "SN_OPT_QUOTES_MISSING", "Options chain unavailable for selected expiration."
            else:
                under_px = _to_float(rows[0].get("stockPrice")) or _to_float(liq.get("price"))
                band_put = _band_liquidity_agg(rows=rows, side="put", underlying=under_px, delta_lo=DELTA_LO, delta_hi=DELTA_HI, min_mid=MIN_MID)
                band_call = _band_liquidity_agg(rows=rows, side="call", underlying=under_px, delta_lo=DELTA_LO, delta_hi=DELTA_HI, min_mid=MIN_MID)

                n_p = _to_int((band_put or {}).get("nBand")) or 0
                n_c = _to_int((band_call or {}).get("nBand")) or 0
                cov_p = _to_float((band_put or {}).get("coverage"))
                cov_c = _to_float((band_call or {}).get("coverage"))

                if n_p <= 0 or n_c <= 0:
                    opt_state, opt_code, opt_explain = "MISSING", "SN_OPT_QUOTES_MISSING", "No strikes found in target delta band for one or both sides."
                elif cov_p is None or cov_c is None or float(cov_p) < float(BAND_QUOTE_COVERAGE_MIN) or float(cov_c) < float(BAND_QUOTE_COVERAGE_MIN):
                    opt_state, opt_code, opt_explain = (
                        "MISSING",
                        "SN_OPT_QUOTES_MISSING",
                        f"Insufficient quote coverage in delta band (P={cov_p if cov_p is not None else '—'}, C={cov_c if cov_c is not None else '—'}) vs min {BAND_QUOTE_COVERAGE_MIN:.2f}.",
                    )
                else:
                    med_sp_p = _to_float((band_put or {}).get("medianSpread"))
                    med_sp_c = _to_float((band_call or {}).get("medianSpread"))
                    p90_sp_p = _to_float((band_put or {}).get("p90Spread"))
                    p90_sp_c = _to_float((band_call or {}).get("p90Spread"))
                    if (med_sp_p is not None and float(med_sp_p) > float(SPREAD_MAX)) or (med_sp_c is not None and float(med_sp_c) > float(SPREAD_MAX)):
                        opt_state, opt_code, opt_explain = "FAIL", "SN_OPT_SPREAD_TOO_WIDE", f"Median spread ratio above {SPREAD_MAX:.2f} in delta band."
                    elif float(SPREAD_MAX_P90) > 0 and (
                        (p90_sp_p is not None and float(p90_sp_p) > float(SPREAD_MAX_P90)) or (p90_sp_c is not None and float(p90_sp_c) > float(SPREAD_MAX_P90))
                    ):
                        opt_state, opt_code, opt_explain = "FAIL", "SN_OPT_SPREAD_TOO_WIDE", f"P90 spread ratio above {SPREAD_MAX_P90:.2f} in delta band."
                    else:
                        oi_p = _to_float((band_put or {}).get("sumOI")) or 0.0
                        oi_c = _to_float((band_call or {}).get("sumOI")) or 0.0
                        vol_p = _to_float((band_put or {}).get("sumVol")) or 0.0
                        vol_c = _to_float((band_call or {}).get("sumVol")) or 0.0

                        def _side_ok(oi_sum: float, vol_sum: float) -> bool:
                            return (float(oi_sum) >= float(BAND_OI_SUM_MIN)) or (float(vol_sum) >= float(BAND_VOL_SUM_MIN))

                        if not _side_ok(oi_p, vol_p) or not _side_ok(oi_c, vol_c):
                            opt_state, opt_code, opt_explain = (
                                "FAIL",
                                "SN_OPT_OI_TOO_LOW",
                                f"Band OI/vol below minimum (need OI_sum>={BAND_OI_SUM_MIN:.0f} or Vol_sum>={BAND_VOL_SUM_MIN:.0f} per side).",
                            )
                        else:
                            opt_state, opt_code, opt_explain = "PASS", None, "Options liquidity looks sufficient in delta band."
    else:
        opt_state, opt_code, opt_explain = "MISSING", "SN_OPT_QUOTES_MISSING", "Missing trade date for options chain."

    # Always record options-side outcome for debug (especially important when underlying liquidity is missing).
    try:
        liq_notes.append(f"Options liquidity: {opt_state}{(' ' + str(opt_code)) if opt_code else ''} — {opt_explain}")
        liq_notes.append(f"Expiry selected: {opt_expiry or '—'} (tradeDate={trade_date or '—'}; todayET={_now_et_date().isoformat()})")
    except Exception:
        pass

    # Decide final liquidity state:
    # - Prefer the spec'd underlying $vol20 gate when available
    # - If underlying $vol is unavailable (common on some ORATS plans), allow a strict options-liquidity proxy
    #   to avoid permanent MISSING when we *do* have strong executable options liquidity.
    if avg_dvol is None:
        # Proxy rule: if options liquidity is PASS and we have a spot, treat underlying as OK-by-proxy.
        ok_by_proxy = False
        proxy_reason = ""
        if opt_state == "PASS" and under_px is not None and band_put and band_call:
            oi_p = _to_float((band_put or {}).get("sumOI")) or 0.0
            oi_c = _to_float((band_call or {}).get("sumOI")) or 0.0
            vol_p = _to_float((band_put or {}).get("sumVol")) or 0.0
            vol_c = _to_float((band_call or {}).get("sumVol")) or 0.0
            # Require BOTH sides to meet at least one of the aggregate thresholds (same as the options gate itself).
            def _side_ok(oi_sum: float, vol_sum: float) -> bool:
                return (float(oi_sum) >= float(BAND_OI_SUM_MIN)) or (float(vol_sum) >= float(BAND_VOL_SUM_MIN))

            if _side_ok(oi_p, vol_p) and _side_ok(oi_c, vol_c):
                ok_by_proxy = True
                proxy_reason = "Underlying $vol unavailable; passed using options-liquidity proxy."

        if ok_by_proxy:
            liq_state, liq_code, liq_explain = "PASS", None, proxy_reason
            liq_notes.append(proxy_reason)
        else:
            liq_state, liq_code, liq_explain = "MISSING", "SN_LIQ_UNDERLYING_MISSING", "Underlying liquidity (avg $vol) unavailable."
    elif not underlying_ok:
        liq_state, liq_code, liq_explain = "FAIL", "SN_LIQ_UNDERLYING_TOO_LOW", f"avgDollarVol20d {float(avg_dvol):.0f} below {AVG_DVOL_MIN:.0f}."
    else:
        liq_state, liq_code, liq_explain = opt_state, opt_code, (opt_explain or "Underlying and options liquidity checks passed.")
        if liq_state == "PASS":
            liq_code = None

    checks.append(
        _mk_check(
            id="SN_LIQUIDITY",
            label="Liquidity deep enough for clean exit",
            state=liq_state,
            code=liq_code,
            value={
                "avgDollarVol20d": avg_dvol,
                "avgDollarVolOk": underlying_ok,
                "underlyingSource": liq.get("source") if isinstance(liq, dict) else None,
                "underlyingOptAgg": {"cVolu": (liq.get("cVolu") if isinstance(liq, dict) else None), "pVolu": (liq.get("pVolu") if isinstance(liq, dict) else None)},
                "notes": liq_notes,
                "expiry": opt_expiry,
                "dteTarget": dte_target,
                "spotUsed": under_px,
                "deltaBandAgg": {"put": band_put, "call": band_call, "deltaBand": [DELTA_LO, DELTA_HI]},
            },
            threshold={
                "avgDollarVol20dMin": AVG_DVOL_MIN,
                "deltaBand": [DELTA_LO, DELTA_HI],
                "dteTarget": dte_target,
                "minMid": MIN_MID,
                "spreadMax": SPREAD_MAX,
                "spreadMaxP90": SPREAD_MAX_P90,
                "bandQuoteCoverageMin": BAND_QUOTE_COVERAGE_MIN,
                "bandOiSumMin": BAND_OI_SUM_MIN,
                "bandVolSumMin": BAND_VOL_SUM_MIN,
                # legacy per-strike knobs kept for backwards-compatibility/visibility
                "oiMin": OI_MIN,
                "volMin": VOL_MIN,
            },
            explain=liq_explain,
        )
    )

    # ===== B1) Market/SPX dealer gamma =====
    try:
        spx_levels = compute_live_levels(
            client,
            underlying="SPX",
            symbols=("SPXW", "SPX", "SPY"),
            view="nearest",
            include_heatmap=True,
            heatmap_view="composite",
            heatmap_mode="slope",
        )
    except Exception as e:
        spx_levels = {"enabled": False, "notes": [f"compute_live_levels failed: {type(e).__name__}: {e}"]}

    dg = spx_levels.get("dealerGamma") if isinstance(spx_levels, dict) and isinstance(spx_levels.get("dealerGamma"), dict) else None
    if not dg or spx_levels.get("enabled") is False:
        checks.append(
            _mk_check(
                id="MACRO_GAMMA",
                label="SPX dealer gamma positive (not tiny)",
                state="MISSING",
                code="MACRO_GAMMA_DATA_MISSING",
                value={"enabled": spx_levels.get("enabled"), "symbolUsed": spx_levels.get("symbolUsed"), "expiry": spx_levels.get("expiry"), "notes": spx_levels.get("notes")},
                threshold={"requireSign": "positive", "minBucket": "medium"},
                explain="Market dealer gamma unavailable.",
            )
        )
    else:
        sign = str(dg.get("netGammaSign") or "").lower()
        mag_bucket = str(dg.get("magnitudeBucket") or "").lower() or _bucket_from_ratio(_to_float(dg.get("magnitudeRatio")))
        if sign != "positive":
            state, code, explain = "FAIL", "MACRO_GAMMA_NOT_POSITIVE", f"netGammaSign={sign or 'missing'}."
        elif mag_bucket not in ("medium", "high"):
            state, code, explain = "FAIL", "MACRO_GAMMA_TOO_SMALL", f"magnitudeBucket={mag_bucket or 'missing'} (needs >= medium)."
        else:
            state, code, explain = "PASS", None, "Positive market gamma with non-trivial magnitude."
        checks.append(
            _mk_check(
                id="MACRO_GAMMA",
                label="SPX dealer gamma positive (not tiny)",
                state=state,
                code=code,
                value={
                    "symbolUsed": spx_levels.get("symbolUsed"),
                    "expiry": spx_levels.get("expiry"),
                    "netGammaSign": sign,
                    "magnitudeBucket": mag_bucket,
                    "magnitudeRatio": dg.get("magnitudeRatio"),
                },
                threshold={"requireSign": "positive", "minBucket": "medium"},
                explain=explain,
            )
        )

    # ===== SPX dailies (for RV) with proxy fallback =====
    today = _now_et_date()
    bars = fetch_dailies_ohlc_range(client, ticker="SPX", start=today - dt.timedelta(days=90), end=today)
    underlying_used = "SPX"
    if not bars:
        bars = fetch_dailies_ohlc_range(client, ticker="SPY", start=today - dt.timedelta(days=90), end=today)
        if bars:
            underlying_used = "SPY"
            notes.append("SPX dailies unavailable; used SPY proxy for RV checks.")

    closes = [float(b.close) for b in (bars or []) if getattr(b, "close", None) is not None]
    rv5_now = _rv_annualized_from_closes(closes, 5) if closes else None
    rv20_now = _rv_annualized_from_closes(closes, 20) if closes else None
    rv5_prev = _rv_annualized_from_closes(closes[:-5], 5) if closes and len(closes) > 10 else None
    rv20_prev = _rv_annualized_from_closes(closes[:-5], 20) if closes and len(closes) > 30 else None
    rv5_jump = (rv5_now / rv5_prev) if (rv5_now is not None and rv5_prev is not None and rv5_prev > 1e-12) else None
    rv20_jump = (rv20_now / rv20_prev) if (rv20_now is not None and rv20_prev is not None and rv20_prev > 1e-12) else None

    # ===== B3) RV acceleration =====
    if rv5_jump is None or rv20_jump is None:
        checks.append(
            _mk_check(
                id="MACRO_RV_ACCEL",
                label="Index RV not accelerating (RV5 + RV20)",
                state="MISSING",
                code="MACRO_RV_DATA_MISSING",
                value={"underlyingUsed": underlying_used, "rv5Now": rv5_now, "rv5Prev5": rv5_prev, "rv5Jump": rv5_jump, "rv20Now": rv20_now, "rv20Prev5": rv20_prev, "rv20Jump": rv20_jump},
                threshold={"rv5JumpMax": RV5_JUMP_MAX, "rv20JumpMax": RV20_JUMP_MAX},
                explain="Insufficient index history to compute RV acceleration.",
            )
        )
    else:
        if float(rv5_jump) > float(RV5_JUMP_MAX):
            state, code, explain = "FAIL", "MACRO_RV5_ACCELERATING", f"RV5 jump {rv5_jump:.2f} > {RV5_JUMP_MAX:.2f}."
        elif float(rv20_jump) > float(RV20_JUMP_MAX):
            state, code, explain = "FAIL", "MACRO_RV20_ACCELERATING", f"RV20 jump {rv20_jump:.2f} > {RV20_JUMP_MAX:.2f}."
        else:
            state, code, explain = "PASS", None, "RV5 and RV20 not accelerating."
        checks.append(
            _mk_check(
                id="MACRO_RV_ACCEL",
                label="Index RV not accelerating (RV5 + RV20)",
                state=state,
                code=code,
                value={"underlyingUsed": underlying_used, "rv5Jump": round(float(rv5_jump), 4), "rv20Jump": round(float(rv20_jump), 4), "rv5Now": rv5_now, "rv20Now": rv20_now, "rv5Prev5": rv5_prev, "rv20Prev5": rv20_prev},
                threshold={"rv5JumpMax": RV5_JUMP_MAX, "rv20JumpMax": RV20_JUMP_MAX},
                explain=explain,
            )
        )

    # ===== Index sensitivity (corr/beta vs SPY) — tighten-only mode =====
    corr20 = None
    beta20 = None
    n_sens = 0
    try:
        start_s = today - dt.timedelta(days=60)
        t_bars = fetch_dailies_ohlc_range(client, ticker=t, start=start_s, end=today) or []
        spy_bars = fetch_dailies_ohlc_range(client, ticker="SPY", start=start_s, end=today) or []
        t_map = {b.trade_date: float(b.close) for b in t_bars if getattr(b, "trade_date", None) and getattr(b, "close", None) is not None}
        spy_map = {b.trade_date: float(b.close) for b in spy_bars if getattr(b, "trade_date", None) and getattr(b, "close", None) is not None}
        corr20, beta20, n_sens = _corr_beta_from_close_maps(a_by_date=t_map, b_by_date=spy_map, lookback_days=20)
    except Exception:
        corr20, beta20, n_sens = None, None, 0

    is_sensitive = False
    if corr20 is not None and abs(float(corr20)) >= float(CORR20_HIGH):
        is_sensitive = True
    if beta20 is not None and abs(float(beta20)) >= float(BETA20_HIGH):
        is_sensitive = True

    # Informational check (PASS always). If sensitivity is high, we tighten macro cutoffs below.
    checks.append(
        _mk_check(
            id="SN_INDEX_SENSITIVITY",
            label="Index sensitivity (corr/beta vs SPY)",
            state="PASS",
            code=None,
            value={
                "corr20": None if corr20 is None else round(float(corr20), 4),
                "beta20": None if beta20 is None else round(float(beta20), 4),
                "nReturns": int(n_sens),
                "sensitive": bool(is_sensitive),
            },
            threshold={"corrHigh": float(CORR20_HIGH), "betaHigh": float(BETA20_HIGH)},
            explain=("High index sensitivity: macro cutoffs tightened." if is_sensitive else "No index-sensitivity tightening applied."),
        )
    )

    # ===== B2) Gamma flip distance in EM (dynamic cutoff) =====
    flip_cutoff = float(FLIP_CUTOFF_BASE)
    if rv5_jump is not None and float(rv5_jump) > float(RV5_ACCEL_TIGHTEN):
        flip_cutoff = max(float(flip_cutoff), float(FLIP_CUTOFF_TIGHT))
    if is_sensitive:
        flip_cutoff = max(float(flip_cutoff), float(FLIP_CUTOFF_TIGHT))
    heat = spx_levels.get("gexHeatmap") if isinstance(spx_levels, dict) else None
    enabled_heat = bool(heat.get("enabled")) if isinstance(heat, dict) else False
    m = heat.get("metrics") if isinstance(heat, dict) and isinstance(heat.get("metrics"), dict) else {}
    down_em = _to_float(m.get("downsideDistanceEm"))
    up_em = _to_float(m.get("upsideDistanceEm"))
    min_flip = None
    if down_em is not None and up_em is not None:
        min_flip = min(float(down_em), float(up_em))
    elif down_em is not None:
        min_flip = float(down_em)
    elif up_em is not None:
        min_flip = float(up_em)

    if not enabled_heat or min_flip is None:
        checks.append(
            _mk_check(
                id="MACRO_GAMMA_FLIP",
                label="No SPX gamma flip within cutoff",
                state="MISSING",
                code="MACRO_GAMMA_FLIP_MISSING",
                value={"enabled": enabled_heat, "downsideEm": down_em, "upsideEm": up_em, "notes": list(heat.get("notes") or []) if isinstance(heat, dict) else []},
                threshold={"flipCutoffEm": flip_cutoff, "rv5AccelTightenTrigger": RV5_ACCEL_TIGHTEN, "tightCutoffEm": FLIP_CUTOFF_TIGHT, "baseCutoffEm": FLIP_CUTOFF_BASE},
                explain="Gamma flip distance unavailable (heatmap missing/disabled).",
            )
        )
    else:
        passed = float(min_flip) >= float(flip_cutoff)
        checks.append(
            _mk_check(
                id="MACRO_GAMMA_FLIP",
                label="No SPX gamma flip within cutoff",
                state="PASS" if passed else "FAIL",
                code=None if passed else "MACRO_TOO_CLOSE_TO_GAMMA_FLIP",
                value={"minFlipEm": round(float(min_flip), 4), "downsideEm": down_em, "upsideEm": up_em, "cutoffEm": flip_cutoff, "symbolUsed": spx_levels.get("symbolUsed") if isinstance(spx_levels, dict) else None},
                threshold={"flipCutoffEm": flip_cutoff},
                explain=("Flip distance OK." if passed else "Too close to gamma flip given current volatility regime."),
            )
        )

    # ===== B4) Forced flows (today + next 3 trading days, severity) =====
    wdays = _next_trading_days(_now_et_date(), FLOW_WINDOW_TD)
    if not wdays:
        checks.append(
            _mk_check(
                id="MACRO_FORCED_FLOWS",
                label="No index-level forced flows imminent",
                state="MISSING",
                code="MACRO_FORCED_FLOW_WINDOW_EMPTY",
                value={"windowTradingDays": [], "high": [], "med": []},
                threshold={"windowTradingDays": FLOW_WINDOW_TD},
                explain="Unable to build trading-day window.",
            )
        )
    else:
        start = wdays[0]
        end = wdays[-1]
        window_set = {d.isoformat() for d in wdays}

        local: List[dict] = []
        for d0, evs in (market_structure_events_by_date(start=start, end=end) or {}).items():
            if d0 in window_set:
                for e in (evs or []):
                    if isinstance(e, dict):
                        local.append({**e, "date": d0})
        for d0, evs in (opex_events_by_date(start=start, end=end) or {}).items():
            if d0 in window_set:
                for e in (evs or []):
                    if isinstance(e, dict):
                        local.append({**e, "date": d0})

        macro: Dict[str, List[dict]] = {}
        macro_notes: List[str] = []
        if benzinga_client is not None:
            try:
                macro, _, macro_notes = macro_events_by_date(bz=benzinga_client, start=start, end=end, importance_min=FLOW_IMPORTANCE_MED_MIN)
            except Exception as e:
                macro, macro_notes = {}, [f"macro_events_by_date failed: {type(e).__name__}: {e}"]

        high: List[dict] = []
        med: List[dict] = []

        # Manual overrides (always treated as HIGH severity).
        for s in FLOW_MANUAL_RANGES:
            s0 = str(s or "").strip()
            if not s0:
                continue
            try:
                if ":" in s0:
                    a, b = s0.split(":", 1)
                    d1 = dt.date.fromisoformat(a[:10])
                    d2 = dt.date.fromisoformat(b[:10])
                else:
                    d1 = dt.date.fromisoformat(s0[:10])
                    d2 = d1
            except Exception:
                continue
            cur = min(d1, d2)
            end0 = max(d1, d2)
            while cur <= end0:
                if cur.isoformat() in window_set:
                    high.append({"date": cur.isoformat(), "kind": "MANUAL", "title": "Manual forced-flow block", "source": "manual", "severity": "HIGH"})
                cur = cur + dt.timedelta(days=1)

        for e in local:
            kind = str(e.get("kind") or "").upper()
            sev = "HIGH" if kind in ("HOLIDAY", "EARLY_CLOSE", "OPEX") else "MED"
            rec = {"date": str(e.get("date") or "")[:10] or None, "kind": kind, "title": e.get("title"), "source": e.get("source") or "local", "severity": sev}
            (high if sev == "HIGH" else med).append(rec)

        for d0, evs in (macro or {}).items():
            if d0 not in window_set:
                continue
            for e in (evs or []):
                if not isinstance(e, dict):
                    continue
                imp = _to_int(e.get("importance")) or 0
                sev = "HIGH" if imp >= FLOW_IMPORTANCE_HIGH_MIN else "MED"
                rec = {"date": d0, "kind": str(e.get("kind") or "ECON"), "title": e.get("title"), "source": e.get("source") or "benzinga", "severity": sev, "importance": imp, "key": e.get("key")}
                (high if sev == "HIGH" else med).append(rec)

        if benzinga_client is None:
            state, code, explain = "MISSING", "MACRO_FORCED_FLOWS_DATA_MISSING", "Benzinga macro calendar unavailable."
        elif high:
            state, code, explain = "FAIL", "MACRO_FORCED_FLOWS_HIGH", f"{len(high)} HIGH-severity forced-flow event(s) in window."
        else:
            state, code, explain = "PASS", None, "No HIGH-severity forced flows in window."

        if med:
            warnings.append({"id": "MACRO_FORCED_FLOWS_MED", "label": "Forced flows (MED)", "events": med[:10]})

        checks.append(
            _mk_check(
                id="MACRO_FORCED_FLOWS",
                label="No index-level forced flows imminent",
                state=state,
                code=code,
                value={"windowTradingDays": [d.isoformat() for d in wdays], "high": high[:10], "med": med[:10], "notes": macro_notes},
                threshold={"windowTradingDays": FLOW_WINDOW_TD, "importanceHighMin": FLOW_IMPORTANCE_HIGH_MIN, "importanceMedMin": FLOW_IMPORTANCE_MED_MIN},
                explain=explain,
            )
        )

    passed_all = all(str(c.get("state") or "").upper() == "PASS" for c in checks)
    return {"status": "GO" if passed_all else "NO_GO", "passed": bool(passed_all), "checks": checks, "warnings": warnings, "notes": notes}


