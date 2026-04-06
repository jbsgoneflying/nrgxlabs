from __future__ import annotations

import datetime as dt
import logging
import math
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from cachetools import TTLCache

from backend.benzinga_client import BenzingaClient
from backend.config import FeatureFlags
from backend.orats_client import OratsClient
from backend.spx_ic.ohlc import (
    DailyOHLC,
    fetch_close_map_range,
)
from backend.spx_ic.utils import (
    _cache_get,
    _cache_set,
    _fmt_date,
    _parse_date,
)

LOG = logging.getLogger("spx_ic.regime")

_macro_cache = TTLCache(maxsize=5_000, ttl=6 * 60 * 60)
_macro_lock = threading.Lock()


# ---- Pure statistical helpers ----

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


# ---- Volatility estimators ----

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
    """Parkinson volatility estimator (uses high/low only), annualized."""
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
    return math.sqrt(max(0.0, sigma2) * 252.0)


def _yang_zhang_vol(bars: List[DailyOHLC], window: int = 20) -> Optional[float]:
    """Yang-Zhang volatility estimator (uses open/high/low/close), annualized."""
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


# ---- Label / gate / z-score helpers ----

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


# ---- Calendar / seasonality helpers ----

def _is_summer(d: dt.date) -> bool:
    return d.month in (6, 7, 8)


def _is_opex_week(d: dt.date) -> bool:
    """OpEx week: week containing the 3rd Friday of the month."""
    first = dt.date(d.year, d.month, 1)
    ff = first
    while ff.weekday() != 4:
        ff += dt.timedelta(days=1)
    third_friday = ff + dt.timedelta(days=14)
    mon = third_friday - dt.timedelta(days=4)
    fri = third_friday
    return mon <= d <= fri


def _macro_classify_name(name: str) -> Optional[str]:
    n = str(name or "").lower()
    if not n:
        return None
    if "cpi" in n or "consumer price" in n:
        return "CPI"
    if "fomc" in n and "minutes" in n:
        return "FOMC_MINUTES"
    if "fomc" in n or "fed rate" in n or "interest rate decision" in n:
        return "FOMC"
    if "nonfarm" in n or "nfp" in n or "payroll" in n:
        return "NFP"
    if "ppi" in n or "producer price" in n:
        return "PPI"
    if "pmi" in n or "ism" in n:
        return "PMI_ISM"
    if "gdp" in n or "gross domestic" in n:
        return "GDP"
    if "pce" in n or "personal consumption" in n:
        return "PCE"
    if "retail" in n and "sales" in n:
        return "RETAIL_SALES"
    if "jobless" in n or "claims" in n:
        return "JOBLESS_CLAIMS"
    if "refunding" in n:
        return "REFUNDING"
    if "auction" in n and ("treasury" in n or "note" in n or "bond" in n or "bill" in n):
        return "TREASURY_AUCTION"
    return None


