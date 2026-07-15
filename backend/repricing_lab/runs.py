"""Research-run registry + promotion report artifacts."""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from backend.config import get_flags
from backend.repricing_lab import store

LOG = logging.getLogger("repricing_lab.runs")


def git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent.parent,
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return "unknown"


def config_hash(config: Dict[str, Any]) -> str:
    blob = json.dumps(config, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:8]


def new_run_id(kind: str, config: Dict[str, Any]) -> str:
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    return f"{ts}-{git_sha()}-{config_hash(config)}-{kind[:6]}"


def start_run(conn, *, kind: str, config: Dict[str, Any], seed: Optional[int] = None) -> str:
    rid = new_run_id(kind, config)
    store.upsert(conn, "research_run", [{
        "run_id": rid,
        "kind": kind,
        "code_version": git_sha(),
        "config_json": json.dumps(config, sort_keys=True, default=str),
        "config_hash": config_hash(config),
        "data_version": None,
        "feature_version": config.get("feature_version"),
        "strategy_version": config.get("strategy_version"),
        "cost_model_version": config.get("cost_model_version"),
        "seed": seed,
        "started_at": store.utcnow_iso(),
        "completed_at": None,
        "status": "running",
        "result_uri": None,
    }])
    return rid


def finish_run(conn, run_id: str, *, ok: bool, result: Dict[str, Any], result_uri: Optional[str] = None) -> str:
    if result_uri is None:
        result_uri = write_artifact(run_id, result)
    rows = store.read_rows(conn, "research_run", where="run_id = ?", params=(run_id,), limit=1)
    if not rows:
        raise KeyError(run_id)
    row = dict(rows[0])
    row["completed_at"] = store.utcnow_iso()
    row["status"] = "ok" if ok else "failed"
    row["result_uri"] = result_uri
    store.upsert(conn, "research_run", [row])
    return result_uri


def write_artifact(run_id: str, payload: Dict[str, Any]) -> str:
    flags = get_flags()
    runs = Path(str(getattr(flags, "REPRICING_LAB_RUNS_DIR", "data/lab_runs")))
    if not runs.is_absolute():
        runs = (Path(__file__).resolve().parent.parent.parent / runs).resolve()
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"{run_id}.json"
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, default=str)
    return str(path)


def write_promotion_report(*, run_id: str, decision: str) -> str:
    """Record a promotion_decision row + human-readable report artifact."""
    criteria = {
        "min_oos_clusters": 150,
        "min_expectancy_r": 0.15,
        "bootstrap_ci_gt_0": True,
        "stress_2x_cost": True,
        "stress_1d_delay": True,
        "max_corr_existing": 0.6,
        "note": "Desk sign-off required. Automated gate is advisory until shadow period completes.",
    }
    with store.connect() as conn:
        rows = store.read_rows(conn, "research_run", where="run_id = ?", params=(run_id,), limit=1)
        strategy = (rows[0]["strategy_version"] if rows else None) or "unknown"
        did = hashlib.sha256(f"{run_id}|{decision}".encode()).hexdigest()[:16]
        store.upsert(conn, "promotion_decision", [{
            "decision_id": did,
            "run_id": run_id,
            "strategy_version": strategy,
            "archetype": "candidate_a_earnings_continuation",
            "decision": decision,
            "criteria_json": json.dumps(criteria, sort_keys=True),
            "decided_at": store.utcnow_iso(),
            "decided_by": "repricing_lab.cli",
        }])
        # Pull expectancy if present in artifact
        result = {}
        if rows and rows[0].get("result_uri"):
            try:
                with open(rows[0]["result_uri"]) as fh:
                    result = json.load(fh)
            except Exception:
                result = {}
        report = {
            "run_id": run_id,
            "decision": decision,
            "criteria": criteria,
            "resultSummary": {
                "expectancy_r": result.get("expectancy_r"),
                "n_closed": result.get("n_closed"),
                "n_rejected": result.get("n_rejected"),
            },
            "recommendation": _narrative(decision, result),
        }
        return write_artifact(f"promotion-{run_id}-{decision}", report)


def _narrative(decision: str, result: dict) -> str:
    if decision == "promote":
        return "Promote to shadow: gates met on frozen bake-off; begin prospective shadow period."
    if decision == "kill":
        return "Kill: expectancy or stress gates failed; archive and do not deploy."
    if decision == "revise":
        return "Revise: signal present but unstable; freeze spec changes and re-run ablations."
    return (
        f"Insufficient data or pending review (expectancy_r={result.get('expectancy_r')}, "
        f"n={result.get('n_closed')}). Do not register a production engine."
    )
