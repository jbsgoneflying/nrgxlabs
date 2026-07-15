"""Chronological portfolio simulation engine."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence

from backend.repricing_lab.labels import label_path
from backend.repricing_lab.simulator.book import Book, OpenPosition
from backend.repricing_lab.simulator.constraints import ConstraintConfig, check_entry
from backend.repricing_lab.simulator.costs import LabCostModel
from backend.repricing_lab.simulator.fills import fill_open, fill_stop

LOG = logging.getLogger("repricing_lab.simulator.engine")


def run_simulation(
    *,
    candidates: Sequence[Dict[str, Any]],
    bars_by_instrument: Dict[str, List[Dict[str, Any]]],
    sessions: Sequence[str],
    account_size: float = 25_000.0,
    cost_model: Optional[LabCostModel] = None,
    constraints: Optional[ConstraintConfig] = None,
    max_hold: int = 40,
    run_id: str = "sim",
) -> Dict[str, Any]:
    """Replay candidates chronologically.

    Each candidate needs: candidate_id, instrument_id, side, decision_session,
    entry_plan_json, stop_plan_json, optional sector / adv20_usd / shares.
    """
    cost_model = cost_model or LabCostModel()
    constraints = constraints or ConstraintConfig(account_size=account_size)
    book = Book(cash=account_size)
    orders: List[dict] = []

    # Index candidates by decision_session
    by_session: Dict[str, List[dict]] = {}
    for c in candidates:
        by_session.setdefault(c["decision_session"][:10], []).append(c)

    pending_entry: Dict[str, dict] = {}  # position_id -> cand awaiting next open

    for session in sessions:
        sess = session[:10]
        # 1) Manage stops on open positions
        for pid, pos in list(book.positions.items()):
            bars = bars_by_instrument.get(pos.instrument_id) or []
            bar = _bar_on(bars, sess)
            if not bar:
                continue
            fr = fill_stop(bar, side=pos.side, stop=pos.stop_price)
            if fr.status == "filled":
                _close(book, pos, sess, fr.fill_price, fr.reason, cost_model, orders, run_id)
                continue
            # Time stop
            held = _sessions_held(pos.entry_session, sess, bars)
            if held >= max_hold:
                px = bar.get("adjusted_close") if bar.get("adjusted_close") is not None else bar.get("close")
                if px is not None:
                    _close(book, pos, sess, float(px), "time_stop", cost_model, orders, run_id)

        # 2) Fill pending entries at today's open
        for pid, cand in list(pending_entry.items()):
            bars = bars_by_instrument.get(cand["instrument_id"]) or []
            bar = _bar_on(bars, sess)
            if not bar:
                continue
            fr = fill_open(bar, side=cand["side"])
            del pending_entry[pid]
            if fr.status != "filled":
                book.rejected.append({"candidate_id": cand["candidate_id"], "reason": fr.reason})
                continue
            entry = json.loads(cand["entry_plan_json"])
            stop = json.loads(cand["stop_plan_json"])
            entry_px = float(fr.fill_price)
            stop_px = float(stop.get("stop_price") or stop.get("stop", {}).get("stop_price"))
            shares = float(cand.get("shares") or 0)
            if shares <= 0:
                # Fallback: 1% notionally tiny for research when shares unset
                shares = max(1.0, (account_size * 0.0025) / max(abs(entry_px - stop_px), 1e-6))
                shares = float(int(shares))
            notional = shares * entry_px
            rej = check_entry(
                open_positions=book.open_list(),
                instrument_id=cand["instrument_id"],
                sector=cand.get("sector"),
                notional=notional,
                config=constraints,
            )
            if rej:
                book.rejected.append({"candidate_id": cand["candidate_id"], "reason": rej.code})
                continue
            slip = cost_model.round_trip_fraction(adv20_usd=cand.get("adv20_usd")) / 2.0
            fill_px = entry_px * (1 + slip) if cand["side"] == "long" else entry_px * (1 - slip)
            book.cash -= shares * fill_px
            book.positions[pid] = OpenPosition(
                position_id=pid,
                candidate_id=cand["candidate_id"],
                instrument_id=cand["instrument_id"],
                side=cand["side"],
                shares=shares,
                entry_session=sess,
                entry_price=fill_px,
                stop_price=stop_px,
                risk_per_share=abs(fill_px - stop_px),
                notional=shares * fill_px,
                sector=cand.get("sector"),
            )
            orders.append({
                "run_id": run_id, "order_id": f"{pid}-entry", "candidate_id": cand["candidate_id"],
                "instrument_id": cand["instrument_id"], "side": cand["side"], "order_type": "open",
                "submitted_session": sess, "filled_session": sess, "intended_price": entry_px,
                "fill_price": fill_px, "shares": shares, "status": "filled", "reject_reason": None,
            })

        # 3) Queue new candidates whose decision_session is today (enter next session)
        for cand in by_session.get(sess, []):
            pid = f"{cand['candidate_id']}-pos"
            pending_entry[pid] = cand

    # Force-close remaining at last session
    if sessions:
        last = sessions[-1][:10]
        for pid, pos in list(book.positions.items()):
            bars = bars_by_instrument.get(pos.instrument_id) or []
            bar = _bar_on(bars, last)
            px = None
            if bar:
                px = bar.get("adjusted_close") if bar.get("adjusted_close") is not None else bar.get("close")
            if px is not None:
                _close(book, pos, last, float(px), "eod_force", cost_model, orders, run_id)

    return {
        "run_id": run_id,
        "closed": book.closed,
        "rejected": book.rejected,
        "orders": orders,
        "ending_cash": book.cash,
        "n_closed": len(book.closed),
        "n_rejected": len(book.rejected),
        "expectancy_r": _expectancy([c.get("realized_r") for c in book.closed]),
    }


def _close(book, pos, session, px, reason, cost_model, orders, run_id):
    slip = cost_model.round_trip_fraction() / 2.0
    fill = px * (1 - slip) if pos.side == "long" else px * (1 + slip)
    book.cash += pos.shares * fill
    risk = pos.risk_per_share or 1e-9
    realized = ((fill - pos.entry_price) if pos.side == "long" else (pos.entry_price - fill)) / risk
    # Path labels for MFE/MAE on the hold window
    # (lightweight: use realized only if no bars walk)
    book.closed.append({
        "position_id": pos.position_id,
        "candidate_id": pos.candidate_id,
        "instrument_id": pos.instrument_id,
        "side": pos.side,
        "entry_session": pos.entry_session,
        "exit_session": session,
        "entry_price": pos.entry_price,
        "exit_price": fill,
        "exit_reason": reason,
        "shares": pos.shares,
        "realized_r": round(realized, 4),
    })
    orders.append({
        "run_id": run_id, "order_id": f"{pos.position_id}-exit", "candidate_id": pos.candidate_id,
        "instrument_id": pos.instrument_id, "side": "sell" if pos.side == "long" else "cover",
        "order_type": "stop" if "stop" in reason else "close",
        "submitted_session": session, "filled_session": session, "intended_price": px,
        "fill_price": fill, "shares": pos.shares, "status": "filled", "reject_reason": None,
    })
    del book.positions[pos.position_id]


def _bar_on(bars, session):
    for b in bars:
        if str(b.get("session_date") or "")[:10] == session[:10]:
            return b
    return None


def _sessions_held(entry, current, bars):
    n = 0
    for b in bars:
        d = str(b.get("session_date") or "")[:10]
        if entry[:10] < d <= current[:10]:
            n += 1
    return n


def _expectancy(xs):
    vals = [float(x) for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else 0.0
