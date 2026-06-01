"""
Engine 3: Red Dog Reversal — backtest harness.

Turns the "A+" label into a *measured* edge. Walks each ticker's history
bar-by-bar, detects the Red Dog pattern using only data available at that bar,
scores it with the exact production scorer, simulates the trade with
`evaluate_outcome`, and aggregates win-rate / avg-R / expectancy / MAE / MFE —
broken out by grade and by trend alignment (the audit's key question: do
counter-trend setups actually work?).

Two entry points:
- `backtest_from_bars(...)` — pure, deterministic, no I/O (unit-testable).
- `backtest_red_dog(client, ...)` — fetches the universe + SPY trend via ORATS
  and delegates to `backtest_from_bars`.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from backend.technicals import DailyBar, fetch_daily_bars_range
from backend.engine3_red_dog import (
    detect_red_dog_enhanced,
    build_red_dog_signal,
    evaluate_outcome,
    _trend_alignment_for,
)
from backend.engine3_screener import _compute_ema

LOG = logging.getLogger("engine3_backtest")


def _blank_stats() -> Dict[str, Any]:
    return {
        "signals": 0,
        "triggered": 0,
        "targetHit": 0,
        "stopped": 0,
        "expired": 0,
        "openAtEnd": 0,
        "_r_sum": 0.0,
        "_mae_sum": 0.0,
        "_mfe_sum": 0.0,
    }


def _record(stats: Dict[str, Any], grade_signal_score: str, outcome: Dict[str, Any]) -> None:
    stats["signals"] += 1
    status = outcome["status"]
    if status == "expired":
        stats["expired"] += 1
        return
    if not outcome.get("triggered"):
        stats["expired"] += 1
        return
    stats["triggered"] += 1
    stats["_r_sum"] += float(outcome.get("rMultiple") or 0.0)
    stats["_mae_sum"] += float(outcome.get("mae") or 0.0)
    stats["_mfe_sum"] += float(outcome.get("mfe") or 0.0)
    if status == "target_hit":
        stats["targetHit"] += 1
    elif status == "stopped":
        stats["stopped"] += 1
    elif status == "triggered":
        stats["openAtEnd"] += 1


def _finalize(stats: Dict[str, Any]) -> Dict[str, Any]:
    triggered = stats["triggered"]
    resolved = stats["targetHit"] + stats["stopped"]
    out = {
        "signals": stats["signals"],
        "triggered": triggered,
        "targetHit": stats["targetHit"],
        "stopped": stats["stopped"],
        "expired": stats["expired"],
        "openAtEnd": stats["openAtEnd"],
        "triggerRate": round(100.0 * triggered / stats["signals"], 1) if stats["signals"] else None,
        "winRate": round(100.0 * stats["targetHit"] / resolved, 1) if resolved else None,
        "avgR": round(stats["_r_sum"] / triggered, 3) if triggered else None,
        "expectancy": round(stats["_r_sum"] / triggered, 3) if triggered else None,
        "avgMae": round(stats["_mae_sum"] / triggered, 3) if triggered else None,
        "avgMfe": round(stats["_mfe_sum"] / triggered, 3) if triggered else None,
    }
    return out


def _build_trend_map(market_bars: Optional[List[DailyBar]]) -> Dict[str, str]:
    """Map trade_date -> SPX trend direction via 21 EMA (for alignment stats)."""
    out: Dict[str, str] = {}
    if not market_bars:
        return out
    closes: List[float] = []
    for b in market_bars:
        if b.close is None:
            continue
        closes.append(float(b.close))
        ema = _compute_ema(closes, 21)
        if ema is not None:
            out[str(b.trade_date)[:10]] = "bullish" if closes[-1] > ema else "bearish"
    return out


def backtest_from_bars(
    bars_by_ticker: Dict[str, List[DailyBar]],
    *,
    min_score: float = 0.0,
    warmup: int = 60,
    trigger_window: int = 3,
    max_hold: int = 10,
    trend_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Pure walk-forward backtest over pre-fetched bars. No network I/O."""
    trend_map = trend_map or {}

    overall = _blank_stats()
    by_grade: Dict[str, Dict[str, Any]] = {}
    by_alignment: Dict[str, Dict[str, Any]] = {}
    tickers_with_signals = 0

    for ticker, bars in bars_by_ticker.items():
        if not bars or len(bars) < warmup + 2:
            continue
        had_signal = False
        # Leave room for forward bars (at least 1) after each candidate bar.
        for i in range(warmup, len(bars) - 1):
            window = bars[: i + 1]
            detection = detect_red_dog_enhanced(window, ticker=ticker)
            if not detection.get("bullish") and not detection.get("bearish"):
                continue
            pattern = detection.get("pattern") or {}
            sig_date = pattern.get("signalDate", str(bars[i].trade_date)[:10])
            trend_direction = trend_map.get(sig_date)

            signal = build_red_dog_signal(
                ticker=ticker,
                detection=detection,
                bars=window,
                trend_direction=trend_direction,
            )
            if signal is None or signal.score < min_score:
                continue

            forward = bars[i + 1:]
            outcome = evaluate_outcome(
                direction=signal.direction,
                entry_trigger=signal.entry_trigger,
                stop_loss=signal.stop_loss,
                target_1=signal.target_1,
                forward_bars=forward,
                trigger_window=trigger_window,
                max_hold=max_hold,
            )

            had_signal = True
            _record(overall, signal.grade, outcome)
            by_grade.setdefault(signal.grade, _blank_stats())
            _record(by_grade[signal.grade], signal.grade, outcome)
            align = signal.trend_alignment or "unknown"
            by_alignment.setdefault(align, _blank_stats())
            _record(by_alignment[align], signal.grade, outcome)
        if had_signal:
            tickers_with_signals += 1

    return {
        "overall": _finalize(overall),
        "byGrade": {g: _finalize(s) for g, s in sorted(by_grade.items())},
        "byTrendAlignment": {a: _finalize(s) for a, s in sorted(by_alignment.items())},
        "params": {
            "minScore": min_score,
            "warmup": warmup,
            "triggerWindow": trigger_window,
            "maxHold": max_hold,
            "tickersTested": len(bars_by_ticker),
            "tickersWithSignals": tickers_with_signals,
        },
    }


