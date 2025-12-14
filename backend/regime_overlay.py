from __future__ import annotations

import datetime as dt
import math
import statistics
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from cachetools import TTLCache

from backend.orats_client import OratsClient, OratsError


_cache = TTLCache(maxsize=512, ttl=2 * 60 * 60)  # 2 hours (current snapshot)
_cache_lock = threading.Lock()

_regime_asof_cache = TTLCache(maxsize=50_000, ttl=24 * 60 * 60)  # 24 hours
_regime_asof_lock = threading.Lock()


def clamp(lo: float, hi: float, x: float) -> float:
    return max(lo, min(hi, x))


def percentile_rank(x: float, xs: List[float]) -> Optional[float]:
    """Return percentile rank in [0,1] using <= comparison."""
    vals = [v for v in xs if v is not None and isinstance(v, (int, float)) and math.isfinite(v)]
    if not vals:
        return None
    c = sum(1 for v in vals if v <= x)
    return c / len(vals)


def _parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(str(s)[:10])


def _fmt_date(d: dt.date) -> str:
    return d.isoformat()


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


def _log_returns(closes: List[float]) -> List[float]:
    rets: List[float] = []
    for i in range(1, len(closes)):
        a = closes[i - 1]
        b = closes[i]
        if a and a > 0 and b and b > 0:
            rets.append(math.log(b / a))
    return rets


def _rv_annualized(log_returns: List[float], window: int = 20) -> Optional[float]:
    if len(log_returns) < window:
        return None
    w = log_returns[-window:]
    if len(w) < 2:
        return None
    # use sample stdev; this is fine for regime percentiles (relative comparisons)
    s = statistics.stdev(w)
    return s * math.sqrt(252.0)


def _rolling_rv20(log_returns: List[float], lookback: int = 252, window: int = 20) -> List[float]:
    # build last `lookback` rv values, including the most recent
    out: List[float] = []
    # log_returns length = n_closes-1
    start = max(window, len(log_returns) - lookback)
    for i in range(start, len(log_returns) + 1):
        w = log_returns[i - window : i]
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


