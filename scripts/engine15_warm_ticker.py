#!/usr/bin/env python3
"""Warm the Engine 15 earnings chain cache for a single ticker.

Runs Engine 1 (``compute_breach_stats``) to harvest the ticker's last
~20 earnings events, then backfills the ORATS historical option chain
slice for a small business-day window around each one via
:mod:`backend.engine15.chain_backfill`.

Usage:
    python scripts/engine15_warm_ticker.py GE
    python scripts/engine15_warm_ticker.py AAPL --n 12 --years 4
    python scripts/engine15_warm_ticker.py AMZN --days-before 1 --days-after 2
    python scripts/engine15_warm_ticker.py GE --force         # re-fetch cached days
    python scripts/engine15_warm_ticker.py GE --plan-only     # print plan; don't fetch

Idempotent: re-running skips trade dates already in the Engine 14
chain-cache manifest.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys

_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backend.deps import get_client  # noqa: E402
from backend.earnings_logic import compute_breach_stats  # noqa: E402
from backend.engine15 import chain_backfill  # noqa: E402

LOG = logging.getLogger("engine15.warm")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def _on_progress(payload):
    stage = payload.get("stage", "?")
    if stage == "fetching":
        LOG.info(
            "  [%d/%d] %s ok=%d fail=%d",
            payload.get("completed", 0),
            payload.get("total", 0),
            payload.get("lastDate"),
            payload.get("succeeded", 0),
            payload.get("failed", 0),
        )
    elif stage == "planned":
        plan = payload.get("plan", {})
        LOG.info(
            "plan: %d events, %d dates to fetch, %d already cached, %d non-trading",
            plan.get("events", 0),
            len(plan.get("datesToFetch", [])),
            len(plan.get("datesAlreadyCached", [])),
            len(plan.get("datesSkippedNonTrading", [])),
        )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Warm Engine 15 earnings chain cache for a ticker.")
    ap.add_argument("ticker", help="Equity ticker (e.g. GE).")
    ap.add_argument("--n", type=int, default=20, help="Max earnings events to harvest from Engine 1.")
    ap.add_argument("--years", type=int, default=5, help="Years of history to scan for earnings.")
    ap.add_argument("--days-before", type=int, default=1, help="Biz days before earnings to cache.")
    ap.add_argument("--days-after", type=int, default=2, help="Biz days after earnings to cache.")
    ap.add_argument("--max-dte", type=int, default=45, help="Max DTE slice to pull per day.")
    ap.add_argument("--delay-ms", type=int, default=200, help="ms to sleep between ORATS fetches.")
    ap.add_argument("--force", action="store_true", help="Re-fetch even if date already cached.")
    ap.add_argument("--plan-only", action="store_true", help="Print the plan; don't issue ORATS calls.")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    _configure_logging(args.verbose)

    ticker = args.ticker.strip().upper()
    LOG.info("engine15 warm start: ticker=%s", ticker)

    client = get_client()
    LOG.info("fetching Engine 1 breach stats for %s...", ticker)
    e1 = compute_breach_stats(
        client=client,
        ticker=ticker,
        n=int(args.n),
        years=int(args.years),
    )
    events = list(e1.get("events") or [])
    if not events:
        LOG.error("No earnings events found for %s — cannot warm.", ticker)
        return 2
    LOG.info("harvested %d earnings events (most recent: %s)", len(events), events[0].get("earnDate"))

    if args.plan_only:
        plan = chain_backfill.plan_event_backfill(
            ticker=ticker,
            events=events,
            days_before=int(args.days_before),
            days_after=int(args.days_after),
            force=bool(args.force),
        )
        print(json.dumps(plan.to_dict(), indent=2, default=str))
        return 0

    result = chain_backfill.backfill_ticker_events(
        client,
        ticker=ticker,
        earnings_events=events,
        days_before=int(args.days_before),
        days_after=int(args.days_after),
        max_dte=int(args.max_dte),
        delay_ms=int(args.delay_ms),
        force=bool(args.force),
        on_progress=_on_progress,
    )
    LOG.info(
        "done: attempted=%d succeeded=%d failed=%d cached=%d",
        result.get("attempted", 0),
        result.get("succeeded", 0),
        result.get("failed", 0),
        result.get("skippedAlreadyCached", 0),
    )
    cov = result.get("coverage") or {}
    LOG.info(
        "coverage after: days=%d rows=%d range=[%s..%s]",
        cov.get("daysCovered", 0),
        cov.get("totalRows", 0),
        cov.get("minDate"),
        cov.get("maxDate"),
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        LOG.warning("interrupted")
        raise SystemExit(130)
