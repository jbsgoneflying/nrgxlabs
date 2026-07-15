"""Canonical earnings event ledger (Phase 1 long cohort)."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence

from backend.engine15.trading_calendar import add_business_days, is_trading_day
from backend.repricing_lab import store
from backend.repricing_lab.instruments import ensure_instrument, eodhd_symbol

LOG = logging.getLogger("repricing_lab.events")


def _timing_norm(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if s in ("bmo", "beforemarket", "before market", "premarket"):
        return "bmo"
    if s in ("amc", "aftermarket", "after market", "after-hours", "afterhours"):
        return "amc"
    if s in ("during", "dmh", "during market"):
        return "during"
    return "unknown"


def decision_session_for(report_date: str, timing: str) -> str:
    """First session the event is actionable (lab standard, holiday-aware).

    BMO / during → same session if trading day, else next.
    AMC / unknown → next trading session.
    """
    d = report_date[:10]
    if timing in ("bmo", "during") and is_trading_day(d):
        return d
    # next trading day
    return add_business_days(d, 1).isoformat()


def available_at_for(report_date: str, timing: str) -> str:
    """Conservative publication clock for PIT reads."""
    d = report_date[:10]
    if timing == "bmo":
        return f"{d}T12:00:00Z"  # ~08:00 ET
    if timing == "during":
        return f"{d}T17:00:00Z"
    # AMC / unknown: after close
    return f"{d}T21:00:00Z"


def event_id_for(instrument_id: str, report_date: str, source: str) -> str:
    raw = f"{instrument_id}|{report_date[:10]}|{source}|earnings"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def earnings_rows_from_calendar(
    instrument_id: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    estimate_is_pit: bool = False,
    ingested_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    now = ingested_at or store.utcnow_iso()
    out: List[Dict[str, Any]] = []
    for r in rows:
        report = str(r.get("report_date") or r.get("date") or "")[:10]
        if not report:
            continue
        timing = _timing_norm(r.get("before_after_market") or r.get("timing"))
        actual = _f(r.get("actual"))
        estimate = _f(r.get("estimate"))
        out.append({
            "instrument_id": instrument_id,
            "fiscal_period": None,
            "report_date": report,
            "timing": timing,
            "available_at": available_at_for(report, timing),
            "decision_session": decision_session_for(report, timing),
            "eps_actual": actual,
            "eps_estimate": estimate,
            "eps_estimate_source": "eodhd_calendar",
            "revenue_actual": None,
            "revenue_estimate": None,
            "estimate_is_pit": 1 if estimate_is_pit else 0,
            "transcript_ref": None,
            "guidance_json": None,
            "source": "eodhd",
            "revision_version": 1,
            "content_hash": hashlib.sha256(
                json.dumps(dict(r), sort_keys=True, default=str).encode()
            ).hexdigest()[:16],
            "ingested_at": now,
        })
    return out


def upsert_canonical_events_from_earnings(conn, earnings_rows: Sequence[Mapping[str, Any]]) -> int:
    """Mirror earnings_event rows into the generic ``event`` table."""
    now = store.utcnow_iso()
    events = []
    for e in earnings_rows:
        eid = event_id_for(e["instrument_id"], e["report_date"], e["source"])
        surprise = None
        if e.get("eps_actual") is not None and e.get("eps_estimate") not in (None, 0):
            try:
                surprise = (float(e["eps_actual"]) - float(e["eps_estimate"])) / abs(float(e["eps_estimate"]))
            except (TypeError, ValueError, ZeroDivisionError):
                surprise = None
        direction = "unknown"
        if surprise is not None:
            direction = "pos" if surprise > 0 else ("neg" if surprise < 0 else "mixed")
        events.append({
            "event_id": eid,
            "instrument_id": e["instrument_id"],
            "event_type": "earnings",
            "event_subtype": e.get("timing"),
            "direction": direction,
            "title": f"Earnings {e['report_date']}",
            "source": e["source"],
            "source_document_id": None,
            "effective_at": e.get("available_at"),
            "published_at": e.get("available_at"),
            "available_at": e["available_at"],
            "session_bucket": {
                "bmo": "premarket", "amc": "afterhours", "during": "regular",
            }.get(str(e.get("timing") or ""), "nontrading"),
            "decision_session": e["decision_session"],
            "materiality": abs(surprise) if surprise is not None else None,
            "novelty": None,
            "confidence": 0.7 if e.get("estimate_is_pit") else 0.4,
            "structured_json": json.dumps({
                "eps_actual": e.get("eps_actual"),
                "eps_estimate": e.get("eps_estimate"),
                "estimate_is_pit": e.get("estimate_is_pit"),
            }, sort_keys=True),
            "source_excerpt": None,
            "raw_uri": None,
            "content_hash": e.get("content_hash"),
            "llm_model": None,
            "llm_prompt_version": None,
            "llm_validated": None,
            "created_at": now,
            "revised_at": None,
        })
    if not events:
        return 0
    return store.upsert(conn, "event", events)


def backfill_earnings(
    conn,
    client,
    symbol: str,
    *,
    from_date: str,
    to_date: str,
    estimate_is_pit: bool = False,
) -> int:
    iid = ensure_instrument(conn, symbol)
    eod_sym = eodhd_symbol(iid)
    resp = client.get_calendar_earnings(symbols=eod_sym, from_date=from_date, to_date=to_date)
    rows = earnings_rows_from_calendar(
        iid, resp.rows or [], estimate_is_pit=estimate_is_pit,
    )
    n = store.upsert(conn, "earnings_event", rows) if rows else 0
    upsert_canonical_events_from_earnings(conn, rows)
    return n


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None
