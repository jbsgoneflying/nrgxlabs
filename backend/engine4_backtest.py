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
        "_hold_sum": 0,
        "_hold_win_sum": 0,
        "_hold_loss_sum": 0,
        "_pct_sum": 0.0,
        "_pct_n": 0,
        # System-exit model extras (stay zero for trigger/close models).
        "systemExit": 0,
        "beStop": 0,
        "gapSkipped": 0,
        "beMoved": 0,
        "wins": 0,
        "losses": 0,
        "_r_win_sum": 0.0,
        "_r_loss_sum": 0.0,
    }


def _record(stats: Dict[str, Any], outcome: Dict[str, Any]) -> None:
    stats["signals"] += 1
    status = outcome["status"]
    if status == "expired" or not outcome.get("triggered"):
        stats["expired"] += 1
        if outcome.get("gapSkipped"):
            stats["gapSkipped"] += 1
        return
    stats["triggered"] += 1
    r = float(outcome.get("rMultiple") or 0.0)
    stats["_r_sum"] += r
    stats["_mae_sum"] += float(outcome.get("mae") or 0.0)
    stats["_mfe_sum"] += float(outcome.get("mfe") or 0.0)
    bars_held = int(outcome.get("barsHeld") or 0)
    stats["_hold_sum"] += bars_held
    pct = outcome.get("pctReturn")
    if pct is not None:
        stats["_pct_sum"] += float(pct)
        stats["_pct_n"] += 1
    if outcome.get("beMoved"):
        stats["beMoved"] += 1
    if status == "target_hit":
        stats["targetHit"] += 1
        stats["_hold_win_sum"] += bars_held
    elif status == "stopped":
        stats["stopped"] += 1
        stats["_hold_loss_sum"] += bars_held
    elif status == "system_exit":
        stats["systemExit"] += 1
    elif status == "be_stop":
        stats["beStop"] += 1
    elif status == "triggered":
        stats["openAtEnd"] += 1
    # System-model win/loss ledger: every closed trade (stop, BE stop, or
    # Kijun-cross exit) resolves by the SIGN of its R — there is no fixed
    # target, so "win" means the trade closed positive. Breakeven scratches
    # (|R| ~ 0) count as neither.
    if outcome.get("exitReason") in ("stopped", "be_stop", "kijun_cross"):
        if r > 1e-9:
            stats["wins"] += 1
            stats["_r_win_sum"] += r
            stats["_hold_win_sum"] += bars_held
        elif r < -1e-9:
            stats["losses"] += 1
            stats["_r_loss_sum"] += r
            if status != "stopped":  # plain stops were already summed above
                stats["_hold_loss_sum"] += bars_held


def _finalize(stats: Dict[str, Any]) -> Dict[str, Any]:
    triggered = stats["triggered"]
    resolved = stats["targetHit"] + stats["stopped"]
    sys_resolved = stats["wins"] + stats["losses"]
    if sys_resolved:
        win_rate = round(100.0 * stats["wins"] / sys_resolved, 1)
        hold_win_n, hold_loss_n = stats["wins"], stats["losses"]
    else:
        win_rate = round(100.0 * stats["targetHit"] / resolved, 1) if resolved else None
        hold_win_n, hold_loss_n = stats["targetHit"], stats["stopped"]
    return {
        "signals": stats["signals"],
        "triggered": triggered,
        "targetHit": stats["targetHit"],
        "stopped": stats["stopped"],
        "expired": stats["expired"],
        "openAtEnd": stats["openAtEnd"],
        "systemExit": stats["systemExit"],
        "beStop": stats["beStop"],
        "gapSkipped": stats["gapSkipped"],
        "beMoved": stats["beMoved"],
        "triggerRate": round(100.0 * triggered / stats["signals"], 1) if stats["signals"] else None,
        "winRate": win_rate,
        "avgR": round(stats["_r_sum"] / triggered, 3) if triggered else None,
        "expectancy": round(stats["_r_sum"] / triggered, 3) if triggered else None,
        "avgWinR": round(stats["_r_win_sum"] / stats["wins"], 3) if stats["wins"] else None,
        "avgLossR": round(stats["_r_loss_sum"] / stats["losses"], 3) if stats["losses"] else None,
        "avgMae": round(stats["_mae_sum"] / triggered, 3) if triggered else None,
        "avgMfe": round(stats["_mfe_sum"] / triggered, 3) if triggered else None,
        "avgHoldBars": round(stats["_hold_sum"] / triggered, 1) if triggered else None,
        "avgHoldWin": round(stats["_hold_win_sum"] / hold_win_n, 1) if hold_win_n else None,
        "avgHoldLoss": round(stats["_hold_loss_sum"] / hold_loss_n, 1) if hold_loss_n else None,
        "avgPctReturn": round(stats["_pct_sum"] / stats["_pct_n"], 3) if stats["_pct_n"] else None,
    }


