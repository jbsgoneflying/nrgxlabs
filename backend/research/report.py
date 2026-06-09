"""Strategy reports and the cross-strategy scorecard.

``build_strategy_report`` packages one strategy's outcome into a comparable dict:
full-sample stats, in-sample vs out-of-sample, yearly decay, optional tag
buckets (e.g. surprise quintiles), and data coverage. ``write_report`` persists
it as both JSON (machine) and a text summary (human) under
``backend/research/reports/``.

``build_scorecard`` ranks multiple reports by their **out-of-sample after-cost
edge** — the metric the plan's decision gate uses — so dead strategies are
obvious at a glance.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Dict, List, Optional

from backend.research.cohort_stats import CohortStats, group_by_tag, summarize
from backend.research.event_study import EventStudyOutcome, TradeResult
from backend.research.splits import decay_by_year, split_in_out

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")


def build_strategy_report(
    name: str,
    outcome: EventStudyOutcome,
    *,
    oos_start: str = "2023-01-01",
    bucket_tags: Optional[List[str]] = None,
    notes: str = "",
) -> dict:
    """Assemble a comparable report dict for one strategy."""
    results = outcome.results
    in_s, oos = split_in_out(results, oos_start)

    buckets: Dict[str, Dict[str, dict]] = {}
    for tag in bucket_tags or []:
        buckets[tag] = {k: v.to_dict() for k, v in group_by_tag(results, tag).items()}

    return {
        "strategy": name,
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "notes": notes,
        "oos_start": oos_start,
        "coverage": {
            "evaluated": outcome.n,
            "skipped": len(outcome.skipped),
            "coverage_ratio": round(outcome.coverage, 4),
        },
        "full_sample": summarize(results).to_dict(),
        "in_sample": summarize(in_s).to_dict(),
        "out_of_sample": summarize(oos).to_dict(),
        "decay_by_year": {y: s.to_dict() for y, s in decay_by_year(results).items()},
        "buckets": buckets,
    }


def write_report(report: dict, out_dir: str = REPORTS_DIR) -> Dict[str, str]:
    """Write a strategy report as JSON + a human-readable .txt. Returns paths."""
    os.makedirs(out_dir, exist_ok=True)
    name = _slug(report.get("strategy", "strategy"))
    json_path = os.path.join(out_dir, f"{name}.json")
    txt_path = os.path.join(out_dir, f"{name}.txt")

    with open(json_path, "w") as fh:
        json.dump(report, fh, indent=2)
    with open(txt_path, "w") as fh:
        fh.write(render_report_text(report))

    return {"json": json_path, "txt": txt_path}


def render_report_text(report: dict) -> str:
    lines: List[str] = []
    lines.append(f"=== {report['strategy']} ===")
    if report.get("notes"):
        lines.append(report["notes"])
    cov = report["coverage"]
    lines.append(
        f"coverage: {cov['evaluated']} trades evaluated, "
        f"{cov['skipped']} skipped ({cov['coverage_ratio']:.0%} coverage)"
    )
    lines.append("")
    for slice_name in ("full_sample", "in_sample", "out_of_sample"):
        lines.append(_fmt_stats_line(slice_name, report[slice_name]))
    lines.append("")
    lines.append("decay by year (entry year -> avg net / hit / n):")
    for y, s in report.get("decay_by_year", {}).items():
        lines.append(
            f"  {y}: {s['avg_net_return']:+.4%}  hit={s['hit_rate']:.0%}  n={s['n']}"
        )
    for tag, bucket in (report.get("buckets") or {}).items():
        lines.append("")
        lines.append(f"buckets by {tag} (avg net / hit / n):")
        for label, s in bucket.items():
            lines.append(
                f"  {label}: {s['avg_net_return']:+.4%}  hit={s['hit_rate']:.0%}  n={s['n']}"
            )
    return "\n".join(lines) + "\n"


def _fmt_stats_line(label: str, s: dict) -> str:
    return (
        f"{label:>14}: n={s['n']:<5} "
        f"avg_net={s['avg_net_return']:+.4%}  "
        f"hit={s['hit_rate']:.0%}  "
        f"t={s['t_stat']:+.2f}  "
        f"sharpe={s['sharpe_annualized']:+.2f}  "
        f"maxDD={s['max_drawdown']:.1%}"
    )


def build_scorecard(reports: List[dict]) -> dict:
    """Rank strategies by OOS after-cost edge for the decision gate.

    Ranking key: out-of-sample avg net return per trade, but a strategy is
    flagged ``alive=False`` when its OOS sample is thin (n<30), its OOS avg net
    return is <= 0, or its OOS t-stat < 1.5 (weak significance).
    """
    rows: List[dict] = []
    for r in reports:
        oos = r.get("out_of_sample", {})
        n = int(oos.get("n", 0))
        avg = float(oos.get("avg_net_return", 0.0))
        t = float(oos.get("t_stat", 0.0))
        sharpe = float(oos.get("sharpe_annualized", 0.0))
        alive = (n >= 30) and (avg > 0) and (t >= 1.5)
        rows.append(
            {
                "strategy": r.get("strategy"),
                "oos_n": n,
                "oos_avg_net_return": round(avg, 6),
                "oos_hit_rate": oos.get("hit_rate", 0.0),
                "oos_t_stat": round(t, 3),
                "oos_sharpe": round(sharpe, 3),
                "oos_max_drawdown": oos.get("max_drawdown", 0.0),
                "alive": alive,
            }
        )

    rows.sort(key=lambda x: (x["alive"], x["oos_avg_net_return"]), reverse=True)
    for i, row in enumerate(rows, 1):
        row["rank"] = i

    return {
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "ranking_key": "out_of_sample.avg_net_return (after costs)",
        "alive_criteria": "oos_n>=30 AND oos_avg_net>0 AND oos_t_stat>=1.5",
        "rows": rows,
    }


def render_scorecard_text(scorecard: dict) -> str:
    lines = ["=== EDGE BAKE-OFF SCORECARD (ranked by OOS after-cost edge) ==="]
    lines.append(scorecard["alive_criteria"])
    lines.append("")
    header = f"{'#':>2}  {'strategy':<28} {'n':>5} {'avg_net':>9} {'hit':>5} {'t':>6} {'sharpe':>7} {'alive':>6}"
    lines.append(header)
    lines.append("-" * len(header))
    for row in scorecard["rows"]:
        lines.append(
            f"{row['rank']:>2}  {str(row['strategy']):<28} "
            f"{row['oos_n']:>5} "
            f"{row['oos_avg_net_return']:>+8.3%} "
            f"{row['oos_hit_rate']:>4.0%} "
            f"{row['oos_t_stat']:>+6.2f} "
            f"{row['oos_sharpe']:>+7.2f} "
            f"{('YES' if row['alive'] else 'no'):>6}"
        )
    return "\n".join(lines) + "\n"


def write_scorecard(scorecard: dict, out_dir: str = REPORTS_DIR) -> Dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "scorecard.json")
    txt_path = os.path.join(out_dir, "scorecard.txt")
    with open(json_path, "w") as fh:
        json.dump(scorecard, fh, indent=2)
    with open(txt_path, "w") as fh:
        fh.write(render_scorecard_text(scorecard))
    return {"json": json_path, "txt": txt_path}


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")
