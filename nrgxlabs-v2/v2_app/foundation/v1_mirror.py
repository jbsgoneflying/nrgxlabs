"""v1 → v2 conformal calibration mirror.

Walks v1's Redis trade journals and replays each closed trade's
(predicted breach probability, realized breach outcome) pair into the
v2 conformal calibrator. After running this once on a populated v1
desk, the conformal tile on the v2 dashboard reads non-synthetic
empirical coverage on real PnL outcomes.

v1 schema (confirmed via grep across the repo):

- E1 (earnings IC): keys ``e1:trades:{trade_id}``, index ``e1:trades:index``
- E2 (SPX IC):       keys ``e2:trades:{trade_id}``, index ``e2:trades:index``

Each trade JSON document has::

    {
      "tradeId":    str,
      "status":     "active" | "monitoring" | "closed",
      "entryContext": {
          "breachPct": float,        # 0..100 (percent), set by frontend
          "vrpScore":  float,        # E1 only
          ...
      },
      "outcome": {                   # only on closed trades
          "outcomeClass": "win" | "loss" | "scratch",
          "realizedPnl":  float,
          "maxBreachProximity": float,   # 0..100
          ...
      },
      ...
    }

Mirror logic for breach_probability:

- prediction: ``entryContext.breachPct / 100.0`` (normalize to [0, 1])
- realized:   ``1.0`` if ``outcome.outcomeClass == "loss"`` else ``0.0``

We use loss-class as the breach indicator because, on credit-spread iron
condors, the dominant loss driver is the underlying breaching one of the
short strikes. Trades that scratched or won did not breach. This is the
same definition the v1 desk reasons about implicitly when it tracks
"breach rate".

Future iterations will add:

- per-ticker calibrators for E1 (earnings tickers vary widely in vol)
- a "PnL %% of credit" continuous metric using the realizedPnl / entryCredit
- a "max breach proximity" continuous metric (regression rather than binary)
"""

from __future__ import annotations

import logging
from typing import Any

from .conformal_store import _redis_client, load_calibrator, now_ts, save_calibrator

LOG = logging.getLogger("v2.v1_mirror")


# ── Engine map ───────────────────────────────────────────


_ENGINE_SOURCES = {
    "e1": {"index_key": "e1:trades:index", "trade_prefix": "e1:trades:"},
    "e2": {"index_key": "e2:trades:index", "trade_prefix": "e2:trades:"},
}


# ── Public API ───────────────────────────────────────────


def mirror_v1_breach_probability(
    *,
    only_engine: str | None = None,
    reset: bool = True,
    max_per_engine: int = 5000,
) -> dict[str, Any]:
    """Replay v1 closed trades into the v2 ``breach_probability`` calibrator.

    Args:
        only_engine: If set to "e1" or "e2", restrict to that engine only.
        reset:       If True (default), clear each touched calibrator before
                     replaying so re-running the mirror is idempotent.
                     If False, append onto the existing rolling window.
        max_per_engine: Hard cap on trades processed per engine (defensive).

    Returns:
        Summary dict with per-engine counts and skip reasons.
    """
    summary: dict[str, Any] = {
        "ok": True,
        "started_at": now_ts(),
        "metric": "breach_probability",
        "reset": reset,
        "engines": {},
        "redis_available": True,
    }

    client = _redis_client()
    if client is None:
        summary["ok"] = False
        summary["redis_available"] = False
        return summary

    targets = (
        {only_engine: _ENGINE_SOURCES[only_engine]}
        if only_engine and only_engine in _ENGINE_SOURCES
        else _ENGINE_SOURCES
    )

    for engine, src in targets.items():
        engine_summary = _mirror_one_engine(
            engine=engine,
            client=client,
            index_key=src["index_key"],
            trade_prefix=src["trade_prefix"],
            reset=reset,
            cap=max_per_engine,
        )
        summary["engines"][engine] = engine_summary

    # Convenience aggregate.
    summary["n_observations_logged"] = sum(
        e.get("n_observations_logged", 0) for e in summary["engines"].values()
    )
    summary["n_trades_seen"] = sum(
        e.get("n_trades_seen", 0) for e in summary["engines"].values()
    )
    summary["finished_at"] = now_ts()
    return summary


# ── Internal ─────────────────────────────────────────────


