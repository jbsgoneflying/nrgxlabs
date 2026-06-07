"""Phase 1 module 2 — analogue retrieval (feature-space MVP).

The cross-ticker / cross-time wedge. v1's E15 finds analogues only inside
the same ticker's history, which severely caps sample size for any given
setup. This module drops that constraint:

    "Show me every closed v1 trade — across every ticker, across every
     calendar year — whose entry context looks most like THIS one. Let
     the desk see the empirical outcome distribution at a glance."

This is the Phase 1 MVP. The embedding is a hand-crafted, z-scored
feature vector — not a learned contrastive embedding (Phase 2 ships that
on top of this same index API). But hand-crafted feature-space ANN is
already enormously useful and is what most production "similar trades"
systems run in practice.

Design choices

- Pure Python: no numpy, no scikit-learn. With <10K trades a brute-force
  cosine similarity sweep is sub-millisecond and the dependency surface
  stays tiny.
- All-numerical features. Categoricals (timing AMC/BMO, regime bucket)
  are one-hot expanded into binary features. Missing values get a
  sentinel that contributes 0 to the cosine score (column is masked).
- z-score standardization computed at index-build time, persisted with
  the index so search-time queries get the same transform.
- Per-engine indexes (e1, e2): an SPX IC and an earnings IC live in
  different feature spaces and shouldn't share a kNN graph.

Future work tracked in module 2 phase 2: replace ``extract_features``
with a learned contrastive encoder (small Transformer over the trade's
multi-timeseries + categorical context), keep the index API stable so
the swap is internal.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .vectorspace import (
    Standardizer,
    feature_coverage as _feature_coverage_helper,
    masked_cosine,
)

LOG = logging.getLogger("v2.analogues")


# ── Feature space ──────────────────────────────────────────


# Per-engine feature definitions. Each entry: (name, [path...], default_to_skip).
# The path is searched in order on the trade document; first non-null number wins.
# Paths can dot into arbitrary depth; brackets denote dict-min reductions for
# EM-bucketed summaries.

_E1_FEATURES: list[tuple[str, list[list[str]]]] = [
    ("vrpScore",        [["entryContext", "vrpScore"], ["vrpSnapshot", "vrpScore"]]),
    ("breachPct",       [["entryContext", "breachPct"], ["breachSnapshot", "breachRatePct"]]),
    ("emMultiple",      [["entry", "emMultiple"], ["entry", "emMult"]]),
    ("ivRank",          [["vrpSnapshot", "ivRank"], ["entryContext", "ivRank"]]),
    ("ivPercentile",    [["vrpSnapshot", "ivPercentile"]]),
    ("daysToExpiry",    [["entry", "daysToExpiry"], ["entry", "dte"]]),
    ("vix",             [["marketSnapshot", "vix"], ["marketSnapshot", "vixSpot"]]),
    # Categorical → binary (1.0 if AMC, 0.0 if BMO).
    ("isAmc",           [["entry", "timing"], ["entryContext", "earningsTiming"]]),
]

_E2_FEATURES: list[tuple[str, list[list[str]]]] = [
    ("regimeScore",     [["entryContext", "regimeScore"]]),
    ("breachPct",       [["entryContext", "breachPct"], ["breachSnapshot", "breachRatePct"]]),
    ("emMultiple",      [["entry", "emMultiple"], ["entry", "wingMultiple"]]),
    ("daysToExpiry",    [["entry", "daysToExpiry"], ["entry", "dte"]]),
    ("vix",             [["marketSnapshot", "vix"], ["marketSnapshot", "vixSpot"]]),
    ("entryCredit",     [["entry", "entryCredit"]]),
]

ENGINE_FEATURES: dict[str, list[tuple[str, list[list[str]]]]] = {
    "e1": _E1_FEATURES,
    "e2": _E2_FEATURES,
}


def feature_names(engine: str) -> list[str]:
    return [name for name, _ in ENGINE_FEATURES.get(engine, [])]


def extract_features(trade: Mapping[str, Any], engine: str) -> dict[str, float | None]:
    """Pull every numerical feature for ``engine`` from a trade document.

    Missing or non-numeric paths return ``None``, which the index treats as
    a masked dimension at search time (zero contribution to cosine score
    for that column, applied symmetrically to query and key).
    """
    spec = ENGINE_FEATURES.get(engine)
    if not spec:
        return {}
    out: dict[str, float | None] = {}
    for name, paths in spec:
        out[name] = _resolve(trade, name, paths)
    return out


def _resolve(trade: Mapping[str, Any], name: str, paths: list[list[str]]) -> float | None:
    for path in paths:
        v = _safe_get(trade, *path)
        if v is None:
            continue
        # Categorical mappings get special treatment.
        if name == "isAmc":
            s = str(v).strip().upper()
            if s in ("AMC", "AFTER", "PM"):
                return 1.0
            if s in ("BMO", "BEFORE", "AM"):
                return 0.0
            continue
        try:
            f = float(v)
            if math.isnan(f) or math.isinf(f):
                continue
            return f
        except (TypeError, ValueError):
            continue
    return None


def _safe_get(doc: Any, *path: str) -> Any:
    cur: Any = doc
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


# ── Index ──────────────────────────────────────────────────


@dataclass
class IndexedTrade:
    trade_id: str
    ticker: str
    closed_at: str | None
    z_vec: list[float]
    mask: list[int]
    features: dict[str, float | None]
    outcome: dict[str, Any]


@dataclass
class AnalogueIndex:
    """In-memory analogue index for one engine."""

    engine: str
    standardizer: Standardizer
    feature_names: list[str]
    rows: list[IndexedTrade] = field(default_factory=list)

    @property
    def n_indexed(self) -> int:
        return len(self.rows)

    def search(
        self,
        query_features: Mapping[str, float | None],
        *,
        k: int = 10,
        ticker_exclude: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the top-``k`` analogues by masked cosine similarity.

        Only features present in *both* the query and the indexed row
        contribute to the score (mask intersection). Rows with no
        overlapping observed dimensions get score 0 and rank last.
        """
        if not self.rows:
            return []
        q_z, q_mask, _ = self.standardizer.transform(query_features)

        scored: list[tuple[float, IndexedTrade]] = []
        for row in self.rows:
            if ticker_exclude and row.ticker.upper() == ticker_exclude.upper():
                continue
            sim = masked_cosine(q_z, q_mask, row.z_vec, row.mask)
            scored.append((sim, row))

        scored.sort(key=lambda t: t[0], reverse=True)
        out: list[dict[str, Any]] = []
        for sim, row in scored[: max(1, int(k))]:
            out.append(
                {
                    "trade_id": row.trade_id,
                    "ticker": row.ticker,
                    "closed_at": row.closed_at,
                    "similarity": round(float(sim), 4),
                    "features": row.features,
                    "outcome": row.outcome,
                }
            )
        return out

    def outcome_summary(self, neighbors: list[Mapping[str, Any]]) -> dict[str, Any]:
        """Aggregate outcomes across a neighbor list. Used by the search
        endpoint so the desk gets one quick read on what these analogues
        did historically."""
        n = len(neighbors)
        if n == 0:
            return {"n": 0}
        wins = losses = scratches = 0
        pnls: list[float] = []
        for x in neighbors:
            oc = (x.get("outcome") or {}).get("outcomeClass")
            if oc == "win":
                wins += 1
            elif oc == "loss":
                losses += 1
            elif oc == "scratch":
                scratches += 1
            pnl = (x.get("outcome") or {}).get("realizedPnl")
            if isinstance(pnl, (int, float)):
                pnls.append(float(pnl))
        decisive = wins + losses
        return {
            "n": n,
            "wins": wins,
            "losses": losses,
            "scratches": scratches,
            "win_rate": (wins / decisive) if decisive else None,
            "avg_pnl": (sum(pnls) / len(pnls)) if pnls else None,
            "n_with_pnl": len(pnls),
        }

    # ── Persistence ──

    def to_json(self) -> str:
        return json.dumps(
            {
                "engine": self.engine,
                "feature_names": self.feature_names,
                "standardizer": self.standardizer.to_json(),
                "rows": [
                    {
                        "trade_id": r.trade_id,
                        "ticker": r.ticker,
                        "closed_at": r.closed_at,
                        "z_vec": r.z_vec,
                        "mask": r.mask,
                        "features": r.features,
                        "outcome": r.outcome,
                    }
                    for r in self.rows
                ],
            }
        )

    @classmethod
    def from_json(cls, blob: str | None) -> "AnalogueIndex | None":
        if not blob:
            return None
        try:
            d = json.loads(blob)
        except Exception:
            return None
        rows = [
            IndexedTrade(
                trade_id=str(r.get("trade_id", "")),
                ticker=str(r.get("ticker", "")),
                closed_at=r.get("closed_at"),
                z_vec=[float(x) for x in (r.get("z_vec") or [])],
                mask=[int(x) for x in (r.get("mask") or [])],
                features={k: (float(v) if isinstance(v, (int, float)) else None)
                          for k, v in (r.get("features") or {}).items()},
                outcome=dict(r.get("outcome") or {}),
            )
            for r in (d.get("rows") or [])
        ]
        return cls(
            engine=str(d.get("engine") or "unknown"),
            standardizer=Standardizer.from_json(d.get("standardizer")),
            feature_names=list(d.get("feature_names") or []),
            rows=rows,
        )


