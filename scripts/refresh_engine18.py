#!/usr/bin/env python3
"""Engine 18 (Earnings Drift / PEAD) — morning refresh (cron wrapper).

Runs the full scan once and writes the snapshot to Redis:

  EODHD calendar (yesterday-AMC + today-BMO actuals, desk universe)
    -> liquidity floor (EODHD bars)
    -> transcript fetch (API Ninjas)
    -> LLM quality grade (OpenAI primary, heuristic fallback, both logged)
    -> deterministic bucket x quintile -> sizing tier
    -> Redis (e18:scan:latest + e18:evidence:{ticker} + e18:grades:*)

Scheduled 12:45 UTC (7:45 ET) weekdays — after most BMO actuals print,
before the open, so entries can go on at the next session open per the
validated backtest mechanics.

Usage:
    python scripts/refresh_engine18.py
"""
from __future__ import annotations

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

LOG = logging.getLogger("refresh_engine18")


def main() -> int:
    from backend.config import get_flags
    from backend.engine18 import pipeline

    flags = get_flags()
    if not getattr(flags, "ENABLE_ENGINE18", False):
        LOG.warning("ENABLE_ENGINE18 is OFF — building the scan anyway so evidence accrues, "
                    "but the /api/engine18 route will 404 until the flag is enabled.")

    LOG.info("Engine 18 refresh starting (lookback=%dd, min_surprise=%.0f%%)",
             flags.ENGINE18_LOOKBACK_DAYS, flags.ENGINE18_MIN_SURPRISE * 100)

    payload = pipeline.build_scan(flags=flags)

    summary = payload.get("summary", {})
    LOG.info("Engine 18 refresh done: %d candidates (%d actionable: %d full / %d half).",
             summary.get("candidates", 0), summary.get("actionable", 0),
             summary.get("fullSize", 0), summary.get("halfSize", 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