def backtest_red_dog(
    client,
    *,
    tickers: List[str],
    start: dt.date,
    end: dt.date,
    min_score: float = 0.0,
    warmup: int = 60,
    trigger_window: int = 3,
    max_hold: int = 10,
    max_tickers: int = 60,
) -> Dict[str, Any]:
    """Universe backtest over a date range using ORATS daily bars."""
    tickers = list(dict.fromkeys(t.upper().strip() for t in tickers if t))[:max_tickers]
    # Pull enough history before `start` to satisfy the warmup.
    fetch_start = start - dt.timedelta(days=int(warmup * 1.6) + 20)

    # SPY trend map for alignment breakdown.
    trend_map: Dict[str, str] = {}
    try:
        spy_bars = fetch_daily_bars_range(client, ticker="SPY", start=fetch_start, end=end)
        trend_map = _build_trend_map(spy_bars)
    except Exception as e:
        LOG.warning(f"Backtest SPY trend fetch failed: {e}")

    bars_by_ticker: Dict[str, List[DailyBar]] = {}
    for t in tickers:
        try:
            bars = fetch_daily_bars_range(client, ticker=t, start=fetch_start, end=end)
            if bars and len(bars) >= warmup + 2:
                bars_by_ticker[t] = bars
        except Exception as e:
            LOG.warning(f"Backtest bars fetch failed for {t}: {e}")

    result = backtest_from_bars(
        bars_by_ticker,
        min_score=min_score,
        warmup=warmup,
        trigger_window=trigger_window,
        max_hold=max_hold,
        trend_map=trend_map,
    )
    result["window"] = {"start": start.isoformat(), "end": end.isoformat()}
    return result