# ── Build pipeline ─────────────────────────────────────────


def build_index_from_v1_trades(
    *,
    engine: str,
    trades: Iterable[Mapping[str, Any]],
    require_outcome: bool = True,
) -> tuple[AnalogueIndex, dict[str, Any]]:
    """Build an analogue index from an iterable of v1 trade documents.

    Returns ``(index, stats)`` where ``stats`` has counts of trades seen,
    indexed, skipped, and per-feature coverage so the operator can see at
    a glance which features are sparse in production data.
    """
    trades = list(trades)
    names = feature_names(engine)
    if not names:
        return AnalogueIndex(engine=engine, standardizer=Standardizer(), feature_names=[]), {
            "ok": False, "reason": f"unknown engine {engine!r}",
        }

    feature_rows: list[dict[str, float | None]] = []
    metadata_rows: list[dict[str, Any]] = []
    skipped = {"not_closed": 0, "no_outcome": 0, "all_features_missing": 0}

    for t in trades:
        if t.get("status") != "closed":
            skipped["not_closed"] += 1
            continue
        outcome = t.get("outcome") or {}
        if require_outcome and not outcome.get("outcomeClass"):
            skipped["no_outcome"] += 1
            continue
        feats = extract_features(t, engine)
        if not any(v is not None for v in feats.values()):
            skipped["all_features_missing"] += 1
            continue
        feature_rows.append(feats)
        metadata_rows.append(
            {
                "trade_id": str(t.get("tradeId") or ""),
                "ticker": str((t.get("ticker") or t.get("entry", {}).get("underlying") or "?")).upper(),
                "closed_at": t.get("closedAt"),
                "outcome": {
                    "outcomeClass": outcome.get("outcomeClass"),
                    "realizedPnl": outcome.get("realizedPnl"),
                    "holdDurationDays": outcome.get("holdDurationDays"),
                    "maxBreachProximity": outcome.get("maxBreachProximity"),
                },
            }
        )

    standardizer = Standardizer()
    standardizer.fit(feature_rows)

    rows: list[IndexedTrade] = []
    for feats, meta in zip(feature_rows, metadata_rows):
        z, mask, _ = standardizer.transform(feats)
        rows.append(
            IndexedTrade(
                trade_id=meta["trade_id"],
                ticker=meta["ticker"],
                closed_at=meta["closed_at"],
                z_vec=z,
                mask=mask,
                features=feats,
                outcome=meta["outcome"],
            )
        )

    coverage = _feature_coverage_helper(feature_rows, names)
    stats = {
        "ok": True,
        "engine": engine,
        "n_trades_seen": len(trades),
        "n_indexed": len(rows),
        "skipped": skipped,
        "feature_names": names,
        "feature_coverage": coverage,
    }
    index = AnalogueIndex(
        engine=engine,
        standardizer=standardizer,
        feature_names=names,
        rows=rows,
    )
    return index, stats