def evaluate_close_entry_outcome(
    *,
    direction: str,
    entry_price: float,
    stop_loss: float,
    target_1: float,
    forward_bars: List[DailyBar],
    max_hold: int = 10,
) -> Dict[str, Any]:
    """Resolve a setup entered AT THE SIGNAL BAR'S CLOSE (the Close Preview
    workflow: the desk takes the trade in the last minutes of the session as
    the daily candle finishes forming).

    No trigger window — the entry is the close itself. Risk is measured from
    the actual fill (close) to the stop, so R multiples are anchored to what
    the desk really risked. Same conservative same-bar tie-break as the
    trigger model (stop assumed to fill first). ``pctReturn`` is the signed
    percentage move from entry to exit.
    """
    is_bull = direction == "bullish"
    entry_price = float(entry_price)
    risk = abs(entry_price - stop_loss)
    out: Dict[str, Any] = {
        "status": "expired",
        "triggered": False,
        "rMultiple": 0.0,
        "barsHeld": 0,
        "mae": 0.0,
        "mfe": 0.0,
        "exitPrice": None,
        "pctReturn": None,
    }
    if risk <= 0 or entry_price <= 0 or not forward_bars:
        return out

    def _pct(exit_px: float) -> float:
        move = (exit_px - entry_price) if is_bull else (entry_price - exit_px)
        return round(100.0 * move / entry_price, 3)

    out["triggered"] = True
    mae_r = 0.0
    mfe_r = 0.0
    j_end = min(len(forward_bars), max_hold)
    for j in range(j_end):
        b = forward_bars[j]
        if b.high is None or b.low is None:
            continue
        hi, lo = float(b.high), float(b.low)
        bars_held = j + 1

        fav = ((hi - entry_price) if is_bull else (entry_price - lo)) / risk
        adv = ((entry_price - lo) if is_bull else (hi - entry_price)) / risk
        mfe_r = max(mfe_r, fav)
        mae_r = max(mae_r, adv)

        stop_hit = (lo <= stop_loss) if is_bull else (hi >= stop_loss)
        target_hit = (hi >= target_1) if is_bull else (lo <= target_1)

        if stop_hit:
            out.update({
                "status": "stopped", "rMultiple": -1.0, "barsHeld": bars_held,
                "exitPrice": round(stop_loss, 4), "pctReturn": _pct(stop_loss),
                "mae": round(mae_r, 3), "mfe": round(mfe_r, 3),
            })
            return out
        if target_hit:
            r_target = abs(target_1 - entry_price) / risk
            out.update({
                "status": "target_hit", "rMultiple": round(r_target, 3), "barsHeld": bars_held,
                "exitPrice": round(target_1, 4), "pctReturn": _pct(target_1),
                "mae": round(mae_r, 3), "mfe": round(mfe_r, 3),
            })
            return out

    # Time stop: mark-to-close at the last managed bar.
    last = forward_bars[j_end - 1]
    last_close = float(last.close) if last.close is not None else entry_price
    r_mult = ((last_close - entry_price) if is_bull else (entry_price - last_close)) / risk
    out.update({
        "status": "triggered",
        "rMultiple": round(r_mult, 3),
        "barsHeld": j_end,
        "exitPrice": round(last_close, 4),
        "pctReturn": _pct(last_close),
        "mae": round(mae_r, 3),
        "mfe": round(mfe_r, 3),
    })
    return out