def _quarter_key_from_date(d: dt.date) -> str:
    q = ((d.month - 1) // 3) + 1
    return f"Q{q}"


def _normalize_rec(rec: Optional[str]) -> str:
    if not rec:
        return "Avoid"
    r = str(rec)
    if r.startswith("Avoid"):
        return "Avoid"
    return r


def _base_wing_factor(rec: str) -> Optional[float]:
    r = _normalize_rec(rec)
    if r == "Tight":
        return 0.5
    if r == "Standard":
        return 1.0
    if r == "Wide":
        return 1.5
    return None


def _guidance_message(
    *,
    label: str,
    base_factor: Optional[float],
    tail_multiplier: float,
) -> Tuple[str, str]:
    gate = _trade_gate(label)
    if gate == "NO_TRADE":
        return gate, "No Trade (stress regime)"
    if base_factor is None:
        if gate == "CAUTION":
            return gate, "Caution (elevated regime)"
        return gate, "Standard"

    suggested = base_factor * tail_multiplier
    pct = (suggested / base_factor - 1.0) * 100.0
    # format like +34%
    sign = "+" if pct >= 0 else "−"
    pct_abs = abs(pct)
    if label == "Elevated":
        return gate, f"Widen wings {sign}{pct_abs:.0f}% (elevated regime)"
    if label == "Calm":
        return gate, f"Tighter allowed {sign}{pct_abs:.0f}% (calm regime)"
    return gate, f"Adjust wings {sign}{pct_abs:.0f}% ({label.lower()} regime)"


def _compute_regime_from_series(
    *,
    as_of_date: str,
    spy_dates: List[str],
    spy_closes: List[float],
    iv_by_date: Dict[str, float],
) -> Dict[str, Any]:
    """Compute regime metrics as-of a given date using only <= as_of_date data (no lookahead)."""
    if not spy_dates or not spy_closes:
        return {
            "label": "Normal",
            "tailMultiplier": 1.0,
            "tradeGate": "OK",
            "scores": {"marketStress": None, "singleNameVol": None, "correlationProxy": None, "regimeScore": None},
            "inputs": {
                "spyRv20": None,
                "spyRv20Percentile": None,
                "tickerIv30": None,
                "tickerIv30Percentile": None,
                "spyAbsRet5d": None,
                "spyAbsRet5dPercentile": None,
            },
        }

    # index up to as_of_date (last <=)
    idx = None
    for i in range(len(spy_dates) - 1, -1, -1):
        if spy_dates[i] <= as_of_date:
            idx = i
            break
    if idx is None or idx < 25:
        return {
            "label": "Normal",
            "tailMultiplier": 1.0,
            "tradeGate": "OK",
            "scores": {"marketStress": None, "singleNameVol": None, "correlationProxy": None, "regimeScore": None},
            "inputs": {
                "spyRv20": None,
                "spyRv20Percentile": None,
                "tickerIv30": None,
                "tickerIv30Percentile": None,
                "spyAbsRet5d": None,
                "spyAbsRet5dPercentile": None,
            },
        }

    closes_upto = spy_closes[: idx + 1]
    logrets = _log_returns(closes_upto)
    spy_rv20 = _rv_annualized(logrets, window=20)
    rv_hist = _rolling_rv20(logrets, lookback=252, window=20)
    spy_rv20_pct = percentile_rank(spy_rv20, rv_hist) if spy_rv20 is not None else None

    abs5_hist = _rolling_abs_ret_5d(closes_upto, lookback=252, window=5)
    spy_abs_5d = abs5_hist[-1] if abs5_hist else None
    spy_abs_5d_pct = percentile_rank(spy_abs_5d, abs5_hist) if spy_abs_5d is not None else None

    # IV series up to as_of_date
    iv_dates = sorted([d for d in iv_by_date.keys() if d <= as_of_date])
    iv_vals = [iv_by_date[d] for d in iv_dates if iv_by_date.get(d) is not None]
    ticker_iv = iv_vals[-1] if iv_vals else None
    iv_hist = iv_vals[-252:] if iv_vals else []
    ticker_iv_pct = percentile_rank(ticker_iv, iv_hist) if ticker_iv is not None else None

    ms = spy_rv20_pct if spy_rv20_pct is not None else 0.5
    sn = ticker_iv_pct if ticker_iv_pct is not None else 0.5
    cp = spy_abs_5d_pct if spy_abs_5d_pct is not None else 0.5
    regime_score = 0.50 * ms + 0.35 * sn + 0.15 * cp
    tail_multiplier = clamp(0.7, 2.0, 0.8 + 1.2 * regime_score)
    label = _label_from_tail_multiplier(tail_multiplier)
    gate = _trade_gate(label)

    return {
        "label": label,
        "tailMultiplier": round(tail_multiplier, 2),
        "tradeGate": gate,
        "scores": {
            "marketStress": None if spy_rv20_pct is None else round(spy_rv20_pct, 2),
            "singleNameVol": None if ticker_iv_pct is None else round(ticker_iv_pct, 2),
            "correlationProxy": None if spy_abs_5d_pct is None else round(spy_abs_5d_pct, 2),
            "regimeScore": round(regime_score, 2),
        },
        "inputs": {
            "spyRv20": None if spy_rv20 is None else round(spy_rv20, 2),
            "spyRv20Percentile": None if spy_rv20_pct is None else round(spy_rv20_pct, 2),
            "tickerIv30": None if ticker_iv is None else round(ticker_iv, 2),
            "tickerIv30Percentile": None if ticker_iv_pct is None else round(ticker_iv_pct, 2),
            "spyAbsRet5d": None if spy_abs_5d is None else round(spy_abs_5d, 4),
            "spyAbsRet5dPercentile": None if spy_abs_5d_pct is None else round(spy_abs_5d_pct, 2),
        },
    }


def compute_regime_as_of(
    client: OratsClient,
    ticker: str,
    *,
    as_of_date: str,
    spy_series: Tuple[List[str], List[float]] | None = None,
    iv_by_date: Dict[str, float] | None = None,
) -> Dict[str, Any]:
    key = (ticker.upper(), str(as_of_date)[:10])
    with _regime_asof_lock:
        cached = _regime_asof_cache.get(key)
    if cached is not None:
        return cached

    if spy_series is None or iv_by_date is None:
        # minimal fallback: compute with neutral overlay
        out = {
            "asOfDate": str(as_of_date)[:10],
            "label": "Normal",
            "tailMultiplier": 1.0,
            "tradeGate": "OK",
            "scores": {"marketStress": None, "singleNameVol": None, "correlationProxy": None, "regimeScore": None},
            "inputs": {
                "spyRv20": None,
                "spyRv20Percentile": None,
                "tickerIv30": None,
                "tickerIv30Percentile": None,
                "spyAbsRet5d": None,
                "spyAbsRet5dPercentile": None,
            },
        }
        with _regime_asof_lock:
            _regime_asof_cache[key] = out
        return out

    spy_dates, spy_closes = spy_series
    core = _compute_regime_from_series(as_of_date=str(as_of_date)[:10], spy_dates=spy_dates, spy_closes=spy_closes, iv_by_date=iv_by_date)
    out = {"asOfDate": str(as_of_date)[:10], **core}
    with _regime_asof_lock:
        _regime_asof_cache[key] = out
    return out


def _build_regime_validation(
    events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    # usable events only: implied+realized exist and breach is bool (matches existing "usable" definition)
    usable = [e for e in events if isinstance(e.get("breach"), bool) and e.get("impliedMovePct") is not None and e.get("realizedMovePct") is not None]
    by_gate = {"OK": [], "CAUTION": [], "NO_TRADE": []}
    for e in usable:
        gate = (e.get("regimeAtEvent") or {}).get("tradeGate") or "OK"
        if gate not in by_gate:
            gate = "OK"
        by_gate[gate].append(e)

    breaches_total = sum(1 for e in usable if e["breach"] is True)
    breaches_flagged = sum(1 for e in usable if e["breach"] is True and (e.get("regimeAtEvent") or {}).get("tradeGate") in ("CAUTION", "NO_TRADE"))
    breaches_missed = sum(1 for e in usable if e["breach"] is True and (e.get("regimeAtEvent") or {}).get("tradeGate") == "OK")
    flagged_nonbreaches = sum(1 for e in usable if e["breach"] is False and (e.get("regimeAtEvent") or {}).get("tradeGate") in ("CAUTION", "NO_TRADE"))

    def _rate(gate: str) -> Optional[float]:
        xs = by_gate[gate]
        if not xs:
            return None
        b = sum(1 for e in xs if e["breach"] is True)
        return round((b / len(xs)) * 100.0, 2)

    def _avg_overshoot(gate: str) -> Optional[float]:
        xs = [e for e in by_gate[gate] if e["breach"] is True and e.get("aboveBreachPct") is not None]
        if not xs:
            return None
        return round(sum(float(e["aboveBreachPct"]) for e in xs) / len(xs), 2)

    def _avg_ratio(gate: str) -> Optional[float]:
        vals = []
        for e in by_gate[gate]:
            imp = e.get("impliedMovePct")
            rea = e.get("realizedMovePct")
            if imp and imp > 0 and rea is not None:
                vals.append(float(rea) / float(imp))
        if not vals:
            return None
        return round(sum(vals) / len(vals), 2)

    out = {
        "eventsUsed": len(usable),
        "breaches": breaches_total,
        "breachesFlagged": breaches_flagged,
        "breachesMissed": breaches_missed,
        "flaggedNonBreaches": flagged_nonbreaches,
        "breachRateByGatePct": {g: _rate(g) for g in ("OK", "CAUTION", "NO_TRADE")},
        "avgOvershootByGatePct": {g: _avg_overshoot(g) for g in ("OK", "CAUTION", "NO_TRADE")},
        "avgRatioByGate": {g: _avg_ratio(g) for g in ("OK", "CAUTION", "NO_TRADE")},
    }
    return out


def _fetch_hist_dailies_range(
    client: OratsClient,
    ticker: str,
    from_date: str,
    to_date: str,
) -> List[dict]:
    # Try range mode (if supported). If not supported, this will throw and callers can fallback.
    get_fn = getattr(client, "get", None)
    if not callable(get_fn):
        raise OratsError("Client does not support range fetch via .get()")
    resp = get_fn(
        "/hist/dailies",
        {
            "ticker": ticker,
            "fromDate": from_date,
            "toDate": to_date,
            "fields": "ticker,tradeDate,clsPx,open",
        },
    )
    return resp.rows


def _fetch_hist_cores_range(
    client: OratsClient,
    ticker: str,
    from_date: str,
    to_date: str,
    fields: str,
) -> List[dict]:
    get_fn = getattr(client, "get", None)
    if not callable(get_fn):
        raise OratsError("Client does not support range fetch via .get()")
    resp = get_fn(
        "/hist/cores",
        {
            "ticker": ticker,
            "fromDate": from_date,
            "toDate": to_date,
            "fields": fields,
        },
    )
    return resp.rows


def compute_regime_overlay(
    client: OratsClient,
    ticker: str,
    *,
    quarters: Dict[str, Any],
    n: int,
    years: int,
    k: float,
) -> Dict[str, Any]:
    cache_key = ("regime", ticker.upper(), int(n), int(years), float(k))
    with _cache_lock:
        cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    # Pull SPY dailies for a large window (calendar buffer), then use trading-day series
    today = dt.date.today()
    from_date = _fmt_date(today - dt.timedelta(days=520))  # ~2y calendar to cover 252+20 trading days
    to_date = _fmt_date(today)

    spy_rows: List[dict] = []
    try:
        spy_rows = _fetch_hist_dailies_range(client, "SPY", from_date, to_date)
    except OratsError:
        # Fallback to sparse daily probing if range is not supported:
        # query backward calendar days until we collect enough trading days
        spy_rows = []
        cur = today
        attempts = 0
        # In production we expect range mode to work; keep fallback bounded.
        # (Also keeps unit tests fast with mocked clients.)
        while len(spy_rows) < 320 and attempts < 160:
            r = client.hist_dailies("SPY", _fmt_date(cur), "ticker,tradeDate,clsPx,open").rows
            if r:
                spy_rows.append(r[0])
            cur = cur - dt.timedelta(days=1)
            attempts += 1

    # If we cannot obtain SPY history, return a neutral regime overlay rather than failing the whole request.
    if not spy_rows:
        out = {
            "asOfDate": _fmt_date(today),
            "label": "Normal",
            "tailMultiplier": 1.0,
            "scores": {
                "marketStress": None,
                "singleNameVol": None,
                "correlationProxy": None,
                "regimeScore": None,
            },
            "inputs": {
                "spyRv20": None,
                "spyRv20Percentile": None,
                "tickerIv30": None,
                "tickerIv30Percentile": None,
                "spyAbsRet5d": None,
                "spyAbsRet5dPercentile": None,
            },
            "guidance": {"tradeGate": "OK", "message": "Standard"},
        }
        with _cache_lock:
            _cache[cache_key] = out
        return out

    spy_rows = sorted(spy_rows, key=lambda r: str(r.get("tradeDate") or "")[:10])
    spy_closes = [_to_float(r.get("clsPx")) for r in spy_rows]
    spy_trade_dates = [str(r.get("tradeDate"))[:10] for r in spy_rows if r.get("tradeDate")]
    spy_closes_f = [c for c in spy_closes if c is not None]

    as_of_date = spy_trade_dates[-1] if spy_trade_dates else _fmt_date(today)

    # Market stress: RV20 percentile
    spy_logrets = _log_returns(spy_closes_f)
    spy_rv20 = _rv_annualized(spy_logrets, window=20)
    rv_hist = _rolling_rv20(spy_logrets, lookback=252, window=20)
    spy_rv20_pct = percentile_rank(spy_rv20, rv_hist) if spy_rv20 is not None else None
    market_stress = spy_rv20_pct

    # Correlation proxy: abs 5d move percentile
    abs5_hist = _rolling_abs_ret_5d(spy_closes_f, lookback=252, window=5)
    spy_abs_5d = abs5_hist[-1] if abs5_hist else None
    spy_abs_5d_pct = percentile_rank(spy_abs_5d, abs5_hist) if spy_abs_5d is not None else None
    corr_proxy = spy_abs_5d_pct

    # Single-name IV30 proxy: choose best available 30d field from cores history
    iv_candidates = ["iv30", "iv30d", "iv30Day", "iv"]  # try iv30 first; fallback to iv if needed
    fields = "ticker,tradeDate," + ",".join(iv_candidates)

    core_rows: List[dict] = []
    try:
        core_rows = _fetch_hist_cores_range(client, ticker.upper(), from_date, to_date, fields=fields)
    except OratsError:
        core_rows = []
        cur = today
        attempts = 0
        while len(core_rows) < 320 and attempts < 160:
            r = client.hist_cores(ticker.upper(), _fmt_date(cur), fields).rows
            if r:
                core_rows.append(r[0])
            cur = cur - dt.timedelta(days=1)
            attempts += 1

    core_rows = sorted(core_rows, key=lambda r: str(r.get("tradeDate") or "")[:10])

    # Pick the candidate with the most usable historical points (prefer iv30)
    series_by_field: Dict[str, List[float]] = {f: [] for f in iv_candidates}
    for r in core_rows:
        for f in iv_candidates:
            v = _to_float(r.get(f))
            if v is not None:
                series_by_field[f].append(v)

    chosen = None
    best_n = -1
    for f in iv_candidates:
        npts = len(series_by_field[f])
        if npts > best_n:
            chosen = f
            best_n = npts
    # if iv30 exists at all, prefer it over iv
    if len(series_by_field.get("iv30", [])) >= max(10, best_n):
        chosen = "iv30"

    iv_series = series_by_field.get(chosen or "", [])
    ticker_iv30 = iv_series[-1] if iv_series else None
    iv_hist = iv_series[-252:] if iv_series else []
    ticker_iv30_pct = percentile_rank(ticker_iv30, iv_hist) if ticker_iv30 is not None else None
    single_name_vol = ticker_iv30_pct

    # Combine scores
    # if any score missing, default to 0.5 (neutral) rather than failing
    ms = market_stress if market_stress is not None else 0.5
    sn = single_name_vol if single_name_vol is not None else 0.5
    cp = corr_proxy if corr_proxy is not None else 0.5
    regime_score = 0.50 * ms + 0.35 * sn + 0.15 * cp
    tail_multiplier = clamp(0.7, 2.0, 0.8 + 1.2 * regime_score)
    label = _label_from_tail_multiplier(tail_multiplier)

    # Suggested adjustment using the current quarter's recommendation
    qk = _quarter_key_from_date(_parse_date(as_of_date))
    q_rec = quarters.get(qk, {}).get("recommendation")
    base_factor = _base_wing_factor(q_rec)
    gate, msg = _guidance_message(label=label, base_factor=base_factor, tail_multiplier=tail_multiplier)

    out = {
        "asOfDate": as_of_date,
        "label": label,
        "tailMultiplier": round(tail_multiplier, 2),
        "scores": {
            "marketStress": round(ms, 2),
            "singleNameVol": round(sn, 2),
            "correlationProxy": round(cp, 2),
            "regimeScore": round(regime_score, 2),
        },
        "inputs": {
            "spyRv20": None if spy_rv20 is None else round(spy_rv20, 2),
            "spyRv20Percentile": None if spy_rv20_pct is None else round(spy_rv20_pct, 2),
            "tickerIv30": None if ticker_iv30 is None else round(ticker_iv30, 2),
            "tickerIv30Percentile": None if ticker_iv30_pct is None else round(ticker_iv30_pct, 2),
            "spyAbsRet5d": None if spy_abs_5d is None else round(spy_abs_5d, 4),
            "spyAbsRet5dPercentile": None if spy_abs_5d_pct is None else round(spy_abs_5d_pct, 2),
        },
        "guidance": {
            "tradeGate": gate,
            "message": msg,
        },
    }

    with _cache_lock:
        _cache[cache_key] = out
    return out


def compute_regime_backtest_view(
    client: OratsClient,
    ticker: str,
    *,
    events: List[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Compute regimeAtEvent for each event (keyed by asOfDate) + a regimeValidation rollup.

    Uses pricingDateUsed as-of date and trailing windows ending at that date (no lookahead).
    """
    as_of_dates = sorted({str(e.get("pricingDateUsed") or "")[:10] for e in events if e.get("pricingDateUsed")})
    if not as_of_dates:
        return {}, _build_regime_validation(events)

    min_date = _parse_date(as_of_dates[0])
    max_date = _parse_date(as_of_dates[-1])
    from_date = _fmt_date(min_date - dt.timedelta(days=520))
    to_date = _fmt_date(max_date)

    # Batch fetch SPY closes
    spy_rows: List[dict] = []
    try:
        spy_rows = _fetch_hist_dailies_range(client, "SPY", from_date, to_date)
    except OratsError:
        spy_rows = []
        cur = max_date
        attempts = 0
        while len(spy_rows) < 420 and attempts < 700:
            r = client.hist_dailies("SPY", _fmt_date(cur), "ticker,tradeDate,clsPx,open").rows
            if r:
                spy_rows.append(r[0])
            cur = cur - dt.timedelta(days=1)
            attempts += 1

    spy_rows = sorted(spy_rows, key=lambda r: str(r.get("tradeDate") or "")[:10])
    spy_dates = [str(r.get("tradeDate"))[:10] for r in spy_rows if r.get("tradeDate")]
    spy_closes = []
    for r in spy_rows:
        c = _to_float(r.get("clsPx"))
        if c is not None:
            spy_closes.append(c)
        else:
            # keep alignment by skipping date too if close missing
            pass
    # Rebuild aligned dates to closes length (skip missing closes)
    aligned = [(str(r.get("tradeDate"))[:10], _to_float(r.get("clsPx"))) for r in spy_rows]
    spy_dates = [d for (d, c) in aligned if c is not None]
    spy_closes = [c for (d, c) in aligned if c is not None]

    # Batch fetch ticker IV series (try multiple field names; pick best by non-null count)
    iv_candidates = ["iv30", "iv30d", "iv30Day", "iv"]
    fields = "ticker,tradeDate," + ",".join(iv_candidates)

    core_rows: List[dict] = []
    try:
        core_rows = _fetch_hist_cores_range(client, ticker.upper(), from_date, to_date, fields=fields)
    except OratsError:
        core_rows = []
        cur = max_date
        attempts = 0
        while len(core_rows) < 420 and attempts < 700:
            r = client.hist_cores(ticker.upper(), _fmt_date(cur), fields).rows
            if r:
                core_rows.append(r[0])
            cur = cur - dt.timedelta(days=1)
            attempts += 1

    core_rows = sorted(core_rows, key=lambda r: str(r.get("tradeDate") or "")[:10])
    counts = {f: 0 for f in iv_candidates}
    for r in core_rows:
        for f in iv_candidates:
            if _to_float(r.get(f)) is not None:
                counts[f] += 1
    chosen = max(iv_candidates, key=lambda f: counts[f])
    if counts.get("iv30", 0) > 0:
        chosen = "iv30"

    iv_by_date: Dict[str, float] = {}
    for r in core_rows:
        d = str(r.get("tradeDate") or "")[:10]
        v = _to_float(r.get(chosen))
        if d and v is not None:
            iv_by_date[d] = v

    # Compute per-date regime objects with caching
    per_date: Dict[str, Dict[str, Any]] = {}
    spy_series = (spy_dates, spy_closes)
    for d in as_of_dates:
        per_date[d] = compute_regime_as_of(client, ticker, as_of_date=d, spy_series=spy_series, iv_by_date=iv_by_date)

    # Attach per-event lightweight regimeAtEvent
    for e in events:
        d = e.get("pricingDateUsed")
        if not d:
            e["regimeAtEvent"] = None
            continue
        ro = per_date.get(str(d)[:10])
        if not ro:
            e["regimeAtEvent"] = None
            continue
        e["regimeAtEvent"] = {
            "label": ro.get("label"),
            "tailMultiplier": ro.get("tailMultiplier"),
            "tradeGate": ro.get("tradeGate"),
            "scores": ro.get("scores"),
            "inputs": ro.get("inputs"),
        }

    return per_date, _build_regime_validation(events)


