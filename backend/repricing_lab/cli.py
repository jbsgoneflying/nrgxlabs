"""CLI for the Equity Repricing Lab.

    python -m backend.repricing_lab.cli backfill|qa|features|labels|bakeoff|report ...

All commands are inert unless invoked explicitly. Cron wrappers call this
module; production engines never import it.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import List, Optional

LOG = logging.getLogger("repricing_lab.cli")


def _cmd_backfill(args: argparse.Namespace) -> int:
    from backend.research.env_loader import load_research_env
    from backend.repricing_lab import store
    from backend.repricing_lab.bars import backfill_symbol_bars
    from backend.repricing_lab.corporate_actions import backfill_corporate_actions
    from backend.repricing_lab.instruments import upsert_instruments_from_exchange_list
    from backend.repricing_lab.universe_pit import build_universe_snapshot

    load_research_env()
    from backend.eodhd_client import EodhdClient

    client = EodhdClient.from_env()
    symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]
    summary = {"bars": 0, "splits": 0, "divs": 0, "instruments": 0, "universe": 0}

    with store.connect() as conn:
        started = store.record_job_start(conn, "lab_backfill")
        try:
            if args.instruments:
                active = client.get_exchange_symbols("US", delisted=False)
                n_a = upsert_instruments_from_exchange_list(conn, active.rows or [], delisted=False)
                try:
                    dead = client.get_exchange_symbols("US", delisted=True)
                    n_d = upsert_instruments_from_exchange_list(conn, dead.rows or [], delisted=True)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("delisted instrument list failed: %s", exc)
                    n_d = 0
                summary["instruments"] = n_a + n_d

            for sym in symbols:
                if args.bars:
                    summary["bars"] += backfill_symbol_bars(
                        conn, client, sym, from_date=args.from_date, to_date=args.to_date,
                        write_bronze=not args.no_bronze,
                    )
                if args.corporate_actions:
                    ns, nd = backfill_corporate_actions(
                        conn, client, sym, from_date=args.from_date, to_date=args.to_date,
                    )
                    summary["splits"] += ns
                    summary["divs"] += nd

            if args.universe and args.snapshot_date:
                summary["universe"] = build_universe_snapshot(
                    conn, snapshot_date=args.snapshot_date,
                )

            store.record_job_finish(conn, "lab_backfill", started, ok=True, detail=summary)
        except Exception as exc:
            store.record_job_finish(
                conn, "lab_backfill", started, ok=False, detail={"error": str(exc)},
            )
            raise

    print(json.dumps({"ok": True, **summary}, indent=2))
    return 0


def _cmd_qa(args: argparse.Namespace) -> int:
    from backend.repricing_lab import store
    from backend.repricing_lab.qa import run_qa, write_qa_report

    with store.connect() as conn:
        report = run_qa(conn, as_of=args.as_of)
        path = write_qa_report(report)
    print(json.dumps({"path": path, **report.to_dict()}, indent=2))
    return 1 if report.has_critical_failures() else 0


def _cmd_features(args: argparse.Namespace) -> int:
    from backend.repricing_lab import store
    from backend.repricing_lab.features.snapshot import build_feature_snapshots_for_date

    with store.connect() as conn:
        n = build_feature_snapshots_for_date(conn, as_of_date=args.as_of_date)
    print(json.dumps({"ok": True, "snapshots": n}))
    return 0


def _cmd_labels(args: argparse.Namespace) -> int:
    from backend.repricing_lab import store
    from backend.repricing_lab.labels import label_candidates_for_run

    with store.connect() as conn:
        n = label_candidates_for_run(conn, run_id=args.run_id)
    print(json.dumps({"ok": True, "labeled": n}))
    return 0


def _cmd_bakeoff(args: argparse.Namespace) -> int:
    from backend.repricing_lab.cohorts import run_bakeoff

    path = run_bakeoff(
        start=args.from_date,
        end=args.to_date,
        synthetic=bool(args.synthetic),
    )
    print(json.dumps({"ok": True, "artifact": path}))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from backend.repricing_lab.runs import write_promotion_report

    path = write_promotion_report(run_id=args.run_id, decision=args.decision)
    print(json.dumps({"ok": True, "artifact": path}))
    return 0


def _cmd_benchmarks(args: argparse.Namespace) -> int:
    from backend.repricing_lab.benchmarks import run_benchmarks

    path = run_benchmarks(synthetic=bool(args.synthetic))
    print(json.dumps({"ok": True, "artifact": path}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repricing_lab")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backfill", help="Ingest instruments/bars/CAs/universe")
    b.add_argument("--symbols", default="", help="Comma-separated tickers (AAPL,MSFT)")
    b.add_argument("--from-date", default="2017-01-01")
    b.add_argument("--to-date", default="2026-07-15")
    b.add_argument("--snapshot-date", default="")
    b.add_argument("--instruments", action="store_true")
    b.add_argument("--bars", action="store_true")
    b.add_argument("--corporate-actions", action="store_true")
    b.add_argument("--universe", action="store_true")
    b.add_argument("--no-bronze", action="store_true")
    b.set_defaults(func=_cmd_backfill)

    q = sub.add_parser("qa", help="Run data-quality gate")
    q.add_argument("--as-of", default=None)
    q.set_defaults(func=_cmd_qa)

    f = sub.add_parser("features", help="Build feature snapshots")
    f.add_argument("--as-of-date", required=True)
    f.set_defaults(func=_cmd_features)

    l = sub.add_parser("labels", help="Compute path-dependent labels for a run")
    l.add_argument("--run-id", required=True)
    l.set_defaults(func=_cmd_labels)

    k = sub.add_parser("bakeoff", help="Run Candidate A/D ablation bake-off")
    k.add_argument("--from-date", default="2018-01-01")
    k.add_argument("--to-date", default="2025-12-31")
    k.add_argument("--synthetic", action="store_true")
    k.set_defaults(func=_cmd_bakeoff)

    r = sub.add_parser("report", help="Write promotion decision report")
    r.add_argument("--run-id", required=True)
    r.add_argument("--decision", default="insufficient_data",
                   choices=["promote", "revise", "kill", "insufficient_data", "shadow", "live"])
    r.set_defaults(func=_cmd_report)

    m = sub.add_parser("benchmarks", help="Replay B0/B1/B2 benchmarks")
    m.add_argument("--synthetic", action="store_true")
    m.set_defaults(func=_cmd_benchmarks)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
