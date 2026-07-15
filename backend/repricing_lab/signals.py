"""Shadow signal lifecycle + live scout builder for Equity Repricing Lab."""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, Dict, List, Optional, Set

from backend.config import get_flags
from backend.repricing_lab import store
from backend.repricing_lab.events import (
    decision_session_for,
    earnings_rows_from_calendar,
    upsert_canonical_events_from_earnings,
)
from backend.repricing_lab.geometry import atr_stop
from backend.repricing_lab.instruments import bare_symbol, ensure_instrument, instrument_id_for

LOG = logging.getLogger("repricing_lab.signals")


def list_shadow_candidates(*, limit: int = 50) -> List[Dict[str, Any]]:
    flags = get_flags()
    if not flags.REPRICING_LAB_ENABLED:
        return []
    with store.connect() as conn:
        rows = store.read_rows(
            conn, "research_candidate",
            order_by="created_at DESC", limit=limit,
        )
    out = []
    for r in rows:
        entry = {}
        stop = {}
        try:
            entry = json.loads(r.get("entry_plan_json") or "{}")
            stop = json.loads(r.get("stop_plan_json") or "{}")
        except Exception:
            pass
        out.append({
            "candidateId": r["candidate_id"],
            "archetype": r["archetype"],
            "instrumentId": r["instrument_id"],
            "ticker": bare_symbol(r["instrument_id"]),
            "side": r["side"],
            "decisionSession": r["decision_session"],
            "strategyVersion": r["strategy_version"],
            "reasonCodes": json.loads(r["reason_codes"] or "[]"),
            "vetoes": json.loads(r["vetoes"] or "[]"),
            "entry": entry,
            "stop": stop,
            "lifecycle": "scout",
            "shadowOnly": bool(flags.REPRICING_LAB_SHADOW_ONLY),
            "createdAt": r.get("created_at"),
        })
    return out


def decay_check(*, expectancy_r: float, n: int, window: str = "6m") -> Dict[str, Any]:
    demote = expectancy_r < 0 and n >= 30
    return {
        "window": window,
        "expectancyR": expectancy_r,
        "n": n,
        "demoteToShadowOnly": demote,
        "action": "force_shadow_only" if demote else "hold",
    }


def _universe_symbols() -> Set[str]:
    root = __file__
    import os
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(root))))
    out: Set[str] = set()
    for name in ("sp500.txt", "nasdaq100.txt"):
        path = os.path.join(here, "data", "universe", name)
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            for line in fh:
                t = line.split("#", 1)[0].strip().upper()
                if t:
                    out.add(t)
    return out


def _surprise_from_row(raw: dict) -> Optional[float]:
    """Prefer vendor percent when present; else (actual-estimate)/|estimate|."""
    pct = raw.get("percent")
    try:
        if pct is not None and str(pct).strip() != "":
            p = float(pct)
            # EODHD percent is already in %-points (e.g. 5.2 = +5.2%)
            return p / 100.0
    except (TypeError, ValueError):
        pass
    actual, estimate = raw.get("actual"), raw.get("estimate")
    try:
        if actual is None or estimate in (None, 0, 0.0):
            return None
        return (float(actual) - float(estimate)) / abs(float(estimate))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _last_price(client, symbol: str) -> Optional[float]:
    """Best-effort last price: live quote, else recent EOD bar."""
    eod_sym = f"{symbol}.US"
    try:
        q = client.get_live_quote(eod_sym)
        rows = q.rows or []
        if rows:
            px = rows[0].get("close") or rows[0].get("previousClose")
            if px is not None:
                return float(px)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("live quote miss %s: %s", eod_sym, exc)
    try:
        end = dt.date.today().isoformat()
        start = (dt.date.today() - dt.timedelta(days=14)).isoformat()
        resp = client.get_eod(eod_sym, from_date=start, to_date=end)
        rows = list(resp.rows or [])
        if not rows:
            return None
        last = rows[-1]
        px = last.get("adjusted_close")
        if px is None:
            px = last.get("close")
        return float(px) if px is not None else None
    except Exception as exc:  # noqa: BLE001
        LOG.warning("eod price miss %s: %s", eod_sym, exc)
        return None


