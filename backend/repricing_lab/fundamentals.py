"""Fundamentals / estimate self-archiver snapshots."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from backend.repricing_lab import store
from backend.repricing_lab.instruments import eodhd_symbol, ensure_instrument

LOG = logging.getLogger("repricing_lab.fundamentals")


def snapshot_fundamentals(conn, client, symbol: str, *, as_of: Optional[str] = None) -> int:
    iid = ensure_instrument(conn, symbol)
    eod_sym = eodhd_symbol(iid)
    data = client.get_fundamentals(eod_sym) or {}
    now = as_of or store.utcnow_iso()
    general = data.get("General") or {}
    shares = data.get("SharesStats") or {}
    highlights = data.get("Highlights") or {}
    row = {
        "instrument_id": iid,
        "as_of": now[:10],
        "shares_outstanding": _f(shares.get("SharesOutstanding")),
        "float_shares": _f(shares.get("SharesFloat")),
        "market_cap": _f(highlights.get("MarketCapitalization") or general.get("MarketCapitalization")),
        "sector": general.get("Sector"),
        "industry": general.get("Industry"),
        "detail_json": json.dumps({
            "PercentInsiders": shares.get("PercentInsiders"),
            "PercentInstitutions": shares.get("PercentInstitutions"),
        }, sort_keys=True),
        "source": "eodhd",
        "available_at": now if "T" in now else f"{now}T23:59:59Z",
    }
    return store.upsert(conn, "fundamental_snapshot", [row])


def archive_estimate(
    conn,
    *,
    instrument_id: str,
    metric: str,
    fiscal_period: str,
    consensus_value: Optional[float],
    analyst_count: Optional[int] = None,
    as_of: Optional[str] = None,
    source: str = "eodhd_self_archive",
) -> int:
    """Forward-archive a consensus estimate so future research is PIT-safe."""
    now = as_of or store.utcnow_iso()
    row = {
        "instrument_id": instrument_id,
        "metric": metric,
        "fiscal_period": fiscal_period,
        "as_of": now[:10],
        "consensus_value": consensus_value,
        "analyst_count": analyst_count,
        "source": source,
        "available_at": now if "T" in str(now) else f"{now}T23:59:59Z",
    }
    return store.upsert(conn, "estimate_snapshot", [row])


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None
