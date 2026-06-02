#!/usr/bin/env python3
"""Raven-Tech – Sequencer Breadth State Refresh (cron wrapper).

Computes three breadth/dispersion metrics for the pattern library sequencer
and persists them to Redis so the Engine 5 sequencer emitter can detect
state changes.

Metrics:
  - earnings_dispersion: How many names report earnings in the next 5 days
  - red_dog_breadth:     % of universe with Red Dog setups (Engine 3)
  - ichimoku_breadth:    % of universe with Ichimoku A+ signals (Engine 4)

Schedule (crontab):
  0 17 * * 1-5 cd /opt/breach-algo && python scripts/refresh_sequencer_state.py

Usage:
    python scripts/refresh_sequencer_state.py [--force]

Exit codes:
    0 = success
    1 = partial (some metrics failed)
    2 = fatal (Redis unavailable)
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

LOG = logging.getLogger("refresh_sequencer_state")

_STATE_TTL_S = 2 * 86400  # 2-day TTL


def _compute_earnings_dispersion(store) -> str:
    """Classify upcoming earnings density from the FMP snapshot already in Redis."""
    from backend.fmp_snapshot import FMP_EARNINGS_SNAPSHOT_KEY

    snap = store.get_json(FMP_EARNINGS_SNAPSHOT_KEY)
    if not snap or not isinstance(snap, dict):
        LOG.warning("FMP earnings snapshot not available in Redis")
        return ""

    by_date = snap.get("byDate", {})
    if not isinstance(by_date, dict):
        return ""

    today = dt.date.today()
    horizon = today + dt.timedelta(days=5)
    count = 0
    for date_str, timings in by_date.items():
        try:
            d = dt.date.fromisoformat(str(date_str)[:10])
        except (ValueError, TypeError):
            continue
        if today <= d <= horizon and isinstance(timings, dict):
            for tickers in timings.values():
                if isinstance(tickers, list):
                    count += len(tickers)

    if count <= 5:
        bucket = "quiet"
    elif count <= 20:
        bucket = "moderate"
    else:
        bucket = "heavy"

    LOG.info("Earnings dispersion: %d names in next 5 days -> %s", count, bucket)
    return bucket


def _compute_red_dog_breadth(orats_client) -> str:
    """Run Engine 3 universe scan and classify setup breadth."""
    from backend.engine3_screener import compute_engine3_scan

    result = compute_engine3_scan(orats_client, use_cache=False)
    scanned = result.get("scannedCount", 0)
    found = result.get("setupsFound", 0)
    ratio = found / max(1, scanned)

    if ratio < 0.05:
        bucket = "narrow"
    elif ratio < 0.15:
        bucket = "normal"
    else:
        bucket = "wide"

    LOG.info("Red Dog breadth: %d/%d (%.1f%%) -> %s", found, scanned, ratio * 100, bucket)
    return bucket


def _compute_ichimoku_breadth(orats_client) -> str:
    """Run Engine 4 universe scan and classify A+ breadth."""
    from backend.engine4_screener import run_universe_scan

    # persist=False: this is a breadth measurement only — it must not seed the
    # desk tracker with names nobody looked at (that was a source of stale
    # "pending" cards lingering for weeks).
    result = run_universe_scan(orats_client, use_cache=False, persist=False)
    scanned = result.get("scannedCount", 0)
    aplus = result.get("totalAPlus", 0)
    ratio = aplus / max(1, scanned)

    if ratio < 0.03:
        bucket = "narrow"
    elif ratio < 0.10:
        bucket = "normal"
    else:
        bucket = "wide"

    LOG.info("Ichimoku breadth: %d/%d (%.1f%%) -> %s", aplus, scanned, ratio * 100, bucket)
    return bucket


def main() -> int:
    from backend.redis_store import get_store_optional

    store = get_store_optional()
    if not store:
        LOG.error("Redis not available. Exiting.")
        return 2

    partial = False

    # 1. Earnings dispersion (lightweight Redis read)
    try:
        dispersion = _compute_earnings_dispersion(store)
        if dispersion:
            store.set_json("sequencer:state:earnings_dispersion", dispersion, ttl_s=_STATE_TTL_S)
        else:
            partial = True
    except Exception as e:
        LOG.warning("Earnings dispersion failed: %s", e)
        partial = True

    # 2. Red Dog breadth (Engine 3 scan — requires ORATS)
    orats_client = None
    try:
        from backend.orats_client import OratsClient
        orats_client = OratsClient.from_env()
    except Exception as e:
        LOG.warning("ORATS client unavailable; skipping Engine 3/4 scans: %s", e)

    if orats_client:
        try:
            breadth = _compute_red_dog_breadth(orats_client)
            if breadth:
                store.set_json("sequencer:state:red_dog_breadth", breadth, ttl_s=_STATE_TTL_S)
            else:
                partial = True
        except Exception as e:
            LOG.warning("Red Dog breadth failed: %s", e)
            partial = True

        # 3. Ichimoku breadth (Engine 4 scan)
        try:
            ichi = _compute_ichimoku_breadth(orats_client)
            if ichi:
                store.set_json("sequencer:state:ichimoku_breadth", ichi, ttl_s=_STATE_TTL_S)
            else:
                partial = True
        except Exception as e:
            LOG.warning("Ichimoku breadth failed: %s", e)
            partial = True
    else:
        partial = True

    if partial:
        LOG.warning("Sequencer state refresh completed with partial failures")
    else:
        LOG.info("Sequencer state refresh completed successfully")

    return 1 if partial else 0


if __name__ == "__main__":
    raise SystemExit(main())