_KIJUN_PERIOD = 26  # standard Ichimoku 9/26/52 — must match the engine


def _kijun_at(bars: List[DailyBar], idx: int, period: int = _KIJUN_PERIOD) -> Optional[float]:
    """Kijun-sen at bar `idx`: midpoint of the high/low range over `period` bars."""
    lo = idx - period + 1
    if lo < 0:
        return None
    highs = [float(b.high) for b in bars[lo: idx + 1] if b.high is not None]
    lows = [float(b.low) for b in bars[lo: idx + 1] if b.low is not None]
    if len(highs) < period or len(lows) < period:
        return None
    return (max(highs) + min(lows)) / 2.0


def evaluate_system_outcome(
    *,
    direction: str,
    entry_trigger: float,
    stop_loss: float,
    bars: List[DailyBar],
    signal_idx: int,
    trigger_window: int = 3,
    max_gap_r: float = 1.0,
    be_at_r: float = 1.0,
    max_hold: int = 60,
) -> Dict[str, Any]:
    """Full Ichimoku-system trade simulation (the desk's third entry model).

    Day 0 is the confirmation candle (the signal bar). Entry is a resting
    stop order at the trigger from day 1, with realistic gap handling:
    - Opens through the trigger fill AT THE OPEN (a stop order can't fill at
      a price the market never traded).
    - Opens more than ``max_gap_r`` × initial risk beyond the trigger cancel
      the order — the desk doesn't chase an invalidating gap.

    Exits are what the system says, not a fixed R:R target:
    - Protective stop at the playbook stop (gap-aware fill at the open when
      gapped through).
    - Once price reaches ``be_at_r`` × risk of favorable excursion intrabar,
      the stop moves to breakeven — effective the NEXT bar (the intrabar
      sequence is unknowable from daily data).
    - Trend exit when the daily close crosses the live 26-bar Kijun against
      the trade; filled at that close. No profit target — winners run.
    - ``max_hold`` (default 60 bars) is a safety cap, not a strategy rule.

    R multiples are anchored to the ACTUAL fill (open on gap entries), so a
    worse fill honestly shrinks the reward and grows the risk.
    """
    is_bull = direction == "bullish"
    risk0 = abs(entry_trigger - stop_loss)
    out: Dict[str, Any] = {
        "status": "expired",
        "triggered": False,
        "rMultiple": 0.0,
        "barsHeld": 0,
        "mae": 0.0,
        "mfe": 0.0,
        "exitPrice": None,
        "pctReturn": None,
        "entryFill": None,
        "gapSkipped": False,
        "beMoved": False,
        "exitReason": None,
    }
    if risk0 <= 0 or signal_idx + 1 >= len(bars):
        return out

    # 1) Entry: stop order at the trigger within the trigger window.
    fill: Optional[float] = None
    entry_j: Optional[int] = None
    for k in range(trigger_window):
        j = signal_idx + 1 + k
        if j >= len(bars):
            break
        b = bars[j]
        if b.open is None or b.high is None or b.low is None:
            continue
        o, h, l = float(b.open), float(b.high), float(b.low)
        gapped_through = (o >= entry_trigger) if is_bull else (o <= entry_trigger)
        if gapped_through:
            gap_r = abs(o - entry_trigger) / risk0
            if gap_r > max_gap_r:
                out["gapSkipped"] = True
                return out  # order canceled — gap invalidates the entry
            fill, entry_j = o, j
            break
        traded_through = (h >= entry_trigger) if is_bull else (l <= entry_trigger)
        if traded_through:
            fill, entry_j = entry_trigger, j
            break
    if fill is None or entry_j is None:
        return out  # never triggered

    risk = abs(fill - stop_loss)
    if risk <= 0:
        return out
    out["triggered"] = True
    out["entryFill"] = round(fill, 4)

    def _r(px: float) -> float:
        move = (px - fill) if is_bull else (fill - px)
        return round(move / risk, 3)

    def _pct(px: float) -> float:
        move = (px - fill) if is_bull else (fill - px)
        return round(100.0 * move / fill, 3)

    def _close_out(status: str, px: float, bars_held: int, reason: str) -> Dict[str, Any]:
        out.update({
            "status": status, "rMultiple": _r(px), "barsHeld": bars_held,
            "exitPrice": round(px, 4), "pctReturn": _pct(px),
            "mae": round(mae_r, 3), "mfe": round(mfe_r, 3), "exitReason": reason,
        })
        return out

    stop = stop_loss
    be_active = False
    be_armed = False
    mae_r = 0.0
    mfe_r = 0.0
    last_j = min(len(bars) - 1, entry_j + max_hold - 1)

    for j in range(entry_j, last_j + 1):
        b = bars[j]
        if b.high is None or b.low is None or b.close is None:
            continue
        o = float(b.open) if b.open is not None else None
        h, l, c = float(b.high), float(b.low), float(b.close)
        bars_held = j - entry_j + 1

        # A breakeven move armed on a previous bar becomes effective now.
        if be_armed and not be_active:
            be_active = True
            stop = max(stop, fill) if is_bull else min(stop, fill)
            out["beMoved"] = True

        fav = ((h - fill) if is_bull else (fill - l)) / risk
        adv = ((fill - l) if is_bull else (h - fill)) / risk
        mfe_r = max(mfe_r, fav)
        mae_r = max(mae_r, adv)

        # 1) Protective stop, gap-aware. The entry bar skips the open check —
        #    the entry itself happened intraday, after the open.
        stop_px: Optional[float] = None
        if is_bull:
            if j > entry_j and o is not None and o <= stop:
                stop_px = o
            elif l <= stop:
                stop_px = stop
        else:
            if j > entry_j and o is not None and o >= stop:
                stop_px = o
            elif h >= stop:
                stop_px = stop
        if stop_px is not None:
            return _close_out("be_stop" if be_active else "stopped", stop_px, bars_held,
                              "be_stop" if be_active else "stopped")

        # 2) Arm breakeven once favorable excursion reaches be_at_r intrabar.
        if not be_armed and fav >= be_at_r:
            be_armed = True

        # 3) System exit: daily close crosses the Kijun against the trade.
        kij = _kijun_at(bars, j)
        if kij is not None and ((c < kij) if is_bull else (c > kij)):
            return _close_out("system_exit", c, bars_held, "kijun_cross")

    # Safety cap / end of data: mark to the last managed close.
    last = bars[last_j]
    c = float(last.close) if last.close is not None else fill
    return _close_out("triggered", c, last_j - entry_j + 1, "open_at_end")


