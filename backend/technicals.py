from __future__ import annotations

import base64
import datetime as dt
import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from backend.orats_client import OratsClient
from backend.market_hours import is_us_equity_market_open

_LOG = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class DailyBar:
    trade_date: str
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[float]
    vwap: Optional[float]


def fetch_daily_bars_range(
    client: Any = None,
    *,
    ticker: str,
    start: dt.date,
    end: dt.date,
) -> List[DailyBar]:
    """Fetch daily OHLCV bars for *ticker* over [start, end].

    Uses EODHD via ``PriceService`` for clean, split-adjusted OHLCV data.
    The *client* parameter is accepted for backward compatibility but ignored
    when the EODHD-backed price service is available.
    """
    if end < start:
        return []

    # Primary path: EODHD via PriceService
    from backend.price_service import get_price_service
    ps = get_price_service()
    if ps is not None:
        return ps.fetch_daily_bars(ticker, start, end)

    # Fallback: ORATS hist_dailies (if EODHD not configured)
    _LOG.debug("PriceService unavailable; falling back to ORATS hist_dailies for %s", ticker)
    if client is None:
        return []
    td = f"{_fmt_date(start)},{_fmt_date(end)}"
    fields = ",".join([
        "ticker", "tradeDate", "open", "opPx", "high", "hiPx",
        "low", "loPx", "close", "clsPx", "volume", "vol", "vwap",
    ])
    try:
        resp = client.hist_dailies(ticker=ticker, trade_date=td, fields=fields)
        rows = resp.rows or []
    except Exception:
        rows = []
    out: List[DailyBar] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        d0 = str(r.get("tradeDate") or r.get("trade_date") or "")[:10]
        if not d0:
            continue
        o = _to_float(r.get("open") or r.get("opPx") or r.get("op_px"))
        h = _to_float(r.get("high") or r.get("hiPx") or r.get("hi") or r.get("hPx"))
        l = _to_float(r.get("low") or r.get("loPx") or r.get("lo") or r.get("lPx"))
        c = _to_float(r.get("close") or r.get("clsPx") or r.get("cls_px"))
        if c is None or c <= 0:
            continue
        vol = _to_float(r.get("volume") or r.get("vol") or r.get("totalVolume"))
        vwap = _to_float(r.get("vwap"))
        out.append(DailyBar(trade_date=d0, open=o, high=h, low=l, close=c, volume=vol, vwap=vwap))
    out.sort(key=lambda b: b.trade_date)
    return out


def fetch_live_price_optional(client: Any = None, *, ticker: str) -> Optional[float]:
    ctx = fetch_live_price_context_optional(client, ticker=ticker)
    px = _to_float(ctx.get("price")) if isinstance(ctx, dict) else None
    return px if (px is not None and px > 0) else None


def _extract_orats_live_summary_price(client: Any, ticker: str) -> Optional[float]:
    try:
        if client is None or not callable(getattr(client, "live_summaries", None)):
            return None
        resp = client.live_summaries(ticker=str(ticker).upper())
        rows = resp.rows or []
        row = next((x for x in rows if isinstance(x, dict)), None)
        if not row:
            return None
        spot = _to_float(row.get("spotPrice"))
        px = spot if (spot is not None and spot > 0) else _to_float(row.get("stockPrice"))
        return px if (px is not None and px > 0) else None
    except Exception:
        return None


def fetch_live_price_context_optional(client: Any = None, *, ticker: str) -> Dict[str, Any]:
    """Session-aware spot resolver for Engine2/technicals overlays.

    Market open: fetches BOTH ORATS live_summaries and EODHD live quote,
    then cross-checks. If they diverge by more than 0.3%, prefer EODHD
    (ORATS delayed data is 15-20 min behind for index spot). If only one
    is available, use it. Market closed: latest close only.
    """
    tk = str(ticker or "").strip().upper()
    market_open = is_us_equity_market_open()
    from backend.price_service import get_price_service

    ps = get_price_service()
    if market_open:
        px_orats = _extract_orats_live_summary_price(client, tk)

        px_eodhd = None
        if ps is not None and callable(getattr(ps, "fetch_intraday_price", None)):
            try:
                px_eodhd = ps.fetch_intraday_price(tk)
                if px_eodhd is not None and float(px_eodhd) <= 0:
                    px_eodhd = None
                elif px_eodhd is not None:
                    px_eodhd = float(px_eodhd)
            except Exception:
                px_eodhd = None

        if px_orats is not None and px_eodhd is not None:
            divergence = abs(px_orats - px_eodhd) / max(px_orats, px_eodhd)
            if divergence > 0.003:
                return {
                    "price": px_eodhd,
                    "source": "eodhd_live_quote",
                    "mode": "open_live_crosscheck",
                    "marketOpen": True,
                    "_oratsPx": round(px_orats, 2),
                    "_divergencePct": round(divergence * 100, 2),
                }
            return {"price": float(px_orats), "source": "orats_live_summaries", "mode": "open_live", "marketOpen": True}

        if px_eodhd is not None:
            return {"price": px_eodhd, "source": "eodhd_live_quote", "mode": "open_live", "marketOpen": True}
        if px_orats is not None:
            return {"price": float(px_orats), "source": "orats_live_summaries", "mode": "open_live", "marketOpen": True}

        px_close = None
        if ps is not None:
            px_close = ps.fetch_live_price(tk)
        return {
            "price": (float(px_close) if (px_close is not None and float(px_close) > 0) else None),
            "source": "latest_close",
            "mode": "open_close_fallback",
            "marketOpen": True,
        }

    px_close = None
    if ps is not None:
        px_close = ps.fetch_live_price(tk)
    return {
        "price": (float(px_close) if (px_close is not None and float(px_close) > 0) else None),
        "source": "latest_close",
        "mode": "closed_close",
        "marketOpen": False,
    }


def _ema_series(closes: List[float], span: int) -> List[float]:
    if not closes:
        return []
    s = int(span)
    if s <= 1:
        return list(closes)
    a = 2.0 / (float(s) + 1.0)
    out = [float(closes[0])]
    for i in range(1, len(closes)):
        out.append(a * float(closes[i]) + (1.0 - a) * out[-1])
    return out


def compute_ema_levels(closes: List[float], spans: List[int]) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    for s in spans:
        key = f"ema{int(s)}"
        if len(closes) < max(2, int(s)):
            out[key] = None
            continue
        series = _ema_series(closes, int(s))
        out[key] = float(series[-1]) if series else None
    return out


def _sma_series(xs: List[float], window: int) -> List[Optional[float]]:
    """
    Rolling simple moving average.
    Returns a list aligned to xs, with None for indices < window-1.
    """
    w = int(window)
    if w <= 0:
        return [None for _ in xs]
    out: List[Optional[float]] = []
    s = 0.0
    for i, x in enumerate(xs):
        s += float(x)
        if i >= w:
            s -= float(xs[i - w])
        if i + 1 < w:
            out.append(None)
        else:
            out.append(s / float(w))
    return out


def _rolling_std_series(xs: List[float], window: int) -> List[Optional[float]]:
    """
    Rolling population std-dev (ddof=0) aligned to xs.
    Returns None until the window is full.
    """
    w = int(window)
    if w <= 1:
        return [0.0 for _ in xs]
    out: List[Optional[float]] = []
    for i in range(len(xs)):
        if i + 1 < w:
            out.append(None)
            continue
        seg = xs[i + 1 - w : i + 1]
        mu = sum(seg) / float(w)
        var = sum((float(v) - mu) ** 2 for v in seg) / float(w)
        out.append(math.sqrt(var))
    return out


def _percentile_rank(x: float, xs: List[float]) -> Optional[float]:
    vals = [float(v) for v in (xs or []) if isinstance(v, (int, float)) and math.isfinite(float(v))]
    if not vals:
        return None
    c = sum(1 for v in vals if v <= float(x))
    return c / float(len(vals))