# ---- Sector / macro analysis ----

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
    out: Dict[str, float] = {}
    if len(dates) < 2 or not sector_tickers:
        return out
    try:
        start = _parse_date(dates[0])
        end = _parse_date(dates[-1])
    except Exception:
        return out

    closes_by_ticker: Dict[str, Dict[str, float]] = {}

    def fetch_sector(ticker: str) -> Tuple[str, Dict[str, float]]:
        return (ticker, fetch_close_map_range(client, ticker=ticker, start=start, end=end))

    with ThreadPoolExecutor(max_workers=min(8, len(sector_tickers))) as executor:
        results = executor.map(fetch_sector, sector_tickers)
        for ticker, closes in results:
            closes_by_ticker[ticker] = closes

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
        "flags": {
            "CPI": False, "FOMC": False, "FOMC_MINUTES": False, "NFP": False,
            "OPEX": False, "REFUNDING": False, "GDP": False, "PCE": False,
            "PPI": False, "PMI_ISM": False, "RETAIL_SALES": False,
            "JOBLESS_CLAIMS": False, "TREASURY_AUCTION": False,
        },
        "multiplier": 1.0,
        "components": {
            "CPI": 0.0, "FOMC": 0.0, "FOMC_MINUTES": 0.0, "NFP": 0.0,
            "OPEX": 0.0, "REFUNDING": 0.0, "GDP": 0.0, "PCE": 0.0,
            "PPI": 0.0, "PMI_ISM": 0.0, "RETAIL_SALES": 0.0,
            "JOBLESS_CLAIMS": 0.0, "TREASURY_AUCTION": 0.0, "OTHER": 0.0,
        },
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
                try:
                    d = _parse_date(date)
                    days = abs((d - as_of).days)
                except Exception:
                    days = None
                if days is not None:
                    decay = math.exp(-float(flags.ENGINE2_MACRO_LAMBDA) * float(days))
                    _base_map = {
                        "CPI": flags.ENGINE2_MACRO_BASE_CPI,
                        "FOMC": flags.ENGINE2_MACRO_BASE_FOMC,
                        "FOMC_MINUTES": flags.ENGINE2_MACRO_BASE_FOMC_MINUTES,
                        "NFP": flags.ENGINE2_MACRO_BASE_NFP,
                        "REFUNDING": flags.ENGINE2_MACRO_BASE_REFUNDING,
                        "GDP": flags.ENGINE2_MACRO_BASE_GDP,
                        "PCE": flags.ENGINE2_MACRO_BASE_PCE,
                        "PPI": flags.ENGINE2_MACRO_BASE_PPI,
                        "PMI_ISM": flags.ENGINE2_MACRO_BASE_PMI_ISM,
                        "RETAIL_SALES": flags.ENGINE2_MACRO_BASE_RETAIL_SALES,
                        "JOBLESS_CLAIMS": flags.ENGINE2_MACRO_BASE_JOBLESS_CLAIMS,
                        "TREASURY_AUCTION": flags.ENGINE2_MACRO_BASE_TREASURY_AUCTION,
                    }
                    base = float(_base_map.get(k, flags.ENGINE2_MACRO_BASE_OTHER))
                    scored.append((k or "OTHER", base * decay, date, name))
        hi.sort(key=lambda x: (-x[0], x[1]))
        out["highImpactUS"]["count"] = int(len(hi))
        out["highImpactUS"]["top"] = [f"{d} {n}".strip() for (_, d, n) in hi[:6] if (d or n)]

        out["flags"]["OPEX"] = bool(_is_opex_week(end))
        if out["flags"]["OPEX"]:
            out["components"]["OPEX"] = float(flags.ENGINE2_MACRO_BASE_OPEX)

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


# ---- Main regime scorer ----

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
    ret5 = None
    ret5_hist = []
    if len(closes) >= 6:
        ret5 = (closes[-1] / closes[-6] - 1.0) * 100.0
        start = max(5, len(closes) - 252)
        for i in range(start, len(closes)):
            a = closes[i - 5]
            b = closes[i]
            if a > 0 and b > 0:
                ret5_hist.append((b / a - 1.0) * 100.0)
    z5 = _zscore(float(ret5 or 0.0), ret5_hist) if ret5 is not None else None

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

    iv30 = None
    iv7 = None
    iv30_pct = None
    term_slope = None
    vv = None
    if iv_weekly_sample and asof in iv_weekly_sample:
        iv7 = iv_weekly_sample[asof].get("iv7")
        iv30 = iv_weekly_sample[asof].get("iv30")
        term_slope = None if (iv7 is None or iv30 is None) else float(iv7) - float(iv30)
        prev_dates = sorted([d for d in iv_weekly_sample.keys() if d < asof])
        if prev_dates and iv7 is not None:
            p = iv_weekly_sample[prev_dates[-1]].get("iv7")
            if p is not None and float(iv7) > 0:
                vv = abs(float(iv7) - float(p)) / float(iv7)
        iv30_hist = [v.get("iv30") for d, v in iv_weekly_sample.items() if d <= asof and v.get("iv30") is not None]
        iv30_pct = percentile_rank(float(iv30), [float(x) for x in iv30_hist if x is not None]) if (iv30 is not None and iv30_hist) else None

    iv_risk = _pctile_or_default(iv30, [float(x.get("iv30")) for x in (iv_weekly_sample or {}).values() if x.get("iv30") is not None], default=0.5) if iv30 is not None else 0.5
    term_risk = clamp(0.0, 1.0, (float(term_slope) + 2.0) / 6.0) if term_slope is not None else 0.5
    vv_risk = clamp(0.0, 1.0, (float(vv) - 0.02) / 0.10) if vv is not None else 0.5
    vol_risk = clamp(0.0, 1.0, 0.45 * rv20_pct + 0.25 * _risk01_from_ratio(rv_ratio, lo=0.8, hi=1.6) + 0.20 * iv_risk + 0.10 * max(term_risk, vv_risk))

    # ---- Stress block ----
    em1d = None
    if iv7 is not None:
        em1d = float(iv7) * math.sqrt(1.0 / 365.0)
    elif rv20 is not None:
        em1d = float(rv20) * 100.0 * math.sqrt(1.0 / 252.0)
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
