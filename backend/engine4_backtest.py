"""
Engine 4: Ichimoku Cloud Continuation — backtest harness.

Turns the "A+" / "actionable vs structure" labels into a *measured* edge.
Walks each ticker's history bar-by-bar, detects every Ichimoku playbook
(core Kijun pullback, TK cross, Kumo breakout) using only data available at
that bar, scores each with the exact production scorer, simulates the trade
with `evaluate_outcome` (reused from Red Dog), and aggregates win-rate /
avg-R / expectancy / MAE / MFE — broken out by grade, by freshness bucket,
AND by playbook. The playbook cohort is the number that decides whether a
research playbook earns desk trust, gets tightened, or gets cut.

Two entry points:
- `backtest_from_bars(...)` — pure, deterministic, no I/O (unit-testable).
- `backtest_ichimoku(client, ...)` — fetches the universe via ORATS and
  delegates to `backtest_from_bars`.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from backend.technicals import DailyBar, fetch_daily_bars_range
from backend.engine4_ichimoku import (
    PLAYBOOK_KIJUN_PULLBACK,
    PLAYBOOK_KUMO_BREAKOUT,
    PLAYBOOK_TK_CROSS,
    build_ichimoku_signal,
    compute_detection_context,
    detect_ichimoku_setup,
    detect_kumo_breakout_setup,
    detect_tk_cross_setup,
)
from backend.engine3_red_dog import evaluate_outcome

# Walked per bar window, on the same shared detection context.
_PLAYBOOK_DETECTORS = (
    (PLAYBOOK_KIJUN_PULLBACK, detect_ichimoku_setup),
    (PLAYBOOK_TK_CROSS, detect_tk_cross_setup),
    (PLAYBOOK_KUMO_BREAKOUT, detect_kumo_breakout_setup),
)

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
    """Pure walk-forward backtest over pre-fetched bars, all playbooks.

    Each distinct (ticker, playbook, signalDate) is counted once — the
    freshness window re-surfaces the same trigger bar for a few sessions, so
    we dedupe to avoid double-counting a single trade. Every playbook uses
    the same outcome evaluator (same trigger window, same max hold), so the
    ``byPlaybook`` cohorts are directly comparable.
    """
    overall = _blank_stats()
    by_grade: Dict[str, Dict[str, Any]] = {}
    by_bucket: Dict[str, Dict[str, Any]] = {}
    by_playbook: Dict[str, Dict[str, Any]] = {}
    tickers_with_signals = 0

    for ticker, bars in bars_by_ticker.items():
        if not bars or len(bars) < warmup + 2:
            continue
        had_signal = False
        seen_keys: set = set()

        for i in range(warmup, len(bars) - 1):
            window = bars[: i + 1]
            # One Ichimoku-series computation per bar window, shared by all
            # playbook detectors AND the signal build.
            context, _err = compute_detection_context(window)
            if context is None:
                continue

            for playbook, detector in _PLAYBOOK_DETECTORS:
                detection = detector(window, ticker=ticker, context=context)
                if not detection.get("hasSignal"):
                    continue

                sig_payload = detection.get("signal") or {}
                sig_date = str(sig_payload.get("signalDate") or bars[i].trade_date)[:10]
                key = (playbook, sig_date)
                if key in seen_keys:
                    continue  # same trigger bar already counted

                signal = build_ichimoku_signal(
                    ticker=ticker,
                    detection=detection,
                    bars=window,
                    closes=context["closes"],
                    tenkan_series=context["tenkan_series"],
                )
                if signal is None or signal.score < min_score:
                    continue
                if signal.freshness_bucket == "rejected":
                    continue

                seen_keys.add(key)
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
                by_playbook.setdefault(playbook, _blank_stats())
                _record(by_playbook[playbook], outcome)
        if had_signal:
            tickers_with_signals += 1

    return {
        "overall": _finalize(overall),
        "byGrade": {g: _finalize(s) for g, s in sorted(by_grade.items())},
        "byBucket": {b: _finalize(s) for b, s in sorted(by_bucket.items())},
        "byPlaybook": {p: _finalize(s) for p, s in sorted(by_playbook.items())},
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
    """Universe continuation backtest over a date range using EODHD daily bars."""
    tickers = list(dict.fromkeys(t.upper().strip() for t in tickers if t))[:max_tickers]
    # Pull enough history before `start` to satisfy the (cloud-heavy) warmup.
    fetch_start = start - dt.timedelta(days=int(warmup * 1.8) + 30)

    bars_by_ticker: Dict[str, List[DailyBar]] = {}
    for t in tickers:
        try:
            bars = fetch_daily_bars_range(ticker=t, start=fetch_start, end=end)
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
