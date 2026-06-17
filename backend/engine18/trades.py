"""Engine 18 — drift trade tracker (Redis).

Key layout (mirrors the E2/E12 pattern):
    e18:trades:{trade_id}   — individual trade JSON document
    e18:trades:index        — ordered list of trade IDs (newest last)

Each trade snapshots the entry signal (candidate card) so closed trades can be
replayed by the monthly continuous-validation loop and compared against the
backtested cohort expectations.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any, Dict, List, Optional

from backend.config import FeatureFlags, get_flags
from backend.engine18.models import add_business_days, utcnow_iso
from backend.redis_store import RedisStore, get_store_optional

LOG = logging.getLogger(__name__)

_TRADE_KEY_PREFIX = "e18:trades:"
_TRADE_INDEX_KEY = "e18:trades:index"


def _ttl(flags: Optional[FeatureFlags] = None) -> int:
    f = flags or get_flags()
    return int(f.ENGINE18_TRADE_TTL_S)


def _max_index(flags: Optional[FeatureFlags] = None) -> int:
    f = flags or get_flags()
    return int(f.ENGINE18_TRADE_MAX_INDEX)


def _generate_trade_id(ticker: str) -> str:
    today = dt.date.today().strftime("%Y%m%d")
    return f"e18-{today}-{str(ticker or 'NA').upper()}-{uuid.uuid4().hex[:8]}"


def rebuild_index_if_missing(
    store: Optional[RedisStore] = None,
    flags: Optional[FeatureFlags] = None,
) -> bool:
    """Rebuild the trade index from existing keys if it has expired."""
    s = store or get_store_optional()
    if s is None:
        return False
    if s.get_json(_TRADE_INDEX_KEY) is not None:
        return False
    keys = s.scan_keys(f"{_TRADE_KEY_PREFIX}*")
    trade_ids = sorted(
        k.replace(_TRADE_KEY_PREFIX, "") for k in keys if k != _TRADE_INDEX_KEY
    )
    if not trade_ids:
        return False
    s.set_json(_TRADE_INDEX_KEY, trade_ids[-_max_index(flags):], ttl_s=_ttl(flags))
    LOG.info("engine18_trades: rebuilt index from %d keys", len(trade_ids))
    return True


def log_trade(
    trade_data: Dict[str, Any],
    store: Optional[RedisStore] = None,
    flags: Optional[FeatureFlags] = None,
) -> Optional[str]:
    """Persist a new tracked drift trade. Returns trade_id or None on failure."""
    s = store or get_store_optional()
    if s is None:
        LOG.warning("engine18_trades.log_trade: Redis unavailable")
        return None
    f = flags or get_flags()

    ticker = str(trade_data.get("ticker") or "").upper().strip()
    if not ticker:
        return None
    trade_id = _generate_trade_id(ticker)
    entry_date = str(trade_data.get("entryDate") or dt.date.today().isoformat())[:10]
    hold_days = int(trade_data.get("holdDays") or f.ENGINE18_HOLD_DAYS)

    trade = {
        "tradeId": trade_id,
        "engine": 18,
        "status": "active",
        "loggedAt": utcnow_iso(),
        "ticker": ticker,
        "entryDate": entry_date,
        "plannedExitDate": str(trade_data.get("plannedExitDate") or add_business_days(entry_date, hold_days)),
        "holdDays": hold_days,
        "entryPrice": trade_data.get("entryPrice"),
        "shares": trade_data.get("shares"),
        "sizing": str(trade_data.get("sizing") or ""),
        "decision": str(trade_data.get("decision") or ""),
        "mode": str(trade_data.get("mode") or "tracked"),
        "signalSnapshot": trade_data.get("signalSnapshot") if isinstance(trade_data.get("signalSnapshot"), dict) else {},
        "notes": str(trade_data.get("notes") or ""),
        "checkIns": [],
        "closedAt": None,
        "closeReason": None,
        "outcome": None,
    }

    ttl = _ttl(flags)
    if not s.set_json(f"{_TRADE_KEY_PREFIX}{trade_id}", trade, ttl_s=ttl):
        LOG.error("engine18_trades.log_trade: write failed for %s", trade_id)
        return None
    index = s.get_json(_TRADE_INDEX_KEY) or []
    index.append(trade_id)
    s.set_json(_TRADE_INDEX_KEY, index[-_max_index(flags):], ttl_s=ttl)
    return trade_id


def get_trade(trade_id: str, store: Optional[RedisStore] = None) -> Optional[Dict[str, Any]]:
    s = store or get_store_optional()
    if s is None:
        return None
    doc = s.get_json(f"{_TRADE_KEY_PREFIX}{trade_id}")
    return doc if isinstance(doc, dict) else None


def list_trades(
    status: Optional[str] = None,
    store: Optional[RedisStore] = None,
) -> List[Dict[str, Any]]:
    """All tracked trades, newest first. Optional status filter (active/closed)."""
    s = store or get_store_optional()
    if s is None:
        return []
    index = s.get_json(_TRADE_INDEX_KEY) or []
    out: List[Dict[str, Any]] = []
    for trade_id in reversed(index):
        doc = s.get_json(f"{_TRADE_KEY_PREFIX}{trade_id}")
        if not isinstance(doc, dict):
            continue
        if status and str(doc.get("status")) != status:
            continue
        out.append(doc)
    return out


def add_checkin(
    trade_id: str,
    note: Dict[str, Any],
    store: Optional[RedisStore] = None,
    flags: Optional[FeatureFlags] = None,
) -> Optional[Dict[str, Any]]:
    s = store or get_store_optional()
    if s is None:
        return None
    doc = get_trade(trade_id, store=s)
    if doc is None:
        return None
    entry = {"at": utcnow_iso(), **{k: v for k, v in (note or {}).items() if k != "at"}}
    doc.setdefault("checkIns", []).append(entry)
    s.set_json(f"{_TRADE_KEY_PREFIX}{trade_id}", doc, ttl_s=_ttl(flags))
    return doc


def close_trade(
    trade_id: str,
    close_data: Dict[str, Any],
    store: Optional[RedisStore] = None,
    flags: Optional[FeatureFlags] = None,
) -> Optional[Dict[str, Any]]:
    """Close a trade with outcome data. Returns the updated doc or None."""
    s = store or get_store_optional()
    if s is None:
        return None
    doc = get_trade(trade_id, store=s)
    if doc is None:
        return None
    exit_price = close_data.get("exitPrice")
    entry_price = doc.get("entryPrice")
    ret_pct = None
    try:
        if exit_price is not None and entry_price not in (None, 0):
            ret_pct = (float(exit_price) - float(entry_price)) / float(entry_price)
    except (TypeError, ValueError, ZeroDivisionError):
        ret_pct = None
    doc.update({
        "status": "closed",
        "closedAt": utcnow_iso(),
        "closeReason": str(close_data.get("reason") or "planned_exit"),
        "outcome": {
            "exitDate": str(close_data.get("exitDate") or dt.date.today().isoformat())[:10],
            "exitPrice": exit_price,
            "returnPct": ret_pct,
            "notes": str(close_data.get("notes") or ""),
        },
    })
    s.set_json(f"{_TRADE_KEY_PREFIX}{trade_id}", doc, ttl_s=_ttl(flags))
    return doc
