"""Daily-bar backfill: EODHD → silver ``daily_bar`` (+ optional bronze raw)."""
from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from backend.config import get_flags
from backend.repricing_lab import store
from backend.repricing_lab.instruments import content_hash, eodhd_symbol, ensure_instrument

LOG = logging.getLogger("repricing_lab.bars")


def _available_at_for_session(session_date: str) -> str:
    """Conservative: bar becomes available after the session (next calendar day 01:00Z)."""
    return f"{session_date}T23:59:59Z"


def bars_from_eod_rows(
    instrument_id: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    source: str = "eodhd",
    ingested_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Normalize EODHD EOD rows into ``daily_bar`` records."""
    now = ingested_at or store.utcnow_iso()
    out: List[Dict[str, Any]] = []
    for r in rows:
        session = str(r.get("date") or "")[:10]
        if not session:
            continue
        close = _f(r.get("close"))
        adj = _f(r.get("adjusted_close") if r.get("adjusted_close") is not None else r.get("adjustedClose"))
        if adj is None:
            adj = close
        factor = 1.0
        if close and adj and abs(close) > 1e-12:
            factor = float(adj) / float(close)
        out.append({
            "instrument_id": instrument_id,
            "session_date": session,
            "open": _f(r.get("open")),
            "high": _f(r.get("high")),
            "low": _f(r.get("low")),
            "close": close,
            "adjusted_close": adj,
            "adj_factor": factor,
            "volume": _f(r.get("volume")) or 0.0,
            "ca_version": 1,
            "source": source,
            "available_at": _available_at_for_session(session),
            "ingested_at": now,
        })
    return out


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        x = float(v)
        if x != x:
            return None
        return x
    except (TypeError, ValueError):
        return None


def write_bronze_payload(
    *,
    provider: str,
    endpoint: str,
    params: Mapping[str, Any],
    payload: Any,
    retrieved_at: Optional[str] = None,
) -> tuple[str, str, Dict[str, Any], str]:
    """Gzip-write raw JSON under REPRICING_LAB_RAW_DIR; return hash, uri, safe_params, retrieved_at."""
    flags = get_flags()
    raw_dir = Path(str(getattr(flags, "REPRICING_LAB_RAW_DIR", "data/lab_raw")))
    if not raw_dir.is_absolute():
        root = Path(__file__).resolve().parent.parent.parent
        raw_dir = (root / raw_dir).resolve()
    safe_params = {
        k: v for k, v in params.items()
        if "token" not in str(k).lower() and "key" not in str(k).lower()
    }
    h = content_hash({"provider": provider, "endpoint": endpoint, "params": safe_params, "payload": payload})
    retrieved = retrieved_at or store.utcnow_iso()
    yyyymm = retrieved[:7].replace("-", "")
    dest_dir = raw_dir / provider / yyyymm
    dest_dir.mkdir(parents=True, exist_ok=True)
    uri = str(dest_dir / f"{h}.json.gz")
    with gzip.open(uri, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, default=str)
    return h, uri, safe_params, retrieved


def upsert_bars(conn, rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    return store.upsert(conn, "daily_bar", list(rows))


def backfill_symbol_bars(
    conn,
    client,
    symbol: str,
    *,
    from_date: str,
    to_date: str,
    write_bronze: bool = True,
) -> int:
    """Fetch EOD for one symbol and upsert. Returns bar count written."""
    iid = ensure_instrument(conn, symbol)
    eod_sym = eodhd_symbol(iid)
    resp = client.get_eod(eod_sym, from_date=from_date, to_date=to_date)
    raw_rows = list(resp.rows or [])
    if write_bronze and raw_rows:
        try:
            h, uri, safe_params, retrieved = write_bronze_payload(
                provider="eodhd",
                endpoint=f"eod/{eod_sym}",
                params={"from": from_date, "to": to_date},
                payload=raw_rows,
            )
            store.upsert(conn, "raw_payload", [{
                "content_hash": h,
                "provider": "eodhd",
                "endpoint": f"eod/{eod_sym}",
                "params_json": json.dumps(safe_params, sort_keys=True),
                "retrieved_at": retrieved,
                "uri": uri,
            }])
        except Exception as exc:  # noqa: BLE001
            LOG.warning("bronze write failed for %s: %s", eod_sym, exc)
    bars = bars_from_eod_rows(iid, raw_rows)
    return upsert_bars(conn, bars)


def load_bars_as_of(
    conn,
    instrument_id: str,
    *,
    as_of: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """PIT bar read for research paths."""
    clauses = ["instrument_id = ?"]
    params: List[Any] = [instrument_id]
    if start:
        clauses.append("session_date >= ?")
        params.append(start)
    if end:
        clauses.append("session_date <= ?")
        params.append(end)
    return store.read_available(
        conn,
        "daily_bar",
        as_of=as_of,
        where=" AND ".join(clauses),
        params=params,
        order_by="session_date ASC",
    )