def compute_rsi_series(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """
    Wilder RSI series aligned to closes.
    Returns None for indices where RSI cannot be computed.
    """
    p = int(period)
    if p <= 1 or len(closes) < p + 1:
        return [None for _ in closes]
    gains: List[float] = [0.0]
    losses: List[float] = [0.0]
    for i in range(1, len(closes)):
        ch = float(closes[i]) - float(closes[i - 1])
        gains.append(max(0.0, ch))
        losses.append(max(0.0, -ch))

    out: List[Optional[float]] = [None for _ in closes]
    # Initial averages over first p periods (using gains/losses indices 1..p)
    avg_gain = sum(gains[1 : p + 1]) / float(p)
    avg_loss = sum(losses[1 : p + 1]) / float(p)

    def _rsi_from_avgs(ag: float, al: float) -> float:
        if al <= 0:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    out[p] = _rsi_from_avgs(avg_gain, avg_loss)
    for i in range(p + 1, len(closes)):
        avg_gain = (avg_gain * (p - 1) + gains[i]) / float(p)
        avg_loss = (avg_loss * (p - 1) + losses[i]) / float(p)
        out[i] = _rsi_from_avgs(avg_gain, avg_loss)
    return out


def compute_macd_series(
    closes: List[float],
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Dict[str, List[Optional[float]]]:
    """
    MACD series aligned to closes. Uses EMA for fast/slow and EMA for signal line.
    Returns dict with macd/signal/hist lists (Optional[float]).
    """
    f = int(fast)
    s = int(slow)
    sig = int(signal)
    if not closes or len(closes) < max(f, s, sig) + 2:
        n = len(closes)
        return {"macd": [None for _ in range(n)], "signal": [None for _ in range(n)], "hist": [None for _ in range(n)]}

    ema_f = _ema_series(closes, f)
    ema_s = _ema_series(closes, s)
    macd_raw = [float(ema_f[i]) - float(ema_s[i]) for i in range(len(closes))]
    sig_raw = _ema_series(macd_raw, sig)
    hist_raw = [float(macd_raw[i]) - float(sig_raw[i]) for i in range(len(closes))]

    # To avoid false confidence early, mask the first max(s, sig) points as None.
    warmup = max(s, sig)
    macd: List[Optional[float]] = []
    sigl: List[Optional[float]] = []
    hist: List[Optional[float]] = []
    for i in range(len(closes)):
        if i < warmup:
            macd.append(None)
            sigl.append(None)
            hist.append(None)
        else:
            macd.append(float(macd_raw[i]))
            sigl.append(float(sig_raw[i]))
            hist.append(float(hist_raw[i]))
    return {"macd": macd, "signal": sigl, "hist": hist}


def compute_bollinger_series(
    closes: List[float],
    *,
    period: int = 20,
    stdev: float = 2.0,
) -> Dict[str, List[Optional[float]]]:
    """
    Bollinger series aligned to closes.
    Returns dict with mid/upper/lower/bandwidthPct/percentB (Optional[float]).
    """
    p = int(period)
    k = float(stdev)
    n = len(closes)
    if n < p:
        return {
            "mid": [None for _ in range(n)],
            "upper": [None for _ in range(n)],
            "lower": [None for _ in range(n)],
            "bandwidthPct": [None for _ in range(n)],
            "percentB": [None for _ in range(n)],
        }
    mid = _sma_series(closes, p)
    sd = _rolling_std_series(closes, p)
    upper: List[Optional[float]] = []
    lower: List[Optional[float]] = []
    bw: List[Optional[float]] = []
    pb: List[Optional[float]] = []
    for i in range(n):
        m = mid[i]
        s0 = sd[i]
        if m is None or s0 is None or not math.isfinite(float(m)) or not math.isfinite(float(s0)):
            upper.append(None)
            lower.append(None)
            bw.append(None)
            pb.append(None)
            continue
        up = float(m) + k * float(s0)
        lo = float(m) - k * float(s0)
        upper.append(up)
        lower.append(lo)
        bw.append(None if float(m) == 0 else ((up - lo) / float(m)) * 100.0)
        denom = (up - lo)
        pb.append(None if denom <= 0 else (float(closes[i]) - lo) / denom)
    return {"mid": mid, "upper": upper, "lower": lower, "bandwidthPct": bw, "percentB": pb}


def detect_candlestick_patterns(bars: List[DailyBar]) -> Dict[str, Any]:
    """
    Detect a small, high-signal set of candlestick patterns on the *most recent* bar(s).
    Daily-only. Best-effort: requires OHLC.
    """
    if not bars or len(bars) < 2:
        return {"enabled": False, "patterns": [], "notes": ["Insufficient bars for candle patterns."]}

    b1 = bars[-1]
    b0 = bars[-2]
    if any(v is None for v in (b1.open, b1.high, b1.low, b1.close, b0.open, b0.high, b0.low, b0.close)):
        return {"enabled": False, "patterns": [], "notes": ["Missing OHLC; candle patterns unavailable."]}

    o1, h1, l1, c1 = float(b1.open), float(b1.high), float(b1.low), float(b1.close)
    o0, h0, l0, c0 = float(b0.open), float(b0.high), float(b0.low), float(b0.close)
    r1 = max(1e-9, h1 - l1)
    body1 = abs(c1 - o1)
    up_wick1 = h1 - max(o1, c1)
    lo_wick1 = min(o1, c1) - l1
    r0 = max(1e-9, h0 - l0)
    body0 = abs(c0 - o0)

    is_green1 = c1 > o1
    is_red1 = c1 < o1
    is_green0 = c0 > o0
    is_red0 = c0 < o0

    patterns: List[Dict[str, Any]] = []

    # Doji: tiny body vs range.
    if (body1 / r1) <= 0.10:
        patterns.append(
            {
                "name": "doji",
                "direction": "neutral",
                "strength": round(max(0.0, 1.0 - (body1 / r1) / 0.10), 3),
                "notes": ["Small real body vs range; indicates indecision."],
            }
        )

    # Hammer / Shooting Star (shape-based; trend/context handled in narrative layer).
    if lo_wick1 >= 2.0 * max(1e-9, body1) and up_wick1 <= 0.50 * max(1e-9, body1):
        patterns.append(
            {
                "name": "hammer_like",
                "direction": "bullish",
                "strength": round(min(1.0, lo_wick1 / max(1e-9, r1)), 3),
                "notes": ["Long lower wick with small body near the highs; can signal demand stepping in."],
            }
        )
    if up_wick1 >= 2.0 * max(1e-9, body1) and lo_wick1 <= 0.50 * max(1e-9, body1):
        patterns.append(
            {
                "name": "shooting_star_like",
                "direction": "bearish",
                "strength": round(min(1.0, up_wick1 / max(1e-9, r1)), 3),
                "notes": ["Long upper wick with small body near the lows; can signal supply stepping in."],
            }
        )

    # Engulfing (2-bar body engulf).
    if is_red0 and is_green1 and (o1 <= c0) and (c1 >= o0):
        patterns.append(
            {
                "name": "bullish_engulfing",
                "direction": "bullish",
                "strength": round(min(1.0, (abs(c1 - o1) / max(1e-9, abs(c0 - o0)))), 3),
                "notes": ["Bullish body engulfs prior bearish body; reversal potential."],
            }
        )
    if is_green0 and is_red1 and (o1 >= c0) and (c1 <= o0):
        patterns.append(
            {
                "name": "bearish_engulfing",
                "direction": "bearish",
                "strength": round(min(1.0, (abs(c1 - o1) / max(1e-9, abs(c0 - o0)))), 3),
                "notes": ["Bearish body engulfs prior bullish body; reversal potential."],
            }
        )

    # Harami (2-bar inside body).
    if body0 > 0 and body1 > 0:
        hi_body0 = max(o0, c0)
        lo_body0 = min(o0, c0)
        hi_body1 = max(o1, c1)
        lo_body1 = min(o1, c1)
        inside_body = (hi_body1 <= hi_body0) and (lo_body1 >= lo_body0)
        if inside_body and is_red0 and is_green1 and (body1 / max(1e-9, body0)) <= 0.7:
            patterns.append(
                {
                    "name": "bullish_harami",
                    "direction": "bullish",
                    "strength": round(min(1.0, (body0 / max(1e-9, r0))), 3),
                    "notes": ["Small bullish body inside prior bearish body; can signal seller exhaustion."],
                }
            )
        if inside_body and is_green0 and is_red1 and (body1 / max(1e-9, body0)) <= 0.7:
            patterns.append(
                {
                    "name": "bearish_harami",
                    "direction": "bearish",
                    "strength": round(min(1.0, (body0 / max(1e-9, r0))), 3),
                    "notes": ["Small bearish body inside prior bullish body; can signal buyer exhaustion."],
                }
            )

    # Piercing line / dark cloud cover (simplified, 2-bar).
    mid0 = (o0 + c0) / 2.0
    if is_red0 and is_green1 and (o1 < c0) and (c1 > mid0) and (c1 < o0):
        patterns.append(
            {
                "name": "piercing_line",
                "direction": "bullish",
                "strength": round(min(1.0, (c1 - mid0) / max(1e-9, (o0 - mid0))), 3),
                "notes": ["Bullish reversal attempt: closes above midpoint of prior red body after a weak open."],
            }
        )
    if is_green0 and is_red1 and (o1 > c0) and (c1 < mid0) and (c1 > o0):
        patterns.append(
            {
                "name": "dark_cloud_cover",
                "direction": "bearish",
                "strength": round(min(1.0, (mid0 - c1) / max(1e-9, (mid0 - o0))), 3),
                "notes": ["Bearish reversal attempt: closes below midpoint of prior green body after a strong open."],
            }
        )

    # Marubozu-like (dominant body, small wicks).
    if (body1 / r1) >= 0.90 and (up_wick1 / r1) <= 0.05 and (lo_wick1 / r1) <= 0.05:
        patterns.append(
            {
                "name": "bullish_marubozu" if is_green1 else "bearish_marubozu" if is_red1 else "marubozu_like",
                "direction": "bullish" if is_green1 else "bearish" if is_red1 else "neutral",
                "strength": round(min(1.0, body1 / r1), 3),
                "notes": ["Large real body with minimal wicks; indicates strong directional conviction for the day."],
            }
        )

    # Inside / outside day
    if h1 < h0 and l1 > l0:
        patterns.append(
            {
                "name": "inside_day",
                "direction": "neutral",
                "strength": round(min(1.0, (h0 - l0) / max(1e-9, r1)) - 1.0, 3),
                "notes": ["Range contraction vs prior day; often precedes expansion."],
            }
        )
    if h1 > h0 and l1 < l0:
        patterns.append(
            {
                "name": "outside_day",
                "direction": "neutral",
                "strength": round(min(1.0, (r1 / max(1e-9, (h0 - l0))) - 1.0), 3),
                "notes": ["Range expansion vs prior day; indicates higher volatility."],
            }
        )

    return {
        "enabled": True,
        "asOfDate": str(b1.trade_date)[:10],
        "patterns": patterns,
        "notes": ["Candlestick patterns are detected mechanically on daily OHLC; context is applied in the narrative layer."],
    }


def detect_red_dog_reversal(bars: List[DailyBar]) -> Dict[str, Any]:
    """
    Default 'Red Dog' (failed-break) definition (2-bar):
    - Bullish: today's low < prior low AND today's close > prior low
    - Bearish: today's high > prior high AND today's close < prior high
    Optional strength filter is included as a field; we do not require it to flag the pattern.
    """
    if not bars or len(bars) < 2:
        return {"enabled": False, "bullish": False, "bearish": False, "notes": ["Insufficient bars for Red Dog."]}
    b1 = bars[-1]
    b0 = bars[-2]
    if any(v is None for v in (b1.high, b1.low, b1.close, b0.high, b0.low)):
        return {"enabled": False, "bullish": False, "bearish": False, "notes": ["Missing OHLC; Red Dog unavailable."]}
    h1, l1, c1 = float(b1.high), float(b1.low), float(b1.close)
    h0, l0 = float(b0.high), float(b0.low)
    rng = max(1e-9, h1 - l1)
    close_pos = (c1 - l1) / rng  # 0..1

    bullish = (l1 < l0) and (c1 > l0)
    bearish = (h1 > h0) and (c1 < h0)
    bullish_strong = bool(bullish and (close_pos >= 0.70))
    bearish_strong = bool(bearish and (close_pos <= 0.30))

    out: Dict[str, Any] = {
        "enabled": True,
        "asOfDate": str(b1.trade_date)[:10],
        "bullish": bool(bullish),
        "bearish": bool(bearish),
        "strength": ("strong" if (bullish_strong or bearish_strong) else "standard") if (bullish or bearish) else None,
        "triggerLevels": None,
        "notes": [
            "Red Dog is implemented as a simple 2-day failed-break pattern (sweep prior extreme then close back through it).",
            "Use as a reversal-risk flag; confirmation is separate (follow-through above/below the reversal day).",
        ],
    }
    if bullish:
        out["triggerLevels"] = {"entryAbove": round(h1, 4), "stopBelow": round(l1, 4)}
    elif bearish:
        out["triggerLevels"] = {"entryBelow": round(l1, 4), "stopAbove": round(h1, 4)}
    return out


def _zigzag_pivots(
    closes: List[float],
    *,
    threshold_pct: float = 0.04,
    max_pivots: int = 12,
) -> List[int]:
    """
    Very lightweight zigzag pivot finder on closes.
    Returns pivot indices (into closes), ordered oldest->newest.
    """
    if not closes or len(closes) < 10:
        return []
    thr = abs(float(threshold_pct))
    if thr <= 0:
        thr = 0.04
    piv: List[int] = [0]
    direction = 0  # 0 unknown, +1 seeking high, -1 seeking low
    extreme_idx = 0
    extreme_px = float(closes[0])
    for i in range(1, len(closes)):
        px = float(closes[i])
        if direction >= 0:
            # seeking/holding a high
            if px >= extreme_px:
                extreme_px = px
                extreme_idx = i
            elif px <= extreme_px * (1.0 - thr):
                # reversal down
                if piv[-1] != extreme_idx:
                    piv.append(extreme_idx)
                direction = -1
                extreme_px = px
                extreme_idx = i
        if direction <= 0:
            # seeking/holding a low
            if px <= extreme_px:
                extreme_px = px
                extreme_idx = i
            elif px >= extreme_px * (1.0 + thr):
                # reversal up
                if piv[-1] != extreme_idx:
                    piv.append(extreme_idx)
                direction = +1
                extreme_px = px
                extreme_idx = i
        if len(piv) >= int(max_pivots):
            break
    # Add the last extreme as the final pivot (captures ongoing leg)
    if piv and piv[-1] != extreme_idx:
        piv.append(extreme_idx)
    # Deduplicate + keep last max_pivots
    piv2 = []
    for idx in piv:
        if not piv2 or idx != piv2[-1]:
            piv2.append(idx)
    if len(piv2) > int(max_pivots):
        piv2 = piv2[-int(max_pivots) :]
    return piv2


def detect_elliott_pivot_structure(
    bars: List[DailyBar],
    *,
    threshold_pct: float = 0.04,
) -> Dict[str, Any]:
    """
    Deterministic 'Elliott-style' structure classifier based on swing pivots (zigzag).
    This does NOT attempt subjective wave numbering; it classifies recent structure as:
      impulse_up | impulse_down | corrective | unclear
    """
    if not bars or len(bars) < 30:
        return {"enabled": False, "structure": "unclear", "confidence01": None, "pivots": [], "notes": ["Insufficient history for pivots."]}
    closes = [float(b.close) for b in bars if b.close is not None and b.close > 0]
    if len(closes) < 30:
        return {"enabled": False, "structure": "unclear", "confidence01": None, "pivots": [], "notes": ["Insufficient close series for pivots."]}

    piv_idx = _zigzag_pivots(closes, threshold_pct=float(threshold_pct), max_pivots=12)
    if len(piv_idx) < 5:
        return {"enabled": True, "structure": "unclear", "confidence01": 0.2, "pivots": [], "notes": ["Not enough pivots detected at threshold; structure unclear."]}

    # Map pivot indices back to bar dates by aligning to the last len(closes) bars.
    bar0 = bars[-len(closes) :]
    pivots = [{"date": str(bar0[i].trade_date)[:10], "close": round(float(closes[i]), 4), "idx": int(i)} for i in piv_idx if 0 <= i < len(bar0)]
    # Use last 5 pivots for classification
    last5 = piv_idx[-5:]
    p = [float(closes[i]) for i in last5]

    # Impulse-ish: higher highs + higher lows (or lower highs + lower lows)
    impulse_up = (p[0] < p[2] < p[4]) and (p[1] < p[3])
    impulse_dn = (p[0] > p[2] > p[4]) and (p[1] > p[3])
    structure = "impulse_up" if impulse_up else "impulse_down" if impulse_dn else "corrective"

    # Confidence based on swing size vs threshold and pivot count.
    swings = [abs(p[i] - p[i - 1]) / max(1e-9, p[i - 1]) for i in range(1, len(p))]
    avg_swing = sum(swings) / float(len(swings)) if swings else 0.0
    conf = 0.35
    if structure.startswith("impulse"):
        conf += 0.25
    conf += min(0.25, max(0.0, (avg_swing - float(threshold_pct)) / max(1e-9, float(threshold_pct))) * 0.10)
    conf += min(0.15, (len(piv_idx) / 12.0) * 0.15)
    conf = max(0.05, min(0.95, conf))

    return {
        "enabled": True,
        "mode": "zigzag_close",
        "thresholdPct": float(threshold_pct),
        "structure": structure if structure else "unclear",
        "confidence01": round(float(conf), 3),
        "pivots": pivots[-8:],  # keep it compact
        "notes": [
            "Structure is derived from zigzag pivots on daily closes (swing threshold).",
            "This is a deterministic structure classifier, not discretionary wave labeling.",
        ],
    }


def _rolling_hl_mid(highs: List[float], lows: List[float], window: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    w = int(window)
    for i in range(len(highs)):
        if i + 1 < w:
            out.append(None)
            continue
        hh = max(highs[i + 1 - w : i + 1])
        ll = min(lows[i + 1 - w : i + 1])
        out.append((float(hh) + float(ll)) / 2.0)
    return out


def compute_ichimoku_levels(bars: List[DailyBar]) -> Dict[str, Any]:
    """
    Ichimoku on daily bars.
    Returns both current conversion/base lines and a best-effort cloud view:
    - cloudNow uses SpanA/SpanB values shifted 26 days forward, aligned to 'today' (i.e., values computed 26 bars ago)
    - cloudFuture uses current (unshifted) SpanA/SpanB as the projected cloud 26 days ahead
    """
    # Require usable H/L/C
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    if not bars or any(v is None for v in highs[-1:] + lows[-1:] + closes[-1:]):
        return {"enabled": False, "notes": ["Insufficient OHLC for Ichimoku."]}

    hs = [float(x) for x in highs if x is not None]
    ls = [float(x) for x in lows if x is not None]
    cs = [float(x) for x in closes if x is not None]
    # If we dropped Nones, indices no longer align; require full alignment.
    if len(hs) != len(bars) or len(ls) != len(bars) or len(cs) != len(bars):
        return {"enabled": False, "notes": ["Missing high/low/close in bar series; cannot compute Ichimoku reliably."]}

    tenkan = _rolling_hl_mid(hs, ls, 9)
    kijun = _rolling_hl_mid(hs, ls, 26)
    span_b = _rolling_hl_mid(hs, ls, 52)
    span_a: List[Optional[float]] = []
    for i in range(len(bars)):
        if tenkan[i] is None or kijun[i] is None:
            span_a.append(None)
        else:
            span_a.append((float(tenkan[i]) + float(kijun[i])) / 2.0)

    # Chikou is close shifted back 26 (i.e. today's chikou value plotted at t-26).
    chikou = cs[-1] if len(cs) >= 1 else None
    chikou_anchor_date = None
    if len(bars) > 26:
        chikou_anchor_date = bars[-27].trade_date  # where chikou would be plotted

    # Cloud now: use values computed 26 bars ago, aligned to today.
    cloud_now = None
    if len(bars) > 26:
        i = len(bars) - 1 - 26
        a_now = span_a[i]
        b_now = span_b[i]
        if a_now is not None and b_now is not None:
            cloud_now = {"spanA": float(a_now), "spanB": float(b_now)}

    # Cloud future (projected): current spanA/spanB (unshifted) represent the forward-projected cloud.
    a_fut = span_a[-1]
    b_fut = span_b[-1]
    cloud_future = None
    if a_fut is not None and b_fut is not None:
        cloud_future = {"spanA": float(a_fut), "spanB": float(b_fut), "projectsToDate": _fmt_date(_parse_date(bars[-1].trade_date) + dt.timedelta(days=26))}

    def _cloud_obj(c: Optional[dict]) -> Optional[dict]:
        if not c:
            return None
        top = max(float(c["spanA"]), float(c["spanB"]))
        bot = min(float(c["spanA"]), float(c["spanB"]))
        return {**c, "cloudTop": top, "cloudBottom": bot, "cloudBias": ("bullish" if float(c["spanA"]) >= float(c["spanB"]) else "bearish")}

    return {
        "enabled": True,
        "tenkan": None if tenkan[-1] is None else float(tenkan[-1]),
        "kijun": None if kijun[-1] is None else float(kijun[-1]),
        "chikou": chikou,
        "chikouPlottedAtDate": chikou_anchor_date,
        "cloudNow": _cloud_obj(cloud_now),
        "cloudFuture": _cloud_obj(cloud_future),
        "notes": [
            "Ichimoku computed on daily bars.",
            "cloudNow is the cloud value aligned to the current date (computed 26 bars ago).",
            "cloudFuture is the projected cloud 26 days forward (computed today).",
        ],
    }


def compute_ichimoku_series(bars: List[DailyBar]) -> Dict[str, Any]:
    """
    Compute full Ichimoku series for historical analysis (Engine4).
    
    Returns complete series for:
    - tenkan_series: 9-period midpoint series
    - kijun_series: 26-period midpoint series
    - span_a_series: (Tenkan + Kijun) / 2 series
    - span_b_series: 52-period midpoint series
    - cloud_series: Combined cloud data per bar (aligned to current date, i.e. shifted back 26)
    - chikou_series: Close values (to be plotted 26 bars back)
    
    This enables pullback detection, Kijun slope analysis, and time-in-cloud measurement.
    """
    if not bars or len(bars) < 52:
        return {
            "enabled": False,
            "notes": ["Insufficient bars for Ichimoku series (need 52+)."],
            "tenkan_series": [],
            "kijun_series": [],
            "span_a_series": [],
            "span_b_series": [],
            "cloud_series": [],
            "chikou_series": [],
        }
    
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    
    # Require full alignment
    if any(v is None for v in highs) or any(v is None for v in lows) or any(v is None for v in closes):
        return {
            "enabled": False,
            "notes": ["Missing OHLC in bar series; cannot compute Ichimoku series reliably."],
            "tenkan_series": [],
            "kijun_series": [],
            "span_a_series": [],
            "span_b_series": [],
            "cloud_series": [],
            "chikou_series": [],
        }
    
    hs = [float(x) for x in highs]
    ls = [float(x) for x in lows]
    cs = [float(x) for x in closes]
    
    # Compute raw series
    tenkan_raw = _rolling_hl_mid(hs, ls, 9)
    kijun_raw = _rolling_hl_mid(hs, ls, 26)
    span_b_raw = _rolling_hl_mid(hs, ls, 52)
    
    # Span A = (Tenkan + Kijun) / 2
    span_a_raw: List[Optional[float]] = []
    for i in range(len(bars)):
        if tenkan_raw[i] is None or kijun_raw[i] is None:
            span_a_raw.append(None)
        else:
            span_a_raw.append((float(tenkan_raw[i]) + float(kijun_raw[i])) / 2.0)
    
    # Build cloud series aligned to current date (shift back 26 bars)
    # cloud_series[i] gives the cloud values that apply to bar[i]
    cloud_series: List[Optional[Dict[str, Any]]] = []
    for i in range(len(bars)):
        # The cloud at bar[i] is the span values from 26 bars earlier
        src_idx = i - 26
        if src_idx < 0:
            cloud_series.append(None)
            continue
        a = span_a_raw[src_idx]
        b = span_b_raw[src_idx]
        if a is None or b is None:
            cloud_series.append(None)
            continue
        top = max(float(a), float(b))
        bot = min(float(a), float(b))
        cloud_series.append({
            "spanA": float(a),
            "spanB": float(b),
            "cloudTop": top,
            "cloudBottom": bot,
            "cloudBias": "bullish" if float(a) >= float(b) else "bearish",
            "thickness": top - bot,
        })
    
    # Chikou series is just the closes (plotted 26 back when charting)
    chikou_series = cs
    
    return {
        "enabled": True,
        "barCount": len(bars),
        "tenkan_series": tenkan_raw,
        "kijun_series": kijun_raw,
        "span_a_series": span_a_raw,
        "span_b_series": span_b_raw,
        "cloud_series": cloud_series,
        "chikou_series": chikou_series,
        "closes": cs,
        "highs": hs,
        "lows": ls,
        "dates": [b.trade_date for b in bars],
        "notes": [
            "Full Ichimoku series for historical analysis.",
            "cloud_series is aligned to bar dates (shifted back 26 from computation).",
        ],
    }


def compute_volume_metrics(bars: List[DailyBar], period: int = 20) -> Dict[str, Any]:
    """
    Compute volume metrics for Engine4 trigger confirmation.
    
    Returns:
    - avg_volume: 20-day average volume
    - current_volume: Most recent bar's volume
    - volume_ratio: current / avg (1.0 = average, 1.5 = 50% above average)
    - volume_series: Full volume series
    - volume_ratio_series: Ratio series for historical analysis
    """
    if not bars:
        return {"enabled": False, "notes": ["No bars available."]}
    
    volumes: List[Optional[float]] = []
    for b in bars:
        if b.volume is not None and b.volume > 0:
            volumes.append(float(b.volume))
        else:
            volumes.append(None)
    
    # Count valid volumes
    valid_vols = [v for v in volumes if v is not None]
    if len(valid_vols) < period:
        return {
            "enabled": False,
            "notes": [f"Insufficient volume data (need {period}+ bars with volume)."],
            "volume_series": volumes,
        }
    
    # Compute rolling average and ratio
    avg_series: List[Optional[float]] = []
    ratio_series: List[Optional[float]] = []
    
    for i in range(len(bars)):
        if i + 1 < period:
            avg_series.append(None)
            ratio_series.append(None)
            continue
        
        window_vols = [v for v in volumes[i + 1 - period: i + 1] if v is not None]
        if len(window_vols) < period * 0.8:  # Require 80% of window to have valid data
            avg_series.append(None)
            ratio_series.append(None)
            continue
        
        avg = sum(window_vols) / len(window_vols)
        avg_series.append(avg)
        
        cur = volumes[i]
        if cur is not None and avg > 0:
            ratio_series.append(cur / avg)
        else:
            ratio_series.append(None)
    
    # Current values
    current_vol = volumes[-1] if volumes else None
    avg_vol = avg_series[-1] if avg_series else None
    ratio = ratio_series[-1] if ratio_series else None
    
    return {
        "enabled": True,
        "avgVolume": avg_vol,
        "currentVolume": current_vol,
        "volumeRatio": ratio,
        "period": period,
        "volume_series": volumes,
        "avg_series": avg_series,
        "ratio_series": ratio_series,
        "notes": [f"Volume metrics computed with {period}-day average."],
    }


def compute_atr_series(bars: List[DailyBar], period: int = 14) -> Dict[str, Any]:
    """
    Compute ATR series for stop placement buffer (Engine4).
    
    Returns:
    - atr: Current ATR value
    - atr_series: Full ATR series
    """
    if not bars or len(bars) < period + 1:
        return {"enabled": False, "atr": None, "atr_series": [], "notes": ["Insufficient bars for ATR."]}
    
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    
    if any(v is None for v in highs) or any(v is None for v in lows) or any(v is None for v in closes):
        return {"enabled": False, "atr": None, "atr_series": [], "notes": ["Missing OHLC for ATR."]}
    
    hs = [float(x) for x in highs]
    ls = [float(x) for x in lows]
    cs = [float(x) for x in closes]
    
    # Compute True Range series
    tr_series: List[float] = [hs[0] - ls[0]]  # First bar: just high - low
    for i in range(1, len(bars)):
        tr = max(
            hs[i] - ls[i],
            abs(hs[i] - cs[i - 1]),
            abs(ls[i] - cs[i - 1])
        )
        tr_series.append(tr)
    
    # Compute ATR using Wilder smoothing
    atr_series: List[Optional[float]] = [None] * (period - 1)
    
    # Initial ATR is simple average of first 'period' TR values
    initial_atr = sum(tr_series[:period]) / period
    atr_series.append(initial_atr)
    
    # Subsequent ATRs use Wilder smoothing
    prev_atr = initial_atr
    for i in range(period, len(bars)):
        atr = (prev_atr * (period - 1) + tr_series[i]) / period
        atr_series.append(atr)
        prev_atr = atr
    
    return {
        "enabled": True,
        "atr": atr_series[-1] if atr_series else None,
        "atr_series": atr_series,
        "period": period,
        "notes": [f"ATR computed with {period}-period Wilder smoothing."],
    }


def compute_vwap_proxy(bars: List[DailyBar], window: int = 20) -> Dict[str, Any]:
    """
    VWAP proxy (daily-only).
    - If ORATS provides a per-day vwap field, we surface it.
    - Else if volume is present, we compute a rolling VWAP over the last `window` daily bars:
        sum(typicalPrice * volume) / sum(volume), where typicalPrice=(H+L+C)/3.
    """
    if not bars:
        return {"enabled": False, "notes": ["No bars available."]}
    last = bars[-1]
    if last.vwap is not None and last.vwap > 0:
        return {
            "enabled": True,
            "mode": "orats_daily_vwap",
            "value": float(last.vwap),
            "window": 1,
            "notes": ["Using ORATS-provided daily VWAP field (if present in entitlement)."],
        }

    # Rolling proxy if volume exists
    use = bars[-int(window) :] if window and len(bars) >= 1 else bars
    numer = 0.0
    denom = 0.0
    have_any_vol = False
    for b in use:
        if b.high is None or b.low is None or b.close is None:
            continue
        if b.volume is None or b.volume <= 0:
            continue
        have_any_vol = True
        tp = (float(b.high) + float(b.low) + float(b.close)) / 3.0
        numer += tp * float(b.volume)
        denom += float(b.volume)
    if have_any_vol and denom > 0:
        return {
            "enabled": True,
            "mode": "rolling_daily_typical_price_vwap",
            "value": float(numer / denom),
            "window": int(window),
            "notes": [
                "Daily VWAP proxy computed from daily OHLC and daily volume.",
                "This is not true intraday VWAP; it’s a rolling daily proxy.",
            ],
        }

    # Last-resort proxy: typical price of last bar
    if last.high is not None and last.low is not None and last.close is not None:
        tp = (float(last.high) + float(last.low) + float(last.close)) / 3.0
        return {
            "enabled": True,
            "mode": "daily_typical_price",
            "value": float(tp),
            "window": 1,
            "notes": [
                "Volume unavailable; using typical price (H+L+C)/3 as a VWAP-like proxy.",
                "This is not true VWAP.",
            ],
        }

    return {"enabled": False, "notes": ["VWAP proxy unavailable (missing OHLC/volume)."]}


def compute_distances(*, live_price: Optional[float], levels: Dict[str, Optional[float]]) -> Dict[str, Any]:
    if live_price is None or live_price <= 0:
        return {"enabled": False, "notes": ["Live price unavailable."]}
    out: Dict[str, Any] = {"enabled": True, "price": float(live_price), "levels": {}, "notes": []}
    for k, v in (levels or {}).items():
        if v is None or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            continue
        diff = float(live_price) - float(v)
        out["levels"][str(k)] = {
            "level": float(v),
            "diffPts": round(diff, 4),
            "diffPct": round((diff / float(live_price)) * 100.0, 4),
        }
    return out


def _nearest_level(*, price: float, levels: Dict[str, Optional[float]], keys: List[str]) -> Optional[Dict[str, Any]]:
    best = None
    best_abs = None
    for k in keys:
        v = levels.get(k)
        if v is None:
            continue
        try:
            lv = float(v)
        except Exception:
            continue
        if not math.isfinite(lv) or lv <= 0:
            continue
        d = float(price) - lv
        ad = abs(d)
        if best is None or best_abs is None or ad < best_abs:
            best = {"key": str(k), "level": lv, "diffPts": d, "diffPct": (d / float(price)) * 100.0}
            best_abs = ad
    return best


def build_ta_signals(
    *,
    price: float,
    ema_levels: Dict[str, Optional[float]],
    ema_slopes: Dict[str, Optional[float]],
    rsi: Dict[str, Any],
    macd: Dict[str, Any],
    boll: Dict[str, Any],
    ich: Dict[str, Any],
    candles: Dict[str, Any],
    red_dog: Dict[str, Any],
    elliott: Dict[str, Any],
    distances: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Convert raw indicators into a compact, deterministic 'signals' object.
    This is the stable interface the narrative builder reads from.
    """
    # Trend regime (EMA)
    e8 = ema_levels.get("ema8")
    e21 = ema_levels.get("ema21")
    e50 = ema_levels.get("ema50")
    e100 = ema_levels.get("ema100")
    e200 = ema_levels.get("ema200")
    bull_stack = all(v is not None for v in (e8, e21, e50, e100, e200)) and (float(e8) > float(e21) > float(e50) > float(e100) > float(e200))
    bear_stack = all(v is not None for v in (e8, e21, e50, e100, e200)) and (float(e8) < float(e21) < float(e50) < float(e100) < float(e200))
    regime = None
    if e200 is not None:
        regime = "bull" if float(price) >= float(e200) else "bear"

    # Momentum block
    rsi_state = str(rsi.get("state") or "") if isinstance(rsi, dict) else ""
    macd_cross = str(macd.get("cross") or "") if isinstance(macd, dict) else ""
    hist_trend = str(macd.get("histTrend") or "") if isinstance(macd, dict) else ""

    # Volatility block
    bb_state = str(boll.get("state") or "") if isinstance(boll, dict) else ""
    squeeze = boll.get("squeeze") if isinstance(boll, dict) else None

    # Ichimoku regime (if available)
    ich_state = None
    if isinstance(ich, dict) and ich.get("enabled") and isinstance(ich.get("cloudNow"), dict):
        cn = ich.get("cloudNow") or {}
        top = cn.get("cloudTop")
        bot = cn.get("cloudBottom")
        if isinstance(top, (int, float)) and isinstance(bot, (int, float)):
            if float(price) > float(top):
                ich_state = "above_cloud"
            elif float(price) < float(bot):
                ich_state = "below_cloud"
            else:
                ich_state = "in_cloud"

    # Pattern summary
    patt = []
    if isinstance(candles, dict) and candles.get("enabled"):
        xs = candles.get("patterns") if isinstance(candles.get("patterns"), list) else []
        for p in xs[:5]:
            if isinstance(p, dict) and p.get("name"):
                patt.append(str(p.get("name")))

    nearest = _nearest_level(price=price, levels=(distances.get("levels") if isinstance(distances, dict) else {}) or {}, keys=["ema21", "ema50", "ema200", "bbMid", "kijun", "tenkan"])

    return {
        "enabled": True,
        "trend": {
            "regime": regime,
            "stack": ("bull" if bull_stack else "bear" if bear_stack else "mixed"),
            "emaSlopes": {k: (None if v is None else round(float(v), 6)) for k, v in (ema_slopes or {}).items()},
        },
        "momentum": {
            "rsiState": rsi_state or None,
            "macdCross": macd_cross or None,
            "macdHistTrend": hist_trend or None,
        },
        "volatility": {"bollingerState": bb_state or None, "squeeze": squeeze},
        "ichimoku": {"state": ich_state},
        "patterns": {
            "candles": patt,
            "redDog": ("bullish" if bool(red_dog.get("bullish")) else "bearish" if bool(red_dog.get("bearish")) else None) if isinstance(red_dog, dict) else None,
        },
        "elliott": {
            "structure": (elliott.get("structure") if isinstance(elliott, dict) else None),
            "confidence01": (elliott.get("confidence01") if isinstance(elliott, dict) else None),
        },
        "levels": {"nearest": nearest},
    }


def build_ta_narrative(
    *,
    ticker: str,
    price: float,
    last_close: float,
    ema_levels: Dict[str, Optional[float]],
    ema_slopes: Dict[str, Optional[float]],
    rsi: Dict[str, Any],
    macd: Dict[str, Any],
    boll: Dict[str, Any],
    ich: Dict[str, Any],
    candles: Dict[str, Any],
    red_dog: Dict[str, Any],
    elliott: Dict[str, Any],
    signals: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Deterministic desk-style narrative (multi-sentence) from daily signals.
    """
    notes: List[str] = []
    bullets: List[str] = []
    invalidation: List[str] = []

    # --- Trend (EMA) ---
    stack = None
    try:
        stack = str(((signals.get("trend") or {}).get("stack") or "")).lower()
    except Exception:
        stack = None
    regime = None
    try:
        regime = (signals.get("trend") or {}).get("regime")
    except Exception:
        regime = None

    e21 = ema_levels.get("ema21")
    e50 = ema_levels.get("ema50")
    e200 = ema_levels.get("ema200")
    slope200 = ema_slopes.get("ema200_slope5") if isinstance(ema_slopes, dict) else None

    trend_sentence = None
    if stack == "bull":
        trend_sentence = f"{ticker} is in a constructive trend regime on the daily timeframe: the EMAs are bull-stacked (8>21>50>100>200), which typically supports swing continuation setups while pullbacks are bought."
        bullets.append("Trend: EMAs are bull-stacked (8>21>50>100>200), a classic uptrend regime for swing trades.")
        if e21 is not None:
            invalidation.append(f"Trend invalidation: a daily close back below EMA21 (~{round(float(e21),2)}) would be an early sign the uptrend is losing momentum.")
    elif stack == "bear":
        trend_sentence = f"{ticker} is in a defensive daily trend regime: the EMAs are bear-stacked (8<21<50<100<200), which usually means rallies are lower-quality and mean-revert until the stack repairs."
        bullets.append("Trend: EMAs are bear-stacked (8<21<50<100<200), favoring rallies being sold rather than chased.")
        if e21 is not None:
            invalidation.append(f"Trend repair: a reclaim of EMA21 (~{round(float(e21),2)}) and then EMA50 would improve the swing profile.")
    else:
        # Mixed stack: focus on where price is vs key trend lines.
        if e200 is not None:
            side = "above" if price >= float(e200) else "below"
            trend_sentence = f"{ticker} has a mixed EMA stack right now, but price is {side} the EMA200 (~{round(float(e200),2)}), which is the main long-term regime line for swing positioning."
            bullets.append(f"Regime: price is {side} EMA200 (~{round(float(e200),2)}); that’s the primary swing regime divider.")
        else:
            trend_sentence = f"{ticker} has a mixed EMA stack and we don’t have enough history for EMA200; treat trend signals as lower-confidence until more history is available."
            notes.append("EMA200 unavailable (insufficient history).")
        if e21 is not None and e50 is not None:
            bullets.append(f"Key levels: EMA21≈{round(float(e21),2)} and EMA50≈{round(float(e50),2)} are the first trend lines to watch for swing follow-through vs pullback risk.")

    # EMA slope note
    if slope200 is not None and e200 is not None and math.isfinite(float(slope200)):
        slope_dir = "rising" if float(slope200) > 0 else "falling" if float(slope200) < 0 else "flat"
        bullets.append(f"EMA200 slope: {slope_dir} over the last week (proxy), which helps frame whether the long-term regime is improving or deteriorating.")

    # --- Momentum (RSI + MACD) ---
    momentum_sentence = None
    rsi_val = rsi.get("value") if isinstance(rsi, dict) else None
    rsi_state = rsi.get("state") if isinstance(rsi, dict) else None
    rsi_slope = rsi.get("slope1d") if isinstance(rsi, dict) else None
    macd_cross = macd.get("cross") if isinstance(macd, dict) else None
    hist_trend = macd.get("histTrend") if isinstance(macd, dict) else None
    if rsi_val is not None and isinstance(rsi_val, (int, float)):
        rsiv = float(rsi_val)
        rsi_phrase = "neutral" if not rsi_state else str(rsi_state)
        slope_phrase = ""
        if rsi_slope is not None and isinstance(rsi_slope, (int, float)):
            slope_phrase = " rising" if float(rsi_slope) > 0 else " falling" if float(rsi_slope) < 0 else " flat"
        momentum_sentence = f"Momentum is best summarized by RSI and MACD: RSI(14) is {round(rsiv,1)} ({rsi_phrase}) and is{'' if slope_phrase else ''}{slope_phrase} vs yesterday, while MACD is {('flashing a ' + str(macd_cross) + ' cross' ) if macd_cross else 'not showing a fresh cross'} with histogram {str(hist_trend) if hist_trend else 'trend not available'}."
        bullets.append(f"RSI(14)={round(rsiv,1)} ({rsi_phrase}); watch for regime holds (bull markets often hold 40–50 on pullbacks; bear markets often fail there).")
        if macd_cross:
            bullets.append(f"MACD: {macd_cross} cross detected; histogram trend is {hist_trend or 'unknown'} (momentum acceleration/deceleration proxy).")
    else:
        momentum_sentence = "Momentum signals (RSI/MACD) are unavailable due to limited close history; treat momentum reads as lower-confidence until more bars are present."
        notes.append("RSI/MACD unavailable (insufficient history).")

    # --- Volatility (Bollinger) ---
    vol_sentence = None
    if isinstance(boll, dict) and boll.get("enabled"):
        bw = boll.get("bandwidthPct")
        st = boll.get("state")
        sq = boll.get("squeeze")
        vol_sentence = f"Volatility context from Bollinger Bands is {'compressed (squeeze)' if sq else 'normal'} with bandwidth {('~'+str(round(float(bw),2))+'%') if isinstance(bw,(int,float)) else 'n/a'}. Price is {st or 'inside the bands'}, which helps frame whether recent moves are trend extensions (outside bands) or mean-reversion (inside)."
        if sq:
            bullets.append("Bollinger squeeze: volatility has compressed; swing breakouts tend to require follow-through confirmation.")
        if st in ("above_upper", "below_lower"):
            bullets.append(f"Bollinger extension: price is {st.replace('_',' ')}; extensions can continue, but odds of a pause/mean-reversion increase without fresh catalysts.")
    else:
        vol_sentence = "Bollinger context is unavailable (insufficient history); volatility regime is therefore less defined in this readout."
        notes.append("Bollinger unavailable (insufficient history).")

    # --- Ichimoku (if available) ---
    ich_sentence = None
    if isinstance(ich, dict) and ich.get("enabled"):
        st = (signals.get("ichimoku") or {}).get("state")
        cn = ich.get("cloudNow") if isinstance(ich.get("cloudNow"), dict) else None
        bias = cn.get("cloudBias") if isinstance(cn, dict) else None
        if st == "above_cloud":
            ich_sentence = f"Ichimoku confirms trend strength: price is above the cloud (Kumo) with a {bias or 'mixed'} cloud bias, which typically supports swing continuation as long as price stays out of the cloud."
            invalidation.append("Ichimoku invalidation: a daily close back into the cloud shifts the regime toward chop/mean-reversion.")
        elif st == "below_cloud":
            ich_sentence = f"Ichimoku is bearish: price is below the cloud with a {bias or 'mixed'} cloud bias, which usually means rallies are resistance-led until price reclaims the cloud."
        elif st == "in_cloud":
            ich_sentence = "Ichimoku is neutral: price is inside the cloud, which is commonly a chop/transition regime where breakouts have higher failure rates."
        else:
            ich_sentence = "Ichimoku is enabled but cloud positioning is unclear; treat cloud-based conclusions as low confidence."
    else:
        ich_sentence = "Ichimoku is unavailable due to missing OHLC inputs; cloud-based trend confirmation is omitted."
        notes.append("Ichimoku unavailable (missing OHLC).")

    # --- Patterns (candles + Red Dog) ---
    patt_sentence = None
    patt_names: List[str] = []
    if isinstance(candles, dict) and candles.get("enabled"):
        xs = candles.get("patterns") if isinstance(candles.get("patterns"), list) else []
        patt_names = [str(p.get("name")) for p in xs if isinstance(p, dict) and p.get("name")]
    rd_bull = bool(red_dog.get("bullish")) if isinstance(red_dog, dict) else False
    rd_bear = bool(red_dog.get("bearish")) if isinstance(red_dog, dict) else False
    if rd_bull or rd_bear or patt_names:
        parts = []
        if rd_bull:
            parts.append("a bullish Red Dog (failed breakdown) reversal")
            invalidation.append("Red Dog invalidation: failure to follow-through (taking out the reversal day low) would reduce reversal confidence.")
        if rd_bear:
            parts.append("a bearish Red Dog (failed breakout) reversal")
            invalidation.append("Red Dog invalidation: failure to follow-through (taking out the reversal day high) would reduce reversal confidence.")
        if patt_names:
            parts.append("candlestick signals: " + ", ".join(patt_names[:3]))
        patt_sentence = f"On the tape, we have {(' and '.join(parts))}. These are short-term swing flags; they matter most when they occur at key levels (EMA21/50/200, cloud edge, or Bollinger bands)."
        if patt_names:
            bullets.append(f"Candles: {', '.join(patt_names[:3])} detected on the most recent bar(s) (mechanical pattern scan).")
        if rd_bull or rd_bear:
            tl = red_dog.get('triggerLevels') if isinstance(red_dog, dict) else None
            if isinstance(tl, dict):
                if rd_bull and tl.get("entryAbove") is not None:
                    bullets.append(f"Red Dog bullish trigger (informational): entry above ~{tl.get('entryAbove')} with stop below ~{tl.get('stopBelow')}.")
                if rd_bear and tl.get("entryBelow") is not None:
                    bullets.append(f"Red Dog bearish trigger (informational): entry below ~{tl.get('entryBelow')} with stop above ~{tl.get('stopAbove')}.")
    else:
        patt_sentence = "No major daily reversal candle patterns are flagged on the most recent bars; the tape read is therefore dominated by trend/momentum/volatility signals."

    # --- Elliott-style pivot structure ---
    ell_sentence = None
    if isinstance(elliott, dict) and elliott.get("enabled"):
        st = str(elliott.get("structure") or "unclear")
        cf = elliott.get("confidence01")
        conf_txt = f" (confidence ~{round(float(cf),2)})" if isinstance(cf, (int, float)) else ""
        if st == "impulse_up":
            ell_sentence = f"Swing structure is impulse-like to the upside{conf_txt}: the recent pivot sequence shows higher highs and higher lows, which tends to favor continuation unless a prior swing low breaks."
        elif st == "impulse_down":
            ell_sentence = f"Swing structure is impulse-like to the downside{conf_txt}: the recent pivot sequence shows lower highs and lower lows, which tends to favor fade-the-rally setups until structure repairs."
        elif st == "corrective":
            ell_sentence = f"Swing structure is corrective/overlapping{conf_txt}: this often means chop and false breaks until price resolves out of the range with confirmation."
        else:
            ell_sentence = f"Swing structure is unclear{conf_txt}: pivots do not form a clean impulse/correction signature at the current swing threshold."
    else:
        ell_sentence = "Swing structure (Elliott-style pivots) is unavailable or low-confidence due to limited history."

    # --- Key level / invalidation (nearest level) ---
    nearest = None
    try:
        nearest = ((signals.get("levels") or {}).get("nearest"))
    except Exception:
        nearest = None
    if isinstance(nearest, dict) and nearest.get("key") and nearest.get("level") is not None:
        k = str(nearest.get("key"))
        lv = float(nearest.get("level"))
        dp = float(nearest.get("diffPts"))
        side = "above" if dp > 0 else "below" if dp < 0 else "at"
        bullets.append(f"Nearest reference level: {k}≈{round(lv,2)}; price is {side} by {round(abs(dp),2)} points.")

    # Build summary paragraph
    sentences = [trend_sentence, momentum_sentence, vol_sentence, ich_sentence, patt_sentence, ell_sentence]
    summary = " ".join([s for s in sentences if s])
    # Ensure it's desk-readable and anchored to EOD
    summary = f"As of the latest daily close ({round(float(last_close),2)}), {summary}"

    # If we have no invalidations, add a generic one
    if not invalidation and e21 is not None:
        invalidation.append(f"Primary invalidation: sustained closes below EMA21 (~{round(float(e21),2)}) would shift the swing profile toward consolidation/pullback risk.")
    if e200 is not None and regime == "bull":
        invalidation.append(f"Regime risk: losing EMA200 (~{round(float(e200),2)}) would be a material deterioration for swing positioning.")
    if e200 is not None and regime == "bear":
        invalidation.append(f"Regime repair: reclaiming EMA200 (~{round(float(e200),2)}) would be a meaningful long-term improvement signal.")

    # Deduplicate bullets/invalidation (keep order)
    def _uniq(xs: List[str]) -> List[str]:
        seen = set()
        out = []
        for x in xs:
            k0 = str(x).strip()
            if not k0 or k0 in seen:
                continue
            seen.add(k0)
            out.append(k0)
        return out

    return {
        "enabled": True,
        "priceUsed": round(float(price), 4),
        "summary": summary.strip(),
        "bullets": _uniq(bullets)[:10],
        "invalidation": _uniq(invalidation)[:10],
        "notes": _uniq(notes),
    }


def compute_technicals_payload(
    client: OratsClient,
    *,
    ticker: str,
    as_of_date: Optional[str] = None,
    lookback_days: int = 420,
) -> Dict[str, Any]:
    """
    Compute technicals on daily bars + a live overlay.\n
    - Indicators are computed on daily bars up to the last available daily close.\n
    - Live overlay is session-aware: open=ORATS->EODHD->close, closed=close.\n
    """
    t = str(ticker).strip().upper()
    today = dt.date.today()
    if as_of_date:
        try:
            today = _parse_date(str(as_of_date)[:10])
        except Exception:
            today = dt.date.today()

    start = today - dt.timedelta(days=int(lookback_days))
    bars = fetch_daily_bars_range(client, ticker=t, start=start, end=today)
    if not bars:
        # Fall back a bit: sometimes today’s EOD is missing intraday; try last 30 days anchored to today anyway.
        start2 = today - dt.timedelta(days=60)
        bars = fetch_daily_bars_range(client, ticker=t, start=start2, end=today)

    if not bars:
        return {"enabled": False, "ticker": t, "notes": ["No daily bars available from ORATS."]}

    closes = [float(b.close) for b in bars if b.close is not None and b.close > 0]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    ok_ohlc = bool(closes) and highs[-1] is not None and lows[-1] is not None

    ema = compute_ema_levels(closes, spans=[8, 21, 50, 100, 200]) if closes else {}
    # EMA slope proxies (5 trading bars) for narrative/regime context.
    ema_slopes: Dict[str, Optional[float]] = {}
    try:
        for span in (21, 50, 200):
            if len(closes) >= int(span) + 6:
                ser = _ema_series(closes, int(span))
                if len(ser) >= 6:
                    ema_slopes[f"ema{int(span)}_slope5"] = float(ser[-1]) - float(ser[-6])
            else:
                ema_slopes[f"ema{int(span)}_slope5"] = None
    except Exception:
        ema_slopes = {}
    # --- RSI / MACD / Bollinger (daily close-based; no OHLC required) ---
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
    if len(closes) >= 16:
        rsi_series = compute_rsi_series(closes, period=14)
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
    if len(closes) >= 40:
        m = compute_macd_series(closes, fast=12, slow=26, signal=9)
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
    if len(closes) >= 40:
        bb = compute_bollinger_series(closes, period=20, stdev=2.0)
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
            c0 = float(closes[-1])
            if c0 > float(up_v):
                state = "above_upper"
            elif c0 < float(lo_v):
                state = "below_lower"
            else:
                state = "inside"
        squeeze = None
        bw_vals = [float(x) for x in bw_s[-120:] if x is not None and math.isfinite(float(x))]
        if bw_v is not None and bw_vals:
            pr = _percentile_rank(float(bw_v), bw_vals)
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

    # --- Pattern layer (daily, best-effort) ---
    candles = detect_candlestick_patterns(bars) if ok_ohlc else {"enabled": False, "patterns": [], "notes": ["Insufficient OHLC for candle patterns."]}
    red_dog = detect_red_dog_reversal(bars) if ok_ohlc else {"enabled": False, "bullish": False, "bearish": False, "notes": ["Insufficient OHLC for Red Dog."]}
    elliott = detect_elliott_pivot_structure(bars, threshold_pct=0.04) if closes else {"enabled": False, "structure": "unclear", "notes": ["Insufficient closes for pivots."]}

    ich = compute_ichimoku_levels(bars) if ok_ohlc else {"enabled": False, "notes": ["Insufficient OHLC for Ichimoku."]}
    vwap = compute_vwap_proxy(bars, window=20)

    live_px = fetch_live_price_optional(client, ticker=t)

    # Distances to key levels for quick “think around” use.
    level_map: Dict[str, Optional[float]] = {}
    level_map.update(ema)
    # Add Bollinger mid/upper/lower as optional levels for distance reporting.
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
    if isinstance(vwap, dict) and vwap.get("enabled") and vwap.get("value") is not None:
        level_map["vwapProxy"] = float(vwap["value"])
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

    last_bar = bars[-1]
    last_close = float(last_bar.close) if (last_bar.close is not None) else float(closes[-1])
    px_for_narr = float(live_px) if (live_px is not None and live_px > 0) else float(last_close)

    # --- Signals + deterministic narrative (daily) ---
    signals = build_ta_signals(
        price=px_for_narr,
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
        ticker=t,
        price=px_for_narr,
        last_close=last_close,
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
    return {
        "enabled": True,
        "ticker": t,
        "asOfDate": str(as_of_date or last_bar.trade_date)[:10],
        "barDateUsed": str(last_bar.trade_date)[:10],
        "lastDailyClose": None if last_bar.close is None else round(float(last_bar.close), 4),
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
        "vwapProxy": ({"enabled": False} if not isinstance(vwap, dict) else {**vwap, "value": (None if vwap.get("value") is None else round(float(vwap["value"]), 4))}),
        "distances": distances,
        "signals": signals,
        "narrative": narrative,
        "notes": [
            "Indicators are computed on daily bars (EOD).",
            "Live overlay is session-aware (open: ORATS -> EODHD -> close fallback; closed: latest close).",
        ],
    }


def encode_image_to_data_url(*, content: bytes, content_type: str) -> str:
    b64 = base64.b64encode(content).decode("utf-8")
    ct = str(content_type or "application/octet-stream")
    return f"data:{ct};base64,{b64}"


