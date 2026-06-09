"""Strategy #1 — Post-Earnings Announcement Drift (PEAD), base anomaly.

Thesis: stocks drift in the direction of their earnings surprise for weeks; the
market underreacts to the magnitude/quality of the beat or miss. We trade the
sign of the standardized EPS surprise and hold ~2 weeks.

Point-in-time discipline:
  * Direction and the tradeable filter use only the surprise known at the report
    (a fixed |surprise| threshold), so signals are knowable in real time.
  * ``signal_date`` = report_date. The harness enters the *next* session, which
    is correct for AMC reports (after the close) and conservative for BMO.

The descriptive ``surprise_bucket`` tag uses fixed absolute bins (not future
quantiles) so it is also point-in-time safe.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from backend.research.data_provider import EarningsProvider
from backend.research.event_study import SignalEvent


def _surprise_bucket(s: float) -> str:
    a = abs(s)
    if s > 0:
        return "beat_large" if a >= 0.20 else "beat_small"
    return "miss_large" if a >= 0.20 else "miss_small"


def generate_pead_events(
    earnings_provider: EarningsProvider,
    tickers: Sequence[str],
    start: str,
    end: str,
    *,
    min_abs_surprise: float = 0.05,
    horizon_days: int = 10,
    long_only: bool = False,
    strategy_name: str = "PEAD",
) -> List[SignalEvent]:
    """Generate PEAD drift signals.

    min_abs_surprise: ignore surprises smaller than this (noise floor).
    long_only: if True, only trade beats (long); else trade misses short too.
    """
    events: List[SignalEvent] = []
    for ticker in tickers:
        try:
            earns = earnings_provider.get_events(ticker, start, end)
        except Exception:
            continue
        for e in earns:
            surprise: Optional[float] = e.eps_surprise_pct()
            if surprise is None or abs(surprise) < min_abs_surprise:
                continue
            direction = 1 if surprise > 0 else -1
            if long_only and direction < 0:
                continue
            events.append(
                SignalEvent(
                    ticker=ticker.upper(),
                    signal_date=e.report_date,
                    direction=direction,
                    horizon_days=horizon_days,
                    strategy=strategy_name,
                    tags={
                        "surprise_bucket": _surprise_bucket(surprise),
                        "surprise_sign": "beat" if direction > 0 else "miss",
                        "timing": e.timing or "unknown",
                    },
                    meta={"eps_surprise": round(surprise, 6)},
                )
            )
    return events
