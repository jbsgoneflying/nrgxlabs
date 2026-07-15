"""Candidate cohorts A/D + ablation ladder + frozen bake-off."""
from __future__ import annotations

import json
import logging
from itertools import product
from typing import Any, Dict, List, Optional, Sequence

from backend.repricing_lab import store
from backend.repricing_lab.geometry import atr_stop, next_open_entry, size_shares
from backend.repricing_lab.features.price_trend import atr_wilder, compute_price_trend
from backend.repricing_lab.labels import label_path
from backend.repricing_lab.runs import finish_run, start_run
from backend.repricing_lab.simulator.costs import LabCostModel
from backend.repricing_lab.simulator.engine import run_simulation
from backend.research.synthetic import build_synthetic_dataset
from backend.repricing_lab.instruments import ensure_instrument
from backend.repricing_lab.bars import bars_from_eod_rows, upsert_bars
from backend.repricing_lab.events import earnings_rows_from_calendar, upsert_canonical_events_from_earnings

LOG = logging.getLogger("repricing_lab.cohorts")

ABLATION_GRID = {
    "event_only": {"require_leadership": False, "require_compression": False, "require_acceptance": False},
    "event_acceptance": {"require_leadership": False, "require_compression": False, "require_acceptance": True},
    "event_leadership": {"require_leadership": True, "require_compression": False, "require_acceptance": True},
    "full_coiled_leader": {"require_leadership": True, "require_compression": True, "require_acceptance": True},
}


def _seed_synthetic_into_store(conn) -> Dict[str, Any]:
    """Materialize a small synthetic PEAD world into the lab store for bake-off."""
    price, earnings, _ins, injected = build_synthetic_dataset(seed=11, n_tickers=12)
    sessions = set()
    for t in range(12):
        ticker = f"SYN{t:02d}"
        iid = ensure_instrument(conn, ticker)
        bars = price.get_bars(ticker, "2021-01-01", "2025-12-31")
        eod = [
            {
                "date": b.date, "open": b.open, "high": b.high, "low": b.low,
                "close": b.close, "adjusted_close": b.close, "volume": b.volume or 1_000_000,
            }
            for b in bars
        ]
        upsert_bars(conn, bars_from_eod_rows(iid, eod))
        for b in bars:
            sessions.add(b.date)
        evs = earnings.get_events(ticker, "2021-01-01", "2025-12-31")
        cal = []
        for e in evs:
            cal.append({
                "report_date": e.report_date,
                "before_after_market": "AfterMarket" if e.timing == "amc" else "BeforeMarket",
                "actual": e.actual_eps,
                "estimate": e.estimate_eps,
            })
        rows = earnings_rows_from_calendar(iid, cal, estimate_is_pit=True)
        store.upsert(conn, "earnings_event", rows)
        upsert_canonical_events_from_earnings(conn, rows)
    return {"sessions": sorted(sessions), "injected": injected}


def build_candidate_a(
    conn,
    *,
    ablation: str,
    run_id: str,
    min_surprise: float = 0.05,
) -> List[Dict[str, Any]]:
    """Earnings-repricing continuation cohort (long-only)."""
    flags = ABLATION_GRID[ablation]
    earns = store.read_rows(conn, "earnings_event", order_by="report_date ASC")
    cands = []
    for e in earns:
        if not e.get("estimate_is_pit"):
            continue
        if e.get("eps_actual") is None or e.get("eps_estimate") in (None, 0):
            continue
        surprise = (float(e["eps_actual"]) - float(e["eps_estimate"])) / abs(float(e["eps_estimate"]))
        if surprise < min_surprise:
            continue
        bars = store.read_rows(
            conn, "daily_bar",
            where="instrument_id = ?", params=(e["instrument_id"],),
            order_by="session_date ASC",
        )
        # Bars available at decision
        as_of = e["available_at"]
        pit_bars = [b for b in bars if b["available_at"] <= as_of]
        trend = compute_price_trend(pit_bars)
        if flags["require_leadership"]:
            if (trend.get("ret_3m") or -1) < 0 or (trend.get("dist_sma50") or -1) < 0:
                continue
        if flags["require_compression"]:
            # Use NR7 on last 7 available bars
            from backend.repricing_lab.features.compression import compute_compression
            comp = compute_compression(pit_bars)
            if not comp.get("nr7"):
                continue
        entry = next_open_entry(bars, decision_session=e["decision_session"])
        if not entry:
            continue
        atr = atr_wilder(pit_bars, 14) or (entry.entry_price * 0.02)
        stop = atr_stop(entry.entry_price, atr, multiple=1.5, side="long")
        if flags["require_acceptance"]:
            # Require first post-event bar not to fully fill the gap downward — soft filter via gap_retention
            from backend.repricing_lab.features.acceptance import compute_acceptance
            acc = compute_acceptance(bars, event_session=e["decision_session"])
            if acc.get("gap_retention") is not None and acc["gap_retention"] < 0:
                continue
        cid = f"{run_id}-{e['instrument_id']}-{e['report_date']}-{ablation}"
        sizing = size_shares(
            account_size=25_000, risk_pct=0.25,
            risk_per_share=stop.risk_per_share,
            gap_stress=atr * 0.5,
            slippage=entry.entry_price * 0.001,
            adv20_usd=None,
            price=entry.entry_price,
        )
        cand = {
            "candidate_id": cid,
            "run_id": run_id,
            "strategy_version": f"candidate_a/{ablation}",
            "archetype": "catalyst_accepted_continuation",
            "instrument_id": e["instrument_id"],
            "side": "long",
            "decision_time": e["available_at"],
            "decision_session": e["decision_session"],
            "event_cluster_id": None,
            "feature_snapshot_id": None,
            "entry_plan_json": json.dumps({
                "entry_type": entry.entry_type,
                "entry_price": entry.entry_price,
                "session": entry.session,
            }),
            "stop_plan_json": json.dumps({
                "stop_type": stop.stop_type,
                "stop_price": stop.stop_price,
                "risk_per_share": stop.risk_per_share,
            }),
            "reason_codes": json.dumps(["earnings_beat", ablation]),
            "vetoes": json.dumps([]),
            "created_at": store.utcnow_iso(),
            # runtime helpers for simulator
            "shares": sizing["shares"],
        }
        cands.append(cand)
    if cands:
        # Strip runtime helpers before upsert
        persist = [{k: v for k, v in c.items() if k != "shares"} for c in cands]
        store.upsert(conn, "research_candidate", persist)
    return cands


