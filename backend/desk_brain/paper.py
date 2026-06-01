"""Wire the Desk Brain target book into the paper-trade framework.

Two jobs:
1. ``record_target_book`` — log each target position as a ``PaperTrade`` so
   realised performance accrues over time through the existing
   ``close_paper_trade`` flow. Risk-% is stored as the trade ``quantity`` so a
   1% position and a 0.4% position are weighted correctly in attribution.
2. ``blended_vs_baseline`` — compare the Desk-Brain-weighted blend (size by
   measured edge) against a naive equal-weight baseline, using whatever paper
   performance already exists per engine. This is the "is the allocator
   actually adding value over equal-weight?" readout.

Everything is defensive: a missing Redis store or empty history degrades to
zeros rather than raising.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from backend.desk_brain import sleeves

_DESK_BRAIN_TAG = "desk_brain"


def record_target_book(payload: Dict[str, Any], *, store: Any = None) -> Dict[str, Any]:
    """Log the current target book as paper trades (one per position).

    De-dupes within a day: a position for the same (engine, ticker) that is
    already open and was opened today is skipped, so repeated refreshes don't
    double-book. Entry price is a normalised 100.0 (P&L is tracked in R via the
    quantity = risk_pct convention, then realised when the desk closes it).
    """
    if store is None:
        return {"logged": 0, "skipped": 0, "tradeIds": []}

    try:
        from backend.backtest_engine import PaperTrade, get_paper_trades, log_paper_trade
    except Exception:
        return {"logged": 0, "skipped": 0, "tradeIds": []}

    book = payload.get("book") or {}
    positions = book.get("positions") or []
    today = dt.date.today().isoformat()

    existing = get_paper_trades(store=store, status="open")
    open_keys = set()
    for t in existing:
        ctx = t.signal_context or {}
        if ctx.get("source") == _DESK_BRAIN_TAG and (t.entry_date or "")[:10] == today:
            open_keys.add((int(t.engine_id), str(t.ticker).upper()))

    logged: List[str] = []
    skipped = 0
    for p in positions:
        engine_id = int(p.get("engineId", 0))
        ticker = str(p.get("ticker", "")).upper()
        if not ticker:
            continue
        if (engine_id, ticker) in open_keys:
            skipped += 1
            continue
        trade = PaperTrade(
            engine_id=engine_id,
            engine_name=str(p.get("engineName", "")),
            ticker=ticker,
            direction=str(p.get("direction", "")),
            structure=str(p.get("structure", "")),
            entry_date=today,
            entry_price=100.0,
            quantity=round(float(p.get("riskPct", 0.0)), 4),
            signal_score=float(p.get("conviction", 0.0)),
            signal_context={
                "source": _DESK_BRAIN_TAG,
                "sleeve": p.get("sleeve"),
                "edgeScore": p.get("edgeScore"),
                "riskDollars": p.get("riskDollars"),
                "regime": book.get("regimeLabel"),
            },
            notes="Logged by Desk Brain meta-allocator",
        )
        tid = log_paper_trade(trade, store=store)
        logged.append(tid)
        open_keys.add((engine_id, ticker))

    return {"logged": len(logged), "skipped": skipped, "tradeIds": logged}


def blended_vs_baseline(*, store: Any = None) -> Dict[str, Any]:
    """Compare Desk-Brain-weighted blended performance vs equal-weight.

    For every engine that has closed paper trades, pull its
    ``EnginePerformance`` and compute:
    - blended expectancy = sum(w_i * avg_pnl_i), w_i proportional to the
      engine's measured edge score (the Desk Brain sizing logic).
    - baseline expectancy = simple mean of avg_pnl_i (equal weight).
    The edge (blended - baseline) is the allocator's value-add over naive
    diversification.
    """
    out: Dict[str, Any] = {
        "byEngine": [],
        "blendedExpectancy": 0.0,
        "baselineExpectancy": 0.0,
        "edge": 0.0,
        "totalClosed": 0,
        "note": "",
    }
    if store is None:
        out["note"] = "No store — paper performance unavailable."
        return out

    try:
        from backend.backtest_engine import compute_performance, get_paper_trades
    except Exception:
        out["note"] = "Backtest framework unavailable."
        return out

    edges = sleeves.all_engine_edges(store=store)
    rows: List[Dict[str, Any]] = []
    total_closed = 0
    for engine_id, edge in edges.items():
        trades = get_paper_trades(store=store, engine_id=engine_id)
        if not trades:
            continue
        perf = compute_performance(trades, engine_id, edge.engine_name)
        if perf.closed_trades <= 0:
            continue
        total_closed += perf.closed_trades
        rows.append({
            "engineId": engine_id,
            "engineName": edge.engine_name,
            "sleeve": edge.sleeve,
            "edgeScore": edge.edge_score,
            "closedTrades": perf.closed_trades,
            "winRate": perf.win_rate,
            "avgPnl": perf.avg_pnl,
            "totalPnl": perf.total_pnl,
            "sharpe": perf.sharpe_estimate,
        })

    out["byEngine"] = rows
    out["totalClosed"] = total_closed
    if not rows:
        out["note"] = "No closed paper trades yet — record the book and let positions resolve."
        return out

    weight_sum = sum(max(0.0, r["edgeScore"]) for r in rows) or 1.0
    blended = sum((max(0.0, r["edgeScore"]) / weight_sum) * r["avgPnl"] for r in rows)
    baseline = sum(r["avgPnl"] for r in rows) / len(rows)
    out["blendedExpectancy"] = round(blended, 3)
    out["baselineExpectancy"] = round(baseline, 3)
    out["edge"] = round(blended - baseline, 3)
    return out