def refresh_live_scout(
    *,
    lookback_days: int = 5,
    min_surprise: float = 0.05,
    client=None,
) -> Dict[str, Any]:
    """Build Candidate-A style shadow scout rows from recent EODHD earnings.

    Lightweight live path for the desk UI — does not require the full
    historical SQLite backfill on the droplet. Fetches a last price per
    kept name so entry/stop are populated even on a cold store.
    """
    flags = get_flags()
    if not flags.REPRICING_LAB_ENABLED:
        return {"ok": False, "error": "lab_disabled", "written": 0}

    if client is None:
        from backend.eodhd_client import EodhdClient
        client = EodhdClient.from_env()

    today = dt.date.today()
    lo = (today - dt.timedelta(days=int(lookback_days))).isoformat()
    hi = today.isoformat()
    allowed = _universe_symbols()

    resp = client.get_calendar_earnings(from_date=lo, to_date=hi)
    rows = list(resp.rows or [])
    written = 0
    skipped = 0
    run_id = f"live-scout-{today.isoformat()}"

    with store.connect() as conn:
        started = store.record_job_start(conn, "lab_live_scout")
        try:
            for raw in rows:
                code = str(raw.get("code") or raw.get("Code") or "")
                sym = code.split(".")[0].upper() if code else ""
                if not sym or (allowed and sym not in allowed):
                    skipped += 1
                    continue
                surprise = _surprise_from_row(raw)
                if surprise is None or surprise < min_surprise:
                    skipped += 1
                    continue

                iid = ensure_instrument(conn, sym)
                earn_rows = earnings_rows_from_calendar(
                    iid, [raw], estimate_is_pit=False,
                )
                if earn_rows:
                    store.upsert(conn, "earnings_event", earn_rows)
                    upsert_canonical_events_from_earnings(conn, earn_rows)
                    e = earn_rows[0]
                else:
                    skipped += 1
                    continue

                timing = e.get("timing") or "unknown"
                decision = e["decision_session"]
                bars = store.read_rows(
                    conn, "daily_bar",
                    where="instrument_id = ?", params=(iid,),
                    order_by="session_date DESC", limit=5,
                )
                last_px = None
                if bars:
                    last_px = bars[0].get("adjusted_close") or bars[0].get("close")
                if last_px is None:
                    last_px = _last_price(client, sym)

                if last_px is None:
                    entry_px = stop_px = risk = None
                else:
                    entry_px = float(last_px)
                    atr = entry_px * 0.02
                    stop = atr_stop(entry_px, atr, multiple=1.5, side="long")
                    stop_px = stop.stop_price
                    risk = stop.risk_per_share

                cid = f"{run_id}-{sym}-{e['report_date']}"
                store.upsert(conn, "research_candidate", [{
                    "candidate_id": cid,
                    "run_id": run_id,
                    "strategy_version": "candidate_a/live_scout",
                    "archetype": "catalyst_accepted_continuation",
                    "instrument_id": iid,
                    "side": "long",
                    "decision_time": e["available_at"],
                    "decision_session": decision,
                    "event_cluster_id": None,
                    "feature_snapshot_id": None,
                    "entry_plan_json": json.dumps({
                        "entry_type": "next_open",
                        "entry_price": entry_px,
                        "session": decision,
                        "timing": timing,
                    }),
                    "stop_plan_json": json.dumps({
                        "stop_type": "atr_multiple",
                        "stop_price": stop_px,
                        "risk_per_share": risk,
                    }),
                    "reason_codes": json.dumps([
                        "earnings_beat",
                        f"surprise={surprise * 100:.1f}%",
                        f"timing={timing}",
                        "shadow_scout",
                    ]),
                    "vetoes": json.dumps(
                        [] if flags.REPRICING_LAB_SHADOW_ONLY else ["shadow_only_required"]
                    ),
                    "created_at": store.utcnow_iso(),
                }])
                written += 1

            detail = {
                "written": written,
                "skipped": skipped,
                "window": [lo, hi],
                "rawRows": len(rows),
                "runId": run_id,
            }
            store.record_job_finish(conn, "lab_live_scout", started, ok=True, detail=detail)
            return {"ok": True, **detail}
        except Exception as exc:
            store.record_job_finish(
                conn, "lab_live_scout", started, ok=False, detail={"error": str(exc)},
            )
            raise