def run_bakeoff(*, start: str, end: str, synthetic: bool = True) -> str:
    config = {
        "start": start, "end": end, "synthetic": synthetic,
        "ablations": list(ABLATION_GRID.keys()),
        "strategy_version": "bakeoff-v1",
        "feature_version": "features-v1",
        "cost_model_version": "lab-v1",
    }
    with store.connect() as conn:
        run_id = start_run(conn, kind="bakeoff", config=config, seed=11)
        try:
            meta = _seed_synthetic_into_store(conn) if synthetic else {"sessions": []}
            sessions = meta.get("sessions") or []
            # Restrict to window
            sessions = [s for s in sessions if start <= s <= end]
            bars_by: Dict[str, List[dict]] = {}
            for row in store.read_rows(conn, "daily_bar", order_by="session_date ASC"):
                bars_by.setdefault(row["instrument_id"], []).append(row)

            scorecards = {}
            for ablation in ABLATION_GRID:
                cands = build_candidate_a(conn, ablation=ablation, run_id=f"{run_id}-{ablation}")
                # Filter candidates into window
                cands = [c for c in cands if start <= c["decision_session"] <= end]
                sim = run_simulation(
                    candidates=cands,
                    bars_by_instrument=bars_by,
                    sessions=sessions,
                    cost_model=LabCostModel(),
                    run_id=f"{run_id}-{ablation}",
                )
                # Stress: 2x cost
                stress_cost = LabCostModel()
                # cheap stress: re-run with elevated base bps via reconstructing
                from backend.research.cost_model import CostModel
                sim_stress = run_simulation(
                    candidates=cands,
                    bars_by_instrument=bars_by,
                    sessions=sessions,
                    cost_model=LabCostModel(base=CostModel(per_side_bps=20.0)),
                    run_id=f"{run_id}-{ablation}-stress2x",
                )
                scorecards[ablation] = {
                    "n_candidates": len(cands),
                    "n_closed": sim["n_closed"],
                    "expectancy_r": sim["expectancy_r"],
                    "stress_2x_expectancy_r": sim_stress["expectancy_r"],
                    "n_rejected": sim["n_rejected"],
                }

            # Candidate D: leadership alone vs leadership+compression (subset of grid)
            result = {
                "run_id": run_id,
                "scorecards": scorecards,
                "candidate_d_note": "Leadership filters encoded in event_leadership vs full_coiled_leader ablations",
                "promotion_hint": _promotion_hint(scorecards),
            }
            return finish_run(conn, run_id, ok=True, result=result)
        except Exception as exc:
            finish_run(conn, run_id, ok=False, result={"error": str(exc)})
            raise


def _promotion_hint(scorecards: Dict[str, Any]) -> str:
    best = None
    best_e = -999
    for name, sc in scorecards.items():
        e = float(sc.get("expectancy_r") or 0)
        if e > best_e and int(sc.get("n_closed") or 0) >= 5:
            best, best_e = name, e
    if best is None:
        return "insufficient_data"
    if best_e >= 0.15 and float(scorecards[best].get("stress_2x_expectancy_r") or 0) > 0:
        return "revise_or_promote_pending_oos"
    if best_e <= 0:
        return "kill"
    return "revise"
