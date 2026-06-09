"""Decision gate — turn the scorecard (+ Pass B overlay) into a build recommendation.

This is the plan's final step expressed as code: read the cross-strategy
scorecard and any Pass B overlay verdicts, then recommend the 1-2 strategies to
promote to a production engine. A strategy is *promotable* when it is alive
out-of-sample AND (if an overlay verdict exists) the LLM overlay adds
incremental edge — i.e. both the anomaly and the moat check out.

Output is a structured dict + text (no markdown doc), consistent with the rest
of the harness. Run via ``python -m backend.research.cli gate``.
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional


def build_decision_gate(
    scorecard: dict,
    overlay_verdicts: Optional[Dict[str, dict]] = None,
    *,
    max_promote: int = 2,
) -> dict:
    """Recommend strategies to build from a scorecard (+ optional overlay verdicts).

    overlay_verdicts: {strategy_name: <out_of_sample incremental_edge dict>}.
    """
    overlay_verdicts = overlay_verdicts or {}
    rows = scorecard.get("rows", [])

    promote: List[dict] = []
    watch: List[dict] = []
    drop: List[dict] = []

    for row in rows:
        name = row.get("strategy")
        alive = bool(row.get("alive"))
        ov = overlay_verdicts.get(name)
        overlay_adds = ov.get("adds_incremental_edge") if ov else None

        entry = {
            "strategy": name,
            "oos_avg_net_return": row.get("oos_avg_net_return"),
            "oos_t_stat": row.get("oos_t_stat"),
            "oos_n": row.get("oos_n"),
            "alive_oos": alive,
            "overlay_adds_edge": overlay_adds,
        }

        if not alive:
            entry["reason"] = "no out-of-sample edge (fails alive criteria)"
            drop.append(entry)
        elif overlay_adds is False:
            entry["reason"] = "anomaly alive but LLM overlay adds no incremental edge (moat unproven)"
            watch.append(entry)
        else:
            # alive and (overlay adds edge OR overlay not yet tested)
            entry["reason"] = (
                "alive OOS + overlay adds edge" if overlay_adds
                else "alive OOS; run Pass B overlay to confirm moat before building"
            )
            promote.append(entry)

    promote = promote[:max_promote]

    if promote:
        names = ", ".join(p["strategy"] for p in promote)
        recommendation = f"BUILD: {names}. Scope production engine(s) next."
    elif watch:
        recommendation = "HOLD: anomalies alive but moat unproven; iterate on the LLM overlay before building."
    else:
        recommendation = "STOP: no strategy cleared the out-of-sample gate; revisit hypotheses or data."

    return {
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "recommendation": recommendation,
        "promote": promote,
        "watch": watch,
        "drop": drop,
        "criteria": {
            "promote": "alive OOS AND (overlay adds edge OR overlay untested)",
            "watch": "alive OOS BUT overlay adds no incremental edge",
            "drop": "not alive OOS",
            "max_promote": max_promote,
        },
    }


def render_decision_gate_text(gate: dict) -> str:
    lines = ["=== EDGE BAKE-OFF DECISION GATE ===", ""]
    lines.append(f">>> {gate['recommendation']}")
    lines.append("")
    for label, key in (("PROMOTE", "promote"), ("WATCH", "watch"), ("DROP", "drop")):
        items = gate.get(key, [])
        lines.append(f"{label} ({len(items)}):")
        for it in items:
            ov = it.get("overlay_adds_edge")
            ov_s = "n/a" if ov is None else ("yes" if ov else "no")
            lines.append(
                f"  - {it['strategy']}: oos_avg_net={it.get('oos_avg_net_return')} "
                f"t={it.get('oos_t_stat')} n={it.get('oos_n')} overlay_edge={ov_s}"
            )
            lines.append(f"      {it.get('reason','')}")
        lines.append("")
    return "\n".join(lines) + "\n"
