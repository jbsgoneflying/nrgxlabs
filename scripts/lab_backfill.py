"""Cron-safe incremental daily ingest for the Equity Repricing Lab.

Designed for ``deploy/crontab`` after the close (23:30 UTC weekdays).
Idempotent: upserts by natural keys; overlaps the last 5 sessions so a
failed day self-heals on the next run.

Honours ``REPRICING_LAB_ENABLED`` — exits 0 without work when the flag is off.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOG = logging.getLogger("lab_backfill")


def _default_symbols() -> list[str]:
    # Bootstrap liquid core for incremental nights until full universe backfill.
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "universe", "sp500.txt",
    )
    if not os.path.exists(path):
        return ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM", "XOM"]
    out = []
    with open(path) as fh:
        for line in fh:
            t = line.strip().upper()
            if t and not t.startswith("#"):
                out.append(t)
    return out[:80]  # rate-limit friendly incremental slice


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from backend.research.env_loader import load_research_env
    load_research_env()

    from backend.config import get_flags
    flags = get_flags()
    if not flags.REPRICING_LAB_ENABLED:
        LOG.info("REPRICING_LAB_ENABLED=0 — skipping incremental backfill")
        return 0

    from backend.eodhd_client import EodhdClient
    from backend.repricing_lab import store
    from backend.repricing_lab.bars import backfill_symbol_bars
    from backend.repricing_lab.corporate_actions import backfill_corporate_actions
    from backend.repricing_lab.universe_pit import build_universe_snapshot

    today = dt.date.today()
    from_date = (today - dt.timedelta(days=10)).isoformat()  # ~5 sessions overlap
    to_date = today.isoformat()
    client = EodhdClient.from_env()
    symbols = _default_symbols()
    summary = {"bars": 0, "splits": 0, "divs": 0, "universe": 0, "symbols": len(symbols)}

    with store.connect() as conn:
        started = store.record_job_start(conn, "lab_backfill_incremental")
        try:
            for sym in symbols:
                try:
                    summary["bars"] += backfill_symbol_bars(
                        conn, client, sym, from_date=from_date, to_date=to_date,
                    )
                    ns, nd = backfill_corporate_actions(
                        conn, client, sym, from_date=from_date, to_date=to_date,
                    )
                    summary["splits"] += ns
                    summary["divs"] += nd
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("symbol %s failed: %s", sym, exc)
            summary["universe"] = build_universe_snapshot(conn, snapshot_date=to_date)
            store.record_job_finish(conn, "lab_backfill_incremental", started, ok=True, detail=summary)
        except Exception as exc:
            store.record_job_finish(
                conn, "lab_backfill_incremental", started, ok=False, detail={"error": str(exc)},
            )
            raise

    print(json.dumps({"ok": True, **summary}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