def _mirror_one_engine(
    *,
    engine: str,
    client: Any,
    index_key: str,
    trade_prefix: str,
    reset: bool,
    cap: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "n_trades_seen": 0,
        "n_closed": 0,
        "n_observations_logged": 0,
        "skips": {
            "not_closed": 0,
            "no_breach_prediction": 0,
            "no_outcome_class": 0,
            "out_of_range_prediction": 0,
            "load_failed": 0,
        },
    }

    try:
        index = _get_json(client, index_key) or []
    except Exception as exc:
        LOG.warning("mirror: failed to read %s: %s", index_key, exc)
        return out

    trade_ids = list(index)[:cap]

    metric = "breach_probability"
    cal = load_calibrator(engine, metric)
    if reset:
        cal.state.scores.clear()

    # Track which extraction path actually populated each observation so the
    # operator can see at a glance whether v1 is storing the prediction in
    # the canonical place or via one of the documented fallbacks.
    out["sources"] = {}

    for tid in trade_ids:
        out["n_trades_seen"] += 1
        try:
            doc = _get_json(client, f"{trade_prefix}{tid}")
        except Exception as exc:
            LOG.debug("mirror: load failed for %s: %s", tid, exc)
            out["skips"]["load_failed"] += 1
            continue
        if not isinstance(doc, dict):
            out["skips"]["load_failed"] += 1
            continue

        if doc.get("status") != "closed":
            out["skips"]["not_closed"] += 1
            continue
        out["n_closed"] += 1

        prediction, source = _extract_breach_prediction(doc)
        if prediction is None:
            out["skips"]["no_breach_prediction"] += 1
            continue
        if not (0.0 <= prediction <= 1.0):
            out["skips"]["out_of_range_prediction"] += 1
            continue

        outcome = doc.get("outcome") or {}
        oc = outcome.get("outcomeClass")
        if oc not in ("win", "loss", "scratch"):
            out["skips"]["no_outcome_class"] += 1
            continue

        realized = 1.0 if oc == "loss" else 0.0
        ts = doc.get("closedAt") or doc.get("loggedAt")
        cal.observe(prediction=prediction, realized=realized, ts=ts)
        out["n_observations_logged"] += 1
        out["sources"][source] = out["sources"].get(source, 0) + 1

    persisted = save_calibrator(engine, metric, cal)
    out["persisted"] = persisted
    out["final_n_calibration"] = cal.state.n
    return out


def _extract_breach_prediction(doc: dict[str, Any]) -> tuple[float | None, str]:
    """Find the predicted breach probability in a v1 trade document.

    v1 stores this in several places depending on which engine + advisor
    path produced the trade. We try them in priority order — the first
    non-null, in-range value wins. Returns (prediction, source_label)
    where source_label is e.g. "entryContext.breachPct" so the operator
    can audit which path is being used in practice.

    All v1 values live on a 0..100 percent scale; we normalize to [0, 1]
    for the conformal calibrator.
    """
    candidates: list[tuple[str, Any]] = [
        # 1. The canonical place — set by the SPX IC advisor (engine2_spx_ic.py
        #    line 3994 of static/app.js: breachPct = breach_close_prob * 100).
        ("entryContext.breachPct", _safe_get(doc, "entryContext", "breachPct")),
        # 2. Top-level breach snapshot (from the e1 post-mortem prompt schema —
        #    backend/prompts/e1_post_mortem.txt: "breachSnapshot — breach rate").
        ("breachSnapshot.breachRate", _safe_get(doc, "breachSnapshot", "breachRate")),
        ("breachSnapshot.breachRatePct", _safe_get(doc, "breachSnapshot", "breachRatePct")),
        ("breachSnapshot.breachPct", _safe_get(doc, "breachSnapshot", "breachPct")),
        ("breachSnapshot.emBreachPct", _safe_get(doc, "breachSnapshot", "emBreachPct")),
        # 3. Entry-side mirror used by some E1 advisor paths.
        ("entry.emBreachPct", _safe_get(doc, "entry", "emBreachPct")),
        ("entry.breachPct", _safe_get(doc, "entry", "breachPct")),
        # 4. EM-bucketed breach summary (E1 keyed by EM multiple, e.g. {"1.0": 18, "1.5": 12}).
        #    We pick the smallest valid value as the "tightest wing" estimate so the calibrator
        #    sees a defensible single number rather than an arbitrary key.
        ("entryContext.emBreachSummary[min]", _min_of_dict_values(_safe_get(doc, "entryContext", "emBreachSummary"))),
        # 5. Market snapshot fallback (rare).
        ("marketSnapshot.breachPct", _safe_get(doc, "marketSnapshot", "breachPct")),
    ]
    for label, raw in candidates:
        try:
            if raw is None:
                continue
            v = float(raw) / 100.0
            if 0.0 <= v <= 1.0:
                return v, label
        except (TypeError, ValueError):
            continue
    return None, "none"


def _safe_get(doc: dict[str, Any], *path: str) -> Any:
    cur: Any = doc
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _min_of_dict_values(d: Any) -> float | None:
    if not isinstance(d, dict) or not d:
        return None
    vals: list[float] = []
    for v in d.values():
        try:
            f = float(v)
            if 0.0 <= f <= 100.0:
                vals.append(f)
        except (TypeError, ValueError):
            continue
    return min(vals) if vals else None


def _get_json(client: Any, key: str) -> Any:
    """Read a JSON value from Redis. Returns None on miss."""
    raw = client.get(key)
    if raw is None:
        return None
    try:
        import json
        return json.loads(raw)
    except Exception:
        return None
