"""
Engine 4: Ichimoku Cloud Continuation — backtest harness.

Turns the "A+" / "actionable vs structure" labels into a *measured* edge.
Walks each ticker's history bar-by-bar, detects the Ichimoku continuation
setup using only data available at that bar, scores it with the exact
production scorer, simulates the trade with `evaluate_outcome` (reused from
Red Dog), and aggregates win-rate / avg-R / expectancy / MAE / MFE — broken
out by grade AND by freshness bucket. The bucket breakdown answers the
audit's key question: *does the "structure" surface actually pay, or is it
noise we should keep suppressing?*

Two entry points:
- `backtest_from_bars(...)` — pure, deterministic, no I/O (unit-testable).
- `backtest_ichimoku(client, ...)` — fetches the universe via ORATS and
  delegates to `backtest_from_bars`.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from backend.technicals import DailyBar, fetch_daily_bars_range, compute_ichimoku_series
from backend.engine4_ichimoku import (
    detect_ichimoku_setup,
    build_ichimoku_signal,
)
from backend.engine3_red_dog import evaluate_outcome

LOG = logging.getLogger("engine4_backtest")


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


def _record(stats: Dict[str, Any], outcome: Dict[str, Any]) -> None:
    stats["signals"] += 1
    status = outcome["status"]
    if status == "expired" or not outcome.get("triggered"):
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
    return {
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


def backtest_from_bars(
    bars_by_ticker: Dict[str, List[DailyBar]],
    *,
    min_score: float = 0.0,
    warmup: int = 80,
    trigger_window: int = 3,
    max_hold: int = 10,
) -> Dict[str, Any]:
    """Pure walk-forward continuation backtest over pre-fetched bars.

    Each distinct (ticker, signalDate) is counted once — the freshness window
    re-surfaces the same trigger bar for a few sessions, so we dedupe to avoid
    double-counting a single trade.
    """
    overall = _blank_stats()
    by_grade: Dict[str, Dict[str, Any]] = {}
    by_bucket: Dict[str, Dict[str, Any]] = {}
    tickers_with_signals = 0

    for ticker, bars in bars_by_ticker.items():
        if not bars or len(bars) < warmup + 2:
            continue
        had_signal = False
        seen_dates: set = set()

        for i in range(warmup, len(bars) - 1):
            window = bars[: i + 1]
            detection = detect_ichimoku_setup(window, ticker=ticker)
            if not detection.get("hasSignal"):
                continue

            sig_payload = detection.get("signal") or {}
            sig_date = str(sig_payload.get("signalDate") or bars[i].trade_date)[:10]
            if sig_date in seen_dates:
                continue  # same trigger bar already counted

            ich = compute_ichimoku_series(window)
            signal = build_ichimoku_signal(
                ticker=ticker,
                detection=detection,
                bars=window,
                closes=ich.get("closes", []),
                tenkan_series=ich.get("tenkan_series", []),
            )
            if signal is None or signal.score < min_score:
                continue
            if signal.freshness_bucket == "rejected":
                continue

            seen_dates.add(sig_date)
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
            _record(overall, outcome)
            by_grade.setdefault(signal.grade, _blank_stats())
            _record(by_grade[signal.grade], outcome)
            bucket = signal.freshness_bucket or "unknown"
            by_bucket.setdefault(bucket, _blank_stats())
            _record(by_bucket[bucket], outcome)
        if had_signal:
            tickers_with_signals += 1

    return {
        "overall": _finalize(overall),
        "byGrade": {g: _finalize(s) for g, s in sorted(by_grade.items())},
        "byBucket": {b: _finalize(s) for b, s in sorted(by_bucket.items())},
        "params": {
            "minScore": min_score,
            "warmup": warmup,
            "triggerWindow": trigger_window,
            "maxHold": max_hold,
            "tickersTested": len(bars_by_ticker),
            "tickersWithSignals": tickers_with_signals,
        },
    }


def backtest_ichimoku(
    client,
    *,
    tickers: List[str],
    start: dt.date,
    end: dt.date,
    min_score: float = 0.0,
    warmup: int = 80,
    trigger_window: int = 3,
    max_hold: int = 10,
    max_tickers: int = 40,
) -> Dict[str, Any]:
    """Universe continuation backtest over a date range using ORATS daily bars."""
    tickers = list(dict.fromkeys(t.upper().strip() for t in tickers if t))[:max_tickers]
    # Pull enough history before `start` to satisfy the (cloud-heavy) warmup.
    fetch_start = start - dt.timedelta(days=int(warmup * 1.8) + 30)

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
    )
    result["window"] = {"start": start.isoformat(), "end": end.isoformat()}
    return result
