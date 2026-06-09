"""Edge Bake-Off command-line runner.

Run a strategy backtest, write its report, then build the cross-strategy
scorecard. LIVE subcommands hit the network (need API keys); ``demo`` runs the
whole pipeline on synthetic data with zero network.

Examples
--------
Offline end-to-end demo (no keys needed)::

    python -m backend.research.cli demo

Live PEAD on 50 names, 2-week hold, 2023+ out-of-sample::

    python -m backend.research.cli pead --limit 50 --start 2018-01-01 --end 2026-06-01

Build the scorecard from whatever reports exist::

    python -m backend.research.cli scorecard
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from typing import List

from backend.research.cost_model import CostModel
from backend.research.event_study import SignalEvent, run_event_study
from backend.research.report import (
    REPORTS_DIR,
    build_scorecard,
    build_strategy_report,
    render_report_text,
    render_scorecard_text,
    write_report,
    write_scorecard,
)
from backend.research.strategies.catalyst_convexity import (
    load_catalyst_calendar,
    run_convexity_study,
)
from backend.research.strategies.insider_cluster import generate_insider_cluster_events
from backend.research.strategies.llm_overlay import (
    HeuristicGuidanceGrader,
    OpenAIQualityGrader,
    run_quality_overlay,
)
from backend.research.strategies.pead import generate_pead_events
from backend.research.strategies.residual_reversal import (
    generate_residual_reversal_events,
)
from backend.research.universe import load_sp500


# ---------------------------------------------------------------------------
# Live provider wiring (imported lazily so `demo` needs no keys)
# ---------------------------------------------------------------------------

def _live_providers():
    from backend.research.live_providers import (
        ApiNinjasInsiderProvider,
        EodhdEarningsProvider,
        EodhdPriceProvider,
    )

    return EodhdPriceProvider(), EodhdEarningsProvider(), ApiNinjasInsiderProvider()


def _universe(limit: int) -> List[str]:
    names = load_sp500()
    return names[:limit] if limit and limit > 0 else names


def _finalize(name: str, outcome, args, *, bucket_tags=None, notes="") -> dict:
    report = build_strategy_report(
        name, outcome, oos_start=args.oos_start, bucket_tags=bucket_tags, notes=notes
    )
    paths = write_report(report, out_dir=args.out_dir)
    print(render_report_text(report))
    print(f"[written] {paths['json']}")
    print(f"[written] {paths['txt']}")
    return report


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_pead(args) -> None:
    price, earnings, _ = _live_providers()
    tickers = _universe(args.limit)
    events = generate_pead_events(
        earnings, tickers, args.start, args.end,
        min_abs_surprise=args.min_surprise, horizon_days=args.horizon,
        long_only=args.long_only,
    )
    print(f"[pead] generated {len(events)} signals across {len(tickers)} names")
    outcome = run_event_study(events, price, cost_model=CostModel(per_side_bps=args.cost_bps))
    _finalize(
        "PEAD", outcome, args,
        bucket_tags=["surprise_bucket", "surprise_sign"],
        notes="Pass A raw anomaly: trade sign of EPS surprise, hold ~2wk.",
    )


def cmd_reversal(args) -> None:
    price, _, _ = _live_providers()
    tickers = _universe(args.limit)
    events = generate_residual_reversal_events(
        price, tickers, args.market, args.start, args.end,
        formation_days=args.formation, hold_days=args.hold,
        beta_window=args.beta_window, top_frac=args.top_frac,
        rebalance_every=args.rebalance_every,
    )
    print(f"[reversal] generated {len(events)} long/short signals")
    outcome = run_event_study(events, price, cost_model=CostModel(per_side_bps=args.cost_bps))
    _finalize(
        "ResidualReversal", outcome, args, bucket_tags=["leg"],
        notes="High-turnover stat-arb. WATCH: survivorship (current SP500) + costs.",
    )


def cmd_insider(args) -> None:
    price, _, insider = _live_providers()
    tickers = _universe(args.limit)
    events = generate_insider_cluster_events(
        insider, tickers, args.start, args.end,
        min_distinct_buyers=args.min_buyers, window_days=args.window_days,
        min_net_dollars=args.min_dollars, horizon_days=args.horizon,
    )
    print(f"[insider] generated {len(events)} cluster signals")
    outcome = run_event_study(events, price, cost_model=CostModel(per_side_bps=args.cost_bps))
    _finalize(
        "InsiderCluster", outcome, args, bucket_tags=["cluster_bucket"],
        notes="Form-4 buy clusters -> multi-week drift.",
    )


def cmd_convexity(args) -> None:
    """Tier 2 pilot: long-straddle convexity into curated catalysts (live)."""
    from backend.research.live_providers import EodhdPriceProvider, OratsChainProvider

    catalysts = load_catalyst_calendar(args.calendar)
    if not catalysts:
        print("no catalysts found; populate data/universe/catalyst_calendar.json first")
        return
    print(f"[convexity] {len(catalysts)} curated catalysts")
    outcome = run_convexity_study(
        catalysts, EodhdPriceProvider(), OratsChainProvider(),
        entry_lead_days=args.entry_lead, exit_offset_days=args.exit_offset,
        target_dte=args.target_dte, premium_cost_pct=args.premium_cost,
    )
    _finalize(
        "CatalystConvexity", outcome, args, bucket_tags=["kind"],
        notes="PILOT. P&L = return on straddle premium (different basis). Small-n; directional only.",
    )


def cmd_overlay(args) -> None:
    """Pass B: PEAD guidance-quality LLM overlay (live)."""
    from backend.research.live_providers import (
        ApiNinjasTranscriptProvider,
        EodhdEarningsProvider,
        EodhdPriceProvider,
    )

    price = EodhdPriceProvider()
    earnings = EodhdEarningsProvider()
    transcripts = ApiNinjasTranscriptProvider()
    tickers = _universe(args.limit)

    events = generate_pead_events(
        earnings, tickers, args.start, args.end,
        min_abs_surprise=args.min_surprise, horizon_days=args.horizon,
    )
    print(f"[overlay] grading {len(events)} PEAD events")
    grader = HeuristicGuidanceGrader() if args.heuristic else OpenAIQualityGrader()

    def ctx(ev):
        return {"text": transcripts.get_text(ev.ticker, ev.signal_date)}

    out = run_quality_overlay(
        events, price, grader, ctx,
        cost_model=CostModel(per_side_bps=args.cost_bps), oos_start=args.oos_start,
    )
    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, "overlay_pead.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=2)
    verdict = out.get("out_of_sample", out["full"])
    print(json.dumps(verdict, indent=2))
    print(
        f"\n[verdict] LLM overlay adds incremental OOS edge: "
        f"{verdict.get('adds_incremental_edge')}  "
        f"(top quintile {verdict.get('top_quintile_avg_net'):+.3%} vs full "
        f"{verdict.get('full_sample_avg_net'):+.3%})"
    )
    print(f"[written] {path}")


def cmd_scorecard(args) -> None:
    reports = []
    for fn in sorted(os.listdir(args.out_dir)) if os.path.isdir(args.out_dir) else []:
        if fn.endswith(".json") and fn != "scorecard.json":
            with open(os.path.join(args.out_dir, fn)) as fh:
                try:
                    reports.append(json.load(fh))
                except Exception:
                    pass
    if not reports:
        print("no strategy reports found; run a strategy or `demo` first")
        return
    sc = build_scorecard(reports)
    paths = write_scorecard(sc, out_dir=args.out_dir)
    print(render_scorecard_text(sc))
    print(f"[written] {paths['json']}")
    print(f"[written] {paths['txt']}")


def cmd_gate(args) -> None:
    """Read scorecard.json (+ overlay) and emit a build recommendation."""
    from backend.research.decision_gate import build_decision_gate, render_decision_gate_text

    sc_path = os.path.join(args.out_dir, "scorecard.json")
    if not os.path.exists(sc_path):
        print("no scorecard.json found; run `scorecard` (or `demo`) first")
        return
    with open(sc_path) as fh:
        scorecard = json.load(fh)

    overlay_verdicts = {}
    ov_path = os.path.join(args.out_dir, "overlay_pead.json")
    if os.path.exists(ov_path):
        with open(ov_path) as fh:
            ov = json.load(fh)
        verdict = ov.get("out_of_sample", ov.get("full", {}))
        overlay_verdicts["PEAD"] = verdict

    gate = build_decision_gate(scorecard, overlay_verdicts)
    with open(os.path.join(args.out_dir, "decision_gate.json"), "w") as fh:
        json.dump(gate, fh, indent=2)
    print(render_decision_gate_text(gate))
    print(f"[written] {os.path.join(args.out_dir, 'decision_gate.json')}")


def cmd_demo(args) -> None:
    """Run the full pipeline on synthetic data (no network)."""
    from backend.research.synthetic import all_synthetic_tickers, build_synthetic_dataset

    price, earnings, insider, injected = build_synthetic_dataset()
    tickers = all_synthetic_tickers()
    cost = CostModel(per_side_bps=args.cost_bps)
    reports = []

    # PEAD (should show a clear edge)
    pead_events = generate_pead_events(earnings, tickers, "2021-01-01", "2025-12-31", horizon_days=10)
    pead_out = run_event_study(pead_events, price, cost_model=cost)
    reports.append(_finalize("PEAD", pead_out, args, bucket_tags=["surprise_sign"],
                             notes="[SYNTHETIC DEMO] injected drift = sign(surprise)."))

    # Insider clusters (should show an edge)
    ins_events = generate_insider_cluster_events(insider, tickers, "2021-01-01", "2025-12-31",
                                                 min_net_dollars=100_000, horizon_days=10)
    ins_out = run_event_study(ins_events, price, cost_model=cost)
    reports.append(_finalize("InsiderCluster", ins_out, args, bucket_tags=["cluster_bucket"],
                             notes="[SYNTHETIC DEMO] injected upward drift post-cluster."))

    # Random control (should look dead)
    rng = random.Random(123)
    ctrl_events: List[SignalEvent] = []
    for t in tickers:
        bars = price.get_bars(t, "2021-01-01", "2025-09-01")
        for _ in range(20):
            b = rng.choice(bars)
            ctrl_events.append(SignalEvent(t, b.date, rng.choice([-1, 1]), 10, "RandomControl"))
    ctrl_out = run_event_study(ctrl_events, price, cost_model=cost)
    reports.append(_finalize("RandomControl", ctrl_out, args,
                             notes="[SYNTHETIC DEMO] no injected edge -> expected dead."))

    sc = build_scorecard(reports)
    paths = write_scorecard(sc, out_dir=args.out_dir)
    print(render_scorecard_text(sc))
    print(f"[written] {paths['json']}")


# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--end", default="2026-06-01")
    p.add_argument("--oos-start", default="2023-01-01")
    p.add_argument("--cost-bps", type=float, default=10.0, help="per-side cost in bps")
    p.add_argument("--horizon", type=int, default=10, help="hold in trading days")
    p.add_argument("--limit", type=int, default=50, help="max tickers (cost control)")
    p.add_argument("--out-dir", default=REPORTS_DIR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="edge-bakeoff", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pead", help="PEAD drift (live)")
    _add_common(p)
    p.add_argument("--min-surprise", type=float, default=0.05)
    p.add_argument("--long-only", action="store_true")
    p.set_defaults(func=cmd_pead)

    p = sub.add_parser("reversal", help="residual reversal (live)")
    _add_common(p)
    p.add_argument("--market", default="SPY")
    p.add_argument("--formation", type=int, default=5)
    p.add_argument("--hold", type=int, default=5)
    p.add_argument("--beta-window", type=int, default=60)
    p.add_argument("--top-frac", type=float, default=0.1)
    p.add_argument("--rebalance-every", type=int, default=5)
    p.set_defaults(func=cmd_reversal)

    p = sub.add_parser("insider", help="insider cluster drift (live)")
    _add_common(p)
    p.add_argument("--min-buyers", type=int, default=2)
    p.add_argument("--window-days", type=int, default=7)
    p.add_argument("--min-dollars", type=float, default=100_000.0)
    p.set_defaults(func=cmd_insider)

    p = sub.add_parser("convexity", help="Tier 2 pilot: catalyst convexity straddle (live)")
    _add_common(p)
    p.add_argument("--calendar", default=None, help="path to catalyst_calendar.json")
    p.add_argument("--entry-lead", type=int, default=10)
    p.add_argument("--exit-offset", type=int, default=1)
    p.add_argument("--target-dte", type=int, default=30)
    p.add_argument("--premium-cost", type=float, default=0.04)
    p.set_defaults(func=cmd_convexity)

    p = sub.add_parser("overlay", help="Pass B: PEAD guidance-quality LLM overlay (live)")
    _add_common(p)
    p.add_argument("--min-surprise", type=float, default=0.05)
    p.add_argument("--heuristic", action="store_true",
                   help="use offline keyword grader instead of OpenAI")
    p.set_defaults(func=cmd_overlay)

    p = sub.add_parser("scorecard", help="rank all written reports")
    p.add_argument("--out-dir", default=REPORTS_DIR)
    p.set_defaults(func=cmd_scorecard)

    p = sub.add_parser("gate", help="emit build recommendation from scorecard + overlay")
    p.add_argument("--out-dir", default=REPORTS_DIR)
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("demo", help="run full pipeline on synthetic data (no network)")
    _add_common(p)
    p.set_defaults(func=cmd_demo)

    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd not in ("demo", "scorecard", "gate"):
        from backend.research.env_loader import load_research_env

        load_research_env()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
