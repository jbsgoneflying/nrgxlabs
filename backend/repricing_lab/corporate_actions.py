"""Corporate-action ingest (splits / dividends / delistings) + re-adjustment helpers."""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from backend.repricing_lab import store
from backend.repricing_lab.instruments import eodhd_symbol, ensure_instrument

LOG = logging.getLogger("repricing_lab.corporate_actions")

_SPLIT_RE = re.compile(r"^\s*([0-9.]+)\s*/\s*([0-9.]+)\s*$")


def parse_split_ratio(split_str: str) -> Optional[float]:
    """``'10.000000/1.000000'`` → 10.0 (new shares per old share)."""
    m = _SPLIT_RE.match(str(split_str or ""))
    if not m:
        return None
    num, den = float(m.group(1)), float(m.group(2))
    if den == 0:
        return None
    return num / den


def ca_available_at(effective_date: str, announcement_date: Optional[str] = None) -> str:
    """Conservative availability: later of announcement vs effective, end-of-day."""
    base = (announcement_date or effective_date)[:10]
    if announcement_date and effective_date and announcement_date[:10] > effective_date[:10]:
        base = announcement_date[:10]
    return f"{base}T23:59:59Z"


def splits_to_rows(
    instrument_id: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    ingested_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    now = ingested_at or store.utcnow_iso()
    out: List[Dict[str, Any]] = []
    for r in rows:
        eff = str(r.get("date") or "")[:10]
        if not eff:
            continue
        ratio = parse_split_ratio(str(r.get("split") or ""))
        out.append({
            "instrument_id": instrument_id,
            "action_type": "split",
            "effective_date": eff,
            "announcement_date": None,
            "ratio_or_amount": ratio,
            "detail_json": None,
            "source": "eodhd",
            "available_at": ca_available_at(eff),
            "ingested_at": now,
            "raw_uri": None,
            "content_hash": None,
        })
    return out


def dividends_to_rows(
    instrument_id: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    ingested_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    now = ingested_at or store.utcnow_iso()
    out: List[Dict[str, Any]] = []
    for r in rows:
        eff = str(r.get("date") or "")[:10]
        if not eff:
            continue
        amount = r.get("value")
        try:
            amount_f = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            amount_f = None
        ann = str(r.get("declarationDate") or "")[:10] or None
        out.append({
            "instrument_id": instrument_id,
            "action_type": "dividend",
            "effective_date": eff,
            "announcement_date": ann,
            "ratio_or_amount": amount_f,
            "detail_json": None,
            "source": "eodhd",
            "available_at": ca_available_at(eff, ann),
            "ingested_at": now,
            "raw_uri": None,
            "content_hash": None,
        })
    return out


def backfill_corporate_actions(
    conn,
    client,
    symbol: str,
    *,
    from_date: str,
    to_date: str,
) -> Tuple[int, int]:
    """Fetch splits + dividends; return (n_splits, n_divs)."""
    iid = ensure_instrument(conn, symbol)
    eod_sym = eodhd_symbol(iid)
    n_s = n_d = 0
    try:
        s_resp = client.get_splits(eod_sym, from_date=from_date, to_date=to_date)
        s_rows = splits_to_rows(iid, s_resp.rows or [])
        n_s = store.upsert(conn, "corporate_action", s_rows) if s_rows else 0
    except Exception as exc:  # noqa: BLE001
        LOG.warning("splits backfill failed for %s: %s", eod_sym, exc)
    try:
        d_resp = client.get_dividends(eod_sym, from_date=from_date, to_date=to_date)
        d_rows = dividends_to_rows(iid, d_resp.rows or [])
        n_d = store.upsert(conn, "corporate_action", d_rows) if d_rows else 0
    except Exception as exc:  # noqa: BLE001
        LOG.warning("dividends backfill failed for %s: %s", eod_sym, exc)
    return n_s, n_d


def apply_split_to_price(price: float, ratio: float, *, forward: bool = True) -> float:
    """Adjust a raw price across a split. ``forward=True`` = post-split scale."""
    if ratio <= 0:
        raise ValueError("split ratio must be positive")
    return price / ratio if forward else price * ratio


def verify_adjusted_consistency(
    raw_close: float,
    adjusted_close: float,
    cum_split_ratio: float,
    *,
    tol: float = 1e-4,
) -> bool:
    """Property check: adjusted ≈ raw / cum_split for pure-split worlds."""
    if cum_split_ratio <= 0 or raw_close is None or adjusted_close is None:
        return False
    expected = raw_close / cum_split_ratio
    return abs(expected - adjusted_close) <= max(tol, tol * abs(expected))
