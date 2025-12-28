#!/usr/bin/env python3

from __future__ import annotations

import os
import sys
import sys

try:
    from dotenv import load_dotenv  # type: ignore

    # In droplet deployments, env vars are usually set by systemd; this is a local-dev helper.
    load_dotenv()
except Exception:
    pass

# Ensure repo root is on sys.path when running as a script (cron-friendly).
# When executed as `python3 scripts/refresh_calendar_snapshot.py`, Python adds the
# script directory (`.../scripts`) to sys.path, not the repo root.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.orats_client import OratsClient
from backend.redis_store import get_store_optional
from backend.calendar_snapshot import refresh_earnings_snapshot_if_needed


def main() -> int:
    store = get_store_optional()
    if store is None:
        print("Missing REDIS_URL; cannot refresh calendar snapshot.", file=sys.stderr)
        return 2
    if not store.ping():
        print("Redis ping failed; cannot refresh calendar snapshot.", file=sys.stderr)
        return 3

    client = OratsClient.from_env()
    res = refresh_earnings_snapshot_if_needed(client, store)
    # Print concise output suitable for cron logs.
    print(
        f"ok={res.ok} etDate={res.etDate} universe={res.universeSize} "
        f"oratsCalls={res.oratsCalls} rowsUsed={res.rowsUsed} byDate={res.byDateSize} errors={res.errors} "
        f"notes={' '.join(res.notes)}"
    )
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())


