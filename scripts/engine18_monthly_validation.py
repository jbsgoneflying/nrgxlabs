#!/usr/bin/env python3
"""Engine 18 (Earnings Drift / PEAD) — monthly continuous validation.

The gold-standard part: the engine measures its OWN edge decay. Monthly cron:

  1. Replays the trailing 6 and 12 months of PEAD signals through the same
     point-in-time event study the edge was validated with
     (``backend/research/event_study.py``, live EODHD providers, long-only,
     +5% surprise floor, 10-trading-day hold, 10bps/side).
  2. Summarizes closed desk-tracker trades (e18:trades) for live-vs-backtest
     comparison.
  3. Writes ``e18:validation:latest``. If the rolling 6-month avg net return
     is negative, the record carries ``degraded: true`` — the page shows a
     DEGRADED banner and Desk Brain excludes the engine from the book until
     the desk reviews it.

Usage:
    python scripts/engine18_monthly_validation.py [--months 6]
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)

LOG = logging.getLogger("engine18_validation")

# Exit horizon cushion: signals younger than ~3 calendar weeks haven't
# completed their 10-trading-day hold, so the replay window ends back then.
_HOLD_CUSHION_DAYS = 21


def _replay_window(months: int) -> tuple:
    end = dt.date.today() - dt.timedelta(days=_HOLD_CUSHION_DAYS)
    start = end - dt.timedelta(days=int(months * 30.44))
    return start.isoformat(), end.isoformat()


def _run_replay(months: int, flags) -> dict:
    """Replay trailing signals through the validated event study. Pure backtest math."""
    from backend.research.cohort_stats import summarize
    from backend.research.cost_model import CostModel
    from backend.research.event_study import run_event_study
    from backend.research.live_providers import EodhdEarningsProvider, EodhdPriceProvider
    from backend.research.strategies.pead import generate_pead_events
    from backend.engine18.ingest import load_universe

    start, end = _replay_window(months)
    tickers = load_universe()
    earnings = EodhdEarningsProvider()
    prices = EodhdPriceProvider()

    events = generate_pead_events(
        earnings, tickers, start, end,
        min_abs_surprise=float(flags.ENGINE18_MIN_SURPRISE),
        horizon_days=int(flags.ENGINE18_HOLD_DAYS),
        long_only=True,
    )
    LOG.info("replay %dm: %d signals in %s..%s", months, len(events), start, end)
    outcome = run_event_study(events, prices, cost_model=CostModel(per_side_bps=10.0))
    stats = summarize(outcome.trades)
    return {
        "months": months,
        "windowStart": start,
        "windowEnd": end,
        "n": stats.n_trades,
        "avgNetPct": round(stats.avg_net_return * 100, 3),
        "hitRate": round(stats.hit_rate, 3),
        "tStat": round(stats.t_stat, 2),
        "skipped": len(outcome.skipped),
    }


def _closed_tracker_summary() -> dict:
    """Live closed-trade P&L from the desk tracker (small n early on)."""
    from backend.engine18 import trades as e18_trades

    closed = e18_trades.list_trades(status="closed")
    rets = [
        t["outcome"]["returnPct"]
        for t in closed
        if isinstance(t.get("outcome"), dict) and t["outcome"].get("returnPct") is not None
    ]
    return {
        "nClosed": len(closed),
        "nWithReturns": len(rets),
        "avgReturnPct": round(sum(rets) / len(rets) * 100, 3) if rets else None,
        "hitRate": round(sum(1 for r in rets if r > 0) / len(rets), 3) if rets else None,
    }


def main() -> int:
    from backend.config import get_flags
    from backend.engine18 import store
    from backend.engine18.models import utcnow_iso

    flags = get_flags()
    six = _run_replay(6, flags)
    twelve = _run_replay(12, flags)
    tracker = _closed_tracker_summary()

    degraded = six["n"] >= 20 and six["avgNetPct"] < 0
    record = {
        "asOf": utcnow_iso(),
        "rolling6mAvgNetPct": six["avgNetPct"],
        "n6m": six["n"],
        "hit6m": six["hitRate"],
        "t6m": six["tStat"],
        "rolling12mAvgNetPct": twelve["avgNetPct"],
        "n12m": twelve["n"],
        "hit12m": twelve["hitRate"],
        "t12m": twelve["tStat"],
        "degraded": bool(degraded),
        "replay": {"6m": six, "12m": twelve},
        "liveTracker": tracker,
        "baseline": "edge-bakeoff OOS 2023+ +0.65%/trade t=2.7 n=843",
    }
    ok = store.set_validation(record)
    LOG.info(
        "validation written (redis=%s): 6m %+0.3f%%/trade (n=%d)%s | 12m %+0.3f%% (n=%d) | live closed n=%d",
        ok, six["avgNetPct"], six["n"], " DEGRADED" if degraded else "",
        twelve["avgNetPct"], twelve["n"], tracker["nClosed"],
    )
    if degraded:
        LOG.warning("ROLLING EDGE DEGRADED: 6-month avg net %.3f%% < 0 — page shows DEGRADED, "
                    "Desk Brain excludes Engine 18 until the desk reviews.", six["avgNetPct"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
