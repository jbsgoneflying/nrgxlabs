"""Path-dependent R-ladder labels (seeded by engine3_red_dog.evaluate_outcome semantics)."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

LOG = logging.getLogger("repricing_lab.labels")

R_LEVELS = (2, 3, 5, 8)


@dataclass
class PathLabels:
    triggered: bool = False
    status: str = "expired"  # expired|stopped|target|time_stop|gap_stopped
    realized_r: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    bars_held: int = 0
    exit_price: Optional[float] = None
    # First-touch probabilities / times for +NR before -1R
    hit_before_stop: Dict[str, Optional[bool]] = field(default_factory=dict)
    time_to_r: Dict[str, Optional[int]] = field(default_factory=dict)
    adverse_gap_hit: bool = False
    adverse_gap_r: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def label_path(
    *,
    side: str,
    entry_price: float,
    stop_price: float,
    forward_bars: Sequence[Dict[str, Any]],
    max_hold: int = 40,
    r_levels: Sequence[int] = R_LEVELS,
) -> PathLabels:
    """Walk forward bars after entry; stop wins on same-bar conflict (conservative).

    Entry is assumed already filled at ``entry_price`` on the first forward bar's
    open (or prior decision). ``forward_bars`` are management bars starting at
    the entry session.
    """
    risk = abs(entry_price - stop_price)
    out = PathLabels()
    if risk <= 0 or not forward_bars:
        return out

    is_long = side == "long"
    out.triggered = True
    hit: Dict[str, Optional[bool]] = {f"{n}R": None for n in r_levels}
    ttr: Dict[str, Optional[int]] = {f"{n}R": None for n in r_levels}
    mfe = mae = 0.0
    adverse_gap_r = 0.0
    adverse_gap_hit = False

    for i, b in enumerate(forward_bars[:max_hold]):
        o = b.get("open")
        h = b.get("high")
        l = b.get("low")
        c = b.get("adjusted_close") if b.get("adjusted_close") is not None else b.get("close")
        if h is None or l is None or c is None:
            continue
        o = float(o) if o is not None else float(c)
        h, l, c = float(h), float(l), float(c)

        # Overnight gap through stop
        if is_long and o < stop_price:
            gap_r = (entry_price - o) / risk
            adverse_gap_hit = True
            adverse_gap_r = max(adverse_gap_r, gap_r)
            out.status = "gap_stopped"
            out.realized_r = -gap_r
            out.exit_price = o
            out.bars_held = i + 1
            out.mfe_r = mfe
            out.mae_r = max(mae, gap_r)
            out.adverse_gap_hit = True
            out.adverse_gap_r = gap_r
            out.hit_before_stop = hit
            out.time_to_r = ttr
            return out
        if (not is_long) and o > stop_price:
            gap_r = (o - entry_price) / risk
            out.status = "gap_stopped"
            out.realized_r = -gap_r
            out.exit_price = o
            out.bars_held = i + 1
            out.adverse_gap_hit = True
            out.adverse_gap_r = gap_r
            out.hit_before_stop = hit
            out.time_to_r = ttr
            return out

        if is_long:
            fav = (h - entry_price) / risk
            adv = (entry_price - l) / risk
            stop_hit = l <= stop_price
        else:
            fav = (entry_price - l) / risk
            adv = (h - entry_price) / risk
            stop_hit = h >= stop_price

        mfe = max(mfe, fav)
        mae = max(mae, adv)

        for n in r_levels:
            key = f"{n}R"
            if hit[key] is None and fav >= n:
                hit[key] = True
                ttr[key] = i + 1

        if stop_hit:
            # Mark any unreached R levels as False
            for n in r_levels:
                key = f"{n}R"
                if hit[key] is None:
                    hit[key] = False
            out.status = "stopped"
            out.realized_r = -1.0
            out.exit_price = stop_price
            out.bars_held = i + 1
            out.mfe_r = mfe
            out.mae_r = mae
            out.hit_before_stop = hit
            out.time_to_r = ttr
            out.adverse_gap_hit = adverse_gap_hit
            out.adverse_gap_r = adverse_gap_r
            return out

    # Time stop — MTM at last close
    last = forward_bars[min(max_hold, len(forward_bars)) - 1]
    last_c = last.get("adjusted_close") if last.get("adjusted_close") is not None else last.get("close")
    last_c = float(last_c) if last_c is not None else entry_price
    r_mult = ((last_c - entry_price) if is_long else (entry_price - last_c)) / risk
    for n in r_levels:
        key = f"{n}R"
        if hit[key] is None:
            hit[key] = False
    out.status = "time_stop"
    out.realized_r = round(r_mult, 4)
    out.exit_price = last_c
    out.bars_held = min(max_hold, len(forward_bars))
    out.mfe_r = mfe
    out.mae_r = mae
    out.hit_before_stop = hit
    out.time_to_r = ttr
    return out


def label_candidates_for_run(conn, *, run_id: str) -> int:
    """Attach path labels into sim_position.lifecycle_json for candidates of a run."""
    from backend.repricing_lab import store

    cands = store.read_rows(conn, "research_candidate", where="run_id = ?", params=(run_id,))
    n = 0
    for cand in cands:
        entry = json.loads(cand["entry_plan_json"])
        stop = json.loads(cand["stop_plan_json"])
        entry_px = float(entry.get("entry_price") or entry.get("entry", {}).get("entry_price") or 0)
        stop_px = float(stop.get("stop_price") or stop.get("stop", {}).get("stop_price") or 0)
        session = str(entry.get("session") or entry.get("entry", {}).get("session") or cand["decision_session"])
        bars = store.read_rows(
            conn, "daily_bar",
            where="instrument_id = ? AND session_date >= ?",
            params=(cand["instrument_id"], session),
            order_by="session_date ASC",
        )
        labels = label_path(
            side=cand["side"],
            entry_price=entry_px,
            stop_price=stop_px,
            forward_bars=bars,
        )
        # Upsert a lightweight sim_position audit row
        store.upsert(conn, "sim_position", [{
            "run_id": run_id,
            "position_id": f"{cand['candidate_id']}-label",
            "candidate_id": cand["candidate_id"],
            "instrument_id": cand["instrument_id"],
            "side": cand["side"],
            "entry_session": session,
            "entry_price": entry_px,
            "shares": 0,
            "stop_price": stop_px,
            "risk_per_share": abs(entry_px - stop_px),
            "planned_risk_pct": None,
            "exit_session": None,
            "exit_price": labels.exit_price,
            "exit_reason": labels.status,
            "realized_r": labels.realized_r,
            "mfe_r": labels.mfe_r,
            "mae_r": labels.mae_r,
            "holding_sessions": labels.bars_held,
            "lifecycle_json": json.dumps(labels.to_dict(), sort_keys=True),
        }])
        n += 1
    return n
