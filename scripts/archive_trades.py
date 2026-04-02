#!/usr/bin/env python3
"""Archive trade data from Redis to JSON files for durability.

Run weekly via cron (Sunday evening) to ensure trade data survives
Redis TTL expiration and is available for long-term analysis.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.redis_store import get_store_optional
from backend.trade_memory import archive_trades_to_json


def main():
    store = get_store_optional()
    if store is None:
        print("Redis unavailable — skipping archive.")
        return

    from backend.engine2_trades import list_all_trades
    e2_trades = list_all_trades(store=store)
    if e2_trades:
        path = archive_trades_to_json(e2_trades, engine="e2")
        print(f"E2 archive: {len(e2_trades)} trades -> {path}")

    from backend.e1_earnings_trades import list_closed_trades, list_active_trades
    e1_closed = list_closed_trades(store=store, limit=500)
    e1_active = list_active_trades(store=store)
    e1_all = e1_closed + e1_active
    if e1_all:
        path = archive_trades_to_json(e1_all, engine="e1")
        print(f"E1 archive: {len(e1_all)} trades -> {path}")


if __name__ == "__main__":
    main()
