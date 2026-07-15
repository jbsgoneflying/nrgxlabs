"""Benchmark replays B0/B1/B2 against the lab simulator / event-study harness."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from backend.research.cost_model import CostModel
from backend.research.event_study import run_event_study
from backend.research.strategies.pead import generate_pead_events
from backend.research.synthetic import all_synthetic_tickers, build_synthetic_dataset
from backend.repricing_lab import store
from backend.repricing_lab.runs import finish_run, start_run, write_artifact

LOG = logging.getLogger("repricing_lab.benchmarks")


def run_benchmarks(*, synthetic: bool = True) -> str:
    """Replay PEAD (B0), PEAD hold variants (B1), and a lightweight B2 stub.

    Synthetic mode keeps CI offline. Live mode would swap providers for EODHD.
    """
    config = {"benchmarks": ["B0", "B1", "B2"], "synthetic": synthetic, "strategy_version": "bench-v1"}
    with store.connect() as conn:
        run_id = start_run(conn, kind="benchmark", config=config, seed=7)
        try:
            price, earnings, _insider, _inj = build_synthetic_dataset(seed=7, n_tickers=20)
            tickers = all_synthetic_tickers(20)
            events = generate_pead_events(
                earnings, tickers, "2021-01-01", "2025-12-31",
                min_abs_surprise=0.05, horizon_days=10, long_only=True,
            )
            b0 = run_event_study(events, price, cost_model=CostModel.liquid_large_cap())
            # B1: hold variants 10/20/40
            b1 = {}
            for h in (10, 20, 40):
                ev = generate_pead_events(
                    earnings, tickers, "2021-01-01", "2025-12-31",
                    min_abs_surprise=0.05, horizon_days=h, long_only=True,
                    strategy_name=f"PEAD_{h}",
                )
                out = run_event_study(ev, price, cost_model=CostModel.liquid_large_cap())
                b1[str(h)] = _summarize_event_study(out)

            # B2: ichimoku parity is validated in unit tests against evaluate_outcome;
            # here we record a placeholder scorecard confirming the adapter wiring.
            b2 = {
                "adapter": "engine4_backtest.evaluate_outcome via engine3_red_dog",
                "note": "Full live Ichimoku replay requires ORATS/EODHD bars; synthetic parity covered in tests.",
                "status": "wired",
            }

            result = {
                "run_id": run_id,
                "B0_pead": _summarize_event_study(b0),
                "B1_hold_variants": b1,
                "B2_ichimoku": b2,
            }
            path = finish_run(conn, run_id, ok=True, result=result)
            return path
        except Exception as exc:
            finish_run(conn, run_id, ok=False, result={"error": str(exc)})
            raise


def _summarize_event_study(outcome: Any) -> Dict[str, Any]:
    trades = getattr(outcome, "results", None) or getattr(outcome, "trades", None) or []
    rets = [float(t.net_return) for t in trades if getattr(t, "net_return", None) is not None]
    return {
        "n_trades": len(trades),
        "mean_net_return": (sum(rets) / len(rets)) if rets else 0.0,
        "hit_rate": (sum(1 for r in rets if r > 0) / len(rets)) if rets else 0.0,
    }