_DOW_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri")


def _dow_name(date_str: str) -> Optional[str]:
    try:
        return _DOW_NAMES[dt.date.fromisoformat(date_str[:10]).weekday()]
    except Exception:
        return None


def backtest_from_bars(
    bars_by_ticker: Dict[str, List[DailyBar]],
    *,
    min_score: float = 0.0,
    warmup: int = 80,
    trigger_window: int = 3,
    max_hold: int = 10,
    entry_model: str = "trigger",
) -> Dict[str, Any]:
    """Pure walk-forward backtest over pre-fetched bars, all playbooks.

    Entry models:
    - ``trigger`` (default): stop order at the signal's entry trigger, filled
      within ``trigger_window`` bars or expired. Structure signals included
      (their cohort is reported separately).
    - ``close``: filled at the signal bar's CLOSE — the Close Preview
      workflow, entering in the final minutes of the session. Only signals
      that are ACTIONABLE on the signal bar are taken (the desk doesn't
      close-enter a stale/extended structure name).
    - ``system``: day 0 is the confirmation candle, day 1+ a resting stop
      order at the trigger with gap-realistic fills (fill at the open when
      gapped through; cancel when the gap exceeds 1R). No profit target —
      breakeven stop after +1R, then the trade runs until the daily close
      crosses the Kijun against it. Actionable signals only, like ``close``.

    Each distinct (ticker, playbook, signalDate) is counted once — the
    freshness window re-surfaces the same trigger bar for a few sessions, so
    we dedupe to avoid double-counting a single trade. Every playbook uses
    the same outcome evaluator, so the ``byPlaybook`` cohorts are directly
    comparable. ``byDow`` cohorts key on the entry day of week (= signal-bar
    day for the close model).
    """
    model = str(entry_model).lower()
    close_entry = model == "close"
    system_model = model == "system"
    overall = _blank_stats()
    by_grade: Dict[str, Dict[str, Any]] = {}
    by_bucket: Dict[str, Dict[str, Any]] = {}
    by_playbook: Dict[str, Dict[str, Any]] = {}
    by_dow: Dict[str, Dict[str, Any]] = {}
    by_playbook_dow: Dict[str, Dict[str, Dict[str, Any]]] = {}
    # Close model only: did the signal candle CLOSE through the trigger level
    # (the entry is effectively tomorrow's stop-fill taken a day early) or is
    # the close still on the near side (entering before confirmation)?
    by_close_vs_trigger: Dict[str, Dict[str, Any]] = {}
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
                if (close_entry or system_model) and signal.freshness_bucket != "actionable":
                    continue  # these models only trade actionable-on-signal-day names

                seen_keys.add(key)
                forward = bars[i + 1:]
                close_pos: Optional[str] = None
                if system_model:
                    outcome = evaluate_system_outcome(
                        direction=signal.direction,
                        entry_trigger=signal.entry_trigger,
                        stop_loss=signal.stop_loss,
                        bars=bars,
                        signal_idx=i,
                        trigger_window=trigger_window,
                        max_hold=max_hold,
                    )
                elif close_entry:
                    entry_px = float(window[-1].close) if window[-1].close is not None else None
                    if not entry_px or entry_px <= 0:
                        continue
                    is_bull = signal.direction == "bullish"
                    through = (entry_px >= signal.entry_trigger) if is_bull else (entry_px <= signal.entry_trigger)
                    close_pos = "throughTrigger" if through else "beforeTrigger"
                    outcome = evaluate_close_entry_outcome(
                        direction=signal.direction,
                        entry_price=entry_px,
                        stop_loss=signal.stop_loss,
                        target_1=signal.target_1,
                        forward_bars=forward,
                        max_hold=max_hold,
                    )
                else:
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
                dow = _dow_name(sig_date)
                if dow:
                    by_dow.setdefault(dow, _blank_stats())
                    _record(by_dow[dow], outcome)
                    by_playbook_dow.setdefault(playbook, {}).setdefault(dow, _blank_stats())
                    _record(by_playbook_dow[playbook][dow], outcome)
                if close_pos:
                    by_close_vs_trigger.setdefault(close_pos, _blank_stats())
                    _record(by_close_vs_trigger[close_pos], outcome)
        if had_signal:
            tickers_with_signals += 1

    def _dow_sorted(d: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        return {k: _finalize(d[k]) for k in _DOW_NAMES if k in d}

    return {
        "overall": _finalize(overall),
        "byGrade": {g: _finalize(s) for g, s in sorted(by_grade.items())},
        "byBucket": {b: _finalize(s) for b, s in sorted(by_bucket.items())},
        "byPlaybook": {p: _finalize(s) for p, s in sorted(by_playbook.items())},
        "byDow": _dow_sorted(by_dow),
        "byPlaybookDow": {p: _dow_sorted(d) for p, d in sorted(by_playbook_dow.items())},
        "byCloseVsTrigger": {k: _finalize(s) for k, s in sorted(by_close_vs_trigger.items())},
        "params": {
            "minScore": min_score,
            "warmup": warmup,
            "triggerWindow": trigger_window,
            "maxHold": max_hold,
            "entryModel": "system" if system_model else ("close" if close_entry else "trigger"),
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
    entry_model: str = "trigger",
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
        entry_model=entry_model,
    )
    result["window"] = {"start": start.isoformat(), "end": end.isoformat()}
    return result
