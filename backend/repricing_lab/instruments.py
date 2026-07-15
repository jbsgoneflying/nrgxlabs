"""Instrument master + symbol-map builders for the Equity Repricing Lab."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from backend.repricing_lab import store

LOG = logging.getLogger("repricing_lab.instruments")

BUILDER_VERSION = "instruments-v1"


def instrument_id_for(symbol: str, *, exchange: str = "US", source: str = "eodhd") -> str:
    """Canonical lab instrument id: ``{source}:{CODE}.{EXCHANGE}``."""
    code = str(symbol).upper().strip()
    if "." not in code:
        code = f"{code}.{exchange.upper()}"
    return f"{source}:{code}"


def bare_symbol(instrument_id: str) -> str:
    """``eodhd:AAPL.US`` → ``AAPL``."""
    body = instrument_id.split(":", 1)[-1]
    return body.split(".", 1)[0]


def eodhd_symbol(instrument_id: str) -> str:
    """``eodhd:AAPL.US`` → ``AAPL.US``."""
    return instrument_id.split(":", 1)[-1]


def _row_from_exchange_symbol(
    row: Mapping[str, Any],
    *,
    delisted: bool,
    ingested_at: str,
) -> Optional[Dict[str, Any]]:
    code = str(row.get("Code") or row.get("code") or "").strip().upper()
    if not code:
        return None
    exchange = str(row.get("Exchange") or row.get("exchange") or "US").strip().upper() or "US"
    # Mutual funds / opaque product codes (0P000…) are not equity research names.
    if code.startswith("0P"):
        return None
    sec_type = str(row.get("Type") or row.get("type") or "").strip()
    etf_flag = 1 if "etf" in sec_type.lower() else 0
    iid = instrument_id_for(code, exchange=exchange if exchange not in ("", "UNKNOWN") else "US")
    # Prefer NASDAQ/NYSE/AMEX style; many EODHD rows already use exchange codes.
    if not iid.endswith(".US") and exchange in ("NASDAQ", "NYSE", "AMEX", "BATS", "ARCA"):
        iid = instrument_id_for(code, exchange="US")
    return {
        "instrument_id": iid,
        "symbol": bare_symbol(iid),
        "exchange": exchange,
        "security_type": sec_type or None,
        "country": str(row.get("Country") or row.get("country") or "") or None,
        "first_trade_date": None,
        "last_trade_date": None,
        "delisted_at": ingested_at[:10] if delisted else None,
        "adr_flag": 1 if "adr" in sec_type.lower() else 0,
        "etf_flag": etf_flag,
        "active_flag": 0 if delisted else 1,
        "source": "eodhd",
        "ingested_at": ingested_at,
        "updated_at": ingested_at,
    }


def upsert_instruments_from_exchange_list(
    conn,
    rows: Sequence[Mapping[str, Any]],
    *,
    delisted: bool = False,
) -> int:
    """Normalize EODHD exchange-symbol-list rows into ``instrument_master``."""
    now = store.utcnow_iso()
    out: List[Dict[str, Any]] = []
    seen = set()
    for r in rows:
        rec = _row_from_exchange_symbol(r, delisted=delisted, ingested_at=now)
        if rec is None or rec["instrument_id"] in seen:
            continue
        seen.add(rec["instrument_id"])
        out.append(rec)
    if not out:
        return 0
    n = store.upsert(conn, "instrument_master", out)
    # Mirror current symbol into symbol_map (valid_from = today for first sighting).
    maps = []
    for rec in out:
        maps.append({
            "instrument_id": rec["instrument_id"],
            "symbol": rec["symbol"],
            "valid_from": now[:10],
            "valid_to": None,
            "source": "eodhd",
            "ingested_at": now,
        })
    store.upsert(conn, "symbol_map", maps)
    return n


def ensure_instrument(
    conn,
    symbol: str,
    *,
    exchange: str = "US",
    security_type: str = "Common Stock",
    active: bool = True,
) -> str:
    """Idempotently ensure a single instrument exists; return instrument_id."""
    now = store.utcnow_iso()
    iid = instrument_id_for(symbol, exchange=exchange)
    existing = store.read_rows(
        conn, "instrument_master", where="instrument_id = ?", params=(iid,), limit=1,
    )
    if existing:
        return iid
    store.upsert(conn, "instrument_master", [{
        "instrument_id": iid,
        "symbol": bare_symbol(iid),
        "exchange": exchange,
        "security_type": security_type,
        "country": "USA",
        "first_trade_date": None,
        "last_trade_date": None,
        "delisted_at": None if active else now[:10],
        "adr_flag": 0,
        "etf_flag": 0,
        "active_flag": 1 if active else 0,
        "source": "eodhd",
        "ingested_at": now,
        "updated_at": now,
    }])
    store.upsert(conn, "symbol_map", [{
        "instrument_id": iid,
        "symbol": bare_symbol(iid),
        "valid_from": now[:10],
        "valid_to": None,
        "source": "eodhd",
        "ingested_at": now,
    }])
    return iid


def content_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
